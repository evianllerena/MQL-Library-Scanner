from __future__ import annotations
import argparse, ctypes, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, time, zipfile
from pathlib import Path

CREATE_NO_WINDOW=getattr(subprocess,'CREATE_NO_WINDOW',0)
SW_HIDE=0

def emit(o):
    b=(json.dumps(o,ensure_ascii=False,default=str)+'\n').encode('utf-8','backslashreplace')
    sys.stdout.buffer.write(b); sys.stdout.buffer.flush()

def read_text(p):
    if not p.exists(): return ''
    for enc in ('utf-16','utf-8','cp1252','latin1'):
        try:return p.read_text(encoding=enc,errors='ignore')
        except Exception:pass
    return ''

def terminals():
    roots=[Path(os.environ[x]) for x in ('ProgramFiles','ProgramFiles(x86)','LOCALAPPDATA') if os.environ.get(x) and Path(os.environ[x]).exists()]
    seen=set(); out=[]
    for root in roots:
        dirs=[]
        for pat in ('MetaTrader*','*MetaTrader*','*MT4*','*MT5*'):
            try:dirs+=list(root.glob(pat))
            except Exception:pass
        try:dirs+=[x for x in root.iterdir() if x.is_dir()]
        except Exception:pass
        for d in dirs:
            for exe,kind,ed in [('terminal.exe','MT4','metaeditor.exe'),('terminal64.exe','MT5','metaeditor64.exe')]:
                p=d/exe
                if not p.exists() or str(p).lower() in seen:continue
                seen.add(str(p).lower()); e=d/ed
                out.append({'kind':kind,'terminal':str(p),'editor':str(e) if e.exists() else None,'install_dir':str(d)})
    base=Path(os.environ.get('APPDATA',''))/'MetaQuotes'/'Terminal'; dds=[]
    if base.exists():
        for d in base.iterdir():
            if not d.is_dir():continue
            origin=read_text(d/'origin.txt').strip().lower().replace('/','\\')
            dds.append((d,origin))
    for t in out:
        ins=str(Path(t['install_dir'])).lower().replace('/','\\'); t['data_dir']=None
        for d,o in dds:
            if o and (ins in o or o in ins):t['data_dir']=str(d);break
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
        target=src.name if rel else str(src); cmd=[str(editor),f'/compile:{target}',f'/inc:{mqlroot}','/log']; attempts.append(' '.join(cmd))
        cp=subprocess.run(cmd,cwd=str(src.parent if rel else editor.parent),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60,creationflags=CREATE_NO_WINDOW)
        end=time.time()+10
        while time.time()<end:
            if expected.exists() and expected.stat().st_size: return expected,attempts,read_text(log)
            time.sleep(.2)
    return None,attempts,read_text(log)

def existing_binary(src,ext):
    if src.suffix.lower()==ext and src.exists():return src
    p=src.with_suffix(ext)
    if p.exists() and p.stat().st_size:return p
    return None

def clone_runtime(t,out,kind):
    install=Path(t['install_dir']); token=hashlib.sha1(str(install).lower().encode()).hexdigest()[:10]; rt=out.parent/'preview-runtime'/f'{kind.lower()}-{token}'
    exe=install/('terminal64.exe' if kind=='MT5' else 'terminal.exe'); stamp=f'{exe.stat().st_size}:{exe.stat().st_mtime_ns}'; marker=rt/'.stamp'
    if not rt.exists() or not marker.exists() or marker.read_text(errors='ignore')!=stamp:
        shutil.rmtree(rt,ignore_errors=True)
        def ign(_d,n):return {x for x in n if x.lower() in {'logs','bases','history','mql4','mql5','profiles','templates','tester'}}
        shutil.copytree(install,rt,ignore=ign); marker.write_text(stamp)
    return rt

def stage(src,mql,kind,editor):
    ext='.ex5' if kind=='MT5' else '.ex4'; job=hashlib.sha1(str(src.resolve()).lower().encode()).hexdigest()[:14]; d=mql/'Indicators'/'MQLLibraryPreview'/job;d.mkdir(parents=True,exist_ok=True)
    staged=d/safe_name(src); shutil.copy2(src,staged); binary=d/(staged.stem+ext); old=existing_binary(src,ext)
    if old:shutil.copy2(old,binary);return job,staged,binary,{'used_existing_binary':True,'existing_binary':str(old)}
    if src.suffix.lower() not in ('.mq4','.mq5'):raise RuntimeError(f'{kind} executable could not be staged.')
    built,cmds,log=compile_file(editor,staged,mql)
    if not built:
        if log:raise RuntimeError(f'{kind} source did not compile and no existing {ext.upper()[1:]} was found.\n{log[-2200:]}')
        raise RuntimeError(f'{kind} MetaEditor produced no executable. Staged={staged}; commands={cmds}')
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

def wait_image(proc,paths,timeout=50):
    end=time.time()+timeout
    while time.time()<end:
        hide_pid(proc.pid)
        for p in paths:
            if p.exists() and p.stat().st_size:return p
        time.sleep(.25)
    return None

def latest_logs(root):
    rows=[]
    if root.exists():
        for p in root.rglob('*.log'):
            try:rows.append((p.stat().st_mtime_ns,p))
            except Exception:pass
    return '\n\n'.join(f'[{p}]\n{read_text(p)[-1600:]}' for _,p in sorted(rows,reverse=True)[:3])

def render_mt4(src,out,t):
    rt=clone_runtime(t,out,'MT4');mql=rt/'MQL4';scripts=mql/'Scripts';templates=rt/'templates';files=mql/'Files';[d.mkdir(parents=True,exist_ok=True) for d in (scripts,templates,files)]
    editor=rt/'metaeditor.exe';terminal=rt/'terminal.exe';sym=copy_mt4_history(Path(t['data_dir']),rt);job,staged,binary,meta=stage(src,mql,'MT4',editor);rel=f'MQLLibraryPreview\\{job}\\{binary.stem}'
    tplname=f'MQLLibraryPreview_{job}.tpl';tpl=templates/tplname;sep='indicator_separate_window' in read_text(src).lower();win='1' if sep else '0';tpl.write_text(f'<chart>\nsymbol={sym}\nperiod=60\ngraph=1\ngrid=1\n<window>\nheight=420\n<indicator>\nname=main\n</indicator>\n<indicator>\nname=Custom Indicator\n<expert>\nname={rel}\nflags=339\nwindow_num={win}\n</expert>\nshow_data=1\n</indicator>\n</window>\n</chart>\n')
    shot=f'MQLLibraryPreview_{job}.gif';cap=scripts/f'MQLLibraryPreviewCapture_{job}.mq4';cap.write_text(f'#property strict\nvoid OnStart(){{Sleep(3000);WindowRedraw();bool ok=WindowScreenShot("{shot}",1200,720);Print("MQLLIB_PREVIEW screenshot=",ok," err=",GetLastError());Sleep(300);TerminalClose(ok?0:23);return;}}\n')
    built,_,log=compile_file(editor,cap,mql)
    if not built:raise RuntimeError('Could not compile MT4 capture script. '+log[-1600:])
    targets=[files/shot,rt/shot,mql/shot];[p.unlink(missing_ok=True) for p in targets]
    cfg=rt/'config'/'mql-preview.ini';cfg.parent.mkdir(parents=True,exist_ok=True);cfg.write_text(f'Symbol={sym}\nPeriod=H1\nTemplate={tplname}\nScript={cap.stem}\n')
    # MT4 uses the config filename directly; /config: is MT5-only syntax.
    proc=subprocess.Popen([str(terminal),'/portable',str(cfg)],cwd=str(rt),creationflags=CREATE_NO_WINDOW,startupinfo=startupinfo());img=wait_image(proc,targets)
    if not img:
        logs=(latest_logs(rt/'logs')+'\n'+latest_logs(mql/'Logs'))[-3000:]
        try:proc.terminate()
        except Exception:pass
        raise RuntimeError(f'MT4 isolated preview produced no screenshot. runtime={rt}; config={cfg}; symbol={sym}. Logs:\n{logs}')
    final=out/(hashlib.sha1(('MT4|'+str(src.resolve()).lower()).encode()).hexdigest()[:16]+'.gif');out.mkdir(parents=True,exist_ok=True);shutil.copy2(img,final);meta.update({'isolated_runtime':str(rt),'symbol':sym,'staged':str(staged),'binary':str(binary)});return final,meta

def render_mt5(src,out,t):
    rt=clone_runtime(t,out,'MT5');mql=rt/'MQL5';scripts=mql/'Scripts';scripts.mkdir(parents=True,exist_ok=True);editor=rt/'metaeditor64.exe';terminal=rt/'terminal64.exe';sym=copy_mt5_history(Path(t['data_dir']),rt);job,staged,binary,meta=stage(src,mql,'MT5',editor);rel=f'MQLLibraryPreview\\{job}\\{binary.stem}';shot=f'MQLLibraryPreview_{job}.png';cap=scripts/f'MQLLibraryPreviewCapture_{job}.mq5';sep='indicator_separate_window' in read_text(src).lower();win='1' if sep else '0'
    cap.write_text(f'void OnStart(){{int h=iCustom(_Symbol,_Period,"{rel}");if(h==INVALID_HANDLE){{TerminalClose(21);return;}}int w={win};if(w==1)w=(int)ChartGetInteger(0,CHART_WINDOWS_TOTAL);if(!ChartIndicatorAdd(0,w,h)){{TerminalClose(22);return;}}ChartRedraw();Sleep(3000);bool ok=ChartScreenShot(0,"{shot}",1200,720,ALIGN_RIGHT);Sleep(300);TerminalClose(ok?0:23);return;}}\n')
    built,_,log=compile_file(editor,cap,mql)
    if not built:raise RuntimeError('Could not compile MT5 capture script. '+log[-1600:])
    targets=[mql/'Files'/shot,rt/shot];[p.unlink(missing_ok=True) for p in targets]
    cfg=rt/'mql-preview.ini';cfg.write_text(f'[Experts]\nEnabled=1\nAllowLiveTrading=0\nAllowDllImport=0\n\n[StartUp]\nSymbol={sym}\nPeriod=H1\nScript={cap.stem}\nShutdownTerminal=1\n')
    proc=subprocess.Popen([str(terminal),'/portable',f'/config:{cfg}'],cwd=str(rt),creationflags=CREATE_NO_WINDOW,startupinfo=startupinfo());img=wait_image(proc,targets)
    if not img:
        logs=(latest_logs(rt/'logs')+'\n'+latest_logs(mql/'Logs'))[-3000:]
        try:proc.terminate()
        except Exception:pass
        raise RuntimeError(f'MT5 isolated preview produced no screenshot. runtime={rt}; symbol={sym}. Logs:\n{logs}')
    final=out/(hashlib.sha1(('MT5|'+str(src.resolve()).lower()).encode()).hexdigest()[:16]+'.png');out.mkdir(parents=True,exist_ok=True);shutil.copy2(img,final);meta.update({'isolated_runtime':str(rt),'symbol':sym,'staged':str(staged),'binary':str(binary)});return final,meta

def render(source,out,terminal=None):
    src=Path(source);dest=Path(out);kind='MT5' if src.suffix.lower() in ('.mq5','.ex5') else 'MT4';ts=[x for x in terminals() if x['kind']==kind and x.get('editor') and x.get('data_dir')]
    if terminal:ts=[x for x in ts if x['terminal'].lower()==terminal.lower()] or ts
    if not ts:raise RuntimeError(f'{kind} + MetaEditor data directory were not detected.')
    image,meta=(render_mt5(src,dest,ts[0]) if kind=='MT5' else render_mt4(src,dest,ts[0]));emit({'ok':True,'image':str(image),'kind':kind,'cached':False,'terminal':ts[0],'meta':meta})

def open_source(source):
    src=Path(source);kind='MT5' if src.suffix.lower() in ('.mq5','.ex5') else 'MT4';ts=[x for x in terminals() if x['kind']==kind]
    if not ts:raise RuntimeError(f'{kind} was not detected.')
    target=ts[0].get('editor') if src.suffix.lower() in ('.mq4','.mq5') else ts[0]['terminal'];subprocess.Popen([target,str(src)] if target.endswith('editor.exe') or target.endswith('editor64.exe') else [target]);emit({'ok':True,'opened':target,'kind':kind})

def main():
    a=argparse.ArgumentParser();s=a.add_subparsers(dest='cmd',required=True);s.add_parser('detect');p=s.add_parser('render');p.add_argument('--source',required=True);p.add_argument('--out',required=True);p.add_argument('--terminal');p=s.add_parser('open-source');p.add_argument('--source',required=True);p=s.add_parser('memory-stats');p.add_argument('--db',required=True);p=s.add_parser('remove-source');p.add_argument('--db',required=True);p.add_argument('--source',required=True);p=s.add_parser('archive-reset');p.add_argument('--db',required=True);x=a.parse_args()
    if x.cmd=='detect':detect()
    elif x.cmd=='render':render(x.source,x.out,x.terminal)
    elif x.cmd=='open-source':open_source(x.source)
    elif x.cmd=='memory-stats':memory_stats(x.db)
    elif x.cmd=='remove-source':remove_source(x.db,x.source)
    elif x.cmd=='archive-reset':archive_reset(x.db)

if __name__=='__main__':
    try:main()
    except Exception as e:emit({'ok':False,'error':f'{type(e).__name__}: {e}'});sys.exit(1)
