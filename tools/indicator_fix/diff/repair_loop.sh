#!/bin/bash
# repair loop + packaging only (the tail of pipeline.sh), on rt1-4
cd /c/Users/Evision/MQL-Library-Scanner/tools/indicator_fix/diff
L=/f/MQLFIX_BUILD/DIFF/pipeline.log
log() { echo "$(date '+%m-%d %H:%M') $*" >> $L; }
while true; do
  out=$(cd /f/MQLFIX_BUILD/RD && py /c/Users/Evision/MQL-Library-Scanner/tools/indicator_fix/diff/repair_diff.py --rts 1,2,3,4 --workers 4 2>&1 | tee -a /f/MQLFIX_BUILD/RD/repair.log)
  jobs=$(echo "$out" | head -1)
  log "repair pass: $jobs; passes so far: $(grep -c '"passes": true' /f/MQLFIX_BUILD/RD/results.jsonl)"
  if echo "$out" | grep -q "USAGE LIMIT"; then log "usage limit - sleeping 30 min"; sleep 1800; continue; fi
  if [ "$jobs" = "0 jobs" ]; then break; fi
done
log "repairs finished"
py package_diff.py >> $L 2>&1; log "packaged"
echo "PIPELINE DONE" >> $L
