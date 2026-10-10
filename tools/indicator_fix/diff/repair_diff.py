r"""AI repair driven by the MT4 differential test (headless `claude -p`, as in ../repair_ai.py).
Usage: repair_diff.py [--workers 4] [--model sonnet] [--names file] [--max-attempts 3]
Jobs: every alias whose compare verdict (DIFF\verdicts.json) is not verified and that has an MT4 reference run.
Per attempt the agent gets RD\<alias>\: <alias>.mq5 (current best), original.mq4 (the MQL4 reference, read-only),
c.py (= diff_one.py: compile + MT5 run + bar-by-bar compare with MT4). After the agent, diff_one.py re-verifies
independently. Max 3 attempts per indicator (owner decision 4A); attempt N+1 starts from the agent's last file and
gets the previous FAILED reason. Results -> RD\results.jsonl (resumable). Stops cleanly at the Claude usage limit."""
import hashlib, json, queue, re, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
HERE = Path(__file__).resolve().parent
D = Path(r'F:\MQLFIX_BUILD\DIFF'); RD = Path(r'F:\MQLFIX_BUILD\RD'); RD.mkdir(exist_ok=True)
CLAUDE = r'C:\Users\Evision\.local\bin\claude.exe'
OUT = RD / 'results.jsonl'
def arg(flag, default):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default
MAX_ATT = int(arg('--max-attempts', 3))
RT_POOL = queue.Queue()
for _n in arg('--rts', '1,2,3,4').split(','): RT_POOL.put(int(_n))
STOP = []

PROMPT = """Fix ONE MetaTrader 5 indicator that was machine-converted from MQL4.
original.mq4 = the MQL4 original (the reference; do not edit). {file} = the MT5 conversion (edit this).
Goal: in MT5, every buffer must hold the SAME values the original produces in MT4, on every bar, with buffer k in
MT5 = buffer k in MT4 (EAs read them by index with iCustom/CopyBuffer), also when bars arrive one at a time.
If the original only draws chart objects (no buffers), the objects it draws (type, times, prices, x/y, text) must match.
Current test result:
{report}
Check with exactly: py c.py   (at most 8 runs). It compiles {file}, runs it in MT5 and compares all buffers
bar by bar with the MT4 original on 3000 EURUSD daily bars: F = full history, C = history 150 bars shorter,
I = the last 150 bars appended one at a time. It prints PASS, or the first differing buffer/bar (bar 0 = oldest)
with the MT4 and MT5 values, and any MT5 runtime errors.
Rules: edit only {file}. Keep input names, types, defaults and order, and the buffer order, as in the original.
No DLLs, trading, file or web functions. Re-writing a section in native MQL5 is fine when the converted code is
wrong; translate the original's logic faithfully, do not redesign or "improve" it.
Common causes: MT4 newest-first indexing without ArraySetAsSeries(buffer/price arrays,true); IndicatorCounted /
prev_calculated / limit handling; MQL4 shims (iMA, iRSI, iStdDev, iMAOnArray, iHighest...) returning different
values or newest-first vs oldest-first mix-ups; #property indicator_buffers / indicator_plots too small;
EMPTY_VALUE vs 0 (SetIndexEmptyValue -> PLOT_EMPTY_VALUE); Bars vs rates_total; statics not reset.
{previous}Finish with exactly one line: DONE (py c.py printed PASS) or FAILED: <short reason>."""

def done():
    res = {}
    if OUT.exists():
        for line in open(OUT, encoding='utf-8', errors='replace'):
            try: r = json.loads(line); res.setdefault(r['alias'], []).append(r)
            except Exception: pass
    return res

def check(target, alias, rt):
    try:
        v = subprocess.run([sys.executable, str(HERE / 'diff_one.py'), str(target), alias, str(rt)],
                           capture_output=True, text=True, timeout=400)
        return v.returncode == 0, v.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, 'check timeout'

def run_job(alias, src, ref, attempt, previous):
    if STOP: return None
    d = RD / (re.sub(r'[^\w\-]', '_', alias)[:40] + '__' + hashlib.sha1(alias.encode('utf-8')).hexdigest()[:8])
    d.mkdir(parents=True, exist_ok=True)
    target = d / 'ind.mq5'
    if attempt == 1 or not target.exists():
        shutil.copy2(src, target)
    raw = Path(ref).read_bytes()
    (d / 'original.mq4').write_text(raw.decode('utf-16' if raw[:2] in (b'\xff\xfe', b'\xfe\xff') else 'cp1252', 'replace'), encoding='utf-8')
    rt = RT_POOL.get()
    try:
        (d / 'c.py').write_text('import subprocess, sys\n'
                                f'sys.exit(subprocess.run([sys.executable, r"{HERE / "diff_one.py"}", r"{target}", r"{alias}", "{rt}"]).returncode)\n',
                                encoding='utf-8')
        ok0, report = check(target, alias, rt)
        if ok0:
            rec = {'alias': alias, 'attempt': attempt, 'passes': True, 'agent': 'already passes', 'path': str(target)}
        else:
            prev = f'Previous attempt ended with: {previous}\n' if previous else ''
            prompt = PROMPT.format(file=target.name, report=report[-2500:], previous=prev)
            t0 = time.time()
            try:
                cp = subprocess.run([CLAUDE, '-p', prompt, '--model', arg('--model', 'sonnet'),
                                     '--effort', 'low' if attempt == 1 else 'medium',
                                     '--permission-mode', 'acceptEdits',
                                     '--allowedTools', 'Read', 'Edit', 'Write', 'Grep', 'Glob', 'Bash(py c.py)', 'Bash(py c.py:*)', 'PowerShell(py c.py)', 'PowerShell(py c.py:*)',
                                     '--output-format', 'text'],
                                    cwd=str(d), capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=1800)
                answer = ((cp.stdout or '').strip().splitlines() or [''])[-1]
                if not answer and cp.stderr: answer = cp.stderr.strip().splitlines()[-1]
            except subprocess.TimeoutExpired:
                answer = 'FAILED: timeout'
            low = answer.lower()
            if 'limit' in low and ('hit your' in low or 'usage' in low or 'resets' in low):
                STOP.append(answer); return None          # not recorded: retried on the next run
            ok, report = check(target, alias, rt)
            rec = {'alias': alias, 'attempt': attempt, 'passes': ok, 'agent': answer[:300],
                   'check': report.splitlines()[:3], 'seconds': round(time.time() - t0), 'path': str(target)}
    finally:
        RT_POOL.put(rt)
    with open(OUT, 'a', encoding='utf-8') as f: f.write(json.dumps(rec) + '\n')
    return rec

if __name__ == '__main__':
    verdicts = json.load(open(arg('--verdicts', str(D / 'verdicts.json')), encoding='utf-8'))
    aliases = json.load(open(D / 'aliases.json', encoding='utf-8'))
    pairs = json.load(open(D / 'pairs.json', encoding='utf-8'))
    vmap = json.load(open(Path(r'F:\MQLFIX_BUILD\V\map.json'), encoding='utf-8'))
    only = set(l.strip() for l in open(arg('--names', ''), encoding='utf-8') if l.strip()) if '--names' in sys.argv else None
    hist = done()
    jobs = []
    for a, v in verdicts.items():
        if v['verdict'] in ('verified', 'verified_near', 'no_reference_run', 'reference_has_no_values'): continue
        if only is not None and a not in only: continue
        if a not in vmap or pairs[aliases[a]]['ref_kind'] != 'mq4': continue
        h = hist.get(a, [])
        if any(r['passes'] for r in h) or len(h) >= MAX_ATT: continue
        jobs.append((a, vmap[a]['mq5'], pairs[aliases[a]]['ref'], len(h) + 1, h[-1]['agent'] if h else ''))
    print(len(jobs), 'jobs', flush=True)
    with ThreadPoolExecutor(max_workers=int(arg('--workers', 4))) as ex:
        def safe(j):
            try: return run_job(*j)
            except Exception as e: print('ERROR', j[0], repr(e), flush=True); return None
        for i, rec in enumerate(ex.map(safe, jobs), 1):
            if rec: print(f"{i}/{len(jobs)} {rec['alias'][:40]} att{rec['attempt']} passes={rec['passes']} {rec['agent'][:60]}", flush=True)
    if STOP: print('USAGE LIMIT:', STOP[0], flush=True)
