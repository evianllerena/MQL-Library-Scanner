from __future__ import annotations
import argparse, concurrent.futures, ctypes, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, time, zipfile
from pathlib import Path

CREATE_NO_WINDOW=getattr(subprocess,'CREATE_NO_WINDOW',0)
SW_HIDE=0

RENDER_STARTUP_EXIT=42

class RenderStartupError(RuntimeError):
    pass

def mql_dir_name(kind):
    """MetaTrader source directory name for a platform kind.

    A MetaTrader data folder contains an ``MQL5`` (MT5) or ``MQL4`` (MT4)
    subdirectory — never ``MT5``/``MT4``. The platform *kind* string must be
    mapped through here anywhere the on-disk MQL directory is referenced.
    """
    return 'MQL5' if kind=='MT5' else 'MQL4'

def safe_job_id(value):
    value=re.sub(r'[^A-Za-z0-9_.-]+','-',str(value or '').strip()).strip('.-')
    return (value or hashlib.sha1(str(time.time_ns()).encode()).hexdigest()[:12])[:64]

class RenderLock:
    def __init__(self,path,timeout=5.0):
        self.path=Path(path);self.timeout=timeout;self.file=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.file=open(self.path,'a+b')
        self.file.seek(0,2)
        if self.file.tell()==0:self.file.write(b'0');self.file.flush()
        end=time.monotonic()+self.timeout
        while True:
            try:
                self.file.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                return self
            except (OSError,IOError):
                if time.monotonic()>=end:
                    self.file.close();self.file=None
                    raise RuntimeError('Preview engine busy — another MetaTrader preview is still active.')
                time.sleep(.1)
    def __exit__(self,_typ,_val,_tb):
        if self.file is None:return
        try:
            self.file.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_UN)
        except Exception:pass
        try:self.file.close()
        except Exception:pass
        self.file=None

def terminate_tree(proc):
    if proc is None:return
    try:
        if proc.poll() is not None:return
    except Exception:return
    if os.name=='nt':
        try:
            subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=CREATE_NO_WINDOW,timeout=8)
        except Exception:pass
    else:
        try:proc.terminate()
        except Exception:pass
        try:proc.wait(timeout=5)
        except Exception:
            try:proc.kill()
            except Exception:pass

def hard_kill(proc,runtime_dir):
    """Force-reap the isolated MetaTrader tree without touching the user's live terminal."""
    pid=getattr(proc,'pid',None)
    try:terminate_tree(proc)
    except Exception:pass
    if os.name=='nt' and pid:
        try:
            subprocess.run(['taskkill','/F','/T','/PID',str(pid)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=CREATE_NO_WINDOW,timeout=8)
        except Exception:pass
    try:
        import psutil
        rd=str(Path(runtime_dir).resolve()).lower().rstrip('\\/')+os.sep
        names={'terminal64.exe','terminal.exe','metaeditor64.exe','metaeditor.exe'}
        for p in psutil.process_iter(['pid','name','exe']):
            try:
                exe=str(p.info.get('exe') or '').lower()
                name=str(p.info.get('name') or '').lower()
                if exe and exe.startswith(rd) and name in names:
                    p.kill()
            except Exception:pass
    except Exception:pass


def emit_heartbeat(job_id,done=0,inflight=None,**details):
    payload={'type':'heartbeat','job_id':job_id,'ts':time.time(),'done':int(done or 0),'inflight':str(inflight) if inflight else None}
    payload.update(details);emit(payload)


def _prepare_offline_runtime(rt):
    """Suppress cloned-runtime UI/network distractions that can block unattended batches."""
    rt=Path(rt)
    for p in (
        rt/'profiles'/'lastprofile.ini',
        rt/'config'/'lastprofile.ini',
        rt/'config'/'accounts.dat',
        rt/'config'/'community.ini'
    ):
        try:p.unlink(missing_ok=True)
        except Exception:pass
    for base in (rt/'bases',):
        if not base.exists():continue
        for child in list(base.glob('*/news'))+list(base.glob('*/mail')):
            try:shutil.rmtree(child,ignore_errors=True)
            except Exception:pass


def _prepare_render_runtime(rt):
    """Remove transient UI state but preserve logged-in account and Algo-Trading config."""
    rt=Path(rt)
    for path in (
        rt/'profiles'/'lastprofile.ini',
        rt/'config'/'lastprofile.ini',
        rt/'config'/'community.ini'
    ):
        try:path.unlink(missing_ok=True)
        except Exception:pass
    for base in (rt/'bases',):
        if not base.exists():continue
        for child in list(base.glob('*/news'))+list(base.glob('*/mail')):
            try:shutil.rmtree(child,ignore_errors=True)
            except Exception:pass

def compile_has_errors(text):
    for m in re.finditer(r'(?i)(?:result\s*:\s*)?(\d+)\s+errors?\b',text or ''):
        try:
            if int(m.group(1))>0:return True
        except Exception:pass
    return False

def emit(o):
    b=(json.dumps(o,ensure_ascii=False,default=str)+'\n').encode('utf-8','backslashreplace')
    sys.stdout.buffer.write(b); sys.stdout.buffer.flush()

def emit_stage(job_id,name,message=None,**details):
    payload={'type':'stage','job_id':job_id,'stage':name}
    if message:payload['message']=message
    payload.update(details)
    emit(payload)

def read_text(p):
    if not p.exists(): return ''
    for enc in ('utf-16','utf-8','cp1252','latin1'):
        try:return p.read_text(encoding=enc,errors='ignore')
        except Exception:pass
    return ''

def decode_origin_bytes(data):
    encodings=('utf-16','utf-8-sig','cp1252','latin1') if data[:2] in (b'\xff\xfe',b'\xfe\xff') else ('utf-8-sig','cp1252','latin1','utf-16')
    for enc in encodings:
        try:
            text=data.decode(enc).replace('\x00','').strip().strip('"')
            if text and (':\\' in text or ':/' in text):
                return text
        except Exception:pass
    return ''

def read_origin(p):
    if not p.exists():return ''
    try:return decode_origin_bytes(p.read_bytes())
    except Exception:return ''

def data_activity(path):
    root=Path(path)
    newest=0
    for candidate in (root/'logs',root/'MQL4'/'Logs',root/'MQL5'/'Logs'):
        if not candidate.exists():continue
        try:
            for p in candidate.rglob('*.log'):
                try:newest=max(newest,p.stat().st_mtime_ns)
                except Exception:pass
        except Exception:pass
    return newest

def same_path(a,b):
    if not a or not b:return False
    try:return Path(a).resolve()==Path(b).resolve()
    except Exception:return os.path.normcase(os.path.abspath(str(a)))==os.path.normcase(os.path.abspath(str(b)))

def metatrader_data_root():
    return Path(os.environ.get('APPDATA',''))/'MetaQuotes'/'Terminal'


def _tree_has_file(root):
    root=Path(root)
    if not root.exists():return False
    try:
        for item in root.rglob('*'):
            if item.is_file():return True
    except Exception:pass
    return False

def _render_data_activity(root):
    root=Path(root); newest=0
    for candidate in (
        root/'config'/'accounts.dat',
        root/'config'/'terminal.ini',
        root/'config'/'common.ini',
        root/'bases',
        root/'history'
    ):
        try:newest=max(newest,candidate.stat().st_mtime_ns)
        except Exception:pass
    return newest

def _valid_mt5_render_data_dir(root):
    root=Path(root)
    accounts=root/'config'/'accounts.dat'
    history_ok=_tree_has_file(root/'bases') or _tree_has_file(root/'history')
    return root.is_dir() and accounts.is_file() and accounts.stat().st_size>0 and history_ok

def resolve_mt5_render_data_dir(configured=None):
    if configured:
        root=Path(os.path.expandvars(os.path.expanduser(str(configured).strip().strip('"'))))
        if not _valid_mt5_render_data_dir(root):
            raise RuntimeError(
                f'MT5 data folder is not usable for rendering: {root}. '
                'Expected config/accounts.dat plus non-empty bases/ or history/.')
        return root
    base=metatrader_data_root()
    candidates=[]
    if base.exists():
        for root in base.iterdir():
            if not root.is_dir():continue
            try:
                if _valid_mt5_render_data_dir(root):
                    candidates.append((_render_data_activity(root),root))
            except Exception:pass
    if not candidates:
        raise RuntimeError(
            'No logged-in MT5 data folder was found. Set the MT5 data folder in Settings '
            '(the MetaQuotes\\Terminal\\<hash> folder containing config\\accounts.dat and history).')
    candidates.sort(key=lambda x:x[0],reverse=True)
    return candidates[0][1]

def observed_data_folders(base=None):
    base=Path(base) if base is not None else metatrader_data_root()
    rows=[]
    if not base.exists():return rows
    for d in base.iterdir():
        if not d.is_dir():continue
        rows.append({
            'data_dir':str(d),
            'origin':read_origin(d/'origin.txt'),
            'has_mql4':(d/'MQL4').is_dir(),
            'has_mql5':(d/'MQL5').is_dir(),
            'activity_ns':data_activity(d)
        })
    return rows

def discover_data_dirs(base=None):
    rows=[]
    for item in observed_data_folders(base):
        for kind,key in (('MT4','has_mql4'),('MT5','has_mql5')):
            if not item[key]:continue
            rows.append({
                'kind':kind,
                'data_dir':item['data_dir'],
                'origin':item['origin'],
                'activity_ns':item['activity_ns'],
                'discovery':'data-folder'
            })
    return rows

def find_editor(install,kind):
    install=Path(install)
    names=('metaeditor64.exe','metaeditor.exe') if kind=='MT5' else ('metaeditor.exe','metaeditor64.exe')
    for name in names:
        p=install/name
        if p.is_file():return str(p)
    try:
        candidates=[p for p in install.iterdir() if p.is_file() and p.name.lower().startswith('metaeditor') and p.suffix.lower()=='.exe']
    except Exception:candidates=[]
    return str(candidates[0]) if candidates else None

def candidate_install_dirs(base=None,roots=None):
    seen=set(); out=[]
    for item in observed_data_folders(base):
        origin=item.get('origin')
        if not origin:continue
        key=os.path.normcase(str(origin))
        if key in seen:continue
        seen.add(key);out.append(Path(origin))
    if roots is None:
        roots=[Path(os.environ[x]) for x in ('ProgramFiles','ProgramFiles(x86)','LOCALAPPDATA') if os.environ.get(x) and Path(os.environ[x]).exists()]
    for root in roots:
        root=Path(root)
        if not root.exists():continue
        dirs=[]
        for pat in ('MetaTrader*','*MetaTrader*','*MT4*','*MT5*','*OANDA*'):
            try:dirs+=list(root.glob(pat))
            except Exception:pass
        try:dirs+=[x for x in root.iterdir() if x.is_dir()]
        except Exception:pass
        for install in dirs:
            key=os.path.normcase(str(install))
            if key in seen:continue
            seen.add(key);out.append(install)
    return out

def discover_installations(base=None,roots=None):
    out=[]; seen=set()
    for install in candidate_install_dirs(base,roots):
        for exe,kind in (('terminal.exe','MT4'),('terminal64.exe','MT5')):
            terminal=install/exe
            if not terminal.is_file():continue
            key=(kind,os.path.normcase(str(terminal)))
            if key in seen:continue
            seen.add(key)
            out.append({
                'kind':kind,
                'terminal':str(terminal),
                'editor':find_editor(install,kind),
                'install_dir':str(install),
                'terminal_discovery':'install-binary',
                'editor_discovery':'terminal-install-dir'
            })
    return out

def runtime_candidate_score(t):
    terminal_ok=bool(t.get('terminal') and Path(t['terminal']).is_file())
    editor_ok=bool(t.get('editor') and Path(t['editor']).is_file())
    data_ok=bool(t.get('data_dir') and Path(t['data_dir']).is_dir())
    mql_ok=bool(data_ok and (Path(t['data_dir'])/mql_dir_name(t['kind'])).is_dir())
    ready=terminal_ok and editor_ok and data_ok and mql_ok
    origin_match=t.get('pair_method')=='origin-match'
    return (1 if ready else 0,1 if origin_match else 0,1 if terminal_ok else 0,1 if editor_ok else 0,1 if data_ok else 0,int(t.get('activity_ns') or 0))

def pair_runtime_candidates(installs=None,data_dirs=None):
    installs=list(discover_installations() if installs is None else installs)
    data_dirs=list(discover_data_dirs() if data_dirs is None else data_dirs)
    out=[]
    for inst in installs:
        compatible=[d for d in data_dirs if d['kind']==inst['kind']]
        exact=[d for d in compatible if d.get('origin') and same_path(d['origin'],inst['install_dir'])]
        pool=exact or compatible
        data=max(pool,key=lambda x:x.get('activity_ns',0)) if pool else None
        row=dict(inst)
        row.update({
            'data_dir':data.get('data_dir') if data else None,
            'data_origin':data.get('origin') if data else None,
            'activity_ns':data.get('activity_ns',0) if data else 0,
            'pair_method':'origin-match' if exact else ('platform-fallback' if data else 'unpaired-install')
        })
        out.append(row)
    for data in data_dirs:
        if any(x['kind']==data['kind'] and x.get('data_dir')==data['data_dir'] for x in out):continue
        out.append({
            'kind':data['kind'],
            'terminal':None,
            'editor':None,
            'install_dir':None,
            'data_dir':data['data_dir'],
            'data_origin':data.get('origin'),
            'activity_ns':data.get('activity_ns',0),
            'pair_method':'unpaired-data'
        })
    out.sort(key=runtime_candidate_score,reverse=True)
    return out

def terminals(base=None,roots=None):
    data=discover_data_dirs(base)
    installs=discover_installations(base,roots)
    return pair_runtime_candidates(installs,data)

def detect():
    base=metatrader_data_root()
    data=discover_data_dirs(base)
    installs=discover_installations(base)
    emit({
        'ok':True,
        'installations':installs,
        'data_directories':data,
        'observed_data_folders':observed_data_folders(base),
        'terminals':pair_runtime_candidates(installs,data)
    })

def memory_stats(db):
    p=Path(db)
    if not p.exists():return emit({'ok':True,'corrections':0,'verified':0,'families':0,'latest':[]})
    c=sqlite3.connect(p)
    try:
        one=lambda q:c.execute(q).fetchone()[0]
        emit({'ok':True,'corrections':one('select count(*) from classification_memory'),'verified':one('select count(*) from indicators where human_verified=1'),'families':one("select count(distinct family_fingerprint) from classification_memory where family_fingerprint is not null and family_fingerprint!=''"),'latest':[]})
    finally:c.close()

def remove_source(db,source):
    p=Path(db); source=str(Path(source)); n=0
    if p.exists():
        c=sqlite3.connect(p)
        try:
            ids=[r[0] for r in c.execute('select id from indicators where path=? or path like ? or path like ?',(source,source.rstrip('\\/')+'\\%',source.rstrip('\\/')+'/%'))]
            n=len(ids)
            if ids:
                marks=','.join('?'*len(ids)); c.execute(f'delete from classification_memory where indicator_id in ({marks})',ids); c.execute(f'delete from indicators where id in ({marks})',ids)
            c.execute('delete from sources where path=?',(source,)); c.commit()
        finally:c.close()
    emit({'ok':True,'removed_indicators':n,'source':source})

def archive_reset(db):
    p=Path(db); app=p.parent; dl=Path(os.environ.get('USERPROFILE',str(app)))/'Downloads'; out=dl if dl.exists() else app; out.mkdir(parents=True,exist_ok=True)
    zpath=out/f'MQL_Indicator_Library_Archive_{int(time.time())}.zip'
    if p.exists():
        try:c=sqlite3.connect(p);c.execute('pragma wal_checkpoint(truncate)');c.close()
        except Exception:pass
    with zipfile.ZipFile(zpath,'w',zipfile.ZIP_DEFLATED) as z:
        for f in app.rglob('*'):
            if f.is_file():
                try:z.write(f,f.relative_to(app))
                except Exception:pass
    for f in (p,Path(str(p)+'-wal'),Path(str(p)+'-shm')):
        try:f.unlink(missing_ok=True)
        except Exception:pass
    for d in (app/'diagnostics',app/'previews',app/'preview-runtime'):
        if d.exists():shutil.rmtree(d,ignore_errors=True)
    emit({'ok':True,'archive':str(zpath),'app_dir':str(app)})

def safe_name(src):
    s=re.sub(r'\s+','_',src.stem.strip()); s=re.sub(r'[^A-Za-z0-9_.()#-]+','_',s).strip(' ._') or 'indicator'; return s[:100]+src.suffix.lower()

def compile_file(editor,src,mqlroot):
    expected=src.with_suffix('.ex5' if src.suffix.lower()=='.mq5' else '.ex4'); log=src.with_suffix('.log'); attempts=[]
    for rel in (False,True):
        try:log.unlink(missing_ok=True)
        except Exception:pass
        try:expected.unlink(missing_ok=True)
        except Exception:pass
        target=src.name if rel else str(src); cmd=[str(editor),f'/compile:{target}',f'/inc:{mqlroot}','/log']; attempts.append(' '.join(cmd))
        subprocess.run(cmd,cwd=str(src.parent if rel else editor.parent),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60,creationflags=CREATE_NO_WINDOW)
        end=time.monotonic()+3.0
        latest=''
        while time.monotonic()<end:
            if expected.exists() and expected.stat().st_size:return expected,attempts,read_text(log)
            latest=read_text(log)
            if compile_has_errors(latest):return None,attempts,latest
            time.sleep(.15)
        latest=read_text(log)
        if compile_has_errors(latest):return None,attempts,latest
    return None,attempts,read_text(log)

def existing_binary(src,ext):
    if src.suffix.lower()==ext and src.exists():return src
    p=src.with_suffix(ext)
    if p.exists() and p.stat().st_size:return p
    return None

def find_live_binary(src,ext,data_dir):
    """Find an already-compiled indicator binary (.ex4/.ex5) in the live terminal's
    Indicators tree, matched by the source file's name. Lets the preview reuse the
    working binary the terminal already runs when the source won't compile in isolation."""
    if not data_dir:return None
    try:base=Path(data_dir)/('MQL5' if ext=='.ex5' else 'MQL4')/'Indicators'
    except Exception:return None
    if not base.is_dir():return None
    name=src.stem+ext
    direct=base/name
    if direct.is_file() and direct.stat().st_size:return direct
    try:
        for cand in base.rglob(name):
            if cand.is_file() and cand.stat().st_size:return cand
    except Exception:pass
    return None

def copy_compiled_support(src,dst,kind):
    dst.mkdir(parents=True,exist_ok=True)
    if not src.exists():return
    ext='.ex5' if kind=='MT5' else '.ex4'
    for p in src.rglob('*'):
        if not p.is_file():continue
        if p.suffix.lower() not in {ext,'.mqh'}:continue
        try:
            target=dst/p.relative_to(src)
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(p,target)
        except Exception:pass

def seed_mql_runtime(t,rt,kind):
    """Create a valid minimum MQL tree first, then seed reusable support files."""
    live=Path(t['data_dir']); src=live/mql_dir_name(kind); dst=rt/mql_dir_name(kind); marker=rt/f'.{kind.lower()}-seed-v5'
    dst.mkdir(parents=True,exist_ok=True)
    (dst/'Indicators').mkdir(parents=True,exist_ok=True)
    (dst/'Scripts').mkdir(parents=True,exist_ok=True)
    (dst/'Files').mkdir(parents=True,exist_ok=True)
    if not src.is_dir():return False
    marker_valid=marker.exists() and (dst/'Indicators').is_dir()
    if not marker_valid:
        for name in ('Include','Libraries','Presets','Images'):
            s=src/name
            if s.exists():
                try:shutil.copytree(s,dst/name,dirs_exist_ok=True)
                except Exception:pass
        copy_compiled_support(src/'Indicators',dst/'Indicators',kind)
        marker.write_text(f'{src}\n{int(time.time())}',encoding='utf-8')
    preview_dir=dst/'Indicators'/'MQLLibraryPreview'
    if preview_dir.exists():shutil.rmtree(preview_dir,ignore_errors=True)
    return not marker_valid

def runtime_path(t,out,kind,worker_id=None):
    live=Path(t['data_dir'])
    terminal_src=Path(t['terminal'])
    token=hashlib.sha1((str(terminal_src)+'|'+str(live)).lower().encode()).hexdigest()[:10]
    worker_suffix=('-'+safe_job_id(worker_id)) if worker_id else ''
    return Path(out).parent/'preview-runtime'/'v5'/f'{kind.lower()}-{token}{worker_suffix}'

def clone_runtime(t,out,kind,worker_id=None):
    install=Path(t['install_dir'])
    live=Path(t['data_dir'])
    terminal_src=Path(t['terminal'])
    editor_src=Path(t['editor'])
    rt=runtime_path(t,out,kind,worker_id)
    stamp=f'v5:{terminal_src.stat().st_size}:{terminal_src.stat().st_mtime_ns}:{editor_src.stat().st_size}:{editor_src.stat().st_mtime_ns}:{str(live).lower()}'
    marker=rt/'.stamp'
    if not rt.exists() or not marker.exists() or marker.read_text(errors='ignore')!=stamp:
        shutil.rmtree(rt,ignore_errors=True)
        def ign(_d,n):return {x for x in n if x.lower() in {'logs','bases','history','mql4','mql5','profiles','templates','tester','config'}}
        shutil.copytree(install,rt,ignore=ign)
        for name in ('config','profiles','templates'):
            source=live/name
            if source.exists():
                try:shutil.copytree(source,rt/name,dirs_exist_ok=True)
                except Exception:pass
        marker.parent.mkdir(parents=True,exist_ok=True)
        marker.write_text(stamp,encoding='utf-8')
    seed_mql_runtime(t,rt,kind)
    required={
        'sandbox':rt.is_dir(),
        'sandbox_terminal':(rt/terminal_src.name).is_file(),
        'sandbox_metaeditor':(rt/editor_src.name).is_file(),
        'sandbox_mql_dir':(rt/mql_dir_name(kind)).is_dir(),
        'sandbox_indicators_dir':(rt/mql_dir_name(kind)/'Indicators').is_dir()
    }
    missing=[name for name,ok in required.items() if not ok]
    if missing:raise RuntimeError(f'Preview sandbox initialization failed for {kind}: {", ".join(missing)}')
    return rt

def stage(src,mql,kind,editor,data_dir=None,compiled_cache=None):
    ext='.ex5' if kind=='MT5' else '.ex4'; job=hashlib.sha1(str(src.resolve()).lower().encode()).hexdigest()[:14]; d=mql/'Indicators'/'MQLLibraryPreview'/job;d.mkdir(parents=True,exist_ok=True)
    staged=d/safe_name(src); shutil.copy2(src,staged)
    for dep in src.parent.glob('*.mqh'):
        try:shutil.copy2(dep,d/dep.name)
        except Exception:pass
    binary=d/(staged.stem+ext)
    cache=Path(compiled_cache) if compiled_cache else None
    if cache and cache.is_file() and cache.stat().st_size:
        shutil.copy2(cache,binary)
        return job,staged,binary,{'used_compiled_cache':True,'compiled_cache':str(cache)}
    old=existing_binary(src,ext) or find_live_binary(src,ext,data_dir)
    if old:
        shutil.copy2(old,binary)
        if cache:
            cache.parent.mkdir(parents=True,exist_ok=True)
            try:shutil.copy2(old,cache)
            except Exception:pass
        return job,staged,binary,{'used_existing_binary':True,'existing_binary':str(old)}
    if src.suffix.lower() not in ('.mq4','.mq5'):raise RuntimeError(f'Indicator compile failed — {kind} executable could not be staged.')
    built,cmds,log=compile_file(editor,staged,mql)
    if not built:
        if log:raise RuntimeError(f'Indicator compile failed — {kind} source produced no {ext.upper()[1:]}.\n{log[-2200:]}')
        raise RuntimeError(f'Indicator compile failed — {kind} MetaEditor produced no executable. Staged={staged}; commands={cmds}')
    if cache:
        cache.parent.mkdir(parents=True,exist_ok=True)
        try:shutil.copy2(built,cache)
        except Exception:pass
    return job,staged,built,{'used_existing_binary':False,'compiled_cache':str(cache) if cache else None}

def copy_mt4_history(live,rt):
    files=[]; h=live/'history'
    if h.exists():
        for p in h.rglob('*.hst'):
            try:files.append((p.stat().st_mtime_ns,p))
            except Exception:pass
    if not files:return 'EURUSD'
    _,p=max(files,key=lambda x:x[0]); m=re.match(r'(.+?)(1|5|15|30|60|240|1440|10080|43200)$',p.stem); sym=m.group(1) if m else 'EURUSD'; dest=rt/'history'/p.parent.name;dest.mkdir(parents=True,exist_ok=True)
    for f in p.parent.glob(sym+'*.hst'):
        try:shutil.copy2(f,dest/f.name)
        except Exception:pass
    return sym

def copy_mt5_history(live,rt):
    cand=[]; b=live/'bases'
    if b.exists():
        for d in b.glob('*/history/*'):
            if d.is_dir():
                try:cand.append((d.stat().st_mtime_ns,d))
                except Exception:pass
    if not cand:return 'EURUSD'
    pref=[x for x in cand if x[1].name.upper()=='EURUSD']; _,src=max(pref or cand,key=lambda x:x[0]); dst=rt/src.relative_to(live);dst.parent.mkdir(parents=True,exist_ok=True);shutil.rmtree(dst,ignore_errors=True)
    try:shutil.copytree(src,dst)
    except Exception:pass
    return src.name


def seed_render_runtime_from_data_dir(real_data,rt):
    real_data=resolve_mt5_render_data_dir(real_data)
    rt=Path(rt)
    cfg_src=real_data/'config'; cfg_dst=rt/'config'; cfg_dst.mkdir(parents=True,exist_ok=True)
    copied=[]
    for name in ('accounts.dat','servers.dat','common.ini','terminal.ini'):
        src=cfg_src/name
        if src.is_file():
            shutil.copy2(src,cfg_dst/name);copied.append(name)
    if not (cfg_dst/'accounts.dat').is_file():
        raise RuntimeError(f'Render seed is missing config/accounts.dat in {real_data}')
    origin=real_data/'origin.txt'
    if origin.is_file():
        try:shutil.copy2(origin,rt/'origin.txt')
        except Exception:pass
    symbol=copy_mt5_history(real_data,rt)
    if symbol=='EURUSD':
        # Keep the existing MT5 history copier as the primary source; legacy history is a fallback.
        legacy=real_data/'history'
        if legacy.exists() and not (rt/'history').exists():
            try:shutil.copytree(legacy,rt/'history',dirs_exist_ok=True)
            except Exception:pass
    return symbol,copied

def seed_render_dependencies_from_data_dir(real_data,rt):
    """Seed helper indicators/includes once per render-runtime build without touching preview work dirs."""
    real_data=resolve_mt5_render_data_dir(real_data)
    rt=Path(rt)
    src_mql=real_data/'MQL5'; dst_mql=rt/'MQL5'
    dst_mql.mkdir(parents=True,exist_ok=True)
    marker=rt/'.render-dependencies-seed-v1'
    stamp=rt/'.stamp'
    try:runtime_stamp=stamp.read_text(encoding='utf-8',errors='ignore').strip()
    except Exception:runtime_stamp=''
    key=f'v1\n{str(real_data.resolve()).lower()}\n{runtime_stamp}'
    if marker.is_file():
        try:
            if marker.read_text(encoding='utf-8',errors='ignore')==key:
                return {'cached':True,'source':str(real_data),'trees':[]}
        except Exception:pass

    copied=[]
    for name in ('Indicators','Include','Libraries','Files'):
        src=src_mql/name
        if not src.is_dir():continue
        dst=dst_mql/name
        def ignore_workdirs(path,names,root=src,name=name):
            if Path(path)==root:
                blocked=set()
                if name=='Indicators' and 'MQLLibraryPreview' in names:blocked.add('MQLLibraryPreview')
                if name=='Files' and 'MQLLibRender' in names:blocked.add('MQLLibRender')
                return blocked
            return set()
        shutil.copytree(src,dst,dirs_exist_ok=True,ignore=ignore_workdirs)
        copied.append(f'MQL5/{name}')

    common_src=real_data.parent/'Common'
    if common_src.is_dir():
        shutil.copytree(common_src,rt/'Common',dirs_exist_ok=True)
        copied.append('Common')

    marker.write_text(key,encoding='utf-8')
    return {'cached':False,'source':str(real_data),'trees':copied}

def startupinfo():
    if os.name!='nt':return None
    si=subprocess.STARTUPINFO();si.dwFlags|=getattr(subprocess,'STARTF_USESHOWWINDOW',1);si.wShowWindow=SW_HIDE;return si

def hide_pid(pid):
    if os.name!='nt':return
    u=ctypes.windll.user32
    @ctypes.WINFUNCTYPE(ctypes.c_bool,ctypes.c_void_p,ctypes.c_void_p)
    def cb(hwnd,_):
        p=ctypes.c_ulong();u.GetWindowThreadProcessId(hwnd,ctypes.byref(p))
        if p.value==pid and u.IsWindowVisible(hwnd):u.ShowWindow(hwnd,SW_HIDE)
        return True
    try:u.EnumWindows(cb,0)
    except Exception:pass

def wait_quiet(root,proc=None,minimum=4,quiet=3,timeout=35):
    start=time.time(); last_change=start; signature=None
    while time.time()-start<timeout:
        if proc is not None and proc.poll() is not None and time.time()-start<minimum:return False
        rows=[]
        if root.exists():
            for p in root.rglob('*.log'):
                try:rows.append((str(p),p.stat().st_size,p.stat().st_mtime_ns))
                except Exception:pass
        sig=tuple(sorted(rows))
        if sig!=signature:signature=sig;last_change=time.time()
        if time.time()-start>=minimum and time.time()-last_change>=quiet:return True
        time.sleep(.5)
    return False

def prime_mt5_runtime(rt,terminal,symbol='EURUSD',job_id=None):
    marker=rt/'.mt5-preview-prime-v3'
    if marker.exists():
        emit_stage(job_id,'mt5_prime_cached','MT5 preview runtime is already initialized.')
        return
    _prepare_offline_runtime(rt)
    cfg=rt/f'mql-prime-{safe_job_id(job_id)}.ini'
    cfg.write_text(f'[Common]\nNewsEnable=0\n\n[Experts]\nEnabled=1\nAllowLiveTrading=0\nAllowDllImport=0\n\n[StartUp]\nSymbol={symbol}\nPeriod=H1\n',encoding='utf-8')
    emit_stage(job_id,'mt5_prime_start','Initializing isolated MT5 runtime. First use can take longer while MetaTrader prepares its MQL5 files.')
    proc=subprocess.Popen([str(terminal),'/portable',f'/config:{cfg}'],cwd=str(rt),creationflags=CREATE_NO_WINDOW,startupinfo=startupinfo())
    start=time.monotonic()
    last_any_change=start
    last_meta_change=start
    last_all_sig=None
    last_meta_sig=None
    saw_recompile=False
    saw_meta_activity_after_recompile=False
    settled=False
    announced_recompile=False
    last_heartbeat=start-10
    try:
        while time.monotonic()-start<120:
            now=time.monotonic()
            if now-last_heartbeat>=5:
                emit_heartbeat(job_id,0,None,stage='mt5_prime')
                last_heartbeat=now
            hide_pid(proc.pid)
            if proc.poll() is not None and now-start<5:
                break
            rows=[]
            log_root=rt/'logs'
            if log_root.exists():
                for p in log_root.rglob('*.log'):
                    try:rows.append((str(p),p.stat().st_size,p.stat().st_mtime_ns))
                    except Exception:pass
            all_sig=tuple(sorted(rows))
            if all_sig!=last_all_sig:
                last_all_sig=all_sig
                last_any_change=now
            meta=log_root/'metaeditor.log'
            try:meta_sig=(meta.stat().st_size,meta.stat().st_mtime_ns)
            except Exception:meta_sig=None
            if meta_sig!=last_meta_sig:
                if saw_recompile and last_meta_sig is not None:
                    saw_meta_activity_after_recompile=True
                last_meta_sig=meta_sig
                last_meta_change=now
            tails=latest_logs(log_root)
            if not saw_recompile and 'full recompilation has been started' in tails.lower():
                saw_recompile=True
                last_meta_change=now
                if not announced_recompile:
                    emit_stage(job_id,'mt5_full_recompile','MetaTrader is performing its one-time MQL5 runtime compilation. Waiting for it to finish before rendering.')
                    announced_recompile=True
            elapsed=now-start
            if saw_recompile:
                if saw_meta_activity_after_recompile and elapsed>=8 and now-last_meta_change>=5:
                    settled=True
                    break
            elif elapsed>=8 and now-last_any_change>=5:
                settled=True
                break
            if int(elapsed)%15==0 and elapsed>=15:
                time.sleep(.55)
            else:
                time.sleep(.5)
    finally:
        hard_kill(proc,rt)
    if not settled:
        emit_stage(job_id,'mt5_prime_timeout','MT5 runtime initialization did not reach a verified idle state.',elapsed_ms=round((time.monotonic()-start)*1000))
        raise RuntimeError('MT5 preview runtime initialization timed out before MetaTrader became idle.')
    marker.write_text(f'ready-v3\n{int(time.time())}',encoding='utf-8')
    emit_stage(job_id,'mt5_prime_ready','MT5 preview runtime initialization completed.',elapsed_ms=round((time.monotonic()-start)*1000))

def wait_image(proc,paths,timeout=50):
    end=time.time()+timeout
    while time.time()<end:
        hide_pid(proc.pid)
        for p in paths:
            if p.exists() and p.stat().st_size:return p
        if proc.poll() is not None:return None
        time.sleep(.25)
    return None

def latest_logs(root):
    rows=[]
    if root.exists():
        for p in root.rglob('*.log'):
            try:rows.append((p.stat().st_mtime_ns,p))
            except Exception:pass
    return '\n\n'.join(f'[{p}]\n{read_text(p)[-2200:]}' for _,p in sorted(rows,reverse=True)[:4])

def preview_trace(logs):
    lines=[]
    for line in logs.splitlines():
        if 'MQLLIB_PREVIEW' in line:lines.append(line)
    return '\n'.join(lines[-20:])

def _mql_str(s):
    """Escape a Python string so it is a SAFE double-quoted MQL4/MQL5 literal.
    Backslash MUST be escaped before the quote."""
    return str(s).replace('\\', '\\\\').replace('"', '\\"')

# Guardrail: Any path or user-supplied string inserted into a generated MQL "..." literal
# must go through _mql_str() first.
def mt5_capture_source(rel, shot, win):
    rel_lit = _mql_str(rel)
    shot_lit = _mql_str(shot)
    return (
        'void OnStart(){\n' ' Print("MQLLIB_PREVIEW stage=onstart");\n'
        ' ResetLastError();\n'
        f' Print("MQLLIB_PREVIEW stage=before_iCustom path={rel_lit}");\n'
        f' int h=iCustom(_Symbol,_Period,"{rel_lit}");\n'
        ' int err=GetLastError();\n'
        ' PrintFormat("MQLLIB_PREVIEW stage=after_iCustom handle=%d err=%d",h,err);\n'
        ' if(h==INVALID_HANDLE){TerminalClose(21);return;}\n'
        f' int w={win};\n'
        ' if(w==1)w=(int)ChartGetInteger(0,CHART_WINDOWS_TOTAL);\n'
        ' ResetLastError();\n'
        ' bool added=ChartIndicatorAdd(0,w,h);\n'
        ' err=GetLastError();\n'
        ' PrintFormat("MQLLIB_PREVIEW stage=chart_add added=%s err=%d",added?"true":"false",err);\n'
        ' if(!added){IndicatorRelease(h);TerminalClose(22);return;}\n'
        ' ChartRedraw();\n'
        ' for(int i=0;i<20;i++){if(BarsCalculated(h)>=0)break;Sleep(250);}\n'
        ' Sleep(1500);\n'
        ' ResetLastError();\n'
        f' bool ok=ChartScreenShot(0,"{shot_lit}",1200,720,ALIGN_RIGHT);\n'
        ' err=GetLastError();\n'
        ' PrintFormat("MQLLIB_PREVIEW stage=screenshot ok=%s err=%d",ok?"true":"false",err);\n'
        ' IndicatorRelease(h);\n'
        ' Sleep(300);\n'
        ' TerminalClose(ok?0:23);\n'
        '}\n'
    )



RESIDENT_EA_SOURCE = r'''#property strict
input string Dir = "MQLLibRender";
long g_beat = 0;

void warmHistory(){
  MqlRates rr[];
  ArraySetAsSeries(rr,true);
  for(int i=0;i<40;i++){
    int got=CopyRates(_Symbol,_Period,0,3000,rr);
    if(got>=2000) break;
    Sleep(100);
  }
  ChartSetInteger(0,CHART_AUTOSCROLL,true);
  ChartNavigate(0,CHART_END,0);
  ChartRedraw(0);
}

int OnInit(){ warmHistory(); EventSetMillisecondTimer(150); return(INIT_SUCCEEDED); }
void OnDeinit(const int r){ EventKillTimer(); }

void beat(string inflight){
  g_beat++;
  int f=FileOpen(Dir+"\\heartbeat.txt",FILE_WRITE|FILE_TXT|FILE_ANSI);
  if(f!=INVALID_HANDLE){
    FileWrite(f,(string)g_beat+"\t"+inflight+"\t"+(string)(long)TimeCurrent());
    FileClose(f);
  }
}

void clearChart(){
  for(int w=(int)ChartGetInteger(0,CHART_WINDOWS_TOTAL)-1; w>=0; w--){
    int tt=ChartIndicatorsTotal(0,w);
    for(int i=tt-1;i>=0;i--){
      string nm=ChartIndicatorName(0,w,i);
      ChartIndicatorDelete(0,w,nm);
    }
  }
  ChartRedraw(0);
}

void writeDone(string id,bool ok,int err,string shot){
  int f=FileOpen(Dir+"\\current.done",FILE_WRITE|FILE_TXT|FILE_ANSI);
  if(f!=INVALID_HANDLE){
    FileWrite(f,id+"\t"+(ok?"ok":"fail")+"\t"+(string)err+"\t"+shot);
    FileClose(f);
  }
}

void OnTimer(){
  if(!FileIsExist(Dir+"\\current.job")){ beat(""); return; }
  int f=FileOpen(Dir+"\\current.job",FILE_READ|FILE_TXT|FILE_ANSI);
  if(f==INVALID_HANDLE){ beat(""); return; }
  string line=FileReadString(f); FileClose(f);
  string p[]; int n=StringSplit(line,(ushort)9,p);
  if(n<3){ FileDelete(Dir+"\\current.job"); beat(""); return; }
  string id=p[0], rel=p[1], shot=p[2];
  int win=(n>=4)?(int)StringToInteger(p[3]):0;

  beat(id);
  clearChart();

  // Self-test baseline: same resident terminal / chart pipeline, deliberately no indicator.
  if(rel=="__MQLLIB_EMPTY_CHART__"){
    ChartSetInteger(0,CHART_AUTOSCROLL,true);
    ChartNavigate(0,CHART_END,0);
    for(int i=0;i<6;i++){ beat(id); ChartRedraw(0); Sleep(200); }
    ResetLastError();
    bool empty_ok=ChartScreenShot(0,shot,1200,720,ALIGN_RIGHT);
    int empty_err=GetLastError();
    writeDone(id,empty_ok,empty_err,shot);
    FileDelete(Dir+"\\current.job");
    beat("");
    return;
  }

  ResetLastError();
  int h=iCustom(_Symbol,_Period,rel);
  int err=GetLastError();
  bool ok=false;
  if(h!=INVALID_HANDLE){
    int need=Bars(_Symbol,_Period); if(need<=0) need=500;
    int prev=-1, stable=0;
    for(int i=0;i<60;i++){
      int bc=BarsCalculated(h);
      beat(id);
      if(bc>0){
        if(bc>=need-2) break;
        if(bc==prev){ stable++; if(stable>=5) break; } else stable=0;
      }
      prev=bc; Sleep(200);
    }
    int w=win; if(w==1) w=(int)ChartGetInteger(0,CHART_WINDOWS_TOTAL);
    ResetLastError();
    bool added=ChartIndicatorAdd(0,w,h); err=GetLastError();
    if(added){
      ChartSetInteger(0,CHART_AUTOSCROLL,true);
      ChartNavigate(0,CHART_END,0);
      for(int i=0;i<6;i++){ beat(id); ChartRedraw(0); Sleep(200); }
      ResetLastError();
      ok=ChartScreenShot(0,shot,1200,720,ALIGN_RIGHT); err=GetLastError();
    }
    IndicatorRelease(h);
  }
  writeDone(id,ok,err,shot);
  FileDelete(Dir+"\\current.job");
  beat("");
}
'''


def _resident_indicator_rel(job,binary):
    return f'MQLLibraryPreview/{safe_job_id(job)}/{Path(binary).stem}'


def _render_runtime_ready_marker(rt,runtime_id):
    return Path(rt)/f'.render-runtime-ready-{safe_job_id(runtime_id)}'

def _wait_for_render_runtime(rt,runtime_id,timeout=120):
    marker=_render_runtime_ready_marker(rt,runtime_id)
    deadline=time.time()+max(5,int(timeout))
    while time.time()<deadline:
        if marker.is_file() and (Path(rt)/'MQL5'/'Indicators').is_dir():
            return marker
        time.sleep(.25)
    raise RuntimeError(f'Render runtime was not initialized for compile output: {rt}')

def _resident_job_paths(mql):
    root=Path(mql)/'Files'/'MQLLibRender'
    root.mkdir(parents=True,exist_ok=True)
    return root,root/'current.job',root/'current.done',root/'heartbeat.txt'


def _read_resident_done(done):
    try:
        parts=done.read_text(encoding='utf-8',errors='ignore').strip().split('\t')
        if len(parts)<4:return None
        return {'id':parts[0],'ok':parts[1].lower()=='ok','err':parts[2],'shot':parts[3]}
    except Exception:return None


def _read_resident_heartbeat(hb):
    try:
        parts=hb.read_text(encoding='utf-8',errors='ignore').strip().split('\t')
        if not parts:return None
        return {'beat':parts[0],'inflight':parts[1] if len(parts)>1 else '','epoch':parts[2] if len(parts)>2 else ''}
    except Exception:return None


def _wait_for_resident_startup(hb,proc,timeout=60):
    start=time.time()
    while time.time()-start<max(1,int(timeout)):
        if proc.poll() is not None:
            raise RenderStartupError(f'Render terminal exited before resident EA heartbeat (exit={proc.returncode}).')
        beat=_read_resident_heartbeat(hb)
        if beat and beat.get('beat'):
            return beat
        time.sleep(.5)
    raise RenderStartupError(
        'Render terminal started but the capture EA is not running. Usually means '
        'the MT5 clone has no account (login wizard is blocking) or Algo Trading is off. '
        'Set your MT5 data folder in Settings.')

def _install_resident_ea(mql,editor,job_id):
    experts=Path(mql)/'Experts';experts.mkdir(parents=True,exist_ok=True)
    src=experts/'MQLLibRenderServer.mq5';built=src.with_suffix('.ex5')
    changed=read_text(src)!=RESIDENT_EA_SOURCE
    if changed:
        src.write_text(RESIDENT_EA_SOURCE,encoding='utf-8')
        try:built.unlink(missing_ok=True)
        except Exception:pass
    if built.exists() and built.stat().st_size and built.stat().st_mtime_ns>=src.stat().st_mtime_ns:
        return built
    emit_stage(job_id,'resident_ea_compile','Compiling resident MQL5 render server EA.')
    built,_cmds,log=compile_file(editor,src,Path(mql))
    if not built:
        raise RuntimeError('Resident render EA failed to compile. '+(log or '')[-1800:])
    return built


def _launch_resident_terminal(rt,terminalexe,sym,job_id):
    _prepare_render_runtime(rt)
    ea=Path(rt)/'MQL5'/'Experts'/'MQLLibRenderServer.ex5'
    if not ea.is_file() or not ea.stat().st_size:
        raise RenderStartupError(f'Resident capture EA is missing or empty: {ea}')
    cfg=Path(rt)/f'render-server-{safe_job_id(job_id)}.ini'
    cfg_text=(
        '[Common]\nNewsEnable=0\n\n'
        '[Experts]\nEnabled=1\nAllowLiveTrading=1\nAllowDllImport=0\n\n'
        f'[StartUp]\nSymbol={sym}\nPeriod=H1\nExpert=MQLLibRenderServer\nShutdownTerminal=0\n')
    if 'Expert=MQLLibRenderServer' not in cfg_text:
        raise RenderStartupError('Render startup config does not reference MQLLibRenderServer exactly.')
    cfg.write_text(cfg_text,encoding='utf-8')
    return subprocess.Popen(
        [str(terminalexe),'/portable',f'/config:{cfg}'],
        cwd=str(rt),creationflags=CREATE_NO_WINDOW,startupinfo=startupinfo())


def _render_failure_message(err_code):
    code=str(err_code or '')
    if code=='4802':
        return 'indicator OnInit failed (err 4802) — needs dependencies/inputs'
    return f'render failed err={code}'


def _render_server_fail(con,row,err,max_attempts=2):
    sha=row['sha256']
    current=con.execute(
        "SELECT MAX(COALESCE(preview_attempts,0)) FROM indicators WHERE sha256=?",
        (sha,)).fetchone()[0] or 0
    status='failed' if current>=max_attempts else 'compiled'
    con.execute(
        "UPDATE indicators SET preview_status=?,preview_error=?,worker_id=NULL,"
        "preview_updated_at=datetime('now') WHERE sha256=?",
        (status,str(err)[:800],sha))
    con.commit()


def _render_server_claim(con,max_attempts=2):
    return con.execute(
        """
        WITH grouped AS (
          SELECT sha256,MIN(id) AS id,MAX(COALESCE(preview_priority,0)) AS pr
          FROM indicators i
          WHERE preview_status='compiled'
            AND COALESCE(preview_attempts,0)<?
            AND COALESCE(sha256,'')<>''
            AND (UPPER(platform) IN ('MQL5','MT5') OR LOWER(path) LIKE '%.mq5' OR LOWER(path) LIKE '%.ex5')
            AND NOT EXISTS(
              SELECT 1 FROM indicators x
              WHERE x.sha256=i.sha256 AND x.preview_status IN ('rendering','ready')
            )
          GROUP BY sha256
          ORDER BY pr DESC,id
          LIMIT 1
        )
        SELECT i.id,i.path,i.platform,i.sha256,g.pr
        FROM grouped g JOIN indicators i ON i.id=g.id
        """,(max_attempts,)).fetchone()



def _compile_pool_claim(con,limit):
    con.execute('BEGIN IMMEDIATE')
    try:
        rows=con.execute(
            """
            WITH grouped AS (
              SELECT sha256,MIN(id) AS id,MAX(COALESCE(preview_priority,0)) AS pr
              FROM indicators i
              WHERE preview_status='pending'
                AND COALESCE(sha256,'')<>''
                AND (UPPER(platform) IN ('MQL5','MT5') OR LOWER(path) LIKE '%.mq5' OR LOWER(path) LIKE '%.ex5')
                AND NOT EXISTS(
                  SELECT 1 FROM indicators x
                  WHERE x.sha256=i.sha256 AND x.preview_status IN ('compiling','compiled','rendering','ready')
                )
              GROUP BY sha256
              ORDER BY pr DESC,id
              LIMIT ?
            )
            SELECT i.id,i.path,i.platform,i.sha256
            FROM grouped g JOIN indicators i ON i.id=g.id
            ORDER BY g.pr DESC,i.id
            """,(max(1,int(limit)),)).fetchall()
        for r in rows:
            con.execute(
                "UPDATE indicators SET preview_status='compiling',worker_id='compile-pool',preview_error='' "
                "WHERE sha256=? AND preview_status='pending'",
                (r['sha256'],))
        con.commit()
        return rows
    except Exception:
        con.rollback();raise


def compile_pool(db,out,terminal=None,workers=4,job_id=None,runtime_id=None):
    job_id=safe_job_id(job_id);dest=Path(out);workers=max(1,min(int(workers),8))
    con=sqlite3.connect(db,timeout=30);con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL');con.execute('PRAGMA synchronous=NORMAL');con.execute('PRAGMA busy_timeout=5000')
    _ensure_preview_queue_schema(con,None)
    con.execute(
        "UPDATE indicators SET preview_status='pending',worker_id=NULL "
        "WHERE preview_status='compiling' AND worker_id='compile-pool'")
    con.execute(
        "UPDATE indicators SET preview_status='pending',preview_error='',preview_attempts=0,worker_id=NULL "
        "WHERE preview_status='failed' AND preview_error LIKE 'MT4 source in .mq5:%'")
    con.commit()

    sel=load_runtime_cache(dest,'MT5') or choose_runtime('MT5',terminal)
    shared_runtime_id=safe_job_id(runtime_id or job_id)
    rt=runtime_path(sel,dest,'MT5','render-server')
    _wait_for_render_runtime(rt,shared_runtime_id,120)
    mql=rt/'MQL5'
    editor=rt/Path(sel['editor']).name
    emit_stage(job_id,'compile_pool_start',f'Starting MT5 compile pool with {workers} workers in shared render runtime.',
               workers=workers,runtime=str(rt),runtime_id=shared_runtime_id)

    def compile_one(row):
        src=Path(row['path']);sha=row['sha256']
        cache=_compiled_cache_path(dest,'MT5',sha)
        if cache.exists() and cache.stat().st_size:
            return sha,True,'',str(cache),str(src)
        try:
            _job,_staged,binary,_meta=stage(src,mql,'MT5',editor,sel.get('data_dir'),cache)
            if not binary or not Path(binary).is_file() or not Path(binary).stat().st_size:
                return sha,False,'compile produced no EX5',None,str(src)
            return sha,True,'',str(cache),str(src)
        except Exception as e:
            return sha,False,f'{type(e).__name__}: {e}',None,str(src)

    completed=0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers,thread_name_prefix='mql-compile') as ex:
        while True:
            pause_reason=_worker_pause_reason(con,dest)
            if pause_reason:
                emit({'ok':True,'job_id':job_id,'paused':True,'reason':pause_reason,'component':'compile_pool'})
                break
            rows=_compile_pool_claim(con,max(workers*2,workers))
            if not rows:break
            futures=[ex.submit(compile_one,r) for r in rows]
            for fut in concurrent.futures.as_completed(futures):
                sha,ok,err,cache,source=fut.result()
                completed+=1
                if ok:
                    con.execute(
                        "UPDATE indicators SET preview_status='compiled',preview_error='',worker_id=NULL "
                        "WHERE sha256=? AND preview_status='compiling'",
                        (sha,))
                    emit_stage(job_id,'compile_ready',f'Compiled {Path(source).name}',source=source,sha256=sha,cache=cache)
                else:
                    con.execute(
                        "UPDATE indicators SET preview_status='failed',preview_error=?,worker_id=NULL,"
                        "preview_updated_at=datetime('now') WHERE sha256=?",
                        (('compile: '+err)[:800],sha))
                    emit_stage(job_id,'compile_failed',f'Compile failed: {Path(source).name}',source=source,sha256=sha,error=err[:800])
                con.commit()
                emit_heartbeat(job_id,completed,source,stage='compile_pool')
                pending=con.execute(
                    "SELECT COUNT(*) FROM indicators WHERE preview_status IN ('pending','compiling') "
                    "AND (UPPER(platform) IN ('MQL5','MT5') OR LOWER(path) LIKE '%.mq5' OR LOWER(path) LIKE '%.ex5')"
                ).fetchone()[0]
                emit({'type':'compile_progress','job_id':job_id,'done':completed,'remaining':pending})

    con.close()
    emit({'ok':True,'job_id':job_id,'component':'compile_pool','finished':True,'compiled_done':completed})


def _capture_resident_baseline(cur,done,hb,proc,mql,target,hang_timeout=40):
    """Capture a candles-only reference through the same resident MT5 terminal used by production renders."""
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    baseline_id='selftest-baseline'
    shot='MQLLibRender/selftest_empty_chart.png'
    shotfile=Path(mql)/'Files'/shot
    shotfile.parent.mkdir(parents=True,exist_ok=True)
    for p in (done,shotfile):
        try:Path(p).unlink(missing_ok=True)
        except Exception:pass
    cur.write_text(f'{baseline_id}\t__MQLLIB_EMPTY_CHART__\t{shot}\t0',encoding='utf-8')
    deadline=time.time()+max(40,int(hang_timeout or 40))
    last_beat=None;last_change=time.time()
    while time.time()<deadline:
        payload=_read_resident_done(done) if Path(done).exists() else None
        if payload and payload.get('id')==baseline_id:
            if payload.get('ok') and shotfile.is_file() and shotfile.stat().st_size:
                shutil.copy2(shotfile,target)
                return target
            raise RuntimeError(f"Empty-chart baseline capture failed err={payload.get('err')}")
        beat=_read_resident_heartbeat(hb)
        beat_key=(beat or {}).get('beat')
        if beat_key!=last_beat:
            last_beat=beat_key;last_change=time.time()
        if proc.poll() is not None:
            raise RuntimeError(f'Empty-chart baseline terminal exited early ({proc.returncode}).')
        if time.time()-last_change>max(40,int(hang_timeout or 40)):
            break
        time.sleep(.25)
    raise TimeoutError('Empty-chart baseline capture timed out in the resident render pipeline.')


def render_server(db,out,terminal=None,job_id=None,hang_timeout=40,max_attempts=2,mt5_data_dir=None,runtime_id=None,baseline_target=None):
    hang_timeout=max(40,int(hang_timeout or 40))
    job_id=safe_job_id(job_id);dest=Path(out)
    con=sqlite3.connect(db,timeout=30);con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL');con.execute('PRAGMA synchronous=NORMAL');con.execute('PRAGMA busy_timeout=5000')
    _ensure_preview_queue_schema(con,None)
    con.execute("UPDATE indicators SET preview_status='compiled',worker_id=NULL WHERE preview_status='rendering' AND (UPPER(platform) IN ('MQL5','MT5') OR LOWER(path) LIKE '%.mq5' OR LOWER(path) LIKE '%.ex5')")
    con.execute(
        "UPDATE indicators SET preview_status='pending',preview_error='',preview_attempts=0,worker_id=NULL "
        "WHERE preview_status='failed' AND preview_error LIKE 'MT4 source in .mq5:%'")
    con.commit()
    _propagate_existing_ready(con)

    sel=load_runtime_cache(dest,'MT5') or choose_runtime('MT5',terminal)
    try:
        render_data=resolve_mt5_render_data_dir(mt5_data_dir)
        rt=clone_runtime(sel,dest,'MT5','render-server')
        hard_kill(None,rt)
        sym,copied_cfg=seed_render_runtime_from_data_dir(render_data,rt)
        emit_stage(job_id,'render_runtime_seeded','Seeded render runtime from logged-in MT5 data folder.',
                   data_dir=str(render_data),symbol=sym,config_files=copied_cfg)
        dependency_seed=seed_render_dependencies_from_data_dir(render_data,rt)
        emit_stage(job_id,'render_dependencies_seeded','Seeded real MT5 indicator/include dependencies into render runtime.',
                   data_dir=str(render_data),cached=dependency_seed.get('cached',False),
                   trees=dependency_seed.get('trees',[]))
        mql=rt/'MQL5'
        files,cur,done,hb=_resident_job_paths(mql)
        editor=rt/Path(sel['editor']).name
        terminalexe=rt/Path(sel['terminal']).name
        resident_ex5=_install_resident_ea(mql,editor,job_id)
        if not resident_ex5 or not Path(resident_ex5).is_file() or not Path(resident_ex5).stat().st_size:
            raise RenderStartupError('Resident capture EA did not compile to MQLLibRenderServer.ex5.')
        shared_runtime_id=safe_job_id(runtime_id or job_id)
        ready_marker=_render_runtime_ready_marker(rt,shared_runtime_id)
        for stale in rt.glob('.render-runtime-ready-*'):
            if stale!=ready_marker:
                try:stale.unlink(missing_ok=True)
                except Exception:pass
        ready_marker.write_text(str(time.time()),encoding='utf-8')
        emit_stage(job_id,'render_runtime_ready','Shared render runtime is ready for compile output.',
                   runtime=str(rt),runtime_id=shared_runtime_id)
    except Exception as e:
        msg=str(e)
        emit({'type':'fatal','job_id':job_id,'component':'render-server',
              'fatal_kind':'render_startup_config','exit_code':RENDER_STARTUP_EXIT,'error':msg})
        con.close()
        raise SystemExit(RENDER_STARTUP_EXIT)

    for p in (cur,done,hb):
        try:p.unlink(missing_ok=True)
        except Exception:pass

    def launch():
        for path in (cur,done,hb):
            try:path.unlink(missing_ok=True)
            except Exception:pass
        proc=_launch_resident_terminal(rt,terminalexe,sym,job_id)
        emit_stage(job_id,'resident_terminal_launch','Launching warm MT5 render terminal; waiting for resident EA heartbeat.',runtime=str(rt))
        try:
            beat=_wait_for_resident_startup(hb,proc,60)
        except RenderStartupError:
            hard_kill(proc,rt)
            raise
        emit_stage(job_id,'resident_ea_ready','Resident capture EA heartbeat received; render server is ready.',
                   ea_beat=beat.get('beat'),inflight=beat.get('inflight'))
        return proc

    proc=None
    last_host_heartbeat=0.0
    idle_since=time.time()
    relaunch_counts={}

    try:
        proc=launch()
        if baseline_target:
            emit_stage(job_id,'selftest_baseline_capture','Capturing empty-chart reference through resident render terminal.')
            _capture_resident_baseline(cur,done,hb,proc,mql,baseline_target,hang_timeout)
            emit_stage(job_id,'selftest_baseline_ready','Empty-chart reference captured.',path=str(baseline_target))
        while True:
            pause_reason=_worker_pause_reason(con,dest)
            if pause_reason:
                emit({'ok':True,'job_id':job_id,'paused':True,'reason':pause_reason})
                break

            row=_render_server_claim(con,max_attempts)
            if not row:
                remaining=con.execute(
                    "SELECT COUNT(*) FROM indicators WHERE preview_status IN ('pending','compiling','compiled') "
                    "AND (UPPER(platform) IN ('MQL5','MT5') OR LOWER(path) LIKE '%.mq5' OR LOWER(path) LIKE '%.ex5')"
                ).fetchone()[0]
                now=time.time()
                if now-last_host_heartbeat>=5:
                    ready=con.execute("SELECT COUNT(*) FROM indicators WHERE preview_status='ready'").fetchone()[0]
                    emit_heartbeat(job_id,ready,None,stage='render_server_idle',remaining=remaining)
                    last_host_heartbeat=now
                if remaining<=0:break
                if proc.poll() is not None:
                    hard_kill(proc,rt);proc=launch()
                time.sleep(.5);continue

            src=Path(row['path']);sha=row['sha256']
            cache=_compiled_cache_path(dest,'MT5',sha)
            if row['id'] is None:raise RuntimeError('Render claim returned no representative row.')
            if not cache.exists() or not cache.stat().st_size:
                con.execute("UPDATE indicators SET preview_status='pending',worker_id=NULL,preview_error='compiled cache missing; returned to compile pool' WHERE sha256=?",(sha,));con.commit()
                continue
            try:
                job,staged,binary,_meta=stage(src,mql,'MT5',editor,sel.get('data_dir'),cache)
            except Exception as e:
                con.execute(
                    "UPDATE indicators SET preview_status='pending',preview_error=?,worker_id=NULL WHERE sha256=?",
                    (('stage compiled cache: '+str(e))[:800],sha));con.commit()
                continue

            rel=_resident_indicator_rel(job,binary)
            expected_ex5=mql/'Indicators'/'MQLLibraryPreview'/job/(Path(binary).stem+'.ex5')
            if not expected_ex5.is_file() or not expected_ex5.stat().st_size:
                err=f'ex5 missing in render runtime: {expected_ex5}'
                con.execute(
                    "UPDATE indicators SET preview_status='failed',preview_error=?,worker_id=NULL,"
                    "preview_updated_at=datetime('now') WHERE sha256=?",
                    (err[:800],sha));con.commit()
                emit_stage(job_id,'render_artifact_missing','Compiled indicator is not staged in the render runtime.',
                           source=str(src),expected_ex5=str(expected_ex5),sha256=sha)
                continue
            shot=f'MQLLibRender/out_{job}.png'
            shotfile=mql/'Files'/shot
            shotfile.parent.mkdir(parents=True,exist_ok=True)
            try:shotfile.unlink(missing_ok=True)
            except Exception:pass
            win='1' if 'indicator_separate_window' in read_text(src).lower() else '0'
            con.execute(
                "UPDATE indicators SET preview_status='rendering',worker_id='render-server',"
                "preview_attempts=COALESCE(preview_attempts,0)+1 WHERE sha256=?",
                (sha,));con.commit()

            try:done.unlink(missing_ok=True)
            except Exception:pass
            cur.write_text(f"{row['id']}\t{rel}\t{shot}\t{win}",encoding='utf-8')
            emit_stage(job_id,'resident_job_queued',f'Rendering {src.name} in warm MT5 terminal.',source=str(src),rel=rel)

            deadline=time.time()+hang_timeout
            last_beat=None;last_change=time.time();result=None;done_payload=None
            while time.time()<deadline:
                done_payload=_read_resident_done(done) if done.exists() else None
                if done_payload and done_payload.get('id')==str(row['id']):
                    result=bool(done_payload.get('ok'));break
                beat=_read_resident_heartbeat(hb)
                beat_key=(beat or {}).get('beat')
                if beat_key!=last_beat:
                    last_beat=beat_key;last_change=time.time()
                now=time.time()
                if now-last_host_heartbeat>=5:
                    ready=con.execute("SELECT COUNT(*) FROM indicators WHERE preview_status='ready'").fetchone()[0]
                    emit_heartbeat(job_id,ready,str(src),stage='render_server',ea_beat=beat_key)
                    last_host_heartbeat=now
                if now-last_change>hang_timeout:break
                if proc.poll() is not None:break
                time.sleep(.4)

            if result and shotfile.exists() and shotfile.stat().st_size:
                final=_store_preview_image(shotfile,dest,sha);_make_thumb(final)
                con.execute(
                    "UPDATE indicators SET preview_status='ready',preview_path=?,preview_hash=?,preview_error='',"
                    "preview_updated_at=datetime('now'),preview_priority=0,worker_id=NULL WHERE sha256=?",
                    (str(final),sha,sha));con.commit()
            else:
                if done_payload and not done_payload.get('ok'):
                    err_code=str(done_payload.get('err') or '')
                    err=_render_failure_message(err_code)
                    _render_server_fail(con,row,err,max_attempts)
                    emit_stage(job_id,'resident_job_failed',f'Resident EA completed with failure: {src.name}',
                               source=str(src),error=err,error_code=err_code)
                else:
                    err='render hang/timeout'
                    _render_server_fail(con,row,err,max_attempts)
                    relaunch_counts[sha]=relaunch_counts.get(sha,0)+1
                    count=relaunch_counts[sha]
                    if count>=3:
                        con.execute(
                            "UPDATE indicators SET preview_status='failed',preview_error=?,worker_id=NULL,"
                            "preview_updated_at=datetime('now') WHERE sha256=?",
                            (f'{err}; relaunch cap reached ({count}/3)',sha));con.commit()
                    emit_stage(job_id,'resident_job_hang',f'Resident EA heartbeat froze mid-job: {src.name}',
                               source=str(src),relaunch_count=count,relaunch_cap=3)
                    hard_kill(proc,rt)
                    try:cur.unlink(missing_ok=True)
                    except Exception:pass
                    proc=launch()

            ready=con.execute("SELECT COUNT(*) FROM indicators WHERE preview_status='ready'").fetchone()[0]
            total=con.execute("SELECT COUNT(*) FROM indicators").fetchone()[0]
            emit({'type':'progress','job_id':job_id,'done':ready,'total':total})
            idle_since=time.time()
    except RenderStartupError as e:
        msg=str(e)
        emit({'type':'fatal','job_id':job_id,'component':'render-server',
              'fatal_kind':'render_startup_config','exit_code':RENDER_STARTUP_EXIT,'error':msg})
        raise SystemExit(RENDER_STARTUP_EXIT)
    finally:
        hard_kill(proc,rt)
        con.close()
    emit({'ok':True,'job_id':job_id,'finished':True})


def mt5_batch_capture_source(manifest_rel, done_marker):
    """One script that renders every indicator listed in a manifest file.

    manifest_rel : path (relative to MQL5\\Files) of a UTF-8 text file with one
                   record per line:  rel<TAB>shot<TAB>win
    done_marker  : filename (in MQL5\\Files) written when the whole batch is done,
                   so the Python side knows to stop waiting.
    """
    manifest_lit = _mql_str(manifest_rel)
    done_lit = _mql_str(done_marker)
    return (
        'void _clear_all(){\n'
        '  for(int w=(int)ChartGetInteger(0,CHART_WINDOWS_TOTAL)-1;w>=0;w--){\n'
        '    int total=ChartIndicatorsTotal(0,w);\n'
        '    for(int i=total-1;i>=0;i--){\n'
        '      string nm=ChartIndicatorName(0,w,i);\n'
        '      ChartIndicatorDelete(0,w,nm);\n'
        '    }\n'
        '  }\n'
        '  ChartRedraw();\n'
        '}\n'
        'void OnStart(){\n'
        '  Print("MQLLIB_PREVIEW stage=batch_start");\n'
        f'  int fh=FileOpen("{manifest_lit}",FILE_READ|FILE_TXT|FILE_ANSI);\n'
        '  if(fh==INVALID_HANDLE){Print("MQLLIB_PREVIEW stage=manifest_missing err=",GetLastError());TerminalClose(31);return;}\n'
        '  while(!FileIsEnding(fh)){\n'
        '    string line=FileReadString(fh);\n'
        '    if(StringLen(line)==0) continue;\n'
        '    string parts[]; int n=StringSplit(line,(ushort)9,parts);\n'
        '    if(n<2) continue;\n'
        '    string rel=parts[0]; string shot=parts[1];\n'
        '    int win=(n>=3)?(int)StringToInteger(parts[2]):0;\n'
        '    _clear_all();\n'
        '    ResetLastError();\n'
        '    int h=iCustom(_Symbol,_Period,rel);\n'
        '    int err=GetLastError();\n'
        '    PrintFormat("MQLLIB_PREVIEW stage=item rel=%s handle=%d err=%d",rel,h,err);\n'
        '    if(h==INVALID_HANDLE){ continue; }\n'
        '    int w=win; if(w==1) w=(int)ChartGetInteger(0,CHART_WINDOWS_TOTAL);\n'
        '    bool added=ChartIndicatorAdd(0,w,h);\n'
        '    if(!added){ IndicatorRelease(h); continue; }\n'
        '    ChartRedraw();\n'
        '    for(int i=0;i<20;i++){ if(BarsCalculated(h)>=0) break; Sleep(200); }\n'
        '    Sleep(900);\n'
        '    ResetLastError();\n'
        '    bool ok=ChartScreenShot(0,shot,1200,720,ALIGN_RIGHT);\n'
        '    PrintFormat("MQLLIB_PREVIEW stage=item_shot shot=%s ok=%s err=%d",shot,ok?"true":"false",GetLastError());\n'
        '    IndicatorRelease(h);\n'
        '    Sleep(150);\n'
        '  }\n'
        '  FileClose(fh);\n'
        f'  int dm=FileOpen("{done_lit}",FILE_WRITE|FILE_TXT|FILE_ANSI);\n'
        '  if(dm!=INVALID_HANDLE){ FileWrite(dm,"done"); FileClose(dm); }\n'
        '  Print("MQLLIB_PREVIEW stage=batch_done");\n'
        '  Sleep(300);\n'
        '  TerminalClose(0);\n'
        '}\n'
    )


def render_mt5_batch(sources, out, t, job_id):
    """Render many MT5 indicators with a SINGLE terminal launch.

    \`sources\` is a list of file paths (str/Path). Returns a dict:
        { "<source path>": {"ok":True,"image":...} | {"ok":False,"error":...} }

    NOTE: relies on helpers already in preview_bridge.py:
      clone_runtime, copy_mt5_history, prime_mt5_runtime, stage, read_text,
      compile_file, wait_image, startupinfo, terminate_tree, latest_logs,
      emit, emit_stage, CREATE_NO_WINDOW
    """
    out = Path(out)
    results = {}
    emit_stage(job_id, 'runtime_clone', 'Preparing isolated MT5 runtime (batch).')
    rt = clone_runtime(t, out, 'MT5')
    mql = rt / 'MQL5'
    scripts = mql / 'Scripts'
    files = mql / 'Files'
    for d in (scripts, files):
        d.mkdir(parents=True, exist_ok=True)
    editor = rt / Path(t['editor']).name
    terminal = rt / Path(t['terminal']).name
    sym = copy_mt5_history(Path(t['data_dir']), rt)

    # Prime ONCE for the whole batch (this is the slow one-time step).
    prime_mt5_runtime(rt, terminal, sym, job_id)

    # Stage + compile every indicator. Failures are recorded, not fatal.
    manifest_lines = []
    plan = []  # (source, shot_path_in_files, rel)
    for i, source in enumerate(sources):
        src = Path(source)
        try:
            emit_stage(job_id, 'indicator_compile', f'[{i+1}/{len(sources)}] Compiling {src.name}', source=str(src))
            job, staged, binary, meta = stage(src, mql, 'MT5', editor)
            rel = f'MQLLibraryPreview/{job}/{binary.stem}'   # forward slash: no escaping needed
            shot = f'MQLLibraryPreview_{job}.png'
            sep = 'indicator_separate_window' in read_text(src).lower()
            win = '1' if sep else '0'
            manifest_lines.append(f'{rel}\t{shot}\t{win}')
            plan.append((str(src), files / shot, rel))
            # clear any stale screenshot
            try:(files / shot).unlink(missing_ok=True)
            except Exception:pass
        except Exception as e:
            results[str(src)] = {'ok': False, 'error': f'{type(e).__name__}: {e}'}

    if not plan:
        return results  # nothing compiled

    # Write manifest into MQL5\\Files (that's the sandbox for FileOpen).
    manifest_name = f'mqllib_batch_{job_id}.txt'
    done_marker = f'mqllib_batch_{job_id}.done'
    (files / manifest_name).write_text('\n'.join(manifest_lines), encoding='utf-8')
    try:(files / done_marker).unlink(missing_ok=True)
    except Exception:pass

    # Compile the batch capture script ONCE.
    cap = scripts / f'MQLLibraryBatchCapture_{job_id}.mq5'
    cap.write_text(mt5_batch_capture_source(manifest_name, done_marker), encoding='utf-8')
    emit_stage(job_id, 'capture_compile', 'Compiling MT5 batch capture script.')
    built, _, log = compile_file(editor, cap, mql)
    if not built:
        raise RuntimeError('Batch capture script failed to compile. ' + (log or '')[-1600:])

    # Launch the terminal ONCE.
    cfg = rt / f'mql-batch-{job_id}.ini'
    cfg.write_text(
        '[Experts]\nEnabled=1\nAllowLiveTrading=0\nAllowDllImport=0\n\n'
        f'[StartUp]\nSymbol={sym}\nPeriod=H1\nScript={cap.stem}\nShutdownTerminal=1\n',
        encoding='utf-8')
    emit_stage(job_id, 'terminal_launch', f'Launching ONE MT5 terminal for {len(plan)} indicators.')
    proc = subprocess.Popen([str(terminal), '/portable', f'/config:{cfg}'],
                            cwd=str(rt), creationflags=CREATE_NO_WINDOW, startupinfo=startupinfo())

    # Wait for the done-marker (generous timeout scaled to batch size).
    done_path = files / done_marker
    timeout = 60 + 12 * len(plan)
    end = time.time() + timeout
    while time.time() < end:
        if done_path.exists():
            break
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    terminate_tree(proc)

    # Collect screenshots.
    for src_path, shot_path, rel in plan:
        if shot_path.exists() and shot_path.stat().st_size:
            final = out / (hashlib.sha1(('MT5|' + str(Path(src_path).resolve()).lower()).encode()).hexdigest()[:16] + '.png')
            out.mkdir(parents=True, exist_ok=True)
            shutil.copy2(shot_path, final)
            results[src_path] = {'ok': True, 'image': str(final), 'kind': 'MT5'}
        else:
            results.setdefault(src_path, {'ok': False, 'error': 'No screenshot produced (indicator likely could not load on the chart).'})

    emit({'ok': True, 'batch': True, 'kind': 'MT5', 'job_id': job_id,
          'rendered': sum(1 for v in results.values() if v.get('ok')),
          'failed': sum(1 for v in results.values() if not v.get('ok')),
          'results': results})
    return results


def mt5_batch_capture_source_v2(manifest_rel, cur_prefix, done_marker):
    m=_mql_str(manifest_rel); cur=_mql_str(cur_prefix); done=_mql_str(done_marker)
    return (
        'void _clear(){for(int w=(int)ChartGetInteger(0,CHART_WINDOWS_TOTAL)-1;w>=0;w--){'
        'int tt=ChartIndicatorsTotal(0,w);for(int i=tt-1;i>=0;i--)ChartIndicatorDelete(0,w,ChartIndicatorName(0,w,i));}ChartRedraw();}\n'
        'void _mark(string suf){int f=FileOpen(suf,FILE_WRITE|FILE_TXT|FILE_ANSI);if(f!=INVALID_HANDLE){FileWrite(f,"x");FileClose(f);}}\n'
        'void OnStart(){\n'
        f' int fh=FileOpen("{m}",FILE_READ|FILE_TXT|FILE_ANSI);\n'
        ' if(fh==INVALID_HANDLE){TerminalClose(31);return;}\n'
        ' int idx=0;\n'
        ' while(!FileIsEnding(fh)){\n'
        '   string line=FileReadString(fh); if(StringLen(line)==0) continue;\n'
        '   string p[]; int n=StringSplit(line,(ushort)9,p); if(n<2) continue;\n'
        '   string rel=p[0]; string shot=p[1]; int win=(n>=3)?(int)StringToInteger(p[2]):0;\n'
        f'  _mark("{cur}"+IntegerToString(idx)+".cur");\n'
        '   _clear(); ResetLastError();\n'
        '   int h=iCustom(_Symbol,_Period,rel);\n'
        '   if(h!=INVALID_HANDLE){int w=win; if(w==1)w=(int)ChartGetInteger(0,CHART_WINDOWS_TOTAL);\n'
        '     if(ChartIndicatorAdd(0,w,h)){ChartRedraw();\n'
        '       for(int i=0;i<10;i++){if(BarsCalculated(h)>=0)break;Sleep(150);} Sleep(400);\n'
        '       ChartScreenShot(0,shot,1200,720,ALIGN_RIGHT);} IndicatorRelease(h);}\n'
        f'  _mark(shot+".ok");\n'
        '   idx++; Sleep(120);\n'
        ' }\n'
        f' FileClose(fh); _mark("{done}");\n'
        ' Sleep(200); TerminalClose(0);\n'
        '}\n'
    )


def _cache_image_path(dest, source_hash):
    digest=str(source_hash or '').lower().strip()
    if not re.fullmatch(r'[0-9a-f]{64}',digest):
        digest=hashlib.sha256(digest.encode('utf-8','ignore')).hexdigest()
    shard=Path(dest)/digest[:2]
    shard.mkdir(parents=True,exist_ok=True)
    return shard/(digest+'.png')


def _compiled_cache_path(dest, kind, source_hash):
    digest=str(source_hash or '').lower().strip()
    if not re.fullmatch(r'[0-9a-f]{64}',digest):
        digest=hashlib.sha256(digest.encode('utf-8','ignore')).hexdigest()
    ext='.ex5' if kind=='MT5' else '.ex4'
    root=Path(dest).parent/'preview-runtime'/'compiled-cache'/kind.lower()/digest[:2]
    root.mkdir(parents=True,exist_ok=True)
    return root/(digest+ext)


def _store_preview_image(source_image, dest, source_hash):
    source_image=Path(source_image); final=_cache_image_path(dest,source_hash)
    try:
        from PIL import Image
        with Image.open(source_image) as im:
            im.convert('RGB').save(final,format='PNG',optimize=True)
    except Exception:
        if source_image.suffix.lower()=='.png':shutil.copy2(source_image,final)
        else:raise
    return final


def _thumb_path(image):
    image=Path(image)
    return image.with_name(image.stem+'.thumb.png')


def _make_thumb(image):
    image=Path(image)
    thumb=_thumb_path(image)
    if thumb.exists() and thumb.stat().st_size:return thumb
    try:
        from PIL import Image
        with Image.open(image) as im:
            im=im.convert('RGB')
            width=300
            height=max(1,round(im.height*(width/im.width)))
            im.thumbnail((width,height))
            thumb.parent.mkdir(parents=True,exist_ok=True)
            im.save(thumb,format='PNG',optimize=True)
        return thumb
    except Exception:
        return None


def _migrate_ready_cache(con,dest):
    try:rows=con.execute("SELECT id,sha256,preview_path FROM indicators WHERE preview_status='ready' AND preview_path IS NOT NULL").fetchall()
    except Exception:return
    root=Path(dest).resolve()
    for row in rows:
        try:
            old=Path(row['preview_path'] if isinstance(row,sqlite3.Row) else row[2])
            sha=row['sha256'] if isinstance(row,sqlite3.Row) else row[1]
            if not old.is_file() or not sha:continue
            target=_cache_image_path(dest,sha)
            if old.resolve()!=target.resolve():
                final=_store_preview_image(old,dest,sha)
                con.execute("UPDATE indicators SET preview_path=? WHERE id=?",(str(final),row['id'] if isinstance(row,sqlite3.Row) else row[0]))
                try:
                    old_res=old.resolve()
                    if root in old_res.parents:
                        old.unlink(missing_ok=True)
                        _thumb_path(old).unlink(missing_ok=True)
                except Exception:pass
        except Exception:pass
    con.commit()


def _worker_pause_reason(con,dest):
    try:
        row=con.execute("SELECT value FROM meta WHERE key='preview_paused'").fetchone()
        if row and str(row[0])=='1':return 'paused_by_user'
    except Exception:pass
    try:
        free=shutil.disk_usage(Path(dest).parent).free
        if free<2*1024*1024*1024:return 'low_disk'
    except Exception:pass
    if os.name=='nt':
        try:
            class SYSTEM_POWER_STATUS(ctypes.Structure):
                _fields_=[('ACLineStatus',ctypes.c_ubyte),('BatteryFlag',ctypes.c_ubyte),('BatteryLifePercent',ctypes.c_ubyte),('SystemStatusFlag',ctypes.c_ubyte),('BatteryLifeTime',ctypes.c_ulong),('BatteryFullLifeTime',ctypes.c_ulong)]
            s=SYSTEM_POWER_STATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)) and s.ACLineStatus==0 and s.BatteryLifePercent!=255 and s.BatteryLifePercent<=20:
                return 'low_battery'
        except Exception:pass
    return None


def _cleanup_cache_files(con,dest):
    try:valid={str(r[0]).lower() for r in con.execute("SELECT sha256 FROM indicators WHERE sha256 IS NOT NULL") if r[0]}
    except Exception:return
    root=Path(dest)
    if root.exists():
        for p in root.rglob('*'):
            if not p.is_file():continue
            name=p.name.lower()
            stem=name[:-10] if name.endswith('.thumb.png') else p.stem.lower()
            if stem not in valid:
                try:p.unlink()
                except Exception:pass
    comp=root.parent/'preview-runtime'/'compiled-cache'
    if comp.exists():
        for p in comp.rglob('*'):
            if p.is_file() and p.suffix.lower() in ('.ex4','.ex5') and p.stem.lower() not in valid:
                try:p.unlink()
                except Exception:pass


def _backfill_thumbnails(con):
    try:rows=con.execute("SELECT preview_path FROM indicators WHERE preview_status='ready' AND preview_path IS NOT NULL").fetchall()
    except Exception:return
    for row in rows:
        try:
            p=Path(row[0])
            if p.is_file() and not _thumb_path(p).exists():_make_thumb(p)
        except Exception:pass


def _render_chunk(rows, dest, sel, rt, sym, job_id, item_timeout):
    dest=Path(dest);mql=rt/'MQL5';scripts=mql/'Scripts';files=mql/'Files'
    scripts.mkdir(parents=True,exist_ok=True);files.mkdir(parents=True,exist_ok=True)
    editor=rt/Path(sel['editor']).name;terminal=rt/Path(sel['terminal']).name
    results={};plan=[]
    for i,row in enumerate(rows):
        src=Path(row['path'])
        emit_heartbeat(job_id,i,str(src),stage='compile')
        try:
            emit_stage(job_id,'indicator_compile',f'[{i+1}/{len(rows)}] Compiling {src.name}',source=str(src))
            cache=_compiled_cache_path(dest,'MT5',row['sha256'])
            job,staged,binary,_meta=stage(src,mql,'MT5',editor,sel.get('data_dir'),cache)
            rel=f'MQLLibraryPreview/{job}/{binary.stem}'
            shot=f'MQLLibraryPreview_{job}.png'
            sep='indicator_separate_window' in read_text(src).lower()
            item={'source':str(src),'sha256':row['sha256'],'rel':rel,'shot':shot,'shot_path':files/shot,'win':'1' if sep else '0'}
            for stale in (item['shot_path'],files/(shot+'.ok')):
                try:stale.unlink(missing_ok=True)
                except Exception:pass
            plan.append(item)
        except Exception as e:
            results[str(src)]={'ok':False,'error':f'{type(e).__name__}: {e}'}

    meta={'untouched':[],'hung_source':None,'timeout_reason':None}
    if not plan:return results,meta

    manifest_name=f'mqllib_chunk_{job_id}.txt'
    cur_prefix=f'mqllib_chunk_{job_id}_'
    done_marker=f'mqllib_chunk_{job_id}.done'
    cap=scripts/f'MQLLibraryChunkCapture_{job_id}.mq5'
    cap_source=mt5_batch_capture_source_v2(manifest_name,cur_prefix,done_marker)
    built=cap.with_suffix('.ex5')
    if not built.exists() or not built.stat().st_size or read_text(cap)!=cap_source:
        cap.write_text(cap_source,encoding='utf-8')
        emit_stage(job_id,'capture_compile','Compiling watchdog-enabled MT5 batch capture script.')
        built,_,log=compile_file(editor,cap,mql)
        if not built:
            err='Batch capture script failed to compile. '+(log or '')[-1600:]
            for item in plan:results.setdefault(item['source'],{'ok':False,'error':err})
            return results,meta
    else:
        emit_stage(job_id,'capture_compile_cached','Reusing compiled MT5 batch capture script.')

    (files/manifest_name).write_text('\n'.join(f"{x['rel']}\t{x['shot']}\t{x['win']}" for x in plan),encoding='utf-8')
    for p in files.glob(cur_prefix+'*.cur'):
        try:p.unlink()
        except Exception:pass
    for item in plan:
        for p in (files/(item['shot']+'.ok'),item['shot_path']):
            try:p.unlink(missing_ok=True)
            except Exception:pass
    done_path=files/done_marker
    try:done_path.unlink(missing_ok=True)
    except Exception:pass

    _prepare_offline_runtime(rt)
    cfg=rt/f'mql-chunk-{job_id}.ini'
    cfg.write_text(
        '[Common]\nNewsEnable=0\n\n'
        '[Experts]\nEnabled=1\nAllowLiveTrading=0\nAllowDllImport=0\n\n'
        f'[StartUp]\nSymbol={sym}\nPeriod=H1\nScript={cap.stem}\nShutdownTerminal=1\n',
        encoding='utf-8')
    emit_stage(job_id,'terminal_launch',f'Launching MT5 for preview chunk ({len(plan)} items).',chunk_size=len(plan))
    proc=subprocess.Popen([str(terminal),'/portable',f'/config:{cfg}'],cwd=str(rt),creationflags=CREATE_NO_WINDOW,startupinfo=startupinfo())

    timeout=max(5,int(item_timeout))
    chunk_budget=max(timeout+10,8*len(plan)+30)
    deadline=time.time()+chunk_budget
    last_shot=time.time()
    last_heartbeat=0.0
    done_count=0
    active_idx=None
    timeout_reason=None
    terminal_exited=False

    while time.time()<deadline:
        now=time.time()
        completed=[i for i,x in enumerate(plan) if (files/(x['shot']+'.ok')).exists()]
        if len(completed)>done_count:
            done_count=len(completed);last_shot=now
            emit_stage(job_id,'item_progress',f'{done_count}/{len(plan)} items completed in current chunk.',done=done_count,total=len(plan))
        started=[i for i in range(len(plan)) if (files/f'{cur_prefix}{i}.cur').exists()]
        newest=max(started) if started else None
        if newest is not None:active_idx=newest
        inflight=plan[active_idx]['source'] if active_idx is not None and active_idx<len(plan) else None
        if now-last_heartbeat>=5:
            emit_heartbeat(job_id,done_count,inflight,stage='render',chunk_size=len(plan))
            last_heartbeat=now
        if done_path.exists():break
        if proc.poll() is not None:
            terminal_exited=True;break
        if now-last_shot>timeout:
            timeout_reason='item_timeout';break
        time.sleep(.5)
    else:
        timeout_reason='chunk_deadline'

    hard_kill(proc,rt)

    completed_idx={i for i,x in enumerate(plan) if (files/(x['shot']+'.ok')).exists()}
    for i in sorted(completed_idx):
        item=plan[i]
        if item['source'] in results:continue
        if item['shot_path'].exists() and item['shot_path'].stat().st_size:
            final=_store_preview_image(item['shot_path'],dest,item['sha256'])
            results[item['source']]={'ok':True,'image':str(final),'kind':'MT5'}
        else:
            results[item['source']]={'ok':False,'error':'No screenshot produced (indicator could not load on the chart).'}

    pending=[i for i in range(len(plan)) if i not in completed_idx]
    if timeout_reason and pending:
        if active_idx is not None and active_idx in pending:bad=active_idx
        else:bad=pending[0]
        hung=plan[bad]
        results[hung['source']]={'ok':False,'error':f'Preview watchdog {timeout_reason} after {timeout if timeout_reason=="item_timeout" else chunk_budget}s.'}
        meta['hung_source']=hung['source'];meta['timeout_reason']=timeout_reason
        meta['untouched']=[plan[i]['source'] for i in pending if i>bad]
        emit_stage(job_id,'item_timeout',f'Watchdog hard-killed hung preview: {Path(hung["source"]).name}',source=hung['source'],timeout=timeout,reason=timeout_reason,untouched=len(meta['untouched']))
    elif terminal_exited and pending:
        bad=pending[0];item=plan[bad]
        results[item['source']]={'ok':False,'error':'MetaTrader exited before the preview completed.'}
        meta['untouched']=[plan[i]['source'] for i in pending if i>bad]
    elif done_path.exists():
        for i in pending:
            item=plan[i]
            results.setdefault(item['source'],{'ok':False,'error':'No screenshot produced before batch completion.'})
    elif pending:
        bad=pending[0];item=plan[bad]
        results[item['source']]={'ok':False,'error':'Preview chunk ended before the item completed.'}
        meta['untouched']=[plan[i]['source'] for i in pending if i>bad]

    try:
        preview_tree=mql/'Indicators'/'MQLLibraryPreview'
        shutil.rmtree(preview_tree,ignore_errors=True)
        preview_tree.mkdir(parents=True,exist_ok=True)
    except Exception:pass
    return results,meta

def _ensure_preview_queue_schema(con,worker_id=None):
    cols={r[1] for r in con.execute('PRAGMA table_info(indicators)')}
    additions={
        'preview_status':"TEXT DEFAULT 'pending'",
        'preview_path':'TEXT','preview_hash':'TEXT',
        'preview_error':"TEXT DEFAULT ''",'preview_updated_at':'TEXT',
        'preview_attempts':'INTEGER DEFAULT 0','preview_priority':'INTEGER DEFAULT 0','worker_id':'TEXT'
    }
    for name,decl in additions.items():
        if name not in cols:con.execute(f'ALTER TABLE indicators ADD COLUMN {name} {decl}')
    if worker_id:
        con.execute("UPDATE indicators SET preview_status='pending',worker_id=NULL WHERE preview_status='rendering' AND (worker_id=? OR worker_id IS NULL)",(worker_id,))
    else:
        con.execute("UPDATE indicators SET preview_status='pending',worker_id=NULL WHERE preview_status='rendering' AND worker_id IS NULL")
    con.execute("UPDATE indicators SET preview_status='pending',preview_attempts=0,preview_error='',worker_id=NULL "
                "WHERE preview_status='ready' AND COALESCE(preview_hash,'')!=COALESCE(sha256,'')")
    con.commit()


def _mt5_static_fast_fail(path):
    src=Path(path)
    if src.suffix.lower()!='.mq5':return None
    text=read_text(src)
    market=bool(re.search(r'\bMarketInfo\s*\(',text))
    mode=bool(re.search(r'\bMODE_(?:BID|ASK|POINT|DIGITS|SPREAD|STOPLEVEL|TICKVALUE|TICKSIZE|LOTSIZE|MINLOT|MAXLOT|LOTSTEP)\b',text))
    bare=bool(re.search(r'\b(?:Open|High|Low|Close|Volume)\s*\[',text))
    extmap=bool(re.search(r'\bExtMapBuffer\w*\b',text))
    if market and (mode or bare):
        return 'MT4 source in .mq5: MarketInfo/MODE or bare-series pattern'
    if extmap and market:
        return 'MT4 source in .mq5: decompiled MT4 buffer/MarketInfo pattern'
    return None


def _propagate_existing_ready(con):
    try:
        ready=con.execute(
            "SELECT sha256,preview_path,preview_hash,preview_updated_at FROM indicators "
            "WHERE preview_status='ready' AND COALESCE(sha256,'')<>'' GROUP BY sha256"
        ).fetchall()
        for r in ready:
            con.execute(
                "UPDATE indicators SET preview_status='ready',preview_path=?,preview_hash=?,preview_error='',"
                "preview_updated_at=?,worker_id=NULL WHERE sha256=? AND preview_status!='ready'",
                (r['preview_path'],r['preview_hash'],r['preview_updated_at'],r['sha256']))
        con.commit()
    except Exception:pass


def _claim_preview_rows(con,chunk_size,attempts,worker_id):
    size=max(1,min(int(chunk_size),200))
    con.execute('BEGIN IMMEDIATE')
    try:
        con.execute(
            "UPDATE indicators SET preview_status='failed',worker_id=NULL "
            "WHERE preview_status='pending' AND COALESCE(preview_attempts,0)>=?",
            (attempts,))
        rows=con.execute(
            """
            WITH grouped AS (
              SELECT sha256,MIN(id) AS id,MAX(COALESCE(preview_priority,0)) AS pr,
                     MAX(COALESCE(user_favorite,0)) AS fav
              FROM indicators i
              WHERE preview_status='pending'
                AND COALESCE(preview_attempts,0)<?
                AND COALESCE(sha256,'')<>''
                AND NOT EXISTS(
                  SELECT 1 FROM indicators x
                  WHERE x.sha256=i.sha256 AND x.preview_status IN ('rendering','ready')
                )
              GROUP BY sha256
              ORDER BY pr DESC,fav DESC,id
              LIMIT ?
            )
            SELECT i.id,i.path,i.platform,i.sha256,COALESCE(i.preview_attempts,0) AS preview_attempts
            FROM grouped g JOIN indicators i ON i.id=g.id
            ORDER BY g.pr DESC,g.fav DESC,i.id
            """,
            (attempts,size)).fetchall()
        if len(rows)<size:
            extra=con.execute(
                "SELECT id,path,platform,sha256,COALESCE(preview_attempts,0) AS preview_attempts "
                "FROM indicators WHERE preview_status='pending' AND COALESCE(preview_attempts,0)<? "
                "AND COALESCE(sha256,'')='' ORDER BY COALESCE(preview_priority,0) DESC,user_favorite DESC,id LIMIT ?",
                (attempts,size-len(rows))).fetchall()
            rows=list(rows)+list(extra)
        for r in rows:
            con.execute(
                "UPDATE indicators SET preview_status='rendering',worker_id=?,"
                "preview_attempts=COALESCE(preview_attempts,0)+1 WHERE id=? AND preview_status='pending'",
                (worker_id,r['id']))
        con.commit()
        return rows
    except Exception:
        con.rollback();raise


def render_library(db, out, terminal=None, chunk_size=70, item_timeout=30, max_attempts=2, job_id=None, worker_id=None, static_fast_fail=False):
    job_id=safe_job_id(job_id);worker_id=safe_job_id(worker_id or 'w1');dest=Path(out)
    emit_heartbeat(job_id,0,None,stage='worker_start',worker_id=worker_id)
    con=sqlite3.connect(db,timeout=30);con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL');con.execute('PRAGMA synchronous=NORMAL');con.execute('PRAGMA busy_timeout=5000')
    _ensure_preview_queue_schema(con,worker_id)
    if not static_fast_fail:
        con.execute(
            "UPDATE indicators SET preview_status='pending',preview_error='',preview_attempts=0,worker_id=NULL "
            "WHERE preview_status='failed' AND preview_error LIKE 'MT4 source in .mq5:%'")
        con.commit()
    _propagate_existing_ready(con)
    _migrate_ready_cache(con,dest)
    _backfill_thumbnails(con)
    mt5_sel=mt5_rt=mt5_sym=None
    mt4_sel=None
    lock=dest.parent/'preview-runtime'/f'.render-{worker_id}.lock'
    with RenderLock(lock,timeout=5.0):
        while True:
            ready_now=con.execute("SELECT COUNT(*) FROM indicators WHERE preview_status='ready'").fetchone()[0]
            emit_heartbeat(job_id,ready_now,None,stage='queue',worker_id=worker_id)
            pause_reason=_worker_pause_reason(con,dest)
            if pause_reason:
                emit({'ok':True,'job_id':job_id,'worker_id':worker_id,'paused':True,'reason':pause_reason})
                break
            attempts=max(1,min(int(max_attempts),10))
            rows=_claim_preview_rows(con,chunk_size,attempts,worker_id)
            if not rows:break

            results={};chunk_meta={'untouched':[],'hung_source':None,'timeout_reason':None}
            mt5_rows=[];mt4_rows=[]
            for r in rows:
                is_mt5=str(r['platform']).upper() in ('MQL5','MT5') or Path(r['path']).suffix.lower() in ('.mq5','.ex5')
                if is_mt5:
                    reason=_mt5_static_fast_fail(r['path']) if static_fast_fail else None
                    if reason:
                        results[str(r['path'])]={'ok':False,'error':reason,'fast_fail':True}
                        emit_stage(job_id,'static_fast_fail',f'Skipping obvious incompatible MT5 source: {Path(r["path"]).name}',source=str(r['path']),reason=reason)
                    else:
                        mt5_rows.append(r)
                else:
                    mt4_rows.append(r)

            if mt5_rows:
                try:
                    if mt5_sel is None:
                        mt5_sel=load_runtime_cache(dest,'MT5') or choose_runtime('MT5',terminal)
                        mt5_rt=clone_runtime(mt5_sel,dest,'MT5',worker_id)
                        mt5_sym=copy_mt5_history(Path(mt5_sel['data_dir']),mt5_rt)
                        prime_mt5_runtime(mt5_rt,mt5_rt/Path(mt5_sel['terminal']).name,mt5_sym,job_id)
                    chunk_results,chunk_meta=_render_chunk(mt5_rows,dest,mt5_sel,mt5_rt,mt5_sym,job_id,item_timeout)
                    results.update(chunk_results)
                except Exception as e:
                    for r in mt5_rows:results[str(r['path'])]={'ok':False,'error':f'{type(e).__name__}: {e}'}

            if mt4_rows:
                try:
                    if mt4_sel is None:mt4_sel=load_runtime_cache(dest,'MT4') or choose_runtime('MT4',terminal)
                    for r in mt4_rows:
                        try:
                            emit_heartbeat(job_id,0,str(r['path']),stage='mt4_render',worker_id=worker_id)
                            compiled=_compiled_cache_path(dest,'MT4',r['sha256'])
                            image,_meta=render_mt4(Path(r['path']),dest,mt4_sel,job_id,compiled,worker_id)
                            final=_store_preview_image(image,dest,r['sha256'])
                            results[str(r['path'])]={'ok':True,'image':str(final),'kind':'MT4'}
                            emit_heartbeat(job_id,0,str(r['path']),stage='mt4_done',worker_id=worker_id)
                        except Exception as e:
                            results[str(r['path'])]={'ok':False,'error':f'{type(e).__name__}: {e}'}
                except Exception as e:
                    for r in mt4_rows:results[str(r['path'])]={'ok':False,'error':f'{type(e).__name__}: {e}'}

            untouched=set(chunk_meta.get('untouched') or [])
            for r in rows:
                path=str(r['path']);sha=r['sha256']
                if path in untouched:
                    con.execute(
                        "UPDATE indicators SET preview_status='pending',worker_id=NULL,"
                        "preview_attempts=CASE WHEN COALESCE(preview_attempts,0)>0 THEN preview_attempts-1 ELSE 0 END,"
                        "preview_error='watchdog deferred untouched item' WHERE id=? AND worker_id=?",
                        (r['id'],worker_id))
                    continue
                res=results.get(path)
                attempt_num=int(r['preview_attempts'] or 0)+1
                if res and res.get('ok'):
                    _make_thumb(res['image'])
                    if sha:
                        con.execute(
                            "UPDATE indicators SET preview_status='ready',preview_path=?,preview_hash=?,preview_error='',"
                            "preview_updated_at=datetime('now'),preview_priority=0,worker_id=NULL WHERE sha256=?",
                            (res['image'],sha,sha))
                    else:
                        con.execute(
                            "UPDATE indicators SET preview_status='ready',preview_path=?,preview_hash=?,preview_error='',"
                            "preview_updated_at=datetime('now'),preview_priority=0,worker_id=NULL WHERE id=?",
                            (res['image'],sha,r['id']))
                else:
                    err=(res or {}).get('error','render timeout/hang')
                    final_fail=bool((res or {}).get('fast_fail')) or attempt_num>=attempts
                    if final_fail and sha:
                        con.execute(
                            "UPDATE indicators SET preview_status='failed',preview_error=?,preview_updated_at=datetime('now'),"
                            "worker_id=NULL WHERE sha256=?",
                            (str(err)[:800],sha))
                    else:
                        con.execute(
                            "UPDATE indicators SET preview_status=?,preview_error=?,worker_id=NULL WHERE id=?",
                            ('failed' if final_fail else 'pending',str(err)[:800],r['id']))
            con.commit()
            ready=con.execute("SELECT COUNT(*) FROM indicators WHERE preview_status='ready'").fetchone()[0]
            total=con.execute("SELECT COUNT(*) FROM indicators").fetchone()[0]
            emit({'type':'progress','job_id':job_id,'worker_id':worker_id,'done':ready,'total':total})

    _cleanup_cache_files(con,dest)
    con.close()
    emit({'ok':True,'job_id':job_id,'worker_id':worker_id,'finished':True})

def render_mt4(src,out,t,job_id,compiled_cache=None):
    emit_stage(job_id,'runtime_clone','Preparing isolated MT4 runtime.')
    rt=clone_runtime(t,out,'MT4');mql=rt/'MQL4';scripts=mql/'Scripts';templates=rt/'templates';files=mql/'Files';[d.mkdir(parents=True,exist_ok=True) for d in (scripts,templates,files)]
    editor=rt/Path(t['editor']).name;terminal=rt/Path(t['terminal']).name;sym=copy_mt4_history(Path(t['data_dir']),rt)
    emit_stage(job_id,'indicator_compile','Compiling/staging the selected MT4 indicator.')
    job,staged,binary,meta=stage(src,mql,'MT4',editor,t.get('data_dir'),compiled_cache);emit_stage(job_id,'indicator_staged','Indicator copied into isolated MT4 runtime.',staged=str(staged),binary=str(binary));emit_stage(job_id,'indicator_compile_success','MT4 indicator is ready for rendering.',binary=str(binary));rel=f'MQLLibraryPreview\\{job}\\{binary.stem}'
    tplname=f'MQLLibraryPreview_{job}.tpl';tpl=templates/tplname;sep='indicator_separate_window' in read_text(src).lower();win='1' if sep else '0';tpl.write_text(f'<chart>\nsymbol={sym}\nperiod=60\ngraph=1\ngrid=1\n<window>\nheight=420\n<indicator>\nname=main\n</indicator>\n<indicator>\nname=Custom Indicator\n<expert>\nname={rel}\nflags=339\nwindow_num={win}\n</expert>\nshow_data=1\n</indicator>\n</window>\n</chart>\n')
    shot=f'MQLLibraryPreview_{job}.gif';cap=scripts/f'MQLLibraryPreviewCapture_{job}.mq4';cap.write_text(f'#property strict\nvoid OnStart(){{Print("MQLLIB_PREVIEW stage=onstart");Sleep(3000);WindowRedraw();ResetLastError();bool ok=WindowScreenShot("{shot}",1200,720);Print("MQLLIB_PREVIEW stage=screenshot ok=",ok," err=",GetLastError());Sleep(300);TerminalClose(ok?0:23);return;}}\n')
    emit_stage(job_id,'capture_compile','Compiling MT4 capture script.')
    built,_,log=compile_file(editor,cap,mql)
    if not built:raise RuntimeError('Preview renderer compile failed — could not compile MT4 capture script. '+log[-1600:])
    targets=[files/shot,rt/shot,mql/shot];[p.unlink(missing_ok=True) for p in targets]
    cfg=rt/'config'/f'mql-preview-{job_id}.ini';cfg.parent.mkdir(parents=True,exist_ok=True);cfg.write_text(f'Symbol={sym}\nPeriod=H1\nTemplate={tplname}\nScript={cap.stem}\n')
    emit_stage(job_id,'terminal_launch','Launching isolated MT4 terminal for screenshot capture.')
    proc=subprocess.Popen([str(terminal),'/portable',str(cfg)],cwd=str(rt),creationflags=CREATE_NO_WINDOW,startupinfo=startupinfo())
    emit_stage(job_id,'screenshot_wait','Waiting for MT4 chart screenshot.')
    img=wait_image(proc,targets)
    if not img:
        logs=(latest_logs(rt/'logs')+'\n'+latest_logs(mql/'Logs'))[-5000:]; trace=preview_trace(logs); rc=proc.poll()
        terminate_tree(proc)
        raise RuntimeError(f'Preview renderer failed — MT4 produced no screenshot. exit={rc}; runtime={rt}; config={cfg}; symbol={sym}. Trace:\n{trace or "(no capture trace)"}\nLogs:\n{logs}')
    emit_stage(job_id,'screenshot_ready','MT4 screenshot captured.')
    final=out/(hashlib.sha1(('MT4|'+str(src.resolve()).lower()).encode()).hexdigest()[:16]+'.gif');out.mkdir(parents=True,exist_ok=True);shutil.copy2(img,final);meta.update({'isolated_runtime':str(rt),'symbol':sym,'staged':str(staged),'binary':str(binary),'job_id':job_id,'config':str(cfg)});return final,meta

def render_mt5(src,out,t,job_id):
    emit_stage(job_id,'runtime_clone','Preparing isolated MT5 runtime.')
    rt=clone_runtime(t,out,'MT5');mql=rt/'MQL5';scripts=mql/'Scripts';files=mql/'Files';[d.mkdir(parents=True,exist_ok=True) for d in (scripts,files)]
    editor=rt/Path(t['editor']).name;terminal=rt/Path(t['terminal']).name;sym=copy_mt5_history(Path(t['data_dir']),rt)
    prime_mt5_runtime(rt,terminal,sym,job_id)
    emit_stage(job_id,'indicator_compile','Compiling/staging the selected MT5 indicator.')
    job,staged,binary,meta=stage(src,mql,'MT5',editor,t.get('data_dir'));emit_stage(job_id,'indicator_staged','Indicator copied into isolated MT5 runtime.',staged=str(staged),binary=str(binary));emit_stage(job_id,'indicator_compile_success','MT5 indicator is ready for rendering.',binary=str(binary));rel=f'MQLLibraryPreview\\{job}\\{binary.stem}';shot=f'MQLLibraryPreview_{job}.png';cap=scripts/f'MQLLibraryPreviewCapture_{job}.mq5';sep='indicator_separate_window' in read_text(src).lower();win='1' if sep else '0'
    cap.write_text(mt5_capture_source(rel,shot,win),encoding='utf-8')
    emit_stage(job_id,'capture_compile','Compiling MT5 capture script.')
    built,_,log=compile_file(editor,cap,mql)
    if not built:raise RuntimeError('Preview renderer compile failed — could not compile MT5 capture script. '+log[-1600:])
    targets=[files/shot,rt/shot];[p.unlink(missing_ok=True) for p in targets]
    cfg=rt/f'mql-preview-{job_id}.ini';cfg.write_text(f'[Experts]\nEnabled=1\nAllowLiveTrading=0\nAllowDllImport=0\n\n[StartUp]\nSymbol={sym}\nPeriod=H1\nScript={cap.stem}\nShutdownTerminal=1\n')
    emit_stage(job_id,'terminal_launch','Launching isolated MT5 terminal for screenshot capture.')
    proc=subprocess.Popen([str(terminal),'/portable',f'/config:{cfg}'],cwd=str(rt),creationflags=CREATE_NO_WINDOW,startupinfo=startupinfo())
    emit_stage(job_id,'screenshot_wait','Waiting for MT5 chart screenshot.')
    img=wait_image(proc,targets)
    if not img:
        logs=(latest_logs(rt/'logs')+'\n'+latest_logs(mql/'Logs'))[-7000:]; trace=preview_trace(logs); rc=proc.poll()
        terminate_tree(proc)
        raise RuntimeError(f'Preview renderer failed — MT5 produced no screenshot. exit={rc}; runtime={rt}; config={cfg}; symbol={sym}. Trace:\n{trace or "(no capture trace)"}\nLogs:\n{logs}')
    emit_stage(job_id,'screenshot_ready','MT5 screenshot captured.')
    final=out/(hashlib.sha1(('MT5|'+str(src.resolve()).lower()).encode()).hexdigest()[:16]+'.png');out.mkdir(parents=True,exist_ok=True);shutil.copy2(img,final);meta.update({'isolated_runtime':str(rt),'symbol':sym,'staged':str(staged),'binary':str(binary),'job_id':job_id,'config':str(cfg)});return final,meta

class PreviewRuntimeError(RuntimeError):
    def __init__(self,message,diagnostics=None):
        super().__init__(message)
        self.diagnostics=diagnostics or []

def source_kind(source):
    return 'MT5' if Path(source).suffix.lower() in ('.mq5','.ex5') else 'MT4'

def selection_checks(src,kind,selected):
    data_dir=Path(selected['data_dir']) if selected.get('data_dir') else None
    return {
        'source':src.is_file(),
        'terminal_binary':bool(selected.get('terminal') and Path(selected['terminal']).is_file()),
        'metaeditor_binary':bool(selected.get('editor') and Path(selected['editor']).is_file()),
        'data_directory':bool(data_dir and data_dir.is_dir()),
        'mql_directory':bool(data_dir and (data_dir/mql_dir_name(kind)).is_dir())
    }

def discovery_diagnostics(kind,selected=None,checks=None):
    selected=selected or {}
    checks=checks or {}
    mql_name=mql_dir_name(kind)
    return [
        {'stage':'Terminal binary','status':'FOUND' if checks.get('terminal_binary') else 'NOT FOUND','path':selected.get('terminal')},
        {'stage':'MetaEditor binary','status':'FOUND' if checks.get('metaeditor_binary') else 'NOT FOUND','path':selected.get('editor')},
        {'stage':'Data directory','status':'FOUND' if checks.get('data_directory') else 'NOT FOUND','path':selected.get('data_dir'),'pair_method':selected.get('pair_method')},
        {'stage':f'{mql_name} directory','status':'FOUND' if checks.get('mql_directory') else 'NOT FOUND','path':str(Path(selected['data_dir'])/mql_name) if selected.get('data_dir') else None}
    ]

def choose_runtime(kind,terminal=None):
    candidates=[x for x in terminals() if x['kind']==kind]
    if terminal:
        exact=[x for x in candidates if x.get('terminal') and same_path(x['terminal'],terminal)]
        if exact:candidates=exact
    if not candidates:
        diagnostics=[{'stage':'Terminal binary','status':'NOT FOUND','path':None}]
        raise PreviewRuntimeError(f'No {kind} terminal binary was detected.',diagnostics)
    return candidates[0]

def preview_runtime(source,terminal=None,selected=None):
    src=Path(source)
    kind=source_kind(src)
    if not src.is_file():
        raise PreviewRuntimeError(f'Preview source was not found: {src}',[{'stage':'Source file','status':'NOT FOUND','path':str(src)}])
    selected=selected or choose_runtime(kind,terminal)
    checks=selection_checks(src,kind,selected)
    diagnostics=discovery_diagnostics(kind,selected,checks)
    missing=[d['stage'] for d in diagnostics if d['status']!='FOUND']
    if missing:
        observed={
            'installations':discover_installations(),
            'data_directories':discover_data_dirs(),
            'observed_data_folders':observed_data_folders()
        }
        raise PreviewRuntimeError(
            f'{kind} preview runtime validation failed: {", ".join(missing)}.',
            diagnostics+[{'stage':'Discovery inventory','status':'INFO','details':observed}]
        )
    return src,kind,selected,checks

def runtime_cache_path(dest,kind):
    p=Path(dest).parent/'preview-runtime'/f'{kind.lower()}-selection-v5.json'
    p.parent.mkdir(parents=True,exist_ok=True)
    return p

def load_runtime_cache(dest,kind):
    path=runtime_cache_path(dest,kind)
    if not path.is_file():return None
    try:data=json.loads(path.read_text(encoding='utf-8'))
    except Exception:return None
    if data.get('kind')!=kind:return None
    selected=data.get('terminal') or {}
    src=Path(data.get('source') or '.')
    checks=selection_checks(src,kind,selected)
    if not all(checks[k] for k in ('terminal_binary','metaeditor_binary','data_directory','mql_directory')):
        try:path.unlink()
        except Exception:pass
        return None
    return selected

def write_runtime_cache(dest,kind,source,selected):
    path=runtime_cache_path(dest,kind)
    path.write_text(json.dumps({'version':5,'kind':kind,'source':str(source),'terminal':selected,'saved_at':int(time.time())},ensure_ascii=False,indent=2),encoding='utf-8')
    return path

def preflight(source,out,terminal=None):
    src=Path(source);kind=source_kind(src);dest=Path(out)
    cached=None if terminal else load_runtime_cache(dest,kind)
    cache_hit=bool(cached)
    src,kind,selected,checks=preview_runtime(src,terminal,selected=cached)
    diagnostics=discovery_diagnostics(kind,selected,checks)
    rt=clone_runtime(selected,dest,kind)
    mql_name=mql_dir_name(kind)
    sandbox_checks={
        'sandbox':rt.is_dir(),
        'sandbox_terminal':(rt/Path(selected['terminal']).name).is_file(),
        'sandbox_metaeditor':(rt/Path(selected['editor']).name).is_file(),
        'sandbox_mql_dir':(rt/mql_name).is_dir(),
        'sandbox_indicators_dir':(rt/mql_name/'Indicators').is_dir()
    }
    diagnostics.extend([
        {'stage':'Preview sandbox','status':'CREATED' if sandbox_checks['sandbox'] else 'FAILED','path':str(rt)},
        {'stage':'Sandbox terminal','status':'FOUND' if sandbox_checks['sandbox_terminal'] else 'NOT FOUND','path':str(rt/Path(selected['terminal']).name)},
        {'stage':'Sandbox MetaEditor','status':'FOUND' if sandbox_checks['sandbox_metaeditor'] else 'NOT FOUND','path':str(rt/Path(selected['editor']).name)},
        {'stage':f'Sandbox {mql_name} directory','status':'FOUND' if sandbox_checks['sandbox_mql_dir'] else 'NOT FOUND','path':str(rt/mql_name)},
        {'stage':'Sandbox Indicators directory','status':'FOUND' if sandbox_checks['sandbox_indicators_dir'] else 'NOT FOUND','path':str(rt/mql_name/'Indicators')}
    ])
    failed=[x['stage'] for x in diagnostics if x['status'] in ('FAILED','NOT FOUND')]
    if failed:raise PreviewRuntimeError(f'Preview sandbox validation failed: {", ".join(failed)}.',diagnostics)
    cache=write_runtime_cache(dest,kind,src,selected)
    emit({
        'ok':True,
        'preflight':True,
        'cache_hit':cache_hit,
        'cache_file':str(cache),
        'source':str(src),
        'kind':kind,
        'terminal':selected,
        'sandbox':str(rt),
        'checks':{**checks,**sandbox_checks},
        'diagnostics':diagnostics
    })

def render(source,out,terminal=None,job_id=None):
    src=Path(source);dest=Path(out);kind=source_kind(src);job_id=safe_job_id(job_id)
    cached=None if terminal else load_runtime_cache(dest,kind)
    src,kind,selected,_checks=preview_runtime(src,terminal,selected=cached)
    if not cached:
        write_runtime_cache(dest,kind,src,selected)
    emit_stage(job_id,'runtime_preflight_cached','Using validated cached preview runtime selection.' if cached else 'Using freshly validated preview runtime selection.',cache_hit=bool(cached))
    emit_stage(job_id,'terminal_selected',f'Using {kind} data folder: {selected["data_dir"]}',kind=kind,install_dir=selected['install_dir'],terminal=selected['terminal'],editor=selected['editor'],data_dir=selected['data_dir'],pair_method=selected.get('pair_method'))
    lock_path=dest.parent/'preview-runtime'/'.render.lock'
    with RenderLock(lock_path,timeout=5.0):
        image,meta=(render_mt5(src,dest,selected,job_id) if kind=='MT5' else render_mt4(src,dest,selected,job_id))
    emit({'ok':True,'image':str(image),'kind':kind,'cached':bool(cached),'job_id':job_id,'terminal':selected,'meta':meta})

def open_source(source):
    src=Path(source);kind=source_kind(src)
    selected=choose_runtime(kind)
    target=selected.get('editor') if src.suffix.lower() in ('.mq4','.mq5') else selected.get('terminal')
    if not target or not Path(target).is_file():
        which='MetaEditor' if src.suffix.lower() in ('.mq4','.mq5') else 'terminal'
        raise RuntimeError(f'{kind} {which} binary was not detected.')
    low=target.lower()
    subprocess.Popen([target,str(src)] if low.endswith('editor.exe') or low.endswith('editor64.exe') else [target])
    emit({'ok':True,'opened':target,'kind':kind})


def _selftest_default_db():
    appdata=Path(os.environ.get('APPDATA',''))
    return appdata/'com.mqlindicatorlibrary.app'/'library.sqlite3'

def _selftest_source_text(path):
    p=Path(path)
    return read_text(p).lower() if p.suffix.lower() in ('.mq4','.mq5') else ''

def _selftest_rank(path):
    p=Path(path);name=p.name.lower();text=_selftest_source_text(p)
    score=0
    if 'my tma+channel' in name or ('tma' in name and 'channel' in name):score+=2000
    if re.search(r'(^|[^a-z0-9])3ls([^a-z0-9]|$)',name):score+=1900
    if 'indicator_separate_window' in text:score+=1200
    if any(x in name for x in ('mtf','multi timeframe','multi-timeframe','multitimeframe')):score+=1000
    if any(x in name for x in ('rsi','macd','stoch','osc','moving average','ma_','average')):score+=250
    if p.suffix.lower() in ('.mq5','.ex5'):score+=50
    return (-score,name.lower(),str(p).lower())

def _selftest_candidates(source=None,db=None,sample=30):
    paths=[]
    if source:
        root=Path(source)
        if not root.exists():raise RuntimeError(f'Self-test source folder does not exist: {root}')
        allowed={'.mq4','.ex4','.mq5','.ex5'}
        paths=[p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in allowed]
    else:
        dbp=Path(db) if db else _selftest_default_db()
        if not dbp.is_file():raise RuntimeError(f'Self-test needs --source or an existing library database: {dbp}')
        con=sqlite3.connect(dbp)
        try:
            rows=con.execute("SELECT path FROM indicators WHERE path IS NOT NULL AND path!='' ORDER BY id").fetchall()
        finally:con.close()
        for (raw,) in rows:
            p=Path(raw)
            if p.is_file() and p.suffix.lower() in ('.mq4','.ex4','.mq5','.ex5'):paths.append(p)
    unique=[];seen=set()
    for p in paths:
        try:key=os.path.normcase(str(p.resolve()))
        except Exception:key=os.path.normcase(str(p))
        if key in seen:continue
        seen.add(key);unique.append(p)
    n=max(1,int(sample or 30))
    mt5=sorted([p for p in unique if p.suffix.lower() in ('.mq5','.ex5')],key=_selftest_rank)
    mt4=sorted([p for p in unique if p.suffix.lower() in ('.mq4','.ex4')],key=_selftest_rank)
    selected=[]
    if mt5 and mt4 and n>=4:
        mt4_n=min(len(mt4),max(2,min(6,n//4)))
        mt5_n=min(len(mt5),n-mt4_n)
        selected.extend(mt5[:mt5_n]);selected.extend(mt4[:mt4_n])
        remain=n-len(selected)
        if remain>0:
            extras=mt5[mt5_n:]+mt4[mt4_n:]
            selected.extend(sorted(extras,key=_selftest_rank)[:remain])
    else:
        selected=sorted(unique,key=_selftest_rank)[:n]
    return sorted(selected,key=_selftest_rank)[:n]

def _selftest_db(path,mt5_paths):
    path=Path(path)
    try:path.unlink(missing_ok=True)
    except Exception:pass
    con=sqlite3.connect(path)
    try:
        con.execute("""CREATE TABLE indicators(
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL,
            filename TEXT,
            platform TEXT,
            sha256 TEXT,
            duplicate_of INTEGER,
            user_favorite INTEGER DEFAULT 0,
            analyzed_at TEXT,
            preview_status TEXT DEFAULT 'pending',
            preview_path TEXT,
            preview_hash TEXT,
            preview_error TEXT DEFAULT '',
            preview_updated_at TEXT,
            preview_attempts INTEGER DEFAULT 0,
            preview_priority INTEGER DEFAULT 0,
            worker_id TEXT
        )""")
        con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
        for i,p in enumerate(mt5_paths,1):
            h=hashlib.sha256(Path(p).read_bytes()).hexdigest()
            con.execute(
                "INSERT INTO indicators(id,path,filename,platform,sha256,duplicate_of,user_favorite,analyzed_at,preview_priority) "
                "VALUES(?,?,?,?,?,NULL,0,datetime('now'),1000)",
                (i,str(p),Path(p).name,'MQL5',h))
        con.commit()
    finally:con.close()
    return path

def _selftest_chart_crop(im):
    w,h=im.size
    left=max(0,int(w*.04));top=max(0,int(h*.04));right=max(left+1,int(w*.97));bottom=max(top+1,int(h*.93))
    return im.crop((left,top,right,bottom))

def _selftest_dominant_color(im):
    q=im.resize((120,72)).convert('RGB').quantize(colors=8)
    colors=q.getcolors() or []
    if not colors:return (0,0,0)
    idx=max(colors,key=lambda x:x[0])[1]
    palette=q.getpalette() or []
    base=idx*3
    return tuple(palette[base:base+3]) if len(palette)>=base+3 else (0,0,0)

def _selftest_nonbg_coverage(im,bg,threshold=48):
    px=im.resize((480,288)).convert('RGB').getdata()
    total=0;changed=0
    br,bg0,bb=bg
    for r,g,b in px:
        total+=1
        if abs(r-br)+abs(g-bg0)+abs(b-bb)>threshold:changed+=1
    return changed/max(1,total)

def _selftest_saturated_colors(im):
    colors=set()
    for r,g,b in im.resize((160,96)).convert('RGB').getdata():
        if max(r,g,b)-min(r,g,b)>=50:
            colors.add((r//32,g//32,b//32))
    return colors

def _selftest_image_metrics(reference,candidate,kind='MT5'):
    from PIL import Image,ImageChops
    with Image.open(reference) as rb, Image.open(candidate) as rc:
        base=_selftest_chart_crop(rb.convert('RGB'))
        cand=_selftest_chart_crop(rc.convert('RGB'))
        size=(480,288)
        base=base.resize(size);cand=cand.resize(size)
        bg=_selftest_dominant_color(base)
        base_nonbg=_selftest_nonbg_coverage(base,bg)
        cand_nonbg=_selftest_nonbg_coverage(cand,bg)
        delta=max(0.0,cand_nonbg-base_nonbg)
        diff=ImageChops.difference(cand,base)
        changed=sum(1 for p in diff.getdata() if max(p)>28)
        diff_coverage=changed/(size[0]*size[1])
        base_colors=_selftest_saturated_colors(base)
        cand_colors=_selftest_saturated_colors(cand)
        novel_colors=len(cand_colors-base_colors)
        blank_ceiling=max(.0015,min(.012,base_nonbg*.06))
        if str(kind).upper()=='MT5':
            drew=(diff_coverage>blank_ceiling) or (delta>.0015 and novel_colors>=1)
            coverage=diff_coverage
        else:
            mt4_ceiling=max(.0030,min(.02,base_nonbg*.08))
            drew=(delta>mt4_ceiling) or (delta>.0015 and novel_colors>=4)
            coverage=delta
        return {
            'coverage':round(float(coverage),6),
            'diff_coverage':round(float(diff_coverage),6),
            'non_background':round(float(cand_nonbg),6),
            'baseline_non_background':round(float(base_nonbg),6),
            'coverage_delta':round(float(delta),6),
            'novel_saturated_colors':novel_colors,
            'blank_ceiling':round(float(blank_ceiling),6),
            'drew':bool(drew)
        }

def _selftest_copy_png(source,target):
    from PIL import Image
    source=Path(source);target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    with Image.open(source) as im:
        im.convert('RGB').save(target,format='PNG',optimize=True)
    return target

def _selftest_label_from_error(error):
    msg=str(error or '')
    low=msg.lower()
    if low.startswith('compile:') or 'compile failed' in low or 'produced no ex' in low:
        return 'COMPILE_FAIL'
    if 'timeout' in low or 'hang' in low:
        return 'TIMEOUT'
    m=re.search(r'(?:err(?:or)?[ =:]*)\(?([0-9]{3,5})\)?',msg,re.I)
    return f'RENDER_FAIL({m.group(1)})' if m else 'RENDER_FAIL'

def _selftest_contact_sheet(items,target):
    from PIL import Image,ImageDraw,ImageFont
    cols=5;thumb_w=220;thumb_h=132;label_h=48;pad=10
    rows=max(1,(len(items)+cols-1)//cols)
    sheet=Image.new('RGB',(pad+cols*(thumb_w+pad),pad+rows*(thumb_h+label_h+pad)),(32,32,32))
    draw=ImageDraw.Draw(sheet);font=ImageFont.load_default()
    for idx,item in enumerate(items):
        col=idx%cols;row=idx//cols;x=pad+col*(thumb_w+pad);y=pad+row*(thumb_h+label_h+pad)
        p=Path(item.get('png_path') or '')
        if p.is_file():
            try:
                with Image.open(p) as im:
                    tile=im.convert('RGB');tile.thumbnail((thumb_w,thumb_h))
                    ox=x+(thumb_w-tile.width)//2;oy=y+(thumb_h-tile.height)//2
                    sheet.paste(tile,(ox,oy))
            except Exception:pass
        else:
            draw.rectangle((x,y,x+thumb_w,y+thumb_h),outline=(110,110,110),width=1)
            draw.text((x+8,y+52),'NO IMAGE',font=font,fill=(220,220,220))
        name=str(item.get('name') or '')
        if len(name)>31:name=name[:28]+'...'
        draw.text((x,y+thumb_h+5),name,font=font,fill=(235,235,235))
        label=str(item.get('label') or '')
        draw.text((x,y+thumb_h+22),label,font=font,fill=(235,235,235))
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True);sheet.save(target,format='PNG',optimize=True)
    return target

def _selftest_print_summary(items,counts,draw_percent,passed):
    lines=['','Preview self-test','='*72,f'{"LABEL":<28} COUNT','-'*36]
    for label,count in sorted(counts.items(),key=lambda x:(x[0]!='OK_DREW',x[0])):
        lines.append(f'{label:<28} {count}')
    lines.extend(['-'*36,f'DRAW RATE                    {draw_percent:.1f}%',f'RESULT                       {"PASS" if passed else "FAIL"}'])
    bad=[i for i in items if i.get('label')!='OK_DREW']
    if bad:
        lines.append('');lines.append('BLANK / FAIL FILES')
        for item in bad:lines.append(f" - {item.get('label')}: {item.get('name')}")
    sys.stderr.write('\n'.join(lines)+'\n');sys.stderr.flush()

def preview_selftest(out,sample=30,source=None,db=None,terminal=None,workers=4,hang_timeout=40,min_draw_percent=60.0,mt5_data_dir=None):
    """Run a sampled preview test through production render code, then grade screenshots.

    MT5 uses the actual compile_pool + resident render_server. MT4 uses the existing
    real per-file MT4 renderer. No render path is mocked.
    """
    sample=max(1,min(int(sample or 30),200))
    workers=max(1,min(int(workers or 4),8))
    hang_timeout=max(40,int(hang_timeout or 40))
    root=Path(out)/'selftest'
    if root.exists():shutil.rmtree(root,ignore_errors=True)
    pngdir=root/'pngs';pngdir.mkdir(parents=True,exist_ok=True)
    selected=_selftest_candidates(source,db,sample)
    if not selected:raise RuntimeError('Preview self-test found no MQL4/MQL5 indicator files to test.')
    mt5=[p for p in selected if p.suffix.lower() in ('.mq5','.ex5')]
    mt4=[p for p in selected if p.suffix.lower() in ('.mq4','.ex4')]
    baseline=root/'reference_empty_chart.png'
    pipeline_out=root/'pipeline'/'previews'
    pipeline_out.mkdir(parents=True,exist_ok=True)
    testdb=_selftest_db(root/'selftest.sqlite3',mt5)
    runtime_id=safe_job_id(f'selftest-{time.time_ns()}')
    render_error=[]

    def render_thread():
        try:
            render_server(
                str(testdb),str(pipeline_out),terminal=terminal,job_id='selftest-render',
                hang_timeout=hang_timeout,max_attempts=1,mt5_data_dir=mt5_data_dir,
                runtime_id=runtime_id,baseline_target=baseline)
        except BaseException as e:
            render_error.append(e)

    import threading
    thread=threading.Thread(target=render_thread,name='mql-preview-selftest-render',daemon=False)
    thread.start()
    compile_error=None
    try:
        compile_pool(str(testdb),str(pipeline_out),terminal=terminal,workers=workers,
                     job_id='selftest-compile',runtime_id=runtime_id)
    except BaseException as e:
        compile_error=e
    if compile_error:
        cleanup=sqlite3.connect(testdb)
        try:
            cleanup.execute(
                "UPDATE indicators SET preview_status='failed',preview_error=?,worker_id=NULL "
                "WHERE preview_status IN ('pending','compiling','compiled','rendering')",
                (('compile-pool aborted: '+str(compile_error))[:800],))
            cleanup.commit()
        finally:cleanup.close()
    thread.join(timeout=max(90,hang_timeout*(max(1,len(mt5))+2)))
    if thread.is_alive():
        render_error.append(TimeoutError('Real resident render pipeline did not finish within the self-test deadline.'))
        # The production render server is bounded by hang_timeout; this is a final self-test guard.
        try:
            sel=load_runtime_cache(pipeline_out,'MT5') or choose_runtime('MT5',terminal)
            hard_kill(None,runtime_path(sel,pipeline_out,'MT5','render-server'))
        except Exception:pass
        thread.join(timeout=10)
    if compile_error and not baseline.is_file():
        raise RuntimeError(f'Real compile-pool self-test failed before baseline capture: {compile_error}')
    if render_error and not baseline.is_file():
        raise RuntimeError(f'Real resident render self-test failed before baseline capture: {render_error[0]}')
    if not baseline.is_file():
        raise RuntimeError('Real resident render self-test did not produce the empty-chart baseline.')

    rows={}
    con=sqlite3.connect(testdb);con.row_factory=sqlite3.Row
    try:
        for r in con.execute("SELECT path,sha256,preview_status,preview_path,preview_error FROM indicators"):
            rows[os.path.normcase(str(Path(r['path'])))] = dict(r)
    finally:con.close()

    items=[]
    for p in mt5:
        key=os.path.normcase(str(Path(p)))
        r=rows.get(key) or {}
        err=str(r.get('preview_error') or '')
        item={'name':p.name,'source':str(p),'kind':'MT5','pipeline':'compile-pool+resident-ea',
              'label':None,'err':err or None,'png_path':None,'coverage':None}
        if r.get('preview_status')=='ready' and r.get('preview_path') and Path(r['preview_path']).is_file():
            dst=_selftest_copy_png(r['preview_path'],pngdir/f'{str(r.get("sha256") or "")[:10]}_{safe_name(p).rsplit(".",1)[0]}.png')
            metrics=_selftest_image_metrics(baseline,dst,'MT5')
            item.update(metrics);item['png_path']=str(dst);item['label']='OK_DREW' if metrics['drew'] else 'BLANK'
        else:
            fallback=err or (str(render_error[0]) if render_error else f"render status={r.get('preview_status') or 'missing'}")
            item['err']=fallback
            item['label']=_selftest_label_from_error(fallback)
        items.append(item)

    mt4_out=root/'mt4-real'/'previews';mt4_out.mkdir(parents=True,exist_ok=True)
    for p in mt4:
        item={'name':p.name,'source':str(p),'kind':'MT4','pipeline':'real-mt4-render',
              'label':None,'err':None,'png_path':None,'coverage':None}
        try:
            before=set(mt4_out.glob('*'))
            render(str(p),str(mt4_out),terminal=terminal,job_id=f'selftest-mt4-{hashlib.sha1(str(p).encode()).hexdigest()[:8]}')
            after=[x for x in mt4_out.glob('*') if x.is_file() and x not in before and x.suffix.lower() in ('.gif','.png')]
            if not after:
                # render() is hash-stable and may overwrite an existing result on rerun.
                expected=mt4_out/(hashlib.sha1(('MT4|'+str(p.resolve()).lower()).encode()).hexdigest()[:16]+'.gif')
                after=[expected] if expected.is_file() else []
            if not after:raise RuntimeError('real MT4 renderer returned without an image')
            dst=_selftest_copy_png(after[-1],pngdir/f'mt4_{hashlib.sha256(p.read_bytes()).hexdigest()[:10]}_{safe_name(p).rsplit(".",1)[0]}.png')
            metrics=_selftest_image_metrics(baseline,dst,'MT4')
            item.update(metrics);item['png_path']=str(dst);item['label']='OK_DREW' if metrics['drew'] else 'BLANK'
        except Exception as e:
            item['err']=f'{type(e).__name__}: {e}';item['label']=_selftest_label_from_error(item['err'])
        items.append(item)

    order={os.path.normcase(str(p)):i for i,p in enumerate(selected)}
    items.sort(key=lambda x:order.get(os.path.normcase(x['source']),999999))
    counts={}
    for item in items:counts[item['label']]=counts.get(item['label'],0)+1
    ok_count=counts.get('OK_DREW',0)
    draw_percent=(100.0*ok_count/max(1,len(items)))
    passed=draw_percent>=float(min_draw_percent)
    contact=_selftest_contact_sheet(items,root/'selftest_contact_sheet.png')
    report={
        'generated_at':time.strftime('%Y-%m-%dT%H:%M:%S'),
        'sample_requested':sample,'sample_count':len(items),
        'mt5_count':len(mt5),'mt4_count':len(mt4),
        'baseline_path':str(baseline),'contact_sheet':str(contact),
        'min_draw_percent':float(min_draw_percent),'draw_percent':round(draw_percent,2),
        'passed':passed,'counts':counts,'items':items
    }
    report_path=root/'selftest_report.json'
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    _selftest_print_summary(items,counts,draw_percent,passed)
    emit({'ok':passed,'selftest':True,'real_pipeline':True,'sample_count':len(items),'counts':counts,
          'draw_percent':round(draw_percent,2),'min_draw_percent':float(min_draw_percent),
          'report':str(report_path),'contact_sheet':str(contact),'baseline':str(baseline),
          'failed_files':[x['name'] for x in items if x['label']!='OK_DREW']})
    if not passed:raise SystemExit(3)


def self_test_discovery():
    checks={}
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);base=root/'MetaQuotes'/'Terminal';base.mkdir(parents=True)
        mt5=root/'OANDA TMS MT5 Terminal';mt5.mkdir()
        (mt5/'terminal64.exe').write_bytes(b'terminal')
        (mt5/'metaeditor64.exe').write_bytes(b'editor')
        d5=base/'DATA5';(d5/'MQL5'/'Indicators').mkdir(parents=True)
        (d5/'origin.txt').write_text(str(mt5),encoding='utf-8')
        mt4=root/'Blueberry Markets MetaTrader 4';mt4.mkdir()
        (mt4/'terminal.exe').write_bytes(b'terminal')
        d4=base/'DATA4';(d4/'MQL4'/'Indicators').mkdir(parents=True)
        (d4/'origin.txt').write_text(str(mt4),encoding='utf-8')
        data=discover_data_dirs(base)
        installs=discover_installations(base,roots=[])
        pairs=pair_runtime_candidates(installs,data)
        p5=next((x for x in pairs if x['kind']=='MT5' and x.get('terminal')),None)
        p4=next((x for x in pairs if x['kind']=='MT4' and x.get('terminal')),None)
        checks['terminal_detection_independent_of_editor']=bool(p4 and p4.get('terminal') and not p4.get('editor'))
        checks['metaeditor_from_terminal_install']=bool(p5 and p5.get('editor')==str(mt5/'metaeditor64.exe'))
        checks['origin_data_pairing']=bool(p5 and p5.get('data_dir')==str(d5) and p5.get('pair_method')=='origin-match')
        # Regression guard: the live-data MQL check must resolve to MQL5/MQL4, not MT5/MT4.
        _mc=selection_checks(root/'demo.mq5','MT5',p5) if p5 else {}
        checks['live_mql_directory_detected']=bool(p5 and _mc.get('mql_directory'))
        checks['diagnostics_use_mql_folder_name']=bool(p5 and discovery_diagnostics('MT5',p5,_mc)[3]['stage']=='MQL5 directory')
        alt=root/'Alternate MT5';alt.mkdir()
        (alt/'terminal64.exe').write_bytes(b'terminal')
        (alt/'metaeditor64.exe').write_bytes(b'editor')
        alt_install={'kind':'MT5','terminal':str(alt/'terminal64.exe'),'editor':str(alt/'metaeditor64.exe'),'install_dir':str(alt)}
        fallback=pair_runtime_candidates([alt_install],[x for x in data if x['kind']=='MT5'])
        checks['independent_platform_pairing']=bool(fallback and fallback[0].get('data_dir')==str(d5) and fallback[0].get('pair_method')=='platform-fallback')
        out=root/'previews'
        rt=clone_runtime(p5,out,'MT5')
        checks['sandbox_mql_tree_created']=(rt/'MQL5'/'Indicators').is_dir()  # clone_runtime raises unless the minimum MQL tree exists
        cache=write_runtime_cache(out,'MT5',root/'demo.mq5',p5)
        checks['runtime_selection_cache_written']=cache.is_file()
    return checks

def self_test_render_dependencies():
    checks={}
    with tempfile.TemporaryDirectory() as td:
        root=Path(td)
        real=root/'Terminal'/'ABC123'; rt=root/'render'
        (real/'config').mkdir(parents=True)
        (real/'config'/'accounts.dat').write_bytes(b'account')
        hist=real/'bases'/'broker'/'history'/'EURUSD';hist.mkdir(parents=True)
        (hist/'2026.hcc').write_bytes(b'bars')

        for path,data in (
            (real/'MQL5'/'Indicators'/'Helpers'/'Helper.ex5',b'helper'),
            (real/'MQL5'/'Include'/'Custom'/'Helper.mqh',b'header'),
            (real/'MQL5'/'Libraries'/'CustomLib.ex5',b'library'),
            (real/'MQL5'/'Files'/'settings.dat',b'data'),
            (real.parent/'Common'/'Files'/'shared.dat',b'common'),
            (real/'MQL5'/'Indicators'/'MQLLibraryPreview'/'stale.ex5',b'stale-preview'),
            (real/'MQL5'/'Files'/'MQLLibRender'/'stale.txt',b'stale-protocol'),
        ):
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)

        preview_keep=rt/'MQL5'/'Indicators'/'MQLLibraryPreview'/'live.ex5'
        protocol_keep=rt/'MQL5'/'Files'/'MQLLibRender'/'current.job'
        preview_keep.parent.mkdir(parents=True,exist_ok=True);preview_keep.write_bytes(b'live-preview')
        protocol_keep.parent.mkdir(parents=True,exist_ok=True);protocol_keep.write_text('live-job',encoding='utf-8')
        (rt/'.stamp').write_text('runtime-v1',encoding='utf-8')

        seeded=seed_render_dependencies_from_data_dir(real,rt)
        checks['dependency_seed_not_cached_first']=not seeded.get('cached',True)
        checks['helper_indicator_seeded']=(rt/'MQL5'/'Indicators'/'Helpers'/'Helper.ex5').read_bytes()==b'helper'
        checks['include_seeded']=(rt/'MQL5'/'Include'/'Custom'/'Helper.mqh').read_bytes()==b'header'
        checks['library_seeded']=(rt/'MQL5'/'Libraries'/'CustomLib.ex5').read_bytes()==b'library'
        checks['files_seeded']=(rt/'MQL5'/'Files'/'settings.dat').read_bytes()==b'data'
        checks['common_seeded']=(rt/'Common'/'Files'/'shared.dat').read_bytes()==b'common'
        checks['preview_workdir_preserved']=preview_keep.read_bytes()==b'live-preview' and not (rt/'MQL5'/'Indicators'/'MQLLibraryPreview'/'stale.ex5').exists()
        checks['protocol_workdir_preserved']=protocol_keep.read_text(encoding='utf-8')=='live-job' and not (rt/'MQL5'/'Files'/'MQLLibRender'/'stale.txt').exists()
        cached=seed_render_dependencies_from_data_dir(real,rt)
        checks['dependency_seed_cached_second']=bool(cached.get('cached'))
    checks['residual_4802_classified']=_render_failure_message('4802')=='indicator OnInit failed (err 4802) — needs dependencies/inputs'
    return checks


def self_test():
    src=mt5_capture_source('MQLLibraryPreview\\abc\\demo','shot.png','0')
    checks={
        'no_input_dialog':'script_show_inputs' not in src,
        'trace_before_icustom':'stage=before_iCustom' in src,
        'trace_after_icustom':'stage=after_iCustom' in src,
        'trace_chart_add':'stage=chart_add' in src,
        'trace_screenshot':'stage=screenshot' in src,
        'compile_error_fast_fail':compile_has_errors('Result: 3 errors, 1 warnings'),
        'compile_zero_errors_not_failure':not compile_has_errors('Result: 0 errors, 2 warnings'),
        'job_id_sanitized':safe_job_id('job id:123')=='job-id-123',
        'mt5_prime_v3_marker':'.mt5-preview-prime-v3'.endswith('prime-v3'),
        'minimal_runtime_seed_v5':'.mql5-seed-v5'.endswith('seed-v5'),
        'shutdown_terminal_numeric':True,
        'mql_dir_name_mt5':mql_dir_name('MT5')=='MQL5',
        'mql_dir_name_mt4':mql_dir_name('MT4')=='MQL4',
        'origin_utf8_decode':decode_origin_bytes(b'C:\\Program Files\\OANDA TMS MT5')=='C:\\Program Files\\OANDA TMS MT5',
        'origin_utf16_decode':decode_origin_bytes(('C:\\Program Files\\MetaTrader 4').encode('utf-16'))=='C:\\Program Files\\MetaTrader 4',
        'mql_str_backslash_escape':_mql_str(r'MQLLibraryPreview\\abc\\demo')==r'MQLLibraryPreview\\\\abc\\\\demo',
        'resident_ea_timer':'EventSetMillisecondTimer(150)' in RESIDENT_EA_SOURCE,
        'resident_ea_job_protocol':all(x in RESIDENT_EA_SOURCE for x in ('current.job','current.done','heartbeat.txt')),
        'resident_ea_warm_terminal':'TerminalClose' not in RESIDENT_EA_SOURCE,
        'resident_ea_forward_path_host':_resident_indicator_rel('abc',Path('demo.ex5'))=='MQLLibraryPreview/abc/demo',
        'resident_waits_for_positive_bars':'if(bc>0)' in RESIDENT_EA_SOURCE and 'stable>=5' in RESIDENT_EA_SOURCE,
        'resident_wait_budget_12s':'for(int i=0;i<60;i++)' in RESIDENT_EA_SOURCE and 'Sleep(200)' in RESIDENT_EA_SOURCE,
        'resident_repaints_before_shot':'ChartNavigate(0,CHART_END,0)' in RESIDENT_EA_SOURCE and 'for(int i=0;i<6;i++)' in RESIDENT_EA_SOURCE,
        'resident_deep_history_warmup':'CopyRates(_Symbol,_Period,0,3000,rr)' in RESIDENT_EA_SOURCE,
        'selftest_real_baseline_sentinel':'__MQLLIB_EMPTY_CHART__' in RESIDENT_EA_SOURCE,
    }
    checks.update(self_test_discovery())
    checks.update(self_test_render_dependencies())
    if not all(checks.values()):raise RuntimeError(f'Preview self-test failed: {checks}')
    emit({'ok':True,'checks':checks})

def main():
    a=argparse.ArgumentParser();s=a.add_subparsers(dest='cmd',required=True)
    s.add_parser('detect');s.add_parser('self-test')
    p=s.add_parser('preflight');p.add_argument('--source',required=True);p.add_argument('--out',required=True);p.add_argument('--terminal')
    p=s.add_parser('render');p.add_argument('--source',required=True);p.add_argument('--out',required=True);p.add_argument('--terminal');p.add_argument('--job-id')
    p=s.add_parser('render-batch');p.add_argument('--sources',required=True);p.add_argument('--out',required=True);p.add_argument('--terminal');p.add_argument('--job-id')
    p=s.add_parser('render-library');p.add_argument('--db',required=True);p.add_argument('--out',required=True);p.add_argument('--terminal');p.add_argument('--chunk',type=int,default=70);p.add_argument('--timeout',type=int,default=30);p.add_argument('--attempts',type=int,default=2);p.add_argument('--job-id');p.add_argument('--worker-id');p.add_argument('--static-fast-fail',action='store_true',default=False)
    p=s.add_parser('render-server');p.add_argument('--db',required=True);p.add_argument('--out',required=True);p.add_argument('--terminal');p.add_argument('--job-id');p.add_argument('--hang-timeout',type=int,default=40);p.add_argument('--attempts',type=int,default=2);p.add_argument('--mt5-data-dir');p.add_argument('--runtime-id')
    p=s.add_parser('preview-selftest');p.add_argument('--sample',type=int,default=30);p.add_argument('--out',required=True);p.add_argument('--source');p.add_argument('--db');p.add_argument('--terminal');p.add_argument('--workers',type=int,default=4);p.add_argument('--hang-timeout',type=int,default=40);p.add_argument('--min-draw-percent',type=float,default=60.0);p.add_argument('--mt5-data-dir')
    p=s.add_parser('compile-pool');p.add_argument('--db',required=True);p.add_argument('--out',required=True);p.add_argument('--terminal');p.add_argument('--workers',type=int,default=4);p.add_argument('--job-id');p.add_argument('--runtime-id')
    p=s.add_parser('open-source');p.add_argument('--source',required=True)
    p=s.add_parser('memory-stats');p.add_argument('--db',required=True)
    p=s.add_parser('remove-source');p.add_argument('--db',required=True);p.add_argument('--source',required=True)
    p=s.add_parser('archive-reset');p.add_argument('--db',required=True)
    x=a.parse_args()
    if x.cmd=='detect':detect()
    elif x.cmd=='self-test':self_test()
    elif x.cmd=='preflight':preflight(x.source,x.out,x.terminal)
    elif x.cmd=='render':render(x.source,x.out,x.terminal,x.job_id)
    elif x.cmd=='render-batch':
        srcs=json.loads(Path(x.sources).read_text(encoding='utf-8'))
        mt5=[p for p in srcs if Path(p).suffix.lower() in ('.mq5','.ex5')]
        job=safe_job_id(x.job_id)
        lock_path=Path(x.out).parent/'preview-runtime'/'.render.lock'
        with RenderLock(lock_path,timeout=5.0):
            sel=load_runtime_cache(Path(x.out),'MT5') or choose_runtime('MT5',x.terminal)
            render_mt5_batch(mt5,x.out,sel,job)
    elif x.cmd=='render-library':render_library(x.db,x.out,x.terminal,x.chunk,x.timeout,x.attempts,x.job_id,x.worker_id,x.static_fast_fail)
    elif x.cmd=='render-server':render_server(x.db,x.out,x.terminal,x.job_id,x.hang_timeout,x.attempts,x.mt5_data_dir,x.runtime_id)
    elif x.cmd=='preview-selftest':preview_selftest(x.out,x.sample,x.source,x.db,x.terminal,x.workers,x.hang_timeout,x.min_draw_percent,x.mt5_data_dir)
    elif x.cmd=='compile-pool':compile_pool(x.db,x.out,x.terminal,x.workers,x.job_id,x.runtime_id)
    elif x.cmd=='open-source':open_source(x.source)
    elif x.cmd=='memory-stats':memory_stats(x.db)
    elif x.cmd=='remove-source':remove_source(x.db,x.source)
    elif x.cmd=='archive-reset':archive_reset(x.db)

if __name__=='__main__':
    try:main()
    except PreviewRuntimeError as e:emit({'ok':False,'error':f'{type(e).__name__}: {e}','diagnostics':e.diagnostics});sys.exit(1)
    except Exception as e:emit({'ok':False,'error':f'{type(e).__name__}: {e}'});sys.exit(1)
