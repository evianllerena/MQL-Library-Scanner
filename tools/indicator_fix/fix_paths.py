r"""Machine-specific paths for the indicator fix pipeline, in one place.
Each one can be overridden with the environment variable named beside it; the defaults are the paths of
the machine the pipeline was first run on, so existing runs behave exactly as before."""
import os
from pathlib import Path


def _env(name, default):
    return os.environ.get(name) or default


BUILD = Path(_env('MQLFIX_BUILD', r'F:\MQLFIX_BUILD'))            # space-free work dir (MetaEditor CLI cuts at spaces)
RT_ROOT = Path(_env('MQLFIX_RT', r'F:\MQLFIX_RT'))                 # portable MT5 runtime copies rt1..rtN
CONVERTED = Path(_env('MQLFIX_CONVERTED', r'F:\Converted Indicators'))
READY = Path(_env('MQLFIX_READY', r'F:\Ready MQL5 Indicators'))
FIXED = Path(_env('MQLFIX_FIXED', r'F:\Fixed MQL5 Indicators'))   # package.py output
METAEDITOR = _env('MQLFIX_METAEDITOR', r'C:\Program Files\MetaTrader 5\MetaEditor64.exe')
MQL5_INC = _env('MQLFIX_MQL5_INC',
                r'C:\Users\Evision\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\MQL5')
CLAUDE = _env('MQLFIX_CLAUDE', r'C:\Users\Evision\.local\bin\claude.exe')
MQLC = _env('MQLFIX_MQLC', str(BUILD / 'tools' / 'mqlc.py'))       # compile helper used by qa_one.py
QA_RT = Path(_env('MQLFIX_QA_RT',                                  # qa_driver.py default runtime (no --rt)
                  r'C:\Users\Evision\AppData\Local\nnfx-backtest-runtime\preview-runtime\v5\mt5-5ece741c1d'))
MQLONE_MODULES = _env('MQLFIX_MQLONE_MODULES',                     # MQL_ONE's src\modules (for textio)
                      r'C:\Users\Evision\AppData\Local\Temp\claude\C--Users-Evision\c27ab46c-7c67-48b8-915c-a0ab7cb2913a'
                      r'\scratchpad\mqlone\src\MQL_ONE_FIXED_v5.9.19\src\modules')


def rt(n):
    """Runtime copy number n (1-based), e.g. F:\\MQLFIX_RT\\rt3."""
    return RT_ROOT / f'rt{n}'
