from __future__ import annotations
import argparse, json, os, shutil, sqlite3, subprocess, sys, time
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
                    seen.add(key)
                    ed=d/editor
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
    base=Path(os.environ.get('APPDATA',''))/'MetaQuotes'/'Terminal'
    out=[]
    if not base.exists(): return out
    for d in base.iterdir():
        if not d.is_dir(): continue
        origin=d/'origin.txt'
        origin_text=''
        if origin.exists():
            try: origin_text=origin.read_text(encoding='utf-16').strip()
            except Exception:
                try: origin_text=origin.read_text(encoding='utf-8',errors='ignore').strip()
                except Exception: pass
        out.append({'path':str(d),'origin':origin_text})
    return out


def enrich(terminals):
    dds=data_dirs()
    for t in terminals:
        install=str(Path(t['terminal']).parent).lower().replace('/','\\')
        match=None
        for d in dds:
            o=d['origin'].lower().replace('/','\\')
            if o and (install in o or o in install): match=d['path']; break
        t['data_dir']=match
    return terminals


def detect():
    terms=enrich(candidates())
    emit({'ok':True,'terminals':terms})
    return terms


def memory_stats(db_s:str):
    db=Path(db_s)
    if not db.exists():
        emit({'ok':True,'corrections':0,'verified':0,'families':0,'latest':[]}); return
    conn=sqlite3.connect(db)
    one=lambda sql: conn.execute(sql).fetchone()[0]
    latest=[]
    try:
        for row in conn.execute("SELECT corrected_primary, original_primary, created_at FROM classification_memory ORDER BY id DESC LIMIT 8"):
            latest.append({'corrected_primary':row[0],'original_primary':row[1],'created_at':row[2]})
        result={'ok':True,'corrections':one('SELECT COUNT(*) FROM classification_memory'),'verified':one('SELECT COUNT(*) FROM indicators WHERE human_verified=1'),'families':one("SELECT COUNT(DISTINCT family_fingerprint) FROM classification_memory WHERE family_fingerprint IS NOT NULL AND family_fingerprint != ''"),'latest':latest}
    finally:
        conn.close()
    emit(result)


def safe_mql_string(s:str)->str:
    return s.replace('\\','\\\\').replace('"','\\"')


def wait_for(path:Path,timeout=20):
    end=time.time()+timeout
    while time.time()<end:
        if path.exists() and path.stat().st_size>0: return True
        time.sleep(.25)
    return False


def compile_mql(editor:Path,source:Path,include_dir:Path|None=None):
    cmd=[str(editor),f'/compile:{source}','/log']
    if include_dir: cmd.append(f'/include:{include_dir}')
    cp=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=45,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    log=source.with_suffix('.log')
    log_text=''
    if log.exists():
        try: log_text=log.read_text(encoding='utf-16',errors='ignore')
        except Exception:
            try: log_text=log.read_text(encoding='utf-8',errors='ignore')
            except Exception: pass
    return cp.returncode,log_text


def render_mt5(source:Path,out_dir:Path,terminal_info:dict):
    terminal=Path(terminal_info['terminal']); editor=Path(terminal_info.get('editor') or '')
    data=Path(terminal_info.get('data_dir') or '')
    if not editor.exists(): raise RuntimeError('MetaEditor 5 was not found beside the selected terminal.')
    if not data.exists(): raise RuntimeError('MetaTrader 5 data directory could not be mapped. Open MT5 once, then retry.')
    mql5=data/'MQL5'; indicators=mql5/'Indicators'/'MQLLibraryPreview'; scripts=mql5/'Scripts'
    indicators.mkdir(parents=True,exist_ok=True); scripts.mkdir(parents=True,exist_ok=True); out_dir.mkdir(parents=True,exist_ok=True)
    compiled=source.with_suffix('.ex5')
    if not compiled.exists():
        rc,log=compile_mql(editor,source,mql5)
        if not compiled.exists():
            raise RuntimeError('MetaEditor could not compile this MQ5 indicator. '+(log[-900:] if log else f'Compiler exit code {rc}.'))
    dest_ex5=indicators/compiled.name
    shutil.copy2(compiled,dest_ex5)
    indicator_rel='MQLLibraryPreview\\'+compiled.stem
    separate=False
    try:
        text=source.read_text(encoding='utf-8',errors='ignore')
        separate='indicator_separate_window' in text.lower()
    except Exception: pass
    shot_name='MQLLibraryPreview.png'
    renderer=scripts/'MQLLibraryPreviewRenderer.mq5'
    renderer.write_text(f'''#property script_show_inputs\nvoid OnStart(){{\n string name="{safe_mql_string(indicator_rel)}";\n int h=iCustom(_Symbol,_Period,name);\n if(h==INVALID_HANDLE){{Print("MQLLIB_PREVIEW: iCustom failed ",GetLastError()); TerminalClose(21); return;}}\n int win={1 if separate else 0};\n if(win==1) win=(int)ChartGetInteger(0,CHART_WINDOWS_TOTAL);\n if(!ChartIndicatorAdd(0,win,h)){{Print("MQLLIB_PREVIEW: ChartIndicatorAdd failed ",GetLastError()); IndicatorRelease(h); TerminalClose(22); return;}}\n ChartRedraw(); Sleep(3000);\n bool ok=ChartScreenShot(0,"{shot_name}",1200,720,ALIGN_RIGHT);\n Print("MQLLIB_PREVIEW: screenshot=",ok," err=",GetLastError());\n Sleep(500); IndicatorRelease(h); TerminalClose(ok?0:23);\n}}\n''',encoding='utf-8')
    rc,log=compile_mql(editor,renderer,mql5)
    renderer_ex5=renderer.with_suffix('.ex5')
    if not renderer_ex5.exists(): raise RuntimeError('Could not compile the MT5 preview renderer. '+(log[-900:] if log else f'Exit code {rc}.'))
    screenshot=mql5/'Files'/shot_name
    try: screenshot.unlink(missing_ok=True)
    except Exception: pass
    config=out_dir/'mt5-preview.ini'
    config.write_text('[StartUp]\nSymbol=EURUSD\nPeriod=H1\nScript=MQLLibraryPreviewRenderer\n',encoding='utf-8')
    subprocess.Popen([str(terminal),f'/config:{config}'],cwd=str(terminal.parent),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if not wait_for(screenshot,30): raise RuntimeError('MT5 opened but no preview screenshot was produced within 30 seconds. Check the MT5 Journal and whether EURUSD/history is available.')
    final=out_dir/(source.stem+'_preview.png')
    shutil.copy2(screenshot,final)
    return final


def render(source_s:str,out_s:str,terminal_s:str|None=None):
    source=Path(source_s); out=Path(out_s)
    if not source.exists(): raise RuntimeError(f'Source file does not exist: {source}')
    terms=enrich(candidates())
    if source.suffix.lower() not in ('.mq5','.ex5'):
        mt4=[t for t in terms if t['kind']=='MT4']
        raise RuntimeError('Automated real-chart rendering currently supports MQ5/EX5 through MT5. MT4 was detected.' if mt4 else 'This is an MQ4/EX4 indicator and no supported MT5 source is available for automated rendering yet.')
    eligible=[t for t in terms if t['kind']=='MT5' and t.get('editor')]
    if terminal_s:
        eligible=[t for t in eligible if t['terminal'].lower()==terminal_s.lower()] or eligible
    if not eligible: raise RuntimeError('MetaTrader 5 + MetaEditor 5 were not detected. Install/open MT5 first.')
    result=render_mt5(source,out,eligible[0])
    emit({'ok':True,'image':str(result),'terminal':eligible[0]})


def open_source(source_s:str):
    source=Path(source_s); terms=enrich(candidates()); kind='MT5' if source.suffix.lower() in ('.mq5','.ex5') else 'MT4'
    matches=[t for t in terms if t['kind']==kind]
    if not matches: raise RuntimeError(f'{kind} was not detected.')
    editor=matches[0].get('editor')
    if editor and source.suffix.lower() in ('.mq4','.mq5'):
        subprocess.Popen([editor,str(source)])
    else:
        subprocess.Popen([matches[0]['terminal']])
    emit({'ok':True,'opened':editor or matches[0]['terminal'],'kind':kind})


def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest='cmd',required=True)
    sub.add_parser('detect')
    p=sub.add_parser('render'); p.add_argument('--source',required=True); p.add_argument('--out',required=True); p.add_argument('--terminal')
    p=sub.add_parser('open-source'); p.add_argument('--source',required=True)
    p=sub.add_parser('memory-stats'); p.add_argument('--db',required=True)
    a=ap.parse_args()
    if a.cmd=='detect': detect()
    elif a.cmd=='render': render(a.source,a.out,a.terminal)
    elif a.cmd=='open-source': open_source(a.source)
    elif a.cmd=='memory-stats': memory_stats(a.db)

if __name__=='__main__':
    try: main()
    except Exception as exc:
        emit({'ok':False,'error':f'{type(exc).__name__}: {exc}'}); sys.exit(1)
