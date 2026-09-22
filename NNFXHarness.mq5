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
input double TP1mult            = 1.0;
input double RiskPct            = 1.0;
input double MinBeyondATR       = 1.0;
input ulong  MagicNumber        = 20260001;
input string RunTag             = "";      // unique per batch run -- keeps each run's trade log from colliding
input double PipSize            = 0.0001;  // for the human-readable pips column in the trade/result log only

// --- bridge-too-far (C1 ONLY; never runs for C2) ---------------------
input bool   EnableBridgeTooFar = true;
input int    BridgeTooFarBars   = 7;     // >= this many bars with an unbroken C1 direction => SKIP
input int    BridgeTooFarLookback = 60;  // hard cap on how far back to scan

// --- continuation trades ---------------------------------------------
input bool   EnableContinuation = true;
// STUB (NNFX_RULESET_THE_TRUTH.txt SS12: "C2 ... full rules ... Implement these as
// stubbed ... never guessed"): SS7 (continuation) names ONLY C1's fresh signal as the
// trigger and explicitly lists exactly two ignored rules (1xATR-beyond-baseline, the
// volume filter) -- it says nothing about C2 either way. Defaulting to true (require
// C2) because NOT checking it would be inventing an unstated third exemption; set
// false only once VP's exact wording on this is available, per SS12's own instruction.
input bool   RequireC2ForContinuation = true;

// --- chart layout: each role in its OWN subwindow, never shared -----
#define WIN_MAIN   0
#define WIN_C1     1
#define WIN_C2     2
#define WIN_VOLUME 3
#define WIN_ATR    4
#define WIN_EXIT   5   // only used when CandidateSlot==SLOT_EXIT

//====================================================================
// STATE
//====================================================================

int h_baseline = INVALID_HANDLE;
int h_c1       = INVALID_HANDLE; // ALWAYS two-line by construction (candidate or reference)
int h_c2       = INVALID_HANDLE; // ALWAYS zero-cross by construction (candidate or reference)
int h_volume   = INVALID_HANDLE;
int h_atr      = INVALID_HANDLE; // fixed, always iATR
int h_exit     = INVALID_HANDLE; // only when CandidateSlot==SLOT_EXIT
double g_c2ZeroRef = 0.0;        // whichever zero_reference is actually in play for h_c2
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

   // Exit candidate (only relevant when testing SLOT_EXIT; hard-exit-on-flip then reads THIS
   // instead of C1). When CandidateSlot != SLOT_EXIT the default exit rule (hard-exit on C1
   // flip) is used and h_exit stays unused.
   if(CandidateSlot == SLOT_EXIT)
   {
      h_exit = iCustom(_Symbol, PERIOD_D1, CandidatePath);
      g_exitBuf = CandidateBufMain;
   }

   if(h_baseline==INVALID_HANDLE || h_c1==INVALID_HANDLE || h_c2==INVALID_HANDLE ||
      h_volume==INVALID_HANDLE || h_atr==INVALID_HANDLE ||
      (CandidateSlot==SLOT_EXIT && h_exit==INVALID_HANDLE))
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
   if(CandidateSlot == SLOT_EXIT)
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
   double avg=AvgBuffer(h_volume,0,shift,VolumeAvgPeriod);
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

void OpenPosition(int dir,double atrVal,bool isContinuation)
{
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
   g_entryPriceForLog=price; g_half1PipsSet=false; g_half2PipsSet=false; g_half1Pips=0; g_half2Pips=0;
   g_tradeIsContinuation=isContinuation;

   LogTrade(isContinuation?"enter:continuation":"enter:standard",dir,totalLots,price,sl,tp1,atrVal,
            isContinuation?"enter:continuation":"enter:standard");
}

double NormalizeLots(double lots)
{
   double step = SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_STEP);
   return(MathFloor(lots/step)*step);
}

// Half #1 TP1 hit -> move half #2's stop to breakeven, then trail. Called every tick so the
// trail keeps up; the ENTRY decision itself only runs once per closed daily bar. Also detects
// EITHER half closing on its own (broker-side TP/SL fill, not a call from this EA) so g_posDir
// never gets stuck non-zero after a natural stop-out.
void ManageOpenPosition(double atrVal)
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
   if(g_half2.open && posInfo.SelectByTicket(g_half2.ticket) && g_half1.tp1Hit)
   {
      double newSL = g_posDir>0 ? SymbolInfoDouble(_Symbol,SYMBOL_BID)-SLmult*atrVal
                                 : SymbolInfoDouble(_Symbol,SYMBOL_ASK)+SLmult*atrVal;
      bool improves = g_posDir>0 ? newSL>posInfo.StopLoss() : newSL<posInfo.StopLoss();
      if(improves) trade.PositionModify(g_half2.ticket, newSL, posInfo.TakeProfit());
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

   // --- hard exit: whole position closes immediately on a C1 flip (or, when testing
   //     SLOT_EXIT, on the candidate exit indicator's own flip instead). --------------
   if(g_posDir!=0)
   {
      int flipSource = (CandidateSlot==SLOT_EXIT) ? ExitCandidateDirection(shift) : c1dir;
      if(flipSource!=0 && flipSource!=g_posDir)
      {
         g_lastExitDir=g_posDir;
         CloseAllHalves("exit:c1_flip");
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

   // --- CONTINUATION entry: fresh same-direction C1 signal, sequence unbroken since the
   //     last exit. Ignores the 1xATR-beyond-baseline rule AND the volume filter. Money
   //     management (ATR sizing, 1.5xATR SL, halves, trail) is UNCHANGED. -----------------
   if(EnableContinuation && g_lastExitDir!=0 && g_continuationOK && g_trendDir==g_lastExitDir)
   {
      bool freshC1Signal = (c1dir!=0 && c1dir!=prevC1DirSeen && c1dir==g_lastExitDir);
      bool c2OkForContinuation = (!RequireC2ForContinuation) || (c2dir==g_lastExitDir);
      if(freshC1Signal && c2OkForContinuation)
      {
         OpenPosition(g_lastExitDir, atrVal, true);
         g_lastExitDir=0; // consumed
         return;
      }
   }

   // --- STANDARD baseline entry: ALL of baseline-cross, C1, C2, Volume must agree. --------
   if(crossDir==0) return;
   if(c1dir!=crossDir || c2dir!=crossDir) return;
   if(!VolumePasses(shift,crossDir)) { LogTrade("skip",crossDir,0,0,0,0,atrVal,"skip:volume_filter"); return; }

   // Pullback / 1xATR-beyond-baseline no-trade zone (standard entries only).
   double baseNow; BufVal(h_baseline,g_baselineBuf,shift,baseNow);
   double closeNow = iClose(_Symbol,PERIOD_D1,shift);
   if(MathAbs(closeNow-baseNow) > MinBeyondATR*atrVal)
   {
      LogTrade("skip",crossDir,0,0,0,0,atrVal,"skip:beyond_1xATR");
      return;
   }

   // Bridge-too-far: C1 ONLY, never C2 (C1 is two-line-cross by construction here).
   if(EnableBridgeTooFar)
   {
      int run = C1DirectionRunLength(shift,crossDir);
      if(run>=BridgeTooFarBars)
      {
         LogTrade("skip",crossDir,0,0,0,0,atrVal,"skip:bridge_too_far");
         return;
      }
   }

   // THIS standard entry's own baseline cross is "the original entry" NNFX_RULESET_THE_TRUTH.txt SS7
   // tracks from -- only a genuine standard entry may (re)arm a trackable sequence, never a bare
   // cross with no trade behind it (see the reset comment above for why that distinction matters).
   g_trendDir=crossDir; g_continuationOK=true;
   OpenPosition(crossDir, atrVal, false);
}

int ExitCandidateDirection(int shift)
{
   // Only meaningful when CandidateSlot==SLOT_EXIT; treated as a generic two-state
   // directional read (>0 => long-side agree, <0 => short-side agree) off buffer 0.
   double v;
   if(!BufVal(h_exit,g_exitBuf,shift,v)) return(0);
   if(v>0) return(+1);
   if(v<0) return(-1);
   return(0);
}

//====================================================================
// EVENTS
//====================================================================

void OnTick()
{
   double atrVal;
   if(BufVal(h_atr,0,1,atrVal)) ManageOpenPosition(atrVal); // trail runs every tick

   datetime t0 = iTime(_Symbol,PERIOD_D1,0);
   if(t0==g_lastBarTime) return; // only act once, on the bar that just closed
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
                "bridge_skips", DoubleToString((double)g_summaryContinuationTrades,0), "continuation_trades");
      FileClose(h);
   }
   return(g_summaryPipsSum);
}
//+------------------------------------------------------------------+
