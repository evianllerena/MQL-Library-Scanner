"""Preview queue schema, claims and image cache in the library DB; maintenance commands. Split out of preview_bridge.py."""
from __future__ import annotations
import argparse, concurrent.futures, ctypes, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, time, zipfile
from pathlib import Path
from mql_text import decode_mql_bytes, read_log_text, read_mql_text
from mt_common import (emit)  # noqa: F401


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


def _cache_image_path(dest, source_hash):
    digest=str(source_hash or '').lower().strip()
    if not re.fullmatch(r'[0-9a-f]{64}',digest):
        digest=hashlib.sha256(digest.encode('utf-8','ignore')).hexdigest()
    shard=Path(dest)/digest[:2]
    shard.mkdir(parents=True,exist_ok=True)
    return shard/(digest+'.png')


def _compiled_cache_path(dest, kind, source_hash):
    digest=str(source_hash or '').lower().strip()
    if not re.fullmatch(r'[0-9a-f]{64}',digest):
        digest=hashlib.sha256(digest.encode('utf-8','ignore')).hexdigest()
    ext='.ex5' if kind=='MT5' else '.ex4'
    root=Path(dest).parent/'preview-runtime'/'compiled-cache'/kind.lower()/digest[:2]
    root.mkdir(parents=True,exist_ok=True)
    return root/(digest+ext)


def _store_preview_image(source_image, dest, source_hash):
    source_image=Path(source_image); final=_cache_image_path(dest,source_hash)
    try:
        from PIL import Image
        with Image.open(source_image) as im:
            im.convert('RGB').save(final,format='PNG',optimize=True)
    except Exception:
        if source_image.suffix.lower()=='.png':shutil.copy2(source_image,final)
        else:raise
    return final


def _thumb_path(image):
    image=Path(image)
    return image.with_name(image.stem+'.thumb.png')


def _make_thumb(image):
    image=Path(image)
    thumb=_thumb_path(image)
    if thumb.exists() and thumb.stat().st_size:return thumb
    try:
        from PIL import Image
        with Image.open(image) as im:
            im=im.convert('RGB')
            width=300
            height=max(1,round(im.height*(width/im.width)))
            im.thumbnail((width,height))
            thumb.parent.mkdir(parents=True,exist_ok=True)
            im.save(thumb,format='PNG',optimize=True)
        return thumb
    except Exception:
        return None


def _worker_pause_reason(con,dest):
    try:
        row=con.execute("SELECT value FROM meta WHERE key='preview_paused'").fetchone()
        if row and str(row[0])=='1':return 'paused_by_user'
    except Exception:pass
    try:
        free=shutil.disk_usage(Path(dest).parent).free
        if free<2*1024*1024*1024:return 'low_disk'
    except Exception:pass
    if os.name=='nt':
        try:
            class SYSTEM_POWER_STATUS(ctypes.Structure):
                _fields_=[('ACLineStatus',ctypes.c_ubyte),('BatteryFlag',ctypes.c_ubyte),('BatteryLifePercent',ctypes.c_ubyte),('SystemStatusFlag',ctypes.c_ubyte),('BatteryLifeTime',ctypes.c_ulong),('BatteryFullLifeTime',ctypes.c_ulong)]
            s=SYSTEM_POWER_STATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)) and s.ACLineStatus==0 and s.BatteryLifePercent!=255 and s.BatteryLifePercent<=20:
                return 'low_battery'
        except Exception:pass
    return None


def _ensure_preview_queue_schema(con,worker_id=None):
    cols={r[1] for r in con.execute('PRAGMA table_info(indicators)')}
    additions={
        'preview_status':"TEXT DEFAULT 'pending'",
        'preview_path':'TEXT','preview_hash':'TEXT',
        'preview_error':"TEXT DEFAULT ''",'preview_updated_at':'TEXT',
        'preview_attempts':'INTEGER DEFAULT 0','preview_priority':'INTEGER DEFAULT 0','worker_id':'TEXT'
    }
    for name,decl in additions.items():
        if name not in cols:con.execute(f'ALTER TABLE indicators ADD COLUMN {name} {decl}')
    if worker_id:
        con.execute("UPDATE indicators SET preview_status='pending',worker_id=NULL WHERE preview_status='rendering' AND (worker_id=? OR worker_id IS NULL)",(worker_id,))
    else:
        con.execute("UPDATE indicators SET preview_status='pending',worker_id=NULL WHERE preview_status='rendering' AND worker_id IS NULL")
    con.execute("UPDATE indicators SET preview_status='pending',preview_attempts=0,preview_error='',worker_id=NULL "
                "WHERE preview_status='ready' AND COALESCE(preview_hash,'')!=COALESCE(sha256,'')")
    con.commit()


def _propagate_existing_ready(con):
    try:
        ready=con.execute(
            "SELECT sha256,preview_path,preview_hash,preview_updated_at FROM indicators "
            "WHERE preview_status='ready' AND COALESCE(sha256,'')<>'' GROUP BY sha256"
        ).fetchall()
        for r in ready:
            con.execute(
                "UPDATE indicators SET preview_status='ready',preview_path=?,preview_hash=?,preview_error='',"
                "preview_updated_at=?,worker_id=NULL WHERE sha256=? AND preview_status!='ready'",
                (r['preview_path'],r['preview_hash'],r['preview_updated_at'],r['sha256']))
        con.commit()
    except Exception:pass
