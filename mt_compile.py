"""MetaEditor compilation and compiled-binary lookup. Split out of preview_bridge.py."""
from __future__ import annotations
import argparse, concurrent.futures, ctypes, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, time, zipfile
from pathlib import Path
from mql_text import decode_mql_bytes, read_log_text, read_mql_text
from mt_common import (CREATE_NO_WINDOW)  # noqa: F401


def compile_has_errors(text):
    for m in re.finditer(r'(?i)(?:result\s*:\s*)?(\d+)\s+errors?\b',text or ''):
        try:
            if int(m.group(1))>0:return True
        except Exception:pass
    return False

def safe_name(src):
    s=re.sub(r'\s+','_',src.stem.strip()); s=re.sub(r'[^A-Za-z0-9_.()#-]+','_',s).strip(' ._') or 'indicator'; return s[:100]+src.suffix.lower()

def compile_file(editor,src,mqlroot):
    expected=src.with_suffix('.ex5' if src.suffix.lower()=='.mq5' else '.ex4'); log=src.with_suffix('.log'); attempts=[]
    for rel in (False,True):
        try:log.unlink(missing_ok=True)
        except Exception:pass
        try:expected.unlink(missing_ok=True)
        except Exception:pass
        target=src.name if rel else str(src); cmd=[str(editor),f'/compile:{target}',f'/inc:{mqlroot}','/log']; attempts.append(' '.join(cmd))
        subprocess.run(cmd,cwd=str(src.parent if rel else editor.parent),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60,creationflags=CREATE_NO_WINDOW)
        end=time.monotonic()+3.0
        latest=''
        while time.monotonic()<end:
            if expected.exists() and expected.stat().st_size:return expected,attempts,read_log_text(log)
            latest=read_log_text(log)
            if compile_has_errors(latest):return None,attempts,latest
            time.sleep(.15)
        latest=read_log_text(log)
        if compile_has_errors(latest):return None,attempts,latest
    return None,attempts,read_log_text(log)

def existing_binary(src,ext):
    if src.suffix.lower()==ext and src.exists():return src
    p=src.with_suffix(ext)
    if p.exists() and p.stat().st_size:return p
    return None

def find_live_binary(src,ext,data_dir):
    """Find an already-compiled indicator binary (.ex4/.ex5) in the live terminal's
    Indicators tree, matched by the source file's name. Lets the preview reuse the
    working binary the terminal already runs when the source won't compile in isolation."""
    if not data_dir:return None
    try:base=Path(data_dir)/('MQL5' if ext=='.ex5' else 'MQL4')/'Indicators'
    except Exception:return None
    if not base.is_dir():return None
    name=src.stem+ext
    direct=base/name
    if direct.is_file() and direct.stat().st_size:return direct
    try:
        for cand in base.rglob(name):
            if cand.is_file() and cand.stat().st_size:return cand
    except Exception:pass
    return None

def copy_compiled_support(src,dst,kind):
    dst.mkdir(parents=True,exist_ok=True)
    if not src.exists():return
    ext='.ex5' if kind=='MT5' else '.ex4'
    for p in src.rglob('*'):
        if not p.is_file():continue
        if p.suffix.lower() not in {ext,'.mqh'}:continue
        try:
            target=dst/p.relative_to(src)
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(p,target)
        except Exception:pass
