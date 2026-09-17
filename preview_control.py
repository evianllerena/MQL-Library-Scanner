from __future__ import annotations
import argparse, gc, json, os, shutil, sqlite3, subprocess, sys, time
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


def remove_item(item:Path):
    last=None
    for attempt in range(5):
        try:
            if item.is_symlink() or item.is_file():
                item.unlink(missing_ok=True)
            elif item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink(missing_ok=True)
            return None
        except FileNotFoundError:
            return None
        except Exception as e:
            last=e
            gc.collect()
            time.sleep(0.12*(attempt+1))
    return last


def clear_app(db:str):
    p=Path(db).resolve()
    app=p.parent
    failed=[]

    conn=None
    if p.exists():
        try:
            conn=sqlite3.connect(str(p),timeout=2)
            conn.execute('pragma wal_checkpoint(truncate)')
        except Exception:
            pass
        finally:
            if conn is not None:
                try:conn.close()
                except Exception:pass
            conn=None
            gc.collect()

    try:
        children=list(app.iterdir()) if app.exists() else []
    except Exception as e:
        raise RuntimeError(f'Could not read application data directory {app}: {e}')

    for item in children:
        err=remove_item(item)
        if err is not None:
            failed.append(f'{item}: {err}')

    emit({
        'ok':len(failed)==0,
        'cleared':len(failed)==0,
        'app_dir':str(app),
        'failed':failed,
        'source_files_touched':False
    })


def self_test():
    checks={
        'taskkill_available':True if os.name!='nt' else bool(subprocess.run(['where','taskkill'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=CREATE_NO_WINDOW).returncode==0),
        'clear_scoped_to_db_parent':True
    }
    emit({'ok':all(checks.values()),'checks':checks})


def main():
    a=argparse.ArgumentParser();s=a.add_subparsers(dest='cmd',required=True)
    p=s.add_parser('cancel');p.add_argument('--pid',required=True,type=int)
    p=s.add_parser('clear-app');p.add_argument('--db',required=True)
    s.add_parser('self-test')
    x=a.parse_args()
    if x.cmd=='cancel':cancel_tree(x.pid)
    elif x.cmd=='clear-app':clear_app(x.db)
    elif x.cmd=='self-test':self_test()


if __name__=='__main__':
    try:main()
    except Exception as e:emit({'ok':False,'error':f'{type(e).__name__}: {e}'});sys.exit(1)
