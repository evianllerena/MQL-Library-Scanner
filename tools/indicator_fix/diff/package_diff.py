r"""Package the MT4-verified library into F:\Fixed MQL5 Indicators (built beside it, then swapped in; the previous
package is kept as F:\MQLFIX_BUILD\previous_fixed_<date>). Sources are never modified.
Folders:
  Verified - matches MT4                    every buffer (or drawn object) equals the MQL4 original in MT4, bar by bar,
                                            on full history, on shorter history and when bars arrive one at a time
  Verified - repaints like the MT4 original same, and the MT4 original itself repaints / looks ahead by design
  Unfixable - differs from MT4              still differs after 3 AI repair attempts (detail: first difference)
  Pending repair                            differs, fewer than 3 attempts made yet (run stopped early)
  No MT4 original\<old status>              no MQL4 source/.ex4 on disk, or the original produces nothing in MT4:
                                            only the earlier runtime checks apply (Working / Repaints / Not working ...)
  Excluded\...                              unchanged from the previous package (DLL, trading tools, not indicators)
REPORT.csv: indicator, status, detail, mt4_reference, reference_confidence, attempts."""
import csv, json, re, shutil, subprocess, sys, time
from pathlib import Path
from build_v import patch, ME, INC
D = Path(r'F:\MQLFIX_BUILD\DIFF'); RD = Path(r'F:\MQLFIX_BUILD\RD'); V2 = Path(r'F:\MQLFIX_BUILD\V2')
FX = Path(r'F:\Fixed MQL5 Indicators'); NEW = Path(r'F:\Fixed MQL5 Indicators.new')
OLD = {'working': 'Working', 'repaints': 'Repaints', 'working (alignment untestable)': 'Working - alignment untestable',
       'not working': 'Not working', 'not compiling': 'Not compiling'}
pairs = json.load(open(D / 'pairs.json', encoding='utf-8'))
aliases = json.load(open(D / 'aliases.json', encoding='utf-8')); byname = {n: a for a, n in aliases.items()}
verdicts = json.load(open(D / 'verdicts.json', encoding='utf-8'))
vmap = json.load(open(Path(r'F:\MQLFIX_BUILD\V\map.json'), encoding='utf-8'))
rep = {}
if (RD / 'results.jsonl').exists():
    for line in open(RD / 'results.jsonl', encoding='utf-8'):
        r = json.loads(line); rep.setdefault(r['alias'], []).append(r)

# indicators without an MT4 verdict still get the mechanical indicator_plots fix (compiled in V2)
def plots_fixed(name, mq5):
    src = Path(mq5)
    if not src.exists(): return None
    raw = src.read_bytes(); enc = 'utf-16' if raw[:2] == b'\xff\xfe' else 'utf-8'
    new = patch(raw.decode(enc, 'replace'))
    if new is None: return None
    d = V2 / f'c{abs(hash(name)) % 40:02d}'; d.mkdir(parents=True, exist_ok=True)
    dst = d / (re.sub(r'[^\w\-+.!#]', '_', name) + '.mq5')
    dst.write_bytes(new.encode('utf-8-sig' if enc == 'utf-8' else 'utf-16'))
    return dst

rows, todo_v2 = [], {}
def emit(name, folder, detail, mq5, ex5, v):
    sub = NEW / folder; sub.mkdir(parents=True, exist_ok=True)
    if mq5 and Path(mq5).exists(): shutil.copy2(mq5, sub / f'{name}.mq5')
    if ex5 and Path(ex5).exists(): shutil.copy2(ex5, sub / f'{name}.ex5')
    att = len(rep.get(byname.get(name), []))
    rows.append({'indicator': name, 'status': folder, 'detail': detail, 'mt4_reference': v.get('ref') or '',
                 'reference_confidence': '' if v.get('score') is None else ('low (inputs differ)' if v['score'] < 0.5 else 'high'),
                 'attempts': att})

for name, v in pairs.items():
    a = byname.get(name); vd = verdicts.get(a, {}) if a else {}
    h = rep.get(a, [])
    win = next((r for r in h if r['passes']), None)
    k = vd.get('verdict')
    if win:
        chk = next((c for c in win.get('check', []) if c.startswith('compare:')), '')
        rp = '"mt4_repaints": true' in chk
        emit(name, 'Verified - repaints like the MT4 original' if rp else 'Verified - matches MT4',
             f"AI-repaired (attempt {win['attempt']}), re-verified against MT4", win['path'], win['path'][:-4] + '.ex5', v)
    elif k in ('verified', 'verified_near'):
        emit(name, 'Verified - repaints like the MT4 original' if vd.get('mt4_repaints') else 'Verified - matches MT4',
             f"F={vd.get('F', vd.get('objects_match'))} C={vd.get('C')} I={vd.get('I')}", vmap[a]['mq5'], vmap[a]['ex5'], v)
    elif a and k and k not in ('no_reference_run', 'reference_has_no_values'):
        last = h[-1] if h else None
        why = (last['agent'] if last else '') + ' | ' + json.dumps(vd.get('F_detail') or {k2: vd.get(k2) for k2 in ('objects_match', 'missing_in_mt5')})
        src = Path(last['path']) if last else Path(vmap.get(a, {}).get('mq5', ''))
        emit(name, 'Unfixable - differs from MT4' if len(h) >= 3 else 'Pending repair', f'{k}: {why}'[:500],
             src, src.with_suffix('.ex5'), v)
    else:
        reason = {'no_reference_run': 'MT4 original hangs or fails to load in MT4',
                  'reference_has_no_values': 'MT4 original produces no values or objects'}.get(k, 'no MQL4 original on disk')
        folder = f"No MT4 original\\{OLD[v['status']]}"
        fixed = plots_fixed(name, v['mq5'])
        if fixed: todo_v2[name] = (fixed, folder, reason, v)
        else: emit(name, folder, reason, v['mq5'], v['ex5'], v)

for d in sorted(V2.glob('c*')):
    subprocess.run([ME, f'/compile:{d}', f'/inc:{INC}', f'/log:{V2 / (d.name + ".log")}'], timeout=3600)
for name, (fixed, folder, reason, v) in todo_v2.items():
    ok = fixed.with_suffix('.ex5').exists()
    if ok: emit(name, folder, reason + '; indicator_plots fixed', fixed, fixed.with_suffix('.ex5'), v)
    else: emit(name, folder, reason, v['mq5'], v['ex5'], v)
if (FX / 'Excluded').exists(): shutil.copytree(FX / 'Excluded', NEW / 'Excluded', dirs_exist_ok=True)
for r in csv.DictReader(open(FX / 'REPORT.csv', encoding='utf-8')):
    if r['status'].startswith('Excluded'):
        rows.append({'indicator': r['indicator'], 'status': r['status'], 'detail': r['detail'], 'mt4_reference': '',
                     'reference_confidence': '', 'attempts': 0})
with open(NEW / 'REPORT.csv', 'w', newline='', encoding='utf-8') as f:
    w = csv.DictWriter(f, fieldnames=['indicator', 'status', 'detail', 'mt4_reference', 'reference_confidence', 'attempts'])
    w.writeheader(); w.writerows(sorted(rows, key=lambda r: (r['status'], r['indicator'].lower())))
keep = Path(r'F:\MQLFIX_BUILD') / f"previous_fixed_{time.strftime('%Y%m%d_%H%M')}"
FX.rename(keep); NEW.rename(FX)
from collections import Counter
print(Counter(r['status'].split('\\')[0] for r in rows).most_common(), '| previous package kept at', keep)
