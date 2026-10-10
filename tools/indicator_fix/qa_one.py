r"""Compile + runtime-test ONE indicator in a given runtime copy; print PASS or exactly what failed.
Usage: python qa_one.py <file.mq5> <runtime number 1-4>      exit 0 = PASS (loads, real values, aligned)
Used as the repair agent's checker (via c.py) and by finalize.py."""
import json, shutil, subprocess, sys, time
from pathlib import Path

MQLC = r'F:\MQLFIX_BUILD\tools\mqlc.py'
src = Path(sys.argv[1]).resolve(); rt = Path(rf'F:\MQLFIX_RT\rt{sys.argv[2]}')
files = rt / 'MQL5' / 'Files'

c = subprocess.run([sys.executable, MQLC, str(src)], capture_output=True, text=True)
if c.returncode != 0:
    print('COMPILE FAILED'); print(c.stdout); sys.exit(1)
dest = rt / 'MQL5' / 'Indicators' / 'QA' / 'ONE'; dest.mkdir(parents=True, exist_ok=True)
shutil.copy2(src.with_suffix('.ex5'), dest / 'one.ex5')
(files / 'qa_jobs.txt').write_text('one|QA\\ONE\\one\n', encoding='ascii')
for f in ('qa_results.jsonl', 'qa_done.flag'):
    try: (files / f).unlink()
    except FileNotFoundError: pass
ini = rt / 'qa.ini'
ini.write_text('[Experts]\nEnabled=1\nAllowLiveTrading=0\nAllowDllImport=0\n\n[StartUp]\nSymbol=EURUSD\nPeriod=D1\n'
               'Script=IndicatorQA\nShutdownTerminal=0\n', encoding='utf-8')
for old in (rt / 'MQL5' / 'Logs').glob('*.log'):   # MT5 logs grow to GBs from indicators' error spam
    try: old.unlink()
    except OSError: pass
started = time.time()
p = subprocess.Popen([str(rt / 'terminal64.exe'), '/portable', f'/config:{ini}'], cwd=str(rt))
while time.time() - started < 150 and not (files / 'qa_done.flag').exists() and p.poll() is None:
    time.sleep(1)
if p.poll() is None:
    subprocess.run(['taskkill', '/F', '/T', '/PID', str(p.pid)], capture_output=True)
res = None
if (files / 'qa_results.jsonl').exists():
    for line in (files / 'qa_results.jsonl').read_text(encoding='utf-8', errors='replace').splitlines():
        try: r = json.loads(line)
        except Exception: continue
        if r.get('id') == 'one' and 'load' in r: res = r
# MT5's own error lines for this indicator (e.g. "array out of range in 'one.mq5' (123,45)")
errs = []
logs = sorted((rt / 'MQL5' / 'Logs').glob('*.log'), key=lambda x: x.stat().st_mtime)
if logs:
    with open(logs[-1], 'rb') as fh:
        fh.seek(0, 2); fh.seek(max(0, fh.tell() - 2_000_000) & ~1)
        tail = fh.read().decode('utf-16-le', errors='replace')
    for line in tail.splitlines():
        if 'one (' in line and ('error' in line or 'out of range' in line or 'cannot' in line or 'divide' in line):
            errs.append(line.split('\t')[-1].strip())
if not res:
    print('RUNTIME: no result (terminal hung or the indicator froze it)')
    print('\n'.join(dict.fromkeys(errs[-8:]))); sys.exit(1)
d = res.get('align_detail') or {}
for s in ('EURUSD', 'GBPJPY'):
    x = res.get(s, {}); print(f"{s}: {x.get('status')} in {x.get('ms')} ms, buffers {x.get('nbuf')}, value stats {x.get('stats')}")
print(f"real values: {res.get('has_values')}   alignment: {res.get('align')} {d}")
print('  (alignment compares the same past bars with 150 extra newer bars appended; any difference means values '
      'depend on future bars, mirrored indexing, or state that is not reset between calculations)')
if errs: print('MT5 errors:\n  ' + '\n  '.join(dict.fromkeys(errs[-8:])))
ok = res.get('load') == 'ok' and res.get('has_values') and res.get('align') == 'aligned'
print('PASS' if ok else 'FAIL'); sys.exit(0 if ok else 1)
