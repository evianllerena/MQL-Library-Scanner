r"""Runtime QA driver: feeds compiled indicators to IndicatorQA.mq5 in the batch's MT5 runtime clone.
Usage: qa_driver.py <variant A|B> [--names file_with_names] [--session 300]
Copies each <name>.ex5 into <runtime>\MQL5\Indicators\QA\<V>\, writes Files\qa_jobs.txt, launches the
terminal, and appends every result line to F:\MQLFIX_BUILD\qa_<V>.jsonl. Resumable: names already in the
results file are skipped. If the terminal stops producing output for HANG_S seconds, it is killed, the
indicator it was on is recorded as 'hang', and a new session continues after it."""
import hashlib, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

def arg(flag, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default

# --rt <portable MT5 dir> and --shard k/N let several runtime copies test disjoint shares in parallel
RT = Path(arg('--rt', r'C:\Users\Evision\AppData\Local\nnfx-backtest-runtime\preview-runtime\v5\mt5-5ece741c1d'))
FILES = RT / 'MQL5' / 'Files'
W = Path(r'F:\MQLFIX_BUILD')
HANG_S = 120
V = sys.argv[1]
SESSION = int(arg('--session', 300))
SHARD_K, SHARD_N = (int(x) for x in arg('--shard', '0/1').split('/'))
only = None
if '--names' in sys.argv:
    only = [l.strip() for l in open(arg('--names'), encoding='utf-8') if l.strip()]
OUT = W / (f'qa_{V}.jsonl' if SHARD_N == 1 else f'qa_{V}_s{SHARD_K}.jsonl')
requeues = {}

def in_shard(name):
    return int(hashlib.md5(name.encode('utf-8')).hexdigest(), 16) % SHARD_N == SHARD_K

def alias(name):
    """MT5's script reads the job file as ANSI, so non-ASCII names (e.g. Cyrillic) become '?' and the
    indicator can't be found. Such indicators are tested under an ASCII alias, mapped back on output."""
    if re.fullmatch(r'[\x20-\x7e]+', name) and '|' not in name:
        return name
    return 'u_' + hashlib.sha1(name.encode('utf-8')).hexdigest()[:12]

REF = {'B': 'A', 'D': 'C'}     # a flipped variant is only worth testing where its base is not aligned

def aligned_in_A():
    ok = set()
    if V not in REF: return ok
    for a in W.glob(f'qa_{REF[V]}_s*.jsonl'):
        for line in open(a, encoding='utf-8', errors='replace'):
            try: r = json.loads(line)
            except Exception: continue
            if r.get('align') == 'aligned': ok.add(r.get('id'))
    return ok

def compiled():
    for chunk in sorted((W / V).glob('c*')):
        if chunk.is_dir():
            for ex in chunk.glob('*.ex5'):
                if ex.stat().st_size > 0:
                    yield ex.stem, ex

def done_ids():
    ids = set()
    for f in W.glob(f'qa_{V}*.jsonl'):
        for line in open(f, encoding='utf-8', errors='replace'):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if 'id' in r and r.get('phase') != 'begin':
                ids.add(r['id'])
    return ids

def run_session(batch):
    dest = RT / 'MQL5' / 'Indicators' / 'QA' / V
    dest.mkdir(parents=True, exist_ok=True)
    jobs = []; back = {}
    for name, ex in batch:
        a = alias(name); back[a] = name
        shutil.copy2(ex, dest / f'{a}.ex5')
        jobs.append(f'{a}|QA\\{V}\\{a}')
    (FILES / 'qa_jobs.txt').write_text('\n'.join(jobs) + '\n', encoding='ascii', errors='replace')
    for f in ('qa_results.jsonl', 'qa_done.flag'):
        try: (FILES / f).unlink()
        except FileNotFoundError: pass
    ini = RT / 'qa.ini'
    ini.write_text('[Experts]\nEnabled=1\nAllowLiveTrading=0\nAllowDllImport=0\n\n[StartUp]\nSymbol=EURUSD\nPeriod=D1\n'
                   'Script=IndicatorQA\nShutdownTerminal=0\n', encoding='utf-8')
    proc = subprocess.Popen([str(RT / 'terminal64.exe'), '/portable', f'/config:{ini}'], cwd=str(RT))
    last_size, last_change = -1, time.time()
    while True:
        time.sleep(2)
        res = FILES / 'qa_results.jsonl'
        size = res.stat().st_size if res.exists() else 0
        if size != last_size:
            last_size, last_change = size, time.time()
        if (FILES / 'qa_done.flag').exists() or proc.poll() is not None:
            break
        if time.time() - last_change > HANG_S:
            proc.kill(); break
    try: proc.wait(timeout=30)
    except Exception: subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True)
    lines = (FILES / 'qa_results.jsonl').read_text(encoding='utf-8', errors='replace').splitlines() if (FILES / 'qa_results.jsonl').exists() else []
    finished, begun = set(), None
    with open(OUT, 'a', encoding='utf-8') as o:
        for line in lines:
            try: r = json.loads(line)
            except Exception: continue
            if 'id' in r: r['id'] = back.get(r['id'], r['id'])
            if r.get('phase') == 'begin':
                begun = r['id']; continue
            if r.get('requeue'):
                # The symbol's calculation queue was still blocked by the PREVIOUS indicator: this one is
                # untested, not failed. Retry it in a fresh session; after 3 tries record queue_unhealthy.
                requeues[r['id']] = requeues.get(r['id'], 0) + 1
                if requeues[r['id']] >= 3:
                    o.write(json.dumps({'id': r['id'], 'load': 'queue_unhealthy', 'has_values': False, 'align': 'skipped'}) + '\n')
                    finished.add(r['id'])
                begun = None
                continue
            if 'id' in r:
                finished.add(r['id']); o.write(json.dumps(r) + '\n')
            elif 'session' in r:
                o.write(json.dumps({'session': r, 'variant': V, 'time': time.time()}) + '\n')
        if begun and begun not in finished:   # the terminal died or hung on this one
            o.write(json.dumps({'id': begun, 'load': 'hang', 'has_values': False, 'align': 'skipped'}) + '\n')
            finished.add(begun)
    return len(finished)

if __name__ == '__main__':
    t0 = time.time(); stalls = 0
    while True:
        done = done_ids()
        skip = aligned_in_A()
        todo = [(n, ex) for n, ex in compiled() if n not in done and n not in skip and in_shard(n) and (only is None or n in only)]
        if not todo:
            break
        n = run_session(todo[:SESSION])
        print(f'{V}: session finished {n}, remaining ~{len(todo) - n}, elapsed {round(time.time() - t0)} s', flush=True)
        stalls = stalls + 1 if n == 0 else 0
        if stalls >= 3:
            print('no progress in 3 sessions -- stopping'); break
    print(V, 'QA complete', round(time.time() - t0), 's')
