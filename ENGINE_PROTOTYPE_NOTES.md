# MQL Indicator Library — Engine Prototype 0.1

This is the first working classification-engine prototype for the future Windows desktop application.

Current prototype capabilities:
- Recursive MQ4/MQ5 discovery
- SHA-256 exact duplicate detection
- MQ4 and MQ5 plot/buffer extraction
- Converted-MQ4-to-MQ5 compatibility-helper exclusion
- Standard indicator dependency detection
- iCustom dependency detection
- Functional category scoring
- Visual category scoring
- Main-chart / separate-window detection
- SQLite persistence (WAL mode)
- CSV validation report
- Review warnings and confidence scoring

This prototype is deliberately engine-first. The production application is planned as a Tauri/Rust Windows desktop application with this data model and behavior ported into the Rust backend for speed and a one-time installer/updater experience.

## Run the sample validation

```bash
python engine.py samples/mq4 samples/mq5 --db indicator_library.sqlite3 --csv classification_report.csv --summary summary.json
```
