"""Rendering: resident render-server EA, compile pool, one-shot preflight/render. Split out of preview_bridge.py."""
from __future__ import annotations
import argparse, concurrent.futures, ctypes, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, time, zipfile
from pathlib import Path
from mql_text import decode_mql_bytes, read_log_text, read_mql_text
from mt_common import (CREATE_NO_WINDOW, PreviewRuntimeError, RENDER_STARTUP_EXIT, RenderLock, RenderStartupError, _same_source, emit, emit_heartbeat, emit_stage, hard_kill, hide_pid, latest_logs, mql_dir_name, read_text, safe_job_id, startupinfo, terminate_tree)  # noqa: F401
from mt_compile import (compile_file)  # noqa: F401
from mt_discovery import (choose_runtime, discovery_diagnostics, load_runtime_cache, preview_runtime, resolve_mt5_render_data_dir, source_kind, write_runtime_cache)  # noqa: F401
from mt_runtime import (_prepare_render_runtime, clone_runtime, copy_mt4_history, copy_mt5_history, prime_mt5_runtime, runtime_path, seed_render_dependencies_from_data_dir, seed_render_runtime_from_data_dir, stage)  # noqa: F401
from preview_db import (_compiled_cache_path, _ensure_preview_queue_schema, _make_thumb, _propagate_existing_ready, _store_preview_image, _worker_pause_reason)  # noqa: F401


def wait_image(proc,paths,timeout=50):
    end=time.time()+timeout
    while time.time()<end:
        hide_pid(proc.pid)
        for p in paths:
            if p.exists() and p.stat().st_size:return p
        if proc.poll() is not None:return None
        time.sleep(.25)
    return None

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
    changed=not _same_source(src,RESIDENT_EA_SOURCE)
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
        # AllowLiveTrading=0: the clone is seeded with the user's real accounts.dat/servers.dat
        # (seed_render_runtime_from_data_dir) and loads arbitrary third-party indicators, so it
        # must never be able to trade. The resident EA places no orders; Enabled=1 (the Algo
        # Trading button) is what lets it run, and stays on.
        '[Experts]\nEnabled=1\nAllowLiveTrading=0\nAllowDllImport=0\n\n'
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


def render_mt4(src,out,t,job_id,compiled_cache=None,worker_id=None):
    # worker_id gives each library worker its own MT4 runtime dir (runtime_path suffix), so
    # parallel workers never share one terminal/profile/Files folder.
    emit_stage(job_id,'runtime_clone','Preparing isolated MT4 runtime.')
    rt=clone_runtime(t,out,'MT4',worker_id);mql=rt/'MQL4';scripts=mql/'Scripts';templates=rt/'templates';files=mql/'Files';[d.mkdir(parents=True,exist_ok=True) for d in (scripts,templates,files)]
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
