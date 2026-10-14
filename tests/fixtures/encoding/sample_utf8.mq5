//+------------------------------------------------------------------+
//| Encoding fixture. Комментарий / 注释 (non-ASCII comment on purpose) |
//+------------------------------------------------------------------+
#property copyright "fixture"
#property link      "https://example.com"
#property indicator_separate_window
#property indicator_buffers 1
#property indicator_plots   1
#property indicator_type1   DRAW_LINE
input int InpPeriod=14; // Период
double Buf[];
int OnInit(){SetIndexBuffer(0,Buf,INDICATOR_DATA);return(INIT_SUCCEEDED);}
int OnCalculate(const int rates_total,const int prev_calculated,const int begin,const double &price[])
{for(int i=prev_calculated;i<rates_total;i++)Buf[i]=price[i];return(rates_total);}
