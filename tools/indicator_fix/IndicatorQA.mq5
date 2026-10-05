//+------------------------------------------------------------------+
//| IndicatorQA.mq5 -- runtime QA for converted indicators            |
//| Reads Files\qa_jobs.txt (one line per job: id|relative path),      |
//| appends one JSON line per job to Files\qa_results.jsonl.           |
//| Tests per indicator:                                               |
//|  1. loads + calculates every bar on EURUSD and GBPJPY D1           |
//|  2. produces real values (some buffer non-empty and varying)       |
//|  3. no look-ahead / no mirrored dates: same values on the shared   |
//|     bars of QA_CUT (history ending CUT bars early) and QA_FULL     |
//+------------------------------------------------------------------+
#property script_show_inputs
#define CUT     150      // bars QA_CUT stops before QA_FULL
#define CMP     400      // shared bars compared (the CMP bars before the cut)
#define REPAINT 60       // differences only within this many bars of the cut = repaint, deeper = misaligned
#define MAXBUF  16

int fh;
uint g_timeout = 10000;

void Out(string s){ int h=FileOpen("qa_results.jsonl",FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI); if(h==INVALID_HANDLE) return; FileSeek(h,0,SEEK_END); FileWriteString(h,s+"\n"); FileClose(h); }
string Esc(string s){ StringReplace(s,"\\","\\\\"); StringReplace(s,"\"","\\\""); return s; }

bool WaitHealthy(string sym,uint limit_ms){
  int need=Bars(sym,PERIOD_D1); uint t0=GetTickCount();
  while(GetTickCount()-t0<limit_ms){
    int h=iCustom(sym,PERIOD_D1,"Examples\\Momentum"); int c=-1; uint s0=GetTickCount();
    while(h!=INVALID_HANDLE && GetTickCount()-s0<750){ c=BarsCalculated(h); if(c>=need) break; Sleep(25); }
    if(h!=INVALID_HANDLE) IndicatorRelease(h);
    if(c>=need) return(true);
    Sleep(250);
  }
  return(false);
}

// load rel on sym; returns handle (caller releases) and status
int LoadCalc(string sym,string rel,string &status,int &calc,uint &ms){
  int need=Bars(sym,PERIOD_D1); uint t0=GetTickCount(); calc=-1;
  ResetLastError();
  int h=iCustom(sym,PERIOD_D1,rel);
  if(h==INVALID_HANDLE){ status="load_failed"; ms=GetTickCount()-t0; return(h); }
  while(true){ calc=BarsCalculated(h); if(calc>=need || GetTickCount()-t0>=g_timeout) break; Sleep(25); }
  ms=GetTickCount()-t0;
  if(calc>=need) status="ok"; else status=(calc<=0)?"never_calculated":"too_slow";
  return(h);
}

// number of readable buffers and value statistics over the newest `count` bars
int BufferStats(int h,int count,string &json,bool &has_values){
  json="["; has_values=false; int nb=0;
  for(int b=0;b<MAXBUF;b++){
    double v[]; ArraySetAsSeries(v,true);
    int got=CopyBuffer(h,b,0,count,v);
    if(got<=0) break;
    nb++;
    int empty=0,finite=0,distinct=0; double last=0; bool first=true;
    double s[]; ArrayResize(s,0);
    for(int i=0;i<got;i++){
      if(v[i]==EMPTY_VALUE || !MathIsValidNumber(v[i])){ empty++; continue; }
      finite++; int n=ArraySize(s); ArrayResize(s,n+1); s[n]=v[i];
    }
    ArraySort(s);
    for(int i=0;i<ArraySize(s);i++){ if(first || s[i]!=last){ distinct++; last=s[i]; first=false; } }
    if(distinct>=2) has_values=true;
    json+=(nb>1?",":"")+"{\"b\":"+IntegerToString(b)+",\"finite\":"+IntegerToString(finite)+",\"empty\":"+IntegerToString(empty)+",\"distinct\":"+IntegerToString(distinct)+"}";
  }
  json+="]";
  return(nb);
}

// compare buffers on QA_CUT vs QA_FULL over the CMP shared bars before the cut
string AlignTest(string rel,string &verdict){
  string st1,st2; int c1,c2; uint m1,m2;
  if(!WaitHealthy("QA_CUT",30000) ){ verdict="queue_unhealthy"; return("{}"); }
  int h1=LoadCalc("QA_CUT",rel,st1,c1,m1);
  if(st1!="ok"){ if(h1!=INVALID_HANDLE) IndicatorRelease(h1); verdict="cut_"+st1; return("{}"); }
  if(!WaitHealthy("QA_FULL",30000)){ IndicatorRelease(h1); verdict="queue_unhealthy"; return("{}"); }
  int h2=LoadCalc("QA_FULL",rel,st2,c2,m2);
  if(st2!="ok"){ IndicatorRelease(h1); if(h2!=INVALID_HANDLE) IndicatorRelease(h2); verdict="full_"+st2; return("{}"); }
  int compared=0,diffs=0,deepest=-1,nb=0;
  for(int b=0;b<MAXBUF;b++){
    double a[],f[]; ArraySetAsSeries(a,true); ArraySetAsSeries(f,true);
    int ga=CopyBuffer(h1,b,0,CMP,a);              // QA_CUT newest CMP bars  (index j = j bars before the cut)
    int gf=CopyBuffer(h2,b,CUT,CMP,f);            // the SAME bars on QA_FULL (CUT bars further back)
    if(ga<=0 || gf<=0) break;
    nb++;
    int n=MathMin(ga,gf);
    for(int j=0;j<n;j++){
      bool ea=(a[j]==EMPTY_VALUE || !MathIsValidNumber(a[j])), ef=(f[j]==EMPTY_VALUE || !MathIsValidNumber(f[j]));
      if(ea && ef) continue;
      compared++;
      bool same=(ea==ef) && (MathAbs(a[j]-f[j])<=1e-8*MathMax(1.0,MathAbs(a[j])));
      if(!same){ diffs++; if(j>deepest) deepest=j; }
    }
  }
  IndicatorRelease(h1); IndicatorRelease(h2);
  if(compared==0) verdict="no_comparable_values";
  else if(diffs==0) verdict="aligned";
  else if(deepest<REPAINT) verdict="repaints";
  else verdict="misaligned";
  return("{\"buffers\":"+IntegerToString(nb)+",\"compared\":"+IntegerToString(compared)+",\"diffs\":"+IntegerToString(diffs)+",\"deepest\":"+IntegerToString(deepest)+"}");
}

bool MakeCustom(string name,string origin,int drop){
  MqlRates r[]; ArraySetAsSeries(r,false);
  int n=CopyRates(origin,PERIOD_D1,0,Bars(origin,PERIOD_D1),r);
  if(n<=CUT+CMP+200) return(false);
  if(!SymbolSelect(name,false)) {}
  CustomSymbolDelete(name);
  if(!CustomSymbolCreate(name,"QA",origin)) return(false);
  MqlRates k[]; ArrayResize(k,n-drop); for(int i=0;i<n-drop;i++) k[i]=r[i];
  if(CustomRatesReplace(name,k[0].time,k[n-drop-1].time+86400,k)<=0) return(false);
  SymbolSelect(name,true);
  uint t0=GetTickCount(); while(Bars(name,PERIOD_D1)<n-drop && GetTickCount()-t0<30000) Sleep(100);
  return(Bars(name,PERIOD_D1)>=n-drop);
}

void OnStart(){
  uint c0=GetTickCount(); while(!TerminalInfoInteger(TERMINAL_CONNECTED) && GetTickCount()-c0<60000) Sleep(250);
  SymbolSelect("EURUSD",true); SymbolSelect("GBPJPY",true);
  datetime d[]; CopyTime("EURUSD",PERIOD_D1,0,3000,d); CopyTime("GBPJPY",PERIOD_D1,0,3000,d);
  bool cuts_ok=MakeCustom("QA_FULL","EURUSD",0) && MakeCustom("QA_CUT","EURUSD",CUT);
  Out("{\"session\":\"start\",\"custom_symbols\":"+(cuts_ok?"true":"false")+",\"bars_eurusd\":"+IntegerToString(Bars("EURUSD",PERIOD_D1))+
      ",\"bars_cut\":"+IntegerToString(Bars("QA_CUT",PERIOD_D1))+",\"bars_full\":"+IntegerToString(Bars("QA_FULL",PERIOD_D1))+"}");
  int jf=FileOpen("qa_jobs.txt",FILE_READ|FILE_TXT|FILE_ANSI);
  if(jf==INVALID_HANDLE){ Out("{\"fatal\":\"no jobs\"}"); TerminalClose(0); return; }
  while(!FileIsEnding(jf) && !IsStopped()){
    string line=FileReadString(jf); if(StringLen(line)<3) continue;
    string parts[]; if(StringSplit(line,'|',parts)<2) continue;
    string id=parts[0], rel=parts[1];
    Out("{\"id\":\""+Esc(id)+"\",\"phase\":\"begin\"}");
    string res="{\"id\":\""+Esc(id)+"\"";
    // 1+2: real symbols
    string worst="ok"; bool any_values=false; string stats="";
    string syms[]={"EURUSD","GBPJPY"};
    for(int s=0;s<2;s++){
      string st; int calc; uint ms; string js="[]"; bool hv=false; int nb=0;
      if(!WaitHealthy(syms[s],60000)){ st="queue_unhealthy"; ms=0; calc=-1; }
      else{
        int h=LoadCalc(syms[s],rel,st,calc,ms);
        if(st=="ok"){ nb=BufferStats(h,400,js,hv); if(hv) any_values=true; }
        if(h!=INVALID_HANDLE) IndicatorRelease(h);
      }
      if(st!="ok" && worst=="ok") worst=st;
      res+=",\""+syms[s]+"\":{\"status\":\""+st+"\",\"calc\":"+IntegerToString(calc)+",\"ms\":"+IntegerToString(ms)+",\"nbuf\":"+IntegerToString(nb)+",\"stats\":"+js+"}";
    }
    res+=",\"load\":\""+worst+"\",\"has_values\":"+(any_values?"true":"false");
    // 3: alignment / look-ahead
    string verdict="skipped";
    if(worst=="ok" && any_values && cuts_ok){ string aj=AlignTest(rel,verdict); res+=",\"align_detail\":"+aj; }
    res+=",\"align\":\""+verdict+"\"}";
    Out(res);
  }
  FileClose(jf);
  int f=FileOpen("qa_done.flag",FILE_WRITE|FILE_TXT|FILE_ANSI); if(f!=INVALID_HANDLE){ FileWriteString(f,"done"); FileClose(f); }
  TerminalClose(0);
}
