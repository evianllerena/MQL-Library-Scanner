"""Shared preview-bridge plumbing: JSON events, process control, runtime lock, small helpers. Split out of preview_bridge.py."""
from __future__ import annotations
import argparse, concurrent.futures, ctypes, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, time, zipfile
from pathlib import Path
from mql_text import decode_mql_bytes, read_log_text, read_mql_text


CREATE_NO_WINDOW=getattr(subprocess,'CREATE_NO_WINDOW',0)
SW_HIDE=0

RENDER_STARTUP_EXIT=42

class RenderStartupError(RuntimeError):
    pass

def mql_dir_name(kind):
    """MetaTrader source directory name for a platform kind.

    A MetaTrader data folder contains an ``MQL5`` (MT5) or ``MQL4`` (MT4)
    subdirectory — never ``MT5``/``MT4``. The platform *kind* string must be
    mapped through here anywhere the on-disk MQL directory is referenced.
    """
    return 'MQL5' if kind=='MT5' else 'MQL4'

def safe_job_id(value):
    value=re.sub(r'[^A-Za-z0-9_.-]+','-',str(value or '').strip()).strip('.-')
    return (value or hashlib.sha1(str(time.time_ns()).encode()).hexdigest()[:12])[:64]

class RenderLock:
    def __init__(self,path,timeout=5.0):
        self.path=Path(path);self.timeout=timeout;self.file=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.file=open(self.path,'a+b')
        self.file.seek(0,2)
        if self.file.tell()==0:self.file.write(b'0');self.file.flush()
        end=time.monotonic()+self.timeout
        while True:
            try:
                self.file.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                return self
            except (OSError,IOError):
                if time.monotonic()>=end:
                    self.file.close();self.file=None
                    raise RuntimeError('Preview engine busy — another MetaTrader preview is still active.')
                time.sleep(.1)
    def __exit__(self,_typ,_val,_tb):
        if self.file is None:return
        try:
            self.file.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_UN)
        except Exception:pass
        try:self.file.close()
        except Exception:pass
        self.file=None

def terminate_tree(proc):
    if proc is None:return
    try:
        if proc.poll() is not None:return
    except Exception:return
    if os.name=='nt':
        try:
            subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=CREATE_NO_WINDOW,timeout=8)
        except Exception:pass
    else:
        try:proc.terminate()
        except Exception:pass
        try:proc.wait(timeout=5)
        except Exception:
            try:proc.kill()
            except Exception:pass

def hard_kill(proc,runtime_dir):
    """Force-reap the isolated MetaTrader tree without touching the user's live terminal."""
    pid=getattr(proc,'pid',None)
    try:terminate_tree(proc)
    except Exception:pass
    if os.name=='nt' and pid:
        try:
            subprocess.run(['taskkill','/F','/T','/PID',str(pid)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=CREATE_NO_WINDOW,timeout=8)
        except Exception:pass
    try:
        import psutil
        rd=str(Path(runtime_dir).resolve()).lower().rstrip('\\/')+os.sep
        names={'terminal64.exe','terminal.exe','metaeditor64.exe','metaeditor.exe'}
        for p in psutil.process_iter(['pid','name','exe']):
            try:
                exe=str(p.info.get('exe') or '').lower()
                name=str(p.info.get('name') or '').lower()
                if exe and exe.startswith(rd) and name in names:
                    p.kill()
            except Exception:pass
    except Exception:pass


def emit_heartbeat(job_id,done=0,inflight=None,**details):
    payload={'type':'heartbeat','job_id':job_id,'ts':time.time(),'done':int(done or 0),'inflight':str(inflight) if inflight else None}
    payload.update(details);emit(payload)

def emit(o):
    b=(json.dumps(o,ensure_ascii=False,default=str)+'\n').encode('utf-8','backslashreplace')
    sys.stdout.buffer.write(b); sys.stdout.buffer.flush()

def emit_stage(job_id,name,message=None,**details):
    payload={'type':'stage','job_id':job_id,'stage':name}
    if message:payload['message']=message
    payload.update(details)
    emit(payload)

def read_text(p):
    # BOM/NUL-pattern sniffing via the shared reader (same text engine_core sees). The old
    # trial loop let utf-8+errors='ignore' win before cp1252, dropping legacy characters.
    return read_mql_text(p)

def _same_source(path,expected):
    # write_text() on Windows writes CRLF, so compare with normalized newlines; otherwise a
    # freshly written EA/script never matches its own source and is rewritten+recompiled.
    norm=lambda t:t.replace('\r\n','\n')
    return norm(read_text(Path(path)))==norm(expected)

def same_path(a,b):
    if not a or not b:return False
    try:return Path(a).resolve()==Path(b).resolve()
    except Exception:return os.path.normcase(os.path.abspath(str(a)))==os.path.normcase(os.path.abspath(str(b)))

def startupinfo():
    if os.name!='nt':return None
    si=subprocess.STARTUPINFO();si.dwFlags|=getattr(subprocess,'STARTF_USESHOWWINDOW',1);si.wShowWindow=SW_HIDE;return si

def hide_pid(pid):
    if os.name!='nt':return
    u=ctypes.windll.user32
    @ctypes.WINFUNCTYPE(ctypes.c_bool,ctypes.c_void_p,ctypes.c_void_p)
    def cb(hwnd,_):
        p=ctypes.c_ulong();u.GetWindowThreadProcessId(hwnd,ctypes.byref(p))
        if p.value==pid and u.IsWindowVisible(hwnd):u.ShowWindow(hwnd,SW_HIDE)
        return True
    try:u.EnumWindows(cb,0)
    except Exception:pass

def latest_logs(root):
    rows=[]
    if root.exists():
        for p in root.rglob('*.log'):
            try:rows.append((p.stat().st_mtime_ns,p))
            except Exception:pass
    return '\n\n'.join(f'[{p}]\n{read_log_text(p)[-2200:]}' for _,p in sorted(rows,reverse=True)[:4])

class PreviewRuntimeError(RuntimeError):
    def __init__(self,message,diagnostics=None):
        super().__init__(message)
        self.diagnostics=diagnostics or []
