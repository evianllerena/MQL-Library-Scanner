from __future__ import annotations
import argparse, json, os, sqlite3, sys, time
from dataclasses import asdict
from pathlib import Path
from typing import Iterable
from engine_core import Analysis, analyze, nnfx_slot_signal

SCHEMA_VERSION = 4
JSON_FIELDS = ['draw_types','standard_indicators','custom_dependencies','secondary_categories','behavior_tags','techniques','evidence','warnings','user_tags','line_buffer_indices']

# Windows/PyInstaller can otherwise inherit a legacy ANSI console encoding even
# when the parent process consumes UTF-8. Keep the engine protocol UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='backslashreplace', line_buffering=True)
    except Exception:
        pass

def emit(kind: str, **payload):
    """Write one JSON protocol line without depending on the Windows charmap codec."""
    obj={'type':kind, **payload}
    text=json.dumps(obj, ensure_ascii=False, default=str) + '\n'
    data=text.encode('utf-8', errors='backslashreplace')
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except Exception:
        safe=(json.dumps(obj, ensure_ascii=True, default=str) + '\n').encode('ascii', errors='backslashreplace')
        try:
            sys.stdout.buffer.write(safe)
            sys.stdout.buffer.flush()
        except Exception:
            pass

def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn=sqlite3.connect(db, timeout=30)
    conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA busy_timeout=30000')
    migrate(conn)
    return conn

def cols(conn, table):
    return {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}

def migrate(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_scan_at TEXT);
    CREATE TABLE IF NOT EXISTS indicators(
      id INTEGER PRIMARY KEY, path TEXT UNIQUE, filename TEXT, platform TEXT, sha256 TEXT, size INTEGER,
      source_structure TEXT, display_location TEXT, declared_buffers INTEGER, declared_plots INTEGER, active_buffers INTEGER,
      draw_types TEXT, line_plots INTEGER, histogram_plots INTEGER, arrow_plots INTEGER, filling_plots INTEGER, object_usage INTEGER,
      standard_indicators TEXT, custom_dependencies TEXT, primary_category TEXT, secondary_categories TEXT, visual_category TEXT,
      behavior_tags TEXT, techniques TEXT DEFAULT '[]', evidence TEXT DEFAULT '[]', classification_status TEXT DEFAULT 'Unknown',
      review_reason TEXT DEFAULT '', classifier_version TEXT DEFAULT '', confidence INTEGER, warnings TEXT, duplicate_of TEXT,
      family_fingerprint TEXT DEFAULT '', family_id TEXT, human_verified INTEGER DEFAULT 0, verified_primary TEXT,
      verified_secondary TEXT DEFAULT '[]', verified_at TEXT, analyzed_at TEXT DEFAULT CURRENT_TIMESTAMP,
      mtime_ns INTEGER DEFAULT 0, scan_status TEXT DEFAULT 'complete', user_favorite INTEGER DEFAULT 0, user_tags TEXT DEFAULT '[]'
    );
    CREATE TABLE IF NOT EXISTS classification_memory(
      id INTEGER PRIMARY KEY, indicator_id INTEGER, family_fingerprint TEXT, original_primary TEXT, corrected_primary TEXT,
      corrected_secondary TEXT DEFAULT '[]', evidence_snapshot TEXT DEFAULT '[]', created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS backtest_results(
      id INTEGER PRIMARY KEY,
      sha256 TEXT NOT NULL,
      slot TEXT NOT NULL,            -- CONFIRMATION | BASELINE | VOLUME | EXIT
      signal_type TEXT,             -- ZERO_CROSS | TWO_LINE_CROSS | ARROW
      bed TEXT,                     -- e.g. "EURUSD|D1|2020-2025"
      stage INTEGER,                -- 1 = standard bed, 2 = robustness basket
      trades INTEGER, wins INTEGER, losses INTEGER,
      win_rate REAL, expectancy_pips REAL, profit_factor REAL,
      max_drawdown REAL, net_pips REAL, net_pips_saved REAL,  -- saved = VOLUME slot
      bridge_skips INTEGER, continuation_trades INTEGER,      -- rule audit counts
      trade_log_path TEXT, tested_at TEXT,
      UNIQUE(sha256, slot, bed)
    );
    CREATE INDEX IF NOT EXISTS idx_sha ON indicators(sha256);
    CREATE INDEX IF NOT EXISTS idx_primary ON indicators(primary_category);
    CREATE INDEX IF NOT EXISTS idx_status ON indicators(classification_status);
    CREATE INDEX IF NOT EXISTS idx_visual ON indicators(visual_category);
    CREATE INDEX IF NOT EXISTS idx_platform ON indicators(platform);
    CREATE INDEX IF NOT EXISTS idx_filename ON indicators(filename);
    CREATE INDEX IF NOT EXISTS idx_mtime_size ON indicators(mtime_ns,size);
    CREATE INDEX IF NOT EXISTS idx_family ON indicators(family_fingerprint);
    CREATE INDEX IF NOT EXISTS idx_verified ON indicators(human_verified);
    CREATE INDEX IF NOT EXISTS ix_bt_slot ON backtest_results(slot);
    CREATE INDEX IF NOT EXISTS ix_bt_sha ON backtest_results(sha256);
    ''')
    # backtest_results is new (STEP 1 of NNFX_INTEGRATION_PLAN.txt). CREATE TABLE
    # IF NOT EXISTS above already makes existing DBs upgrade cleanly with zero
    # rows lost; this table_info check is the same guard style used for
    # `indicators` below. The columns mirror the indicators verify/correct flow
    # (human_verified/verified_primary/verified_secondary/verified_at) so a
    # per-indicator slot/signal_type override (STEP 2, wired up later) never
    # needs another migration.
    bt_additions={
      'mapping_source':"TEXT DEFAULT 'auto'",'mapping_reason':'TEXT','human_verified':'INTEGER DEFAULT 0',
      'verified_slot':'TEXT','verified_signal_type':'TEXT','verified_at':'TEXT','zero_reference':'REAL',
      'buf_a':'INTEGER','buf_b':'INTEGER'  # real resolved buffer index/indices (fast/slow for
                                            # CONFIRMATION_1, single main buffer -- buf_b NULL -- otherwise)
    }
    bt_existing=cols(conn,'backtest_results')
    for name,decl in bt_additions.items():
        if name not in bt_existing: conn.execute(f'ALTER TABLE backtest_results ADD COLUMN {name} {decl}')
    additions={
      'techniques':"TEXT DEFAULT '[]'",'evidence':"TEXT DEFAULT '[]'",'classification_status':"TEXT DEFAULT 'Unknown'",
      'review_reason':"TEXT DEFAULT ''",'classifier_version':"TEXT DEFAULT ''",'family_fingerprint':"TEXT DEFAULT ''",
      'family_id':'TEXT','human_verified':'INTEGER DEFAULT 0','verified_primary':'TEXT','verified_secondary':"TEXT DEFAULT '[]'",
      'verified_at':'TEXT','mtime_ns':'INTEGER DEFAULT 0','scan_status':"TEXT DEFAULT 'complete'",'user_favorite':'INTEGER DEFAULT 0','user_tags':"TEXT DEFAULT '[]'",
      'preview_status':"TEXT DEFAULT 'pending'",'preview_path':'TEXT','preview_hash':'TEXT','preview_error':"TEXT DEFAULT ''",'preview_updated_at':'TEXT',
      'line_buffer_indices':"TEXT DEFAULT '[]'"
    }
    existing=cols(conn,'indicators')
    for name,decl in additions.items():
        if name not in existing: conn.execute(f'ALTER TABLE indicators ADD COLUMN {name} {decl}')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_preview_status ON indicators(preview_status)')
    conn.execute("INSERT INTO meta(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(SCHEMA_VERSION),))
    conn.commit()

def display_path(p: Path) -> str:
    try:
        return str(p.resolve())
    except Exception:
        return str(p.absolute())

def discover(sources: Iterable[Path]):
    found: dict[str, Path] = {}
    diagnostics=[]
    for root in sources:
        root_path=display_path(root)
        info={'path':root_path,'exists':root.exists(),'is_dir':root.is_dir(),'mq4':0,'mq5':0,'compiled_ex4':0,'compiled_ex5':0,'access_errors':[]}
        if not root.exists():
            diagnostics.append(info)
            continue
        if root.is_file():
            ext=root.suffix.lower()
            if ext in ('.mq4','.mq5'):
                found[root_path.lower()]=root
                info[ext[1:]]+=1
            elif ext=='.ex4': info['compiled_ex4']+=1
            elif ext=='.ex5': info['compiled_ex5']+=1
            diagnostics.append(info)
            continue
        def onerror(err):
            info['access_errors'].append(f'{getattr(err,"filename",root_path)}: {err}')
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=onerror, followlinks=False):
            for name in filenames:
                ext=Path(name).suffix.lower()
                if ext not in ('.mq4','.mq5','.ex4','.ex5'):
                    continue
                p=Path(dirpath)/name
                if ext=='.mq4':
                    info['mq4']+=1
                    found[display_path(p).lower()]=p
                elif ext=='.mq5':
                    info['mq5']+=1
                    found[display_path(p).lower()]=p
                elif ext=='.ex4': info['compiled_ex4']+=1
                elif ext=='.ex5': info['compiled_ex5']+=1
        diagnostics.append(info)
    return sorted(found.values(),key=lambda p:str(p).casefold()), diagnostics

def save_analysis(conn,a:Analysis,mtime_ns:int):
    d=asdict(a)
    for k in JSON_FIELDS:
        if k in d: d[k]=json.dumps(d[k],ensure_ascii=False)
    d['object_usage']=1 if d['object_usage'] else 0; d['mtime_ns']=mtime_ns; d['scan_status']='complete'
    cols_=list(d.keys())
    sql=f"INSERT INTO indicators({','.join(cols_)}) VALUES({','.join('?' for _ in cols_)}) ON CONFLICT(path) DO UPDATE SET "+','.join(f"{c}=excluded.{c}" for c in cols_ if c!='path')+", analyzed_at=CURRENT_TIMESTAMP"
    conn.execute(sql,[d[c] for c in cols_])

def unchanged(conn,p):
    st=p.stat(); row=conn.execute('SELECT size,mtime_ns,scan_status,classifier_version FROM indicators WHERE path=?',(display_path(p),)).fetchone()
    return bool(row and row['scan_status']=='complete' and row['size']==st.st_size and row['mtime_ns']==st.st_mtime_ns and row['classifier_version']=='evidence-v5')

def empty_scan_message(diagnostics):
    parts=[]
    for d in diagnostics:
        if not d['exists']:
            parts.append(f"Path not found: {d['path']}")
            continue
        src=d['mq4']+d['mq5']; compiled=d['compiled_ex4']+d['compiled_ex5']
        msg=f"{d['path']}: {src} MQ4/MQ5 source files"
        if compiled:
            msg+=f", {compiled} compiled EX4/EX5 files (compiled files cannot be source-classified)"
        if d['access_errors']:
            msg+=f", {len(d['access_errors'])} inaccessible subfolder(s)"
        parts.append(msg)
    return 'No MQ4/MQ5 source files were discovered. ' + ' | '.join(parts)

def scan(db,sources,force=False):
    conn=connect(db)
    for src in sources: conn.execute('INSERT INTO sources(path,enabled) VALUES(?,1) ON CONFLICT(path) DO UPDATE SET enabled=1',(display_path(src),))
    conn.commit()
    files, diagnostics=discover(sources)
    for d in diagnostics:
        emit('source_preflight', **d, source_files=d['mq4']+d['mq5'], compiled_files=d['compiled_ex4']+d['compiled_ex5'])
    total=len(files)
    emit('scan_start',total=total,sources=[display_path(x) for x in sources])
    if total==0:
        message=empty_scan_message(diagnostics)
        emit('fatal',error=message,diagnostics=diagnostics)
        conn.close()
        return
    processed=skipped=failed=0; started=time.time(); dirty=0
    sha_first={r['sha256']:r['path'] for r in conn.execute('SELECT sha256,path FROM indicators WHERE sha256 IS NOT NULL AND duplicate_of IS NULL')}
    for idx,p in enumerate(files,1):
        try:
            if not force and unchanged(conn,p):
                skipped+=1
                if idx==1 or idx%25==0 or idx==total: emit('progress',current=idx,total=total,processed=processed,skipped=skipped,failed=failed,filename=p.name)
                continue
            a=analyze(p); a.duplicate_of=sha_first.get(a.sha256)
            if a.sha256 not in sha_first: sha_first[a.sha256]=a.path
            save_analysis(conn,a,p.stat().st_mtime_ns); processed+=1; dirty+=1
            if dirty>=25:
                conn.commit(); dirty=0
            emit('item',current=idx,total=total,processed=processed,skipped=skipped,failed=failed,filename=p.name,category=a.primary_category,status=a.classification_status,confidence=a.confidence)
        except Exception as exc:
            failed+=1
            try:
                if conn.in_transaction and isinstance(exc, sqlite3.Error):
                    conn.rollback(); dirty=0
            except Exception:
                pass
            emit('error',current=idx,total=total,processed=processed,skipped=skipped,failed=failed,filename=p.name,error=f'{type(exc).__name__}: {exc}')
            continue
    conn.commit(); now=time.strftime('%Y-%m-%d %H:%M:%S')
    for src in sources: conn.execute('UPDATE sources SET last_scan_at=? WHERE path=?',(now,display_path(src)))
    conn.commit(); elapsed=round(time.time()-started,3)
    emit('scan_complete',total=total,processed=processed,skipped=skipped,failed=failed,elapsed_seconds=elapsed); conn.close()

def stats(db):
    conn=connect(db); one=lambda sql,args=(): conn.execute(sql,args).fetchone()[0]
    result={'total':one('SELECT COUNT(*) FROM indicators'),'mq4':one("SELECT COUNT(*) FROM indicators WHERE platform='MQL4'"),'mq5':one("SELECT COUNT(*) FROM indicators WHERE platform='MQL5'"),'review':one("SELECT COUNT(*) FROM indicators WHERE human_verified=0 AND classification_status IN ('Needs Review','Unknown')"),'duplicates':one('SELECT COUNT(*) FROM indicators WHERE duplicate_of IS NOT NULL'),'favorites':one('SELECT COUNT(*) FROM indicators WHERE user_favorite=1'),'verified':one('SELECT COUNT(*) FROM indicators WHERE human_verified=1'),'sources':[dict(r) for r in conn.execute('SELECT path,enabled,last_scan_at FROM sources ORDER BY path')]}
    text=json.dumps(result,ensure_ascii=False,default=str)+'\n'
    try:
        sys.stdout.buffer.write(text.encode('utf-8',errors='backslashreplace')); sys.stdout.buffer.flush()
    except Exception:
        print(json.dumps(result,ensure_ascii=True,default=str),flush=True)
    conn.close()

def nnfx_map(db):
    """NNFX_INTEGRATION_PLAN.txt STEP 2: derive slot/signal_type for every
    non-duplicate indicator from fields the scanner already stored, and write
    a pre-test candidacy row (bed='') into backtest_results. Never overwrites
    a row a human has already corrected (mapping_source='user'), so it is
    always safe to re-run after a rescan or a Review-queue correction."""
    conn=connect(db)
    rows=conn.execute(
        "SELECT sha256, visual_category, display_location, primary_category, human_verified, verified_primary, "
        "standard_indicators, techniques, path, line_plots, evidence, declared_buffers, line_buffer_indices "
        "FROM indicators WHERE duplicate_of IS NULL AND sha256 IS NOT NULL"
    ).fetchall()
    counts={}; processed=0; skipped_user=0
    for r in rows:
        sha=r['sha256']
        existing=conn.execute("SELECT mapping_source FROM backtest_results WHERE sha256=? AND bed=''",(sha,)).fetchone()
        if existing and existing['mapping_source']=='user':
            skipped_user+=1
            continue
        effective_primary=r['verified_primary'] if r['human_verified'] and r['verified_primary'] else r['primary_category']
        try: std=json.loads(r['standard_indicators'] or '[]')
        except Exception: std=[]
        try: techs=json.loads(r['techniques'] or '[]')
        except Exception: techs=[]
        try: evidence=json.loads(r['evidence'] or '[]')
        except Exception: evidence=[]
        try: line_buf_idx=json.loads(r['line_buffer_indices'] or '[]')
        except Exception: line_buf_idx=[]
        slot,signal_type,zero_ref,buf_a,buf_b,reason=nnfx_slot_signal(
            r['visual_category'],r['display_location'],effective_primary,std,techs,r['path'],
            line_plots=r['line_plots'],evidence=evidence,declared_buffers=r['declared_buffers'],
            line_buffer_indices=line_buf_idx
        )
        conn.execute("DELETE FROM backtest_results WHERE sha256=? AND bed='' AND mapping_source='auto'",(sha,))
        conn.execute(
            "INSERT INTO backtest_results(sha256,slot,signal_type,zero_reference,buf_a,buf_b,bed,mapping_source,mapping_reason) "
            "VALUES(?,?,?,?,?,?,'','auto',?) "
            "ON CONFLICT(sha256,slot,bed) DO UPDATE SET signal_type=excluded.signal_type,zero_reference=excluded.zero_reference,"
            "buf_a=excluded.buf_a,buf_b=excluded.buf_b,mapping_reason=excluded.mapping_reason,mapping_source='auto'",
            (sha,slot,signal_type,zero_ref,buf_a,buf_b,reason)
        )
        counts[slot]=counts.get(slot,0)+1; processed+=1
    conn.commit()
    result={'processed':processed,'skipped_user_overridden':skipped_user,'by_slot':counts}
    text=json.dumps(result,ensure_ascii=False,default=str)+'\n'
    try:
        sys.stdout.buffer.write(text.encode('utf-8',errors='backslashreplace')); sys.stdout.buffer.flush()
    except Exception:
        print(json.dumps(result,ensure_ascii=True,default=str),flush=True)
    conn.close()

def main():
    ap=argparse.ArgumentParser(prog='mql-engine'); sub=ap.add_subparsers(dest='cmd',required=True)
    sp=sub.add_parser('scan'); sp.add_argument('--db',required=True); sp.add_argument('--source',action='append',required=True); sp.add_argument('--force',action='store_true')
    sp=sub.add_parser('stats'); sp.add_argument('--db',required=True)
    sp=sub.add_parser('nnfx-map'); sp.add_argument('--db',required=True)
    args=ap.parse_args(); db=Path(args.db)
    if args.cmd=='scan': scan(db,[Path(x) for x in args.source],args.force)
    elif args.cmd=='stats': stats(db)
    elif args.cmd=='nnfx-map': nnfx_map(db)
if __name__=='__main__':
    try: main()
    except Exception as exc:
        emit('fatal',error=f'{type(exc).__name__}: {exc}'); sys.exit(1)