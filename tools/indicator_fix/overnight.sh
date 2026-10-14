#!/bin/bash
# Overnight run (uses Claude usage for AI repair; stops cleanly at the usage limit and can be re-run).
#   1. rebuild the repair queue from all test results (skips what already works)
#   2. AI repair, 4 at a time, each fix verified by compile + MT5 runtime + look-ahead test
#   3. repackage F:\Fixed MQL5 Indicators (Working / Repaints / Not working / ... + REPORT.csv)
# Re-running continues where the last run stopped (finished jobs are skipped).
# Paths: MQLFIX_BUILD / MQLFIX_FIXED (Windows paths, same variables as fix_paths.py) override the defaults.
cd "$(dirname "$0")"
B=$(cygpath -u "${MQLFIX_BUILD:-F:\\MQLFIX_BUILD}")
FX=$(cygpath -u "${MQLFIX_FIXED:-F:\\Fixed MQL5 Indicators}")
L="$B/overnight.log"
echo "$(date '+%m-%d %H:%M') start" >> "$L"
python build_jobs.py >> "$L" 2>&1
mkdir -p "$B/R2"
python repair_ai.py jobs_runtime.json --workers 4 --out "$(cygpath -w "$B/R2/results.jsonl")" >> "$B/R2/progress.txt" 2>&1
echo "$(date '+%m-%d %H:%M') repair pass ended: $(grep -c '"passes": true' "$B/R2/results.jsonl") passing so far" >> "$L"
for d in Working Repaints "Not working" "Not compiling" Excluded "Working - alignment untestable"; do rm -rf "${FX:?}/$d"; done
[ -d "$FX/_build" ] && mv "$FX/_build" "$B/_build_raw_conversions"
python package.py >> "$L" 2>&1
echo "$(date '+%m-%d %H:%M') packaged" >> "$L"
