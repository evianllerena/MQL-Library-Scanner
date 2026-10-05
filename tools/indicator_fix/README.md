# Indicator fix pipeline (converted MQL4 -> MQL5 library)

Fixes the user's converted indicator library so every indicator computes on the right bars, then
runtime-tests each one. Inputs are never modified; fixed copies go to a new folder.

## Why
The MQL4->MQL5 conversions in `F:\Converted Indicators` / `F:\Ready MQL5 Indicators` (MQL_ONE 5.9.9)
keep MT4 code that indexes buffers newest-first (`buf[i]`, i = bars back) but never call
`ArraySetAsSeries(buf, true)`. MT5 buffers default to oldest-first, so values land on mirror-image
dates. Proven on Kijun-seny: its output equals the true Kijun mirrored across the series (0 error).
MQL_ONE 5.9.19 added this flip (convert.py FIX 1), but the existing outputs predate it.

## Steps
1. `inventory.py` - every unique original MT4 source (13,912) across both folders (content hash).
2. `map_validated.py` - maps each original to an existing MQL_ONE-repaired, compiling conversion
   (MQL_ONE/OUTPUT/VALIDATED via evidence.json, or a Ready top-level conversion): 11,430 covered.
3. `build_variants.py` - variant A = the conversion as-is; variant B = A plus `ArraySetAsSeries(buf,true)`
   for each unflipped `SetIndexBuffer` array at the top of OnCalculate. Work dir is space-free
   (`F:\MQLFIX_BUILD`) because MetaEditor's CLI cuts paths at the first space.
4. `compile_variant.py A|B` - MetaEditor folder compiles in chunks of 500.
5. `qa_driver.py A|B` + `IndicatorQA.mq5` - runtime QA in the batch's MT5 runtime clone, per indicator:
   loads + calculates every bar on EURUSD and GBPJPY D1 (health-checked queue, 10 s limit); produces
   real (non-empty, varying) values; and a no-look-ahead/alignment test: values on the shared bars of
   two custom symbols (QA_CUT = EURUSD history ending 150 bars early, QA_FULL = full history) must match.
   0 differences = aligned; sparse differences = repaints (e.g. ZigZag); most bars differ = misaligned.
   Known-answer check: Kijun-seny/Moving Averages A = misaligned, B = aligned; RSI both aligned.
6. Selection: per indicator keep the variant that is aligned (A preferred when both are).
   Indicators with no working variant, plus the 2,482 originals MQL_ONE never repaired, go to repair.
