"""
NNFX backtest batch engine for the MQL Indicator Library.

Mirrors preview_batch.py's architecture exactly (same JSONL stdout protocol,
same PRAGMA-guarded migration idiom, same `import preview_bridge as pb` reuse
of runtime discovery/clone/compile/prime helpers, same bulk-stage ->
bulk-compile -> one controller script -> one MT5 session -> JSONL
results + done-flag pattern used by render_shard/MT5_BATCH_SRC) so the two
batch engines stay consistent instead of diverging into two MT5 automation
styles.

What's different from preview_batch.py: instead of screenshotting each
indicator, the controller script extracts REAL per-bar buffer values (via
iCustom + CopyBuffer -- proven throughout the NNFX pilot work this session,
never the Strategy Tester, which was unreliable for headless automation) for
every candidate plus the fixed reference backdrop (Examples\\Custom Moving
Average / MACD / RVI) and real ATR(14), across a symbol basket, into one CSV
per symbol. Python then classifies each candidate (EXTRACT_OK /
EXTRACT_GARBAGE, never silently dropped) and runs EXTRACT_OK ones through the
verified nnfx_engine.py, writing real backtest_results rows.

Commands (JSONL on stdout, like the other sidecars):
  plan --db <db> [--limit N]
  run  --db <db> --out <dir> --bed-symbols EURUSD,GBPUSD,USDJPY [--limit N]
       [--min-trades 30] [--force]
"""
from __future__ import annotations
import argparse, json, os, shutil, sqlite3, sys, time, hashlib, threading, tempfile
from pathlib import Path

import preview_bridge as pb  # discovery, clone_runtime, compile_file, stage, mql_dir_name, ...

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nnfx_engine import NNFXEngine, NNFXParams

# ---- output (identical protocol to preview_batch.py) ------------------------

_emit_lock = threading.Lock()

def emit(o):
    b = (json.dumps(o, ensure_ascii=False, default=str) + '\n').encode('utf-8', 'backslashreplace')
    with _emit_lock:
        sys.stdout.buffer.write(b)
        sys.stdout.buffer.flush()

def stage_event(name, **kw):
    emit({'type': 'stage', 'stage': name, **kw})

# ---- DB ----------------------------------------------------------------------

def connect(db):
    conn = sqlite3.connect(db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA busy_timeout=30000')
    return conn

def ensure_backtest_table(conn):
    """Defensive, idempotent guard mirroring preview_batch.py's
    ensure_preview_columns -- the table already exists in engine.py's
    migrate() (STEP 1 of NNFX_INTEGRATION_PLAN.txt), but a batch engine
    should never assume it's talking to an up-to-date DB."""
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS backtest_results(
      id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL, slot TEXT NOT NULL, signal_type TEXT,
      bed TEXT, stage INTEGER, trades INTEGER, wins INTEGER, losses INTEGER,
      win_rate REAL, expectancy_pips REAL, profit_factor REAL, max_drawdown REAL,
      net_pips REAL, net_pips_saved REAL, bridge_skips INTEGER, continuation_trades INTEGER,
      trade_log_path TEXT, tested_at TEXT, mapping_source TEXT DEFAULT 'auto', mapping_reason TEXT,
      human_verified INTEGER DEFAULT 0, verified_slot TEXT, verified_signal_type TEXT,
      verified_at TEXT, zero_reference REAL, buf_a INTEGER, buf_b INTEGER,
      UNIQUE(sha256, slot, bed)
    );
    CREATE INDEX IF NOT EXISTS ix_bt_slot ON backtest_results(slot);
    CREATE INDEX IF NOT EXISTS ix_bt_sha ON backtest_results(sha256);
    ''')
    conn.commit()

TESTABLE_SLOTS = ('CONFIRMATION_1', 'CONFIRMATION_2', 'BASELINE', 'VOLUME')

def candidacy_rows(conn, limit=None):
    """Reads the candidacy rows engine.py's nnfx_map already wrote
    (bed='', mapping_source in ('auto','user')) -- slot/signal_type/
    zero_reference/buf_a/buf_b come from the LIVE classifier's real resolved
    buffer indices, never guessed as 0/1 here."""
    sql = f"""
        SELECT bt.sha256, bt.slot, bt.signal_type, bt.zero_reference, bt.buf_a, bt.buf_b,
               i.path, i.filename, i.platform
        FROM backtest_results bt JOIN indicators i ON i.sha256 = bt.sha256
        WHERE bt.bed = '' AND bt.slot IN {TESTABLE_SLOTS!r} AND bt.buf_a IS NOT NULL
              AND i.duplicate_of IS NULL AND i.platform = 'MQL5'
        ORDER BY i.user_favorite DESC, i.analyzed_at DESC
    """
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    if limit:
        rows = rows[:limit]
    return rows

# ---- plan ---------------------------------------------------------------------

def plan(db, limit=None):
    conn = connect(db)
    ensure_backtest_table(conn)
    rows = candidacy_rows(conn, limit)
    by_slot = {}
    for r in rows:
        by_slot[r['slot']] = by_slot.get(r['slot'], 0) + 1
    emit({'type': 'plan', 'total': len(rows), 'by_slot': by_slot,
          'items': [{'sha256': r['sha256'], 'slot': r['slot'], 'filename': r['filename']} for r in rows]})
    conn.close()

# ---- MT5 controller script (mirrors preview_batch.py's MT5_BATCH_SRC) --------

CONTROLLER_SRC = r'''
#property strict
// NNFX backtest batch controller (MT5). Reads Files\nnfx_batch.json (job list
// + symbol list), extracts real per-bar buffer values via iCustom+CopyBuffer
// for every job plus the fixed ma/macd/rvi backdrop and real ATR(14), across
// every symbol, into one CSV per symbol. Never uses the Strategy Tester.
string ReadFileText(string name){
  int h=FileOpen(name,FILE_READ|FILE_TXT|FILE_ANSI); if(h==INVALID_HANDLE) return "";
  string s=""; while(!FileIsEnding(h)) s+=FileReadString(h); FileClose(h); return s;
}
void AppendResult(string line){
  int h=FileOpen("nnfx_batch_results.jsonl",FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI);
  if(h==INVALID_HANDLE) return; FileSeek(h,0,SEEK_END); FileWriteString(h,line+"\n"); FileClose(h);
}
string JStr(string obj,string key){
  int k=StringFind(obj,"\""+key+"\""); if(k<0) return "";
  int c=StringFind(obj,":",k); int q1=StringFind(obj,"\"",c); if(q1<0) return "";
  int q2=StringFind(obj,"\"",q1+1); if(q2<0) return ""; return StringSubstr(obj,q1+1,q2-q1-1);
}
int JInt(string obj,string key,int dflt=-1){
  int k=StringFind(obj,"\""+key+"\""); if(k<0) return dflt; int c=StringFind(obj,":",k);
  bool neg=false; string num=""; int i=c+1;
  while(i<StringLen(obj)){ ushort ch=StringGetCharacter(obj,i);
    if(ch=='-'&&StringLen(num)==0){neg=true;i++;continue;}
    if(ch>='0'&&ch<='9'){num+=CharToString((uchar)ch);i++;} else break; }
  if(StringLen(num)==0) return dflt;
  int v=(int)StringToInteger(num); return neg? -v: v;
}
#define EV(x) ((x)==EMPTY_VALUE ? "EMPTY" : DoubleToString((x),8))

void OnStart(){
  string body=ReadFileText("nnfx_batch.json");
  if(StringLen(body)==0){ AppendResult("{\"fatal\":\"no job file\"}"); return; }
  int jstart=StringFind(body,"\"jobs\""); int arrS=StringFind(body,"[",jstart); int arrE=StringFind(body,"]",arrS);
  string jarr=StringSubstr(body,arrS+1,arrE-arrS-1);
  int sstart=StringFind(body,"\"symbols\""); int sArrS=StringFind(body,"[",sstart); int sArrE=StringFind(body,"]",sArrS);
  string sarr=StringSubstr(body,sArrS+1,sArrE-sArrS-1);

  string rels[], keys[], roles[]; int bufA[], bufB[]; int njobs=0;
  int pos=0;
  while(true){
    int ob=StringFind(jarr,"{",pos); if(ob<0) break; int cb=StringFind(jarr,"}",ob); if(cb<0) break;
    string obj=StringSubstr(jarr,ob,cb-ob+1); pos=cb+1;
    ArrayResize(rels,njobs+1); ArrayResize(keys,njobs+1); ArrayResize(roles,njobs+1);
    ArrayResize(bufA,njobs+1); ArrayResize(bufB,njobs+1);
    rels[njobs]=JStr(obj,"rel"); keys[njobs]=JStr(obj,"key"); roles[njobs]=JStr(obj,"role");
    bufA[njobs]=JInt(obj,"buf_a",0); bufB[njobs]=JInt(obj,"buf_b",-1);
    njobs++;
  }
  string syms[]; int nsyms=0; pos=0;
  while(true){
    int q1=StringFind(sarr,"\"",pos); if(q1<0) break; int q2=StringFind(sarr,"\"",q1+1); if(q2<0) break;
    ArrayResize(syms,nsyms+1); syms[nsyms]=StringSubstr(sarr,q1+1,q2-q1-1); nsyms++; pos=q2+1;
  }
  AppendResult("{\"info\":\"loaded\",\"jobs\":"+IntegerToString(njobs)+",\"symbols\":"+IntegerToString(nsyms)+"}");

  for(int s=0;s<nsyms;s++){
    string sym=syms[s];
    if(!SymbolSelect(sym,true)){ AppendResult("{\"symbol\":\""+sym+"\",\"status\":\"symbol_select_failed\"}"); continue; }
    int total=iBars(sym,PERIOD_D1);

    int h_ma=iCustom(sym,PERIOD_D1,"Examples\\Custom Moving Average",20,0,MODE_SMA);
    int h_macd=iCustom(sym,PERIOD_D1,"Examples\\MACD");
    int h_rvi=iCustom(sym,PERIOD_D1,"Examples\\RVI");
    int h_atr=iATR(sym,PERIOD_D1,14);

    int handles[]; ArrayResize(handles,njobs);
    for(int k=0;k<njobs;k++) handles[k]=iCustom(sym,PERIOD_D1,rels[k]);

    int waited=0;
    while(waited<20){
      bool ready=(BarsCalculated(h_ma)>0 && BarsCalculated(h_macd)>0 && BarsCalculated(h_rvi)>0 && BarsCalculated(h_atr)>0);
      for(int k=0;k<njobs;k++) if(BarsCalculated(handles[k])<=0) ready=false;
      if(ready) break;
      Sleep(100); waited++;
    }

    double ma[],macdM[],macdS[],rvi[],atr[];
    ArraySetAsSeries(ma,true);ArraySetAsSeries(macdM,true);ArraySetAsSeries(macdS,true);ArraySetAsSeries(rvi,true);ArraySetAsSeries(atr,true);
    CopyBuffer(h_ma,0,0,total,ma);CopyBuffer(h_macd,0,0,total,macdM);CopyBuffer(h_macd,1,0,total,macdS);
    CopyBuffer(h_rvi,0,0,total,rvi);CopyBuffer(h_atr,0,0,total,atr);

    double allA[]; ArrayResize(allA,njobs*total);
    double allB[]; ArrayResize(allB,njobs*total);
    for(int k=0;k<njobs;k++){
      double a[]; ArraySetAsSeries(a,true);
      int gotA = (handles[k]!=INVALID_HANDLE) ? CopyBuffer(handles[k],bufA[k],0,total,a) : -1;
      for(int i=0;i<total;i++) allA[k*total+i]=(i<gotA)?a[i]:EMPTY_VALUE;
      if(bufB[k]>=0){
        double b[]; ArraySetAsSeries(b,true);
        int gotB=(handles[k]!=INVALID_HANDLE) ? CopyBuffer(handles[k],bufB[k],0,total,b) : -1;
        for(int i=0;i<total;i++) allB[k*total+i]=(i<gotB)?b[i]:EMPTY_VALUE;
      }
      AppendResult("{\"symbol\":\""+sym+"\",\"key\":\""+keys[k]+"\",\"handle\":"+(handles[k]==INVALID_HANDLE?"\"invalid\"":"\"ok\"")+"}");
    }

    string outfile="nnfx_batch_extract_"+sym+".csv";
    int fh=FileOpen(outfile,FILE_WRITE|FILE_TXT|FILE_ANSI);
    if(fh==INVALID_HANDLE){ AppendResult("{\"symbol\":\""+sym+"\",\"status\":\"cannot_open_output\"}"); continue; }
    string header="date,close,high,low,ma,macd_m,macd_s,rvi,atr";
    for(int k=0;k<njobs;k++){ header+=","+keys[k]+"_a"; if(bufB[k]>=0) header+=","+keys[k]+"_b"; }
    FileWriteString(fh,header+"\r\n");
    for(int i=total-1;i>=0;i--){
      datetime t=iTime(sym,PERIOD_D1,i);
      string line=TimeToString(t,TIME_DATE)+","+DoubleToString(iClose(sym,PERIOD_D1,i),8)+","+
                  DoubleToString(iHigh(sym,PERIOD_D1,i),8)+","+DoubleToString(iLow(sym,PERIOD_D1,i),8)+","+
                  EV(ma[i])+","+EV(macdM[i])+","+EV(macdS[i])+","+EV(rvi[i])+","+EV(atr[i]);
      for(int k=0;k<njobs;k++){ line+=","+EV(allA[k*total+i]); if(bufB[k]>=0) line+=","+EV(allB[k*total+i]); }
      FileWriteString(fh,line+"\r\n");
    }
    FileClose(fh);
    for(int k=0;k<njobs;k++) IndicatorRelease(handles[k]);
    IndicatorRelease(h_ma); IndicatorRelease(h_macd); IndicatorRelease(h_rvi); IndicatorRelease(h_atr);
    AppendResult("{\"symbol\":\""+sym+"\",\"status\":\"done\",\"rows\":"+IntegerToString(total)+"}");
  }
  int f=FileOpen("nnfx_batch_done.flag",FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI);
  if(f!=INVALID_HANDLE){ FileWriteString(f,"done"); FileClose(f); }
  TerminalClose(0);
}
'''

def copy_history_for_symbols(live, rt, symbols):
    """pb.copy_mt5_history() only seeds ONE symbol's cached history into a
    fresh clone (whichever was most recently active). A multi-pair basket
    needs all of them, so this copies each basket symbol's history folder
    directly -- same mechanism, just not limited to a single symbol. Lives
    here rather than in preview_bridge.py since preview never needed more
    than one symbol; this doesn't touch preview_bridge.py at all."""
    copied = []
    bases_dir = Path(live) / 'bases'
    if not bases_dir.exists():
        return copied
    for server_dir in bases_dir.iterdir():
        hist = server_dir / 'history'
        if not hist.is_dir():
            continue
        for sym in symbols:
            src = hist / sym
            if not src.is_dir():
                continue
            dst = Path(rt) / src.relative_to(live)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(dst, ignore_errors=True)
            try:
                shutil.copytree(src, dst)
                copied.append(sym)
            except Exception:
                pass
    return copied

def _read_jsonl(path):
    out = []
    if not path.exists():
        return out
    for line in pb.read_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out

def _wait_batch(files_dir, proc, timeout):
    done = files_dir / 'nnfx_batch_done.flag'
    end = time.monotonic() + timeout
    last = -1
    while time.monotonic() < end:
        pb.hide_pid(proc.pid)
        res = _read_jsonl(files_dir / 'nnfx_batch_results.jsonl')
        if len(res) != last:
            last = len(res)
            stage_event('batch_progress', events=last)
        if done.exists() or (proc.poll() is not None):
            break
        time.sleep(1.0)
    return done.exists()

# ---- extraction classification (same rules used throughout this pilot) ------

def fnum(s):
    if s in ('EMPTY', 'FAILED', ''):
        return None
    try:
        v = float(s)
    except Exception:
        return None
    if v != v:
        return None
    return v

def classify_column(rows, col):
    vals_raw = [r[col] for r in rows]
    n_total = len(vals_raw)
    numeric = [float(v) for v in vals_raw if v not in ('EMPTY', 'FAILED', '')]
    n_sentinel = sum(1 for v in numeric if abs(v) > 1e100 or v != v)
    usable = [v for v in numeric if abs(v) <= 1e100 and v == v]
    distinct_recent = len(set(round(v, 4) for v in usable[-500:])) if len(usable) >= 20 else len(set(round(v, 4) for v in usable))
    if n_sentinel > 0.05 * n_total:
        return 'EXTRACT_GARBAGE', f'{n_sentinel}/{n_total} rows leak sentinel as real number'
    if len(usable) < 30:
        return 'EXTRACT_GARBAGE', f'only {len(usable)} usable rows'
    if distinct_recent <= 3:
        return 'EXTRACT_GARBAGE', f'only {distinct_recent} distinct recent values -- degenerate'
    return 'EXTRACT_OK', ''

# ---- engine wiring (same roles as run_pilot_mass.py, generalized) -----------

def rolling_avg(vals, window=20):
    out = [None] * len(vals)
    for i in range(len(vals)):
        window_vals = vals[max(0, i - window + 1):i + 1]
        if len(window_vals) == window and all(v is not None for v in window_vals):
            out[i] = sum(window_vals) / window
    return out

def score_candidate(symbol_data, symbols, role, col_a, col_b, zero_ref):
    per_symbol = {}
    pooled_trades = []
    bridge_skips = continuation_trades = 0
    fingerprints = {}
    for sym in symbols:
        d = symbol_data[sym]
        rows = d['rows']
        cand_a = [fnum(r[col_a]) for r in rows]
        cand_b = [fnum(r[col_b]) for r in rows] if col_b else None
        vol_avg = rolling_avg(cand_a, 20) if role == 'VOLUME' else None

        eng = NNFXEngine(NNFXParams(pip_size=0.01 if 'JPY' in sym else 0.0001))
        records = []
        prev_close = prev_baseline = None
        for i, r in enumerate(rows):
            atrv = fnum(r['atr'])
            if role == 'CONFIRMATION_1':
                c1f, c1s = cand_a[i], (cand_b[i] if cand_b else None)
                c2v = fnum(r['rvi']); base = fnum(r['ma']); c2z = 0.0
                volv, volavg = 1.0, 0.5
            elif role == 'CONFIRMATION_2':
                c1f, c1s = fnum(r['macd_m']), fnum(r['macd_s'])
                c2v = cand_a[i]; base = fnum(r['ma']); c2z = zero_ref if zero_ref is not None else 0.0
                volv, volavg = 1.0, 0.5
            elif role == 'BASELINE':
                c1f, c1s = fnum(r['macd_m']), fnum(r['macd_s'])
                c2v = fnum(r['rvi']); base = cand_a[i]; c2z = 0.0
                volv, volavg = 1.0, 0.5
            else:  # VOLUME
                c1f, c1s = fnum(r['macd_m']), fnum(r['macd_s'])
                c2v = fnum(r['rvi']); base = fnum(r['ma']); c2z = 0.0
                volv = cand_a[i]; volavg = vol_avg[i]
            if None in (c1f, c1s, c2v, base, volv, volavg, atrv):
                prev_close = fnum(r['close'])
                if base is not None:
                    prev_baseline = base
                continue
            bar = dict(date=r['date'], close=fnum(r['close']), high=fnum(r['high']), low=fnum(r['low']),
                       close_prev=prev_close if prev_close is not None else fnum(r['close']),
                       baseline=base, baseline_prev=prev_baseline if prev_baseline is not None else base,
                       c1_fast=c1f, c1_slow=c1s, c2_value=c2v, c2_zero_reference=c2z,
                       volume_value=volv, volume_avg=volavg, atr=atrv)
            rec = eng.process_bar(bar)
            rec['date'] = r['date']
            records.append(rec)
            prev_close = fnum(r['close']); prev_baseline = base

        window = [r for r in records if r['date'][:4] >= '2019']
        bridge_skips += sum(1 for r in window if r['reason'] == 'skip:bridge_too_far')
        continuation_trades += sum(1 for r in window if r['reason'] == 'enter:continuation')
        trades = []
        pending_half1 = None
        for r in window:
            if r['action'] == 'exit_half':
                pending_half1 = r['pips']
            elif r['action'] == 'exit':
                trades.append((pending_half1 + r['pips']) / 2.0 if pending_half1 is not None else r['pips'])
                pending_half1 = None
        per_symbol[sym] = len(trades)
        pooled_trades.extend(trades)
        fingerprints[sym] = tuple((r['date'], r['action'], r['reason']) for r in window)

    n = len(pooled_trades)
    wins = sum(1 for t in pooled_trades if t > 0)
    losses = sum(1 for t in pooled_trades if t < 0)
    win_rate = round(100 * wins / n, 1) if n else 0.0
    expectancy = round(sum(pooled_trades) / n, 2) if n else 0.0
    gains = sum(t for t in pooled_trades if t > 0)
    loss_sum = -sum(t for t in pooled_trades if t < 0)
    profit_factor = round(gains / loss_sum, 2) if loss_sum > 0 else (999.0 if gains > 0 else 0.0)
    equity = peak = maxdd = 0.0
    for t in pooled_trades:
        equity += t; peak = max(peak, equity); maxdd = min(maxdd, equity - peak)
    return dict(trades=n, wins=wins, losses=losses, win_rate=win_rate, expectancy_pips=expectancy,
                profit_factor=profit_factor, max_drawdown=round(maxdd, 1), bridge_skips=bridge_skips,
                continuation_trades=continuation_trades, per_symbol=per_symbol, fingerprints=fingerprints)

# ---- run ----------------------------------------------------------------------

def run(db, out, symbols, limit=None, min_trades=30, force=False):
    conn = connect(db)
    ensure_backtest_table(conn)
    items = candidacy_rows(conn, limit)
    if not items:
        emit({'type': 'fatal', 'error': 'No MQL5 candidacy rows found (run nnfx-map first, or every candidate is NEEDS_REVIEW/non-MQL5).'})
        conn.close()
        return
    bed = '+'.join(symbols) + '|D1|LIVE'

    stage_event('discovering_runtime')
    all_terminals = pb.terminals()
    mt5 = next((t for t in all_terminals if t['kind'] == 'MT5' and t.get('terminal') and t.get('editor') and t.get('data_dir')), None)
    if not mt5:
        emit({'type': 'fatal', 'error': 'No usable MT5 terminal+data pairing found on this machine.'})
        conn.close()
        return
    stage_event('runtime_found', terminal=mt5['terminal'], data_dir=mt5['data_dir'])

    # NOTE: MetaEditor's own /inc:<path> argument parsing truncates at the
    # first space in the value -- confirmed directly (a runtime cloned under
    # "...MQL Indicator Library\_pilot_run\..." produced
    # "error 106: file 'D:\MQL\Include\mql4_compat.mqh' not found", i.e. it
    # silently cut the path at "MQL Indicator" -> "MQL"). This reproduces
    # even though the OS-level argv is correctly quoted by subprocess, so
    # it's a MetaEditor-internal parsing limitation, not something fixable
    # from the Python side of the call. Windows 8.3 short-path generation is
    # disabled on this volume (confirmed: GetFolder(...).ShortPath returns
    # the path unchanged), so that workaround isn't available either. The
    # fix: never clone the MT5 runtime under a space-containing directory,
    # regardless of what --out was given (that stays available for anything
    # that doesn't get passed through the /inc: flag).
    runtime_base = Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir())) / 'nnfx-backtest-runtime'
    rt = pb.clone_runtime(mt5, runtime_base / 'shard-0', 'MT5')
    mqlroot = rt / pb.mql_dir_name('MT5')
    indicators_dir = mqlroot / 'Indicators' / 'NNFXBacktestBatch'
    scripts_dir = mqlroot / 'Scripts'
    files_dir = mqlroot / 'Files'
    for d in (indicators_dir, scripts_dir, files_dir):
        d.mkdir(parents=True, exist_ok=True)
    for fn in ('nnfx_batch.json', 'nnfx_batch_results.jsonl', 'nnfx_batch_done.flag'):
        try:
            (files_dir / fn).unlink()
        except Exception:
            pass
    for f in files_dir.glob('nnfx_batch_extract_*.csv'):
        try:
            f.unlink()
        except Exception:
            pass

    terminal_exe = rt / Path(mt5['terminal']).name
    editor = rt / Path(mt5['editor']).name

    stage_event('priming_runtime')
    copied = copy_history_for_symbols(Path(mt5['data_dir']), rt, symbols)
    stage_event('history_copied', symbols=copied)
    sym0 = copied[0] if copied else (pb.copy_mt5_history(Path(mt5['data_dir']), rt) or symbols[0])
    pb.prime_mt5_runtime(rt, terminal_exe, sym0, 'nnfx-backtest')

    # 1) stage every candidate source (mirrors render_shard's staging loop)
    prepared = {}
    staged_any = False
    for it in items:
        sha = it['sha256']
        src = Path(it['path'])
        if not src.is_file():
            prepared[sha] = {'status': 'compile_failed', 'error': 'source file missing'}
            continue
        d = indicators_dir / sha
        d.mkdir(parents=True, exist_ok=True)
        staged = d / pb.safe_name(src)
        try:
            shutil.copy2(src, staged)
        except Exception as e:
            prepared[sha] = {'status': 'compile_failed', 'error': f'stage: {e}'}
            continue
        for dep in src.parent.glob('*.mqh'):
            try:
                shutil.copy2(dep, d / dep.name)
            except Exception:
                pass
        existing = pb.existing_binary(src, '.ex5')
        if existing:
            try:
                shutil.copy2(existing, d / (staged.stem + '.ex5'))
            except Exception:
                pass
        else:
            staged_any = True
        prepared[sha] = {'rel': f'NNFXBacktestBatch\\{sha}\\{staged.stem}', 'staged': staged, 'needs_compile': not existing}

    # 2) compile each staged file via pb.compile_file -- the same tested
    # single-file primitive pb.stage() uses. A folder-level `/compile:<dir>`
    # invocation was tried first (mirroring render_shard's apparent pattern)
    # but produced no .ex5/.log at all in this environment even for a single
    # flat file, so this loop is the verified-working path instead.
    to_compile = sum(1 for v in prepared.values() if v.get('needs_compile'))
    if to_compile:
        stage_event('compiling', count=to_compile)
    for sha, info in prepared.items():
        if not info.get('needs_compile'):
            continue
        built, _cmds, log = pb.compile_file(editor, info['staged'], mqlroot)
        if not built:
            info['status'] = 'compile_failed'
            info['error'] = (log or 'MetaEditor produced no .ex5')[-800:]

    # 3) build job list; anything without a compiled binary is COMPILE_FAILED
    jobs = []
    compile_failed = []
    for it in items:
        sha = it['sha256']
        info = prepared.get(sha, {})
        if info.get('status') == 'compile_failed':
            compile_failed.append({'item': it, 'reason': info.get('error', '')})
            continue
        binary = info['staged'].with_suffix('.ex5')
        if not (binary.exists() and binary.stat().st_size > 0):
            compile_failed.append({'item': it, 'reason': 'MetaEditor produced no .ex5 (source did not compile)'})
            continue
        jobs.append({'key': sha, 'rel': info['rel'], 'role': it['slot'], 'buf_a': it['buf_a'], 'buf_b': it['buf_b'], 'item': it})

    for cf in compile_failed:
        it = cf['item']
        write_status_row(conn, it, bed, 'COMPILE_FAILED', cf['reason'])
    stage_event('compile_summary', ok=len(jobs), failed=len(compile_failed))

    if not jobs:
        emit({'type': 'batch_complete', 'compiled': 0, 'compile_failed': len(compile_failed), 'scored': 0})
        conn.close()
        return

    # 4) write job list + controller, compile controller, launch ONE session
    job_json = {'jobs': [{'key': j['key'], 'rel': j['rel'], 'role': j['role'],
                           'buf_a': j['buf_a'], 'buf_b': j['buf_b'] if j['buf_b'] is not None else -1} for j in jobs],
                'symbols': symbols}
    # compact separators (no space after ':' or ','): the controller's hand-rolled
    # JInt() parser doesn't skip whitespace before a digit, so json.dumps's default
    # "key": 1 style silently made every int field parse back as its -1 default
    # (confirmed directly: buf_b=1 in the DB came back as -1 in the CSV column
    # count, dropping the whole _b column and crashing the Python-side lookup).
    (files_dir / 'nnfx_batch.json').write_text(json.dumps(job_json, separators=(',', ':')), encoding='utf-8')
    ctrl = scripts_dir / 'NNFXBacktestBatchController.mq5'
    ctrl.write_text(CONTROLLER_SRC, encoding='utf-8')
    built, _cmds, log = pb.compile_file(editor, ctrl, mqlroot)
    if not built:
        emit({'type': 'fatal', 'error': 'batch controller failed to compile', 'log': (log or '')[-2000:]})
        conn.close()
        return

    cfg = rt / 'nnfx-backtest-batch.ini'
    cfg.write_text(f'[Experts]\nEnabled=1\nAllowLiveTrading=0\nAllowDllImport=0\n\n'
                    f'[StartUp]\nSymbol={symbols[0]}\nPeriod=D1\nScript={ctrl.stem}\nShutdownTerminal=0\n', encoding='utf-8')
    stage_event('session_launch', jobs=len(jobs), symbols=symbols)
    proc = pb.subprocess.Popen([str(terminal_exe), '/portable', f'/config:{cfg}'],
                                cwd=str(rt), creationflags=pb.CREATE_NO_WINDOW, startupinfo=pb.startupinfo())
    try:
        finished = _wait_batch(files_dir, proc, timeout=max(180, 20 * len(jobs) * len(symbols)))
    finally:
        pb.terminate_tree(proc)
    if not finished:
        stage_event('batch_timeout')

    # keep a durable copy of the raw extraction + job artifacts wherever the
    # caller wanted them (--out); this is a plain file copy, not passed
    # through MetaEditor's /inc: flag, so it's fine even if it contains spaces.
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in files_dir.glob('nnfx_batch_extract_*.csv'):
        try:
            shutil.copy2(f, out_dir / f.name)
        except Exception:
            pass
    for fn in ('nnfx_batch.json', 'nnfx_batch_results.jsonl'):
        try:
            shutil.copy2(files_dir / fn, out_dir / fn)
        except Exception:
            pass

    # 5) load per-symbol CSVs, classify, score, write results
    symbol_data = {}
    for sym in symbols:
        p = files_dir / f'nnfx_batch_extract_{sym}.csv'
        if not p.exists():
            symbol_data[sym] = {'rows': []}
            continue
        import csv as _csv
        symbol_data[sym] = {'rows': list(_csv.DictReader(open(p, encoding='utf-8', errors='replace')))}

    scored = 0
    for j in jobs:
        it = j['item']
        try:
            col_a = j['key'] + '_a'
            col_b = j['key'] + '_b' if j['buf_b'] is not None else None
            bad = False
            bad_reason = ''
            for sym in symbols:
                rows = symbol_data[sym]['rows']
                if not rows:
                    bad, bad_reason = True, f'{sym}: no extraction rows'
                    break
                if col_b and col_b not in rows[0]:
                    bad, bad_reason = True, f'{sym}: expected column {col_b} missing from extraction (buf_b resolution mismatch)'
                    break
                status, detail = classify_column(rows, col_a)
                if status != 'EXTRACT_OK':
                    bad, bad_reason = True, f'{sym}: {detail}'
                    break
            if bad:
                write_status_row(conn, it, bed, 'EXTRACT_GARBAGE', bad_reason)
                continue
            result = score_candidate(symbol_data, symbols, it['slot'], col_a, col_b, it['zero_reference'])
            write_score_row(conn, it, bed, result)
            scored += 1
            emit({'type': 'item_scored', 'filename': it['filename'], 'slot': it['slot'],
                  'trades': result['trades'], 'expectancy_pips': result['expectancy_pips']})
        except Exception as exc:
            # Never let one candidate's unexpected failure take the whole batch
            # down -- record it and keep going, same as every other failure
            # class this batch tracks.
            write_status_row(conn, it, bed, 'SCORING_ERROR', f'{type(exc).__name__}: {exc}')
            emit({'type': 'item_error', 'filename': it['filename'], 'error': f'{type(exc).__name__}: {exc}'})

    conn.commit()
    conn.close()
    emit({'type': 'batch_complete', 'compiled': len(jobs), 'compile_failed': len(compile_failed), 'scored': scored, 'bed': bed})

def write_status_row(conn, it, bed, status, reason):
    """Never silently drops a candidate: COMPILE_FAILED / EXTRACT_GARBAGE both
    get a real row with trades=0 and the reason recorded, exactly like every
    classification pass this session wrote to backtest_results."""
    conn.execute("""
        INSERT INTO backtest_results(sha256, slot, signal_type, bed, stage, trades, wins, losses,
            win_rate, expectancy_pips, profit_factor, max_drawdown, bridge_skips, continuation_trades,
            mapping_source, mapping_reason, tested_at, zero_reference, buf_a, buf_b)
        VALUES (?,?,?,?,1,0,0,0,0,0,0,0,0,0,'batch',?,datetime('now'),?,?,?)
        ON CONFLICT(sha256, slot, bed) DO UPDATE SET mapping_reason=excluded.mapping_reason, tested_at=excluded.tested_at
    """, (it['sha256'], it['slot'], it['signal_type'], bed, f'[{status}] {reason} :: {it["filename"]}',
          it['zero_reference'], it['buf_a'], it['buf_b']))
    conn.commit()  # per-candidate, not batched to the end -- a later candidate's
                   # crash must never lose an earlier one's already-written row
                   # (confirmed the hard way: an uncommitted-until-the-end batch
                   # lost all 14 real results to one unrelated exception).

def write_score_row(conn, it, bed, r):
    conn.execute("""
        INSERT INTO backtest_results(sha256, slot, signal_type, bed, stage, trades, wins, losses,
            win_rate, expectancy_pips, profit_factor, max_drawdown, bridge_skips, continuation_trades,
            mapping_source, mapping_reason, tested_at, zero_reference, buf_a, buf_b)
        VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?,?,'batch',?,datetime('now'),?,?,?)
        ON CONFLICT(sha256, slot, bed) DO UPDATE SET trades=excluded.trades, wins=excluded.wins,
            losses=excluded.losses, win_rate=excluded.win_rate, expectancy_pips=excluded.expectancy_pips,
            profit_factor=excluded.profit_factor, max_drawdown=excluded.max_drawdown,
            bridge_skips=excluded.bridge_skips, continuation_trades=excluded.continuation_trades, tested_at=excluded.tested_at
    """, (it['sha256'], it['slot'], it['signal_type'], bed, r['trades'], r['wins'], r['losses'], r['win_rate'],
          r['expectancy_pips'], r['profit_factor'], r['max_drawdown'], r['bridge_skips'], r['continuation_trades'],
          f'[EXTRACT_OK] per_symbol={r["per_symbol"]} :: {it["filename"]}', it['zero_reference'], it['buf_a'], it['buf_b']))
    conn.commit()

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('plan'); p.add_argument('--db', required=True); p.add_argument('--limit', type=int)
    p = sub.add_parser('run')
    p.add_argument('--db', required=True); p.add_argument('--out', required=True)
    p.add_argument('--bed-symbols', default='EURUSD,GBPUSD,USDJPY')
    p.add_argument('--limit', type=int); p.add_argument('--min-trades', type=int, default=30)
    p.add_argument('--force', action='store_true')
    args = ap.parse_args()
    if args.cmd == 'plan':
        plan(args.db, args.limit)
    elif args.cmd == 'run':
        run(args.db, args.out, [s.strip() for s in args.bed_symbols.split(',') if s.strip()],
            args.limit, args.min_trades, args.force)

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        emit({'type': 'fatal', 'error': f'{type(exc).__name__}: {exc}'})
        sys.exit(1)
