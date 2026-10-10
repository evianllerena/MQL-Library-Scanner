r"""Find the MT4 reference for every non-excluded indicator in F:\Fixed MQL5 Indicators\REPORT.csv.
Candidates: every .mq4/.ex4 on F: whose normalized name equals the original's (copies are named "x_2", "x (1)",
"x_www_fx1618_com", ...). Several candidates -> the .mq4 whose extern/input declarations best match the
converted source wins; ties are content-identical copies. .ex4 (compiled MT4, no source) is used only when
no .mq4 exists. Writes F:\MQLFIX_BUILD\DIFF\pairs.json: name -> {status, ex5, mq5, original, ref, ref_kind, score}."""
import csv, json, os, re
from collections import Counter
FX, D = r'F:\Fixed MQL5 Indicators', r'F:\MQLFIX_BUILD\DIFF'
FOLDER = {'working': 'Working', 'repaints': 'Repaints', 'working (alignment untestable)': 'Working - alignment untestable',
          'not working': 'Not working', 'not compiling': 'Not compiling'}
def norm(s):
    s = s.lower().replace('%20', ' ')
    s = re.sub(r'_?www_fx1618_com', '', s); s = re.sub(r'\[[^\]]*\]', '', s)
    s = re.sub(r'(\s*\(\d+\))+$', '', s); s = re.sub(r'(_\d{1,2})+$', '', s)
    return re.sub(r'[^a-z0-9]', '', s)
def text(p):
    b = open(p, 'rb').read()
    return b.decode('utf-16', 'replace') if b[:2] in (b'\xff\xfe', b'\xfe\xff') else b.decode('utf-8', 'replace')
DECL = re.compile(r'^\s*(?:extern|input|sinput)\s+[\w ]+?\s+(\w+)\s*=\s*([^;]+);', re.M)
def inputs(p):
    return {(n.lower(), re.sub(r'^clr', '', re.sub(r'\s+', '', v).lower())) for n, v in DECL.findall(text(p))}
def names(s): return {n for n, _ in s}
def index(listing):
    d = {}
    for l in open(os.path.join(D, listing), encoding='utf-8', errors='replace'):
        p = 'F:' + l.strip()[2:]; d.setdefault(norm(os.path.basename(p)[:-4]), []).append(p)
    return d
if __name__ == '__main__':
    mq4, ex4 = index('mq4_all.txt'), index('ex4_all.txt')
    pairs, c = {}, Counter()
    for r in csv.DictReader(open(os.path.join(FX, 'REPORT.csv'), encoding='utf-8')):
        if r['status'].startswith('Excluded'): continue
        ex5 = os.path.join(FX, FOLDER[r['status']], r['indicator'] + '.ex5')
        key = norm(os.path.splitext(os.path.basename(r['original']))[0])
        ref, kind, score = None, None, None
        if key in mq4:
            mine = inputs(r['original']) if os.path.exists(r['original']) else set()
            def rank(p):
                t = inputs(p); return (len(names(mine) & names(t)) - len(names(mine) ^ names(t)), len(mine & t), 'Indicator Scrape' in p)
            best = max(mq4[key], key=rank)
            ref, kind = best, 'mq4'
            a, b = names(mine), names(inputs(best)); score = round(len(a & b) / max(1, len(a | b)), 2) if a | b else 1.0
        elif key in ex4:
            ref, kind = sorted(ex4[key])[0], 'ex4'
        c[kind] += 1
        pairs[r['indicator']] = {'status': r['status'], 'ex5': ex5 if os.path.exists(ex5) else None, 'mq5': ex5[:-4] + '.mq5',
                                 'original': r['original'], 'ref': ref, 'ref_kind': kind, 'score': score}
    json.dump(pairs, open(os.path.join(D, 'pairs.json'), 'w', encoding='utf-8'), indent=0)
    print(dict(c), 'input-match score<0.5:', sum(1 for v in pairs.values() if v['score'] is not None and v['score'] < 0.5))
