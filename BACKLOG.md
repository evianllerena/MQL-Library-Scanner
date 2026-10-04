# Backlog

## Known divergences

- **Same-bar re-entry (MQL5 harness only).** `NNFXHarness.mq5` can open a new trade on the
  SAME bar it exits: its exit branches (X3 C1-flip, X2 exit indicator, X4 baseline) close the
  position and then fall through to the entry logic. `nnfx_engine.py` returns on every exit bar,
  so it never does. The harness is reference-only, so this does not affect the leaderboard.
  Align during Fix 8.
