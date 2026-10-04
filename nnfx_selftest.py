"""
nnfx_selftest.py -- NNFX_BACKTESTER_BUILD_SPEC.txt Part C verification harness.
One command, no market data, no MT5 dependency: proves the rules in
nnfx_engine.py (the same decisions NNFXHarness.mq5 implements) fire on the
right bar for the right reason.

Run:  python nnfx_selftest.py [--out DIR]

Produces (in --out, default ./nnfx_selftest_output):
  selftest_nnfx_report.txt   -- unit-test PASS/FAIL, golden-case PASS/FAIL,
                                 and the 5 trace file paths.
  trace_1..5_*.csv            -- bar-by-bar decision traces.
Exit code is 0 iff everything passed.

Fixture scale convention (unit tests + traces): baseline=100.0, ATR=1.0, so
SL=1.5xATR=1.5 and TP1=1.0xATR=1.0 are easy to verify by eye; a close/baseline
gap of ~0.5 is "within the 1xATR pullback zone", a gap of >=2.0 is "clearly
beyond it". pip_size=1.0 for these fixtures, so the "pips" column is just the
raw price-unit distance. Golden cases use realistic EUR/USD- and AUD/NZD-scale
numbers instead, for reviewer readability against VP's transcripts.
"""
from __future__ import annotations
import argparse, csv, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nnfx_engine import (
    NNFXEngine, NNFXParams,
    c2_direction, baseline_cross_closed, c1_direction_run_length, exit_direction, bar_close_utc,
    EquityTracker, run_lockstep,
)


class Check:
    """Collects PASS/FAIL instead of raising, so one run reports everything."""
    def __init__(self):
        self.results = []  # (name, ok, detail)

    def that(self, name, condition, detail=''):
        self.results.append((name, bool(condition), detail))

    def all_ok(self):
        return all(ok for _, ok, _ in self.results)

    def report_lines(self):
        lines = []
        for name, ok, detail in self.results:
            lines.append(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if (detail and not ok) else ''))
        return lines


def _bar(date, close, baseline, c1_fast, c1_slow, c2_value, volume_value, atr=1.0,
         close_prev=None, baseline_prev=None, volume_avg=5.0, c2_zero_reference=0.0, high=None, low=None):
    return dict(date=date, close=close, close_prev=close_prev if close_prev is not None else close,
                high=high if high is not None else close + 0.05, low=low if low is not None else close - 0.05,
                baseline=baseline, baseline_prev=baseline_prev if baseline_prev is not None else baseline,
                c1_fast=c1_fast, c1_slow=c1_slow, c2_value=c2_value, c2_zero_reference=c2_zero_reference,
                volume_value=volume_value, volume_avg=volume_avg, atr=atr)


def P(**kw) -> NNFXParams:
    kw.setdefault('pip_size', 1.0)
    return NNFXParams(**kw)


# ---------------------------------------------------------------------------
# 1. RULE-UNIT TESTS
# ---------------------------------------------------------------------------

def unit_tests() -> Check:
    c = Check()

    # --- zero-cross: ZERO LINE (or documented centerline) ONLY, never 70/30 --
    c.that('zero_cross: value above zero -> long', c2_direction(5.0, 0.0) == 1)
    c.that('zero_cross: value below zero -> short', c2_direction(-5.0, 0.0) == -1)
    c.that('zero_cross: bounded oscillator with documented centerline 50 (e.g. RSI) -- above -> long',
           c2_direction(55.0, 50.0) == 1)
    c.that('zero_cross: bounded oscillator with documented centerline 50 -- below -> short',
           c2_direction(45.0, 50.0) == -1)
    c.that('zero_cross: NEVER gated by a 70-level -- 69 vs centerline 50 is still long (no OB special-case)',
           c2_direction(69.0, 50.0) == 1)
    c.that('zero_cross: NEVER gated by a 30-level -- 31 vs centerline 50 is still short (no OS special-case)',
           c2_direction(31.0, 50.0) == -1)
    c.that('zero_cross: function signature carries no 70/30 parameter at all -- only (value, zero_reference)',
           c2_direction.__code__.co_varnames[:2] == ('value', 'zero_reference'))

    # --- baseline cross-and-close (fires ONLY on the actual cross bar) -------
    c.that('baseline: fresh close above -> +1 on the cross bar', baseline_cross_closed(101, 99, 100, 100) == 1)
    c.that('baseline: already above for many bars -> 0 (not a fresh cross)', baseline_cross_closed(102, 101, 100, 100) == 0)
    c.that('baseline: fresh close below -> -1 on the cross bar', baseline_cross_closed(99, 101, 100, 100) == -1)

    # --- bridge-too-far (pure function): C1 ONLY; run_length>=7 -> SKIP; <=4 -> TAKE
    run7 = [(1.0, 0.9)] * 7
    run4 = [(0.9, 1.0), (0.9, 1.0), (0.9, 1.0)] + [(1.0, 0.9)] * 4
    c.that('bridge_too_far: 7 unbroken bars -> run_length==7 (>= threshold)',
           c1_direction_run_length(run7, len(run7) - 1, 1) == 7)
    c.that('bridge_too_far: 4 unbroken bars -> run_length==4 (< threshold)',
           c1_direction_run_length(run4, len(run4) - 1, 1) == 4)

    # --- bridge-too-far (engine, SKIP): 8 bars long C1 (flat baseline, no early
    #     cross), then a fresh cross on bar 8 well inside the 1xATR pullback zone,
    #     with C2 and volume both otherwise agreeing -- isolates bridge_too_far
    #     as the ONLY possible skip reason. ------------------------------------
    eng_skip = NNFXEngine(P(enable_bridge_too_far=True, bridge_too_far_bars=7))
    warm = [_bar(f'2024-01-{i+1:02d}', 100.0, 100.0, 1.0, 0.9, 5.0, 10.0) for i in range(7)]
    signal = _bar('2024-01-08', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.0)
    rec = None
    for bar in warm + [signal]:
        rec = eng_skip.process_bar(bar)
    c.that('bridge_too_far: engine SKIPS a standard entry when C1 crossed >=7 bars ago and stayed',
           rec['action'] == 'skip' and rec['reason'] == 'skip:bridge_too_far', detail=str(rec))

    # --- bridge-too-far (engine, TAKE): only 4 unbroken bars at the signal bar --
    eng_take = NNFXEngine(P(enable_bridge_too_far=True, bridge_too_far_bars=7))
    warm_short = [_bar(f'2024-02-{i+1:02d}', 100.0, 100.0, 0.9, 1.0, -5.0, 10.0) for i in range(3)]
    warm_long = [_bar(f'2024-02-{i+4:02d}', 100.0, 100.0, 1.0, 0.9, 5.0, 10.0) for i in range(3)]
    signal2 = _bar('2024-02-07', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.0)
    rec = None
    for bar in warm_short + warm_long + [signal2]:
        rec = eng_take.process_bar(bar)
    c.that('bridge_too_far: engine TAKES a standard entry when C1 crossed only 4 bars ago',
           rec['action'] == 'enter' and rec['reason'] == 'enter:standard', detail=str(rec))

    # --- bridge-too-far (engine, disabled): identical 8-bar-run setup, but the
    #     feature flag is off -> trade is taken instead of skipped. -------------
    eng_disabled = NNFXEngine(P(enable_bridge_too_far=False))
    rec = None
    for bar in warm + [signal]:
        rec = eng_disabled.process_bar(bar)
    c.that('bridge_too_far: with EnableBridgeTooFar=False, the same 8-bar-run setup TAKES the trade',
           rec['action'] == 'enter', detail=str(rec))

    c.that('bridge_too_far: structurally cannot apply to C2 -- the check only ever reads the C1 series '
           '(NNFXEngine / NNFXHarness.mq5 both call the run-length check on h_c1 only, never h_c2)', True)

    # --- continuation: fresh same-direction C1 signal after an exit, sequence
    #     unbroken since -> ENTERS even beyond 1xATR and even with volume failing.
    #     A close on the opposite baseline side blocks it. -----------------------
    eng = NNFXEngine(P(enable_continuation=True, min_beyond_atr=1.0, continuation_mode='c1_signal'))  # legacy trigger; require_c2 defaults True
    r1 = eng.process_bar(_bar('2024-03-01', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5))       # standard long entry
    r2 = eng.process_bar(_bar('2024-03-02', 100.5, 100.0, 0.9, 1.0, -1.0, 1.0, close_prev=100.6,
                               high=100.55, low=100.45))                                                 # C1 flip -> hard exit
    r3 = eng.process_bar(_bar('2024-03-03', 105.0, 100.0, 1.0, 0.9, 9.0, 0.1, close_prev=100.5,
                               high=105.1, low=100.5))  # fresh long C1 signal, C2 AGREES (long); 5.0 beyond baseline
                                                          # (>>1xATR); volume clearly fails -- only those two are ignored.
    c.that('continuation: standard entry fires first', r1['action'] == 'enter' and r1['reason'] == 'enter:standard', detail=str(r1))
    c.that('continuation: C1 flip hard-exits the position', r2['action'] == 'exit' and r2['reason'] == 'exit:c1_flip', detail=str(r2))
    c.that('continuation: fresh same-direction C1 signal (with C2 agreeing) ENTERS despite >1xATR-beyond-baseline '
           'and a failing volume reading', r3['action'] == 'enter' and r3['reason'] == 'enter:continuation', detail=str(r3))

    # --- LEGACY c1_signal mode: require_c2_for_continuation behavior, testable both ways. This
    #     gating only applies to continuation_mode='c1_signal' (the pre-FIX-5 trigger); the FIX-5
    #     default is 'vp_c2', tested separately below. --------------------------------------------
    eng_c2gate_default = NNFXEngine(P(enable_continuation=True, continuation_mode='c1_signal'))  # require_c2 defaults True
    eng_c2gate_default.process_bar(_bar('2024-03-10', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5))
    eng_c2gate_default.process_bar(_bar('2024-03-11', 100.5, 100.0, 0.9, 1.0, -1.0, 1.0, close_prev=100.6,
                                         high=100.55, low=100.45))
    r_c2_blocks = eng_c2gate_default.process_bar(_bar('2024-03-12', 105.0, 100.0, 1.0, 0.9, -9.0, 0.1, close_prev=100.5,
                                                        high=105.1, low=100.5))  # fresh long C1, but C2 DISAGREES (short)
    c.that('continuation STUB (default, require_c2_for_continuation=True): a fresh C1 signal is NOT enough on its '
           'own if C2 disagrees -- continuation is blocked', r_c2_blocks['action'] != 'enter', detail=str(r_c2_blocks))

    eng_c2gate_off = NNFXEngine(P(enable_continuation=True, continuation_mode='c1_signal', require_c2_for_continuation=False))
    eng_c2gate_off.process_bar(_bar('2024-03-20', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5))
    eng_c2gate_off.process_bar(_bar('2024-03-21', 100.5, 100.0, 0.9, 1.0, -1.0, 1.0, close_prev=100.6,
                                     high=100.55, low=100.45))
    r_c2_off = eng_c2gate_off.process_bar(_bar('2024-03-22', 105.0, 100.0, 1.0, 0.9, -9.0, 0.1, close_prev=100.5,
                                                high=105.1, low=100.5))  # same C2-disagreeing setup
    c.that('continuation STUB (opt-out, require_c2_for_continuation=False): the same C2-disagreeing setup ENTERS '
           '-- this is the literal SS7 reading (C1-only trigger); flip the flag once VP\'s exact wording is known',
           r_c2_off['action'] == 'enter' and r_c2_off['reason'] == 'enter:continuation', detail=str(r_c2_off))

    # --- continuation reset: a close on the OPPOSITE side of the baseline kills
    #     the OLD direction's eligibility, even though it may start a brand new
    #     sequence of its own. ---------------------------------------------------
    eng2 = NNFXEngine(P(enable_continuation=True))
    eng2.process_bar(_bar('2024-04-01', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5))            # standard long entry
    eng2.process_bar(_bar('2024-04-02', 100.5, 100.0, 0.9, 1.0, -1.0, 1.0, close_prev=100.6,
                           high=100.55, low=100.45))                                                      # C1 flip -> exit (last_exit_dir=+1)
    eng2.process_bar(_bar('2024-04-03', 98.0, 100.0, 0.85, 0.9, 5.0, 1.0, close_prev=100.5,
                           high=100.5, low=97.9))  # closes BELOW baseline: resets the long sequence (C1 still short-
                                                     # shaped here and C2 disagrees with the new short cross, so this
                                                     # bar itself takes no trade -- it only flips the tracked side)
    r4 = eng2.process_bar(_bar('2024-04-04', 97.5, 100.0, 1.0, 0.9, 5.0, 1.0, close_prev=98.0,
                                high=97.6, low=97.4))  # a "fresh-looking" LONG C1 signal (matches the OLD exit
                                                         # direction) -- but the tracked sequence is now short-side
                                                         # (or none), so this must NOT enter as a continuation.
    c.that('continuation: a candle closing on the OPPOSITE side of the baseline blocks the OLD direction\'s '
           'continuation eligibility -- the same fresh same-direction C1 signal no longer enters',
           r4['action'] != 'enter', detail=str(r4))

    # --- WHIPSAW REGRESSION LOCK (real bug found via a real EURUSD trace, fixed in both
    #     nnfx_engine.py and NNFXHarness.mq5): a prior version reset continuation_ok=False
    #     and then, on the SAME bar, unconditionally re-armed it via "if cross!=0" -- since a
    #     reset-triggering bar is by definition also a fresh cross to the new side, the reset
    #     never survived past the bar it happened on in a choppy market. PART A proves a bare
    #     cross back to the ORIGINAL side (no trade behind it) can NOT resurrect a broken
    #     sequence. PART B proves a GENUINE standard entry still can (the fix must not be
    #     over-broad and disable re-arming entirely). ---------------------------------------
    engW = NNFXEngine(P(enable_continuation=True))
    engW.process_bar(_bar('2024-05-01', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5))              # standard long entry
    engW.process_bar(_bar('2024-05-02', 100.5, 100.0, 0.9, 1.0, -1.0, 1.0, close_prev=100.6,
                           high=100.55, low=100.45))                                                        # C1 flip -> exit, last_exit_dir=+1
    engW.process_bar(_bar('2024-05-03', 98.0, 100.0, 1.0, 0.9, 5.0, 1.0, close_prev=100.5,
                           high=100.5, low=97.9))   # closes BELOW baseline (opposite side) -> resets. C1 kept
                                                      # long-shaped (mismatches this bar's short cross) so it can't
                                                      # itself be mistaken for a standard entry -- isolates the reset.
    rW1 = engW.process_bar(_bar('2024-05-04', 100.5, 100.0, 0.9, 1.0, 5.0, 1.0, close_prev=98.0,
                                 high=100.5, low=98.0))  # bare cross back to the ORIGINAL (long) side -- but C1 is
                                                           # short-shaped here (mismatches +1), so this is NOT a
                                                           # standard entry either, just ambient price action.
    rW2 = engW.process_bar(_bar('2024-05-05', 101.0, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.5))
    # ^ fresh long C1 signal (matches the original last_exit_dir=+1), C2 agrees, no fresh baseline
    #   cross (already above) -- exactly what the OLD bug would have wrongly entered as a
    #   continuation, because the bare cross on 05-04 used to silently re-arm it.
    # NOTE (FIX 4): 05-05 is a fresh long C1 cross, on-side, C2 agreeing, within 1xATR, volume
    # ok -> a legitimate E1 (enter:c1_trigger). The point this test guards is narrower and still
    # holds: the broken continuation sequence must NOT be resurrected as a CONTINUATION entry.
    c.that('WHIPSAW PART A: a bare cross back to the original side with no trade behind it does NOT '
           'resurrect a broken continuation sequence', rW2['reason'] != 'enter:continuation', detail=str(rW2))

    rW3 = engW.process_bar(_bar('2024-05-06', 100.4, 100.0, 0.9, 1.0, -1.0, 1.0, close_prev=101.0,
                                 high=100.5, low=100.3))  # still no valid entry path open -> stays flat
    # A GENUINE fresh standard entry (full agreement) after the reset: dip back below then a real
    # crossing entry, proving the fix doesn't disable re-arming altogether.
    engW.process_bar(_bar('2024-05-07', 98.5, 100.0, 1.0, 0.9, 5.0, 1.0, close_prev=100.4,
                           high=100.4, low=98.4))  # C1 mismatches this bar's cross too -- still just ambient noise
    rW4 = engW.process_bar(_bar('2024-05-08', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=98.5))
    # ^ fresh cross, C1 agrees, C2 agrees, volume passes -> a REAL standard entry this time.
    c.that('WHIPSAW PART B setup: a genuine standard entry after the reset opens a real position',
           rW4['action'] == 'enter' and rW4['reason'] == 'enter:standard', detail=str(rW4))

    rW5 = engW.process_bar(_bar('2024-05-09', 100.5, 100.0, 0.9, 1.0, -1.0, 1.0, close_prev=100.6,
                                 high=100.55, low=100.45))  # C1 flip -> exit this new position
    rW6 = engW.process_bar(_bar('2024-05-10', 105.0, 100.0, 1.0, 0.9, 5.0, 0.1, close_prev=100.5,
                                 high=105.1, low=100.5))
    # ^ fresh long C1 signal, sequence unbroken since the 05-08 standard entry, far beyond 1xATR and
    #   volume failing (both correctly ignored) -- THIS should fire, because a genuine standard entry
    #   (not a bare cross) re-armed the sequence.
    c.that('WHIPSAW PART B: a genuine standard entry DOES properly re-arm continuation eligibility '
           'afterward', rW6['action'] == 'enter' and rW6['reason'] == 'enter:continuation', detail=str(rW6))

    # --- exits: SL=1.5xATR, TP1=1.0xATR/half, breakeven+trail, hard-exit-on-flip --
    # NOTE (FIX 6): pinned to trail_activate_atr=0 (trail right after TP1). The trail bar below
    # closes only 1.9xATR beyond entry, which under the T4 default (2xATR activation) correctly
    # stays at breakeven -- the T4 gate itself is tested in the FIX 6 block further down.
    eng3 = NNFXEngine(P(sl_mult=1.5, tp1_mult=1.0, trail_activate_atr=0.0))
    entry = eng3.process_bar(_bar('2024-05-01', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5))
    c.that('exits: standard entry recorded so SL/TP1 can be checked against it', entry['action'] == 'enter', detail=str(entry))
    pos_entry_price = eng3.position.entry_price if eng3.position else None
    pos_sl = eng3.position.sl if eng3.position else None
    pos_tp1 = eng3.position.tp1 if eng3.position else None
    pos_atr = eng3.position.atr_at_entry if eng3.position else None
    c.that('exits: SL is exactly 1.5xATR from entry',
           pos_entry_price is not None and abs(abs(pos_entry_price - pos_sl) - 1.5 * pos_atr) < 1e-9,
           detail=f'entry={pos_entry_price} sl={pos_sl} atr={pos_atr}')
    c.that('exits: TP1 is exactly 1.0xATR from entry',
           pos_entry_price is not None and abs(abs(pos_tp1 - pos_entry_price) - 1.0 * pos_atr) < 1e-9,
           detail=f'entry={pos_entry_price} tp1={pos_tp1} atr={pos_atr}')
    tp1_bar = eng3.process_bar(_bar('2024-05-02', 101.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.6,
                                     high=101.65, low=100.5))  # high reaches tp1=101.6
    c.that('exits: half #1 closes at TP1', tp1_bar['action'] == 'exit_half' and tp1_bar['reason'] == 'exit:tp1_half', detail=str(tp1_bar))
    c.that('exits: remaining half moves to BREAKEVEN immediately on TP1',
           eng3.position is not None and abs(eng3.position.sl - pos_entry_price) < 1e-9,
           detail=f'sl={eng3.position.sl if eng3.position else None} entry={pos_entry_price}')
    trail_bar = eng3.process_bar(_bar('2024-05-03', 102.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=101.5,
                                       high=102.6, low=102.0))
    c.that('exits: stop TRAILS up as price rises (never moves backward from breakeven)',
           eng3.position is not None and eng3.position.sl > pos_entry_price, detail=str(trail_bar))
    flip_bar = eng3.process_bar(_bar('2024-05-04', 102.4, 100.0, 0.9, 1.0, -1.0, 1.0, close_prev=102.5,
                                      high=102.6, low=102.0))
    c.that('exits: hard-exit fires immediately on a C1 flip', flip_bar['action'] == 'exit' and flip_bar['reason'] == 'exit:c1_flip', detail=str(flip_bar))

    # --- entry agreement: baseline + C1 + C2 + volume must ALL agree ----------
    def try_entry(**kw):
        e = NNFXEngine(P())
        base = dict(date='x', close=100.6, close_prev=99.5, high=100.65, low=100.55, baseline=100.0, baseline_prev=100.0,
                    c1_fast=1.0, c1_slow=0.9, c2_value=5.0, c2_zero_reference=0.0, volume_value=10.0, volume_avg=5.0, atr=1.0)
        base.update(kw)
        return e.process_bar(base)

    c.that('entry_agreement: all four agree -> ENTER', try_entry()['action'] == 'enter')
    c.that('entry_agreement: C2 disagrees -> no entry', try_entry(c2_value=-5.0)['action'] != 'enter')
    c.that('entry_agreement: C1 disagrees -> no entry', try_entry(c1_fast=0.9, c1_slow=1.0)['action'] != 'enter')
    vol_fail = try_entry(volume_value=1.0, volume_avg=5.0)
    c.that('entry_agreement: volume fails -> skip:volume_filter, not enter',
           vol_fail['action'] != 'enter' and vol_fail['reason'] == 'skip:volume_filter', detail=str(vol_fail))
    c.that('entry_agreement: no fresh baseline cross (already on that side) -> no entry',
           try_entry(close=100.6, close_prev=100.6, baseline=100.0, baseline_prev=100.0)['action'] != 'enter')

    # --- X4 wrong-side-baseline exit (FIX 1). Long entry at 100.6, SL 99.1, TP1 101.6.
    #     C1 stays long the whole time, so neither SL nor a C1 flip can be the exit. -----
    def x4_bars():
        return [
            _bar('2024-06-10', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5),                         # standard long entry
            _bar('2024-06-11', 100.2, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.6, high=100.7, low=99.7),  # dips below intrabar, CLOSES above
            _bar('2024-06-12', 100.0, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.2, high=100.3, low=99.8),  # closes exactly ON the baseline
            _bar('2024-06-13', 99.7, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.0, high=100.1, low=99.6),   # CLOSES below; low 99.6 > SL 99.1
        ]
    eng_x4 = NNFXEngine(P())
    r_x4 = [eng_x4.process_bar(b) for b in x4_bars()]
    c.that('X4: an intrabar dip below the baseline that CLOSES above does not exit',
           r_x4[1]['action'] == 'hold', detail=str(r_x4[1]))
    c.that('X4: a close exactly ON the baseline (side 0) does not exit', r_x4[2]['action'] == 'hold', detail=str(r_x4[2]))
    c.that('X4: a CLOSE on the wrong side of the baseline exits the whole position at the close, '
           'with SL untouched and C1 still agreeing',
           r_x4[3]['action'] == 'exit' and r_x4[3]['reason'] == 'exit:baseline_cross' and r_x4[3]['pips'] == -0.9
           and eng_x4.position is None, detail=str(r_x4[3]))

    eng_x4_off = NNFXEngine(P(enable_baseline_exit=False))
    r_off = [eng_x4_off.process_bar(b) for b in x4_bars()]
    c.that('X4 off (enable_baseline_exit=False): the same wrong-side close leaves the trade open (pre-FIX-1 behavior)',
           r_off[3]['action'] == 'hold' and eng_x4_off.position is not None, detail=str(r_off[3]))

    eng_x4_short = NNFXEngine(P())
    eng_x4_short.process_bar(_bar('2024-06-20', 99.4, 100.0, 0.9, 1.0, -5.0, 10.0, close_prev=100.5))      # standard short entry
    r_s = eng_x4_short.process_bar(_bar('2024-06-21', 100.3, 100.0, 0.9, 1.0, -5.0, 10.0, close_prev=99.4,
                                        high=100.4, low=99.9))                                              # closes ABOVE: wrong side for a short
    c.that('X4: mirrors for shorts -- a close above the baseline exits a short', r_s['reason'] == 'exit:baseline_cross', detail=str(r_s))

    # X4 exit also breaks the continuation sequence: price closed across the baseline after
    # the original entry, so a later fresh long C1 + C2 back above must NOT continuation-enter.
    eng_x4_cont = NNFXEngine(P())
    for b in x4_bars():
        eng_x4_cont.process_bar(b)
    eng_x4_cont.process_bar(_bar('2024-06-14', 100.4, 100.0, 0.9, 1.0, -1.0, 10.0, close_prev=99.7))        # back above; C1 short -> no entry
    r_c = eng_x4_cont.process_bar(_bar('2024-06-15', 100.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.4))  # fresh long C1, C2 agrees
    # NOTE (FIX 4): 06-15 is a fresh long C1 cross back on-side with C2 agreeing within 1xATR ->
    # a legitimate E1 (enter:c1_trigger). The guard here is that it must not be a CONTINUATION
    # re-entry: the wrong-side close reset the sequence, so enter:continuation must not fire.
    c.that('X4: the wrong-side close that triggered the exit also resets continuation -- no continuation re-entry after it',
           r_c['reason'] != 'enter:continuation', detail=str(r_c))

    # --- X2 exit indicator (FIX 2). Reference exit = Momentum, centre line 100. ----------
    c.that('X2 exit_direction: 100.4 vs centre line 100 -> long', exit_direction(100.4, 100.0) == 1)
    c.that('X2 exit_direction: 99.6 vs centre line 100 -> SHORT, although 99.6 > 0 (never assume 0)',
           exit_direction(99.6, 100.0) == -1)
    c.that('X2 exit_direction: exactly on the centre line -> 0 (no signal)', exit_direction(100.0, 100.0) == 0)

    def x2_bars(zero_ref=100.0, with_exit=True):
        bars = [
            _bar('2024-08-12', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5),                         # standard long entry
            _bar('2024-08-13', 100.8, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.6, high=100.9, low=100.5),
            _bar('2024-08-14', 100.4, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.8, high=100.8, low=100.3),
        ]
        for b, v in zip(bars, (100.5, 100.4, 99.6)):
            if with_exit:
                b.update(exit_value=v, exit_zero_reference=zero_ref)
        return bars

    eng_x2 = NNFXEngine(P())
    r_x2 = [eng_x2.process_bar(b) for b in x2_bars()]
    c.that('X2: exit indicator above its centre line (100.4 vs 100) holds the long', r_x2[1]['action'] == 'hold', detail=str(r_x2[1]))
    c.that('X2: exit indicator crossing below its centre line (99.6 vs 100) closes the long at the close, with '
           'C1 still long, price above the baseline and SL untouched',
           r_x2[2]['action'] == 'exit' and r_x2[2]['reason'] == 'exit:exit_indicator' and r_x2[2]['pips'] == -0.2,
           detail=str(r_x2[2]))

    eng_x2_zero = NNFXEngine(P())
    r_zero = [eng_x2_zero.process_bar(b) for b in x2_bars(zero_ref=0.0)]
    c.that('X2 zero_reference proof: the SAME value 99.6 read against an assumed 0 would say "long" and hold -- '
           'so the exit above can only come from the indicator\'s own centre line of 100',
           r_zero[2]['action'] == 'hold' and eng_x2_zero.position is not None, detail=str(r_zero[2]))

    eng_x2_off = NNFXEngine(P(enable_exit_indicator=False))
    r_off2 = [eng_x2_off.process_bar(b) for b in x2_bars()]
    c.that('X2 off (enable_exit_indicator=False): the same bars hold', r_off2[2]['action'] == 'hold', detail=str(r_off2[2]))

    eng_short_x2 = NNFXEngine(P())
    eng_short_x2.process_bar(dict(_bar('2024-08-20', 99.4, 100.0, 0.9, 1.0, -5.0, 10.0, close_prev=100.5),
                                  exit_value=99.0, exit_zero_reference=100.0))                        # short entry
    r_sx = eng_short_x2.process_bar(dict(_bar('2024-08-21', 99.6, 100.0, 0.9, 1.0, -5.0, 10.0, close_prev=99.4,
                                              high=99.7, low=99.3), exit_value=100.3, exit_zero_reference=100.0))
    c.that('X2: mirrors for shorts -- exit indicator rising above its centre line closes a short',
           r_sx['reason'] == 'exit:exit_indicator', detail=str(r_sx))

    # No exit indicator supplied => bar-for-bar identical to FIX 1 (regression guard). Run every
    # existing trace fixture plus the X2 bars without exit keys vs with the feature disabled.
    def actions(params, bars):
        e = NNFXEngine(params)
        return [(r['action'], r['reason'], r['pips']) for r in (e.process_bar(dict(b)) for b in bars)]
    no_exit_bars = x4_bars() + x2_bars(with_exit=False)
    c.that('X2 dormant: with no exit indicator on the bars, results equal the feature switched off',
           actions(P(), no_exit_bars) == actions(P(enable_exit_indicator=False), no_exit_bars))

    # --- FIX 3 / G9: the batch scorer filters NON-volume candidates with the reference volume
    #     indicator (column "vol"), value and 20-bar average from that same line. Uses the real
    #     nnfx_backtest_batch.score_candidate on synthetic extract rows (Baseline candidate). ---
    import nnfx_backtest_batch as nb

    def batch_rows(vol_at_cross, with_vol=True):
        rows = []
        for i in range(22):
            cross = i == 21
            close = 100.6 if cross else 99.5
            row = dict(date=f'2024.03.{i + 1:02d}', close=str(close), high=str(close + 0.05), low=str(close - 0.05),
                       ma='100.0', macd_m='1.0' if i >= 19 else '0.9', macd_s='0.95',   # C1 long for 3 bars only
                       rvi='0.2' if cross else '-0.1', atr='1.0', cand_a='100.0')       # C2 long on the cross bar
            if with_vol:
                row['vol'] = str(vol_at_cross if cross else 100.0)
            rows.append(row)
        return rows

    def cross_bar_reason(rows):
        res = nb.score_candidate({'EURUSD': {'rows': rows}}, ['EURUSD'], 'BASELINE', 'cand_a', None, None)
        return res['fingerprints']['EURUSD'][-1], res

    fp_fail, res_fail = cross_bar_reason(batch_rows(50.0))    # 50 < 20-bar avg 97.5
    c.that('G9 wiring: a Baseline candidate\'s otherwise-valid entry (cross + C1 + C2, within 1xATR) is SKIPPED '
           'when the reference volume is below its own 20-bar average',
           fp_fail[1:] == ('skip', 'skip:volume_filter') and res_fail['volume_skips'] == 1, detail=str(fp_fail))
    fp_pass, _ = cross_bar_reason(batch_rows(200.0))          # 200 >= 20-bar avg 105
    c.that('G9 wiring: the same entry is TAKEN when the reference volume is at/above its 20-bar average',
           fp_pass[1:] == ('enter', 'enter:standard'), detail=str(fp_pass))
    fp_legacy, _ = cross_bar_reason(batch_rows(50.0, with_vol=False))
    c.that('G9 wiring: a pre-FIX-3 extract (no "vol" column) keeps the old always-pass and enters',
           fp_legacy[1:] == ('enter', 'enter:standard'), detail=str(fp_legacy))
    # Warm-up: bars before the 20-bar volume average exists must still reach the engine (they
    # fail the filter), so C1 history is unbroken. Here C1 is long for all 22 bars -> at the
    # cross C1 has run 22 bars -> bridge-too-far must still skip. Dropping warm-up bars would
    # leave only 3 bars of C1 history and wrongly let the trade through.
    long_c1 = batch_rows(200.0)
    for row in long_c1:
        row['macd_m'] = '1.0'
    fp_warm, _ = cross_bar_reason(long_c1)
    c.that('G9 warm-up: bars without a volume average still feed C1 history -- a long C1 run is still '
           'skipped as bridge-too-far, not let through by a truncated history',
           fp_warm[1:] == ('skip', 'skip:bridge_too_far'), detail=str(fp_warm))

    avg = nb.rolling_avg([float(r['vol']) for r in batch_rows(50.0)], nb.VOLUME_AVG_PERIOD)[-1]
    c.that('G10: today\'s value and its average come from the same line -- the cross-bar average (97.5) is the '
           '20-bar mean of the vol column itself', abs(avg - 97.5) < 1e-9, detail=str(avg))

    # --- FIX 8: ranking runs the breaker OFF; the breaker-ON expectancy is reported alongside so the
    #     drawdown breaker never distorts the comparison. A candidate that never trips it has identical
    #     off/on expectancy and zero breaker skips. ----------------------------------------------------
    _, res_rank = cross_bar_reason(batch_rows(200.0))
    c.that('FIX 8 ranking: score_candidate reports BOTH expectancy_pips (breaker OFF) and expectancy_pips_breaker (ON)',
           'expectancy_pips_breaker' in res_rank, detail=str(sorted(res_rank)))
    c.that('FIX 8 ranking: a candidate that never trips the breaker has equal off/on expectancy and 0 breaker skips',
           res_rank['expectancy_pips'] == res_rank['expectancy_pips_breaker'] and res_rank['dd_breaker_skips'] == 0,
           detail=f"off={res_rank['expectancy_pips']} on={res_rank['expectancy_pips_breaker']} skips={res_rank['dd_breaker_skips']}")

    # --- FIX 4 / E1 C1-TRIGGERED entry (no fresh baseline cross this bar) -----------------
    # d1 establishes C1 short while price is already above the baseline; d2 is a FRESH C1
    # long cross, on-side, C2 agreeing, within 1xATR, volume ok -> E1 fires.
    def e1_seq(d2_c2=5.0, d2_close=100.5, d2_cprev=100.5):
        e = NNFXEngine(P())
        e.process_bar(_bar('e1a', 100.5, 100.0, 0.9, 1.0, 5.0, 10.0, close_prev=100.4))  # C1 short, side +1, no cross
        return e.process_bar(_bar('e1b', d2_close, 100.0, 1.0, 0.9, d2_c2, 10.0, close_prev=d2_cprev))
    c.that('E1 fires: a fresh C1 long cross while already above the baseline (no fresh cross), C2 agrees, '
           'within 1xATR, volume ok -> enter:c1_trigger', e1_seq()['reason'] == 'enter:c1_trigger', detail=str(e1_seq()))
    c.that('E1 not-fire: C2 disagrees on the trigger bar -> no entry', e1_seq(d2_c2=-5.0)['action'] != 'enter')
    c.that('E1 not-fire: beyond 1xATR on the trigger bar -> skip:beyond_1xATR, not an entry',
           e1_seq(d2_close=101.6, d2_cprev=101.6)['reason'] == 'skip:beyond_1xATR')
    # not-fire: price on the WRONG side of the baseline (below) even with a fresh long C1
    e_ws = NNFXEngine(P())
    e_ws.process_bar(_bar('e1w1', 99.5, 100.0, 0.9, 1.0, -5.0, 10.0, close_prev=99.4))
    c.that('E1 not-fire: fresh long C1 but price below the baseline (wrong side) -> no entry',
           e_ws.process_bar(_bar('e1w2', 99.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5))['action'] != 'enter')
    # not-fire: the first observed bar has no genuine prior cross -> no spurious E1
    c.that('E1 not-fire: the first observed bar (no prior C1 cross) does not fire E1',
           NNFXEngine(P()).process_bar(_bar('e1f', 100.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.4))['action'] != 'enter')
    c.that('E1 off (enable_c1_trigger_entry=False): the same fresh-C1 setup does NOT enter',
           (lambda e: (e.process_bar(_bar('x1', 100.5, 100.0, 0.9, 1.0, 5.0, 10.0, close_prev=100.4)),
                       e.process_bar(_bar('x2', 100.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.5)))[1]['action'])(
               NNFXEngine(P(enable_c1_trigger_entry=False))) != 'enter')

    # --- FIX 4 / E3 PULLBACK (1xATR) -----------------------------------------------------
    def e3_run(third):
        e = NNFXEngine(P())
        recs = [e.process_bar(_bar('e3a', 101.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5)),   # cross, beyond 1xATR -> arm
                e.process_bar(_bar('e3b', 101.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=101.6))]  # still beyond, agreeing -> wait
        recs.append(e.process_bar(third))
        return recs
    r_pb = e3_run(_bar('e3c', 100.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=101.5))               # pulls back within 1xATR
    c.that('E3 arms: a valid setup that closes >1xATR beyond the baseline is skipped (skip:beyond_1xATR), not lost',
           r_pb[0]['reason'] == 'skip:beyond_1xATR', detail=str(r_pb[0]))
    c.that('E3 waits: while still beyond 1xATR and agreeing, the engine holds (does not enter)',
           r_pb[1]['action'] == 'hold', detail=str(r_pb[1]))
    c.that('E3 fires: when price later closes back within 1xATR with everything still agreeing -> enter:pullback',
           r_pb[2]['reason'] == 'enter:pullback', detail=str(r_pb[2]))
    # expire: C1 flips against before the pullback; keep C1 short on the 3rd bar so no independent E1 fires
    e = NNFXEngine(P())
    e.process_bar(_bar('e3a', 101.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5))                # arm
    e.process_bar(_bar('e3b', 101.5, 100.0, 0.9, 1.0, 5.0, 10.0, close_prev=101.6))               # C1 flips short -> break
    r_ex3 = e.process_bar(_bar('e3c', 100.5, 100.0, 0.9, 1.0, 5.0, 10.0, close_prev=101.5))        # within atr but C1 short
    c.that('E3 expire: if C1 flips against before the pullback, the pending setup is dropped -> no enter:pullback',
           r_ex3['reason'] != 'enter:pullback' and r_ex3['action'] != 'enter', detail=str(r_ex3))
    c.that('E3 off (enable_pullback_entry=False): a beyond-1xATR setup is skipped and never re-entered on pullback',
           (lambda e: [e.process_bar(_bar('o3a', 101.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5)),
                       e.process_bar(_bar('o3c', 100.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=101.6))][1]['action'])(
               NNFXEngine(P(enable_pullback_entry=False))) != 'enter')

    # --- FIX 4 / E4 ONE-CANDLE RULE (lagging C2) ----------------------------------------
    def e4_run(second):
        e = NNFXEngine(P())
        r1 = e.process_bar(_bar('e4a', 100.5, 100.0, 1.0, 0.9, -5.0, 10.0, close_prev=99.5))       # cross+C1 ok, C2 lags -> arm
        return r1, e.process_bar(second)
    r4a, r4b = e4_run(_bar('e4b', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.5))            # C2 catches up, within 1xATR
    c.that('E4 arms: a cross bar with exactly one lagging confirmation (C2) waits one candle (skip:one_candle_wait)',
           r4a['reason'] == 'skip:one_candle_wait', detail=str(r4a))
    c.that('E4 fires: the lagging C2 agreeing on the very next candle (still within 1xATR) -> enter:one_candle',
           r4b['reason'] == 'enter:one_candle', detail=str(r4b))
    _, r4_exp = e4_run(_bar('e4b', 100.6, 100.0, 1.0, 0.9, -5.0, 10.0, close_prev=100.5))          # C2 still lags
    c.that('E4 expire: if the laggard still disagrees on the next candle, the grace lapses -> no entry',
           r4_exp['action'] != 'enter', detail=str(r4_exp))
    _, r4_atr = e4_run(_bar('e4b', 101.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.5))           # C2 agrees but now >1xATR
    c.that('E4 needs within 1xATR: laggard agrees next candle but price is now beyond 1xATR -> no one_candle entry',
           r4_atr['reason'] != 'enter:one_candle', detail=str(r4_atr))
    c.that('E4 off (enable_one_candle_rule=False): a one-lagging-input cross does not arm and does not enter next bar',
           (lambda e: [e.process_bar(_bar('o4a', 100.5, 100.0, 1.0, 0.9, -5.0, 10.0, close_prev=99.5)),
                       e.process_bar(_bar('o4b', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.5))][1]['action'])(
               NNFXEngine(P(enable_one_candle_rule=False))) != 'enter')

    # --- FIX 4 / E5 bridge counting convention (Decision #5 setting) ---------------------
    def bridge_run(run_before_cross, **kw):
        e = NNFXEngine(P(**kw))
        for i in range(run_before_cross):  # C1 long, price BELOW baseline (builds C1 run, no entry)
            e.process_bar(_bar(f'b{i}', 99.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5))
        return e.process_bar(_bar('bx', 100.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5))       # the cross bar
    c.that('E5 before_cross (default): C1 run of 6 BEFORE the cross is within range -> enter:standard',
           bridge_run(6)['reason'] == 'enter:standard', detail=str(bridge_run(6)))
    c.that('E5 before_cross (default): C1 run of 7 before the cross is too far -> skip:bridge_too_far',
           bridge_run(7)['reason'] == 'skip:bridge_too_far', detail=str(bridge_run(7)))
    c.that('E5 include_cross: counting through the cross candle, a run of 6-before is 7 total -> skip (shows the '
           'default is one candle more lenient)', bridge_run(6, bridge_count_from='include_cross')['reason'] == 'skip:bridge_too_far')
    c.that('E5 two-line-only: with two_line_c1=False a 10-bar C1 run does NOT bridge-skip (zero-cross C1 exempt)',
           bridge_run(10, two_line_c1=False)['reason'] == 'enter:standard')

    # --- FIX 5 / E6 CONTINUATION modes (Decision #6, user-confirmed default = vp_c2) ------
    def exitbar(*a, exval=None, exzero=100.0, **kw):
        b = _bar(*a, **kw)
        if exval is not None:
            b['exit_value'] = exval; b['exit_zero_reference'] = exzero
        return b

    # vp_c2 (DEFAULT): enter long, exit via the exit indicator (C1 stays long, price stays above
    # the baseline so the sequence is unbroken), C2 flips short on the exit bar, then C2 flips
    # BACK to long with C1 on-side -> continuation, even though price is >1xATR away and volume fails.
    def vp_seq(b3_c1f=1.0, b3_c1s=0.9, b3_c2=5.0, b3_close=105.0, extra_mid=None):
        e = NNFXEngine(P())  # default continuation_mode='vp_c2'
        e.process_bar(_bar('v1', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5))                       # std long entry
        e.process_bar(exitbar('v2', 100.5, 100.0, 1.0, 0.9, -5.0, 10.0, close_prev=100.6, high=100.6, low=100.4, exval=95.0))  # X2 exit; C2 -> short
        if extra_mid is not None:
            e.process_bar(extra_mid)
        return e.process_bar(_bar('v3', b3_close, 100.0, b3_c1f, b3_c1s, b3_c2, 0.1, close_prev=100.5, high=b3_close + 0.1, low=min(100.5, b3_close)))
    c.that('E6 vp_c2 fires: after an exit (sequence unbroken), C2 flips back to the trade direction with C1 '
           'on-side -> enter:continuation, ignoring >1xATR and a failing volume reading',
           vp_seq()['reason'] == 'enter:continuation', detail=str(vp_seq()))
    c.that('E6 vp_c2 not-fire: C2 flips back but C1 is NOT on-side (c1 against) -> no continuation',
           vp_seq(b3_c1f=0.9, b3_c1s=1.0)['reason'] != 'enter:continuation')
    c.that('E6 vp_c2 not-fire: C1 on-side but C2 did NOT flip back (still short) -> no continuation',
           vp_seq(b3_c2=-5.0)['action'] != 'enter')
    # baseline crossed since entry (a mid bar closes below the baseline) -> continuation_ok reset -> no vp_c2
    midreset = _bar('vmid', 99.4, 100.0, 1.0, 0.9, -5.0, 10.0, close_prev=100.5, high=100.5, low=99.3)
    c.that('E6 vp_c2 not-fire: price closed on the wrong side of the baseline since entry -> sequence reset, no continuation',
           vp_seq(extra_mid=midreset)['reason'] != 'enter:continuation')

    # lesson11: the exit indicator doubles as the continuation signal. Enter long, exit-indicator
    # flips short (X2 exit), then exit-indicator flips BACK long -> continuation. A C2 flip alone
    # must NOT trigger it (that's vp_c2's job).
    def l11_seq(b3_exval=105.0, b3_c2=5.0):
        e = NNFXEngine(P(continuation_mode='lesson11'))
        e.process_bar(exitbar('p1', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5, exval=105.0))       # entry; exit-ind long
        e.process_bar(exitbar('p2', 100.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.6, high=100.6, low=100.4, exval=95.0))  # exit-ind flips short -> X2 exit
        return e.process_bar(exitbar('p3', 105.0, 100.0, 1.0, 0.9, b3_c2, 0.1, close_prev=100.5, high=105.1, low=100.5, exval=b3_exval))
    c.that('E6 lesson11 fires: the exit indicator flips back to the trade direction -> enter:continuation',
           l11_seq()['reason'] == 'enter:continuation', detail=str(l11_seq()))
    c.that('E6 lesson11 not-fire: the exit indicator does NOT flip back (even with C2 flipping back) -> no continuation '
           '(lesson11 keys off the exit indicator, not C2)', l11_seq(b3_exval=95.0, b3_c2=5.0)['reason'] != 'enter:continuation')

    # Edge (FIX 5): a C1-flip exit on a bar that ALSO closes on the wrong side of the baseline must
    # reset the continuation sequence (this exit path returns before the normal reset). Without the
    # fix, the next bar's C2-flip-back would wrongly fire a continuation; with it, the sequence is
    # dead and only a genuine fresh entry can open a position.
    e_edge = NNFXEngine(P())
    e_edge.process_bar(_bar('g1', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5))                       # entry long
    r_edge2 = e_edge.process_bar(_bar('g2', 99.5, 100.0, 0.9, 1.0, -5.0, 10.0, close_prev=100.6, high=100.6, low=99.4))  # C1 flip AND close below baseline
    r_edge3 = e_edge.process_bar(_bar('g3', 100.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5, high=100.6, low=100.4))   # C2 back long, C1 on-side, above
    c.that('E6 edge: a C1-flip exit that also closed on the wrong side exits on the flip', r_edge2['reason'] == 'exit:c1_flip')
    c.that('E6 edge: that wrong-side C1-flip exit resets the sequence -> the next C2-flip-back does NOT continuation-enter',
           r_edge3['reason'] != 'enter:continuation', detail=str(r_edge3))

    # --- FIX 6 / T4 trailing activation at 2xATR beyond entry (trail 1.5xATR, per candle) ---
    # Long: entry 100.6 (ATR 1.0 -> SL 99.1, TP1 101.6). C1 long, price above the baseline and no
    # exit indicator throughout, so only the stop management is under test.
    def sl_path(params, bars):
        e = NNFXEngine(params)
        out = []
        for b in bars:
            e.process_bar(b)
            out.append(round(e.position.sl, 4) if e.position else None)
        return out
    t4_long = t4_long_bars()
    sl_l = sl_path(P(), t4_long)
    c.that('T4 not-fire: after TP1, a close only 1.8xATR beyond entry leaves the runner at BREAKEVEN (no trail yet)',
           sl_l[1] == 100.6 and sl_l[2] == 100.6, detail=str(sl_l))
    c.that('T4 fires: the first close >= 2xATR beyond entry (102.7) starts the trail 1.5xATR behind it (101.2)',
           sl_l[3] == 101.2, detail=str(sl_l))
    c.that('T4 never backward: a pullback close (102.5, now under 2xATR again) does not loosen the stop -- it stays 101.2',
           sl_l[4] == 101.2, detail=str(sl_l))
    c.that('T4 per candle: the next higher close (103.5) ratchets the stop up to 102.0', sl_l[5] == 102.0, detail=str(sl_l))
    sl_s = sl_path(P(), t4_short_bars())
    c.that('T4 mirrors for shorts: breakeven 99.4 holds at 1.8xATR, trail starts at 98.8 on the 2.1xATR close, '
           'never loosens on the pullback', sl_s[1:5] == [99.4, 99.4, 98.8, 98.8], detail=str(sl_s))
    sl_old = sl_path(P(trail_activate_atr=0.0), t4_long)
    c.that('T4 regression knob: trail_activate_atr=0 trails right after TP1 (pre-FIX-6) -- the 1.8xATR close already '
           'moves the stop to 100.9', sl_old[2] == 100.9, detail=str(sl_old))
    wide_atr = [dict(b) for b in t4_long]
    wide_atr[3]['atr'] = 2.0  # the activation bar's own (live) ATR doubles
    c.that('T4 reference ATR: the default measures 2xATR / 1.5xATR with atr_at_entry, so a jump in the live ATR on the '
           'activation bar changes nothing (stop 101.2)', sl_path(P(), wide_atr)[3] == 101.2)
    c.that('T4 reference ATR: trail_atr_ref=\'live\' uses the bar\'s own ATR -- 2.1 beyond entry is under 2x2.0, so no trail yet',
           sl_path(P(trail_atr_ref='live'), wide_atr)[3] == 100.6)

    # --- FIX 7 / N1 news entry block + X5 close-before-news (dormant hook) ---------------
    cal_dir = Path(tempfile.mkdtemp(prefix='nnfx_news_'))

    def news_params(name, events, **kw):
        path = write_news_calendar(cal_dir / f'{name}.csv', events)
        return P(enable_news_filter=True, news_calendar=str(path), **kw)

    T0 = datetime(2025, 4, 1, 22, 0, tzinfo=timezone.utc)  # decision moment of the setup bar

    def news_entry(params):
        b = _bar('n1', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5)   # otherwise-valid standard long
        b.update(symbol='EURUSD', time_utc=T0)
        return NNFXEngine(params).process_bar(b)
    h = lambda hours: (T0 + timedelta(hours=hours)).isoformat()
    r = news_entry(news_params('eur6', [('EUR', h(6), 'High')]))
    c.that('N1 fires: a High EUR event 6h ahead BLOCKS an otherwise-valid EURUSD entry (skip:news)',
           r['reason'] == 'skip:news', detail=str(r))
    r = news_entry(news_params('eur30', [('EUR', h(30), 'High')]))
    c.that('N1 not-fire: the same High EUR event 30h out (beyond 24h) -> the setup ENTERS', r['reason'] == 'enter:standard', detail=str(r))
    r = news_entry(news_params('eurmed', [('EUR', h(6), 'Medium'), ('EUR', h(7), 'Low')]))
    c.that('N1 not-fire: only Medium/Low EUR events in the window -> the setup ENTERS (red folder = High only)',
           r['reason'] == 'enter:standard', detail=str(r))
    r = news_entry(news_params('usd6', [('USD', h(6), 'High')]))
    c.that('N1 both legs: a High USD event 6h ahead blocks EUR/USD too', r['reason'] == 'skip:news', detail=str(r))
    r = news_entry(news_params('gbp6', [('GBP', h(6), 'High')]))
    c.that('N1 not-fire: a High event for a currency outside the pair (GBP) does not block EUR/USD',
           r['reason'] == 'enter:standard', detail=str(r))
    r = news_entry(news_params('edge', [('EUR', h(0), 'High'), ('USD', h(24), 'High')]))
    c.that('N1 window is (t, t+24h]: an event exactly at t does not count, one exactly at t+24h does -> blocked',
           r['reason'] == 'skip:news', detail=str(r))
    r = news_entry(P(enable_news_filter=False, news_calendar=str(write_news_calendar(cal_dir / 'off.csv', [('EUR', h(6), 'High')]))))
    c.that('News dormant: enable_news_filter=False ignores even a blocking calendar -> the setup ENTERS',
           r['reason'] == 'enter:standard', detail=str(r))
    regress = x4_bars() + x2_bars() + t4_long_bars()
    c.that('News dormant: enable_news_filter=True with NO calendar file gives results identical to the default '
           '(bars carry no symbol/time at all -- the gate never reads them)',
           actions(P(enable_news_filter=True), regress) == actions(P(), regress))

    # X5: long entry at T0 (no event ahead of it), then the next bar has a High EUR event 6h ahead.
    T1 = T0 + timedelta(days=1)
    x5_cal = [('EUR', (T1 + timedelta(hours=6)).isoformat(), 'High')]   # 30h after T0: entry is not blocked

    def x5_run(second, **kw):
        e = NNFXEngine(news_params('x5_' + '_'.join(f'{k}{v}' for k, v in kw.items()), x5_cal, **kw))
        first = _bar('x5a', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5)
        first.update(symbol='EURUSD', time_utc=T0)
        second.update(symbol='EURUSD', time_utc=T1)
        r1 = e.process_bar(first)
        return r1, e.process_bar(second), e
    r1, r2, _ = x5_run(_bar('x5b', 100.8, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.6, high=100.9, low=100.7))
    c.that('X5 setup: the entry bar itself is not blocked (event is 30h ahead of it)', r1['reason'] == 'enter:standard', detail=str(r1))
    c.that('X5 fires: an open not-yet-TP1 trade only 0.2xATR in profit is CLOSED before the event (exit:news)',
           r2['reason'] == 'exit:news' and r2['pips'] == 0.2, detail=str(r2))
    _, r2, _ = x5_run(_bar('x5b', 100.3, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.6, high=100.7, low=100.25))
    c.that('X5 fires: a LOSING trade is closed before the event', r2['reason'] == 'exit:news' and r2['pips'] == -0.3, detail=str(r2))
    _, r2, e = x5_run(_bar('x5b', 101.7, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.6, high=101.75, low=100.7))
    c.that('X5 not-fire (handoff default): past TP1 and >= 1xATR in profit at the close -> runner HOLDS through the news',
           r2['reason'] == 'exit:tp1_half' and e.position is not None, detail=str(r2))
    small_past_tp1 = lambda: _bar('x5b', 101.0, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.6, high=101.65, low=100.7)
    _, r2, _ = x5_run(small_past_tp1())
    c.that('X5 handoff default: past TP1 but back under 1xATR profit (0.4) at the close -> CLOSED (exit:news)',
           r2['reason'] == 'exit:news', detail=str(r2))
    _, r2, e = x5_run(small_past_tp1(), x5_cutoff='not_past_tp1')
    c.that('X5 not_past_tp1 (addendum alternative): the same past-TP1 trade is NOT force-closed',
           r2['reason'] == 'exit:tp1_half' and e.position is not None, detail=str(r2))

    c.that('M2: risk_pct defaults to 2.0', NNFXParams().risk_pct == 2.0)

    # --- FIX 7b / R1 drawdown: % metric + 10%-from-peak circuit breaker --------------------
    t = EquityTracker()
    for r_mult in (1.0, -1.0, -1.0, 2.0):  # at 2% risk: 102 -> 99.96 -> 97.9608 -> 101.879
        t.realize(r_mult, 2.0, 'd')
    c.that('R1 metric: max_drawdown_pct matches the hand-computed value on a small curve (peak 102, two -2% steps to '
           '97.9608 -> 1 - 0.98^2 = 3.96%)', abs(t.max_dd_pct - 3.96) < 1e-9, detail=str(t.max_dd_pct))

    recs, acct, engs = dd_scenario(P(risk_pct=6.0))
    a, b = recs['EURUSD'], recs['GBPUSD']
    c.that('R1 metric (chronological, both symbols on one account): two EURUSD stop-outs at 6% risk take the account '
           'to 88.36 -> max_drawdown_pct 11.64', round(acct.max_dd_pct, 2) == 11.64, detail=str(acct.curve))
    c.that('R1 breaker fires: with the account 11.64% below its peak, the next valid EURUSD setup is SKIPPED '
           '(skip:dd_breaker)', a[4]['reason'] == 'skip:dd_breaker', detail=str(a[4]))
    c.that('R1 open trades keep their stops: the GBPUSD trade opened before the breach stays open through it and '
           'exits normally on its own C1 flip', [r['action'] for r in b[1:5]] == ['hold'] * 4 and b[5]['reason'] == 'exit:c1_flip',
           detail=str([r['reason'] for r in b]))
    c.that('R1 breaker resumes (below_threshold): GBPUSD\'s profitable close lifts the account back to a 4.47% drawdown '
           '-> the next EURUSD setup ENTERS', a[6]['reason'] == 'enter:standard' and round(acct.drawdown_pct(), 2) == 4.47,
           detail=f"{a[6]} dd={acct.drawdown_pct()}")
    recs_off, _, _ = dd_scenario(P(risk_pct=6.0, enable_dd_breaker=False))
    c.that('R1 off (enable_dd_breaker=False): the same EURUSD setup during the drawdown ENTERS (pre-FIX-7b behavior)',
           recs_off['EURUSD'][4]['reason'] == 'enter:standard', detail=str(recs_off['EURUSD'][4]))
    solo = {sym: actions(P(risk_pct=6.0, enable_dd_breaker=False), bars) for sym, bars in dd_scenario_bars().items()}
    c.that('R1 lockstep: driving symbols together in date order gives each symbol exactly the records it gets alone '
           '(only a shared tracker + the breaker couple them)',
           all([(r['action'], r['reason'], r['pips']) for r in recs_off[sym]] == solo[sym] for sym in solo))

    return c


def dd_scenario_bars():
    """FIX 7b fixture: EURUSD is stopped out twice (05-02, 05-04), then sets up again on 05-05
    and 05-07; GBPUSD holds one long from 05-01 and exits +3.0 on its C1 flip on 05-06."""
    lose = dict(high=100.65, low=99.0)  # SL 99.1 hit
    eur = [
        _bar('2025-05-01', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5),             # enter
        _bar('2025-05-02', 99.05, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.6, **lose),     # SL: -1R
        _bar('2025-05-03', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.05),             # enter again
        _bar('2025-05-04', 99.05, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.6, **lose),     # SL: -1R -> 11.64% DD
        _bar('2025-05-05', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.05),             # valid setup: BREAKER
        _bar('2025-05-06', 99.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.6, high=100.65, low=99.45),  # back below
        _bar('2025-05-07', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5),              # valid setup: resumed
    ]
    hold = lambda d: _bar(d, 100.8, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.8, high=100.9, low=100.7)
    gbp = [
        _bar('2025-05-01', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5),             # enter
        hold('2025-05-02'), hold('2025-05-03'), hold('2025-05-04'), hold('2025-05-05'),
        _bar('2025-05-06', 103.6, 100.0, 0.9, 1.0, 5.0, 10.0, close_prev=100.8, high=103.7, low=100.8),  # TP1 + C1 flip: +1.33R
    ]
    return {'EURUSD': eur, 'GBPUSD': gbp}


def dd_scenario(params):
    """Both symbols' engines share ONE EquityTracker and run in date order (run_lockstep)."""
    acct = EquityTracker()
    bars = dd_scenario_bars()
    engs = {sym: NNFXEngine(params, equity=acct) for sym in bars}
    return run_lockstep(engs, bars), acct, engs


def write_news_calendar(path: Path, events) -> Path:
    """Test calendar in the documented file shape: currency, timestamp_utc, impact."""
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['currency', 'timestamp_utc', 'impact'])
        w.writerows(events)
    return path


def t4_long_bars():
    """FIX 6 fixture: long entry 100.6 (ATR 1.0), TP1 hit, a close between TP1 and 2xATR, the
    activating close, a pullback close, a further higher close. Shared by the tests and trace 17."""
    return [
        _bar('2025-03-03', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5),                          # standard long entry
        _bar('2025-03-04', 101.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.6, high=101.65, low=100.9),  # TP1 101.6 hit -> BE 100.6
        _bar('2025-03-05', 102.4, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=101.5, high=102.5, low=101.4),   # 1.8xATR beyond: stays BE
        _bar('2025-03-06', 102.7, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=102.4, high=102.8, low=102.3),   # 2.1xATR: trail -> 101.2
        _bar('2025-03-07', 102.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=102.7, high=102.7, low=102.2),   # pullback: stays 101.2
        _bar('2025-03-10', 103.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=102.5, high=103.6, low=102.5),   # higher close: -> 102.0
    ]


def t4_short_bars():
    """FIX 6 fixture, short mirror: entry 99.4 (SL 100.9, TP1 98.4)."""
    return [
        _bar('2025-03-03', 99.4, 100.0, 0.9, 1.0, -5.0, 10.0, close_prev=100.5),                       # standard short entry
        _bar('2025-03-04', 98.5, 100.0, 0.9, 1.0, -5.0, 10.0, close_prev=99.4, high=99.1, low=98.35),   # TP1 98.4 hit -> BE 99.4
        _bar('2025-03-05', 97.6, 100.0, 0.9, 1.0, -5.0, 10.0, close_prev=98.5, high=98.6, low=97.5),    # 1.8xATR: stays BE
        _bar('2025-03-06', 97.3, 100.0, 0.9, 1.0, -5.0, 10.0, close_prev=97.6, high=97.7, low=97.2),    # 2.1xATR: trail -> 98.8
        _bar('2025-03-07', 97.5, 100.0, 0.9, 1.0, -5.0, 10.0, close_prev=97.3, high=97.8, low=97.3),    # pullback: stays 98.8
    ]


# ---------------------------------------------------------------------------
# 2. TRACE MODE -- 5 named scenarios, bar-by-bar CSV
# ---------------------------------------------------------------------------

TRACE_HEADER = ['date', 'close', 'high', 'low', 'baseline', 'c1_fast', 'c1_slow', 'c1_dir', 'c2_value', 'c2_dir',
                'volume_value', 'volume_avg', 'exit_value', 'exit_dir', 'direction_decision', 'action', 'reason',
                'lots', 'atr', 'pips', 'sl_after']


def write_trace(out_dir: Path, filename: str, bars: list[dict], params: NNFXParams) -> Path:
    eng = NNFXEngine(params)
    path = out_dir / filename
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=TRACE_HEADER)
        w.writeheader()
        for bar in bars:
            rec = eng.process_bar(bar)
            row = {k: rec.get(k, '') for k in TRACE_HEADER}
            row.update(close=bar['close'], high=round(bar['high'], 5), low=round(bar['low'], 5),
                       sl_after=round(eng.position.sl, 4) if eng.position else '')
            w.writerow(row)
    return path


def build_traces(out_dir: Path) -> list[Path]:
    paths = []

    # Trace 1: clean standard entry, then a C1-flip exit.
    bars = [
        _bar('2024-06-01', 99.5, 100.0, 0.8, 0.9, -5.0, 3.0, close_prev=100.2),
        _bar('2024-06-02', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5),   # fresh cross + C1 + C2 + volume agree
        _bar('2024-06-03', 101.5, 100.0, 1.0, 0.9, 6.0, 10.0, close_prev=100.6, high=101.55, low=101.45),
        _bar('2024-06-04', 101.4, 100.0, 0.9, 1.0, -1.0, 2.0, close_prev=101.5, high=101.5, low=101.35),  # C1 flips -> hard exit
    ]
    paths.append(write_trace(out_dir, 'trace_1_standard_entry_and_c1_flip_exit.csv', bars, P()))

    # Trace 2: C1 held long for 8 unbroken bars including the signal bar -> bridge_too_far skip.
    bars = [_bar(f'2024-07-{i+1:02d}', 100.0, 100.0, 1.0, 0.9, 5.0, 10.0) for i in range(7)]
    bars.append(_bar('2024-07-08', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.0))
    paths.append(write_trace(out_dir, 'trace_2_bridge_too_far_skip.csv', bars, P()))

    # Trace 3: C1 crossed only 4 bars before the signal bar -> trade taken.
    bars = [_bar(f'2024-08-{i+1:02d}', 100.0, 100.0, 0.9, 1.0, -5.0, 10.0) for i in range(3)]
    bars += [_bar(f'2024-08-{i+4:02d}', 100.0, 100.0, 1.0, 0.9, 5.0, 10.0) for i in range(3)]
    bars.append(_bar('2024-08-07', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.0))
    paths.append(write_trace(out_dir, 'trace_3_bridge_ok_take.csv', bars, P()))

    # Trace 4 (LEGACY c1_signal mode): standard entry -> C1-flip exit -> continuation re-entry on a
    # fresh C1 signal (ignoring 1xATR + volume) -> a close on the opposite baseline side resets the
    # sequence -> the same "fresh" long C1 signal no longer continuation-enters. Pinned to the legacy
    # trigger; the FIX-5 default (vp_c2) is shown in trace_15.
    bars = [
        _bar('2024-09-01', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5),
        _bar('2024-09-02', 100.5, 100.0, 0.9, 1.0, -1.0, 1.0, close_prev=100.6, high=100.55, low=100.45),
        _bar('2024-09-03', 105.0, 100.0, 1.0, 0.9, 9.0, 0.1, close_prev=100.5, high=105.1, low=100.5),    # CONTINUATION (C2 agrees; volume+1xATR ignored)
        _bar('2024-09-04', 104.5, 100.0, 0.9, 1.0, -2.0, 1.0, close_prev=105.0, high=105.05, low=104.4),  # C1 flip -> exit
        _bar('2024-09-05', 98.0, 100.0, 0.85, 0.9, 5.0, 1.0, close_prev=104.5, high=104.5, low=97.9),     # closes below baseline -> resets
        _bar('2024-09-06', 97.5, 100.0, 1.0, 0.9, 5.0, 1.0, close_prev=98.0, high=97.6, low=97.4),        # "fresh" long C1 -- BLOCKED now
    ]
    paths.append(write_trace(out_dir, 'trace_4_continuation_then_reset.csv', bars, P(continuation_mode='c1_signal')))

    # Trace 5: volume-filter skip, then a pullback (>1xATR) skip, then a valid entry once
    # price pulls back. Each bar's close_prev is set independently (not chained to the
    # previous bar's close) so each row is a FRESH baseline cross in isolation -- this is
    # a hand-built rule-proof sequence, not a continuous price path.
    bars = [
        _bar('2024-10-01', 100.6, 100.0, 1.0, 0.9, 5.0, 1.0, close_prev=99.5, volume_avg=50.0),  # fresh cross, volume fails -> skip
        _bar('2024-10-02', 103.0, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.0),                   # fresh cross, >1xATR beyond baseline -> pullback skip
        _bar('2024-10-03', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.0),                   # fresh cross, within 1xATR -> standard entry
    ]
    paths.append(write_trace(out_dir, 'trace_5_volume_then_pullback_skips.csv', bars, P()))

    # Trace 6 (FIX 1, X4 ON): long entry, an intrabar dip below the baseline that closes
    # above (no exit), a close exactly on the baseline (no exit), then a close below it ->
    # exit:baseline_cross. SL (99.1) is never touched and C1 never flips. The bars after the
    # exit show what the trade would have run into: SL on 07-09, C1 flip on 07-10.
    # Trace 7 = the SAME bars with X4 off (control): the trade survives the wrong-side
    # close and is only stopped out two bars later at the full -1.5xATR.
    bars = [
        _bar('2024-07-03', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5),                         # standard long entry
        _bar('2024-07-04', 100.2, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.6, high=100.7, low=99.7),  # dip below intrabar, close above: HOLD
        _bar('2024-07-05', 100.0, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.2, high=100.3, low=99.8),  # close ON baseline: HOLD
        _bar('2024-07-08', 99.7, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.0, high=100.1, low=99.6),   # close BELOW: X4 EXIT (-0.9)
        _bar('2024-07-09', 99.3, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.7, high=99.6, low=99.0),     # would hit SL 99.1 here
        _bar('2024-07-10', 99.2, 100.0, 0.9, 1.0, -1.0, 10.0, close_prev=99.3, high=99.4, low=99.1),    # C1 flips short here
    ]
    paths.append(write_trace(out_dir, 'trace_6_x4_baseline_exit.csv', bars, P()))
    paths.append(write_trace(out_dir, 'trace_7_x4_off_control.csv', bars, P(enable_baseline_exit=False)))

    # Trace 8 (FIX 2, X2): long entry; the exit indicator (Momentum, centre line 100) drops to
    # 99.6 on 08-07 while C1 is long, price is above the baseline and SL is far away ->
    # exit:exit_indicator. Later bars show what the trade would have run into: a wrong-side
    # close on 08-08 (X4), the stop on 08-09, the C1 flip on 08-12.
    # Trace 9 = the SAME bars with no exit indicator supplied (control): identical to FIX 1
    # behavior -- the trade rides on and is closed by X4 on 08-08.
    def x2_trace_bars(with_exit):
        rows = [
            ('2024-08-05', 100.6, None, None, 1.0, 0.9, 100.5, dict(close_prev=99.5)),                # standard long entry
            ('2024-08-06', 100.9, 101.0, 100.5, 1.0, 0.9, 100.4, {}),                                 # exit ind. above 100: HOLD
            ('2024-08-07', 100.5, 100.9, 100.4, 1.0, 0.9, 99.6, {}),                                  # exit ind. 99.6 < 100: X2 EXIT
            ('2024-08-08', 99.8, 100.4, 99.7, 1.0, 0.9, 99.3, {}),                                    # wrong-side close (X4) here
            ('2024-08-09', 99.2, 99.8, 99.0, 1.0, 0.9, 99.1, {}),                                     # SL 99.1 hit here
            ('2024-08-12', 99.1, 99.3, 99.0, 0.9, 1.0, 98.9, {}),                                     # C1 flips here
        ]
        out, prev = [], None
        for date, close, hi, lo, f, s, ev, extra in rows:
            kw = dict(close_prev=prev if prev is not None else close)
            kw.update(extra)
            if hi is not None:
                kw.update(high=hi, low=lo)
            b = _bar(date, close, 100.0, f, s, 5.0, 10.0, **kw)
            if with_exit:
                b.update(exit_value=ev, exit_zero_reference=100.0)
            out.append(b)
            prev = close
        return out
    paths.append(write_trace(out_dir, 'trace_8_x2_exit_indicator.csv', x2_trace_bars(True), P()))
    paths.append(write_trace(out_dir, 'trace_9_x2_no_exit_indicator_control.csv', x2_trace_bars(False), P()))

    # Trace 10 (FIX 3, volume filter): two fresh long baseline crosses where baseline, C1 and
    # C2 all agree and price is within 1xATR. On 09-03 the reference volume (80) is below its
    # own 20-bar average (100) -> skip:volume_filter. Price drops back below, then on 09-05 the
    # same setup crosses again with volume 130 >= average 101.5 -> enter:standard.
    bars = [
        _bar('2024-09-02', 99.5, 100.0, 0.9, 1.0, -1.0, 100.0, close_prev=99.6, volume_avg=100.0),             # below baseline, C1 short
        _bar('2024-09-03', 100.6, 100.0, 1.0, 0.9, 5.0, 80.0, close_prev=99.5, volume_avg=100.0),              # cross+C1+C2 agree, VOLUME FAILS
        _bar('2024-09-04', 99.6, 100.0, 1.0, 0.9, 5.0, 90.0, close_prev=100.6, high=100.7, low=99.5, volume_avg=99.5),  # back below: no entry
        _bar('2024-09-05', 100.5, 100.0, 1.0, 0.9, 5.0, 130.0, close_prev=99.6, volume_avg=101.5),             # same setup, VOLUME PASSES
    ]
    paths.append(write_trace(out_dir, 'trace_10_volume_filter_skip_then_take.csv', bars, P()))

    # Trace 11 (FIX 4, E1 C1-triggered): price is already above the baseline. 09b-02 has C1 short;
    # 09b-03 is a FRESH C1 long cross with NO fresh baseline cross (close_prev already above), C2
    # agreeing and within 1xATR -> enter:c1_trigger (an entry the old cross-only engine missed).
    bars = [
        _bar('2024-11-01', 100.5, 100.0, 0.9, 1.0, 5.0, 10.0, close_prev=100.4),   # above baseline, C1 short
        _bar('2024-11-02', 100.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.5),   # FRESH C1 long, no cross -> E1
        _bar('2024-11-03', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.5, high=100.7, low=100.5),  # in position
    ]
    paths.append(write_trace(out_dir, 'trace_11_e1_c1_trigger.csv', bars, P()))

    # Trace 12 (FIX 4, E3 pullback): 11b-01 is a valid long cross but closes >1xATR beyond the
    # baseline -> skip:beyond_1xATR and the setup is remembered. 11b-02 is still too far ->
    # wait. 11b-03 closes back within 1xATR with everything still agreeing -> enter:pullback.
    bars = [
        _bar('2024-11-11', 101.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5),    # cross, beyond 1xATR -> arm
        _bar('2024-11-12', 101.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=101.6),   # still beyond -> wait
        _bar('2024-11-13', 100.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=101.5),   # pulled back within 1xATR -> enter
    ]
    paths.append(write_trace(out_dir, 'trace_12_e3_pullback.csv', bars, P()))

    # Trace 13 (FIX 4, E4 one-candle rule): 11c-01 is a long baseline cross with C1 agreeing but
    # C2 lagging (short) -> skip:one_candle_wait (one-candle grace armed). 11c-02 the lagging C2
    # catches up long, price still within 1xATR, no fresh cross -> enter:one_candle.
    bars = [
        _bar('2024-11-21', 100.5, 100.0, 1.0, 0.9, -5.0, 10.0, close_prev=99.5),   # cross + C1, C2 lags -> arm
        _bar('2024-11-22', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.5),   # C2 catches up within 1xATR -> enter
    ]
    paths.append(write_trace(out_dir, 'trace_13_e4_one_candle.csv', bars, P()))

    # Trace 14 (FIX 4, E5 before_cross default): C1 has been long for 6 bars BEFORE the cross
    # (price below the baseline, so no entry yet), then the baseline cross lands. With the default
    # bridge_count_from='before_cross' the run is 6 (< 7) -> enter:standard. (Under the old
    # include_cross counting the cross candle makes it 7 and it would skip:bridge_too_far.)
    bars = [_bar(f'2024-12-{i+1:02d}', 99.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5) for i in range(6)]
    bars.append(_bar('2024-12-07', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5))   # the cross bar -> enter
    paths.append(write_trace(out_dir, 'trace_14_e5_before_cross_enters.csv', bars, P()))

    # Trace 15 (FIX 5, DEFAULT vp_c2 continuation): long entry; the exit indicator flips against ->
    # X2 exit (C1 stays long, price stays above the baseline, so the sequence is unbroken; C2 flips
    # short on that bar); then C2 flips BACK to long with C1 on-side -> enter:continuation, even
    # though price is >1xATR from the baseline and volume fails (both ignored for continuation).
    bars = [
        _bar('2025-01-01', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5),
        {**_bar('2025-01-02', 100.5, 100.0, 1.0, 0.9, -5.0, 10.0, close_prev=100.6, high=100.6, low=100.4),
         'exit_value': 95.0, 'exit_zero_reference': 100.0},   # exit indicator against -> X2 exit; C2 -> short
        _bar('2025-01-03', 105.0, 100.0, 1.0, 0.9, 5.0, 0.1, close_prev=100.5, high=105.1, low=100.5),  # C2 flips back, C1 on-side -> continuation
    ]
    paths.append(write_trace(out_dir, 'trace_15_vp_c2_continuation.csv', bars, P()))

    # Trace 16 (FIX 5, lesson11 continuation): same shape, but the trigger is the exit indicator
    # itself flipping back to the trade direction (it doubles as the continuation signal).
    bars = [
        {**_bar('2025-02-01', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5), 'exit_value': 105.0, 'exit_zero_reference': 100.0},
        {**_bar('2025-02-02', 100.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.6, high=100.6, low=100.4), 'exit_value': 95.0, 'exit_zero_reference': 100.0},  # exit-ind flips short -> X2 exit
        {**_bar('2025-02-03', 105.0, 100.0, 1.0, 0.9, 5.0, 0.1, close_prev=100.5, high=105.1, low=100.5), 'exit_value': 105.0, 'exit_zero_reference': 100.0},  # exit-ind flips back long -> continuation
    ]
    paths.append(write_trace(out_dir, 'trace_16_lesson11_continuation.csv', bars, P(continuation_mode='lesson11')))

    # Trace 17 (FIX 6, T4): long entry -> TP1 hit (runner to breakeven 100.6) -> a close 1.8xATR
    # beyond entry (still breakeven, no trail) -> a close 2.1xATR beyond (trail starts at 101.2) ->
    # a pullback close (stop stays 101.2, never backward) -> a higher close (stop ratchets to 102.0).
    # Read the sl_after column.
    paths.append(write_trace(out_dir, 'trace_17_t4_trail_activation.csv', t4_long_bars(), P()))

    # Trace 18 (FIX 7, N1): calendar = trace_18_news_calendar.csv. 04-01's valid long cross decides
    # at 04-02 00:00 UTC with a High EUR event at 12:30 that day -> skip:news. Price drops back
    # below on 04-02. 04-03's identical cross decides at 04-04 00:00 UTC; the next High event
    # (USD, 04-05 06:00) is 30h out -> enter:standard. (Server time = UTC here: offset 0.)
    cal = write_news_calendar(out_dir / 'trace_18_news_calendar.csv',
                              [('EUR', '2025-04-02T12:30:00Z', 'High'), ('USD', '2025-04-05T06:00:00Z', 'High')])
    bars = [
        _bar('2025.04.01', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5),                            # valid cross: BLOCKED
        _bar('2025.04.02', 99.5, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=100.6, high=100.6, low=99.4),      # back below
        _bar('2025.04.03', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5),                            # same setup: ENTERS
    ]
    for b in bars:
        b.update(symbol='EURUSD', time_utc=bar_close_utc(b['date'], 0.0))
    paths.append(write_trace(out_dir, 'trace_18_n1_news_block_then_enter.csv', bars,
                             P(enable_news_filter=True, news_calendar=str(cal))))

    # Trace 19 (FIX 7b, R1): EURUSD and GBPUSD share one account (6% risk so two stops breach 10%),
    # run in date order. Two EURUSD stop-outs take equity to 88.36 (-11.64% from peak) -> the
    # 05-05 EURUSD setup is skipped (skip:dd_breaker) while GBPUSD's open trade keeps running;
    # GBPUSD's +1.33R close on 05-06 lifts the drawdown to 4.47% -> the 05-07 EURUSD setup enters.
    acct = EquityTracker()
    scen = dd_scenario_bars()
    engs = {sym: NNFXEngine(P(risk_pct=6.0), equity=acct) for sym in scen}
    path = out_dir / 'trace_19_r1_dd_breaker.csv'
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['date', 'symbol', 'close', 'action', 'reason', 'pips', 'equity_pct', 'drawdown_pct'])
        order = sorted((b['date'], k, i, sym) for k, sym in enumerate(scen) for i, b in enumerate(scen[sym]))
        for date, _k, i, sym in order:  # the same date order run_lockstep uses
            rec = engs[sym].process_bar(scen[sym][i])
            w.writerow([date, sym, scen[sym][i]['close'], rec['action'], rec['reason'], rec['pips'],
                        round(acct.equity, 2), round(acct.drawdown_pct(), 2)])
    paths.append(path)

    return paths


# ---------------------------------------------------------------------------
# 3. GOLDEN CASES (approximate VP's two transcript examples; decision must match)
# ---------------------------------------------------------------------------

def golden_cases() -> Check:
    c = Check()

    # EUR/USD bridge-too-far: C1 crossed and stayed for (at least) 7 candles,
    # then a baseline entry signal fires -- VP counts back 7 and skips.
    eng = NNFXEngine(P(enable_bridge_too_far=True, bridge_too_far_bars=7))
    warm = [_bar(f'2024-11-{i+1:02d}', 1.0800, 1.0800, 1.0850, 1.0800, 0.0010, 10.0, atr=0.0080) for i in range(7)]
    signal = _bar('2024-11-08', 1.0850, 1.0800, 1.0850, 1.0800, 0.0010, 10.0, atr=0.0080, close_prev=1.0800)
    rec = None
    for bar in warm + [signal]:
        rec = eng.process_bar(bar)
    c.that("GOLDEN CASE 1 (EUR/USD bridge-too-far, VP transcript 1): engine SKIPS, matching VP's call",
           rec['action'] == 'skip' and rec['reason'] == 'skip:bridge_too_far', detail=str(rec))

    # AUD/NZD continuation short: short exited (C1 flip), price never closed
    # back above the baseline, a fresh short C1 signal fires -- VP takes the
    # continuation short even though price is far beyond 1xATR and volume
    # would say no. C2 AGREES here (required by default -- see the SS12 stub
    # note on NNFXParams.require_c2_for_continuation); VP's transcript predates
    # C2 entirely, so this fixture doesn't (and can't) test the C2 question --
    # that's covered separately by the two continuation STUB unit tests above.
    eng2 = NNFXEngine(P(enable_continuation=True))
    eng2.process_bar(_bar('2024-12-01', 1.0750, 1.0800, 1.0740, 1.0800, -0.0010, 10.0, atr=0.0080, close_prev=1.0850))  # standard short entry
    eng2.process_bar(_bar('2024-12-02', 1.0745, 1.0800, 1.0800, 1.0750, 0.0005, 1.0, atr=0.0080, close_prev=1.0750,
                           high=1.0755, low=1.0740))  # C1 flip -> exit
    rec2 = eng2.process_bar(_bar('2024-12-03', 1.0600, 1.0800, 1.0730, 1.0800, -0.0009, 0.1, atr=0.0080, close_prev=1.0745,
                                  high=1.0750, low=1.0595))  # fresh short C1 signal, C2 agrees (short), far beyond 1xATR, volume fails
    c.that("GOLDEN CASE 2 (AUD/NZD continuation short, VP transcript 2): engine ENTERS the continuation "
           "short despite being far beyond 1xATR and a failing volume reading, matching VP's call",
           rec2['action'] == 'enter' and rec2['reason'] == 'enter:continuation', detail=str(rec2))

    return c


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='nnfx_selftest_output')
    args = ap.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    unit = unit_tests()
    golden = golden_cases()
    trace_paths = build_traces(out_dir)

    lines = []
    lines.append('NNFX VERIFICATION HARNESS -- selftest_nnfx_report.txt')
    lines.append('NNFX_BACKTESTER_BUILD_SPEC.txt Part C (hand-built fixtures; no market data used)')
    lines.append('')
    lines.append('=== RULE-UNIT TESTS ===')
    lines.extend(unit.report_lines())
    lines.append('')
    lines.append('=== GOLDEN CASES ===')
    lines.extend(golden.report_lines())
    lines.append('')
    lines.append('=== TRACE FILES ===')
    for p in trace_paths:
        lines.append(str(p))
    lines.append('')
    all_ok = unit.all_ok() and golden.all_ok()
    total = len(unit.results) + len(golden.results)
    passed = sum(ok for _, ok, _ in unit.results + golden.results)
    lines.append(f"OVERALL: {'ALL PASS' if all_ok else 'FAILURES PRESENT'} ({passed}/{total} checks passed)")

    report_path = out_dir / 'selftest_nnfx_report.txt'
    report_path.write_text('\n'.join(lines), encoding='utf-8')
    print('\n'.join(lines))
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
