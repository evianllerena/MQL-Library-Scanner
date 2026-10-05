r"""Build the AI repair queue (jobs_runtime.json) from every test result so far.
Order (most valuable first, so a usage limit cuts off the least important work):
  1. never compiled (Offline Converter sources still failing after the first AI pass)
  2. not working at runtime (crash, no values, misaligned) in every tested version
  3. runs but alignment untestable
  4. repaints (the agent may answer 'repaints by design')
Excluded: trading tools, Windows-DLL dependent, no entry point. Already-working indicators are skipped."""
import json, re
from pathlib import Path
import importlib.util

HERE = Path(__file__).resolve().parent; W = Path(r'F:\MQLFIX_BUILD')
spec = importlib.util.spec_from_file_location('pk', HERE / 'package.py'); pk = importlib.util.module_from_spec(spec); spec.loader.exec_module(pk)
best = pk.load_qa()
variants = json.load(open(W / 'variants.json', encoding='utf-8'))
xmap = json.load(open(W / 'X_map.json', encoding='utf-8'))
excluded = {s for s, _ in json.load(open(HERE / 'repair_excluded.json', encoding='utf-8'))}
groups = {1: [], 2: [], 3: [], 4: []}

def src_of(name, v):
    if v == 'X': return xmap[name]['mq5']
    return next((str(p) for p in (W / v).glob(f'c*/{name}.mq5')), None)

for name in list(variants) + list(xmap):
    b = best.get(name)
    if not b: continue
    rank, _, v, label, detail = b
    if label == 'working': continue
    src = src_of(name, v)
    if not src: continue
    g = {'not working': 2, 'working (alignment untestable)': 3, 'repaints': 4}[label]
    groups[g].append({'name': name, 'source': src, 'problem': f'runtime test: {label}', 'detail': f'Last test: {detail}'})

# still not compiling: Offline Converter sources (first AI pass failed or never reached)
done = {}
for line in open(W / 'R' / 'repair_results.jsonl', encoding='utf-8'):
    r = json.loads(line); done[r['name']] = r
for p in sorted(W.glob('O/c*/*.mq5')):
    name = p.stem
    if p.with_suffix('.ex5').exists() or name in xmap or name in excluded: continue
    prev = done.get(name, {}).get('path')
    src = prev if prev and Path(prev).exists() else str(p)
    groups[1].append({'name': name, 'source': src, 'problem': 'compile errors (earlier repair attempt did not finish)',
                      'detail': 'Run python c.py to see the current compiler errors.'})

jobs = groups[1] + groups[2] + groups[3] + groups[4]
json.dump(jobs, open(HERE / 'jobs_runtime.json', 'w', encoding='utf-8'), indent=0)
print({'not compiling': len(groups[1]), 'not working': len(groups[2]), 'untestable': len(groups[3]),
       'repaints': len(groups[4]), 'total': len(jobs)})
