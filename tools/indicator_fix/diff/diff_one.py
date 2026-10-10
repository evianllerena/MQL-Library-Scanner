r"""Compile ONE MT5 indicator, run it through DiffDump5 in runtime copy N and compare it with its MT4 reference dumps.
Usage: python diff_one.py <file.mq5> <alias> <runtime 1-4>
Prints PASS (exit 0) or what differs: per check (F full history, C cut history, I bar-by-bar), the worst MT4 buffer,
the first differing bar (0 = oldest of 3000 daily bars) and sample values, plus MT5 runtime errors.
Used by the repair agent (via c.py) and to re-verify its result independently."""
import json, shutil, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import compare, rtproc
MQLC = r'F:\MQLFIX_BUILD\tools\mqlc.py'
src, alias, n = Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3]
rt = Path(rf'F:\MQLFIX_RT\rt{n}'); files = rt / 'MQL5' / 'Files'
out = src.parent / 'd5'; out.mkdir(exist_ok=True)

c = subprocess.run([sys.executable, MQLC, str(src)], capture_output=True, text=True)
if c.returncode != 0:
    print('COMPILE FAILED'); print(c.stdout); sys.exit(1)
dest = rt / 'MQL5' / 'Indicators' / 'DIFF'; dest.mkdir(parents=True, exist_ok=True)
shutil.copy2(src.with_suffix('.ex5'), dest / f'one{n}.ex5')
(files / 'd5').mkdir(exist_ok=True)
(files / 'diff_jobs.txt').write_text(f'one{n}|DIFF\\one{n}\n', encoding='ascii')
for f in ['diff_results.jsonl', 'diff_done.flag'] + [p.name for p in (files / 'd5').glob(f'one{n}.*')]:
    for base in (files, files / 'd5'):
        try: (base / f).unlink()
        except (FileNotFoundError, PermissionError): pass
for old in (rt / 'MQL5' / 'Logs').glob('*.log'):
    try: old.unlink()
    except OSError: pass
ini = rt / 'diff.ini'
ini.write_text('[Experts]\nEnabled=1\nAllowLiveTrading=0\nAllowDllImport=0\n\n[StartUp]\nSymbol=DIFF\nPeriod=D1\n'
               'Script=DiffDump5\nShutdownTerminal=0\n', encoding='utf-8')
t0 = time.time()
rtproc.kill(rt)
p = rtproc.launch(rt, ['/portable', f'/config:{ini}'])
while time.time() - t0 < 420 and not (files / 'diff_done.flag').exists() and (p.poll() is None or rtproc.alive(rt)):
    if not (rt / 'skiptoken.txt').exists(): rtproc.learn(rt)
    time.sleep(1)
rtproc.kill(rt)
for f in out.glob('*.bin'): f.unlink()
for f in (files / 'd5').glob(f'one{n}.*.bin'):
    shutil.move(str(f), str(out / f.name.replace(f'one{n}.', f'{alias}.', 1)))
run = {}
if (files / 'diff_results.jsonl').exists():
    for line in (files / 'diff_results.jsonl').read_text(encoding='utf-8', errors='replace').splitlines():
        try: r = json.loads(line)
        except Exception: continue
        if r.get('id') == f'one{n}' and 'F' in r: run = r
errs = []
for log in (rt / 'MQL5' / 'Logs').glob('*.log'):
    for line in log.read_text(encoding='utf-16', errors='replace').splitlines():
        if f'one{n}' in line and any(k in line.lower() for k in ('error', 'out of range', 'divide', 'invalid', 'critical', 'failed')):
            errs.append(line.split('\t')[-1][:200])
v = compare.verdict(alias, out)
ok = v['verdict'] in ('verified', 'verified_near')
print('PASS' if ok else 'FAIL: ' + v['verdict'])
print('MT5 run:', json.dumps({k: run.get(k) for k in 'FCI'}) if run else 'no result (crashed or hung)')
print('compare:', json.dumps(v))
if errs: print('MT5 errors:', *sorted(set(errs))[:8], sep='\n  ')
sys.exit(0 if ok else 1)
