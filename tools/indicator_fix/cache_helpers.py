r"""Rewrite "per-call" built-in indicator helpers into cached ones (deterministic, no AI).

Converted sources (Offline Converter helpers kept through MQL_ONE) contain helpers like
    double MQL4_iCCI(const string symbol, ..., const int shift) {
       int h=iCCI(symbol,(ENUM_TIMEFRAMES)timeframe,period,(ENUM_APPLIED_PRICE)applied_price);
       if(h==INVALID_HANDLE) return EMPTY_VALUE;
       ... CopyBuffer(h,0,shift,1,v); IndicatorRelease(h); ...
which create a NEW built-in indicator for every value and release it at once. A fresh MT5 indicator has
no data yet, so the copy fails and the bar stays EMPTY; values only appear as live ticks re-trigger the
calculation (seen: 1,514 indicators work on live EURUSD but never calculate on a tick-less copy).
Rewrite: (1) keep each handle in a static cache keyed by its exact arguments and never release it;
(2) when a cached handle has not finished calculating, set __mqlfix_not_ready, and OnCalculate returns 0
so MT5 recalculates the whole history on the next call (MQL_ONE 5.9.19 FIX 3 does the same for its shim).
Usage: cache_helpers.py <in_dir> <out_dir>   (files without the pattern are copied unchanged)"""
import re, sys
from pathlib import Path

HELPER = re.compile(r'(?P<indent>[ \t]*)int\s+h\s*=\s*(?P<fn>i[A-Z]\w*)\((?P<args>[^;]*)\);'
                    r'(?P<mid>\s*\r?\n\s*if\s*\(\s*h\s*==\s*INVALID_HANDLE\s*\)\s*return\s+EMPTY_VALUE;)')
RELEASE = re.compile(r'IndicatorRelease\(\s*h\s*\)\s*;')
FLAG = 'bool __mqlfix_not_ready=false;   // [mqlfix] a cached built-in indicator was not ready yet\n'

def split_args(a):
    out, depth, cur = [], 0, ''
    for ch in a:
        if ch in '([': depth += 1
        elif ch in ')]': depth -= 1
        if ch == ',' and depth == 0:
            out.append(cur.strip()); cur = ''
        else:
            cur += ch
    if cur.strip(): out.append(cur.strip())
    return out

def func_bounds(text, pos):
    """(start of the enclosing function body '{', index of its closing '}')."""
    depth = 0
    for i in range(pos, -1, -1):
        if text[i] == '}': depth += 1
        elif text[i] == '{':
            if depth == 0: start = i; break
            depth -= 1
    else:
        return None
    depth = 0
    for j in range(start, len(text)):
        if text[j] == '{': depth += 1
        elif text[j] == '}':
            depth -= 1
            if depth == 0: return start, j
    return None

def rewrite(text):
    n = 0
    for m in sorted(HELPER.finditer(text), key=lambda m: -m.start()):
        b = func_bounds(text, m.start())
        if not b: continue
        body = text[m.start():b[1]]
        if not RELEASE.search(body): continue
        ind, fn, args = m.group('indent'), m.group('fn'), m.group('args')
        key = '+"|"+'.join(f'(string)({a})' for a in split_args(args)) or '""'
        new = (f'{ind}static string __fk[]; static int __fh[];   // [mqlfix] cached handles\n'
               f'{ind}string __key={key};\n'
               f'{ind}int h=INVALID_HANDLE;\n'
               f'{ind}for(int __i=0;__i<ArraySize(__fk);__i++) if(__fk[__i]==__key){{h=__fh[__i];break;}}\n'
               f'{ind}if(h==INVALID_HANDLE){{ h={fn}({args}); if(h!=INVALID_HANDLE){{int __n=ArraySize(__fk);'
               f'ArrayResize(__fk,__n+1);ArrayResize(__fh,__n+1);__fk[__n]=__key;__fh[__n]=h;}} }}'
               + m.group('mid'))
        body2 = RELEASE.sub('if(BarsCalculated(h)<=0) __mqlfix_not_ready=true;', body[len(m.group(0)):])
        text = text[:m.start()] + new + body2 + text[b[1]:]
        n += 1
    if not n:
        return text, 0
    # global flag before the first function, reset + "return 0 while not ready" in OnCalculate
    first = re.search(r'(?m)^[A-Za-z_][\w\s\*&\[\]]*\s+[A-Za-z_]\w*\s*\([^;]*\)\s*\{', text)
    text = text[:first.start()] + FLAG + text[first.start():] if first else FLAG + text
    oc = re.search(r'\bint\s+OnCalculate\s*\([^)]*\)\s*\{', text)
    fb = func_bounds(text, oc.end() - 1) if oc else None
    if fb:
        end = fb[1]
        body = text[oc.end():end]
        body = re.sub(r'\breturn\s*\(?\s*rates_total\s*\)?\s*;', 'return(__mqlfix_not_ready?0:rates_total);', body)
        text = text[:oc.end()] + '\n   __mqlfix_not_ready=false;' + body + text[end:]
    return text, n

def read(p):
    raw = Path(p).read_bytes()
    enc = 'utf-16' if raw[:2] in (b'\xff\xfe', b'\xfe\xff') else 'utf-8-sig' if raw[:3] == b'\xef\xbb\xbf' else 'utf-8'
    return raw.decode(enc, errors='replace'), enc

if __name__ == '__main__':
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    files = [src] if src.is_file() else sorted(src.rglob('*.mq5'))
    changed = 0
    for p in files:
        text, enc = read(p)
        new, n = rewrite(text)
        out = dst if src.is_file() else dst / p.relative_to(src)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(new.encode(enc))
        changed += bool(n)
    print('files', len(files), 'rewritten', changed)
