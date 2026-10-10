r"""Inventory: every unique indicator source in F:\Converted Indicators and F:\Ready MQL5 Indicators (top level),
resolved to its ORIGINAL MT4 source. Writes inventory.json."""
import glob, hashlib, json, os, time
OUT = os.path.dirname(os.path.abspath(__file__))
def sha(b): return hashlib.sha256(b).hexdigest()
t = time.time()
# 1) raw MT4 sources from Converted Indicators
items = {}   # original_hash -> record
def add_original(path, data, origin_tag):
    h = sha(data)
    rec = items.setdefault(h, {'hash': h, 'names': [], 'paths': [], 'sources': set()})
    n = os.path.basename(path)
    if n not in rec['names']: rec['names'].append(n)
    rec['paths'].append(path); rec['sources'].add(origin_tag)
    return h
conv = glob.glob(r'F:\Converted Indicators\*.mq5')
for p in conv:
    add_original(p, open(p, 'rb').read(), 'converted')
print('converted files', len(conv), 'unique originals so far', len(items), round(time.time() - t), 's', flush=True)
# 2) VALIDATED evidence: converted-output hash -> original hash
val_map = {}
for ev in glob.glob(r'F:\Ready MQL5 Indicators\MQL_ONE\OUTPUT\VALIDATED\*\evidence.json'):
    try:
        e = json.load(open(ev, encoding='utf-8'))
        d = os.path.dirname(ev)
        for m in glob.glob(os.path.join(d, '*.mq5')):
            val_map[sha(open(m, 'rb').read())] = e.get('original_hash')
    except Exception:
        pass
print('validated outputs mapped', len(val_map), round(time.time() - t), 's', flush=True)
# 3) Ready top level: raw ones are originals; converted ones map back via VALIDATED evidence
ready_raw = ready_mapped = ready_orphan = 0
orphans = []
for p in glob.glob(r'F:\Ready MQL5 Indicators\*.mq5'):
    data = open(p, 'rb').read()
    if b'MQLONE_COMPAT_SHIM' not in data:
        add_original(p, data, 'ready_raw'); ready_raw += 1
        continue
    oh = val_map.get(sha(data))
    if oh and oh in items:
        items[oh]['sources'].add('ready_converted'); items[oh].setdefault('ready_converted', []).append(p); ready_mapped += 1
    else:
        orphans.append(p); ready_orphan += 1
print(f'ready: raw={ready_raw} converted->original={ready_mapped} converted-without-original={ready_orphan}', flush=True)
for r in items.values(): r['sources'] = sorted(r['sources'])
json.dump({'originals': list(items.values()), 'ready_orphans': orphans}, open(os.path.join(OUT, 'inventory.json'), 'w'), indent=0)
print('unique originals', len(items), 'orphans', len(orphans), 'seconds', round(time.time() - t))
