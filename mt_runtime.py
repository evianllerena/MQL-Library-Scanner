"""Private MetaTrader runtimes: cloning, seeding, history copy, priming. Split out of preview_bridge.py."""
from __future__ import annotations
import argparse, concurrent.futures, ctypes, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, time, zipfile
from pathlib import Path
from mql_text import decode_mql_bytes, read_log_text, read_mql_text
from mt_common import (CREATE_NO_WINDOW, emit_heartbeat, emit_stage, hard_kill, hide_pid, latest_logs, mql_dir_name, safe_job_id, startupinfo)  # noqa: F401
from mt_compile import (compile_file, copy_compiled_support, existing_binary, find_live_binary, safe_name)  # noqa: F401
from mt_discovery import (resolve_mt5_render_data_dir)  # noqa: F401


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
