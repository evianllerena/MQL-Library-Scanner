r"""Compare MT5 dumps against the MT4 reference dumps, bar by bar.
  F = full history (3000 bars), C = history ending 150 bars early, I = MT5 only: C, then the 150 bars appended one
  at a time with the indicator loaded (the way a backtest/live chart updates).
Every MT4 buffer that has values must be reproduced by some MT5 buffer (matched by content, so re-ordered buffers
are fine) on F and on C; I must equal MT4's F unless the MT4 original itself repaints (F vs C differ in MT4).
Bars before WARM and the last (still forming) bar are not compared. Values match if within 1e-6 relative; EMPTY
(MT4 2147483647 / MT5 DBL_MAX / NaN) matches EMPTY or 0.
Usage: compare.py [--names file] -> F:\MQLFIX_BUILD\DIFF\verdicts.json (alias -> verdict dict).
Also importable: verdict(alias, d5dir=None) for the repair loop."""
import json, sys
from pathlib import Path
import numpy as np
D = Path(r'F:\MQLFIX_BUILD\DIFF')
WARM, CUT, NEAR = 300, 150, 0.995

def load(path):
    if not path.exists(): return None
    raw = path.read_bytes()
    n, nb = np.frombuffer(raw[:8], '<i4')
    m = np.frombuffer(raw[8:8 + n * 16 * 8], '<f8').reshape(n, 16).copy()
    m[~np.isfinite(m) | (np.abs(m) >= 2147483647.0)] = np.nan
    return m

def match(a, b):
    """per-bar agreement of two 1-D arrays (NaN = empty; empty matches empty or 0)"""
    ea, eb = np.isnan(a), np.isnan(b)
    both = ~ea & ~eb
    ok = np.zeros(len(a), bool)
    ok[both] = np.abs(a[both] - b[both]) <= 1e-6 * np.maximum(1.0, np.abs(a[both]))
    ok |= (ea & eb) | (ea & (b == 0)) | (eb & (a == 0))
    return ok

_vis = None
def visible(alias):
    """MT4 '#property indicator_buffers' of the reference (what iCustom exposes); 16 when unknown (.ex4)"""
    global _vis
    if _vis is None:
        p = D / 'visible.json'
        _vis = json.load(open(p, encoding='utf-8')) if p.exists() else {}
    return _vis.get(alias, 16)

def pairing(m4, m5, lo, hi, nvis=16):
    """best MT5 buffer for each MT4 buffer with values in [lo, hi) -> {b4: (b5, score, first_bad)}.
    The same index is tried first; a different index only counts as 'reordered' (an EA reads buffers by index)."""
    out = {}
    for b in range(min(16, nvis)):
        col = m4[lo:hi, b]
        if np.all(np.isnan(col) | (col == 0)): continue
        best = (None, -1.0, None)
        for c in [b] + [x for x in range(16) if x != b]:
            ok = match(col, m5[lo:hi, c]); s = ok.mean()
            if s > best[1]:
                bad = np.flatnonzero(~ok)
                best = (c, float(s), int(lo + bad[0]) if len(bad) else None)
            if s == 1.0: break
        out[b] = best
    return out

def sample(m4, m5, b4, b5, at):
    return [(i, None if np.isnan(m4[i, b4]) else round(float(m4[i, b4]), 8), None if np.isnan(m5[i, b5]) else round(float(m5[i, b5]), 8))
            for i in range(at, min(at + 3, len(m4)))]

def objects(path):
    """multiset of drawn objects: type|time1|price1|time2|price2|x|y|text (object names ignored)"""
    from collections import Counter
    if not path.exists(): return Counter()
    rows = path.read_text(encoding='cp1252', errors='replace').splitlines()
    return Counter('|'.join(r.split('|', 7)) for r in rows if r.count('|') >= 7)

def verdict(alias, d5dir=None):
    d5 = Path(d5dir) if d5dir else D / 'd5'
    f4, c4 = load(D / 'd4' / f'{alias}.F.bin'), load(D / 'd4' / f'{alias}.C.bin')
    f5, c5, i5 = (load(d5 / f'{alias}.{t}.bin') for t in 'FCI')
    if f4 is None: return {'verdict': 'no_reference_run'}
    if f5 is None: return {'verdict': 'mt5_failed'}
    n = len(f4)
    nvis = visible(alias)
    pf = pairing(f4, f5, WARM, n - 1, nvis)
    if not pf:                                    # object-drawing indicator: compare the objects it drew on F
        o4, o5 = objects(D / 'd4' / f'{alias}.F.obj'), objects(d5 / f'{alias}.F.obj')
        if not o4: return {'verdict': 'reference_has_no_values'}
        common = sum((o4 & o5).values()); s = common / max(sum(o4.values()), sum(o5.values()))
        out = {'objects4': sum(o4.values()), 'objects5': sum(o5.values()), 'objects_match': round(s, 4)}
        if s < 1.0:
            out['missing_in_mt5'] = list((o4 - o5).elements())[:3]; out['extra_in_mt5'] = list((o5 - o4).elements())[:3]
        out['verdict'] = 'verified' if s == 1.0 else 'verified_near' if s >= NEAR else 'object_mismatch'
        return out
    out = {'buffers4': len(pf)}
    # MT4 original repaints / looks ahead by design: same bars differ between its F and C runs
    out['mt4_repaints'] = bool(c4 is not None and any(not match(f4[WARM:n - CUT - 1, b], c4[WARM:n - CUT - 1, b]).all() for b in pf))
    worst = min(s for _, s, _ in pf.values())
    bad = {b: v for b, v in pf.items() if v[1] < 1.0}
    out['F'] = round(worst, 4)
    if bad:
        b4, (b5, s, at) = min(bad.items(), key=lambda kv: kv[1][1])
        out['F_detail'] = {'mt4_buffer': b4, 'best_mt5_buffer': b5, 'match': round(s, 4), 'first_bad_bar': at,
                           'bars_total': n, 'sample(bar, mt4, mt5)': sample(f4, f5, b4, b5, at)}
    if c4 is not None and c5 is not None:
        pc = pairing(c4, c5, WARM, len(c4) - 1, nvis); out['C'] = round(min((s for _, s, _ in pc.values()), default=1.0), 4)
    else: out['C'] = None
    if i5 is not None:
        si = [match(f4[WARM:n - 1, b], i5[WARM:n - 1, c]).mean() for b, (c, _, _) in pf.items() if c is not None]
        out['I'] = round(float(min(si)) if si else 0.0, 4)
    else: out['I'] = None
    out['reordered'] = sorted(b for b, (c, s, _) in pf.items() if c != b and s >= NEAR)
    if out['reordered']: worst = 0.0
    exact = worst == 1.0 and (out['C'] in (None, 1.0))
    near = worst >= NEAR and (out['C'] is None or out['C'] >= NEAR)
    inc_ok = out['I'] is not None and (out['I'] >= NEAR or out['mt4_repaints'])
    if (exact or near) and inc_ok:
        out['verdict'] = 'verified' if exact and (out['I'] == 1.0 or out['mt4_repaints']) else 'verified_near'
    elif exact or near:
        out['verdict'] = 'bar_by_bar_mismatch'
    else:
        out['verdict'] = 'mismatch'
    return out

if __name__ == '__main__':
    aliases = json.load(open(D / 'aliases.json', encoding='utf-8'))
    only = set(l.strip() for l in open(sys.argv[sys.argv.index('--names') + 1], encoding='utf-8') if l.strip()) if '--names' in sys.argv else None
    res = {}
    for a in aliases:
        if only is not None and a not in only: continue
        if not (D / 'd4' / f'{a}.F.bin').exists() and not (D / 'd5' / f'{a}.F.bin').exists(): continue
        res[a] = verdict(a)
    out = D / ('verdicts.json' if only is None else 'verdicts_subset.json')
    json.dump(res, open(out, 'w', encoding='utf-8'), indent=0)
    from collections import Counter
    print(Counter(v['verdict'] for v in res.values()).most_common())
