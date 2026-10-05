r"""Final selection + packaging into F:\Fixed MQL5 Indicators (inputs are never modified).

Per unique indicator, from all QA results (qa_<V>*.jsonl for V in A, B, X, RA, RB ...):
  Working   -- loads + calculates on EURUSD and GBPJPY, real values, aligned (no look-ahead / mirroring)
  Repaints  -- works, but past values change when new bars arrive (sparse differences, e.g. ZigZag)
  Not working -- crashed / never calculated / no values / misaligned in every tested version
  Not compiling -- no version compiles
  Excluded  -- trading tools, Windows-DLL dependent, no entry point (listed, original copied)
Preference among versions: aligned > repaints > failing; ties -> A (unchanged) over B (flipped) over X/R.
Writes <dest>\{Working,Repaints,Not working,Not compiling,Excluded\...}\ and REPORT.csv."""
import csv, json, shutil, sys
from collections import Counter
from pathlib import Path

W = Path(r'F:\MQLFIX_BUILD')
DEST = Path(sys.argv[1] if len(sys.argv) > 1 else r'F:\Fixed MQL5 Indicators')
HERE = Path(__file__).resolve().parent
ORDER = ['A', 'B', 'X', 'RA', 'RB']          # variant preference on ties

def verdict(r):
    """Classify one QA record -> (rank, label, detail); lower rank is better."""
    if r.get('load') != 'ok':
        return 3, 'not working', f"load: {r.get('load')}"
    if not r.get('has_values'):
        return 3, 'not working', 'no real values (empty or constant buffers)'
    d = r.get('align_detail') or {}
    if r.get('align') == 'aligned':
        return 0, 'working', 'aligned'
    if d.get('compared'):
        ratio = d['diffs'] / d['compared']
        if ratio < 0.5:
            return 1, 'repaints', f"repaints: {d['diffs']}/{d['compared']} past values change"
        return 3, 'not working', f"misaligned: {d['diffs']}/{d['compared']} past values differ"
    return 2, 'working (alignment untestable)', f"alignment test: {r.get('align')}"

def load_qa():
    best = {}                                 # name -> (rank, variant_index, variant, label, detail)
    for v in ORDER:
        for f in W.glob(f'qa_{v}_s*.jsonl'):
            for line in open(f, encoding='utf-8', errors='replace'):
                try: r = json.loads(line)
                except Exception: continue
                if 'id' not in r or 'load' not in r: continue
                rank, label, detail = verdict(r)
                cand = (rank, ORDER.index(v), v, label, detail)
                if r['id'] not in best or cand < best[r['id']]:
                    best[r['id']] = cand
    return best

def ex5_for(v, name):
    hits = list((W / v).glob(f'c*/{name}.ex5'))
    return hits[0] if hits else None

if __name__ == '__main__':
    variants = json.load(open(W / 'variants.json', encoding='utf-8'))
    xmap = json.load(open(W / 'X_map.json', encoding='utf-8')) if (W / 'X_map.json').exists() else {}
    excluded = json.load(open(HERE / 'repair_excluded.json', encoding='utf-8'))
    best = load_qa()
    rows = []
    folders = {'working': 'Working', 'repaints': 'Repaints', 'working (alignment untestable)': 'Working - alignment untestable',
               'not working': 'Not working', 'not compiling': 'Not compiling'}
    def emit(name, original, status, detail, src_mq5=None, src_ex5=None):
        sub = DEST / folders.get(status, status)
        sub.mkdir(parents=True, exist_ok=True)
        if src_mq5 and Path(src_mq5).exists(): shutil.copy2(src_mq5, sub / f'{name}.mq5')
        if src_ex5 and Path(src_ex5).exists(): shutil.copy2(src_ex5, sub / f'{name}.ex5')
        rows.append({'indicator': name, 'status': status, 'detail': detail, 'original': original})
    for name, rec in variants.items():
        b = best.get(name)
        if not b:
            emit(name, rec['original'], 'not compiling' if not ex5_for('A', name) else 'not working',
                 'no compiled version' if not ex5_for('A', name) else 'not runtime-tested', None, None); continue
        rank, _, v, label, detail = b
        mq5 = next((W / v).glob(f'c*/{name}.mq5'), None)
        emit(name, rec['original'], label, f'{detail} (version {v})', mq5, ex5_for(v, name))
    for name, info in xmap.items():           # Offline-Converter originals: compiled directly or AI-repaired
        b = best.get(name)
        if b:
            emit(info['name'], info['original'], b[3], f"{b[4]} ({info['how']})", info['mq5'], info['ex5'])
        else:
            emit(info['name'], info['original'], 'not compiling' if not info.get('ex5') else 'not working',
                 info.get('why', 'not runtime-tested'), info.get('mq5'), info.get('ex5'))
    labels = {'trading': ('Excluded/Trading tools (not indicators)', 'uses the MT4 order API'),
              'dll': ('Excluded/Needs Windows DLL', 'imports a Windows DLL'),
              'no_entry': ('Excluded/Not an indicator', 'no OnCalculate/start/OnInit')}
    for stem, why in excluded:
        folder, detail = labels[why]
        src = next(W.glob(f'O/c*/{stem}.mq5'), None)
        emit(stem, str(src) if src else '', folder, detail, src, None)
    with open(DEST / 'REPORT.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['indicator', 'status', 'detail', 'original'])
        w.writeheader(); w.writerows(sorted(rows, key=lambda r: (r['status'], r['indicator'].lower())))
    print(Counter(r['status'] for r in rows).most_common())
