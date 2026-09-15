from __future__ import annotations
import argparse, csv, hashlib, json, os, re, sqlite3, sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterable

DRAW_TYPES = [
    'DRAW_LINE','DRAW_SECTION','DRAW_HISTOGRAM','DRAW_HISTOGRAM2','DRAW_ARROW','DRAW_COLOR_ARROW',
    'DRAW_ZIGZAG','DRAW_FILLING','DRAW_BARS','DRAW_COLOR_BARS','DRAW_CANDLES','DRAW_COLOR_CANDLES','DRAW_COLOR_LINE'
]
BUILTINS = {
    'iMA': ('Trend','Moving Average'), 'iBands': ('Volatility','Bollinger Bands'), 'iIchimoku': ('Trend','Ichimoku'),
    'iADX': ('Trend','ADX'), 'iRSI': ('Oscillator','RSI'), 'iStochastic': ('Oscillator','Stochastic'),
    'iCCI': ('Oscillator','CCI'), 'iMACD': ('Oscillator','MACD'), 'iMomentum': ('Momentum','Momentum'),
    'iATR': ('Volatility','ATR'), 'iStdDev': ('Volatility','Standard Deviation'), 'iMFI': ('Volume','Money Flow Index'),
    'iOBV': ('Volume','On Balance Volume'), 'iVolumes': ('Volume','Volumes'), 'iAlligator': ('Bill Williams','Alligator'),
    'iAO': ('Bill Williams','Awesome Oscillator'), 'iAC': ('Bill Williams','Accelerator Oscillator'),
    'iFractals': ('Bill Williams','Fractals'), 'iSAR': ('Trend','Parabolic SAR'), 'iWPR': ('Oscillator','Williams %R'),
    'iRVI': ('Oscillator','RVI'), 'iDeMarker': ('Oscillator','DeMarker'), 'iBearsPower': ('Oscillator','Bears Power'),
    'iBullsPower': ('Oscillator','Bulls Power'), 'iForce': ('Volume','Force Index')
}
NAME_HINTS = [
    (r'\brsi\b|rsx|jrsx', 'Oscillator','RSI/RSX family'), (r'\bmacd\b', 'Oscillator','MACD'),
    (r'\bstoch', 'Oscillator','Stochastic'), (r'\bcci\b', 'Oscillator','CCI'), (r'\batr\b', 'Volatility','ATR'),
    (r'bolli|bollinger|\bbb\b', 'Volatility','Bollinger Bands'), (r'awesome|\bao\b', 'Bill Williams','Awesome Oscillator'),
    (r'fractal', 'Bill Williams','Fractals'), (r'volume|\bobv\b|\bmfi\b|\bkvo\b', 'Volume','Volume'),
    (r'supertrend|trend|hull|tema|dema|\bema\b|\bsma\b|moving average|stepma', 'Trend','Trend'),
    (r'zigzag', 'Support/Resistance','ZigZag'), (r'pivot|support|resistance|s[_ -]?r|levels?', 'Support/Resistance','Levels'),
    (r'heiken|ha(?:shi)?', 'Price Action','Heiken Ashi'), (r'divergen', 'Signal','Divergence'),
]

@dataclass
class Analysis:
    path: str
    filename: str
    platform: str
    sha256: str
    size: int
    source_structure: str = 'Unknown'
    display_location: str = 'Unknown'
    declared_buffers: int = 0
    declared_plots: int = 0
    active_buffers: int = 0
    draw_types: list[str] = field(default_factory=list)
    line_plots: int = 0
    histogram_plots: int = 0
    arrow_plots: int = 0
    filling_plots: int = 0
    object_usage: bool = False
    standard_indicators: list[str] = field(default_factory=list)
    custom_dependencies: list[str] = field(default_factory=list)
    primary_category: str = 'Unknown'
    secondary_categories: list[str] = field(default_factory=list)
    visual_category: str = 'Unknown'
    behavior_tags: list[str] = field(default_factory=list)
    confidence: int = 0
    warnings: list[str] = field(default_factory=list)
    duplicate_of: str | None = None


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ('utf-8-sig','utf-8','cp1252','latin1'):
        try: return raw.decode(enc)
        except UnicodeDecodeError: pass
    return raw.decode('latin1', errors='replace')

def strip_comments(text: str) -> str:
    # preserve strings while removing comments well enough for static analysis
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)
    text = re.sub(r'//[^\r\n]*', ' ', text)
    return text

def normalize_newlines(text: str) -> str:
    return text.replace('\r\n','\n').replace('\r','\n').replace('\x00','')

def function_spans(code: str) -> list[tuple[str,int,int]]:
    # tolerant parser for MQL/C-style functions
    pat = re.compile(r'(?m)^\s*(?:[\w:<>&*\[\]]+\s+)+(?P<name>[A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{')
    spans=[]
    for m in pat.finditer(code):
        depth=0; end=None
        for i in range(m.end()-1, len(code)):
            if code[i]=='{': depth += 1
            elif code[i]=='}':
                depth -= 1
                if depth==0:
                    end=i+1; break
        if end: spans.append((m.group('name'), m.start(), end))
    return spans

def logic_code(code: str) -> tuple[str,bool]:
    spans=function_spans(code)
    compat_names={name for name,_,_ in spans if name.startswith('MQL4_') or name.startswith('__mql4_')}
    compat_spans=[(s,e) for name,s,e in spans if name in compat_names]
    if not compat_spans:
        return code, False
    parts=[]; pos=0
    for s,e in sorted(compat_spans):
        parts.append(code[pos:s]); pos=e
    parts.append(code[pos:])
    return '\n'.join(parts), True

def count_actual_calls(code: str, name: str) -> int:
    # Call token followed by (, minus function definitions for exactly this name.
    total=len(re.findall(r'\b'+re.escape(name)+r'\s*\(', code))
    defs=len(re.findall(r'(?m)^\s*(?:[\w:<>&*\[\]]+\s+)+'+re.escape(name)+r'\s*\([^;{}]*\)\s*\{', code))
    return max(0,total-defs)

def extract_int_property(code: str, prop: str) -> int:
    m=re.search(r'#property\s+'+re.escape(prop)+r'\s+(\d+)', code, re.I)
    return int(m.group(1)) if m else 0

def infer_visual(a: Analysis) -> str:
    has_line=a.line_plots>0; has_hist=a.histogram_plots>0; has_arrow=a.arrow_plots>0; has_fill=a.filling_plots>0
    kinds=sum(bool(x) for x in [has_line,has_hist,has_arrow,has_fill,a.object_usage])
    if kinds>1: return 'Mixed'
    if has_arrow: return 'Arrow/Icon'
    if has_hist: return 'Histogram'
    if has_fill: return 'Zones/Filling'
    if has_line:
        if a.line_plots==1: return 'One-Line'
        if a.line_plots==2: return 'Two-Line'
        return 'Multi-Line'
    if a.object_usage: return 'Objects'
    return 'Unknown'

def score_categories(filename: str, logic: str, stds: list[str], a: Analysis) -> tuple[str,list[str],list[str],int]:
    scores={k:0 for k in ['Trend','Oscillator','Volume','Bill Williams','Volatility','Support/Resistance','Momentum','Signal','Price Action','Statistical','Utility']}
    tags=[]
    for std in stds:
        base=std.replace('MQL4_','')
        if base in BUILTINS:
            cat,tech=BUILTINS[base]; scores[cat]+=35; tags.append(tech)
            if base in ('iRSI','iCCI','iStochastic','iMACD','iWPR','iRVI','iDeMarker'): scores['Momentum']+=10
            if base=='iBands': scores['Trend']+=8; scores['Support/Resistance']+=8
            if base=='iFractals': scores['Support/Resistance']+=18; scores['Signal']+=12
    low=(filename+' '+logic[:8000]).lower()
    for pat,cat,tech in NAME_HINTS:
        if re.search(pat, low, re.I):
            scores[cat]+=18; tags.append(tech)
    # direct formula/behavior cues
    if re.search(r'ObjectCreate\s*\(|OBJ_HLINE|OBJ_TREND|OBJ_RECTANGLE|OBJ_FIBO', logic, re.I): scores['Support/Resistance']+=8
    if a.arrow_plots: scores['Signal']+=18; tags.append('Buy/Sell or event markers')
    if re.search(r'alert\s*\(|SendNotification\s*\(|SendMail\s*\(', logic, re.I): scores['Signal']+=8; tags.append('Alerts')
    if re.search(r'High\s*\[.*\].*Low\s*\[|iHighest\s*\(|iLowest\s*\(', logic, re.I|re.S): scores['Support/Resistance']+=6
    if re.search(r'StdDev|standard\s+deviation|MathSqrt\s*\(', logic, re.I): scores['Statistical']+=10
    if re.search(r'Heiken|ha(Open|Close|High|Low)', logic, re.I): scores['Price Action']+=25
    ranked=sorted(scores.items(), key=lambda kv:kv[1], reverse=True)
    best, bestscore=ranked[0]
    if bestscore < 12: return 'Unknown', [], sorted(set(tags)), 35 if a.visual_category!='Unknown' else 20
    secondary=[k for k,v in ranked[1:] if v>=max(12,bestscore*0.45)]
    # confidence rewards multiple independent cues but avoids false certainty
    confidence=min(98, 50 + min(bestscore,35) + 5*len(stds) + (5 if a.visual_category!='Unknown' else 0))
    return best, secondary, sorted(set(tags)), confidence

def analyze(path: Path) -> Analysis:
    text=normalize_newlines(read_text(path)); code=strip_comments(text)
    platform='MQL5' if path.suffix.lower()=='.mq5' else 'MQL4'
    sha=hashlib.sha256(path.read_bytes()).hexdigest()
    a=Analysis(str(path.resolve()), path.name, platform, sha, path.stat().st_size)
    converted=bool(re.search(r'AUTO[- ]CONVERTED\s+MQL4\s*[-=]>?\s*MQL5|compatibility helpers generated', text, re.I))
    marker=re.search(r'=====\s*Converted source\s*=====', text, re.I)
    if converted and marker:
        logic=strip_comments(text[marker.end():])
        compat=True
    else:
        logic, compat=logic_code(code)
    if platform=='MQL5' and converted: a.source_structure='Converted MQL4→MQL5'
    elif platform=='MQL5' and compat: a.source_structure='Compatibility-heavy MQL5'
    else: a.source_structure='Native/Legacy '+platform
    if re.search(r'#property\s+indicator_chart_window', code, re.I): a.display_location='Main Chart'
    if re.search(r'#property\s+indicator_separate_window', code, re.I): a.display_location='Separate Window'
    a.declared_buffers=extract_int_property(code,'indicator_buffers')
    a.declared_plots=extract_int_property(code,'indicator_plots')
    a.active_buffers=len(set(re.findall(r'SetIndexBuffer\s*\(\s*(\d+)', logic, re.I)))
    draw=[]
    for dt in DRAW_TYPES:
        n=len(re.findall(r'\b'+dt+r'\b', logic, re.I))
        draw.extend([dt]*n)
    # MQ5 property draw types (may be in declarations outside functions)
    for dt in DRAW_TYPES:
        n=len(re.findall(r'#property\s+indicator_type\d+\s+'+dt+r'\b', code, re.I))
        draw.extend([dt]*n)
    a.draw_types=sorted(set(draw))
    # plot counts by SetIndexStyle/property occurrences; set uniqueness is impossible if variable index used
    style_entries=re.findall(r'(?:MQL4_)?SetIndexStyle\s*\(\s*([^,]+)\s*,\s*(DRAW_[A-Z0-9_]+)', logic, re.I)
    prop_entries=re.findall(r'#property\s+indicator_type\d+\s+(DRAW_[A-Z0-9_]+)', code, re.I)
    types=[t.upper() for _,t in style_entries]+[t.upper() for t in prop_entries]
    # runtime MQL5 PlotIndexSetInteger(...PLOT_DRAW_TYPE...)
    types += [t.upper() for t in re.findall(r'PlotIndexSetInteger\s*\([^,]+,\s*PLOT_DRAW_TYPE\s*,\s*(DRAW_[A-Z0-9_]+)', logic, re.I)]
    a.line_plots=sum(t in ('DRAW_LINE','DRAW_COLOR_LINE','DRAW_SECTION','DRAW_ZIGZAG') for t in types)
    a.histogram_plots=sum(t in ('DRAW_HISTOGRAM','DRAW_HISTOGRAM2') for t in types)
    a.arrow_plots=sum(t in ('DRAW_ARROW','DRAW_COLOR_ARROW') for t in types)
    a.filling_plots=sum(t=='DRAW_FILLING' for t in types)
    a.object_usage=bool(re.search(r'ObjectCreate\s*\(|OBJ_(?:HLINE|VLINE|TREND|RECTANGLE|TEXT|LABEL|ARROW|FIBO)', logic, re.I))
    stds=[]
    for base in BUILTINS:
        for nm in (base,'MQL4_'+base):
            if count_actual_calls(logic,nm)>0: stds.append(nm)
    a.standard_indicators=sorted(set(stds))
    deps=[]
    for m in re.finditer(r'\biCustom\s*\((.*?)\)', logic, re.I|re.S):
        chunk=m.group(1)[:500]
        ss=re.findall(r'"([^"]+)"',chunk)
        if ss: deps.append(ss[0])
    a.custom_dependencies=sorted(set(deps))
    a.visual_category=infer_visual(a)
    a.primary_category,a.secondary_categories,a.behavior_tags,a.confidence=score_categories(path.stem,logic,a.standard_indicators,a)
    if compat: a.warnings.append('Compatibility/helper functions detected and excluded from indicator-call scoring')
    if a.visual_category=='Unknown': a.warnings.append('No reliable plot style detected; may be object-only or dynamically configured')
    if a.primary_category=='Unknown': a.warnings.append('Low-confidence functional classification; manual review recommended')
    return a

def init_db(conn: sqlite3.Connection):
    conn.executescript('''
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS indicators(
      id INTEGER PRIMARY KEY, path TEXT UNIQUE, filename TEXT, platform TEXT, sha256 TEXT, size INTEGER,
      source_structure TEXT, display_location TEXT, declared_buffers INTEGER, declared_plots INTEGER,
      active_buffers INTEGER, draw_types TEXT, line_plots INTEGER, histogram_plots INTEGER, arrow_plots INTEGER,
      filling_plots INTEGER, object_usage INTEGER, standard_indicators TEXT, custom_dependencies TEXT,
      primary_category TEXT, secondary_categories TEXT, visual_category TEXT, behavior_tags TEXT,
      confidence INTEGER, warnings TEXT, duplicate_of TEXT, analyzed_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_sha ON indicators(sha256);
    CREATE INDEX IF NOT EXISTS idx_primary ON indicators(primary_category);
    CREATE INDEX IF NOT EXISTS idx_visual ON indicators(visual_category);
    CREATE INDEX IF NOT EXISTS idx_platform ON indicators(platform);
    ''')

def save(conn: sqlite3.Connection, a: Analysis):
    d=asdict(a)
    for k in ['draw_types','standard_indicators','custom_dependencies','secondary_categories','behavior_tags','warnings']:
        d[k]=json.dumps(d[k],ensure_ascii=False)
    d['object_usage']=1 if d['object_usage'] else 0
    cols=list(d.keys()); vals=[d[c] for c in cols]
    sql=f"INSERT INTO indicators({','.join(cols)}) VALUES({','.join('?' for _ in cols)}) ON CONFLICT(path) DO UPDATE SET "+','.join(f'{c}=excluded.{c}' for c in cols if c!='path')
    conn.execute(sql,vals)

def scan(paths: Iterable[Path], db: Path, csv_path: Path | None=None):
    files=[]
    for root in paths:
        if root.is_file() and root.suffix.lower() in ('.mq4','.mq5'): files.append(root)
        elif root.is_dir(): files += [p for p in root.rglob('*') if p.suffix.lower() in ('.mq4','.mq5')]
    files=sorted(files,key=lambda p:str(p).lower())
    conn=sqlite3.connect(db); init_db(conn)
    results=[]; sha_first={}
    for i,p in enumerate(files,1):
        a=analyze(p)
        if a.sha256 in sha_first: a.duplicate_of=sha_first[a.sha256]
        else: sha_first[a.sha256]=a.path
        save(conn,a); results.append(a)
        if i%25==0: conn.commit()
    conn.commit(); conn.close()
    if csv_path:
        fields=list(asdict(results[0]).keys()) if results else list(Analysis.__annotations__.keys())
        with csv_path.open('w',newline='',encoding='utf-8-sig') as f:
            w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
            for a in results:
                row=asdict(a)
                for k,v in row.items():
                    if isinstance(v,list): row[k]='; '.join(v)
                w.writerow(row)
    return results

def summary(results):
    from collections import Counter
    return {
      'total':len(results),'platforms':dict(Counter(a.platform for a in results)),
      'primary_categories':dict(Counter(a.primary_category for a in results)),
      'visual_categories':dict(Counter(a.visual_category for a in results)),
      'source_structure':dict(Counter(a.source_structure for a in results)),
      'duplicates':sum(bool(a.duplicate_of) for a in results),
      'needs_review':sum(a.confidence<70 or a.primary_category=='Unknown' for a in results),
      'average_confidence':round(sum(a.confidence for a in results)/len(results),1) if results else 0,
    }

def main():
    ap=argparse.ArgumentParser(description='MQL Indicator Library prototype analyzer')
    ap.add_argument('paths',nargs='+'); ap.add_argument('--db',default='indicator_library.sqlite3'); ap.add_argument('--csv',default='classification_report.csv'); ap.add_argument('--summary',default='summary.json')
    args=ap.parse_args()
    results=scan([Path(p) for p in args.paths],Path(args.db),Path(args.csv))
    s=summary(results); Path(args.summary).write_text(json.dumps(s,indent=2),encoding='utf-8')
    print(json.dumps(s,indent=2))
if __name__=='__main__': main()
