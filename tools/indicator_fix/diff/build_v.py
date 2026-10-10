r"""Working tree V for the MT4-difference repairs: F:\MQLFIX_BUILD\V\<chunk>\<alias>.mq5|.ex5, one per pair with an MT5 source.
Mechanical fix applied here: conversions without '#property indicator_plots' expose only buffer 0 to iCustom/CopyBuffer
(an EA cannot read the other lines). Insert indicator_plots = MT4's indicator_buffers (the visible buffers) and raise
indicator_buffers to MQL4's IndicatorBuffers(M) when M is larger (calculation buffers).
Patched files go to p### chunks and are compiled; unchanged ones are copied with their .ex5 to k### chunks.
Writes V\map.json: alias -> {"mq5", "ex5", "patched", "compiled"}."""
import json, re, shutil, subprocess
from pathlib import Path
D = Path(r'F:\MQLFIX_BUILD\DIFF'); V = Path(r'F:\MQLFIX_BUILD\V')
ME = r'C:\Program Files\MetaTrader 5\MetaEditor64.exe'
INC = r'C:\Users\Evision\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\MQL5'
PROP = re.compile(r'^([ \t]*#property[ \t]+indicator_buffers[ \t]+)(\d+)', re.M)

def patch(s):
    if re.search(r'^\s*#property\s+indicator_plots', s, re.M): return None
    m = PROP.search(s)
    if not m: return None
    n = int(m.group(2)); calc = [int(x) for x in re.findall(r'(?:MQL4_)?IndicatorBuffers\s*\(\s*(\d+)\s*\)', s)]
    total = max([n] + calc)
    return s[:m.start()] + f'{m.group(1)}{total}\r\n#property indicator_plots {n}' + s[m.end():]

if __name__ == '__main__':
    pairs = json.load(open(D / 'pairs.json', encoding='utf-8'))
    aliases = json.load(open(D / 'aliases.json', encoding='utf-8'))
    amap, patched, kept = {}, [], []
    for a, name in aliases.items():
        v = pairs[name]
        src = Path(v['mq5'])
        if not src.exists(): continue
        raw = src.read_bytes()
        enc = 'utf-16' if raw[:2] == b'\xff\xfe' else 'utf-8'
        new = patch(raw.decode(enc, 'replace'))
        (patched if new is not None else kept).append((a, src, new, enc))
    for group, prefix in ((patched, 'p'), (kept, 'k')):
        for i, (a, src, new, enc) in enumerate(group):
            d = V / f'{prefix}{i // 500:03d}'; d.mkdir(parents=True, exist_ok=True)
            dst = d / f'{a}.mq5'
            if new is None:
                shutil.copy2(src, dst)
                if src.with_suffix('.ex5').exists(): shutil.copy2(src.with_suffix('.ex5'), dst.with_suffix('.ex5'))
            else:
                dst.write_bytes(new.encode('utf-8-sig' if enc == 'utf-8' else 'utf-16'))
            amap[a] = {'mq5': str(dst), 'ex5': str(dst.with_suffix('.ex5')), 'patched': new is not None}
    for d in sorted(V.glob('p*')):
        subprocess.run([ME, f'/compile:{d}', f'/inc:{INC}', f'/log:{V / (d.name + ".log")}'], timeout=3600)
        print(d.name, 'compiled', flush=True)
    for a, r in amap.items():
        ex = Path(r['ex5']); r['compiled'] = ex.exists() and ex.stat().st_size > 0
    json.dump(amap, open(V / 'map.json', 'w', encoding='utf-8'), indent=0)
    print('patched', len(patched), 'compiled ok', sum(1 for r in amap.values() if r['patched'] and r['compiled']),
          '| unchanged', len(kept), 'with ex5', sum(1 for r in amap.values() if not r['patched'] and r['compiled']))
