"""
Batch preview engine for the MQL Indicator Library.

Goal: turn per-click, one-at-a-time previewing (14-35s each, serialized) into a
library-wide background job that scales to 20k+ files. The four levers, in order
of impact:

  1. Dedup + incremental cache  -- never render the same file content twice.
     Previews are keyed by the file's sha256 (already computed by the scanner),
     so exact duplicates share one render and a rescan only touches new/changed
     files. This is what turns a 110-hour job into a one-time cost then near-free.

  2. One long-lived MetaTrader session renders many indicators.
     Instead of launch->screenshot->kill per file, a single terminal runs a
     controller script that loops the whole shard. Cold-start is amortized across
     thousands of items, dropping per-item cost from ~20s to a few seconds.

  3. Parallel shards -- N isolated portable runtimes process the queue at once.

  4. Bulk compile -- MetaEditor compiles a whole folder in one invocation.

This module reuses discovery/runtime/compile helpers from preview_bridge.py so
the single-preview path and the batch path stay consistent.

Commands (JSONL on stdout, like the other sidecars):
  plan  --db <db> --out <dir> [--limit N] [--force]
  run   --db <db> --out <dir> [--shards N] [--limit N] [--force] [--platform MT4|MT5]

Testable without MetaTrader: plan, sharding, cache decisions, DB writeback.
The render_* functions drive MetaTrader and must be validated on Windows.
"""
from __future__ import annotations
import argparse, json, os, shutil, sqlite3, sys, time, hashlib, threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import preview_bridge as pb  # discovery, clone_runtime, compile_file, stage, mql_dir_name, ...

# ---- output -----------------------------------------------------------------

_emit_lock = threading.Lock()

def emit(o):
    b = (json.dumps(o, ensure_ascii=False, default=str) + '\n').encode('utf-8', 'backslashreplace')
    with _emit_lock:
        sys.stdout.buffer.write(b)
        sys.stdout.buffer.flush()

def stage_event(name, **kw):
    emit({'type': 'stage', 'stage': name, **kw})

# ---- DB ---------------------------------------------------------------------

PREVIEW_COLUMNS = {
    'preview_status': "TEXT DEFAULT 'pending'",
    'preview_path': 'TEXT',
    'preview_hash': 'TEXT',
    'preview_error': "TEXT DEFAULT ''",
    'preview_updated_at': 'TEXT',
}

def connect(db):
    conn = sqlite3.connect(db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA busy_timeout=30000')
    return conn

def ensure_preview_columns(conn):
    """Idempotent migration so the batch engine works even against a DB written
    by an older scanner build that predates the preview columns."""
    existing = {r[1] for r in conn.execute('PRAGMA table_info(indicators)')}
    for name, decl in PREVIEW_COLUMNS.items():
        if name not in existing:
            conn.execute(f'ALTER TABLE indicators ADD COLUMN {name} {decl}')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_preview_status ON indicators(preview_status)')
    conn.commit()

# ---- cache / dedup ----------------------------------------------------------

def kind_of(platform):
    return 'MT5' if str(platform).upper() in ('MQL5', 'MT5') else 'MT4'

def preview_ext(kind):
    # ChartScreenShot picks format from the extension. MT5 -> png, MT4 -> gif
    # (gif is the proven WindowScreenShot/legacy format on MT4).
    return '.png' if kind == 'MT5' else '.gif'

def preview_file(out, sha, kind):
    return Path(out) / 'previews' / f'{sha}{preview_ext(kind)}'

def plan_jobs(db, out, force=False, limit=None):
    """Decide what actually needs rendering.

    - One job per UNIQUE sha256 (canonical rows: duplicate_of IS NULL). Duplicates
      inherit the canonical render via propagate_duplicates().
    - Skip anything already rendered for the same hash whose image exists on disk.
    - Skip known-bad (incompatible/failed) for the same hash unless force -- we do
      not relaunch MetaTrader for files we already know won't produce an image.
    """
    conn = connect(db)
    ensure_preview_columns(conn)
    rows = conn.execute(
        "SELECT id, path, filename, platform, sha256, "
        "COALESCE(preview_status,'pending') AS preview_status, preview_hash "
        "FROM indicators "
        "WHERE sha256 IS NOT NULL AND sha256 != '' AND duplicate_of IS NULL "
        "ORDER BY user_favorite DESC, analyzed_at DESC"
    ).fetchall()
    conn.close()

    jobs = {'MT5': [], 'MT4': []}
    cached = skipped_bad = 0
    seen = set()
    for r in rows:
        sha = r['sha256']
        if sha in seen:
            continue
        seen.add(sha)
        kind = kind_of(r['platform'])
        img = preview_file(out, sha, kind)
        same_hash = (r['preview_hash'] == sha)
        if not force:
            if r['preview_status'] == 'ok' and same_hash and img.exists() and img.stat().st_size > 0:
                cached += 1
                continue
            if r['preview_status'] in ('incompatible', 'failed', 'blank') and same_hash:
                skipped_bad += 1
                continue
        jobs[kind].append({'id': r['id'], 'path': r['path'], 'stem': Path(r['path']).stem,
                           'sha': sha, 'kind': kind})

    if limit:
        # Trim fairly across platforms, favorites/recent first (already ordered).
        keep = int(limit)
        trimmed = {'MT5': [], 'MT4': []}
        i = 0
        pools = [('MT5', jobs['MT5']), ('MT4', jobs['MT4'])]
        idxs = {'MT5': 0, 'MT4': 0}
        while sum(len(v) for v in trimmed.values()) < keep and any(idxs[k] < len(v) for k, v in pools):
            k, v = pools[i % 2]; i += 1
            if idxs[k] < len(v):
                trimmed[k].append(v[idxs[k]]); idxs[k] += 1
        jobs = trimmed

    return {'jobs': jobs, 'cached': cached, 'skipped_known_bad': skipped_bad,
            'todo': len(jobs['MT5']) + len(jobs['MT4']),
            'todo_mt5': len(jobs['MT5']), 'todo_mt4': len(jobs['MT4'])}

def chunk(items, n):
    """Split a list into n roughly-equal shards (round-robin keeps them balanced
    even when later items are cheaper/costlier)."""
    n = max(1, int(n))
    out = [[] for _ in range(n)]
    for i, it in enumerate(items):
        out[i % n].append(it)
    return [s for s in out if s]

# ---- DB writeback -----------------------------------------------------------

def writeback(db, results):
    """Persist a batch of results and propagate each to duplicate rows sharing the
    same sha256. results: list of {sha, status, path|None, error}."""
    if not results:
        return
    conn = connect(db)
    ensure_preview_columns(conn)
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    try:
        for r in results:
            conn.execute(
                "UPDATE indicators SET preview_status=?, preview_path=?, preview_hash=?, "
                "preview_error=?, preview_updated_at=? WHERE sha256=?",
                (r['status'], r.get('path'), r['sha'], (r.get('error') or '')[:1000], now, r['sha'])
            )
        conn.commit()
    finally:
        conn.close()

# ---- MQL batch controllers (embedded; written into each runtime at render) --
# One controller per session loops the whole shard, so MetaTrader starts once.
# Both read Files\mqllib_batch.json and append Files\mqllib_batch_results.jsonl,
# then drop Files\mqllib_batch_done.flag.

MT5_BATCH_SRC = r'''
#property strict
// Batch preview controller (MT5). Loads each pre-compiled indicator onto the
// chart, screenshots it, removes it, then moves on -- one terminal for the whole
// shard. Reads Files\mqllib_batch.json, writes Files\mqllib_batch_results.jsonl.
string ReadFileText(string name){
  int h=FileOpen(name,FILE_READ|FILE_TXT|FILE_ANSI); if(h==INVALID_HANDLE) return "";
  string s=""; while(!FileIsEnding(h)) s+=FileReadString(h); FileClose(h); return s;
}
void AppendResult(string line){
  int h=FileOpen("mqllib_batch_results.jsonl",FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI);
  if(h==INVALID_HANDLE) return; FileSeek(h,0,SEEK_END); FileWriteString(h,line+"\n"); FileClose(h);
}
// tiny helpers to pull values out of one flat JSON object {"rel":"..","sub":0,"shot":"..","key":".."}
string JStr(string obj,string key){
  int k=StringFind(obj,"\""+key+"\""); if(k<0) return "";
  int c=StringFind(obj,":",k); int q1=StringFind(obj,"\"",c); if(q1<0) return "";
  int q2=StringFind(obj,"\"",q1+1); if(q2<0) return ""; return StringSubstr(obj,q1+1,q2-q1-1);
}
int JInt(string obj,string key){
  int k=StringFind(obj,"\""+key+"\""); if(k<0) return 0; int c=StringFind(obj,":",k);
  string num=""; for(int i=c+1;i<StringLen(obj);i++){ ushort ch=StringGetCharacter(obj,i);
    if(ch>='0'&&ch<='9'){num+=CharToString((uchar)ch);} else if(StringLen(num)>0) break; }
  return (int)StringToInteger(num);
}
void OnStart(){
  string body=ReadFileText("mqllib_batch.json");
  if(StringLen(body)==0){ AppendResult("{\"fatal\":\"no job file\"}"); return; }
  // split objects on "},{" boundaries
  int start=StringFind(body,"["); int end=StringFind(body,"]");
  if(start<0||end<0) return; string arr=StringSubstr(body,start+1,end-start-1);
  int pos=0;
  while(true){
    int ob=StringFind(arr,"{",pos); if(ob<0) break; int cb=StringFind(arr,"}",ob); if(cb<0) break;
    string obj=StringSubstr(arr,ob,cb-ob+1); pos=cb+1;
    string rel=JStr(obj,"rel"); string shot=JStr(obj,"shot"); string key=JStr(obj,"key"); int sub=JInt(obj,"sub");
    if(StringLen(rel)==0){ continue; }
    ResetLastError();
    int handle=iCustom(_Symbol,_Period,rel);
    if(handle==INVALID_HANDLE){ AppendResult("{\"key\":\""+key+"\",\"status\":\"no_handle\",\"err\":"+IntegerToString(GetLastError())+"}"); continue; }
    int win=sub; if(sub==1) win=(int)ChartGetInteger(0,CHART_WINDOWS_TOTAL);
    ResetLastError();
    bool added=ChartIndicatorAdd(0,win,handle);
    if(!added){ IndicatorRelease(handle); AppendResult("{\"key\":\""+key+"\",\"status\":\"no_add\",\"err\":"+IntegerToString(GetLastError())+"}"); continue; }
    ChartRedraw();
    for(int i=0;i<40;i++){ if(BarsCalculated(handle)>0) break; Sleep(100); }
    Sleep(600);
    ResetLastError();
    bool ok=ChartScreenShot(0,shot,1200,720,ALIGN_RIGHT);
    int err=GetLastError();
    // remove just-added indicator from its subwindow (last one added)
    int total=(int)ChartIndicatorsTotal(0,win);
    if(total>0){ string nm=ChartIndicatorName(0,win,total-1); if(nm!="") ChartIndicatorDelete(0,win,nm); }
    IndicatorRelease(handle);
    AppendResult("{\"key\":\""+key+"\",\"status\":\""+(ok?"ok":"no_shot")+"\",\"err\":"+IntegerToString(err)+"}");
    Sleep(120);
  }
  int f=FileOpen("mqllib_batch_done.flag",FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI); if(f!=INVALID_HANDLE){ FileWriteString(f,"done"); FileClose(f); }
  TerminalClose(0);
}
'''

MT4_BATCH_SRC = r'''
#property strict
// Batch preview controller (MT4). MQL4 has no ChartIndicatorAdd, so for each
// indicator we open a CHILD chart, apply that indicator's template to it, shoot
// it, and close it. Applying a template unloads the program on THAT chart, so we
// only ever template child charts -- never the controller's own chart, which
// keeps this script alive for the whole shard. One terminal for the whole shard.
string ReadFileText(string name){
  int h=FileOpen(name,FILE_READ|FILE_TXT|FILE_ANSI); if(h==INVALID_HANDLE) return "";
  string s=""; while(!FileIsEnding(h)) s+=FileReadString(h); FileClose(h); return s;
}
void AppendResult(string line){
  int h=FileOpen("mqllib_batch_results.jsonl",FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI);
  if(h==INVALID_HANDLE) return; FileSeek(h,0,SEEK_END); FileWriteString(h,line+"\n"); FileClose(h);
}
string JStr(string obj,string key){
  int k=StringFind(obj,"\""+key+"\""); if(k<0) return "";
  int c=StringFind(obj,":",k); int q1=StringFind(obj,"\"",c); if(q1<0) return "";
  int q2=StringFind(obj,"\"",q1+1); if(q2<0) return ""; return StringSubstr(obj,q1+1,q2-q1-1);
}
void OnStart(){
  string body=ReadFileText("mqllib_batch.json");
  if(StringLen(body)==0){ AppendResult("{\"fatal\":\"no job file\"}"); return; }
  int start=StringFind(body,"["); int end=StringFind(body,"]");
  if(start<0||end<0) return; string arr=StringSubstr(body,start+1,end-start-1);
  int pos=0;
  while(true){
    int ob=StringFind(arr,"{",pos); if(ob<0) break; int cb=StringFind(arr,"}",ob); if(cb<0) break;
    string obj=StringSubstr(arr,ob,cb-ob+1); pos=cb+1;
    string tpl=JStr(obj,"tpl"); string shot=JStr(obj,"shot"); string key=JStr(obj,"key"); string sym=JStr(obj,"sym");
    if(StringLen(sym)==0) sym=_Symbol;
    if(StringLen(tpl)==0){ continue; }
    long cid=ChartOpen(sym,PERIOD_H1);
    if(cid==0){ AppendResult("{\"key\":\""+key+"\",\"status\":\"no_chart\",\"err\":"+IntegerToString(GetLastError())+"}"); continue; }
    Sleep(400);
    ResetLastError();
    bool applied=ChartApplyTemplate(cid,tpl);   // applies to CHILD chart -> controller survives
    if(!applied){ ChartClose(cid); AppendResult("{\"key\":\""+key+"\",\"status\":\"no_template\",\"err\":"+IntegerToString(GetLastError())+"}"); continue; }
    // template application is queued+async; give it time to load the indicator & paint
    for(int i=0;i<25;i++){ ChartRedraw(cid); Sleep(160); }
    ResetLastError();
    bool ok=ChartScreenShot(cid,shot,1200,720,ALIGN_RIGHT);
    int err=GetLastError();
    ChartClose(cid);
    AppendResult("{\"key\":\""+key+"\",\"status\":\""+(ok?"ok":"no_shot")+"\",\"err\":"+IntegerToString(err)+"}");
    Sleep(120);
  }
  int f=FileOpen("mqllib_batch_done.flag",FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI); if(f!=INVALID_HANDLE){ FileWriteString(f,"done"); FileClose(f); }
}
'''

MT4_TEMPLATE = (
    "<chart>\n"
    "symbol={sym}\nperiod=60\n"
    "<window>\n<indicator>\nname=Custom Indicator\n"
    "<expert>\nname={rel}\nflags=339\n</expert>\nshow_data=1\n</indicator>\n</window>\n</chart>\n"
)

# ---- render: MT5 shard ------------------------------------------------------

def _read_results(files_dir):
    res = {}
    p = files_dir / 'mqllib_batch_results.jsonl'
    if not p.exists():
        return res
    for line in pb.read_text(p).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get('key'):
            res[o['key']] = o
    return res

def _wait_batch(files_dir, proc, total, timeout):
    """Poll the results file / done flag while the batch controller runs."""
    done = files_dir / 'mqllib_batch_done.flag'
    end = time.monotonic() + timeout
    last = -1
    while time.monotonic() < end:
        pb.hide_pid(proc.pid)
        res = _read_results(files_dir)
        if len(res) != last:
            last = len(res)
            stage_event('batch_progress', done=last, total=total)
        if done.exists() or (proc.poll() is not None):
            break
        time.sleep(1.0)
    return _read_results(files_dir)

def render_shard(shard_id, kind, items, terminal, out, timeout_per_item=25):
    """Render one shard in a single isolated MetaTrader session."""
    if not items:
        return []
    out = Path(out)
    (out / 'previews').mkdir(parents=True, exist_ok=True)
    prevdir = out / 'previews'

    stage_event('shard_start', shard=shard_id, kind=kind, count=len(items))
    rt = pb.clone_runtime(terminal, out / f'shard-{shard_id}', kind)
    mqlroot = rt / pb.mql_dir_name(kind)
    indicators = mqlroot / 'Indicators' / 'MQLLibraryBatch'
    scripts = mqlroot / 'Scripts'
    files = mqlroot / 'Files'
    templates = rt / 'templates'
    for d in (indicators, scripts, files, templates):
        d.mkdir(parents=True, exist_ok=True)
    # clean any prior run's artifacts
    for fn in ('mqllib_batch.json', 'mqllib_batch_results.jsonl', 'mqllib_batch_done.flag'):
        try:
            (files / fn).unlink()
        except Exception:
            pass

    editor = rt / Path(terminal['editor']).name
    terminal_exe = rt / Path(terminal['terminal']).name
    if kind == 'MT5':
        sym = pb.copy_mt5_history(Path(terminal['data_dir']), rt)
        pb.prime_mt5_runtime(rt, terminal_exe, sym, f'batch-{shard_id}')
    else:
        sym = pb.copy_mt4_history(Path(terminal['data_dir']), rt)

    ext = '.mq5' if kind == 'MT5' else '.mq4'
    bin_ext = '.ex5' if kind == 'MT5' else '.ex4'

    # 1) stage every indicator source (or reuse an existing compiled binary)
    job_items = []
    staged_any = False
    prepared = {}   # sha -> dict(rel, shot, need_compile, staged_src)
    for it in items:
        sha = it['sha']
        src = Path(it['path'])
        if not src.is_file():
            prepared[sha] = {'status': 'failed', 'error': 'source file missing'}
            continue
        d = indicators / sha
        d.mkdir(parents=True, exist_ok=True)
        staged = d / (pb.safe_name(src))
        try:
            shutil.copy2(src, staged)
        except Exception as e:
            prepared[sha] = {'status': 'failed', 'error': f'stage: {e}'}
            continue
        # bring along sibling .mqh dependencies
        for dep in src.parent.glob('*.mqh'):
            try:
                shutil.copy2(dep, d / dep.name)
            except Exception:
                pass
        existing = pb.existing_binary(src, bin_ext)
        rel = f'MQLLibraryBatch\\{sha}\\{staged.stem}'
        if existing:
            try:
                shutil.copy2(existing, d / (staged.stem + bin_ext))
            except Exception:
                pass
        else:
            staged_any = True
        prepared[sha] = {'rel': rel, 'staged': staged, 'src_has_ext': src.suffix.lower() in ('.mq4', '.mq5')}

    # 2) bulk-compile the whole batch folder in ONE MetaEditor invocation
    if staged_any:
        stage_event('batch_compile', shard=shard_id, kind=kind)
        try:
            pb.subprocess.run(
                [str(editor), f'/compile:{indicators}', f'/inc:{mqlroot}', '/log'],
                cwd=str(rt), stdout=pb.subprocess.PIPE, stderr=pb.subprocess.PIPE,
                timeout=max(120, 6 * len(items)), creationflags=pb.CREATE_NO_WINDOW)
        except Exception as e:
            stage_event('batch_compile_warning', shard=shard_id, error=str(e))

    # 3) build the job list; anything without a compiled binary is 'incompatible'
    results = []
    for it in items:
        sha = it['sha']
        info = prepared.get(sha, {})
        if info.get('status'):   # staging failure recorded above
            results.append({'sha': sha, 'status': info['status'], 'path': None, 'error': info.get('error', '')})
            continue
        d = indicators / sha
        binary = d / (info['staged'].stem + bin_ext)
        if not (binary.exists() and binary.stat().st_size > 0):
            results.append({'sha': sha, 'status': 'incompatible', 'path': None,
                           'error': 'no compiled binary (source did not compile)'})
            continue
        shot = f'{sha}{preview_ext(kind)}'
        if kind == 'MT5':
            sep = 'indicator_separate_window' in pb.read_text(info['staged']).lower()
            job_items.append({'key': sha, 'rel': info['rel'], 'sub': 1 if sep else 0, 'shot': shot})
        else:
            tplname = f'MQLLibraryBatch_{sha}.tpl'
            (templates / tplname).write_text(MT4_TEMPLATE.format(sym=sym, rel=info['rel']), encoding='utf-8')
            job_items.append({'key': sha, 'tpl': tplname, 'sym': sym, 'shot': shot})

    if not job_items:
        stage_event('shard_done', shard=shard_id, rendered=0, note='nothing compiled')
        return results

    # 4) write job list + controller, compile controller, launch ONE session
    (files / 'mqllib_batch.json').write_text(json.dumps({'items': job_items}), encoding='utf-8')
    ctrl_src = MT5_BATCH_SRC if kind == 'MT5' else MT4_BATCH_SRC
    ctrl = scripts / (f'MQLLibraryBatchController{ext}')
    ctrl.write_text(ctrl_src, encoding='utf-8')
    built, _cmds, log = pb.compile_file(editor, ctrl, mqlroot)
    if not built:
        stage_event('shard_error', shard=shard_id, error='controller compile failed')
        for j in job_items:
            results.append({'sha': j['key'], 'status': 'failed', 'path': None, 'error': 'batch controller compile failed'})
        return results

    cfg = rt / f'mqllib-batch-{shard_id}.ini'
    if kind == 'MT5':
        cfg.write_text(f'[Experts]\nEnabled=1\nAllowLiveTrading=0\nAllowDllImport=0\n\n[StartUp]\nSymbol={sym}\nPeriod=H1\nScript={ctrl.stem}\nShutdownTerminal=0\n', encoding='utf-8')
        args = [str(terminal_exe), '/portable', f'/config:{cfg}']
    else:
        cfg.write_text(f'Symbol={sym}\nPeriod=H1\nScript={ctrl.stem}\n', encoding='utf-8')
        args = [str(terminal_exe), '/portable', str(cfg)]

    stage_event('session_launch', shard=shard_id, kind=kind, jobs=len(job_items))
    proc = pb.subprocess.Popen(args, cwd=str(rt), creationflags=pb.CREATE_NO_WINDOW, startupinfo=pb.startupinfo())
    try:
        res = _wait_batch(files, proc, len(job_items), timeout=max(90, timeout_per_item * len(job_items)))
    finally:
        pb.terminate_tree(proc)

    # 5) collect screenshots -> previews/<sha>.<ext>, map statuses
    for j in job_items:
        sha = j['key']
        r = res.get(sha, {})
        status = r.get('status')
        shot_path = files / j['shot']
        if status == 'ok' and shot_path.exists() and shot_path.stat().st_size > 0:
            dest = prevdir / j['shot']
            try:
                shutil.copy2(shot_path, dest)
                results.append({'sha': sha, 'status': 'ok', 'path': str(dest), 'error': ''})
                continue
            except Exception as e:
                results.append({'sha': sha, 'status': 'failed', 'path': None, 'error': f'copy: {e}'})
                continue
        # compiled but produced no usable image
        results.append({'sha': sha, 'status': 'blank', 'path': None,
                       'error': f"controller status={status or 'timeout'} err={r.get('err')}"})

    rendered = sum(1 for r in results if r['status'] == 'ok')
    stage_event('shard_done', shard=shard_id, rendered=rendered, total=len(job_items))
    return results

# ---- top-level commands -----------------------------------------------------

def cmd_plan(db, out, force, limit):
    plan = plan_jobs(db, out, force=force, limit=limit)
    emit({'ok': True, 'plan': True, 'todo': plan['todo'], 'todo_mt5': plan['todo_mt5'],
          'todo_mt4': plan['todo_mt4'], 'cached': plan['cached'],
          'skipped_known_bad': plan['skipped_known_bad']})

def cmd_run(db, out, shards, force, limit, platform=None):
    started = time.time()
    plan = plan_jobs(db, out, force=force, limit=limit)
    emit({'ok': True, 'run': True, 'phase': 'planned', 'todo': plan['todo'],
          'todo_mt5': plan['todo_mt5'], 'todo_mt4': plan['todo_mt4'],
          'cached': plan['cached'], 'skipped_known_bad': plan['skipped_known_bad']})

    kinds = ['MT5', 'MT4'] if not platform else [platform.upper()]
    tasks = []  # (kind, shard_index, items, terminal)
    for kind in kinds:
        items = plan['jobs'].get(kind, [])
        if not items:
            continue
        terminal = pb.choose_runtime(kind)  # raises PreviewRuntimeError if no terminal
        for i, sh in enumerate(chunk(items, shards)):
            tasks.append((kind, i, sh, terminal))

    if not tasks:
        emit({'ok': True, 'run': True, 'phase': 'done', 'rendered': 0, 'todo': 0,
              'elapsed_s': round(time.time() - started, 1)})
        return

    total_rendered = 0
    total_results = 0
    # Parallel shards. Each shard owns its own portable runtime + terminal.
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futs = {}
        for kind, i, sh, terminal in tasks:
            sid = f'{kind.lower()}-{i}'
            futs[ex.submit(render_shard, sid, kind, sh, terminal, out)] = sid
        for fut in as_completed(futs):
            sid = futs[fut]
            try:
                results = fut.result()
            except Exception as e:
                emit({'level': 'ERROR', 'event': 'shard_failed', 'shard': sid, 'error': str(e)})
                continue
            writeback(db, results)
            total_results += len(results)
            total_rendered += sum(1 for r in results if r['status'] == 'ok')
            emit({'ok': True, 'run': True, 'phase': 'shard_committed', 'shard': sid,
                  'rendered_so_far': total_rendered, 'processed': total_results})

    emit({'ok': True, 'run': True, 'phase': 'done', 'rendered': total_rendered,
          'processed': total_results, 'elapsed_s': round(time.time() - started, 1)})

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('plan'); p.add_argument('--db', required=True); p.add_argument('--out', required=True)
    p.add_argument('--limit', type=int); p.add_argument('--force', action='store_true')
    p = sub.add_parser('run'); p.add_argument('--db', required=True); p.add_argument('--out', required=True)
    p.add_argument('--shards', type=int, default=4); p.add_argument('--limit', type=int)
    p.add_argument('--force', action='store_true'); p.add_argument('--platform', choices=['MT4', 'MT5'])
    a = ap.parse_args()
    if a.cmd == 'plan':
        cmd_plan(a.db, a.out, a.force, a.limit)
    elif a.cmd == 'run':
        cmd_run(a.db, a.out, a.shards, a.force, a.limit, a.platform)

if __name__ == '__main__':
    try:
        main()
    except pb.PreviewRuntimeError as e:
        emit({'ok': False, 'error': f'{type(e).__name__}: {e}', 'diagnostics': getattr(e, 'diagnostics', [])})
        sys.exit(1)
    except Exception as e:
        emit({'ok': False, 'error': f'{type(e).__name__}: {e}'})
        sys.exit(1)
