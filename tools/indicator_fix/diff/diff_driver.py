r"""Differential-test driver: runs DiffDump4 (MT4) or DiffDump5 (MT5) over indicator batches in a portable terminal.
Usage: diff_driver.py 4|5 --rt <portable dir> [--shard k/N] [--names file] [--session 200] [--tag name]
  4: the references staged by stage_mt4.py in <rt>\MQL4\Indicators\DIFF\<alias>.ex4
  5: the MT5 indicators from pairs.json (or --ex5map json: alias -> .ex5 path, used by the repair loop)
Dumps are moved to F:\MQLFIX_BUILD\DIFF\d4|d5\, result lines appended to DIFF\r<side>_<tag>.jsonl. Resumable:
ids already in that file are skipped. A terminal silent for HANG_S seconds is killed; the indicator it was on
is recorded as 'hang' and the next session continues after it."""
import hashlib, json, os, shutil, subprocess, sys, time
import rtproc
from pathlib import Path
def arg(flag, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default
D = Path(r'F:\MQLFIX_BUILD\DIFF')
SIDE = sys.argv[1]
RT = Path(arg('--rt'))
SESSION = int(arg('--session', 200))
K, N = (int(x) for x in arg('--shard', '0/1').split('/'))
TAG = arg('--tag', f's{K}')
MODE = arg('--mode', 'FCI')              # MT5: FC = skip the bar-by-bar run
HANG_S = 30 if SIDE == '4' else 90     # MT4 needs ~0.2 s per indicator; MT5 up to 3 x 10 s + the 40 s bar-by-bar run
MQL = RT / ('MQL4' if SIDE == '4' else 'MQL5')
FILES = MQL / 'Files'
OUT = D / f'r{SIDE}_{TAG}.jsonl'
DUMPS = D / f'd{SIDE}'; DUMPS.mkdir(exist_ok=True)
only = set(l.strip() for l in open(arg('--names'), encoding='utf-8') if l.strip()) if arg('--names') else None

def in_shard(a): return int(hashlib.md5(a.encode('utf-8')).hexdigest(), 16) % N == K

def candidates():
    if SIDE == '4':
        return {p.stem: p for p in sorted((MQL / 'Indicators' / 'DIFF').glob('*.ex4'))}
    if arg('--ex5map'):
        return {a: Path(p) for a, p in json.load(open(arg('--ex5map'), encoding='utf-8')).items()}
    aliases = json.load(open(D / 'aliases.json', encoding='utf-8'))
    pairs = json.load(open(D / 'pairs.json', encoding='utf-8'))
    return {a: Path(pairs[n]['ex5']) for a, n in aliases.items() if pairs[n]['ex5']}

def done_ids():
    ids = set()
    if OUT.exists():
        for line in open(OUT, encoding='utf-8', errors='replace'):
            try: ids.add(json.loads(line)['id'])
            except Exception: pass
    return ids

def launch():
    if SIDE == '4':
        ini = RT / 'diff.ini'
        ini.write_text('ExpertsEnable=true\nExpertsDllImport=false\nSymbol=DIFF\nPeriod=D1\nScript=DiffDump4\n', encoding='ascii')
        return subprocess.Popen([str(RT / 'terminal.exe'), '/portable', str(ini)], cwd=str(RT))
    ini = RT / 'diff.ini'
    ini.write_text('[Experts]\nEnabled=1\nAllowLiveTrading=0\nAllowDllImport=0\n\n[StartUp]\nSymbol=DIFF\nPeriod=D1\n'
                   'Script=DiffDump5\nShutdownTerminal=0\n', encoding='utf-8')
    return rtproc.launch(RT, ['/portable', f'/config:{ini}'])

def run_session(batch):
    if SIDE == '5':
        dest = MQL / 'Indicators' / 'DIFF'; dest.mkdir(parents=True, exist_ok=True)
        for a, p in batch: shutil.copy2(p, dest / f'{a}.ex5')
    (FILES / f'd{SIDE}').mkdir(exist_ok=True)
    for _ in range(30):                       # a killed terminal can hold its files for a few seconds
        try: (FILES / 'diff_jobs.txt').open('a').close(); break
        except PermissionError: time.sleep(2)
    (FILES / 'diff_jobs.txt').write_text(''.join(f'{a}|DIFF\\{a}|{MODE}\n' for a, _ in batch), encoding='ascii', errors='replace')
    for f in ('diff_results.jsonl', 'diff_done.flag'):
        try: (FILES / f).unlink()
        except FileNotFoundError: pass
    for old in list((MQL / 'Logs').glob('*.log')) + list((RT / 'logs').glob('*.log')):   # error spam grows these to GBs
        try:
            if old.stat().st_size > 50_000_000: old.unlink()
        except OSError: pass
    # every launch adds a chart to the saved profile (objects included); start from an empty profile
    for chr_ in list(RT.glob('MQL5/Profiles/Charts/*/*.chr')) + list(RT.glob('profiles/*/*.chr')):
        try: chr_.unlink()
        except OSError: pass
    if SIDE == '5': rtproc.kill(RT)
    proc = launch()
    res = FILES / 'diff_results.jsonl'
    last_size, last_change, t0, gone = -1, time.time(), time.time(), 0
    while True:
        time.sleep(2)
        size = res.stat().st_size if res.exists() else 0
        if size != last_size: last_size, last_change = size, time.time()
        if (FILES / 'diff_done.flag').exists(): break
        if SIDE == '4':
            if proc.poll() is not None: break
        else:
            # MT5 may exit into the LiveUpdate cycle and come back as a relaunched process: follow the runtime,
            # not the first PID, and learn its /skipupdate token for the next launches
            if not (RT / 'skiptoken.txt').exists(): rtproc.learn(RT)
            gone = gone + 1 if proc.poll() is not None and not rtproc.alive(RT) else 0
            if gone >= 3: break
        limit = HANG_S if size > 0 else 400        # first output can take minutes after an update cycle
        if time.time() - last_change > limit: break
    if SIDE == '5': rtproc.kill(RT)
    else:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True)
        try: proc.wait(timeout=60)
        except Exception: pass
    lines = res.read_text(encoding='utf-8', errors='replace').splitlines() if res.exists() else []
    finished, begun = set(), None
    with open(OUT, 'a', encoding='utf-8') as o:
        for line in lines:
            try: r = json.loads(line)
            except Exception: continue
            if r.get('phase') == 'begin': begun = r['id']; continue
            if 'id' in r:
                finished.add(r['id']); o.write(json.dumps(r) + '\n')
        if begun and begun not in finished:
            o.write(json.dumps({'id': begun, 'F': {'status': 'hang'}}) + '\n'); finished.add(begun)
    for f in list((FILES / f'd{SIDE}').glob('*.bin')) + list((FILES / f'd{SIDE}').glob('*.obj')):
        shutil.move(str(f), str(DUMPS / f.name))
    return len(finished)

if __name__ == '__main__':
    t0, stalls = time.time(), 0
    while True:
        done = done_ids()
        todo = [(a, p) for a, p in candidates().items() if a not in done and in_shard(a) and (only is None or a in only)]
        if not todo: break
        n = run_session(todo[:SESSION])
        print(f'{SIDE}/{TAG}: session {n} done, ~{len(todo) - n} left, {round(time.time() - t0)} s', flush=True)
        stalls = stalls + 1 if n == 0 else 0
        if stalls >= 10: print('no progress in 10 sessions -- stopping'); break
    print(SIDE, TAG, 'complete', round(time.time() - t0), 's')
