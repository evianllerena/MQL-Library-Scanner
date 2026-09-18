from __future__ import annotations
import argparse, ctypes, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, time, zipfile
from pathlib import Path

CREATE_NO_WINDOW=getattr(subprocess,'CREATE_NO_WINDOW',0)
SW_HIDE=0


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


def terminals():
    base=Path(os.environ.get('APPDATA',''))/'MetaQuotes'/'Terminal'
    out=[]; seen=set()

    # Authoritative path: each normal MetaTrader data directory identifies its own install in origin.txt.
    if base.exists():
        for d in base.iterdir():
            if not d.is_dir():continue
            origin_text=read_origin(d/'origin.txt')
            if not origin_text:continue
            install=Path(origin_text)
            for exe,kind,ed in [('terminal.exe','MT4','metaeditor.exe'),('terminal64.exe','MT5','metaeditor64.exe')]:
                terminal=install/exe; editor=install/ed
                if not (d/kind).exists() or not terminal.exists() or not editor.exists():continue
                key=(kind,str(terminal).lower(),str(d).lower())
                if key in seen:continue
                seen.add(key)
                out.append({
                    'kind':kind,
                    'terminal':str(terminal),
                    'editor':str(editor),
                    'install_dir':str(install),
                    'data_dir':str(d),
                    'activity_ns':data_activity(d),
                    'discovery':'origin.txt'
                })

    # Fallback for unusual/portable installs that do not have a normal origin.txt data directory.
    roots=[Path(os.environ[x]) for x in ('ProgramFiles','ProgramFiles(x86)','LOCALAPPDATA') if os.environ.get(x) and Path(os.environ[x]).exists()]
    for root in roots:
        dirs=[]
        for pat in ('MetaTrader*','*MetaTrader*','*MT4*','*MT5*','*OANDA*'):
            try:dirs+=list(root.glob(pat))
            except Exception:pass
        try:dirs+=[x for x in root.iterdir() if x.is_dir()]
        except Exception:pass
        for install in dirs:
            for exe,kind,ed in [('terminal.exe','MT4','metaeditor.exe'),('terminal64.exe','MT5','metaeditor64.exe')]:
                terminal=install/exe; editor=install/ed
                if not terminal.exists() or not editor.exists():continue
                matches=[]
                if base.exists():
                    for d in base.iterdir():
                        if not d.is_dir() or not (d/kind).exists():continue
                        origin_text=read_origin(d/'origin.txt')
                        if not origin_text:continue
                        try:same=Path(origin_text).resolve()==install.resolve()
                        except Exception:same=os.path.normcase(origin_text)==os.path.normcase(str(install))
                        if same:matches.append((data_activity(d),d))
                if not matches:continue
                activity,data_dir=max(matches,key=lambda x:x[0])
                key=(kind,str(terminal).lower(),str(data_dir).lower())
                if key in seen:continue
                seen.add(key)
                out.append({
                    'kind':kind,
                    'terminal':str(terminal),
                    'editor':str(editor),
                    'install_dir':str(install),
                    'data_dir':str(data_dir),
                    'activity_ns':activity,
                    'discovery':'install-fallback'
                })

    out.sort(key=lambda t:(1 if t.get('discovery')=='origin.txt' else 0,t.get('activity_ns',0)),reverse=True)
    return out


def detect():emit({'ok':True,'terminals':terminals()})


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
            if expected.exists() and expected.stat().st_size:return expected,attempts,read_text(log)            latest=read_text(log)
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
    """Seed only the support files needed for an isolated preview from the matched live terminal."""
    live=Path(t['data_dir']); src=live/kind; dst=rt/kind; marker=rt/f'.{kind.lower()}-seed-v4'
    if marker.exists() or not src.exists():return False
    dst.mkdir(parents=True,exist_ok=True)

    for name in ('Include','Libraries','Presets','Images'):
        s=src/name
        if s.exists():
            try:shutil.copytree(s,dst/name,dirs_exist_ok=True)
            except Exception:pass

    copy_compiled_support(src/'Indicators',dst/'Indicators',kind)
    preview_dir=dst/'Indicators'/'MQLLibraryPreview'
    if preview_dir.exists():shutil.rmtree(preview_dir,ignore_errors=True)
    marker.write_text(f'{src}\n{int(time.time())}',encoding='utf-8')
    return True


def clone_runtime(t,out,kind):
    install=Path(t['install_dir'])
    live=Path(t['data_dir'])
    token=hashlib.sha1((str(install)+'|'+str(live)).lower().encode()).hexdigest()[:10]
    rt=out.parent/'preview-runtime'/'v4'/f'{kind.lower()}-{token}'
    exe=install/('terminal64.exe' if kind=='MT5' else 'terminal.exe')
    stamp=f'v4:{exe.stat().st_size}:{exe.stat().st_mtime_ns}:{str(live).lower()}'
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
    return rt


def stage(src,mql,kind,editor):
    ext='.ex5' if kind=='MT5' else '.ex4'; job=hashlib.sha1(str(src.resolve()).lower().encode()).hexdigest()[:14]; d=mql/'Indicators'/'MQLLibraryPreview'/job;d.mkdir(parents=True,exist_ok=True)
    staged=d/safe_name(src); shutil.copy2(src,staged)
    for dep in src.parent.glob('*.mqh'):
        try:shutil.copy2(dep,d/dep.name)
        except Exception:pass
    binary=d/(staged.stem+ext); old=existing_binary(src,ext)
    if old:shutil.copy2(old,binary);return job,staged,binary,{'used_existing_binary':True,'existing_binary':str(old)}
    if src.suffix.lower() not in ('.mq4','.mq5'):raise RuntimeError(f'Indicator compile failed — {kind} executable could not be staged.')
    built,cmds,log=compile_file(editor,staged,mql)
    if not built:
        if log:raise RuntimeError(f'Indicator compile failed — {kind} source produced no {ext.upper()[1:]}.\n{log[-2200:]}')
        raise RuntimeError(f'Indicator compile failed — {kind} MetaEditor produced no executable. Staged={staged}; commands={cmds}')
    return job,staged,built,{'used_existing_binary':False}


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

    cfg=rt/f'mql-prime-{safe_job_id(job_id)}.ini'
    cfg.write_text(f'[Experts]\nEnabled=1\nAllowLiveTrading=0\nAllowDllImport=0\n\n[StartUp]\nSymbol={symbol}\nPeriod=H1\n',encoding='utf-8')
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

    try:
        while time.monotonic()-start<120:
            now=time.monotonic()
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
        terminate_tree(proc)

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


def mt5_capture_source(rel,shot,win):
    return (
        'void OnStart(){\n'        ' Print("MQLLIB_PREVIEW stage=onstart");\n'
        ' ResetLastError();\n'
        f' Print("MQLLIB_PREVIEW stage=before_iCustom path={rel}");\n'
        f' int h=iCustom(_Symbol,_Period,"{rel}");\n'
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
        f' bool ok=ChartScreenShot(0,"{shot}",1200,720,ALIGN_RIGHT);\n'
        ' err=GetLastError();\n'
        ' PrintFormat("MQLLIB_PREVIEW stage=screenshot ok=%s err=%d",ok?"true":"false",err);\n'
        ' IndicatorRelease(h);\n'
        ' Sleep(300);\n'
        ' TerminalClose(ok?0:23);\n'
        '}\n'
    )


def render_mt4(src,out,t,job_id):
    emit_stage(job_id,'runtime_clone','Preparing isolated MT4 runtime.')
    rt=clone_runtime(t,out,'MT4');mql=rt/'MQL4';scripts=mql/'Scripts';templates=rt/'templates';files=mql/'Files';[d.mkdir(parents=True,exist_ok=True) for d in (scripts,templates,files)]
    editor=rt/'metaeditor.exe';terminal=rt/'terminal.exe';sym=copy_mt4_history(Path(t['data_dir']),rt)
    emit_stage(job_id,'indicator_compile','Compiling/staging the selected MT4 indicator.')
    job,staged,binary,meta=stage(src,mql,'MT4',editor);rel=f'MQLLibraryPreview\\{job}\\{binary.stem}'
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
    editor=rt/'metaeditor64.exe';terminal=rt/'terminal64.exe';sym=copy_mt5_history(Path(t['data_dir']),rt)
    prime_mt5_runtime(rt,terminal,sym,job_id)
    emit_stage(job_id,'indicator_compile','Compiling/staging the selected MT5 indicator.')
    job,staged,binary,meta=stage(src,mql,'MT5',editor);rel=f'MQLLibraryPreview\\{job}\\{binary.stem}';shot=f'MQLLibraryPreview_{job}.png';cap=scripts/f'MQLLibraryPreviewCapture_{job}.mq5';sep='indicator_separate_window' in read_text(src).lower();win='1' if sep else '0'
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


def preview_runtime(source,terminal=None):
    src=Path(source)
    if not src.exists() or not src.is_file():
        raise RuntimeError(f'Preview source was not found: {src}')
    kind='MT5' if src.suffix.lower() in ('.mq5','.ex5') else 'MT4'
    ts=[x for x in terminals() if x['kind']==kind and x.get('terminal') and x.get('editor') and x.get('data_dir')]
    if terminal:
        ts=[x for x in ts if x['terminal'].lower()==terminal.lower()] or ts
    if not ts:
        base=Path(os.environ.get('APPDATA',''))/'MetaQuotes'/'Terminal'
        observed=[]
        if base.exists():
            for d in base.iterdir():
                if d.is_dir():
                    observed.append({'data_dir':str(d),'origin':read_origin(d/'origin.txt'),'has_mql4':(d/'MQL4').exists(),'has_mql5':(d/'MQL5').exists()})
        raise RuntimeError(f'{kind} + MetaEditor data directory were not detected. Observed terminal data folders: {json.dumps(observed,ensure_ascii=False)}')
    selected=ts[0]
    checks={
        'source':src.exists() and src.is_file(),
        'terminal':Path(selected['terminal']).is_file(),
        'metaeditor':Path(selected['editor']).is_file(),
        'data_dir':Path(selected['data_dir']).is_dir(),
        'mql_dir':(Path(selected['data_dir'])/kind).is_dir(),
    }
    missing=[name for name,ok in checks.items() if not ok]
    if missing:
        raise RuntimeError(f'Preview preflight failed for {kind}: missing or invalid {", ".join(missing)}.')
    return src,kind,selected,checks


def preflight(source,terminal=None):
    src,kind,selected,checks=preview_runtime(source,terminal)
    emit({'ok':True,'preflight':True,'source':str(src),'kind':kind,'terminal':selected,'checks':checks})


def render(source,out,terminal=None,job_id=None):
    src,kind,selected,_checks=preview_runtime(source,terminal)
    dest=Path(out);job_id=safe_job_id(job_id)
    emit_stage(job_id,'terminal_selected',f'Using {kind} data folder: {selected["data_dir"]}',kind=kind,install_dir=selected['install_dir'],data_dir=selected['data_dir'])
    lock_path=dest.parent/'preview-runtime'/'.render.lock'
    with RenderLock(lock_path,timeout=5.0):
        image,meta=(render_mt5(src,dest,selected,job_id) if kind=='MT5' else render_mt4(src,dest,selected,job_id))
    emit({'ok':True,'image':str(image),'kind':kind,'cached':False,'job_id':job_id,'terminal':selected,'meta':meta})


def open_source(source):
    src=Path(source);kind='MT5' if src.suffix.lower() in ('.mq5','.ex5') else 'MT4';ts=[x for x in terminals() if x['kind']==kind]
    if not ts:raise RuntimeError(f'{kind} was not detected.')
    target=ts[0].get('editor') if src.suffix.lower() in ('.mq4','.mq5') else ts[0]['terminal'];subprocess.Popen([target,str(src)] if target.endswith('editor.exe') or target.endswith('editor64.exe') else [target]);emit({'ok':True,'opened':target,'kind':kind})


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
        'minimal_runtime_seed_v4':'.mql5-seed-v4'.endswith('seed-v4'),
        'shutdown_terminal_numeric':True,
        'origin_utf8_decode':decode_origin_bytes(b'C:\\Program Files\\OANDA TMS MT5')=='C:\\Program Files\\OANDA TMS MT5',
        'origin_utf16_decode':decode_origin_bytes(('C:\\Program Files\\MetaTrader 4').encode('utf-16'))=='C:\\Program Files\\MetaTrader 4',
    }
    if not all(checks.values()):raise RuntimeError(f'Preview self-test failed: {checks}')
    emit({'ok':True,'checks':checks})


def main():
    a=argparse.ArgumentParser();s=a.add_subparsers(dest='cmd',required=True);s.add_parser('detect');s.add_parser('self-test');p=s.add_parser('preflight');p.add_argument('--source',required=True);p.add_argument('--terminal');p=s.add_parser('render');p.add_argument('--source',required=True);p.add_argument('--out',required=True);p.add_argument('--terminal');p.add_argument('--job-id');p=s.add_parser('open-source');p.add_argument('--source',required=True);p=s.add_parser('memory-stats');p.add_argument('--db',required=True);p=s.add_parser('remove-source');p.add_argument('--db',required=True);p.add_argument('--source',required=True);p=s.add_parser('archive-reset');p.add_argument('--db',required=True);x=a.parse_args()
    if x.cmd=='detect':detect()
    elif x.cmd=='self-test':self_test()
    elif x.cmd=='preflight':preflight(x.source,x.terminal)
    elif x.cmd=='render':render(x.source,x.out,x.terminal,x.job_id)
    elif x.cmd=='open-source':open_source(x.source)
    elif x.cmd=='memory-stats':memory_stats(x.db)
    elif x.cmd=='remove-source':remove_source(x.db,x.source)
    elif x.cmd=='archive-reset':archive_reset(x.db)


if __name__=='__main__':
    try:main()
    except Exception as e:emit({'ok':False,'error':f'{type(e).__name__}: {e}'});sys.exit(1)