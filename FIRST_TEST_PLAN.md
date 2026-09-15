# First installed Beta 0.2 test

## Test A — Installation and first launch
- Install the generated NSIS Setup.exe.
- Launch MQL Indicator Library from the Start menu.
- Confirm the application opens without a console window.
- Confirm the Library, Scan, Review and Settings views load.

## Test B — 100-file validation set
- Put the 50 MQ4 + 50 MQ5 validation files in one or two test folders.
- Open Scan → Add Folder.
- Select the test folder(s).
- Run Scan Library.
- Confirm 100 files are discovered and the UI remains responsive.
- Confirm Library shows MQ4/MQ5 counts and classifications.

## Test C — Persistence and incremental scan
- Close the app and reopen it.
- Confirm the 100 indicators are still present without rescanning.
- Scan the same folders again.
- Expected: unchanged files are skipped.

## Test D — Search quality
Try searches such as:
- MACD
- RSI
- ATR
- Bollinger
- Fractal
- arrow
- histogram

Open several results and compare the displayed classification with what the indicator actually does.

## Test E — Review queue
- Open Review.
- Record any indicator that is obviously misclassified even if confidence is high.
- Record low-confidence indicators that are actually easy to identify.

## Test F — Scale test
After the 100-file test is stable:
1. 500–1,000 indicators
2. ~5,000 indicators
3. Full 20,000+ collection

Do not move or delete original source files during beta testing.
