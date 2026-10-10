//+------------------------------------------------------------------+
//| DiffDump4.mq4 -- MT4 side of the MT4-vs-MT5 differential test     |
//| Offline history from make_hst.py: EURUSD = all bars (F),          |
//| GBPUSD = the same EURUSD bars minus the last 150 (C).             |
//| Per line of Files\diff_jobs.txt (id|indicator path) writes         |
//| Files\d4\<id>.<F|C>.bin in DiffDump5's layout + a JSON line.       |
//+------------------------------------------------------------------+
#property strict
#define MAXBUF 16
void Out(string s){ int h=FileOpen("diff_results.jsonl",FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI); if(h==INVALID_HANDLE) return; FileSeek(h,0,SEEK_END); FileWriteString(h,s+"\n"); FileClose(h); }
void DumpObj(string file){
  int n=ObjectsTotal(); if(n<=0) return;
  int fh=FileOpen(file,FILE_WRITE|FILE_TXT|FILE_ANSI); if(fh==INVALID_HANDLE) return;
  for(int i=0;i<n;i++){ string nm=ObjectName(i);
    FileWriteString(fh,StringFormat("%d|%d|%.5f|%d|%.5f|%d|%d|%s\n",ObjectType(nm),(int)ObjectGet(nm,OBJPROP_TIME1),ObjectGet(nm,OBJPROP_PRICE1),
      (int)ObjectGet(nm,OBJPROP_TIME2),ObjectGet(nm,OBJPROP_PRICE2),(int)ObjectGet(nm,OBJPROP_XDISTANCE),(int)ObjectGet(nm,OBJPROP_YDISTANCE),ObjectDescription(nm))); }
  FileClose(fh);
}
string RunOne(string id,string name,string sym,string tag){
  if(tag=="F") ObjectsDeleteAll();
  uint t0=GetTickCount(); int nb=iBars(sym,PERIOD_D1); double m[]; ArrayResize(m,nb*MAXBUF);
  ResetLastError(); iCustom(sym,PERIOD_D1,name,0,0); int err=GetLastError();
  int nbuf=0;
  for(int b=0;b<MAXBUF;b++){
    bool any=false;
    for(int i=0;i<nb;i++){ double v=iCustom(sym,PERIOD_D1,name,b,i); m[(nb-1-i)*MAXBUF+b]=v; if(v!=0.0 && v!=EMPTY_VALUE) any=true; }
    if(any) nbuf=b+1;
  }
  int fh=FileOpen("d4\\"+id+"."+tag+".bin",FILE_WRITE|FILE_BIN); if(fh!=INVALID_HANDLE){ FileWriteInteger(fh,nb); FileWriteInteger(fh,nbuf); FileWriteArray(fh,m); FileClose(fh); }
  string r="\""+tag+"\":{\"status\":\""+(err==4072||err==4802?"load_failed":"ok")+"\",\"err\":"+IntegerToString(err)+",\"nbuf\":"+IntegerToString(nbuf)+",\"objects\":"+IntegerToString(ObjectsTotal())+",\"ms\":"+IntegerToString(GetTickCount()-t0)+"}";
  if(tag=="F") DumpObj("d4\\"+id+".F.obj");
  ObjectsDeleteAll();
  return(r);
}
void OnStart(){
  Out("{\"session\":\"start\",\"F\":"+IntegerToString(iBars("EURUSD",PERIOD_D1))+",\"C\":"+IntegerToString(iBars("GBPUSD",PERIOD_D1))+",\"digits\":"+IntegerToString((int)MarketInfo("GBPUSD",MODE_DIGITS))+"}");
  int jf=FileOpen("diff_jobs.txt",FILE_READ|FILE_TXT|FILE_ANSI); if(jf==INVALID_HANDLE) return;
  while(!FileIsEnding(jf) && !IsStopped()){
    string p[]; if(StringSplit(FileReadString(jf),'|',p)<2) continue;
    Out("{\"id\":\""+p[0]+"\",\"phase\":\"begin\"}");
    string res="{\"id\":\""+p[0]+"\","+RunOne(p[0],p[1],"EURUSD","F");
    res+=","+RunOne(p[0],p[1],"GBPUSD","C");
    Out(res+"}");
  }
  FileClose(jf);
  int f=FileOpen("diff_done.flag",FILE_WRITE|FILE_TXT|FILE_ANSI); if(f!=INVALID_HANDLE){ FileWriteString(f,"done"); FileClose(f); }
  TerminalClose(0);
}
