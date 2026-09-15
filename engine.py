from __future__ import annotations
import argparse, json, sqlite3, sys, time
from pathlib import Path
from typing import Iterable

from engine_core import Analysis, analyze

SCHEMA_VERSION = 2

def emit(kind: str, **payload):
    print(json.dumps({"type": kind, **payload}, ensure_ascii=False), flush=True)

def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    migrate(conn)
    return conn

def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

def migrate(conn: sqlite3.Connection):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sources(
      path TEXT PRIMARY KEY,
      enabled INTEGER NOT NULL DEFAULT 1,
      added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      last_scan_at TEXT
    );
    CREATE TABLE IF NOT EXISTS indicators(
      id INTEGER PRIMARY KEY,
      path TEXT UNIQUE, filename TEXT, platform TEXT, sha256 TEXT, size INTEGER,
      source_structure TEXT, display_location TEXT, declared_buffers INTEGER, declared_plots INTEGER,
      active_buffers INTEGER, draw_types TEXT, line_plots INTEGER, histogram_plots INTEGER, arrow_plots INTEGER,
      filling_plots INTEGER, object_usage INTEGER, standard_indicators TEXT, custom_dependencies TEXT,
      primary_category TEXT, secondary_categories TEXT, visual_category TEXT, behavior_tags TEXT,
      confidence INTEGER, warnings TEXT, duplicate_of TEXT, analyzed_at TEXT DEFAULT CURRENT_TIMESTAMP,
      mtime_ns INTEGER DEFAULT 0, scan_status TEXT DEFAULT 'complete', user_favorite INTEGER DEFAULT 0,
      user_tags TEXT DEFAULT '[]'
    );
    CREATE INDEX IF NOT EXISTS idx_sha ON indicators(sha256);
    CREATE INDEX IF NOT EXISTS idx_primary ON indicators(primary_category);
    CREATE INDEX IF NOT EXISTS idx_visual ON indicators(visual_category);
    CREATE INDEX IF NOT EXISTS idx_platform ON indicators(platform);
    CREATE INDEX IF NOT EXISTS idx_filename ON indicators(filename);
    CREATE INDEX IF NOT EXISTS idx_mtime_size ON indicators(mtime_ns, size);
    ''')
    cols = table_columns(conn, 'indicators')
    additions = {
        'mtime_ns': "INTEGER DEFAULT 0",
        'scan_status': "TEXT DEFAULT 'complete'",
        'user_favorite': "INTEGER DEFAULT 0",
        'user_tags': "TEXT DEFAULT '[]'",
    }
    for name, decl in additions.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE indicators ADD COLUMN {name} {decl}")
    conn.execute("INSERT INTO meta(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),))
    conn.commit()

def discover(sources: Iterable[Path]) -> list[Path]:
    found: dict[str, Path] = {}
    for root in sources:
        if root.is_file() and root.suffix.lower() in ('.mq4','.mq5'):
            found[str(root.resolve()).lower()] = root
        elif root.is_dir():
            for p in root.rglob('*'):
                if p.is_file() and p.suffix.lower() in ('.mq4','.mq5'):
                    found[str(p.resolve()).lower()] = p
    return sorted(found.values(), key=lambda p: str(p).lower())

def serialize_list(value):
    return json.dumps(value, ensure_ascii=False)

def save_analysis(conn: sqlite3.Connection, a: Analysis, mtime_ns: int):
    d = a.__dict__.copy()
    for k in ['draw_types','standard_indicators','custom_dependencies','secondary_categories','behavior_tags','warnings']:
        d[k] = serialize_list(d[k])
    d['object_usage'] = 1 if d['object_usage'] else 0
    d['mtime_ns'] = mtime_ns
    d['scan_status'] = 'complete'
    cols = list(d.keys())
    sql = f"INSERT INTO indicators({','.join(cols)}) VALUES({','.join('?' for _ in cols)}) ON CONFLICT(path) DO UPDATE SET " + ','.join(f"{c}=excluded.{c}" for c in cols if c != 'path') + ", analyzed_at=CURRENT_TIMESTAMP"
    conn.execute(sql, [d[c] for c in cols])

def is_unchanged(conn: sqlite3.Connection, path: Path) -> bool:
    st = path.stat()
    row = conn.execute("SELECT size,mtime_ns,scan_status FROM indicators WHERE path=?", (str(path.resolve()),)).fetchone()
    return bool(row and row['scan_status'] == 'complete' and row['size'] == st.st_size and row['mtime_ns'] == st.st_mtime_ns)

def scan(db: Path, sources: list[Path], force: bool = False):
    conn = connect(db)
    for src in sources:
        conn.execute("INSERT INTO sources(path,enabled) VALUES(?,1) ON CONFLICT(path) DO UPDATE SET enabled=1", (str(src.resolve()),))
    conn.commit()
    files = discover(sources)
    total = len(files)
    emit('scan_start', total=total, sources=[str(x.resolve()) for x in sources])
    processed = skipped = failed = 0
    started = time.time()
    sha_first = {r['sha256']: r['path'] for r in conn.execute("SELECT sha256,path FROM indicators WHERE sha256 IS NOT NULL AND duplicate_of IS NULL")}
    for idx, p in enumerate(files, 1):
        try:
            if not force and is_unchanged(conn, p):
                skipped += 1
                if idx == 1 or idx % 25 == 0 or idx == total:
                    emit('progress', current=idx, total=total, processed=processed, skipped=skipped, failed=failed, filename=p.name)
                continue
            a = analyze(p)
            a.duplicate_of = sha_first.get(a.sha256)
            if a.sha256 not in sha_first:
                sha_first[a.sha256] = a.path
            save_analysis(conn, a, p.stat().st_mtime_ns)
            processed += 1
            if processed % 25 == 0:
                conn.commit()
            emit('item', current=idx, total=total, filename=p.name, platform=a.platform, category=a.primary_category, visual=a.visual_category, confidence=a.confidence)
        except Exception as exc:
            failed += 1
            emit('error', current=idx, total=total, filename=p.name, error=str(exc))
    conn.commit()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    for src in sources:
        conn.execute("UPDATE sources SET last_scan_at=? WHERE path=?", (now, str(src.resolve())))
    conn.commit()
    elapsed = round(time.time() - started, 3)
    emit('scan_complete', total=total, processed=processed, skipped=skipped, failed=failed, elapsed_seconds=elapsed)
    conn.close()

def stats(db: Path):
    conn = connect(db)
    def one(sql, args=()):
        return conn.execute(sql,args).fetchone()[0]
    result = {
        'total': one('SELECT COUNT(*) FROM indicators'),
        'mq4': one("SELECT COUNT(*) FROM indicators WHERE platform='MQL4'"),
        'mq5': one("SELECT COUNT(*) FROM indicators WHERE platform='MQL5'"),
        'review': one("SELECT COUNT(*) FROM indicators WHERE confidence < 70 OR primary_category='Unknown'"),
        'duplicates': one('SELECT COUNT(*) FROM indicators WHERE duplicate_of IS NOT NULL'),
        'favorites': one('SELECT COUNT(*) FROM indicators WHERE user_favorite=1'),
        'sources': [dict(r) for r in conn.execute('SELECT path,enabled,last_scan_at FROM sources ORDER BY path')]
    }
    print(json.dumps(result, ensure_ascii=False))
    conn.close()

def query(db: Path, search: str = '', platform: str = '', category: str = '', review_only: bool = False, limit: int = 500):
    conn = connect(db)
    clauses=[]; args=[]
    if search:
        clauses.append("(filename LIKE ? OR primary_category LIKE ? OR secondary_categories LIKE ? OR behavior_tags LIKE ? OR standard_indicators LIKE ?)")
        like=f"%{search}%"; args += [like]*5
    if platform and platform != 'ALL':
        clauses.append('platform=?'); args.append(platform)
    if category and category != 'ALL':
        clauses.append('primary_category=?'); args.append(category)
    if review_only:
        clauses.append("(confidence < 70 OR primary_category='Unknown')")
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    sql = f'''SELECT id,path,filename,platform,source_structure,display_location,primary_category,
                    secondary_categories,visual_category,behavior_tags,standard_indicators,custom_dependencies,
                    confidence,warnings,duplicate_of,user_favorite,user_tags,analyzed_at
             FROM indicators{where}
             ORDER BY user_favorite DESC, confidence DESC, filename COLLATE NOCASE ASC LIMIT ?'''
    args.append(max(1,min(limit,5000)))
    rows=[dict(r) for r in conn.execute(sql,args)]
    for row in rows:
        for k in ['secondary_categories','behavior_tags','standard_indicators','custom_dependencies','warnings','user_tags']:
            try: row[k]=json.loads(row[k] or '[]')
            except Exception: row[k]=[]
        row['user_favorite']=bool(row['user_favorite'])
    print(json.dumps(rows, ensure_ascii=False))
    conn.close()

def set_favorite(db: Path, indicator_id: int, value: bool):
    conn=connect(db)
    conn.execute('UPDATE indicators SET user_favorite=? WHERE id=?',(1 if value else 0, indicator_id)); conn.commit(); conn.close()
    print(json.dumps({'ok':True,'id':indicator_id,'favorite':value}))

def sources(db: Path):
    conn=connect(db)
    print(json.dumps([dict(r) for r in conn.execute('SELECT path,enabled,last_scan_at FROM sources ORDER BY path')], ensure_ascii=False))
    conn.close()

def main():
    ap=argparse.ArgumentParser(prog='mql-engine')
    sub=ap.add_subparsers(dest='cmd', required=True)
    sp=sub.add_parser('scan'); sp.add_argument('--db',required=True); sp.add_argument('--source',action='append',required=True); sp.add_argument('--force',action='store_true')
    sp=sub.add_parser('stats'); sp.add_argument('--db',required=True)
    sp=sub.add_parser('query'); sp.add_argument('--db',required=True); sp.add_argument('--search',default=''); sp.add_argument('--platform',default='ALL'); sp.add_argument('--category',default='ALL'); sp.add_argument('--review-only',action='store_true'); sp.add_argument('--limit',type=int,default=500)
    sp=sub.add_parser('favorite'); sp.add_argument('--db',required=True); sp.add_argument('--id',required=True,type=int); sp.add_argument('--value',required=True,choices=['0','1'])
    sp=sub.add_parser('sources'); sp.add_argument('--db',required=True)
    args=ap.parse_args()
    db=Path(args.db)
    if args.cmd=='scan': scan(db,[Path(x) for x in args.source],args.force)
    elif args.cmd=='stats': stats(db)
    elif args.cmd=='query': query(db,args.search,args.platform,args.category,args.review_only,args.limit)
    elif args.cmd=='favorite': set_favorite(db,args.id,args.value=='1')
    elif args.cmd=='sources': sources(db)

if __name__=='__main__':
    try: main()
    except Exception as exc:
        emit('fatal', error=str(exc))
        sys.exit(1)
