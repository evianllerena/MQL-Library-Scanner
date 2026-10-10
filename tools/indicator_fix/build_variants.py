r"""Build the two variants of every indicator that already has an MQL_ONE-repaired conversion:
  A = the existing conversion, unchanged
  B = the same file with ArraySetAsSeries(buf,true) for each unflipped SetIndexBuffer array, inserted at
      the top of OnCalculate (MT4 code indexes buffers newest-first; MT5 buffers default oldest-first).
Runtime QA later keeps whichever variant passes the no-look-ahead test. Space-free work dir (MetaEditor
cuts paths at spaces): F:\MQLFIX_BUILD\{A,B}\<name>.mq5 + variants.json."""
import json, os, re, sys
from pathlib import Path
sys.path.insert(0, r'C:\Users\Evision\AppData\Local\Temp\claude\C--Users-Evision\c27ab46c-7c67-48b8-915c-a0ab7cb2913a\scratchpad\mqlone\src\MQL_ONE_FIXED_v5.9.19\src\modules')
import textio
HERE = Path(__file__).resolve().parent
W = Path(r'F:\MQLFIX_BUILD')
(W / 'A').mkdir(parents=True, exist_ok=True); (W / 'B').mkdir(parents=True, exist_ok=True)
inv = {r['hash']: r for r in json.load(open(HERE / 'inventory.json'))['originals']}
conv = json.load(open(HERE / 'conversions.json'))
IDENT = re.compile(r'^[A-Za-z_]\w*$')

def flip(text):
    bufs = []
    for m in re.finditer(r'\bSetIndexBuffer\s*\(\s*[^,()]+?\s*,\s*([^,()]+?)\s*[,)]', text):
        b = m.group(1).strip()
        if b not in bufs: bufs.append(b)
    todo = [b for b in bufs if IDENT.match(b) and not re.search(r'ArraySetAsSeries\s*\(\s*' + re.escape(b) + r'\s*,\s*true', text)]
    skipped = [b for b in bufs if not IDENT.match(b)]
    if not todo:
        return None, bufs, skipped
    lines = ''.join(f'   ArraySetAsSeries({b},true);\n' for b in todo)
    for anchor in (r'(__mql4_prev_calculated\s*=\s*prev_calculated\s*;[ \t]*\r?\n)',
                   r'(__mqlone_prev_calculated\s*=\s*prev_calculated\s*;[ \t]*\r?\n)',
                   r'(\bint\s+OnCalculate\s*\([^)]*\)\s*\{[ \t]*\r?\n)'):
        new, n = re.subn(anchor, lambda m: m.group(1) + lines, text, count=1)
        if n:
            return new, bufs, skipped
    return None, bufs, skipped

out = {}; used = set(); nB = 0
for h, rec in sorted(inv.items(), key=lambda kv: kv[1]['names'][0].lower()):
    src = None
    if h in conv['validated']:
        src = max(conv['validated'][h], key=os.path.getmtime)
    elif h in conv['ready']:
        src = conv['ready'][h][0]
    if not src:
        continue
    stem = re.sub(r'[^\w\-\.#!&+ ]', '_', Path(rec['names'][0]).stem).strip() or 'indicator'
    stem = stem.replace(' ', '_')                  # keep MetaEditor/iCustom paths space-free
    name = stem if stem.lower() not in used else f'{stem}__{h[:8]}'
    used.add(name.lower())
    text = textio.read_source_text(Path(src))
    textio.write_source_text(W / 'A' / f'{name}.mq5', text)
    b_text, bufs, skipped = flip(text)
    if b_text:
        textio.write_source_text(W / 'B' / f'{name}.mq5', b_text); nB += 1
    out[name] = {'hash': h, 'original': rec['paths'][0], 'names': rec['names'], 'conversion': src,
                 'buffers': bufs, 'flip_skipped_nonident': skipped, 'has_B': bool(b_text)}
json.dump(out, open(W / 'variants.json', 'w', encoding='utf-8'), indent=1)
print('A variants', len(out), 'B variants', nB)
