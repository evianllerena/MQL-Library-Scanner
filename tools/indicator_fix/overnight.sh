#!/bin/bash
# Overnight run (uses Claude usage for AI repair; stops cleanly at the usage limit and can be re-run).
#   1. rebuild the repair queue from all test results (skips what already works)
#   2. AI repair, 4 at a time, each fix verified by compile + MT5 runtime + look-ahead test
#   3. repackage F:\Fixed MQL5 Indicators (Working / Repaints / Not working / ... + REPORT.csv)
# Re-running continues where the last run stopped (finished jobs are skipped).
cd "$(dirname "$0")"
L=/f/MQLFIX_BUILD/overnight.log
echo "$(date '+%m-%d %H:%M') start" >> $L
python build_jobs.py >> $L 2>&1
mkdir -p /f/MQLFIX_BUILD/R2
python repair_ai.py jobs_runtime.json --workers 4 --out 'F:\MQLFIX_BUILD\R2\results.jsonl' >> /f/MQLFIX_BUILD/R2/progress.txt 2>&1
echo "$(date '+%m-%d %H:%M') repair pass ended: $(grep -c '"passes": true' /f/MQLFIX_BUILD/R2/results.jsonl) passing so far" >> $L
for d in Working Repaints "Not working" "Not compiling" Excluded "Working - alignment untestable"; do rm -rf "/f/Fixed MQL5 Indicators/$d"; done
[ -d "/f/Fixed MQL5 Indicators/_build" ] && mv "/f/Fixed MQL5 Indicators/_build" /f/MQLFIX_BUILD/_build_raw_conversions
python package.py >> $L 2>&1
echo "$(date '+%m-%d %H:%M') packaged" >> $L
