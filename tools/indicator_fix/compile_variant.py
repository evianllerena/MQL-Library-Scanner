r"""Compile every .mq5 of one variant folder (F:\MQLFIX_BUILD\<V>) in chunks of 500, one MetaEditor folder-compile
per chunk. Writes <V>_compile.json: {name: {'ok': bool, 'errors': n, 'first_errors': [...]}}."""
import json, os, re, shutil, subprocess, sys, time
from pathlib import Path
V = sys.argv[1]
root = Path(r'F:\MQLFIX_BUILD') / V
ME = r'C:\Program Files\MetaTrader 5\MetaEditor64.exe'
files = sorted(p for p in root.glob('*.mq5'))
for i in range(0, len(files), 500):           # move loose files into chunk folders
    d = root / f'c{i // 500:03d}'; d.mkdir(exist_ok=True)
    for p in files[i:i + 500]: shutil.move(str(p), str(d / p.name))
results = {}
chunks = sorted(d for d in root.iterdir() if d.is_dir() and d.name.startswith('c'))
t = time.time()
for d in chunks:
    log = root / f'{d.name}.log'
    if not log.exists():
        subprocess.run([ME, f'/compile:{d}', f'/log:{log}'], timeout=3600)
    text = log.read_text(encoding='utf-16', errors='replace') if log.exists() else ''
    cur = None
    for line in text.splitlines():
        m = re.search(r"compiling '([^']+)'", line)
        if m: cur = Path(m.group(1)).stem; results.setdefault(cur, {'errors': 0, 'first_errors': []}); continue
        if cur and ' error ' in line:
            r = results[cur]; r['errors'] += 1
            if len(r['first_errors']) < 3: r['first_errors'].append(line.split(' : ', 1)[-1][:160])
    for p in d.glob('*.mq5'):
        r = results.setdefault(p.stem, {'errors': 0, 'first_errors': []})
        ex = p.with_suffix('.ex5'); r['ok'] = ex.exists() and ex.stat().st_size > 0
    print(V, d.name, 'done', round(time.time() - t), 's', flush=True)
json.dump(results, open(Path(r'F:\MQLFIX_BUILD') / f'{V}_compile.json', 'w'), indent=0)
print(V, 'compiled ok', sum(1 for r in results.values() if r.get('ok')), 'of', len(results), round(time.time() - t), 's')
