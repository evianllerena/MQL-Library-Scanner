//+------------------------------------------------------------------+
//| NNFXHarness.mq5                                                    |
//| NNFX slot-tester harness EA. Implements NNFX_RULESET_THE_TRUTH.txt |
//| exactly, using the slot design locked during Step-2 slot-mapping   |
//| review (see NNFX_INTEGRATION_PLAN.txt / STEP2 prompt):             |
//|   CONFIRMATION_1 (C1) = two-line-cross, bridge-too-far ELIGIBLE.    |
//|   CONFIRMATION_2 (C2) = zero-cross, read at its OWN zero_reference  |
//|                         (never assumed 0; never 70/30).             |
//|   BASELINE            = single line on the price chart.             |
//|   VOLUME              = loss filter (pass/fail).                    |
//|   ATR                 = FIXED engine machinery, never auditioned.   |
//|   EXIT                = optional 5th candidate role (see below).    |
//|                                                                      |
//| One EA drives every audition: CandidateSlot selects which ONE role  |
//| is under test; the other three (Baseline/C1/C2/Volume) are held at  |
//| a fixed reference backdrop supplied via Ref* inputs (VP's teaching  |
//| defaults conceptually -- the actual .ex5 paths are never hardcoded, |
//| per NNFX_RULESET_THE_TRUTH.txt SS8: "expose them as settings").      |
//|                                                                      |
//| Closed DAILY bars only. Never reads the forming bar (shift 0).      |
//+------------------------------------------------------------------+
#property copyright "MQL Indicator Library"
#property version   "1.00"
#property strict

#include <Trade/Trade.mqh>
#include <Trade/PositionInfo.mqh>

//====================================================================
// INPUTS
//====================================================================

enum ENUM_NNFX_SLOT
{
   SLOT_CONFIRMATION_1, // C1: two-line-cross (bridge-too-far eligible)
   SLOT_CONFIRMATION_2, // C2: zero-cross vs zero_reference
   SLOT_BASELINE,       // single line on the price chart
   SLOT_VOLUME,         // loss filter
   SLOT_EXIT            // optional: candidate exit signal vs default trail
};

input ENUM_NNFX_SLOT CandidateSlot = SLOT_CONFIRMATION_1; // which role the candidate under test fills

// --- candidate indicator (whichever role CandidateSlot selects) -----
input string CandidatePath      = "";  // iCustom short name / relative path of the candidate .ex5/.ex4
input int    CandidateBufFast   = 0;   // C1 fast line buffer   (used only if CandidateSlot==SLOT_CONFIRMATION_1)
input int    CandidateBufSlow   = 1;   // C1 slow line buffer   (used only if CandidateSlot==SLOT_CONFIRMATION_1)
input int    CandidateBufMain   = 0;   // main buffer for C2 / BASELINE / VOLUME / EXIT candidates
input double CandidateZeroReference = 0.0; // C2 candidate's zero_reference (from backtest_results.zero_reference --
                                            // NEVER assumed here; must be supplied by the slot-mapping pipeline)

// --- fixed reference backdrop (VP's teaching defaults; concrete paths are operator-supplied) ---
input string RefBaselinePath = ""; input int RefBaselineBuf = 0;                              // e.g. a 20-period SMA
// Passed as the reference baseline indicator's OWN input parameters (e.g. MQL5's shipped
// "Examples\Custom Moving Average": Period,Shift,Method) -- NOT buffer indices. Defaults
// give a genuine 20-period SMA per NNFX_RULESET_THE_TRUTH.txt SS8's "20 SMA" teaching default.
input int             RefBaselineMAPeriod = 20;
input int             RefBaselineMAShift  = 0;
input ENUM_MA_METHOD  RefBaselineMAMethod = MODE_SMA;
input string RefC1Path       = ""; input int RefC1BufFast = 0; input int RefC1BufSlow = 1;     // fixed two-line C1
input string RefC2Path       = ""; input int RefC2Buf = 0;     input double RefC2ZeroReference = 0.0; // fixed zero-cross C2
input string RefVolumePath   = ""; input int RefVolumeBuf = 0;                                 // fixed volume/volatility filter
// Fixed reference exit (X2). Empty path = no reference exit indicator (pre-FIX-2 behavior).
// nnfx_backtest_batch.py uses "Examples\Momentum" (default period 14), centre line 100.
input string RefExitPath     = ""; input int RefExitBuf = 0;   input double RefExitZeroReference = 100.0;

// --- generic Volume/Volatility pass-rule (held IDENTICAL across every candidate during Stage-1
//     so ranking is apples-to-apples per NNFX_RULESET_THE_TRUTH.txt SS9: "hold baseline+C1+C2
//     fixed, run WITH and WITHOUT the candidate filter". The RULE ITSELF is a documented,
//     adjustable convention -- current reading above its own N-bar average -- not a per-
//     indicator invented threshold.) --------------------------------------------------------
input int    VolumeAvgPeriod    = 20;
input double VolumeThresholdMult = 1.0;

// --- money management (ATR is FIXED -- never the thing under test) --
input int    ATRPeriod          = 14;
input double SLmult             = 1.5;
// --- T4 trailing (FIX 6; mirrors NNFXParams trail_*): after TP1 the runner sits at breakeven
//     until a candle CLOSES TrailActivateATR x ATR beyond entry; then it trails TrailDistanceATR x
//     ATR behind each close, once per candle, never backward. TrailActivateATR=0 = pre-FIX-6.
input double TrailActivateATR   = 2.0;
input double TrailDistanceATR   = 1.5;
input string TrailStep          = "per_candle"; // the only step: trailing runs once per closed bar
input string TrailATRRef        = "entry";      // "entry" = ATR at entry (default) | "live" = closed bar's ATR
input double TP1mult            = 1.0;
input double RiskPct            = 2.0;     // M2 (label A): 2% per trade
input double MinBeyondATR       = 1.0;
input ulong  MagicNumber        = 20260001;
input string RunTag             = "";      // unique per batch run -- keeps each run's trade log from colliding
input double PipSize            = 0.0001;  // for the human-readable pips column in the trade/result log only

// --- bridge-too-far (C1 ONLY; never runs for C2) ---------------------
input bool   EnableBridgeTooFar = true;
input int    BridgeTooFarBars   = 7;     // >= this many bars with an unbroken C1 direction => SKIP
input int    BridgeTooFarLookback = 60;  // hard cap on how far back to scan
// E5 counting convention (user UNRESOLVED, so a SETTING): "before_cross" (default) counts C1's
// unbroken run ENDING ON THE CANDLE BEFORE the baseline cross; "include_cross" counts through
// the cross candle itself (pre-FIX-4 behavior). Mirrors NNFXParams.bridge_count_from.
input string BridgeCountFrom    = "before_cross"; // or "include_cross"
input bool   TwoLineC1          = true;  // bridge-too-far applies to a two-line C1 only

// --- FIX 4 entry types (NNFX_RULESET_THE_TRUTH.txt SS4). Each a SETTING, default ON; turn one
//     off to isolate it in the backtest. Mirrors nnfx_engine.py NNFXParams. ------------------
input bool   EnableC1TriggerEntry = true; // E1: fresh C1 cross while already on-side (no bridge check)
input bool   EnablePullbackEntry  = true; // E3: valid-but-beyond-1xATR setup enters on a pullback
input bool   EnableOneCandleRule  = true; // E4: exactly one lagging confirmation gets ONE candle

// --- continuation trades ---------------------------------------------
input bool   EnableContinuation = true;
// E6 CONTINUATION trigger (Decision #6, user-confirmed 2026-10-04). Every mode ignores the
// volume filter AND the 1xATR-beyond rule; money management is unchanged. Mirrors
// NNFXParams.continuation_mode:
//   "vp_c2"     (default): C2 flips back in the trade's direction AND C1 is currently on-side.
//   "lesson11" : the exit indicator flips back to the trade's direction (needs an exit indicator).
//   "c1_signal" (legacy, pre-FIX-5): a fresh C1 signal back in the direction, gated by
//               RequireC2ForContinuation.
input string ContinuationMode = "vp_c2"; // "vp_c2" | "lesson11" | "c1_signal"
// Applies ONLY to ContinuationMode="c1_signal". SS12 stub: C2's role in the C1-signal
// continuation was never defined; default requires C2 agreement.
input bool   RequireC2ForContinuation = true;

// --- X4 wrong-side-baseline exit (NNFX_RULESET_THE_TRUTH.txt SS5, label B) --
input bool   EnableBaselineExit = true;  // false = pre-FIX-1 behavior
// --- X2 exit indicator (SS5): read vs its OWN zero_reference, never 0 ------
input bool   EnableExitIndicator = true; // no-op unless an exit indicator is active

// --- FIX 7 news hook (RULEBOOK_ADDENDUM_NEWS.txt; WIRED-BUT-DORMANT; mirrors NNFXParams) --------
// Calendar = CSV in MQL5\Files with a header row and columns currency,timestamp_utc,impact
// (ForexFactory shape; timestamp like 2025-04-02T12:30:00Z, UTC). Only High rows are kept.
// N1: no new trade if either currency of the pair has a High event in (t, t+NewsWindowHours],
//     t = the closed bar's close in UTC. X5: close an open trade with such an event ahead per
//     X5Cutoff: "handoff" (default) = losing or < X5ProfitATR x ATR-at-entry in profit;
//     "not_past_tp1" = TP1 not hit yet. Sources disagree -> flagged for the user.
input bool   EnableNewsFilter     = false;
input string NewsCalendarFile     = "";
input double NewsWindowHours      = 24.0;
input string X5Cutoff             = "handoff"; // "handoff" | "not_past_tp1"
input double X5ProfitATR          = 1.0;
input double ServerUTCOffsetHours = 0.0;       // broker server time - UTC (e.g. 2 or 3)

// --- chart layout: each role in its OWN subwindow, never shared -----
#define WIN_MAIN   0
#define WIN_C1     1
#define WIN_C2     2
#define WIN_VOLUME 3
#define WIN_ATR    4
#define WIN_EXIT   5   // only used when an exit indicator is active

//====================================================================
// STATE
//====================================================================

int h_baseline = INVALID_HANDLE;
int h_c1       = INVALID_HANDLE; // ALWAYS two-line by construction (candidate or reference)
int h_c2       = INVALID_HANDLE; // ALWAYS zero-cross by construction (candidate or reference)
int h_volume   = INVALID_HANDLE;
int h_atr      = INVALID_HANDLE; // fixed, always iATR
int h_exit     = INVALID_HANDLE; // candidate (SLOT_EXIT) or RefExitPath; INVALID = no exit indicator
bool g_exitWanted = false;       // an exit indicator was requested, so a failed handle is fatal
double g_c2ZeroRef = 0.0;        // whichever zero_reference is actually in play for h_c2
double g_exitZeroRef = 0.0;      // whichever zero_reference is actually in play for h_exit
// Actual buffer indices used to READ each handle via CopyBuffer -- set once in OnInit from
// whichever of Candidate*/Ref* is actually active for that role. Never hardcoded elsewhere.
int g_baselineBuf = 0, g_c1BufFast = 0, g_c1BufSlow = 1, g_c2Buf = 0, g_volumeBuf = 0, g_exitBuf = 0;

CTrade      trade;
CPositionInfo posInfo;

datetime g_lastBarTime = 0;

// Continuation state: tracks the trend sequence since the last standard-entry baseline cross,
// independent of any single trade -- resets the moment price closes on the OPPOSITE side of
// the baseline from the tracked direction.
int      g_trendDir        = 0;    // +1 long-side sequence, -1 short-side sequence, 0 = none active
bool     g_continuationOK  = false; // true while the sequence is unbroken since the last exit
int      g_lastExitDir     = 0;    // direction of the most recent exit (for continuation re-entry)
int      g_lastC1DirSeen   = 0;    // updated UNCONDITIONALLY every closed bar (see OnNewDailyBar) so a
                                    // same-direction re-entry right after a flip-exit is still "fresh"
int      g_lastC2DirSeen   = 0;    // FIX 5: previous C2 direction (fresh C2 flip back, vp_c2)
int      g_lastExitIndDirSeen = 0; // FIX 5: previous exit-indicator direction (lesson11)

// E3: a standard/E1 setup was valid but beyond 1xATR; remember its direction until price pulls
// back in or agreement breaks. E4: exactly one confirmation lagged on the cross bar; remember the
// direction + the bar counter so the grace is exactly ONE candle. g_barIndex is a monotonic
// closed-bar counter (shift can't be used -- it is always 1 for the bar being decided).
int      g_barIndex            = -1;
int      g_pendingPullbackDir  = 0;
int      g_pendingOneCandleDir = 0;
int      g_pendingOneCandleBar = -1;

// Position halves (hedging account required -- two independent tickets per symbol/direction)
struct Half
{
   ulong  ticket;
   bool   open;
   bool   tp1Hit;
};
Half g_half1, g_half2;
int  g_posDir = 0; // +1 long, -1 short, 0 flat

// Round-trip trade result tracking: both halves are equal-sized (see OpenPosition), so the
// blended per-lot pip result of the whole trade is the simple average of each half's own
// pip result -- reported once, when BOTH halves are finally flat.
double g_entryPriceForLog = 0.0;
string   g_newsCur[];         // FIX 7: High-impact events (currency, UTC time), loaded in OnInit
datetime g_newsTime[];
long     g_summaryNewsSkips=0;
double g_atrAtEntry = 0.0;   // FIX 6: T4 reference ATR (TrailATRRef="entry")
bool   g_trailActive = false; // FIX 6: sticky once a close reached TrailActivateATR beyond entry
bool   g_half1PipsSet = false, g_half2PipsSet = false;
double g_half1Pips = 0.0, g_half2Pips = 0.0;
bool   g_tradeIsContinuation = false;
string g_tradeLogFile = "";
long   g_summaryTrades=0, g_summaryWins=0, g_summaryLosses=0;
double g_summaryPipsSum=0.0;
long   g_summaryBridgeSkips=0, g_summaryContinuationTrades=0;

//====================================================================
// LIFECYCLE
//====================================================================

int OnInit()
{
   trade.SetExpertMagicNumber(MagicNumber);
   g_tradeLogFile = "NNFX_" + (RunTag=="" ? "run" : RunTag) + "_tradelog.csv";

   // Candidates run on their OWN compiled defaults (iCustom with no extra parameters) --
   // per NNFX_BACKTESTER_BUILD_SPEC.txt Part A, "you never write per-indicator code"; auditioning
   // means taking the indicator as shipped, not tuning it. Buffer indices (which line is which)
   // are genuinely operator-supplied per candidate, since an arbitrary scanned file's buffer
   // layout isn't something the scanner records precisely enough to infer automatically.

   // Baseline
   if(CandidateSlot == SLOT_BASELINE)
   {
      h_baseline = iCustom(_Symbol, PERIOD_D1, CandidatePath);
      g_baselineBuf = CandidateBufMain;
   }
   else
   {
      // Fixed reference: a genuine 20-period SMA (NNFX_RULESET_THE_TRUTH.txt SS8) via MQL5's
      // shipped "Examples\Custom Moving Average" -- its OWN inputs (Period,Shift,Method) are
      // passed explicitly so the default MODE_SMMA doesn't silently replace the intended SMA.
      h_baseline = iCustom(_Symbol, PERIOD_D1, RefBaselinePath, RefBaselineMAPeriod, RefBaselineMAShift, RefBaselineMAMethod);
      g_baselineBuf = RefBaselineBuf;
   }

   // C1 (two-line, always)
   if(CandidateSlot == SLOT_CONFIRMATION_1)
   {
      h_c1 = iCustom(_Symbol, PERIOD_D1, CandidatePath);
      g_c1BufFast = CandidateBufFast; g_c1BufSlow = CandidateBufSlow;
   }
   else
   {
      h_c1 = iCustom(_Symbol, PERIOD_D1, RefC1Path);
      g_c1BufFast = RefC1BufFast; g_c1BufSlow = RefC1BufSlow;
   }

   // C2 (zero-cross, always) -- zero_reference travels WITH whichever indicator is in play
   if(CandidateSlot == SLOT_CONFIRMATION_2)
   {
      h_c2 = iCustom(_Symbol, PERIOD_D1, CandidatePath);
      g_c2Buf = CandidateBufMain;
      g_c2ZeroRef = CandidateZeroReference;
   }
   else
   {
      // Fixed reference: RVI (NNFX_RULESET_THE_TRUTH.txt SS8 -- "VP used the RVI ... for
      // teaching"), MQL5's shipped "Examples\RVI", its own compiled default period, buffer 0
      // (the RVI value itself, not the signal line) -- a true zero-line oscillator, zero_reference=0.
      h_c2 = iCustom(_Symbol, PERIOD_D1, RefC2Path);
      g_c2Buf = RefC2Buf;
      g_c2ZeroRef = RefC2ZeroReference;
   }

   // Volume/Volatility filter
   if(CandidateSlot == SLOT_VOLUME)
   {
      h_volume = iCustom(_Symbol, PERIOD_D1, CandidatePath);
      g_volumeBuf = CandidateBufMain;
   }
   else
   {
      // Fixed reference: MQL5's shipped "Examples\Volumes" on its own compiled default (tick volume).
      h_volume = iCustom(_Symbol, PERIOD_D1, RefVolumePath);
      g_volumeBuf = RefVolumeBuf;
   }

   // ATR: fixed, never a candidate
   h_atr = iATR(_Symbol, PERIOD_D1, ATRPeriod);

   // Exit indicator (X2). Runs ALONGSIDE the C1-flip and baseline exits, never instead of
   // them -- whichever fires first closes the trade. zero_reference travels with whichever
   // indicator is in play, exactly like C2.
   if(CandidateSlot == SLOT_EXIT)
   {
      h_exit = iCustom(_Symbol, PERIOD_D1, CandidatePath);
      g_exitBuf = CandidateBufMain;
      g_exitZeroRef = CandidateZeroReference;
      g_exitWanted = true;
   }
   else if(RefExitPath != "")
   {
      h_exit = iCustom(_Symbol, PERIOD_D1, RefExitPath);
      g_exitBuf = RefExitBuf;
      g_exitZeroRef = RefExitZeroReference;
      g_exitWanted = true;
   }

   // News calendar (FIX 7): dormant unless switched on AND a file is named; a named file that
   // can't be read is fatal, never a silent "no news".
   if(EnableNewsFilter && NewsCalendarFile!="" && !LoadNewsCalendar())
   {
      Print("NNFXHarness: cannot read news calendar ", NewsCalendarFile);
      return(INIT_FAILED);
   }

   if(h_baseline==INVALID_HANDLE || h_c1==INVALID_HANDLE || h_c2==INVALID_HANDLE ||
      h_volume==INVALID_HANDLE || h_atr==INVALID_HANDLE ||
      (g_exitWanted && h_exit==INVALID_HANDLE))
   {
      Print("NNFXHarness: failed to create one or more indicator handles");
      return(INIT_FAILED);
   }

   // Chart layout: each role in its own subwindow, never shared.
   ChartIndicatorAdd(0, WIN_MAIN, h_baseline);
   ChartIndicatorAdd(0, WIN_C1, h_c1);
   ChartIndicatorAdd(0, WIN_C2, h_c2);
   ChartIndicatorAdd(0, WIN_VOLUME, h_volume);
   ChartIndicatorAdd(0, WIN_ATR, h_atr);
   if(g_exitWanted)
      ChartIndicatorAdd(0, WIN_EXIT, h_exit);

   g_half1.open=false; g_half1.tp1Hit=false; g_half1.ticket=0;
   g_half2.open=false; g_half2.tp1Hit=false; g_half2.ticket=0;

   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   if(h_baseline!=INVALID_HANDLE) IndicatorRelease(h_baseline);
   if(h_c1!=INVALID_HANDLE)       IndicatorRelease(h_c1);
   if(h_c2!=INVALID_HANDLE)       IndicatorRelease(h_c2);
   if(h_volume!=INVALID_HANDLE)   IndicatorRelease(h_volume);
   if(h_atr!=INVALID_HANDLE)      IndicatorRelease(h_atr);
   if(h_exit!=INVALID_HANDLE)     IndicatorRelease(h_exit);
}

//====================================================================
// SMALL READ HELPERS (shift-indexed, closed bars only: shift>=1)
//====================================================================

bool BufVal(int handle,int bufferIndex,int shift,double &out)
{
   double b[];
   ArraySetAsSeries(b,true);
   if(CopyBuffer(handle,bufferIndex,shift,1,b)!=1) return(false);
   out=b[0];
   return(true);
}

// C1 direction at shift: +1 fast>slow, -1 fast<slow, 0 undecided/unreadable
int C1Direction(int shift)
{
   double f,s;
   if(!BufVal(h_c1,g_c1BufFast,shift,f) || !BufVal(h_c1,g_c1BufSlow,shift,s)) return(0);
   if(f>s) return(+1);
   if(f<s) return(-1);
   return(0);
}

// C2 direction at shift vs its OWN zero_reference: +1 above, -1 below, 0 undecided.
// ZERO LINE (or documented centerline) ONLY -- never 70/30, never overbought/oversold.
int C2Direction(int shift)
{
   double v;
   if(!BufVal(h_c2,g_c2Buf,shift,v)) return(0);
   if(v>g_c2ZeroRef) return(+1);
   if(v<g_c2ZeroRef) return(-1);
   return(0);
}

// Baseline CROSS-AND-CLOSE at shift: returns +1/-1 only on the bar the close actually crossed
// to that side (not merely "is currently on that side" -- that would fire every bar).
int BaselineCrossClosed(int shift)
{
   double baseNow, basePrev, closeNow, closePrev;
   if(!BufVal(h_baseline,g_baselineBuf,shift,baseNow) || !BufVal(h_baseline,g_baselineBuf,shift+1,basePrev)) return(0);
   closeNow  = iClose(_Symbol,PERIOD_D1,shift);
   closePrev = iClose(_Symbol,PERIOD_D1,shift+1);
   bool nowAbove  = closeNow  > baseNow;
   bool prevAbove = closePrev > basePrev;
   if(nowAbove && !prevAbove) return(+1);
   if(!nowAbove && prevAbove) return(-1);
   return(0);
}

// Current side of baseline (not requiring a fresh cross) -- used for continuation reset checks.
int BaselineSide(int shift)
{
   double baseNow, closeNow;
   if(!BufVal(h_baseline,g_baselineBuf,shift,baseNow)) return(0);
   closeNow = iClose(_Symbol,PERIOD_D1,shift);
   if(closeNow>baseNow) return(+1);
   if(closeNow<baseNow) return(-1);
   return(0);
}

double AvgBuffer(int handle,int bufferIndex,int shift,int period)
{
   double b[];
   ArraySetAsSeries(b,true);
   if(CopyBuffer(handle,bufferIndex,shift,period,b)!=period) return(0.0);
   double sum=0.0;
   for(int i=0;i<period;i++) sum+=b[i];
   return(sum/period);
}

// VOLUME filter pass/fail: candidate reading above its own N-bar average * threshold.
// This mechanism is held IDENTICAL for every Volume/Volatility candidate during Stage-1
// so results are comparable; only the underlying indicator (candidate vs reference) varies.
bool VolumePasses(int shift,int dir)
{
   double v;
   if(!BufVal(h_volume,g_volumeBuf,shift,v)) return(false);
   // G10: the average must come from the SAME configured line as today's value (was buffer 0).
   double avg=AvgBuffer(h_volume,g_volumeBuf,shift,VolumeAvgPeriod);
   return(v >= avg*VolumeThresholdMult);
}

// Bridge-too-far: count back from `shift` how many consecutive bars C1 has held the given
// direction, unbroken. Returns the count (capped at BridgeTooFarLookback).
int C1DirectionRunLength(int shift,int dir)
{
   int count=0;
   for(int i=shift;i<shift+BridgeTooFarLookback;i++)
   {
      if(C1Direction(i)!=dir) break;
      count++;
   }
   return(count);
}

// E5. Two-line C1 ONLY (suppressed when TwoLineC1 is false). "before_cross" counts C1's unbroken
// run ending on the candle BEFORE the cross (shift+1, one bar further back); a run >=
// BridgeTooFarBars means C1 led the cross by too many candles -> skip. Mirrors _bridge_too_far().
bool BridgeTooFar(int shift,int dir)
{
   if(!EnableBridgeTooFar || !TwoLineC1) return(false);
   int from = (BridgeCountFrom=="before_cross") ? shift+1 : shift;
   return(C1DirectionRunLength(from,dir)>=BridgeTooFarBars);
}

// FIX 7: read the calendar's High-impact rows. ISO-8601 "2025-04-02T12:30:00Z" -> StringToTime's
// "2025.04.02 12:30:00" (the trailing Z / UTC is the file's contract). Mirrors load_news_calendar().
bool LoadNewsCalendar()
{
   int h=FileOpen(NewsCalendarFile,FILE_READ|FILE_CSV|FILE_ANSI,',');
   if(h==INVALID_HANDLE) return(false);
   for(int i=0;i<3 && !FileIsEnding(h);i++) FileReadString(h); // header row
   while(!FileIsEnding(h))
   {
      string cur=FileReadString(h), ts=FileReadString(h), impact=FileReadString(h);
      StringToLower(impact); StringToUpper(cur);
      StringTrimLeft(impact); StringTrimRight(impact); StringTrimLeft(cur); StringTrimRight(cur);
      if(impact!="high") continue;
      StringReplace(ts,"-","."); StringReplace(ts,"T"," "); StringReplace(ts,"Z","");
      int n=ArraySize(g_newsCur);
      ArrayResize(g_newsCur,n+1); ArrayResize(g_newsTime,n+1);
      g_newsCur[n]=cur; g_newsTime[n]=StringToTime(ts);
   }
   FileClose(h);
   return(true);
}

// N1/X5: a High event for EITHER currency of the pair in (t, t+NewsWindowHours], t = the close of
// the bar at `shift` in UTC. Always false while dormant. Mirrors NNFXEngine._news_ahead().
bool NewsAhead(int shift)
{
   if(!EnableNewsFilter || ArraySize(g_newsCur)==0) return(false);
   datetime t = iTime(_Symbol,PERIOD_D1,shift) + 86400 - (datetime)(ServerUTCOffsetHours*3600);
   datetime until = t + (datetime)(NewsWindowHours*3600);
   string base=StringSubstr(_Symbol,0,3), quote=StringSubstr(_Symbol,3,3);
   for(int i=0;i<ArraySize(g_newsCur);i++)
      if((g_newsCur[i]==base || g_newsCur[i]==quote) && g_newsTime[i]>t && g_newsTime[i]<=until)
         return(true);
   return(false);
}

// Rules that veto ANY new trade (every entry type, continuation included). Logs the skip.
bool EntryBlocked(int dir,double atrVal)
{
   if(NewsAhead(1)) { LogTrade("skip",dir,0,0,0,0,atrVal,"skip:news"); g_summaryNewsSkips++; return(true); }
   return(false);
}

//====================================================================
// TRADE LOG (auditable trade log per NNFX_BACKTESTER_BUILD_SPEC.txt Part A)
//====================================================================

void LogTrade(string action,int dir,double lots,double price,double sl,double tp,double atrVal,string reason)
{
   int h=FileOpen(g_tradeLogFile,FILE_READ|FILE_WRITE|FILE_CSV|FILE_ANSI,',');
   if(h==INVALID_HANDLE) return;
   FileSeek(h,0,SEEK_END);
   FileWrite(h, TimeToString(TimeCurrent(),TIME_DATE|TIME_SECONDS), action,
             dir==1?"LONG":(dir==-1?"SHORT":"-"), DoubleToString(lots,2),
             DoubleToString(price,_Digits), DoubleToString(sl,_Digits), DoubleToString(tp,_Digits),
             DoubleToString(atrVal,_Digits), reason, "");
   FileClose(h);
   if(reason=="skip:bridge_too_far") g_summaryBridgeSkips++;
   if(reason=="enter:continuation") g_summaryContinuationTrades++;
}

// One line per fully-closed round trip: both halves flat, blended per-lot pips logged.
void LogTradeClosed(int dir,double pips,bool wasContinuation)
{
   int h=FileOpen(g_tradeLogFile,FILE_READ|FILE_WRITE|FILE_CSV|FILE_ANSI,',');
   if(h==INVALID_HANDLE) return;
   FileSeek(h,0,SEEK_END);
   FileWrite(h, TimeToString(TimeCurrent(),TIME_DATE|TIME_SECONDS), "trade_closed",
             dir==1?"LONG":(dir==-1?"SHORT":"-"), "", "", "", "",
             "", wasContinuation?"continuation":"standard", DoubleToString(pips,1));
   FileClose(h);
   g_summaryTrades++;
   if(pips>0) g_summaryWins++; else if(pips<0) g_summaryLosses++;
   g_summaryPipsSum+=pips;
}

// Actual fill price of a position's closing deal -- correct regardless of whether it closed
// via TP, SL, or a manual PositionClose (position ticket == opening order ticket on a
// hedging account, per MQL5 documentation).
double GetClosePrice(ulong positionTicket)
{
   if(!HistorySelectByPosition((long)positionTicket)) return(0.0);
   int total=HistoryDealsTotal();
   for(int i=total-1;i>=0;i--)
   {
      ulong dealTicket=HistoryDealGetTicket(i);
      if(dealTicket==0) continue;
      if((ENUM_DEAL_ENTRY)HistoryDealGetInteger(dealTicket,DEAL_ENTRY)==DEAL_ENTRY_OUT)
         return HistoryDealGetDouble(dealTicket,DEAL_PRICE);
   }
   return(0.0);
}

double PipsFor(int dir,double entry,double exitp)
{
   if(PipSize<=0) return(0.0);
   return((dir>0 ? (exitp-entry) : (entry-exitp))/PipSize);
}

// Called once both halves are confirmed flat -- logs the blended round-trip result and
// resets per-trade tracking so the next trade starts clean.
void FinalizeTradeIfFlat(int dir)
{
   if(!g_half1PipsSet || !g_half2PipsSet) return;
   double blended=(g_half1Pips+g_half2Pips)/2.0;
   LogTradeClosed(dir, blended, g_tradeIsContinuation);
   g_half1PipsSet=false; g_half2PipsSet=false; g_half1Pips=0; g_half2Pips=0;
}

//====================================================================
// MONEY MANAGEMENT
//====================================================================

double LotsForRisk(double stopDistancePoints)
{
   double riskMoney = AccountInfoDouble(ACCOUNT_EQUITY)*RiskPct/100.0;
   double tickValue = SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_SIZE);
   if(tickSize<=0 || tickValue<=0 || stopDistancePoints<=0) return(0.0);
   double lossPerLot = (stopDistancePoints/tickSize)*tickValue;
   if(lossPerLot<=0) return(0.0);
   double lots = riskMoney/lossPerLot;
   double step = SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_STEP);
   double minLot = SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MIN);
   lots = MathFloor(lots/step)*step;
   if(lots<minLot) lots=0.0; // too small to size two halves at the required risk -- skip, do not force-trade
   return(lots);
}

//====================================================================
// ENTRY / EXIT
//====================================================================

void CloseAllHalves(string reason)
{
   int dir=g_posDir;
   if(g_half1.open && posInfo.SelectByTicket(g_half1.ticket))
   {
      trade.PositionClose(g_half1.ticket);
      if(!g_half1PipsSet){ g_half1Pips=PipsFor(dir,g_entryPriceForLog,trade.ResultPrice()); g_half1PipsSet=true; }
   }
   if(g_half2.open && posInfo.SelectByTicket(g_half2.ticket))
   {
      trade.PositionClose(g_half2.ticket);
      if(!g_half2PipsSet){ g_half2Pips=PipsFor(dir,g_entryPriceForLog,trade.ResultPrice()); g_half2PipsSet=true; }
   }
   g_half1.open=false; g_half2.open=false; g_posDir=0;
   LogTrade("exit_all",0,0,0,0,0,0,reason);
   FinalizeTradeIfFlat(dir);
}

void OpenPosition(int dir,double atrVal,bool isContinuation,string reason="")
{
   if(reason=="") reason = isContinuation?"enter:continuation":"enter:standard";
   double price = dir>0 ? SymbolInfoDouble(_Symbol,SYMBOL_ASK) : SymbolInfoDouble(_Symbol,SYMBOL_BID);
   double slDist = SLmult*atrVal;
   double sl = dir>0 ? price-slDist : price+slDist;
   double tp1 = dir>0 ? price+TP1mult*atrVal : price-TP1mult*atrVal;
   double point = SymbolInfoDouble(_Symbol,SYMBOL_POINT);
   double stopDistPoints = slDist; // price units; LotsForRisk converts via tick size/value
   double totalLots = LotsForRisk(stopDistPoints);
   if(totalLots<=0.0) return;
   double halfLots = MathMax(SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MIN), NormalizeLots(totalLots/2.0));

   bool ok1 = dir>0 ? trade.Buy(halfLots,_Symbol,price,sl,tp1,"H1") : trade.Sell(halfLots,_Symbol,price,sl,tp1,"H1");
   bool ok2 = dir>0 ? trade.Buy(halfLots,_Symbol,price,sl,0.0,"H2") : trade.Sell(halfLots,_Symbol,price,sl,0.0,"H2");
   if(ok1){ g_half1.ticket=trade.ResultOrder(); g_half1.open=true; g_half1.tp1Hit=false; }
   if(ok2){ g_half2.ticket=trade.ResultOrder(); g_half2.open=true; g_half2.tp1Hit=false; }
   g_posDir=dir;
   g_atrAtEntry=atrVal; g_trailActive=false;
   g_entryPriceForLog=price; g_half1PipsSet=false; g_half2PipsSet=false; g_half1Pips=0; g_half2Pips=0;
   g_tradeIsContinuation=isContinuation;

   LogTrade(reason,dir,totalLots,price,sl,tp1,atrVal,reason);
}

// Open a fresh (non-continuation) position and arm the continuation tracker. Shared by
// E1/E2/E3/E4 -- all of them establish an "original entry" SS7 tracks from. Mirrors _open().
void OpenFresh(int dir,double atrVal,string reason)
{
   if(EntryBlocked(dir,atrVal)) return; // vetoed: opens nothing, arms nothing
   g_trendDir=dir; g_continuationOK=true;
   OpenPosition(dir, atrVal, false, reason);
}

double NormalizeLots(double lots)
{
   double step = SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_STEP);
   return(MathFloor(lots/step)*step);
}

// Half #1 TP1 hit -> move half #2's stop to breakeven. Called every tick so breakeven and
// broker-side fills are picked up promptly; the T4 trail itself only moves on a newly closed
// daily bar (newBar), read off that bar's close, like the engine. Also detects EITHER half
// closing on its own (broker-side TP/SL fill, not a call from this EA) so g_posDir never gets
// stuck non-zero after a natural stop-out.
void ManageOpenPosition(double atrVal,bool newBar)
{
   if(g_posDir==0) return;
   int dir=g_posDir;

   if(g_half1.open && !posInfo.SelectByTicket(g_half1.ticket))
   {
      g_half1.open=false; g_half1.tp1Hit=true;
      if(!g_half1PipsSet){ g_half1Pips=PipsFor(dir,g_entryPriceForLog,GetClosePrice(g_half1.ticket)); g_half1PipsSet=true; }
   }
   if(!g_half1.open && g_half2.open && !g_half1.tp1Hit)
   {
      // Half #1 closed (TP1 or otherwise) and half #2 is still open: move to breakeven, then trail.
      g_half1.tp1Hit=true;
      double entryPrice = posInfo.SelectByTicket(g_half2.ticket) ? posInfo.PriceOpen() : 0.0;
      if(entryPrice>0)
         trade.PositionModify(g_half2.ticket, entryPrice, posInfo.TakeProfit());
   }
   if(newBar && g_half2.open && posInfo.SelectByTicket(g_half2.ticket) && g_half1.tp1Hit)
   {
      // T4: activation is sticky once a close reaches TrailActivateATR beyond entry.
      double refATR = (TrailATRRef=="entry") ? g_atrAtEntry : atrVal;
      double closeNow = iClose(_Symbol,PERIOD_D1,1);
      if(!g_trailActive)
         g_trailActive = TrailActivateATR<=0 || (closeNow-g_entryPriceForLog)*g_posDir >= TrailActivateATR*refATR;
      if(g_trailActive)
      {
         double newSL = g_posDir>0 ? closeNow-TrailDistanceATR*refATR : closeNow+TrailDistanceATR*refATR;
         bool improves = g_posDir>0 ? newSL>posInfo.StopLoss() : newSL<posInfo.StopLoss();
         if(improves) trade.PositionModify(g_half2.ticket, newSL, posInfo.TakeProfit());
      }
   }
   if(g_half2.open && !posInfo.SelectByTicket(g_half2.ticket))
   {
      g_half2.open=false;
      if(!g_half2PipsSet){ g_half2Pips=PipsFor(dir,g_entryPriceForLog,GetClosePrice(g_half2.ticket)); g_half2PipsSet=true; }
   }
   if(!g_half2.open)
   {
      g_posDir=0; g_half1.open=false;
      if(!g_half1PipsSet){ g_half1Pips=g_half2Pips; g_half1PipsSet=true; } // SL hit before TP1: both halves closed at the same price
      FinalizeTradeIfFlat(dir);
   }
}

//====================================================================
// MAIN DECISION (runs ONCE per newly-closed Daily bar)
//====================================================================

void OnNewDailyBar()
{
   int shift=1; // last CLOSED bar; shift 0 is the still-forming bar, never read
   g_barIndex++; // one per closed bar, like the engine's idx -- so E4's "next bar" is exactly +1
   double atrVal;
   if(!BufVal(h_atr,0,shift,atrVal) || atrVal<=0) return;

   int c1dir = C1Direction(shift);
   // Captured BEFORE overwriting, and the overwrite happens unconditionally right here --
   // every bar, regardless of which branch below returns early -- so a same-direction
   // re-entry right after an exit is correctly recognized as "fresh" even though the
   // hard-exit branch above returns early on the very bar C1 flips. (A prior version had
   // this update nested inside the continuation-eligibility block only; the Python mirror
   // in nnfx_engine.py had the identical bug, caught by nnfx_selftest.py -- fixed in both.)
   int prevC1DirSeen = g_lastC1DirSeen;
   g_lastC1DirSeen = c1dir;
   int c2dir = C2Direction(shift);
   int crossDir = BaselineCrossClosed(shift);
   int side = BaselineSide(shift);
   int exitdir = (h_exit!=INVALID_HANDLE) ? ExitDirection(shift) : 0;
   // FIX 5: same capture-then-overwrite-every-bar discipline as g_lastC1DirSeen, so a fresh
   // C2 / exit-indicator "flip back" is detected even on a bar an exit fires.
   int prevC2 = g_lastC2DirSeen;
   g_lastC2DirSeen = c2dir;
   int prevExitInd = g_lastExitIndDirSeen;
   g_lastExitIndDirSeen = exitdir;

   // --- X3 hard exit: whole position closes immediately on a C1 flip. ---------------
   if(g_posDir!=0 && c1dir!=0 && c1dir!=g_posDir)
   {
      g_lastExitDir=g_posDir;
      CloseAllHalves("exit:c1_flip");
      // FIX 5 edge: a C1-flip exit bar that ALSO closed on the wrong side of the baseline
      // resets the continuation sequence (mirrors the engine's reset on this exit path).
      if(g_trendDir!=0 && side!=0 && side!=g_trendDir){ g_trendDir=0; g_continuationOK=false; }
   }

   // --- X2: exit indicator turned against the trade (vs its own zero_reference). -------
   if(g_posDir!=0 && EnableExitIndicator && h_exit!=INVALID_HANDLE)
   {
      if(exitdir!=0 && exitdir!=g_posDir)
      {
         g_lastExitDir=g_posDir;
         CloseAllHalves("exit:exit_indicator");
      }
   }

   // --- X4: a close on the wrong side of the baseline closes what is left. The
   //     continuation reset just below then sees the same wrong-side close. -----------
   if(g_posDir!=0 && EnableBaselineExit && side!=0 && side!=g_posDir)
   {
      g_lastExitDir=g_posDir;
      CloseAllHalves("exit:baseline_cross");
   }

   // --- X5 (FIX 7, dormant): a High event for either currency is within the window -> close
   //     what is left at this close, per X5Cutoff. ---------------------------------------------
   if(g_posDir!=0 && NewsAhead(shift))
   {
      bool smallOrLosing = (X5Cutoff=="not_past_tp1") ? !g_half1.tp1Hit
                         : (iClose(_Symbol,PERIOD_D1,shift)-g_entryPriceForLog)*g_posDir < X5ProfitATR*g_atrAtEntry;
      if(smallOrLosing)
      {
         g_lastExitDir=g_posDir;
         CloseAllHalves("exit:news");
      }
   }

   // --- continuation bookkeeping: the sequence resets the moment a candle closes on the
   //     OPPOSITE side of the baseline from the tracked direction. This is a STICKY reset --
   //     once broken, only a genuine standard entry (not just any bare cross) may re-arm a
   //     trackable sequence; see the standard-entry block below for where g_trendDir/
   //     g_continuationOK actually get (re)armed. (A prior version re-armed on ANY crossDir!=0
   //     unconditionally, which silently undid the reset on the very same bar whenever the
   //     reset bar was itself a fresh cross to the new side -- caught by tracing a real choppy
   //     EURUSD stretch through the Python mirror, never by the simpler Part C fixtures; fixed
   //     in both nnfx_engine.py and here.) --------------------------------------------------
   if(g_trendDir!=0 && side!=0 && side!=g_trendDir)
   {
      g_trendDir=0; g_continuationOK=false;
   }

   if(g_posDir!=0) return; // already in a trade; nothing else to evaluate this bar

   // --- CONTINUATION entry (E6): same direction as the last trade, sequence unbroken since
   //     the original entry. EVERY mode ignores the 1xATR-beyond-baseline rule AND the volume
   //     filter. Money management (ATR sizing, 1.5xATR SL, halves, trail) is UNCHANGED. The
   //     trigger depends on ContinuationMode. ----------------------------------------------
   if(EnableContinuation && g_lastExitDir!=0 && g_continuationOK && g_trendDir==g_lastExitDir)
   {
      int d=g_lastExitDir;
      bool trigger=false;
      if(ContinuationMode=="vp_c2")
         trigger = (c2dir==d && prevC2!=d && c1dir==d);
      else if(ContinuationMode=="lesson11")
         trigger = (exitdir==d && prevExitInd!=d);
      else // "c1_signal" (legacy)
      {
         bool freshC1Signal = (c1dir!=0 && c1dir!=prevC1DirSeen && c1dir==d);
         bool c2OkForContinuation = (!RequireC2ForContinuation) || (c2dir==d);
         trigger = freshC1Signal && c2OkForContinuation;
      }
      if(trigger)
      {
         if(EntryBlocked(d,atrVal)) return; // vetoed: nothing opens; the sequence stays as it was
         OpenPosition(d, atrVal, true);
         g_lastExitDir=0; // consumed
         return;
      }
   }

   // --- shared entry conditions for this bar (1xATR-beyond-baseline zone; volume) ---------
   double baseNow; BufVal(h_baseline,g_baselineBuf,shift,baseNow);
   double closeNow = iClose(_Symbol,PERIOD_D1,shift);
   bool withinATR = !(MathAbs(closeNow-baseNow) > MinBeyondATR*atrVal);

   // --- E3 PULLBACK resolution: we already committed to waiting on a setup that was valid
   //     but beyond 1xATR. Enter when price closes back WITHIN 1xATR with everything still
   //     agreeing; drop it if agreement breaks; else keep waiting. -------------------------
   if(g_pendingPullbackDir!=0)
   {
      int d=g_pendingPullbackDir;
      bool broke = (side==-d) || (c1dir!=0 && c1dir!=d) || (c2dir!=0 && c2dir!=d);
      if(broke)
         g_pendingPullbackDir=0;
      else
      {
         if(withinATR && side==d && c1dir==d && c2dir==d && VolumePasses(shift,d) && !BridgeTooFar(shift,d))
         {
            g_pendingPullbackDir=0;
            OpenFresh(d, atrVal, "enter:pullback");
         }
         return; // entered, or still on-side and agreeing but not yet back within 1xATR -> wait
      }
   }

   // --- E4 ONE-CANDLE resolution: exactly one input lagged on the PREVIOUS bar; the grace is
   //     exactly one candle. Enter if the laggard has caught up AND price is still within
   //     1xATR; otherwise the grace expires and this bar is evaluated fresh. ---------------
   if(g_pendingOneCandleDir!=0)
   {
      int d=g_pendingOneCandleDir;
      bool isNextBar = (g_barIndex==g_pendingOneCandleBar+1);
      g_pendingOneCandleDir=0; g_pendingOneCandleBar=-1;
      if(isNextBar && withinATR && side==d && c1dir==d && c2dir==d && VolumePasses(shift,d) && !BridgeTooFar(shift,d))
      {
         OpenFresh(d, atrVal, "enter:one_candle");
         return;
      }
   }

   // --- E2 STANDARD baseline entry (cross-triggered): baseline-cross + C1 + C2 + Volume +
   //     within 1xATR + not bridge-too-far. A valid-but-too-far setup arms E3; a setup with
   //     exactly ONE lagging confirmation arms E4. Only an actual entry (re)arms the
   //     continuation sequence, never a bare cross (see the reset comment above). ---------
   if(crossDir!=0)
   {
      bool c1ok = (c1dir==crossDir), c2ok = (c2dir==crossDir);
      if(c1ok && c2ok)
      {
         if(!VolumePasses(shift,crossDir)) { LogTrade("skip",crossDir,0,0,0,0,atrVal,"skip:volume_filter"); return; }
         if(!withinATR)
         {
            if(EnablePullbackEntry && !BridgeTooFar(shift,crossDir)) g_pendingPullbackDir=crossDir;
            LogTrade("skip",crossDir,0,0,0,0,atrVal,"skip:beyond_1xATR");
            return;
         }
         if(BridgeTooFar(shift,crossDir)) { LogTrade("skip",crossDir,0,0,0,0,atrVal,"skip:bridge_too_far"); return; }
         OpenFresh(crossDir, atrVal, "enter:standard");
         return;
      }
      // exactly one of C1/C2 lags on the cross bar -> one-candle grace (E4)
      if(EnableOneCandleRule && withinATR && VolumePasses(shift,crossDir) && !BridgeTooFar(shift,crossDir))
      {
         int lagging = (c1ok?0:1) + (c2ok?0:1);
         if(lagging==1)
         {
            g_pendingOneCandleDir=crossDir; g_pendingOneCandleBar=g_barIndex;
            LogTrade("skip",crossDir,0,0,0,0,atrVal,"skip:one_candle_wait");
         }
      }
      return;
   }

   // --- E1 C1-TRIGGERED entry (no fresh cross this bar): C1 FRESHLY crosses (it was on the
   //     OPPOSITE side last bar) while price is ALREADY on the correct side, within 1xATR,
   //     C2 + volume agree. Bridge-too-far is NOT applied to E1 (the cross already happened;
   //     C1 is the trigger, so the "bridge" does not map). ---------------------------------
   if(EnableC1TriggerEntry)
   {
      int d=c1dir;
      bool freshC1 = (d!=0 && prevC1DirSeen==-d);
      if(freshC1 && side==d && c2dir==d)
      {
         if(!VolumePasses(shift,d)) { LogTrade("skip",d,0,0,0,0,atrVal,"skip:volume_filter"); return; }
         if(!withinATR)
         {
            if(EnablePullbackEntry) g_pendingPullbackDir=d;
            LogTrade("skip",d,0,0,0,0,atrVal,"skip:beyond_1xATR");
            return;
         }
         OpenFresh(d, atrVal, "enter:c1_trigger");
      }
   }
}

int ExitDirection(int shift)
{
   // Centre-line read against the active exit indicator's OWN zero_reference (never 0):
   // above => long side, below => short side. Mirrors nnfx_engine.exit_direction().
   double v;
   if(!BufVal(h_exit,g_exitBuf,shift,v)) return(0);
   if(v>g_exitZeroRef) return(+1);
   if(v<g_exitZeroRef) return(-1);
   return(0);
}

//====================================================================
// EVENTS
//====================================================================

void OnTick()
{
   double atrVal;
   datetime t0 = iTime(_Symbol,PERIOD_D1,0);
   bool newBar = (t0!=g_lastBarTime);
   if(BufVal(h_atr,0,1,atrVal)) ManageOpenPosition(atrVal,newBar); // fills every tick; T4 trail on newBar only

   if(!newBar) return; // only act once, on the bar that just closed
   g_lastBarTime=t0;

   OnNewDailyBar();
}

// Called once by the Strategy Tester after a single test pass completes. Writes ONE summary
// line: this EA's own round-trip pip tally (trades/wins/losses/expectancy, bridge_skips,
// continuation_trades -- none of which the tester's native stats know about) alongside MT5's
// own native TesterStatistics() for profit_factor and max_drawdown (authoritative, not
// something this EA computes itself, to avoid re-deriving numbers MT5 already gets right).
double OnTester()
{
   int h=FileOpen(g_tradeLogFile,FILE_READ|FILE_WRITE|FILE_CSV|FILE_ANSI,',');
   if(h!=INVALID_HANDLE)
   {
      FileSeek(h,0,SEEK_END);
      double expectancy = g_summaryTrades>0 ? g_summaryPipsSum/g_summaryTrades : 0.0;
      FileWrite(h, TimeToString(TimeCurrent(),TIME_DATE|TIME_SECONDS), "summary", "-",
                DoubleToString((double)g_summaryTrades,0), DoubleToString((double)g_summaryWins,0),
                DoubleToString((double)g_summaryLosses,0), DoubleToString(expectancy,2),
                "trades=wins=losses=expectancy_pips",
                DoubleToString(TesterStatistics(STAT_PROFIT_FACTOR),3),
                DoubleToString(TesterStatistics(STAT_EQUITY_DDREL_PERCENT),2));
      FileWrite(h, TimeToString(TimeCurrent(),TIME_DATE|TIME_SECONDS), "summary_rule_audit", "-",
                DoubleToString((double)g_summaryBridgeSkips,0), "", "", "",
                "bridge_skips", DoubleToString((double)g_summaryContinuationTrades,0), "continuation_trades",
                DoubleToString((double)g_summaryNewsSkips,0), "news_skips");
      FileClose(h);
   }
   return(g_summaryPipsSum);
}
//+------------------------------------------------------------------+
