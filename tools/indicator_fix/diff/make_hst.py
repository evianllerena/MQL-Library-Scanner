r"""Write the frozen EURUSD D1 history (F:\MQLFIX_BUILD\DIFF\rates.csv, exported from MT5 by DiffDump5) into an
MT4 portable copy as offline .hst files, so MT4 and MT5 compute on identical bars:
  EURUSD = all bars (MT5 symbol DIFF), GBPUSD = the same bars minus the last CUT (MT5 symbol DIFFC).
MT5's custom symbols store each D1 bar as one M1 bar at 00:00, so every intraday timeframe has one bar per
day there; the same is written here for M1..H4. W1 (Sunday-stamped) and MN1 are aggregated.
Both symbols are set to 5 digits / point 0.00001 like MT5's EURUSD (MT4's offline defaults are 4-digit).
Usage: make_hst.py <mt4 portable dir>"""
import calendar, csv, struct, sys, time
from pathlib import Path
CUT = 150
ALL = [list(map(float, r)) for r in csv.reader(open(r'F:\MQLFIX_BUILD\DIFF\rates.csv'))]
dest = Path(sys.argv[1]) / 'history' / 'default'
def write(sym, period, bars):
    with open(dest / f'{sym}{period}.hst', 'wb') as f:
        f.write(struct.pack('<i64s12siiii52x', 401, b'(C)opyright 2003, MetaQuotes Software Corp.', sym.encode(), period, 5, int(time.time()), 0))
        for t, o, h, l, c, v, s, rv in bars:
            f.write(struct.pack('<qddddqiq', int(t), o, h, l, c, int(v), int(s), int(rv)))
def agg(rows, key):
    out = {}
    for b in rows:
        k = key(int(b[0]))
        if k not in out: out[k] = [k, b[1], b[2], b[3], b[4], b[5], b[6], b[7]]
        else:
            a = out[k]; a[2] = max(a[2], b[2]); a[3] = min(a[3], b[3]); a[4] = b[4]; a[5] += b[5]; a[7] += b[7]
    return [out[k] for k in sorted(out)]
def month(t):
    g = time.gmtime(t); return calendar.timegm((g.tm_year, g.tm_mon, 1, 0, 0, 0))
raw = dest / 'symbols.raw'; d = bytearray(raw.read_bytes())
for sym, rows in (('EURUSD', ALL), ('GBPUSD', ALL[:-CUT])):
    for p in (1, 5, 15, 30, 60, 240, 1440): write(sym, p, rows)
    write(sym, 10080, agg(rows, lambda t: t - (t // 86400 + 4) % 7 * 86400))      # back to Sunday 00:00
    write(sym, 43200, agg(rows, month))
    i = d.find(sym.encode() + b'\0')
    struct.pack_into('<i', d, i + 104, 5); struct.pack_into('<d', d, i + 1776, 0.00001)
    print(sym, len(rows), 'bars, 5 digits')
raw.write_bytes(bytes(d))
