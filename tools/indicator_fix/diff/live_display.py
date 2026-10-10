r"""Object-only indicators (no buffers) whose MQL4 source shows live market data -- Bid/Ask, spread, server or local
time, MarketInfo -- cannot be compared offline: MT4's offline chart has no live quote (Bid = 0) while MT5's test
symbol has one. They are dashboards, not signals. Writes DIFF\livedisplay.json (alias -> matched functions)."""
import json, re
from map_ref import text, D
LIVE = re.compile(r'\b(Bid|Ask|TimeCurrent|TimeLocal|CurTime|LocalTime|MarketInfo|RefreshRates|SymbolInfoTick|SymbolInfoDouble|GetTickCount)\b')
pairs = json.load(open(D + r'\pairs.json', encoding='utf-8'))
aliases = json.load(open(D + r'\aliases.json', encoding='utf-8'))
vis = json.load(open(D + r'\visible.json', encoding='utf-8'))
out = {}
for a, n in aliases.items():
    v = pairs[n]
    if v['ref_kind'] != 'mq4' or vis.get(a, 16) != 0: continue
    hits = sorted(set(LIVE.findall(text(v['ref']))))
    if hits: out[a] = hits
json.dump(out, open(D + r'\livedisplay.json', 'w', encoding='utf-8'), indent=0)
print(len(out), 'object-only live displays of', sum(1 for a in aliases if vis.get(a, 16) == 0))
