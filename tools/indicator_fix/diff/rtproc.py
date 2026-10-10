r"""Launch / watch / kill a portable MT5 runtime that may be caught in MetaQuotes' LiveUpdate cycle.
The demo server announces a newer build; the terminal then exits and starts an updater, which fails here and
relaunches the old terminal with '/skipupdate:<token>' plus the original arguments. Once that token has been seen
for a runtime it is stored in <rt>\skiptoken.txt and passed on every launch, so the update cycle is skipped."""
import json, re, subprocess, time
from pathlib import Path

def procs(rt):
    """terminal64 processes belonging to runtime rt (its own exe, or an updater working on its path)"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='terminal64.exe'\" | "
          "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    out = subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True, text=True).stdout.strip()
    if not out: return []
    rows = json.loads(out); rows = rows if isinstance(rows, list) else [rows]
    key = str(rt).lower().rstrip('\\')
    return [r for r in rows if r.get('CommandLine') and key in r['CommandLine'].lower().replace('/', '\\')]

def learn(rt):
    tf = Path(rt) / 'skiptoken.txt'
    for r in procs(rt):
        m = re.search(r'/skipupdate:(\w+)', r['CommandLine'])
        if m:
            tf.write_text(m.group(1)); return m.group(1)
    return None

def launch(rt, args):
    rt = Path(rt); tf = rt / 'skiptoken.txt'
    pre = [f'/skipupdate:{tf.read_text().strip()}'] if tf.exists() else []
    return subprocess.Popen([str(rt / 'terminal64.exe')] + pre + list(args), cwd=str(rt))

def kill(rt):
    for r in procs(rt):
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(r['ProcessId'])], capture_output=True)
    time.sleep(2)

def alive(rt):
    return bool(procs(rt))
