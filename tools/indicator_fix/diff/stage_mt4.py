r"""Stage MT4 references into MT4 portable copy <m4dir>: MQL4\Indicators\DIFF\<alias>.mq4|.ex4 for every pair, plus
any custom indicator a reference loads with iCustom("<name>") copied into MQL4\Indicators\<name>.mq4|.ex4 (looked up
by normalized name). Then compiles both folders with MT4's MetaEditor. Writes DIFF\aliases.json (alias -> name).
Usage: stage_mt4.py <m4dir>"""
import hashlib, json, os, re, shutil, subprocess, sys
from pathlib import Path
from map_ref import norm, text, index, D
M4 = Path(sys.argv[1]); IND = M4 / 'MQL4' / 'Indicators'; DST = IND / 'DIFF'; DST.mkdir(parents=True, exist_ok=True)
def alias(name):
    return name if re.fullmatch(r'[\x20-\x7e]+', name) and '|' not in name else 'u_' + hashlib.sha1(name.encode('utf-8')).hexdigest()[:12]
pairs = json.load(open(os.path.join(D, 'pairs.json'), encoding='utf-8'))
mq4, ex4 = index('mq4_all.txt'), index('ex4_all.txt')
aliases, deps = {}, set()
for name, v in pairs.items():
    if not v['ref']: continue
    a = alias(name); aliases[a] = name
    shutil.copy2(v['ref'], DST / f"{a}.{v['ref_kind']}")
    if v['ref_kind'] == 'mq4':
        deps |= set(re.findall(r'iCustom\s*\([^,;]*,[^,;]*,\s*"([^"]+)"', text(v['ref'])))
found = 0
for dep in deps:
    base = dep.replace(chr(92), '/').split('/')[-1]
    for lib, ext in ((mq4, 'mq4'), (ex4, 'ex4')):
        hits = lib.get(norm(base))
        if hits:
            exact = [h for h in hits if os.path.basename(h)[:-4].lower() == base.lower()]
            tgt = IND / (dep.replace('/', chr(92)) +'.' + ext); tgt.parent.mkdir(parents=True, exist_ok=True)
            if not tgt.exists(): shutil.copy2((exact or hits)[0], tgt)
            found += 1; break
json.dump(aliases, open(os.path.join(D, 'aliases.json'), 'w', encoding='utf-8'), indent=0)
print('staged', len(aliases), 'dependencies', len(deps), 'found', found, flush=True)
for folder in (IND, DST):
    subprocess.run([str(M4 / 'metaeditor.exe'), f'/compile:{folder}', f'/log:{M4 / "compile.log"}'], timeout=7200)
    log = (M4 / 'compile.log').read_text(encoding='utf-16', errors='replace')
    print(folder, [l for l in log.splitlines() if l.startswith('Result')][-1:])
print('ex4 in DIFF:', len(list(DST.glob('*.ex4'))))
# visible buffer count of each .mq4 reference (MT4 iCustom exposes only these) -> DIFF\visible.json
vis = {}
for a, name in aliases.items():
    v = pairs[name]
    if v['ref_kind'] == 'mq4':
        t = text(v['ref']); m = re.search(r'^\s*#property\s+indicator_buffers\s+(\d+)', t, re.M)
        ib = [int(x) for x in re.findall(r'IndicatorBuffers\s*\(\s*(\d+)\s*\)', t)]
        sib = {int(x) for x in re.findall(r'SetIndexBuffer\s*\(\s*(\d+)\s*,', t)}
        vis[a] = int(m.group(1)) if m else (max(ib) if ib else (max(sib) + 1 if sib else 0))
json.dump(vis, open(os.path.join(D, 'visible.json'), 'w', encoding='utf-8'), indent=0)
