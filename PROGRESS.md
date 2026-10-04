# NNFX Backtester — Plan of Record & Status

Single source of truth for what's built, what's decided, and what's open. The agent updates
this file with every fix; the reviewer verifies the repo against it. If the repo and this file
disagree, that's a deviation to resolve.

## How we work (so nothing gets lost)
1. Reviewer writes a short SPEC (rule + acceptance criteria + required tests).
2. Agent implements on `nnfx-rulebook-sync`: engine + Part C tests + MQL5 mirror (compile 0/0)
   + rescore leaderboard. One commit per fix. Updates this file.
3. Every push auto-runs `verify.yml` (Part C self-test + golden cases). Green = logic intact.
4. Reviewer pulls the repo, re-runs the self-test, reads the diff + leaderboard, checks rules.
5. Reviewed + CI green => merge to main.
Conventions: additive only; `nnfx_engine.py` is the source of truth; MQL5 harness mirrored;
`zero_reference` never assumed 0; no scanner/preview/Rust/UI changes.

## Fix status (8-fix plan)
| # | Fix | Status | Commit |
|---|-----|--------|--------|
| 1 | X4 wrong-side-baseline exit | DONE (main) | 54b0d26 |
| 2 | X2 exit indicator vs own zero_reference | DONE (main) | 5b1ad42 |
| 3 | Volume filter (G9) + same-line average (G10) | DONE (main) | cdcddb9 |
| 4 | Entries E1 / E3 / E4 + bridge before_cross | DONE (main) | b1c82ba / ad71d0a |
| 5 | Continuation E6 (vp_c2 default + lesson11 + legacy) | DONE (main) | dff1117 |
| 6 | Trailing T4 activates at 2xATR beyond entry | DONE (main) | 019a07f |
| 7 | News hook N1/X5 (dormant) + M2 risk 2% | DONE (main) | 75593eb |
| 7b | Drawdown R1: max_drawdown_pct + 10% breaker | DONE (main) | 9451171 |
| 8 | Doc sync + verification infra + harness align + final PR | IN PROGRESS | — |

## Confirmed rules (reviewer-verified at code level)
- Entries: E1 c1_trigger (fresh C1 cross, on-side, within 1xATR, C2+vol agree) · E2 standard
  (baseline cross) · E3 pullback (valid-but->1xATR remembered, enters on pull-back) ·
  E4 one_candle (exactly one of C1/C2 lags, one-candle grace, still within 1xATR) ·
  E5 bridge-too-far (default `before_cross`, 7 bars, TWO-LINE C1 ONLY). [Decision #5 = before_cross, USER-CONFIRMED]
- Continuation E6 default `vp_c2`: C2 flips back to the trade direction AND C1 currently on-side;
  ignores volume + 1xATR; sequence must be unbroken since entry. [USER-CONFIRMED]. Also `lesson11`
  (exit indicator as the signal) and `c1_signal` (legacy) as settings.
- Exits: X1 (SL 1.5xATR, TP1 1xATR on half, breakeven, then trail) · X2 (exit indicator vs own
  zero_reference) · X3 (C1 flip) · X4 (wrong-side baseline close) · X5 (news; close if losing OR
  profit < 1xATR). [X5 cutoff USER-CONFIRMED]
- Trailing T4: activates once a close is >= 2xATR beyond entry (ATR fixed at entry), then trails
  1.5xATR behind each close, per candle, never backward. [USER-CONFIRMED]
- Volume: reference indicator, reading >= its own 20-bar average (same line).
- News N1/X5: ForexFactory HIGH impact, either currency, 24h window. WIRED-BUT-DORMANT.
- Money: ATR(14), risk 2% per trade, SL 1.5xATR, two equal halves.
- Drawdown R1: max_drawdown_pct on a chronological, cross-symbol equity curve; 10%-from-peak
  circuit breaker (resume when back under threshold). RANK with breaker OFF; show both
  breaker-off and breaker-on expectancy + max_drawdown_pct as a risk column. [USER-CONFIRMED]

## Open / STUB-TO-CONFIRM (working defaults in place; confirm against VP's videos)
- E4 scope: implemented C1-OR-C2 symmetric. Source unclear; revisit if VP restricts it.
- dd_resume_rule: `below_threshold` (resume when drawdown back under 10%). Confirm.
(X5 cutoff and Decision #5 are now confirmed above.)

## Out of scope (recorded so they're not mistaken for missing work)
Multi-timeframe (we are Daily only), rollover windows, M6/M7 shared exposure, D1/D2 dead-market
filter, three-EA architecture, broker lot rounding, hedging/netting. EA-only; do not build.

## Known divergences (see BACKLOG.md)
- MQL5 harness can re-enter on the same bar it exits; Python never does. Align in Fix 8.
- MQL5 harness breaker/`max_drawdown_pct` is per-symbol; Python is one account across symbols.

## Biggest open workstream (NOT a code fix)
Data history: MT5 serves ~455 daily bars/symbol => 5-40 trades per candidate => rankings are
NOT yet trustworthy. Needs "Max bars in chart" raised and/or more history downloaded (target the
2019+ window). This is the real blocker to a usable scoreboard; schedule after Fix 8.
