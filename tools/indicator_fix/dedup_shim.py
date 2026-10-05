r"""Remove duplicate function definitions created by converting an already-converted source.

2,103 of the 2,482 originals MQL_ONE never repaired were themselves produced by an earlier tool
("Offline Converter v3.0.3") that embeds its own MQL4_* helpers (MQL4_Bid, MQL4_iMA, ...). MQL_ONE's
converter injects its own copies on top, so the helpers are defined twice (MetaEditor error 165).
Rule: for every function defined more than once with the same name and parameter count, keep ONE
definition -- the one that appears verbatim in the original source if any, otherwise the first --
and replace the others with a marker comment. Bytes and line endings are preserved.
Usage: dedup_shim.py <in_dir> <out_dir> [manifest.json for originals]"""
import json, re, sys
from pathlib import Path

FUNC = re.compile(r'(?m)^[ \t]*(?:static\s+)?(?:const\s+)?[A-Za-z_]\w*(?:\s*\[\s*\])?\s+&?\s*([A-Za-z_]\w*)\s*\(([^;{}()]*(?:\([^()]*\)[^;{}()]*)*)\)\s*(?:const\s*)?\{')
KEYWORDS = {'if', 'for', 'while', 'switch', 'return', 'else', 'do'}

def block_end(text, brace):
    depth = 0
    for j in range(brace, len(text)):
        if text[j] == '{': depth += 1
        elif text[j] == '}':
            depth -= 1
            if depth == 0: return j + 1
    return len(text)

def nparams(sig):
    sig = re.sub(r'\([^()]*\)', '', sig).strip()
    return 0 if not sig or sig == 'void' else sig.count(',') + 1

def norm(s):
    return re.sub(r'\s+', '', s)

def dedup(text, original=''):
    defs = []
    for m in FUNC.finditer(text):
        if m.group(1) in KEYWORDS: continue
        end = block_end(text, m.end() - 1)
        defs.append((m.start(), end, m.group(1), nparams(m.group(2))))
    groups = {}
    for d in defs: groups.setdefault((d[2], d[3]), []).append(d)
    orig_norm = norm(original)
    cuts = []
    for key, ds in groups.items():
        if len(ds) < 2: continue
        verbatim = [d for d in ds if orig_norm and norm(text[d[0]:d[1]]) in orig_norm]
        keep = verbatim[0] if verbatim else ds[0]
        cuts += [d for d in ds if d is not keep]
    removed = []
    for a, b, name, _ in sorted(cuts, reverse=True):
        nl = '\r\n' if '\r\n' in text[max(0, a - 2):a + 200] else '\n'
        text = text[:a] + f'// [dedup] duplicate {name}() removed{nl}' + text[b:]
        removed.append(name)
    return text, sorted(set(removed))

def read(p):
    raw = Path(p).read_bytes()
    enc = 'utf-16' if raw[:2] in (b'\xff\xfe', b'\xfe\xff') else 'utf-8-sig' if raw[:3] == b'\xef\xbb\xbf' else 'utf-8'
    return raw.decode(enc, errors='replace'), enc

if __name__ == '__main__':
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    originals = {}
    if len(sys.argv) > 3:
        items = json.load(open(sys.argv[3], encoding='utf-8'))['items']
        originals = {v['hash'][:8]: v['original'] for v in items.values()}
    total = changed = 0
    for p in sorted(src.rglob('*.mq5')):
        text, enc = read(p)
        orig_path = originals.get(p.stem.rsplit('__', 1)[-1])
        original = read(orig_path)[0] if orig_path else ''
        new, removed = dedup(text, original)
        out = dst / p.relative_to(src); out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(new.encode(enc))
        total += 1; changed += bool(removed)
    print('files', total, 'deduplicated', changed)
