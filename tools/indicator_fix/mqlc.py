r"""Compile ONE .mq5 with MetaEditor (standard + user includes) and print the result as plain text.
Usage: python F:\MQLFIX_BUILD\tools\mqlc.py <file.mq5>   -> exit 0 on 0 errors."""
import subprocess, sys, tempfile
from pathlib import Path
ME = r'C:\Program Files\MetaTrader 5\MetaEditor64.exe'
INC = r'C:\Users\Evision\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\MQL5'
src = Path(sys.argv[1]).resolve()
if ' ' in str(src):
    print('ERROR: path contains a space; MetaEditor cannot compile it'); sys.exit(2)
log = Path(tempfile.gettempdir()) / f'mqlc_{src.stem}.log'
subprocess.run([ME, f'/compile:{src}', f'/inc:{INC}', f'/log:{log}'], timeout=300)
text = log.read_text(encoding='utf-16', errors='replace') if log.exists() else ''
errs = [l.split(str(src.name), 1)[-1].strip() for l in text.splitlines() if ' error ' in l]
res = [l for l in text.splitlines() if l.startswith('Result')]
print(res[-1] if res else 'Result: no compiler log')
for e in errs[:40]: print(e)
if len(errs) > 40: print(f'... {len(errs) - 40} more errors')
ok = bool(res) and res[-1].startswith('Result: 0 errors') and src.with_suffix('.ex5').exists()
sys.exit(0 if ok else 1)
