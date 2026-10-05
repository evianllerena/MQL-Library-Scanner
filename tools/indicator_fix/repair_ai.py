r"""AI repair worker (MQL_ONE-style loop, using the installed `claude` CLI headless).
Usage: repair_ai.py <jobs.json> [--workers 4] [--model sonnet]
jobs.json: [{"name": ..., "source": <path .mq5>, "problem": "compile" | "runtime", "detail": <text>}]
Each job gets F:\MQLFIX_BUILD\R\<name>\<name>.mq5 (+ any local .mqh it includes, copied from beside the
original). One `claude -p` run per job may only Read/Edit that folder and run the compile helper. The
result is then re-verified by an independent compile. Results -> F:\MQLFIX_BUILD\R\repair_results.jsonl
(resumable: names already there are skipped)."""
import hashlib, json, re, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

W = Path(r'F:\MQLFIX_BUILD'); R = W / 'R'; R.mkdir(parents=True, exist_ok=True)
CLAUDE = r'C:\Users\Evision\.local\bin\claude.exe'
MQLC = r'F:\MQLFIX_BUILD\tools\mqlc.py'
QA_ONE = str(Path(__file__).resolve().parent / 'qa_one.py')   # compile + MT5 runtime + look-ahead test
INCLUDE_DIRS = [Path(r'F:\Converted Indicators'), Path(r'F:\Ready MQL5 Indicators')]

def arg(flag, default):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default

OUT = Path(arg('--out', str(R / 'repair_results.jsonl')))
import queue
RT_POOL = queue.Queue()
for _n in (1, 2, 3, 4): RT_POOL.put(_n)          # one MT5 runtime copy (F:\MQLFIX_RT\rtN) per concurrent job

PROMPT = """You are repairing ONE MetaTrader 5 custom indicator that was machine-converted from MQL4.
File: {file} (in the current directory). Problem: {problem}.
{detail}

Goal: the indicator must compile, run in MetaTrader 5 without errors, produce real values, and compute
the SAME values the original MQL4 indicator did, on the correct bars (past values must not change when
newer bars arrive).
Rules:
- Edit only {file} (and .mqh files in this directory if they are the cause). Keep every input
  parameter, buffer, plot and the calculation logic; fix conversion mistakes, do not redesign.
  Renaming an identifier that collides with an MQL5 built-in (e.g. an input named SymbolName) is fine.
- MQL4 code indexes price series and indicator buffers newest-first (index 0 = current bar). If the
  code is written that way, call ArraySetAsSeries(buffer,true) for its buffers (and for any
  OnCalculate price arrays it reads) at the start of OnCalculate. Never read beyond an array's size:
  guard loops so indexes stay within 0..size-1 ("array out of range" stops the indicator in MT5).
- No DLL imports, no trading/order functions, no file/network/web access.
- Common causes: MT4-style newest-first indexing without ArraySetAsSeries; a new iMA/iCCI/... handle
  created per value (cache handles instead); globals/statics that accumulate across OnCalculate calls
  (reset them when prev_calculated==0); loops reading index i+1 or i+period past the array end; using
  Bars/MQL4_Bars() where rates_total is meant.
- Check your work by running exactly:  python c.py
  It compiles {file}, runs it in MetaTrader 5 (EURUSD and GBPJPY daily) plus a look-ahead test, and
  prints PASS or exactly what failed (with MT5 error lines). Run it at most 8 times.
- If the indicator repaints or uses future bars BY DESIGN (e.g. ZigZag-type), stop and answer
  FAILED: repaints by design.
Finish with exactly one line: DONE (if c.py printed PASS) or FAILED: <short reason>."""

def done_names():
    names = set()
    if OUT.exists():
        for line in open(OUT, encoding='utf-8', errors='replace'):
            try: names.add(json.loads(line)['name'])
            except Exception: pass
    return names

def local_includes(text, original):
    found = []
    for inc in re.findall(r'#include\s+"([^"]+)"', text):
        for base in [Path(original).parent] + INCLUDE_DIRS:
            p = base / inc
            if p.exists():
                found.append(p); break
    return found

STOP = []   # set when the Claude usage limit is hit: remaining jobs are left untouched for the next run

def run_job(job):
    if STOP: return {'name': job['name'], 'skipped': True}
    name = job['name']
    short = re.sub(r'[^\w\-]', '_', name)[:40] + '__' + hashlib.sha1(name.encode('utf-8')).hexdigest()[:8]
    d = R / short                                  # stay well under Windows' 260-character path limit
    shutil.rmtree(d, ignore_errors=True); d.mkdir(parents=True)
    target = d / f'{short}.mq5'
    shutil.copy2(job['source'], target)
    text = target.read_bytes().decode('utf-8', 'replace')
    for inc in local_includes(text, job.get('original', job['source'])):
        shutil.copy2(inc, d / inc.name)
    rt = RT_POOL.get()
    (d / 'c.py').write_text('import subprocess, sys\n'
                            f'sys.exit(subprocess.run([sys.executable, r"{QA_ONE}", r"{target}", "{rt}"]).returncode)\n',
                            encoding='utf-8')
    prompt = PROMPT.format(file=target.name, problem=job['problem'], detail=job.get('detail', ''),
                           mqlc=MQLC, path=str(target))
    t0 = time.time()
    try:
        cp = subprocess.run([CLAUDE, '-p', prompt, '--model', arg('--model', 'sonnet'), '--effort', arg('--effort', 'low'),
                             '--permission-mode', 'acceptEdits',
                             '--allowedTools', 'Read', 'Edit', 'Write', 'Grep', 'Glob', 'Bash(python c.py)', 'Bash(python c.py:*)',
                             '--output-format', 'text'],
                            cwd=str(d), capture_output=True, text=True, encoding='utf-8', errors='replace',
                            timeout=int(arg('--timeout', 1200)))
        answer = (cp.stdout or '').strip().splitlines()[-1:] or ['']
    except subprocess.TimeoutExpired:
        answer = ['FAILED: timeout']
    low = answer[0].lower()
    if 'limit' in low and ('hit your' in low or 'usage' in low or 'resets' in low):
        STOP.append(True); RT_POOL.put(rt)
        return {'name': name, 'skipped': True}        # not recorded: retried on the next run
    try:
        verify = subprocess.run([sys.executable, QA_ONE, str(target), str(rt)], capture_output=True, text=True, timeout=400)
        vout, vcode = verify.stdout, verify.returncode
    except subprocess.TimeoutExpired:
        vout, vcode = 'verification timeout', 1
    RT_POOL.put(rt)
    rec = {'name': name, 'problem': job['problem'], 'agent': answer[0][:300],
           'passes': vcode == 0, 'compiles': 'COMPILE FAILED' not in vout, 'check': vout.splitlines()[-3:],
           'seconds': round(time.time() - t0), 'path': str(target)}
    with open(OUT, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec) + '\n')
    return rec

if __name__ == '__main__':
    jobs = json.load(open(sys.argv[1], encoding='utf-8'))
    skip = done_names()
    todo = [j for j in jobs if j['name'] not in skip]
    print(f'{len(todo)} jobs ({len(jobs) - len(todo)} already done)', flush=True)
    with ThreadPoolExecutor(max_workers=int(arg('--workers', 4))) as ex:
        def safe(job):
            try:
                return run_job(job)
            except Exception as e:                 # one bad job must not stop the batch
                rec = {'name': job['name'], 'problem': job['problem'], 'agent': f'ERROR: {e!r}'[:300],
                       'compiles': False, 'compile_result': [], 'seconds': 0, 'path': ''}
                with open(OUT, 'a', encoding='utf-8') as f: f.write(json.dumps(rec) + '\n')
                return rec
        for i, rec in enumerate(ex.map(safe, todo), 1):
            if rec.get('skipped'): continue
            print(f"{i}/{len(todo)} {rec['name'][:40]} passes={rec.get("passes")} {rec['seconds']}s {rec['agent'][:60]}", flush=True)
