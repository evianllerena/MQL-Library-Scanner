from __future__ import annotations
import argparse, json, os, sqlite3, subprocess, sys, time, zipfile
from pathlib import Path

CREATE_NO_WINDOW=getattr(subprocess,'CREATE_NO_WINDOW',0)


def emit(obj):
    data=(json.dumps(obj,ensure_ascii=False,default=str)+'\n').encode('utf-8','backslashreplace')
    sys.stdout.buffer.write(data);sys.stdout.buffer.flush()


def cancel_tree(pid:int):
    if pid<=0: return emit({'ok':True,'cancelled':False,'reason':'invalid pid'})
    if os.name=='nt':
        cp=subprocess.run(['taskkill','/PID',str(pid),'/T','/F'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,creationflags=CREATE_NO_WINDOW)
        emit({'ok':True,'cancelled':cp.returncode==0,'pid':pid,'returncode':cp.returncode,'output':(cp.stdout or cp.stderr or '').strip()[-1200:]})
        return
    try:
        os.kill(pid,15);emit({'ok':True,'cancelled':True,'pid':pid})
    except ProcessLookupError:
        emit({'ok':True,'cancelled':False,'pid':pid,'reason':'not running'})


def archive_backup(db:str):
    p=Path(db);app=p.parent
    out=Path(os.environ.get('USERPROFILE',str(app)))/'Downloads'
    if not out.exists():out=app
    out.mkdir(parents=True,exist_ok=True)
    if p.exists():
        try:
            c=sqlite3.connect(p);c.execute('pragma wal_checkpoint(truncate)');c.close()
        except Exception:pass
    stamp=time.strftime('%Y%m%d_%H%M%S')
    zpath=out/f'MQL_Indicator_Library_Backup_{stamp}.zip'
    excluded_roots={'preview-runtime'}
    added=0
    with zipfile.ZipFile(zpath,'w',zipfile.ZIP_DEFLATED) as z:
        for f in app.rglob('*'):
            if not f.is_file():continue
            try:rel=f.relative_to(app)
            except Exception:continue
            if rel.parts and rel.parts[0].lower() in excluded_roots:continue
            if f.resolve()==zpath.resolve():continue
            try:z.write(f,rel);added+=1
            except Exception:pass
    emit({'ok':True,'archive':str(zpath),'files':added,'app_dir':str(app),'excluded':['preview-runtime']})


def self_test():
    emit({'ok':True,'checks':{'taskkill_available':True if os.name!='nt' else bool(subprocess.run(['where','taskkill'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=CREATE_NO_WINDOW).returncode==0)}})


def main():
    a=argparse.ArgumentParser();s=a.add_subparsers(dest='cmd',required=True)
    p=s.add_parser('cancel');p.add_argument('--pid',required=True,type=int)
    p=s.add_parser('archive');p.add_argument('--db',required=True)
    s.add_parser('self-test')
    x=a.parse_args()
    if x.cmd=='cancel':cancel_tree(x.pid)
    elif x.cmd=='archive':archive_backup(x.db)
    elif x.cmd=='self-test':self_test()


if __name__=='__main__':
    try:main()
    except Exception as e:emit({'ok':False,'error':f'{type(e).__name__}: {e}'});sys.exit(1)
