"""MetaTrader terminal / data-folder discovery and runtime selection. Split out of preview_bridge.py."""
from __future__ import annotations
import argparse, concurrent.futures, ctypes, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, time, zipfile
from pathlib import Path
from mql_text import decode_mql_bytes, read_log_text, read_mql_text
from mt_common import (PreviewRuntimeError, emit, mql_dir_name, same_path)  # noqa: F401


def decode_origin_bytes(data):
    encodings=('utf-16','utf-8-sig','cp1252','latin1') if data[:2] in (b'\xff\xfe',b'\xfe\xff') else ('utf-8-sig','cp1252','latin1','utf-16')
    for enc in encodings:
        try:
            text=data.decode(enc).replace('\x00','').strip().strip('"')
            if text and (':\\' in text or ':/' in text):
                return text
        except Exception:pass
    return ''

def read_origin(p):
    if not p.exists():return ''
    try:return decode_origin_bytes(p.read_bytes())
    except Exception:return ''

def data_activity(path):
    root=Path(path)
    newest=0
    for candidate in (root/'logs',root/'MQL4'/'Logs',root/'MQL5'/'Logs'):
        if not candidate.exists():continue
        try:
            for p in candidate.rglob('*.log'):
                try:newest=max(newest,p.stat().st_mtime_ns)
                except Exception:pass
        except Exception:pass
    return newest

def metatrader_data_root():
    return Path(os.environ.get('APPDATA',''))/'MetaQuotes'/'Terminal'


def _tree_has_file(root):
    root=Path(root)
    if not root.exists():return False
    try:
        for item in root.rglob('*'):
            if item.is_file():return True
    except Exception:pass
    return False

def _render_data_activity(root):
    root=Path(root); newest=0
    for candidate in (
        root/'config'/'accounts.dat',
        root/'config'/'terminal.ini',
        root/'config'/'common.ini',
        root/'bases',
        root/'history'
    ):
        try:newest=max(newest,candidate.stat().st_mtime_ns)
        except Exception:pass
    return newest

def _valid_mt5_render_data_dir(root):
    root=Path(root)
    accounts=root/'config'/'accounts.dat'
    history_ok=_tree_has_file(root/'bases') or _tree_has_file(root/'history')
    return root.is_dir() and accounts.is_file() and accounts.stat().st_size>0 and history_ok

def resolve_mt5_render_data_dir(configured=None):
    if configured:
        root=Path(os.path.expandvars(os.path.expanduser(str(configured).strip().strip('"'))))
        if not _valid_mt5_render_data_dir(root):
            raise RuntimeError(
                f'MT5 data folder is not usable for rendering: {root}. '
                'Expected config/accounts.dat plus non-empty bases/ or history/.')
        return root
    base=metatrader_data_root()
    candidates=[]
    if base.exists():
        for root in base.iterdir():
            if not root.is_dir():continue
            try:
                if _valid_mt5_render_data_dir(root):
                    candidates.append((_render_data_activity(root),root))
            except Exception:pass
    if not candidates:
        raise RuntimeError(
            'No logged-in MT5 data folder was found. Set the MT5 data folder in Settings '
            '(the MetaQuotes\\Terminal\\<hash> folder containing config\\accounts.dat and history).')
    candidates.sort(key=lambda x:x[0],reverse=True)
    return candidates[0][1]

def observed_data_folders(base=None):
    base=Path(base) if base is not None else metatrader_data_root()
    rows=[]
    if not base.exists():return rows
    for d in base.iterdir():
        if not d.is_dir():continue
        rows.append({
            'data_dir':str(d),
            'origin':read_origin(d/'origin.txt'),
            'has_mql4':(d/'MQL4').is_dir(),
            'has_mql5':(d/'MQL5').is_dir(),
            'activity_ns':data_activity(d)
        })
    return rows

def discover_data_dirs(base=None):
    rows=[]
    for item in observed_data_folders(base):
        for kind,key in (('MT4','has_mql4'),('MT5','has_mql5')):
            if not item[key]:continue
            rows.append({
                'kind':kind,
                'data_dir':item['data_dir'],
                'origin':item['origin'],
                'activity_ns':item['activity_ns'],
                'discovery':'data-folder'
            })
    return rows

def find_editor(install,kind):
    install=Path(install)
    names=('metaeditor64.exe','metaeditor.exe') if kind=='MT5' else ('metaeditor.exe','metaeditor64.exe')
    for name in names:
        p=install/name
        if p.is_file():return str(p)
    try:
        candidates=[p for p in install.iterdir() if p.is_file() and p.name.lower().startswith('metaeditor') and p.suffix.lower()=='.exe']
    except Exception:candidates=[]
    return str(candidates[0]) if candidates else None

def candidate_install_dirs(base=None,roots=None):
    seen=set(); out=[]
    for item in observed_data_folders(base):
        origin=item.get('origin')
        if not origin:continue
        key=os.path.normcase(str(origin))
        if key in seen:continue
        seen.add(key);out.append(Path(origin))
    if roots is None:
        roots=[Path(os.environ[x]) for x in ('ProgramFiles','ProgramFiles(x86)','LOCALAPPDATA') if os.environ.get(x) and Path(os.environ[x]).exists()]
    for root in roots:
        root=Path(root)
        if not root.exists():continue
        dirs=[]
        for pat in ('MetaTrader*','*MetaTrader*','*MT4*','*MT5*','*OANDA*'):
            try:dirs+=list(root.glob(pat))
            except Exception:pass
        try:dirs+=[x for x in root.iterdir() if x.is_dir()]
        except Exception:pass
        for install in dirs:
            key=os.path.normcase(str(install))
            if key in seen:continue
            seen.add(key);out.append(install)
    return out

def discover_installations(base=None,roots=None):
    out=[]; seen=set()
    for install in candidate_install_dirs(base,roots):
        for exe,kind in (('terminal.exe','MT4'),('terminal64.exe','MT5')):
            terminal=install/exe
            if not terminal.is_file():continue
            key=(kind,os.path.normcase(str(terminal)))
            if key in seen:continue
            seen.add(key)
            out.append({
                'kind':kind,
                'terminal':str(terminal),
                'editor':find_editor(install,kind),
                'install_dir':str(install),
                'terminal_discovery':'install-binary',
                'editor_discovery':'terminal-install-dir'
            })
    return out

def runtime_candidate_score(t):
    terminal_ok=bool(t.get('terminal') and Path(t['terminal']).is_file())
    editor_ok=bool(t.get('editor') and Path(t['editor']).is_file())
    data_ok=bool(t.get('data_dir') and Path(t['data_dir']).is_dir())
    mql_ok=bool(data_ok and (Path(t['data_dir'])/mql_dir_name(t['kind'])).is_dir())
    ready=terminal_ok and editor_ok and data_ok and mql_ok
    origin_match=t.get('pair_method')=='origin-match'
    return (1 if ready else 0,1 if origin_match else 0,1 if terminal_ok else 0,1 if editor_ok else 0,1 if data_ok else 0,int(t.get('activity_ns') or 0))

def pair_runtime_candidates(installs=None,data_dirs=None):
    installs=list(discover_installations() if installs is None else installs)
    data_dirs=list(discover_data_dirs() if data_dirs is None else data_dirs)
    out=[]
    for inst in installs:
        compatible=[d for d in data_dirs if d['kind']==inst['kind']]
        exact=[d for d in compatible if d.get('origin') and same_path(d['origin'],inst['install_dir'])]
        pool=exact or compatible
        data=max(pool,key=lambda x:x.get('activity_ns',0)) if pool else None
        row=dict(inst)
        row.update({
            'data_dir':data.get('data_dir') if data else None,
            'data_origin':data.get('origin') if data else None,
            'activity_ns':data.get('activity_ns',0) if data else 0,
            'pair_method':'origin-match' if exact else ('platform-fallback' if data else 'unpaired-install')
        })
        out.append(row)
    for data in data_dirs:
        if any(x['kind']==data['kind'] and x.get('data_dir')==data['data_dir'] for x in out):continue
        out.append({
            'kind':data['kind'],
            'terminal':None,
            'editor':None,
            'install_dir':None,
            'data_dir':data['data_dir'],
            'data_origin':data.get('origin'),
            'activity_ns':data.get('activity_ns',0),
            'pair_method':'unpaired-data'
        })
    out.sort(key=runtime_candidate_score,reverse=True)
    return out

def terminals(base=None,roots=None):
    data=discover_data_dirs(base)
    installs=discover_installations(base,roots)
    return pair_runtime_candidates(installs,data)

def detect():
    base=metatrader_data_root()
    data=discover_data_dirs(base)
    installs=discover_installations(base)
    emit({
        'ok':True,
        'installations':installs,
        'data_directories':data,
        'observed_data_folders':observed_data_folders(base),
        'terminals':pair_runtime_candidates(installs,data)
    })

def source_kind(source):
    return 'MT5' if Path(source).suffix.lower() in ('.mq5','.ex5') else 'MT4'

def selection_checks(src,kind,selected):
    data_dir=Path(selected['data_dir']) if selected.get('data_dir') else None
    return {
        'source':src.is_file(),
        'terminal_binary':bool(selected.get('terminal') and Path(selected['terminal']).is_file()),
        'metaeditor_binary':bool(selected.get('editor') and Path(selected['editor']).is_file()),
        'data_directory':bool(data_dir and data_dir.is_dir()),
        'mql_directory':bool(data_dir and (data_dir/mql_dir_name(kind)).is_dir())
    }

def discovery_diagnostics(kind,selected=None,checks=None):
    selected=selected or {}
    checks=checks or {}
    mql_name=mql_dir_name(kind)
    return [
        {'stage':'Terminal binary','status':'FOUND' if checks.get('terminal_binary') else 'NOT FOUND','path':selected.get('terminal')},
        {'stage':'MetaEditor binary','status':'FOUND' if checks.get('metaeditor_binary') else 'NOT FOUND','path':selected.get('editor')},
        {'stage':'Data directory','status':'FOUND' if checks.get('data_directory') else 'NOT FOUND','path':selected.get('data_dir'),'pair_method':selected.get('pair_method')},
        {'stage':f'{mql_name} directory','status':'FOUND' if checks.get('mql_directory') else 'NOT FOUND','path':str(Path(selected['data_dir'])/mql_name) if selected.get('data_dir') else None}
    ]

def choose_runtime(kind,terminal=None):
    candidates=[x for x in terminals() if x['kind']==kind]
    if terminal:
        exact=[x for x in candidates if x.get('terminal') and same_path(x['terminal'],terminal)]
        if exact:candidates=exact
    if not candidates:
        diagnostics=[{'stage':'Terminal binary','status':'NOT FOUND','path':None}]
        raise PreviewRuntimeError(f'No {kind} terminal binary was detected.',diagnostics)
    return candidates[0]

def preview_runtime(source,terminal=None,selected=None):
    src=Path(source)
    kind=source_kind(src)
    if not src.is_file():
        raise PreviewRuntimeError(f'Preview source was not found: {src}',[{'stage':'Source file','status':'NOT FOUND','path':str(src)}])
    selected=selected or choose_runtime(kind,terminal)
    checks=selection_checks(src,kind,selected)
    diagnostics=discovery_diagnostics(kind,selected,checks)
    missing=[d['stage'] for d in diagnostics if d['status']!='FOUND']
    if missing:
        observed={
            'installations':discover_installations(),
            'data_directories':discover_data_dirs(),
            'observed_data_folders':observed_data_folders()
        }
        raise PreviewRuntimeError(
            f'{kind} preview runtime validation failed: {", ".join(missing)}.',
            diagnostics+[{'stage':'Discovery inventory','status':'INFO','details':observed}]
        )
    return src,kind,selected,checks

def runtime_cache_path(dest,kind):
    p=Path(dest).parent/'preview-runtime'/f'{kind.lower()}-selection-v5.json'
    p.parent.mkdir(parents=True,exist_ok=True)
    return p

def load_runtime_cache(dest,kind):
    path=runtime_cache_path(dest,kind)
    if not path.is_file():return None
    try:data=json.loads(path.read_text(encoding='utf-8'))
    except Exception:return None
    if data.get('kind')!=kind:return None
    selected=data.get('terminal') or {}
    src=Path(data.get('source') or '.')
    checks=selection_checks(src,kind,selected)
    if not all(checks[k] for k in ('terminal_binary','metaeditor_binary','data_directory','mql_directory')):
        try:path.unlink()
        except Exception:pass
        return None
    return selected

def write_runtime_cache(dest,kind,source,selected):
    path=runtime_cache_path(dest,kind)
    path.write_text(json.dumps({'version':5,'kind':kind,'source':str(source),'terminal':selected,'saved_at':int(time.time())},ensure_ascii=False,indent=2),encoding='utf-8')
    return path
