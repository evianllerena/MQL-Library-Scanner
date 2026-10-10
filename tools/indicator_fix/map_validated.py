r"""Map each unique original (inventory.json) to an existing MQL_ONE-repaired conversion:
VALIDATED/<id>/<name>.mq5 (via evidence.json original_hash / origin) or a Ready top-level conversion (by name)."""
import json, os, re, time
HERE = os.path.dirname(os.path.abspath(__file__))
inv = json.load(open(os.path.join(HERE, 'inventory.json')))
by_hash = {r['hash']: r for r in inv['originals']}
by_path = {p.lower(): r['hash'] for r in inv['originals'] for p in r['paths']}
VAL = r'F:\Ready MQL5 Indicators\MQL_ONE\OUTPUT\VALIDATED'
t = time.time(); found = {}; unmatched_ev = 0
def item_dirs():
    for d in os.listdir(VAL):
        dp = os.path.join(VAL, d)
        if d.startswith('BATCH_'):
            for d2 in os.listdir(dp): yield os.path.join(dp, d2)
        else:
            yield dp
for dp in item_dirs():
    try:
        ev = json.load(open(os.path.join(dp, 'evidence.json'), encoding='utf-8'))
    except Exception:
        continue
    oh = ev.get('original_hash')
    if oh not in by_hash:
        oh = by_path.get(str(ev.get('origin', '')).lower())
    mq5 = [f for f in os.listdir(dp) if f.lower().endswith('.mq5')]
    if not oh or not mq5:
        unmatched_ev += 1; continue
    found.setdefault(oh, []).append(os.path.join(dp, mq5[0]))
print('validated dirs mapped to originals:', sum(len(v) for v in found.values()), 'unique originals covered:', len(found), 'unmatched dirs:', unmatched_ev, round(time.time() - t), 's')
orph = json.load(open(os.path.join(HERE, 'orphans.json')))['orphan_matched']
def norm(n):
    n = os.path.splitext(n)[0].lower(); n = re.sub(r'__[0-9a-f]{8}$', '', n); n = re.sub(r'(__\d+)+$', '', n)
    n = re.sub(r'_www_fx1618_com$', '', n); return re.sub(r'[\s_\-\(\)\[\]]+', '', n)
name_to_hash = {}
for r in inv['originals']:
    for n in r['names']: name_to_hash.setdefault(norm(n), []).append(r['hash'])
ready_cov = {}
for p in orph:
    for h in name_to_hash.get(norm(os.path.basename(p)), []):
        ready_cov.setdefault(h, []).append(p)
covered = set(found) | set(ready_cov)
print('originals with a VALIDATED conversion:', len(found), '| with a Ready conversion:', len(ready_cov), '| covered by either:', len(covered), 'of', len(by_hash))
json.dump({'validated': found, 'ready': ready_cov}, open(os.path.join(HERE, 'conversions.json'), 'w'), indent=0)
