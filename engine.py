from __future__ import annotations
import argparse, json, sqlite3, sys, time
from dataclasses import asdict
from pathlib import Path
from typing import Iterable
from engine_core import Analysis, analyze

SCHEMA_VERSION = 3
JSON_FIELDS = ['draw_types','standard_indicators','custom_dependencies','secondary_categories','behavior_tags','techniques','evidence','warnings','user_tags']

def emit(kind: str, **payload):
    print(json.dumps({'type':kind, **payload}, ensure_ascii=False), flush=True)

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
    CREATE INDEX IF NOT EXISTS idx_sha ON indicators(sha256);
    CREATE INDEX IF NOT EXISTS idx_primary ON indicators(primary_category);
    CREATE INDEX IF NOT EXISTS idx_status ON indicators(classification_status);
    CREATE INDEX IF NOT EXISTS idx_visual ON indicators(visual_category);
    CREATE INDEX IF NOT EXISTS idx_platform ON indicators(platform);
    CREATE INDEX IF NOT EXISTS idx_filename ON indicators(filename);
    CREATE INDEX IF NOT EXISTS idx_mtime_size ON indicators(mtime_ns,size);
    CREATE INDEX IF NOT EXISTS idx_family ON indicators(family_fingerprint);
    CREATE INDEX IF NOT EXISTS idx_verified ON indicators(human_verified);
    ''')
    additions={
      'techniques':"TEXT DEFAULT '[]'",'evidence':"TEXT DEFAULT '[]'",'classification_status':"TEXT DEFAULT 'Unknown'",
      'review_reason':"TEXT DEFAULT ''",'classifier_version':"TEXT DEFAULT ''",'family_fingerprint':"TEXT DEFAULT ''",
      'family_id':'TEXT','human_verified':'INTEGER DEFAULT 0','verified_primary':'TEXT','verified_secondary':"TEXT DEFAULT '[]'",
      'verified_at':'TEXT','mtime_ns':'INTEGER DEFAULT 0','scan_status':"TEXT DEFAULT 'complete'",'user_favorite':'INTEGER DEFAULT 0','user_tags':"TEXT DEFAULT '[]'"
    }
    existing=cols(conn,'indicators')
    for name,decl in additions.items():
        if name not in existing: conn.execute(f'ALTER TABLE indicators ADD COLUMN {name} {decl}')
    conn.execute("INSERT INTO meta(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(SCHEMA_VERSION),))
    conn.commit()

def discover(sources: Iterable[Path]):
    found={}
    for root in sources:
        if root.is_file() and root.suffix.lower() in ('.mq4','.mq5'): found[str(root.resolve()).lower()]=root
        elif root.is_dir():
            for p in root.rglob('*'):
                if p.is_file() and p.suffix.lower() in ('.mq4','.mq5'): found[str(p.resolve()).lower()]=p
    return sorted(found.values(),key=lambda p:str(p).lower())

def save_analysis(conn,a:Analysis,mtime_ns:int):
    d=asdict(a)
    for k in JSON_FIELDS:
        if k in d: d[k]=json.dumps(d[k],ensure_ascii=False)
    d['object_usage']=1 if d['object_usage'] else 0; d['mtime_ns']=mtime_ns; d['scan_status']='complete'
    cols_=list(d.keys())
    sql=f"INSERT INTO indicators({','.join(cols_)}) VALUES({','.join('?' for _ in cols_)}) ON CONFLICT(path) DO UPDATE SET "+','.join(f"{c}=excluded.{c}" for c in cols_ if c!='path')+", analyzed_at=CURRENT_TIMESTAMP"
    conn.execute(sql,[d[c] for c in cols_])

def unchanged(conn,p):
    st=p.stat(); row=conn.execute('SELECT size,mtime_ns,scan_status,classifier_version FROM indicators WHERE path=?',(str(p.resolve()),)).fetchone()
    return bool(row and row['scan_status']=='complete' and row['size']==st.st_size and row['mtime_ns']==st.st_mtime_ns and row['classifier_version']=='evidence-v3')

def scan(db,sources,force=False):
    conn=connect(db)
    for src in sources: conn.execute('INSERT INTO sources(path,enabled) VALUES(?,1) ON CONFLICT(path) DO UPDATE SET enabled=1',(str(src.resolve()),))
    conn.commit(); files=discover(sources); total=len(files); emit('scan_start',total=total,sources=[str(x.resolve()) for x in sources])
    processed=skipped=failed=0; started=time.time()
    sha_first={r['sha256']:r['path'] for r in conn.execute('SELECT sha256,path FROM indicators WHERE sha256 IS NOT NULL AND duplicate_of IS NULL')}
    for idx,p in enumerate(files,1):
        try:
            if not force and unchanged(conn,p):
                skipped+=1
                if idx==1 or idx%25==0 or idx==total: emit('progress',current=idx,total=total,processed=processed,skipped=skipped,failed=failed,filename=p.name)
                continue
            a=analyze(p); a.duplicate_of=sha_first.get(a.sha256)
            if a.sha256 not in sha_first: sha_first[a.sha256]=a.path
            save_analysis(conn,a,p.stat().st_mtime_ns); processed+=1
            if processed%50==0: conn.commit()
            emit('item',current=idx,total=total,processed=processed,skipped=skipped,failed=failed,filename=p.name,category=a.primary_category,status=a.classification_status,confidence=a.confidence)
        except Exception as exc:
            failed+=1; emit('error',current=idx,total=total,processed=processed,skipped=skipped,failed=failed,filename=p.name,error=str(exc))
    conn.commit(); now=time.strftime('%Y-%m-%d %H:%M:%S')
    for src in sources: conn.execute('UPDATE sources SET last_scan_at=? WHERE path=?',(now,str(src.resolve())))
    conn.commit(); elapsed=round(time.time()-started,3)
    emit('scan_complete',total=total,processed=processed,skipped=skipped,failed=failed,elapsed_seconds=elapsed); conn.close()

def stats(db):
    conn=connect(db); one=lambda sql,args=(): conn.execute(sql,args).fetchone()[0]
    result={'total':one('SELECT COUNT(*) FROM indicators'),'mq4':one("SELECT COUNT(*) FROM indicators WHERE platform='MQL4'"),'mq5':one("SELECT COUNT(*) FROM indicators WHERE platform='MQL5'"),'review':one("SELECT COUNT(*) FROM indicators WHERE human_verified=0 AND classification_status IN ('Needs Review','Unknown')"),'duplicates':one('SELECT COUNT(*) FROM indicators WHERE duplicate_of IS NOT NULL'),'favorites':one('SELECT COUNT(*) FROM indicators WHERE user_favorite=1'),'verified':one('SELECT COUNT(*) FROM indicators WHERE human_verified=1'),'sources':[dict(r) for r in conn.execute('SELECT path,enabled,last_scan_at FROM sources ORDER BY path')]}
    print(json.dumps(result,ensure_ascii=False)); conn.close()

def main():
    ap=argparse.ArgumentParser(prog='mql-engine'); sub=ap.add_subparsers(dest='cmd',required=True)
    sp=sub.add_parser('scan'); sp.add_argument('--db',required=True); sp.add_argument('--source',action='append',required=True); sp.add_argument('--force',action='store_true')
    sp=sub.add_parser('stats'); sp.add_argument('--db',required=True)
    args=ap.parse_args(); db=Path(args.db)
    if args.cmd=='scan': scan(db,[Path(x) for x in args.source],args.force)
    elif args.cmd=='stats': stats(db)
if __name__=='__main__':
    try: main()
    except Exception as exc:
        emit('fatal',error=str(exc)); sys.exit(1)
