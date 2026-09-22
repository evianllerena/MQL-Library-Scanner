from __future__ import annotations
import hashlib, re
from dataclasses import dataclass, field
from pathlib import Path

DRAW_TYPES = [
    'DRAW_LINE','DRAW_SECTION','DRAW_HISTOGRAM','DRAW_HISTOGRAM2','DRAW_ARROW','DRAW_COLOR_ARROW',
    'DRAW_ZIGZAG','DRAW_FILLING','DRAW_BARS','DRAW_COLOR_BARS','DRAW_CANDLES','DRAW_COLOR_CANDLES','DRAW_COLOR_LINE'
]
BUILTINS = {
    'iMA': ('Trend','Moving Average'), 'iBands': ('Volatility','Bollinger Bands'), 'iIchimoku': ('Trend','Ichimoku'),
    'iADX': ('Trend','ADX'), 'iRSI': ('Oscillator','RSI'), 'iStochastic': ('Oscillator','Stochastic'),
    'iCCI': ('Oscillator','CCI'), 'iMACD': ('Oscillator','MACD'), 'iMomentum': ('Momentum','Momentum'),
    'iATR': ('Volatility','ATR'), 'iStdDev': ('Statistical','Standard Deviation'), 'iMFI': ('Volume','Money Flow Index'),
    'iOBV': ('Volume','On Balance Volume'), 'iVolumes': ('Volume','Volumes'), 'iAlligator': ('Bill Williams','Alligator'),
    'iAO': ('Bill Williams','Awesome Oscillator'), 'iAC': ('Bill Williams','Accelerator Oscillator'),
    'iFractals': ('Bill Williams','Fractals'), 'iSAR': ('Trend','Parabolic SAR'), 'iWPR': ('Oscillator','Williams %R'),
    'iRVI': ('Oscillator','RVI'), 'iDeMarker': ('Oscillator','DeMarker'), 'iBearsPower': ('Oscillator','Bears Power'),
    'iBullsPower': ('Oscillator','Bulls Power'), 'iForce': ('Volume','Force Index')
}
CATEGORIES = ['Trend','Oscillator','Volume','Bill Williams','Volatility','Support/Resistance','Momentum','Signal','Price Action','Market Structure','Statistical','Utility','Custom / Specialized','Composite / Multi-Purpose','Unknown']

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
    line_buffer_indices: list[int] = field(default_factory=list)
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
    techniques: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    classification_status: str = 'Unknown'
    review_reason: str = ''
    classifier_version: str = 'evidence-v5'  # v5: adds line_buffer_indices (NNFX Part B needs real
                                              # buffer indices, not a 0/1 guess -- see nnfx_slot_signal)
    confidence: int = 0
    warnings: list[str] = field(default_factory=list)
    duplicate_of: str | None = None
    family_fingerprint: str = ''

def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ('utf-8-sig','utf-8','cp1252','latin1'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode('latin1', errors='replace')

def strip_comments(text: str) -> str:
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)
    text = re.sub(r'//[^\r\n]*', ' ', text)
    return text

def normalize_newlines(text: str) -> str:
    return text.replace('\r\n','\n').replace('\r','\n').replace('\x00','')

def function_spans(code: str) -> list[tuple[str,int,int]]:
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
        if end:
            spans.append((m.group('name'), m.start(), end))
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
        return 'One-Line' if a.line_plots==1 else ('Two-Line' if a.line_plots==2 else 'Multi-Line')
    if a.object_usage: return 'Objects'
    return 'Unknown'

ZERO_REFERENCE_BY_BUILTIN = {
    # Bounded 0..100 (or similar) oscillators with NO true zero line: NNFX_RULESET_THE_TRUTH.txt
    # SS4 explicitly allows treating the documented midpoint as the zero equivalent for these.
    'irsi': 50.0, 'istochastic': 50.0, 'imfi': 50.0, 'iwpr': -50.0, 'idemarker': 0.5,
    'imomentum': 100.0,  # iMomentum oscillates around 100 (ratio to N bars ago), not 0
    # True zero-line oscillators: 0 is their actual centerline.
    'icci': 0.0, 'imacd': 0.0, 'irvi': 0.0, 'ibearspower': 0.0, 'ibullspower': 0.0, 'iforce': 0.0,
}

def _builtin_names(standard_indicators) -> set[str]:
    return {str(s).replace('MQL4_', '').lower() for s in (standard_indicators or [])}

def zero_reference_for(standard_indicators) -> tuple[float | None, str | None]:
    """Documented-builtin lookup ONLY. Returns (None, None) -- never a default
    -- when the active builtins don't include one of the known bounded/centered
    oscillators above."""
    names = _builtin_names(standard_indicators)
    for key, ref in ZERO_REFERENCE_BY_BUILTIN.items():
        if key in names:
            return ref, f"matched active builtin {key!r} -> documented centerline {ref}"
    return None, None

_LEVEL_PATTERNS = (
    re.compile(r'#property\s+indicator_level\d+\s+(-?\d+(?:\.\d+)?)', re.I),
    re.compile(r'\bSetLevelValue\s*\(\s*\d+\s*,\s*(-?\d+(?:\.\d+)?)\s*\)', re.I),
    re.compile(r'IndicatorSetDouble\s*\(\s*INDICATOR_LEVELVALUE\s*,\s*\d+\s*,\s*(-?\d+(?:\.\d+)?)\s*\)', re.I),
)

def declared_zero_level(path) -> tuple[float | None, str | None]:
    """Look for an unambiguous single declared level line in the indicator's
    own source (#property indicator_levelN, SetLevelValue, or MQL5's
    IndicatorSetDouble(INDICATOR_LEVELVALUE,...)). Only trusted when exactly
    ONE distinct level value is declared -- classic overbought/oversold pairs
    (e.g. 30 and 70) are ambiguous and must never be guessed at as a
    centerline. Returns (None, reason) if no path, unreadable, zero, or
    multiple distinct levels are found."""
    if not path:
        return None, None
    try:
        code = strip_comments(normalize_newlines(read_text(Path(path))))
    except Exception:
        return None, None
    values = set()
    for pat in _LEVEL_PATTERNS:
        for m in pat.finditer(code):
            values.add(float(m.group(1)))
    if len(values) == 1:
        v = next(iter(values))
        return v, f"single declared level line found in source (value={v})"
    if len(values) > 1:
        return None, f"multiple declared level lines found {sorted(values)} -- ambiguous, not usable as a centerline"
    return None, None

def _calls_atr(standard_indicators, techniques) -> bool:
    if 'iatr' in _builtin_names(standard_indicators):
        return True
    return 'ATR' in (techniques or [])

_COMPETING_ATR_CATEGORIES = ('Trend', 'Support/Resistance')

def _competing_atr_evidence(evidence) -> str | None:
    """A genuine ATR indicator is a bare readout of the ATR value -- if the
    evidence also shows Trend/Support-Resistance scoring, or any band/channel
    structure, the indicator is using ATR as an ingredient (e.g. a channel
    width), not displaying ATR itself."""
    for e in (evidence or []):
        if not isinstance(e, dict):
            continue
        cat = e.get('category')
        detail = str(e.get('detail', '')).lower()
        if cat in _COMPETING_ATR_CATEGORIES:
            return f"competing {cat} evidence: {e.get('detail')}"
        if 'band' in detail or 'channel' in detail:
            return f"competing band/channel evidence: {e.get('detail')}"
    return None

def is_atr_indicator(display_location, line_plots, evidence, standard_indicators, techniques) -> tuple[bool, str]:
    """A genuine ATR indicator is a single-line readout in its OWN separate
    window -- not a multi-line chart overlay that merely calls iATR() to scale
    something else. Requires ALL of: an active iATR call, display_location=
    'Separate Window', line_plots<=2, and no competing channel/band/Trend/
    Support-Resistance evidence. Returns (is_atr, reason)."""
    if not _calls_atr(standard_indicators, techniques):
        return False, "no active ATR builtin call recorded"
    if display_location != 'Separate Window':
        return False, f"iATR is called but display_location={display_location!r}, not 'Separate Window' -- not a genuine ATR readout"
    if (line_plots or 0) > 2:
        return False, f"iATR is called but line_plots={line_plots} (>2) -- multi-line overlay, not a single ATR readout"
    competing = _competing_atr_evidence(evidence)
    if competing:
        return False, f"iATR is called but {competing} -- ATR is an ingredient here, not the indicator's own readout"
    return True, "single/dual-line ATR readout in its own separate window with no competing channel/band/trend/support-resistance evidence"

def nnfx_slot_signal(visual_category: str, display_location: str, primary_category: str,
                      standard_indicators=None, techniques=None, path=None,
                      line_plots=0, evidence=None, declared_buffers=0,
                      line_buffer_indices=None) -> tuple[str, str | None, float | None, int | None, int | None, str]:
    """NNFX_INTEGRATION_PLAN.txt STEP 2 (final rules). Maps already-stored
    classifier fields onto a backtest slot + signal_type + zero_reference +
    the REAL buffer index/indices to read (buf_a/buf_b: for CONFIRMATION_1
    these are fast/slow; for everything else buf_a is the single main buffer
    and buf_b is None). Buffer indices come ONLY from line_buffer_indices
    (literal indices resolved from the indicator's own SetIndexStyle/
    PlotIndexSetInteger/#property indicator_typeN declarations -- see
    engine_core.analyze()); a shape that would otherwise qualify but has no
    resolved index is routed to NEEDS_REVIEW rather than guessing 0/1 (this is
    exactly the bug a real pilot run caught: NNFXHarness.mq5 used to default to
    buffer 0/1 for every candidate, which is wrong whenever the actual line
    buffers aren't literally indices 0/1 in that file's own source).
    zero_reference is trusted ONLY from a documented bounded builtin or an
    unambiguous declared level line in the indicator's own source -- it is
    NEVER assumed to be 0. Returns (slot, signal_type, zero_reference, buf_a,
    buf_b, reason). slot is one of CONFIRMATION_1/CONFIRMATION_2/BASELINE/
    VOLUME/ATR/EXCLUDED_NON_NNFX/NEEDS_REVIEW."""
    idx = sorted(line_buffer_indices) if line_buffer_indices else []

    atr, atr_reason = is_atr_indicator(display_location, line_plots, evidence, standard_indicators, techniques)
    if atr:
        return 'ATR', None, None, None, None, f"ATR slot: {atr_reason}"

    if primary_category in ('Volume', 'Volatility'):
        # declared_buffers (from #property indicator_buffers / IndicatorBuffers) is
        # used here rather than line_plots: it's a mechanically simpler, more
        # reliable already-stored signal -- line_plots depends on matching
        # SetIndexStyle/PlotIndexSetInteger DRAW_LINE calls, which can miss on
        # obfuscated/auto-converted files (seen directly on mth_FastTMALine_2.mq5,
        # which has 6 declared buffers but line_plots=0). A real volatility-band
        # filter (Bollinger/Keltner/Donchian-style) is typically 2-3 buffers;
        # >3 plus competing Trend/S-R/band-channel evidence means Volatility/Volume
        # is one ingredient of a larger multi-purpose overlay, not the point.
        competing = _competing_atr_evidence(evidence) if (declared_buffers or 0) > 3 else None
        if not competing:
            if not idx:
                return 'NEEDS_REVIEW', None, None, None, None, "primary_category indicates VOLUME but no resolved buffer index -- cannot wire the filter's own reading"
            return 'VOLUME', None, None, idx[0], None, f"primary_category={primary_category!r} and not an ATR indicator -> VOLUME slot"
        # Fall through and let it be routed by its actual visual shape below.

    if visual_category == 'Two-Line':
        if len(idx) < 2:
            return 'NEEDS_REVIEW', None, None, None, None, f"visual_category='Two-Line' but only {len(idx)} buffer index(es) resolved -- cannot tell fast from slow"
        return 'CONFIRMATION_1', 'TWO_LINE_CROSS', None, idx[0], idx[1], f"visual_category='Two-Line' -> two-line-cross confirmation (bridge-too-far eligible); buffers {idx[0]}/{idx[1]}"

    if visual_category == 'One-Line':
        if not idx:
            return 'NEEDS_REVIEW', None, None, None, None, "visual_category='One-Line' but no resolved buffer index -- cannot wire the candidate's own reading"
        if display_location == 'Separate Window':
            zero_ref, zref_reason = zero_reference_for(standard_indicators)
            if zero_ref is None:
                declared_ref, declared_reason = declared_zero_level(path)
                if declared_ref is not None:
                    zero_ref, zref_reason = declared_ref, declared_reason
                elif declared_reason:
                    zref_reason = declared_reason
            if zero_ref is None:
                return 'NEEDS_REVIEW', None, None, None, None, "no declared/known neutral line — cannot use as zero-cross" + (f" ({zref_reason})" if zref_reason else "")
            return 'CONFIRMATION_2', 'ZERO_CROSS', zero_ref, idx[0], None, f"visual_category='One-Line' + display_location='Separate Window' -> zero-cross confirmation; buffer {idx[0]}; {zref_reason}"
        if display_location == 'Main Chart':
            return 'BASELINE', None, None, idx[0], None, f"visual_category='One-Line' + display_location='Main Chart' -> baseline candidate; buffer {idx[0]}"
        return 'NEEDS_REVIEW', None, None, None, None, "visual_category='One-Line' but display_location is not stored as 'Main Chart' or 'Separate Window'"

    if visual_category in ('Arrow/Icon', 'Histogram'):
        return 'EXCLUDED_NON_NNFX', None, None, None, None, f"visual_category={visual_category!r} is not part of the final 6-slot NNFX mapping -> excluded"

    return 'NEEDS_REVIEW', None, None, None, None, f"visual_category={visual_category!r} (mixed/unknown shape) -> route to review"

def add_evidence(evidence: list[dict], scores: dict[str,float], category: str, weight: float, kind: str, detail: str, technique: str | None = None):
    scores[category] = scores.get(category, 0.0) + weight
    item={'category':category,'weight':round(weight,2),'kind':kind,'detail':detail}
    if technique: item['technique']=technique
    evidence.append(item)

def structural_fingerprint(logic: str) -> str:
    normalized = re.sub(r'"(?:\\.|[^"\\])*"', '"STR"', logic)
    normalized = re.sub(r'\b\d+(?:\.\d+)?\b', 'N', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip().lower()
    tokens = re.findall(r'[a-z_][a-z0-9_]*|[+\-*/<>=!&|]+', normalized)
    return hashlib.sha256(' '.join(tokens[:12000]).encode('utf-8', errors='ignore')).hexdigest()

def _has(logic: str, pattern: str, flags=re.I) -> bool:
    return bool(re.search(pattern, logic, flags))

def classify(filename: str, logic: str, stds: list[str], a: Analysis):
    scores={k:0.0 for k in CATEGORIES if k not in ('Unknown','Custom / Specialized','Composite / Multi-Purpose')}
    evidence=[]; tags=[]; techniques=[]

    for std in stds:
        base=std.replace('MQL4_','')
        if base not in BUILTINS: continue
        cat,tech=BUILTINS[base]
        add_evidence(evidence,scores,cat,32,'active_builtin',f'{base} is actively called in calculation logic',tech)
        techniques.append(tech)
        if base in ('iRSI','iCCI','iStochastic','iMACD','iWPR','iRVI','iDeMarker'):
            add_evidence(evidence,scores,'Momentum',8,'derived_behavior',f'{base} commonly contributes momentum information')
        if base=='iBands':
            add_evidence(evidence,scores,'Support/Resistance',8,'derived_behavior','Bollinger bands can act as dynamic boundaries')
        if base=='iFractals':
            add_evidence(evidence,scores,'Support/Resistance',14,'derived_behavior','Fractals identify local extrema')
            add_evidence(evidence,scores,'Signal',8,'derived_behavior','Fractal events may be used as signals')

    if _has(logic, r'\b(?:MODE_SMA|MODE_EMA|MODE_SMMA|MODE_LWMA|ExponentialMA|SimpleMA|LinearWeightedMA|MovingAverage)\b'):
        add_evidence(evidence,scores,'Trend',18,'formula','Moving-average/smoothing formula or mode detected'); techniques.append('Moving-average / smoothing')
    if _has(logic, r'\b(?:ema|sma|smma|lwma|moving\s*average|mafast|maslow|fastma|slowma)\b') and _has(logic, r'\[[^\]]+\]'):
        add_evidence(evidence,scores,'Trend',10,'formula','Custom moving-average style series logic detected')
    if _has(logic, r'\b(?:slope|linearregression|linreg|least\s*squares)\b'):
        add_evidence(evidence,scores,'Trend',12,'formula','Slope/regression trend logic detected'); techniques.append('Slope / regression')
    if _has(logic, r'\b(?:MathSqrt|MathPow|StdDev|variance|deviation|correlation|regression)\b'):
        add_evidence(evidence,scores,'Statistical',16,'formula','Statistical/deviation mathematics detected'); techniques.append('Statistical calculation')
    if _has(logic, r'\b(?:High|high)\s*\[[^\]]+\]\s*-\s*(?:Low|low)\s*\[[^\]]+\]') or _has(logic, r'\b(?:true\s*range|truerange|rangebuffer)\b'):
        add_evidence(evidence,scores,'Volatility',14,'formula','Price-range / true-range style formula detected'); techniques.append('Range / volatility')
    if _has(logic, r'\b(?:upperband|lowerband|upper_band|lower_band|channel|envelope)\b'):
        add_evidence(evidence,scores,'Volatility',10,'structure','Upper/lower band or channel structure detected')
        add_evidence(evidence,scores,'Support/Resistance',6,'structure','Band/channel boundaries may act as dynamic levels')
    if _has(logic, r'\b(?:Volume|tick_volume|real_volume)\b'):
        add_evidence(evidence,scores,'Volume',14,'data_dependency','Uses volume/tick-volume data'); techniques.append('Volume analysis')
    if _has(logic, r'\b(?:iHighest|iLowest)\s*\(|\bHigh\s*\[.*?\].*?\bLow\s*\[', re.I|re.S):
        add_evidence(evidence,scores,'Support/Resistance',12,'price_structure','High/low extrema logic detected'); techniques.append('Extrema / levels')
    if _has(logic, r'\b(?:pivot|support|resistance|fib(?:o|onacci)?|retracement)\b'):
        add_evidence(evidence,scores,'Support/Resistance',16,'price_structure','Pivot/support/resistance/Fibonacci logic detected'); techniques.append('Levels / pivots')
    if _has(logic, r'\b(?:swing|bos|choch|break\s*of\s*structure|market\s*structure|higher\s*high|lower\s*low)\b'):
        add_evidence(evidence,scores,'Market Structure',22,'behavior','Market-structure logic detected'); techniques.append('Market structure')
    if _has(logic, r'\b(?:Open|High|Low|Close)\s*\[[^\]]+\].*\b(?:Open|High|Low|Close)\s*\[', re.I|re.S):
        add_evidence(evidence,scores,'Price Action',8,'price_dependency','Direct multi-price-bar calculations detected')
    if _has(logic, r'\b(?:bullish|bearish|engulf|doji|pinbar|pin\s*bar|inside\s*bar|outside\s*bar)\b'):
        add_evidence(evidence,scores,'Price Action',16,'pattern','Candlestick/price-action pattern logic detected'); techniques.append('Candlestick pattern')
    if _has(logic, r'\b(?:Heiken|haOpen|haClose|haHigh|haLow)\b'):
        add_evidence(evidence,scores,'Price Action',24,'formula','Heiken Ashi-style calculation detected','Heiken Ashi'); techniques.append('Heiken Ashi')
    if _has(logic, r'\b(?:cross|crossover|crossunder)\b') or _has(logic, r'\[[^\]]*\]\s*[<>]\s*[^;\n]+\[[^\]]*\]'):
        add_evidence(evidence,scores,'Signal',10,'behavior','Cross/comparison signal logic detected'); tags.append('Crossover / comparative signal')
    if _has(logic, r'\b(?:divergen|bullish divergence|bearish divergence)\b'):
        add_evidence(evidence,scores,'Signal',20,'behavior','Divergence logic detected'); tags.append('Divergence')
    if _has(logic, r'\b(?:breakout|break\s+above|break\s+below)\b'):
        add_evidence(evidence,scores,'Signal',12,'behavior','Breakout logic detected'); add_evidence(evidence,scores,'Support/Resistance',8,'behavior','Breakout references price boundaries'); tags.append('Breakout')
    if _has(logic, r'\b(?:70|80)\b.*\b(?:30|20)\b|\boverbought\b|\boversold\b', re.I|re.S):
        add_evidence(evidence,scores,'Oscillator',12,'threshold','Overbought/oversold threshold behavior detected'); tags.append('Overbought/Oversold')
    if _has(logic, r'\b(?:SetLevelValue|indicator_level\d+|indicator_minimum|indicator_maximum)\b'):
        add_evidence(evidence,scores,'Oscillator',10,'visual_structure','Separate-window level/boundary metadata detected')
    if a.display_location=='Separate Window' and (a.histogram_plots>0 or a.line_plots>0):
        add_evidence(evidence,scores,'Oscillator',6,'visual_structure','Separate-window plotted series supports oscillator-style behavior')
    if _has(logic, r'\b(?:Alert|SendNotification|SendMail)\s*\('):
        add_evidence(evidence,scores,'Signal',8,'output','Alert/notification output detected'); tags.append('Alerts')
    if a.arrow_plots:
        add_evidence(evidence,scores,'Signal',14,'visual_output','Arrow/icon plot output detected'); tags.append('Event markers')
    if a.histogram_plots and a.display_location=='Separate Window':
        add_evidence(evidence,scores,'Oscillator',6,'visual_output','Separate-window histogram detected')
    if a.filling_plots:
        add_evidence(evidence,scores,'Volatility',6,'visual_output','Filled band/zone plot detected')
    if a.object_usage:
        add_evidence(evidence,scores,'Support/Resistance',6,'visual_output','Chart-object drawing detected')
    if _has(logic, r'\b(?:OBJ_HLINE|OBJ_TREND|OBJ_RECTANGLE|OBJ_FIBO)\b'):
        add_evidence(evidence,scores,'Support/Resistance',12,'visual_output','Support/resistance-style chart objects detected')
    if _has(logic, r'\b(?:ObjectCreate|Comment|Print)\s*\(') and not stds and not (a.line_plots or a.histogram_plots or a.arrow_plots or a.filling_plots):
        add_evidence(evidence,scores,'Utility',12,'utility_behavior','Object/text utility behavior detected without numeric plots')

    fname=filename.lower()
    filename_hints=[
        ('Trend', r'\b(?:trend|supertrend|ma|ema|sma|hull|tema|dema)\b'),
        ('Oscillator', r'\b(?:osc|rsi|stoch|cci|wpr|macd)\b'),
        ('Volatility', r'\b(?:atr|volatility|band|channel)\b'),
        ('Support/Resistance', r'\b(?:support|resistance|pivot|fibo|sr)\b'),
        ('Signal', r'\b(?:signal|arrow|alert|entry|exit)\b'),
        ('Volume', r'\b(?:volume|obv|mfi)\b'),
        ('Price Action', r'\b(?:candle|price\s*action|heiken|engulf|pinbar)\b'),
    ]
    for cat,pat in filename_hints:
        if scores.get(cat,0)>=8 and re.search(pat, fname, re.I):
            add_evidence(evidence,scores,cat,4,'filename_support','Filename agrees with source-derived evidence; used only as a weak reinforcement')

    ranked=sorted(scores.items(), key=lambda kv:kv[1], reverse=True)
    positive=[(c,s) for c,s in ranked if s>0]
    if not positive:
        return 'Unknown', [], sorted(set(tags)), sorted(set(techniques)), evidence, 20, 'Unknown', 'No reliable functional evidence detected'

    best,bestscore=positive[0]; secondscore=positive[1][1] if len(positive)>1 else 0
    best_evidence=[e for e in evidence if e['category']==best]
    source_kinds={e['kind'] for e in best_evidence if e['kind']!='filename_support'}
    independent=[c for c,s in positive if s>=max(14,bestscore*0.55)]
    if len(independent)>=2 and secondscore>=18 and secondscore/bestscore>=0.60:
        primary='Composite / Multi-Purpose'; secondary=independent[:5]
        confidence=min(92,int(55+min(25,bestscore/2)+min(12,secondscore/3)))
        status='High Confidence' if confidence>=85 else 'Probable'; reason='Multiple independent functional families have strong source evidence'
    elif bestscore>=20 and (len(source_kinds)>=2 or any(e['kind']=='active_builtin' for e in best_evidence)):
        primary=best; secondary=[c for c,s in positive[1:] if s>=max(10,bestscore*0.42)][:5]
        evidence_count=len(best_evidence); margin=max(0,bestscore-secondscore)
        confidence=min(97,int(60+min(20,bestscore/2)+min(9,margin/3)+min(8,evidence_count*2)))
        status='High Confidence' if confidence>=85 else ('Probable' if confidence>=70 else 'Needs Review')
        reason='' if status!='Needs Review' else 'Evidence is coherent but still below the automatic-trust threshold'
    else:
        primary='Custom / Specialized' if (a.custom_dependencies or len(evidence)>=2) else 'Unknown'
        secondary=[c for c,s in positive if s>=8][:5]
        confidence=min(69,int(35+bestscore))
        status='Needs Review'
        reason='Some functional evidence exists, but it is insufficient for a definitive standard category'
    return primary,secondary,sorted(set(tags)),sorted(set(techniques)),evidence,confidence,status,reason

def analyze(path: Path) -> Analysis:
    text=normalize_newlines(read_text(path)); code=strip_comments(text)
    platform='MQL5' if path.suffix.lower()=='.mq5' else 'MQL4'
    raw=path.read_bytes(); sha=hashlib.sha256(raw).hexdigest(); a=Analysis(str(path.resolve()),path.name,platform,sha,len(raw))
    converted=bool(re.search(r'AUTO[- ]CONVERTED\s+MQL4\s*[-=]>?\s*MQL5|compatibility helpers generated',text,re.I)); marker=re.search(r'=====\s*Converted source\s*=====',text,re.I)
    if converted and marker: logic=strip_comments(text[marker.end():]); compat=True
    else: logic,compat=logic_code(code)
    if platform=='MQL5' and converted: a.source_structure='Converted MQL4→MQL5'
    elif platform=='MQL5' and compat: a.source_structure='Compatibility-heavy MQL5'
    else: a.source_structure='Native/Legacy '+platform
    if re.search(r'#property\s+indicator_chart_window',code,re.I): a.display_location='Main Chart'
    if re.search(r'#property\s+indicator_separate_window',code,re.I): a.display_location='Separate Window'
    a.declared_buffers=extract_int_property(code,'indicator_buffers'); a.declared_plots=extract_int_property(code,'indicator_plots')
    a.active_buffers=len(set(re.findall(r'SetIndexBuffer\s*\(\s*(\d+)',logic,re.I)))
    draw=[]
    for dt in DRAW_TYPES:
        if re.search(r'\b'+dt+r'\b',logic,re.I) or re.search(r'#property\s+indicator_type\d+\s+'+dt+r'\b',code,re.I): draw.append(dt)
    a.draw_types=sorted(set(draw))
    style_calls=re.findall(r'(?:MQL4_)?SetIndexStyle\s*\(\s*([^,]+)\s*,\s*(DRAW_[A-Z0-9_]+)',logic,re.I)
    prop_calls=[(str(int(n)-1),t) for n,t in re.findall(r'#property\s+indicator_type(\d+)\s+(DRAW_[A-Z0-9_]+)',code,re.I)]
    plot_calls=re.findall(r'PlotIndexSetInteger\s*\(\s*([^,]+),\s*PLOT_DRAW_TYPE\s*,\s*(DRAW_[A-Z0-9_]+)',logic,re.I)
    types=[t.upper() for _,t in style_calls]+[t.upper() for _,t in prop_calls]+[t.upper() for _,t in plot_calls]
    a.line_plots=sum(t in ('DRAW_LINE','DRAW_COLOR_LINE','DRAW_SECTION','DRAW_ZIGZAG') for t in types); a.histogram_plots=sum(t in ('DRAW_HISTOGRAM','DRAW_HISTOGRAM2') for t in types); a.arrow_plots=sum(t in ('DRAW_ARROW','DRAW_COLOR_ARROW') for t in types); a.filling_plots=sum(t=='DRAW_FILLING' for t in types)
    # Which buffer INDEX is each line plot -- not just how many exist. Only trusted when the
    # index argument is a literal integer (#property indicator_typeN always is: N-1; a
    # SetIndexStyle/PlotIndexSetInteger call is only usable when its own index arg is a literal,
    # not a variable/expression -- those are left unresolved rather than guessed at). Sorted
    # ascending: ascending buffer index is the near-universal plot-stacking convention.
    line_idx=set()
    for idx_s,t in style_calls+prop_calls+plot_calls:
        if t.upper() not in ('DRAW_LINE','DRAW_COLOR_LINE','DRAW_SECTION','DRAW_ZIGZAG'): continue
        idx_s=idx_s.strip()
        if re.fullmatch(r'\d+',idx_s): line_idx.add(int(idx_s))
    a.line_buffer_indices=sorted(line_idx)
    a.object_usage=bool(re.search(r'ObjectCreate\s*\(|OBJ_(?:HLINE|VLINE|TREND|RECTANGLE|TEXT|LABEL|ARROW|FIBO)',logic,re.I))
    stds=[]
    for base in BUILTINS:
        for nm in (base,'MQL4_'+base):
            if count_actual_calls(logic,nm)>0: stds.append(nm)
    a.standard_indicators=sorted(set(stds))
    deps=[]
    for m in re.finditer(r'\biCustom\s*\((.*?)\)',logic,re.I|re.S):
        ss=re.findall(r'"([^"]+)"',m.group(1)[:800])
        if ss: deps.append(ss[0])
    a.custom_dependencies=sorted(set(deps)); a.visual_category=infer_visual(a); a.family_fingerprint=structural_fingerprint(logic)
    (a.primary_category,a.secondary_categories,a.behavior_tags,a.techniques,a.evidence,a.confidence,a.classification_status,a.review_reason)=classify(path.stem,logic,a.standard_indicators,a)
    if compat: a.warnings.append('Compatibility/helper functions were excluded from active indicator-call scoring')
    if a.visual_category=='Unknown': a.warnings.append('No reliable static plot style detected; the indicator may configure output dynamically or use objects only')
    if a.classification_status in ('Needs Review','Unknown'): a.warnings.append('Functional classification intentionally abstained from high-confidence labeling')
    return a
