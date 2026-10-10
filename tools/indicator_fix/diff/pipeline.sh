#!/bin/bash
# Unattended pipeline after the MT4 reference run (resumable; every step skips finished work):
#   1. MT5 main pass (F + C) over working tree V on rt1-4      (run_mt5.sh, if not finished)
#   2. compare -> verdicts.json
#   3. bar-by-bar (I) pass for indicators whose F + C already match, then compare again
#   4. AI repair loop on rt1-5 (max 3 attempts per indicator); on the Claude usage limit: sleep 30 min, retry
#   5. package -> F:\Fixed MQL5 Indicators
cd /c/Users/Evision/MQL-Library-Scanner/tools/indicator_fix/diff
L=/f/MQLFIX_BUILD/DIFF/pipeline.log
log() { echo "$(date '+%m-%d %H:%M') $*" >> $L; }
log "pipeline start"
missing() { py -c "
import json,glob
m=json.load(open(r'F:\MQLFIX_BUILD\DIFF\ex5map_v.json',encoding='utf-8')); done=set()
for f in glob.glob(r'F:\MQLFIX_BUILD\DIFF/r5_v*.jsonl'):
    for l in open(f,encoding='utf-8',errors='replace'):
        try: done.add(json.loads(l)['id'])
        except Exception: pass
print(len(set(m)-done))"; }
for round in 1 2 3 4 5 6; do                     # the MT5 pass must be complete before anything is compared
  bash /f/MQLFIX_BUILD/DIFF/run_mt5.sh
  m=$(missing); log "MT5 pass round $round: $m indicators without a result"
  [ "$m" = "0" ] && break
done
[ "$(missing)" = "0" ] || { log "MT5 pass incomplete - stopping"; exit 1; }
py compare.py >> $L 2>&1; log "compare 1 done"
py -c "
import json; v=json.load(open(r'F:\MQLFIX_BUILD\DIFF\verdicts.json'))
open(r'F:\MQLFIX_BUILD\DIFF\i_names.txt','w',encoding='utf-8').write('\n'.join(a for a,r in v.items() if r['verdict']=='fc_ok_needs_I'))"
for k in 0 1 2 3; do py diff_driver.py 5 --rt F:/MQLFIX_RT/rt$((k+1)) --shard $k/4 --mode FCI --names F:/MQLFIX_BUILD/DIFF/i_names.txt --ex5map F:/MQLFIX_BUILD/DIFF/ex5map_v.json --tag i$k >> $L 2>&1 & done
wait
py compare.py >> $L 2>&1; log "compare 2 done"
powershell -NoProfile -c "Get-CimInstance Win32_Process | ? { \$_.CommandLine -match 'repair_diff.py' } | % { Stop-Process -Id \$_.ProcessId -Force }" >/dev/null 2>&1; sleep 5
while true; do
  out=$(cd /f/MQLFIX_BUILD/RD && py /c/Users/Evision/MQL-Library-Scanner/tools/indicator_fix/diff/repair_diff.py --rts 1,2,3,4,5 --workers 5 2>&1 | tee -a /f/MQLFIX_BUILD/RD/repair.log)
  jobs=$(echo "$out" | head -1)
  log "repair pass: $jobs; passes so far: $(grep -c '"passes": true' /f/MQLFIX_BUILD/RD/results.jsonl)"
  if echo "$out" | grep -q "USAGE LIMIT"; then log "usage limit - sleeping 30 min"; sleep 1800; continue; fi
  if [ "$jobs" = "0 jobs" ]; then break; fi
done
log "repairs finished"
py package_diff.py >> $L 2>&1; log "packaged"
echo "PIPELINE DONE" >> $L
