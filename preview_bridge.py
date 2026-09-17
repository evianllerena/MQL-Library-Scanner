from __future__ import annotations
import argparse, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, time, zipfile
from pathlib import Path


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
    patterns=('MetaTrader*','*MetaTrader*','*MT5*','*MT4*')
    for root in roots:
        dirs=[]
        for pat in patterns:
            try: dirs.extend(root.glob(pat))
            except Exception: pass
        for d in dirs:
            if not d.is_dir(): continue
            for exe,kind,editor in [('terminal64.exe','MT5','metaeditor64.exe'),('terminal.exe','MT4','metaeditor.exe')]:
                p=d/exe
                if p.exists():
                    key=str(p).lower()
                    if key in seen: continue
                    seen.add(key); ed=d/editor
                    out.append({'kind':kind,'terminal':str(p),'editor':str(ed) if ed.exists() else None,'install_dir':str(d)})
    for root in roots[:2]:
        try:
            for d in root.iterdir():
                if not d.is_dir(): continue
                for exe,kind,editor in [('terminal64.exe','MT5','metaeditor64.exe'),('terminal.exe','MT4','metaeditor.exe')]:
                    p=d/exe
                    if p.exists() and str(p).lower() not in seen:
                        seen.add(str(p).lower()); ed=d/editor
                        out.append({'kind':kind,'terminal':str(p),'editor':str(ed) if ed.exists() else None,'install_dir':str(d)})
        except Exception: pass
    return out


def data_dirs():
    base=Path(os.environ.get('APPDATA',''))/'MetaQuotes'/'Terminal'; out=[]
    if not base.exists(): return out
    for d in base.iterdir():
        if not d.is_dir(): continue
        origin=d/'origin.txt'; origin_text=''
        if origin.exists():
            for enc in ('utf-16','utf-8'):
                try: origin_text=origin.read_text(encoding=enc,errors='ignore').strip(); break
                except Exception: pass
        out.append({'path':str(d),'origin':origin_text})
    return out


def enrich(terminals):
    dds=data_dirs()
    for t in terminals:
        install=str(Path(t['terminal']).parent).lower().replace('/','\\'); match=None
        for d in dds:
            o=d['origin'].lower().replace('/','\\')
            if o and (install in o or o in install): match=d['path']; break
        t['data_dir']=match
    return terminals


def detect():
    terms=enrich(candidates()); emit({'ok':True,'terminals':terms}); return terms


def memory_stats(db_s:str):
    db=Path(db_s)
    if not db.exists(): emit({'ok':True,'corrections':0,'verified':0,'families':0,'latest':[]}); return
    conn=sqlite3.connect(db); one=lambda sql: conn.execute(sql).fetchone()[0]; latest=[]
    try:
        for row in conn.execute("SELECT corrected_primary, original_primary, created_at FROM classification_memory ORDER BY id DESC LIMIT 8"):
            latest.append({'corrected_primary':row[0],'original_primary':row[1],'created_at':row[2]})
        result={'ok':True,'corrections':one('SELECT COUNT(*) FROM classification_memory'),'verified':one('SELECT COUNT(*) FROM indicators WHERE human_verified=1'),'families':one("SELECT COUNT(DISTINCT family_fingerprint) FROM classification_memory WHERE family_fingerprint IS NOT NULL AND family_fingerprint != ''"),'latest':latest}
    finally: conn.close()
    emit(result)


def remove_source(db_s:str,source_s:str):
    db=Path(db_s); source=str(Path(source_s))
    if not db.exists(): emit({'ok':True,'removed_indicators':0,'source':source}); return
    conn=sqlite3.connect(db)
    try:
        ids=[r[0] for r in conn.execute("SELECT id FROM indicators WHERE path=? OR path LIKE ? OR path LIKE ?",(source,source.rstrip('\\/')+'\\%',source.rstrip('\\/')+'/%')).fetchall()]
        if ids:
            marks=','.join('?' for _ in ids)
            conn.execute(f"DELETE FROM classification_memory WHERE indicator_id IN ({marks})",ids)
            conn.execute(f"DELETE FROM indicators WHERE id IN ({marks})",ids)
        conn.execute("DELETE FROM sources WHERE path=?",(source,)); conn.commit()
    finally: conn.close()
    emit({'ok':True,'removed_indicators':len(ids),'source':source})


def archive_reset(db_s:str):
    db=Path(db_s); app_dir=db.parent
    downloads=Path(os.environ.get('USERPROFILE',str(app_dir)))/'Downloads'
    out_dir=downloads if downloads.exists() else app_dir
    out_dir.mkdir(parents=True,exist_ok=True)
    stamp=int(time.time()); archive=out_dir/f'MQL_Indicator_Library_Archive_{stamp}.zip'
    if db.exists():
        try:
            conn=sqlite3.connect(db); conn.execute('PRAGMA wal_checkpoint(TRUNCATE)'); conn.close()
        except Exception: pass
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in app_dir.rglob('*'):
            if not p.is_file(): continue
            try: z.write(p,p.relative_to(app_dir))
            except Exception: pass
    for p in [db,Path(str(db)+'-wal'),Path(str(db)+'-shm')]:
        try: p.unlink(missing_ok=True)
        except Exception: pass
    for folder in [app_dir/'diagnostics',app_dir/'previews']:
        try:
            if folder.exists(): shutil.rmtree(folder)
        except Exception: pass
    emit({'ok':True,'archive':str(archive),'app_dir':str(app_dir)})


def safe_mql_string(s:str)->str: return s.replace('\\','\\\\').replace('"','\\"')

def wait_for_any(paths,timeout=35):
    end=time.time()+timeout
    while time.time()<end:
        for p in paths:
            try:
                if p.exists() and p.stat().st_size>0: return p
            except Exception: pass
        time.sleep(.25)
    return None


def read_compile_log(source:Path):
    log=source.with_suffix('.log')
    if not log.exists(): return ''
    for enc in ('utf-16','utf-8','cp1252'):
        try: return log.read_text(encoding=enc,errors='ignore')
        except Exception: pass
    return ''


def compile_mql(editor:Path,source:Path,include_dir:Path|None=None):
    cmd=[str(editor),f'/compile:{source}','/log']
    if include_dir: cmd.append(f'/include:{include_dir}')
    cp=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    return cp.returncode,read_compile_log(source)


def copy_local_includes(src:Path,dst:Path,root_src:Path,seen=None):
    if seen is None: seen=set()
    key=str(src.resolve()).lower()
    if key in seen or not src.exists(): return
    seen.add(key)
    try: text=src.read_text(encoding='utf-8',errors='ignore')
    except Exception: return
    for inc in re.findall(r'#\s*include\s*["<]([^">]+)[">]',text,re.I):
        rel=Path(inc.replace('\\',os.sep).replace('/',os.sep))
        for dep in [src.parent/rel,root_src/rel]:
            if dep.exists() and dep.is_file():
                try: rel_out=dep.relative_to(root_src)
                except Exception: rel_out=Path(dep.name)
                out=dst.parent/rel_out; out.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(dep,out)
                copy_local_includes(dep,out,root_src,seen); break


def stage_source(source:Path,mql_root:Path,kind:str):
    ext='.ex5' if kind=='MT5' else '.ex4'; job=hashlib.sha1(str(source.resolve()).lower().encode('utf-8','ignore')).hexdigest()[:14]
    stage_dir=mql_root/'Indicators'/'MQLLibraryPreview'/job; stage_dir.mkdir(parents=True,exist_ok=True)
    staged=stage_dir/source.name; shutil.copy2(source,staged); copy_local_includes(source,staged,source.parent)
    existing=source.with_suffix(ext)
    if existing.exists(): shutil.copy2(existing,staged.with_suffix(ext))
    return job,stage_dir,staged


def cached_preview_path(source:Path,out_dir:Path,kind:str):
    token=hashlib.sha1((kind+'|'+str(source.resolve()).lower()).encode('utf-8','ignore')).hexdigest()[:16]
    return out_dir/f'{token}{".gif" if kind=="MT4" else ".png"}'


def cached_preview(source:Path,out_dir:Path,kind:str):
    out_dir.mkdir(parents=True,exist_ok=True); out=cached_preview_path(source,out_dir,kind)
    try:
        if out.exists() and out.stat().st_mtime_ns>=source.stat().st_mtime_ns and out.stat().st_size>0: return out
    except Exception: pass
    return None


def source_separate(source:Path):
    try: return 'indicator_separate_window' in source.read_text(encoding='utf-8',errors='ignore').lower()
    except Exception: return False


def render_mt5(source:Path,out_dir:Path,t:dict):
    cache=cached_preview(source,out_dir,'MT5')
    if cache: return cache,True
    terminal=Path(t['terminal']); editor=Path(t.get('editor') or ''); data=Path(t.get('data_dir') or '')
    if not editor.exists(): raise RuntimeError('MetaEditor 5 was not found beside the selected terminal.')
    if not data.exists(): raise RuntimeError('MetaTrader 5 data directory could not be mapped. Open MT5 once, then retry.')
    mql=data/'MQL5'; scripts=mql/'Scripts'; scripts.mkdir(parents=True,exist_ok=True)
    job,stage_dir,staged=stage_source(source,mql,'MT5'); compiled=staged.with_suffix('.ex5')
    if not compiled.exists():
        rc,log=compile_mql(editor,staged,mql)
        if not compiled.exists(): raise RuntimeError('MT5 staging compile did not produce EX5. '+(log[-1600:] if log else f'MetaEditor exit code {rc}. Staged source: {staged}'))
    rel=f'MQLLibraryPreview\\{job}\\{compiled.stem}'; shot=f'MQLLibraryPreview_{job}.png'; renderer=scripts/f'MQLLibraryPreview_{job}.mq5'; sep=source_separate(source)
    renderer.write_text(f'''#property script_show_inputs\nvoid OnStart(){{\n int h=iCustom(_Symbol,_Period,"{safe_mql_string(rel)}");\n if(h==INVALID_HANDLE){{Print("MQLLIB_PREVIEW iCustom failed ",GetLastError()); return;}}\n int win={1 if sep else 0}; if(win==1) win=(int)ChartGetInteger(0,CHART_WINDOWS_TOTAL);\n if(!ChartIndicatorAdd(0,win,h)){{Print("MQLLIB_PREVIEW add failed ",GetLastError()); return;}}\n ChartRedraw(); Sleep(3500);\n bool ok=ChartScreenShot(0,"{shot}",1200,720,ALIGN_RIGHT); Print("MQLLIB_PREVIEW screenshot=",ok," err=",GetLastError());\n Sleep(500); ChartClose(0);\n}}\n''',encoding='utf-8')
    rc,log=compile_mql(editor,renderer,mql)
    if not renderer.with_suffix('.ex5').exists(): raise RuntimeError('Could not compile MT5 preview capture script. '+(log[-1200:] if log else f'Exit code {rc}.'))
    screenshots=[mql/'Files'/shot,data/shot]
    for p in screenshots:
        try: p.unlink(missing_ok=True)
        except Exception: pass
    out_dir.mkdir(parents=True,exist_ok=True); config=out_dir/f'mt5-preview-{job}.ini'; config.write_text(f'[StartUp]\nSymbol=EURUSD\nPeriod=H1\nScript={renderer.stem}\n',encoding='utf-8')
    subprocess.Popen([str(terminal),f'/config:{config}'],cwd=str(terminal.parent),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    found=wait_for_any(screenshots,40)
    if not found: raise RuntimeError(f'MT5 opened but no screenshot was produced. Renderer={renderer.stem}; staged={staged}. The terminal may already be running or EURUSD/history may be unavailable.')
    final=cached_preview_path(source,out_dir,'MT5'); shutil.copy2(found,final); return final,False


def mt4_template(indicator_name:str,separate:bool):
    main='''<window>\nheight=420\n<indicator>\nname=main\n</indicator>\n'''
    block=f'''<indicator>\nname=Custom Indicator\n<expert>\nname={indicator_name}\nflags=339\nwindow_num={1 if separate else 0}\n</expert>\nshow_data=1\n</indicator>\n'''
    if separate: return '<chart>\nsymbol=EURUSD\nperiod=60\ngraph=1\ngrid=1\nshift=1\n'+main+'</window>\n<window>\nheight=180\n'+block+'</window>\n</chart>\n'
    return '<chart>\nsymbol=EURUSD\nperiod=60\ngraph=1\ngrid=1\nshift=1\n'+main+block+'</window>\n</chart>\n'


def render_mt4(source:Path,out_dir:Path,t:dict):
    cache=cached_preview(source,out_dir,'MT4')
    if cache: return cache,True
    terminal=Path(t['terminal']); editor=Path(t.get('editor') or ''); data=Path(t.get('data_dir') or '')
    if not editor.exists(): raise RuntimeError('MetaEditor 4 was not found beside the selected terminal.')
    if not data.exists(): raise RuntimeError('MetaTrader 4 data directory could not be mapped. Open MT4 once, then retry.')
    mql=data/'MQL4'; scripts=mql/'Scripts'; templates=data/'profiles'/'templates'; files=mql/'Files'
    scripts.mkdir(parents=True,exist_ok=True); templates.mkdir(parents=True,exist_ok=True); files.mkdir(parents=True,exist_ok=True)
    job,stage_dir,staged=stage_source(source,mql,'MT4'); compiled=staged.with_suffix('.ex4')
    if not compiled.exists():
        rc,log=compile_mql(editor,staged,mql)
        if not compiled.exists(): raise RuntimeError('MT4 staging compile did not produce EX4. '+(log[-1600:] if log else f'MetaEditor exit code {rc}. Staged source: {staged}'))
    rel=f'MQLLibraryPreview\\{job}\\{compiled.stem}'; template_name=f'MQLLibraryPreview_{job}'; tpl=templates/f'{template_name}.tpl'; tpl.write_text(mt4_template(rel,source_separate(source)),encoding='utf-8')
    shot=f'MQLLibraryPreview_{job}.gif'; capture=scripts/f'MQLLibraryPreviewCapture_{job}.mq4'
    capture.write_text(f'''#property strict\nvoid OnStart(){{ Sleep(3500); WindowRedraw(); bool ok=WindowScreenShot("{shot}",1200,720); Print("MQLLIB_PREVIEW screenshot=",ok," err=",GetLastError()); Sleep(500); ChartClose(0); }}\n''',encoding='utf-8')
    rc,log=compile_mql(editor,capture,mql)
    if not capture.with_suffix('.ex4').exists(): raise RuntimeError('Could not compile MT4 preview capture script. '+(log[-1200:] if log else f'Exit code {rc}.'))
    screenshots=[files/shot,data/shot,mql/shot]
    for p in screenshots:
        try: p.unlink(missing_ok=True)
        except Exception: pass
    out_dir.mkdir(parents=True,exist_ok=True); config=out_dir/f'mt4-preview-{job}.ini'; config.write_text(f'[StartUp]\nSymbol=EURUSD\nPeriod=H1\nTemplate={template_name}\nScript={capture.stem}\n',encoding='utf-8')
    subprocess.Popen([str(terminal),f'/config:{config}'],cwd=str(terminal.parent),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    found=wait_for_any(screenshots,40)
    if not found: raise RuntimeError(f'MT4 opened but no screenshot was produced. Template={template_name}; script={capture.stem}; staged={staged}. The terminal may already be running or EURUSD/history may be unavailable.')
    final=cached_preview_path(source,out_dir,'MT4'); shutil.copy2(found,final); return final,False


def render(source_s:str,out_s:str,terminal_s:str|None=None):
    source=Path(source_s); out=Path(out_s)
    if not source.exists(): raise RuntimeError(f'Source file does not exist: {source}')
    suffix=source.suffix.lower(); kind='MT5' if suffix in ('.mq5','.ex5') else 'MT4' if suffix in ('.mq4','.ex4') else None
    if not kind: raise RuntimeError(f'Unsupported indicator extension: {source.suffix}')
    terms=enrich(candidates()); eligible=[t for t in terms if t['kind']==kind and t.get('editor')]
    if terminal_s: eligible=[t for t in eligible if t['terminal'].lower()==terminal_s.lower()] or eligible
    if not eligible: raise RuntimeError(f'{kind} + MetaEditor were not detected. Install/open {kind} once first.')
    if suffix in ('.ex4','.ex5') and not source.with_suffix('.mq4' if kind=='MT4' else '.mq5').exists(): raise RuntimeError(f'Compiled-only {source.suffix.upper()} preview needs matching source in this build so the renderer can determine dependencies and window type.')
    actual=source.with_suffix('.mq4' if kind=='MT4' else '.mq5') if suffix.startswith('.ex') else source
    started=time.time(); result,cached=(render_mt5(actual,out,eligible[0]) if kind=='MT5' else render_mt4(actual,out,eligible[0]))
    emit({'ok':True,'image':str(result),'terminal':eligible[0],'kind':kind,'cached':cached,'elapsed_ms':round((time.time()-started)*1000)})


def open_source(source_s:str):
    source=Path(source_s); terms=enrich(candidates()); kind='MT5' if source.suffix.lower() in ('.mq5','.ex5') else 'MT4'; matches=[t for t in terms if t['kind']==kind]
    if not matches: raise RuntimeError(f'{kind} was not detected.')
    editor=matches[0].get('editor')
    if editor and source.suffix.lower() in ('.mq4','.mq5'): subprocess.Popen([editor,str(source)])
    else: subprocess.Popen([matches[0]['terminal']])
    emit({'ok':True,'opened':editor or matches[0]['terminal'],'kind':kind})


def template_smoke(kind:str='MT4'):
    if kind.upper()!='MT4': raise RuntimeError('Only MT4 template smoke is defined.')
    text=mt4_template('MQLLibraryPreview\\sample\\SampleIndicator',True); ok='<indicator>' in text and 'name=Custom Indicator' in text and 'window_num=1' in text and 'SampleIndicator' in text
    emit({'ok':ok,'kind':'MT4','template':text})
    if not ok: raise RuntimeError('MT4 template smoke test failed.')


def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest='cmd',required=True)
    sub.add_parser('detect')
    p=sub.add_parser('render'); p.add_argument('--source',required=True); p.add_argument('--out',required=True); p.add_argument('--terminal')
    p=sub.add_parser('open-source'); p.add_argument('--source',required=True)
    p=sub.add_parser('memory-stats'); p.add_argument('--db',required=True)
    p=sub.add_parser('remove-source'); p.add_argument('--db',required=True); p.add_argument('--source',required=True)
    p=sub.add_parser('archive-reset'); p.add_argument('--db',required=True)
    p=sub.add_parser('template-smoke'); p.add_argument('--kind',default='MT4')
    a=ap.parse_args()
    if a.cmd=='detect': detect()
    elif a.cmd=='render': render(a.source,a.out,a.terminal)
    elif a.cmd=='open-source': open_source(a.source)
    elif a.cmd=='memory-stats': memory_stats(a.db)
    elif a.cmd=='remove-source': remove_source(a.db,a.source)
    elif a.cmd=='archive-reset': archive_reset(a.db)
    elif a.cmd=='template-smoke': template_smoke(a.kind)

if __name__=='__main__':
    try: main()
    except Exception as exc:
        emit({'ok':False,'error':f'{type(exc).__name__}: {exc}'}); sys.exit(1)
