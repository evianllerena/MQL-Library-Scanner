//+------------------------------------------------------------------+
//| DiffDump5.mq5 -- MT5 side of the MT4-vs-MT5 differential test     |
//| No Files\diff_rates.csv: exports the last NB completed EURUSD D1  |
//| bars to it and exits. Otherwise builds custom symbols from it:     |
//|   DIFF  = all NB bars, DIFFC = first NB-CUT bars,                  |
//|   DIFFI = first NB-CUT bars, then the last CUT bars are appended   |
//|           one at a time with the indicator loaded (bar-by-bar      |
//|           updating, as in a backtest or on a live chart).          |
//| Per line of Files\diff_jobs.txt (id|relative path) writes          |
//| Files\d5\<id>.<F|C|I>.bin: int bars,int nbuf, bars rows of MAXBUF  |
//| doubles (oldest bar first), plus a JSON line to diff_results.jsonl |
//+------------------------------------------------------------------+
#define NB     3000
#define CUT    150
#define MAXBUF 16
uint g_timeout=10000;
MqlRates g_r[];
void Out(string s){ int h=FileOpen("diff_results.jsonl",FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI); if(h==INVALID_HANDLE) return; FileSeek(h,0,SEEK_END); FileWriteString(h,s+"\n"); FileClose(h); }
// Some indicators only finish on a new tick; custom symbols get none. Add same-price ticks inside the LAST bar's
// day (OHLC unchanged), with a per-symbol clock so one symbol's ticks never land beyond another's last bar.
string g_ts[]; long g_tm[];
void PumpTick(string sym){
  MqlRates r[]; if(CopyRates(sym,PERIOD_D1,0,1,r)!=1) return;
  int k=-1; for(int i=0;i<ArraySize(g_ts);i++) if(g_ts[i]==sym) k=i;
  if(k<0){ k=ArraySize(g_ts); ArrayResize(g_ts,k+1); ArrayResize(g_tm,k+1); g_ts[k]=sym; g_tm[k]=0; }
  long lo=(long)r[0].time*1000+3600000, hi=(long)r[0].time*1000+86000000;
  if(g_tm[k]<lo || g_tm[k]>=hi) g_tm[k]=lo; g_tm[k]+=100;
  MqlTick tk[1]; tk[0].time=(datetime)(g_tm[k]/1000); tk[0].time_msc=g_tm[k];
  tk[0].bid=r[0].close; tk[0].ask=r[0].close+10*_Point; tk[0].last=r[0].close; tk[0].volume=1; tk[0].flags=TICK_FLAG_BID|TICK_FLAG_ASK;
  CustomTicksAdd(sym,tk);
}
bool Export(){
  SymbolSelect("EURUSD",true); MqlRates r[]; datetime d[];
  uint t0=GetTickCount(); while(Bars("EURUSD",PERIOD_D1)<NB+10 && GetTickCount()-t0<60000){ CopyTime("EURUSD",PERIOD_D1,0,NB+10,d); Sleep(200); }
  int n=CopyRates("EURUSD",PERIOD_D1,1,NB,r); if(n!=NB) return(false);   // shift 1: completed bars only
  int h=FileOpen("diff_rates.csv",FILE_WRITE|FILE_TXT|FILE_ANSI); if(h==INVALID_HANDLE) return(false);
  for(int i=0;i<n;i++) FileWriteString(h,StringFormat("%I64d,%.5f,%.5f,%.5f,%.5f,%I64d,%d,%I64d\n",(long)r[i].time,r[i].open,r[i].high,r[i].low,r[i].close,r[i].tick_volume,r[i].spread,r[i].real_volume));
  FileClose(h); return(true);
}
bool LoadCsv(){
  int h=FileOpen("diff_rates.csv",FILE_READ|FILE_TXT|FILE_ANSI); if(h==INVALID_HANDLE) return(false);
  ArrayResize(g_r,NB); int n=0;
  while(!FileIsEnding(h) && n<NB){ string p[]; if(StringSplit(FileReadString(h),',',p)<8) continue;
    g_r[n].time=(datetime)StringToInteger(p[0]); g_r[n].open=StringToDouble(p[1]); g_r[n].high=StringToDouble(p[2]); g_r[n].low=StringToDouble(p[3]); g_r[n].close=StringToDouble(p[4]);
    g_r[n].tick_volume=StringToInteger(p[5]); g_r[n].spread=(int)StringToInteger(p[6]); g_r[n].real_volume=StringToInteger(p[7]); n++; }
  FileClose(h); return(n==NB);
}
bool WaitBars(string sym,int n){ uint t0=GetTickCount(); while(Bars(sym,PERIOD_D1)!=n && GetTickCount()-t0<30000){ PumpTick(sym); Sleep(20); } return(Bars(sym,PERIOD_D1)==n); }
// custom symbol holding the first n bars of the frozen history
bool MakeSym(string sym,int n){
  bool exists=false; if(SymbolExist(sym,exists) && exists && Bars(sym,PERIOD_D1)==n) return(true);
  if(!exists && !CustomSymbolCreate(sym,"QA","EURUSD")) return(false);
  MqlRates k[]; ArrayCopy(k,g_r,0,0,n);
  CustomTicksDelete(sym,0,LONG_MAX); CustomRatesDelete(sym,0,D'3000.01.01');
  if(CustomRatesReplace(sym,k[0].time,k[n-1].time+86400,k)<=0) return(false);
  SymbolSelect(sym,true);
  return(WaitBars(sym,n));
}
int Calc(int h,string sym,string &st){
  int need=Bars(sym,PERIOD_D1),calc=-1; uint t0=GetTickCount(),lp=0;
  while(true){ calc=BarsCalculated(h); if(calc>=need || GetTickCount()-t0>=g_timeout) break; if(GetTickCount()-lp>=250){ PumpTick(sym); lp=GetTickCount(); } Sleep(5); }
  st=(calc>=need)?"ok":(calc<=0?"never_calculated":"too_slow"); return(calc);
}
int g_err=0;
int Dump(int h,string sym,string file){
  int n=Bars(sym,PERIOD_D1),nbuf=0; double m[]; ArrayResize(m,n*MAXBUF); ArrayInitialize(m,EMPTY_VALUE);
  for(int b=0;b<MAXBUF;b++){ double v[]; ArraySetAsSeries(v,false); ResetLastError(); int g=CopyBuffer(h,b,0,n,v); if(g<=0){ g_err=GetLastError(); break; } nbuf++;
    int off=n-g; for(int i=0;i<g;i++) m[(off+i)*MAXBUF+b]=v[i]; }
  int fh=FileOpen(file,FILE_WRITE|FILE_BIN); if(fh!=INVALID_HANDLE){ FileWriteInteger(fh,n); FileWriteInteger(fh,nbuf); FileWriteArray(fh,m); FileClose(fh); }
  return(nbuf);
}
string RunOne(string id,string rel,string sym,string tag){
  uint t0=GetTickCount(); string st; int calc=-1,nbuf=0; g_err=0;
  int h=iCustom(sym,PERIOD_D1,rel);
  if(h==INVALID_HANDLE) st="load_failed";
  else{
    calc=Calc(h,sym,st);
    if(st=="ok" && tag=="I"){                      // append the cut bars one at a time
      for(int k=NB-CUT;k<NB && st=="ok";k++){
        MqlRates one[1]; one[0]=g_r[k];
        if(CustomRatesUpdate(sym,one)<=0 || !WaitBars(sym,k+1)){ st="append_failed"; break; }
        calc=Calc(h,sym,st);
      }
    }
    if(st=="ok") nbuf=Dump(h,sym,"d5\\"+id+"."+tag+".bin");
    IndicatorRelease(h);
  }
  return("\""+tag+"\":{\"status\":\""+st+"\",\"calc\":"+IntegerToString(calc)+",\"nbuf\":"+IntegerToString(nbuf)+",\"err\":"+IntegerToString(g_err)+",\"ms\":"+IntegerToString(GetTickCount()-t0)+"}");
}
void OnStart(){
  uint c0=GetTickCount(); while(!TerminalInfoInteger(TERMINAL_CONNECTED) && GetTickCount()-c0<60000) Sleep(250);
  if(!FileIsExist("diff_rates.csv")){ Out(Export()?"{\"export\":\"ok\"}":"{\"export\":\"failed\"}"); TerminalClose(0); return; }
  bool ok=LoadCsv() && MakeSym("DIFF",NB) && MakeSym("DIFFC",NB-CUT);
  Out("{\"session\":\"start\",\"ok\":"+(ok?"true":"false")+",\"bars\":"+IntegerToString(Bars("DIFF",PERIOD_D1))+",\"cut\":"+IntegerToString(Bars("DIFFC",PERIOD_D1))+"}");
  if(!ok){ TerminalClose(0); return; }
  int jf=FileOpen("diff_jobs.txt",FILE_READ|FILE_TXT|FILE_ANSI); if(jf==INVALID_HANDLE){ TerminalClose(0); return; }
  while(!FileIsEnding(jf) && !IsStopped()){
    string p[]; if(StringSplit(FileReadString(jf),'|',p)<2) continue;
    string id=p[0]; Out("{\"id\":\""+id+"\",\"phase\":\"begin\"}");
    string res="{\"id\":\""+id+"\","+RunOne(id,p[1],"DIFF","F");
    if(StringFind(res,"\"ok\"")>0){
      res+=","+RunOne(id,p[1],"DIFFC","C");
      if(MakeSym("DIFFI",NB-CUT)) res+=","+RunOne(id,p[1],"DIFFI","I"); else res+=",\"I\":{\"status\":\"setup_failed\"}";
    }
    Out(res+"}");
  }
  FileClose(jf);
  int f=FileOpen("diff_done.flag",FILE_WRITE|FILE_TXT|FILE_ANSI); if(f!=INVALID_HANDLE){ FileWriteString(f,"done"); FileClose(f); }
  TerminalClose(0);
}
