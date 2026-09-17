from __future__ import annotations
import argparse, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, time, zipfile
from pathlib import Path

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def emit(obj):
    data=(json.dumps(obj,ensure_ascii=False,default=str)+'\n').encode('utf-8',errors='backslashreplace')
    try:
        sys.stdout.buffer.write(data); sys.stdout.buffer.flush()
    except Exception:
        print(json.dumps(obj,ensure_ascii=True,default=str),flush=True)


def candidates():
    roots=[]
    for env in ('ProgramFiles','ProgramFiles(x86)','LOCALAPPDATA'):
        v=os.environ.get(env)
        if v and Path(v).exists(): roots.append(Path(v))
    seen=set(); out=[]
    pats=('MetaTrader*','*MetaTrader*','*MT5*','*MT4*')
    for root in roots:
        dirs=[]
        for pat in pats:
            try: dirs.extend(root.glob(pat))
            except Exception: pass
        try: dirs.extend([x for x in root.iterdir() if x.is_dir()])
        except Exception: pass
        for d in dirs:
            if not d.is_dir(): continue
            for exe,kind,editor in [('terminal64.exe','MT5','metaeditor64.exe'),('terminal.exe','MT4','metaeditor.exe')]:
                p=d/exe
                if not p.exists(): continue
                k=str(p).lower()
                if k in seen: continue
                seen.add(k); ed=d/editor
                out.append({'kind':kind,'terminal':str(p),'editor':str(ed) if ed.exists() else None,'install_dir':str(d)})
    return out


def data_dirs():
    base=Path(os.environ.get('APPDATA',''))/'MetaQuotes'/'Terminal'; out=[]
    if not base.exists(): return out
    for d in base.iterdir():
        if not d.is_dir(): continue
        origin=d/'origin.txt'; txt=''
        if origin.exists():
            for enc in ('utf-16','utf-8','cp1252'):
                try: txt=origin.read_text(encoding=enc,errors='ignore').strip(); break
                except Exception: pass
        out.append({'path':str(d),'origin':txt})
    return out


def enrich(terms):
    dds=data_dirs()
    for t in terms:
        install=str(Path(t['terminal']).parent).lower().replace('/','\\'); match=None
        for d in dds:
            o=d['origin'].lower().replace('/','\\')
            if o and (install in o or o in install): match=d['path']; break
        t['data_dir']=match
    return terms


def detect():
    terms=enrich(candidates()); emit({'ok':True,'terminals':terms}); return terms


def memory_stats(db_s):
    db=Path(db_s)
    if not db.exists(): emit({'ok':True,'corrections':0,'verified':0,'families':0,'latest':[]}); return
    conn=sqlite3.connect(db)
    try:
        one=lambda q: conn.execute(q).fetchone()[0]
        latest=[{'corrected_primary':r[0],'original_primary':r[1],'created_at':r[2]} for r in conn.execute('SELECT corrected_primary,original_primary,created_at FROM classification_memory ORDER BY id DESC LIMIT 8')]
        emit({'ok':True,'corrections':one('SELECT COUNT(*) FROM classification_memory'),'verified':one('SELECT COUNT(*) FROM indicators WHERE human_verified=1'),'families':one("SELECT COUNT(DISTINCT family_fingerprint) FROM classification_memory WHERE family_fingerprint IS NOT NULL AND family_fingerprint != ''"),'latest':latest})
    finally: conn.close()


def remove_source(db_s,source_s):
    db=Path(db_s); source=str(Path(source_s)); removed=0
    if db.exists():
        conn=sqlite3.connect(db)
        try:
            ids=[r[0] for r in conn.execute('SELECT id FROM indicators WHERE path=? OR path LIKE ? OR path LIKE ?',(source,source.rstrip('\\/')+'\\%',source.rstrip('\\/')+'/%')).fetchall()]
            removed=len(ids)
            if ids:
                marks=','.join('?' for _ in ids)
                conn.execute(f'DELETE FROM classification_memory WHERE indicator_id IN ({marks})',ids)
                conn.execute(f'DELETE FROM indicators WHERE id IN ({marks})',ids)
            conn.execute('DELETE FROM sources WHERE path=?',(source,)); conn.commit()
        finally: conn.close()
    emit({'ok':True,'removed_indicators':removed,'source':source})


def archive_reset(db_s):
    db=Path(db_s); app_dir=db.parent
    downloads=Path(os.environ.get('USERPROFILE',str(app_dir)))/'Downloads'; out_dir=downloads if downloads.exists() else app_dir
    out_dir.mkdir(parents=True,exist_ok=True); archive=out_dir/f'MQL_Indicator_Library_Archive_{int(time.time())}.zip'
    if db.exists():
        try:
            conn=sqlite3.connect(db); conn.execute('PRAGMA wal_checkpoint(TRUNCATE)'); conn.close()
        except Exception: pass
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in app_dir.rglob('*'):
            if p.is_file():
                try: z.write(p,p.relative_to(app_dir))
                except Exception: pass
    for p in (db,Path(str(db)+'-wal'),Path(str(db)+'-shm')):
        try: p.unlink(missing_ok=True)
        except Exception: pass
    for folder in (app_dir/'diagnostics',app_dir/'previews'):
        try:
            if folder.exists(): shutil.rmtree(folder)
        except Exception: pass
    emit({'ok':True,'archive':str(archive),'app_dir':str(app_dir)})


def safe_mql_string(s): return str(s).replace('\\','\\\\').replace('"','\\"')


def read_text_any(path):
    if not path.exists(): return ''
    for enc in ('utf-16','utf-8','cp1252','latin1'):
        try: return path.read_text(encoding=enc,errors='ignore')
        except Exception: pass
    return ''


def wait_for_file(path,timeout=10):
    end=time.time()+timeout
    while time.time()<end:
        try:
            if path.exists() and path.stat().st_size>0: return True
        except Exception: pass
        time.sleep(.2)
    return False


def wait_for_any(paths,timeout=40):
    end=time.time()+timeout
    while time.time()<end:
        for p in paths:
            try:
                if p.exists() and p.stat().st_size>0: return p
            except Exception: pass
        time.sleep(.25)
    return None


def compile_once(editor,source,mql_root,relative=False):
    log=source.with_suffix('.log')
    try: log.unlink(missing_ok=True)
    except Exception: pass
    target=source.name if relative else str(source)
    cmd=[str(editor),f'/compile:{target}',f'/inc:{mql_root}','/log']
    cp=subprocess.run(cmd,cwd=str(source.parent) if relative else str(editor.parent),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60,creationflags=CREATE_NO_WINDOW)
    expected=source.with_suffix('.ex5' if source.suffix.lower()=='.mq5' else '.ex4')
    wait_for_file(expected,8)
    return {'returncode':cp.returncode,'cmd':cmd,'expected':str(expected),'log_path':str(log),'log':read_text_any(log),'stdout':cp.stdout.decode('utf-8','ignore') if cp.stdout else '','stderr':cp.stderr.decode('utf-8','ignore') if cp.stderr else ''}


def compile_mql(editor,source,mql_root):
    expected=source.with_suffix('.ex5' if source.suffix.lower()=='.mq5' else '.ex4')
    attempts=[]
    for relative in (False,True):
        a=compile_once(editor,source,mql_root,relative); attempts.append(a)
        if expected.exists() and expected.stat().st_size>0: return expected,attempts
    return None,attempts


def include_dependencies(src,stage_dir,source_root,seen=None):
    if seen is None: seen=set()
    try: key=str(src.resolve()).lower()
    except Exception: key=str(src).lower()
    if key in seen or not src.exists(): return
    seen.add(key); text=read_text_any(src)
    for inc in re.findall(r'#\s*include\s*["<]([^">]+)[">]',text,re.I):
        rel=Path(inc.replace('\\',os.sep).replace('/',os.sep)); found=None
        for dep in (src.parent/rel,source_root/rel):
            if dep.exists() and dep.is_file(): found=dep; break
        if not found: continue
        try: relout=found.relative_to(source_root)
        except Exception: relout=Path(found.name)
        out=stage_dir/relout; out.parent.mkdir(parents=True,exist_ok=True)
        try: shutil.copy2(found,out)
        except Exception: continue
        include_dependencies(found,stage_dir,source_root,seen)


def find_existing_compiled(source,ext):
    if source.suffix.lower()==ext and source.exists(): return source
    direct=source.with_suffix(ext)
    if direct.exists() and direct.stat().st_size>0: return direct
    stem=source.stem.lower()
    try:
        for p in source.parent.iterdir():
            if p.is_file() and p.suffix.lower()==ext and p.stem.lower()==stem and p.stat().st_size>0: return p
    except Exception: pass
    return None


def stage_source(source,mql_root,kind):
    ext='.ex5' if kind=='MT5' else '.ex4'; job=hashlib.sha1(str(source.resolve()).lower().encode('utf-8','ignore')).hexdigest()[:14]
    stage=mql_root/'Indicators'/'MQLLibraryPreview'/job; stage.mkdir(parents=True,exist_ok=True)
    staged=stage/source.name; shutil.copy2(source,staged)
    if source.suffix.lower() in ('.mq4','.mq5'): include_dependencies(source,stage,source.parent)
    existing=find_existing_compiled(source,ext); staged_binary=stage/(source.stem+ext)
    if existing: shutil.copy2(existing,staged_binary)
    return job,stage,staged,staged_binary,existing


def source_separate(source):
    if source.suffix.lower() not in ('.mq4','.mq5'): return False
    return 'indicator_separate_window' in read_text_any(source).lower()


def cache_path(source,out_dir,kind):
    token=hashlib.sha1((kind+'|'+str(source.resolve()).lower()).encode('utf-8','ignore')).hexdigest()[:16]
    return out_dir/f'{token}{".gif" if kind=="MT4" else ".png"}'


def cached_preview(source,out_dir,kind):
    out=cache_path(source,out_dir,kind)
    try:
        if out.exists() and out.stat().st_size>0 and out.stat().st_mtime_ns>=source.stat().st_mtime_ns: return out
    except Exception: pass
    return None


def compile_error(kind,staged,attempts):
    logs='\n\n'.join(a['log'] for a in attempts if a.get('log')).strip(); cmds=[' '.join(a['cmd']) for a in attempts]
    if logs: return f'{kind} source did not compile and no existing {"EX5" if kind=="MT5" else "EX4"} was found.\n{logs[-2200:]}'
    return f'{kind} MetaEditor produced no executable after two compile attempts. Staged source: {staged}. Commands: {cmds}'


def ensure_binary(source,mql_root,kind,editor):
    job,stage,staged,binary,existing=stage_source(source,mql_root,kind)
    if binary.exists() and binary.stat().st_size>0: return job,stage,staged,binary,{'used_existing_binary':True,'existing_binary':str(existing) if existing else None,'attempts':[]}
    if source.suffix.lower() not in ('.mq4','.mq5'): raise RuntimeError(f'{kind} executable could not be staged from {source}.')
    built,attempts=compile_mql(editor,staged,mql_root)
    if not built: raise RuntimeError(compile_error(kind,staged,attempts))
    return job,stage,staged,built,{'used_existing_binary':False,'existing_binary':None,'attempts':attempts}


def render_mt5(source,out_dir,t):
    cache=cached_preview(source,out_dir,'MT5')
    if cache: return cache,True,{'cached':True}
    terminal=Path(t['terminal']); editor=Path(t.get('editor') or ''); data=Path(t.get('data_dir') or '')
    if not editor.exists(): raise RuntimeError('MetaEditor 5 was not found beside the selected terminal.')
    if not data.exists(): raise RuntimeError('MetaTrader 5 data directory could not be mapped. Open MT5 once, then retry.')
    mql=data/'MQL5'; scripts=mql/'Scripts'; scripts.mkdir(parents=True,exist_ok=True)
    job,stage,staged,binary,meta=ensure_binary(source,mql,'MT5',editor)
    rel=f'MQLLibraryPreview\\{job}\\{binary.stem}'; shot=f'MQLLibraryPreview_{job}.png'; renderer=scripts/f'MQLLibraryPreview_{job}.mq5'; sep=source_separate(source)
    renderer.write_text(f'''#property script_show_inputs\nvoid OnStart(){{ int h=iCustom(_Symbol,_Period,"{safe_mql_string(rel)}"); if(h==INVALID_HANDLE){{Print("MQLLIB_PREVIEW iCustom failed ",GetLastError()); return;}} int win={1 if sep else 0}; if(win==1) win=(int)ChartGetInteger(0,CHART_WINDOWS_TOTAL); if(!ChartIndicatorAdd(0,win,h)){{Print("MQLLIB_PREVIEW add failed ",GetLastError()); return;}} ChartRedraw(); Sleep(3500); bool ok=ChartScreenShot(0,"{shot}",1200,720,ALIGN_RIGHT); Print("MQLLIB_PREVIEW screenshot=",ok," err=",GetLastError()); Sleep(400); ChartClose(0); }}\n''',encoding='utf-8')
    rex,rattempts=compile_mql(editor,renderer,mql)
    if not rex: raise RuntimeError('Could not compile MT5 preview capture script. '+compile_error('MT5',renderer,rattempts))
    screenshots=[mql/'Files'/shot,data/shot]
    for p in screenshots:
        try: p.unlink(missing_ok=True)
        except Exception: pass
    out_dir.mkdir(parents=True,exist_ok=True); cfg=out_dir/f'mt5-preview-{job}.ini'; cfg.write_text(f'[StartUp]\nSymbol=EURUSD\nPeriod=H1\nScript={renderer.stem}\n',encoding='utf-8')
    subprocess.Popen([str(terminal),f'/config:{cfg}'],cwd=str(terminal.parent),creationflags=CREATE_NO_WINDOW)
    found=wait_for_any(screenshots,45)
    if not found: raise RuntimeError(f'MT5 executable was prepared successfully but no chart screenshot was produced within 45 seconds. Terminal may already be running, the startup script may not have executed, or EURUSD/history may be unavailable. staged={staged}; binary={binary}')
    final=cache_path(source,out_dir,'MT5'); shutil.copy2(found,final); meta.update({'renderer':str(renderer),'staged':str(staged),'binary':str(binary)}); return final,False,meta


def mt4_template(indicator_name,separate):
    main='<window>\nheight=420\n<indicator>\nname=main\n</indicator>\n'; block=f'<indicator>\nname=Custom Indicator\n<expert>\nname={indicator_name}\nflags=339\nwindow_num={1 if separate else 0}\n</expert>\nshow_data=1\n</indicator>\n'; head='<chart>\nsymbol=EURUSD\nperiod=60\ngraph=1\ngrid=1\nshift=1\n'
    if separate: return head+main+'</window>\n<window>\nheight=180\n'+block+'</window>\n</chart>\n'
    return head+main+block+'</window>\n</chart>\n'


def render_mt4(source,out_dir,t):
    cache=cached_preview(source,out_dir,'MT4')
    if cache: return cache,True,{'cached':True}
    terminal=Path(t['terminal']); editor=Path(t.get('editor') or ''); data=Path(t.get('data_dir') or '')
    if not editor.exists(): raise RuntimeError('MetaEditor 4 was not found beside the selected terminal.')
    if not data.exists(): raise RuntimeError('MetaTrader 4 data directory could not be mapped. Open MT4 once, then retry.')
    mql=data/'MQL4'; scripts=mql/'Scripts'; templates=data/'profiles'/'templates'; files=mql/'Files'; scripts.mkdir(parents=True,exist_ok=True); templates.mkdir(parents=True,exist_ok=True); files.mkdir(parents=True,exist_ok=True)
    job,stage,staged,binary,meta=ensure_binary(source,mql,'MT4',editor)
    rel=f'MQLLibraryPreview\\{job}\\{binary.stem}'; template_name=f'MQLLibraryPreview_{job}'; tpl=templates/f'{template_name}.tpl'; tpl.write_text(mt4_template(rel,source_separate(source)),encoding='utf-8')
    shot=f'MQLLibraryPreview_{job}.gif'; capture=scripts/f'MQLLibraryPreviewCapture_{job}.mq4'; capture.write_text(f'''#property strict\nvoid OnStart(){{ Sleep(3500); WindowRedraw(); bool ok=WindowScreenShot("{shot}",1200,720); Print("MQLLIB_PREVIEW screenshot=",ok," err=",GetLastError()); Sleep(400); ChartClose(0); }}\n''',encoding='utf-8')
    cex,cattempts=compile_mql(editor,capture,mql)
    if not cex: raise RuntimeError('Could not compile MT4 preview capture script. '+compile_error('MT4',capture,cattempts))
    screenshots=[files/shot,data/shot,mql/shot]
    for p in screenshots:
        try: p.unlink(missing_ok=True)
        except Exception: pass
    out_dir.mkdir(parents=True,exist_ok=True); cfg=out_dir/f'mt4-preview-{job}.ini'; cfg.write_text(f'[StartUp]\nSymbol=EURUSD\nPeriod=H1\nTemplate={template_name}\nScript={capture.stem}\n',encoding='utf-8')
    subprocess.Popen([str(terminal),f'/config:{cfg}'],cwd=str(terminal.parent),creationflags=CREATE_NO_WINDOW)
    found=wait_for_any(screenshots,45)
    if not found: raise RuntimeError(f'MT4 executable was prepared successfully but no chart screenshot was produced within 45 seconds. Terminal may already be running, the startup template/script may not have executed, or EURUSD/history may be unavailable. staged={staged}; binary={binary}')
    final=cache_path(source,out_dir,'MT4'); shutil.copy2(found,final); meta.update({'template':str(tpl),'capture':str(capture),'staged':str(staged),'binary':str(binary)}); return final,False,meta


def render(source_s,out_s,terminal_s=None):
    source=Path(source_s); out=Path(out_s)
    if not source.exists(): raise RuntimeError(f'Source file does not exist: {source}')
    ext=source.suffix.lower(); kind='MT5' if ext in ('.mq5','.ex5') else 'MT4'; terms=enrich(candidates()); eligible=[t for t in terms if t['kind']==kind and t.get('editor') and t.get('data_dir')]
    if terminal_s:
        exact=[t for t in eligible if t['terminal'].lower()==terminal_s.lower()]
        if exact: eligible=exact
    if not eligible: raise RuntimeError(f'{kind} + MetaEditor data directory were not detected. Open {kind} once, then retry.')
    t=eligible[0]
    if kind=='MT5': image,cached,meta=render_mt5(source,out,t)
    else: image,cached,meta=render_mt4(source,out,t)
    emit({'ok':True,'image':str(image),'kind':kind,'cached':cached,'terminal':t,'meta':meta})


def open_source(source_s):
    source=Path(source_s); kind='MT5' if source.suffix.lower() in ('.mq5','.ex5') else 'MT4'; terms=enrich(candidates()); matches=[t for t in terms if t['kind']==kind]
    if not matches: raise RuntimeError(f'{kind} was not detected.')
    editor=matches[0].get('editor')
    if editor and source.suffix.lower() in ('.mq4','.mq5'): subprocess.Popen([editor,str(source)])
    else: subprocess.Popen([matches[0]['terminal']])
    emit({'ok':True,'opened':editor or matches[0]['terminal'],'kind':kind})


def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest='cmd',required=True); sub.add_parser('detect')
    p=sub.add_parser('render'); p.add_argument('--source',required=True); p.add_argument('--out',required=True); p.add_argument('--terminal')
    p=sub.add_parser('open-source'); p.add_argument('--source',required=True)
    p=sub.add_parser('memory-stats'); p.add_argument('--db',required=True)
    p=sub.add_parser('remove-source'); p.add_argument('--db',required=True); p.add_argument('--source',required=True)
    p=sub.add_parser('archive-reset'); p.add_argument('--db',required=True)
    a=ap.parse_args()
    if a.cmd=='detect': detect()
    elif a.cmd=='render': render(a.source,a.out,a.terminal)
    elif a.cmd=='open-source': open_source(a.source)
    elif a.cmd=='memory-stats': memory_stats(a.db)
    elif a.cmd=='remove-source': remove_source(a.db,a.source)
    elif a.cmd=='archive-reset': archive_reset(a.db)


if __name__=='__main__':
    try: main()
    except Exception as exc:
        emit({'ok':False,'error':f'{type(exc).__name__}: {exc}'}); sys.exit(1)
