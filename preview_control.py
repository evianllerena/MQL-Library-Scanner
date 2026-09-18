from __future__ import annotations
import argparse, gc, json, os, re, shutil, sqlite3, subprocess, sys, time
from pathlib import Path

CREATE_NO_WINDOW=getattr(subprocess,'CREATE_NO_WINDOW',0)


def emit(obj):
    data=(json.dumps(obj,ensure_ascii=False,default=str)+'\n').encode('utf-8','backslashreplace')
    sys.stdout.buffer.write(data);sys.stdout.buffer.flush()


def pid_exists(pid:int)->bool:
    if pid<=0:
        return False
    if os.name=='nt':
        cp=subprocess.run(
            ['tasklist','/FI',f'PID eq {pid}','/FO','CSV','/NH'],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
            creationflags=CREATE_NO_WINDOW
        )
        text=(cp.stdout or '').strip()
        return cp.returncode==0 and re.search(rf',"{pid}",',text) is not None
    try:
        os.kill(pid,0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def cancel_tree_result(pid:int):
    started=time.monotonic()
    if pid<=0:
        return {'ok':False,'cancelled':False,'pid':pid,'reason':'invalid pid','verified_gone':False}

    if not pid_exists(pid):
        return {
            'ok':True,'cancelled':True,'pid':pid,'already_exited':True,
            'verified_gone':True,'elapsed_ms':round((time.monotonic()-started)*1000)
        }

    if os.name=='nt':
        cp=subprocess.run(
            ['taskkill','/PID',str(pid),'/T','/F'],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
            creationflags=CREATE_NO_WINDOW
        )
        output=(cp.stdout or cp.stderr or '').strip()[-1600:]
        gone=False
        for _ in range(60):
            if not pid_exists(pid):
                gone=True
                break
            time.sleep(.05)
        return {
            'ok':gone,
            'cancelled':gone,
            'pid':pid,
            'returncode':cp.returncode,
            'verified_gone':gone,
            'output':output,
            'elapsed_ms':round((time.monotonic()-started)*1000)
        }

    try:
        os.kill(pid,15)
    except ProcessLookupError:
        return {'ok':True,'cancelled':True,'pid':pid,'already_exited':True,'verified_gone':True}
    except Exception as e:
        return {'ok':False,'cancelled':False,'pid':pid,'verified_gone':False,'error':str(e)}

    gone=False
    for _ in range(60):
        if not pid_exists(pid):
            gone=True
            break
        time.sleep(.05)
    if not gone:
        try:os.kill(pid,9)
        except Exception:pass
        time.sleep(.1)
        gone=not pid_exists(pid)
    return {
        'ok':gone,'cancelled':gone,'pid':pid,'verified_gone':gone,
        'elapsed_ms':round((time.monotonic()-started)*1000)
    }


def cancel_tree(pid:int):
    emit(cancel_tree_result(pid))


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
        'taskkill_available':True if os.name!='nt' else bool(subprocess.run(
            ['where','taskkill'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW
        ).returncode==0),
        'clear_scoped_to_db_parent':True,
        'cancel_verifies_process_exit':False
    }
    proc=None
    try:
        if os.name=='nt':
            proc=subprocess.Popen(
                ['cmd','/c','ping','127.0.0.1','-n','30'],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW
            )
        else:
            proc=subprocess.Popen(['sleep','30'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        time.sleep(.2)
        result=cancel_tree_result(proc.pid)
        checks['cancel_verifies_process_exit']=bool(
            result.get('ok') and result.get('cancelled') and result.get('verified_gone')
            and not pid_exists(proc.pid)
        )
    finally:
        if proc is not None and pid_exists(proc.pid):
            try:proc.kill()
            except Exception:pass

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
