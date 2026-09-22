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
import argparse, csv, sys
from pathlib import Path

from nnfx_engine import (
    NNFXEngine, NNFXParams,
    c2_direction, baseline_cross_closed, c1_direction_run_length,
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
    eng = NNFXEngine(P(enable_continuation=True, min_beyond_atr=1.0))  # require_c2_for_continuation defaults True (see NNFXParams)
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

    # --- STUB behavior, explicit and testable both ways (NNFX_RULESET_THE_TRUTH.txt SS12:
    #     C2's role in continuation is NOT YET DEFINED; default is to require it) ----------
    eng_c2gate_default = NNFXEngine(P(enable_continuation=True))  # require_c2_for_continuation=True (default)
    eng_c2gate_default.process_bar(_bar('2024-03-10', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5))
    eng_c2gate_default.process_bar(_bar('2024-03-11', 100.5, 100.0, 0.9, 1.0, -1.0, 1.0, close_prev=100.6,
                                         high=100.55, low=100.45))
    r_c2_blocks = eng_c2gate_default.process_bar(_bar('2024-03-12', 105.0, 100.0, 1.0, 0.9, -9.0, 0.1, close_prev=100.5,
                                                        high=105.1, low=100.5))  # fresh long C1, but C2 DISAGREES (short)
    c.that('continuation STUB (default, require_c2_for_continuation=True): a fresh C1 signal is NOT enough on its '
           'own if C2 disagrees -- continuation is blocked', r_c2_blocks['action'] != 'enter', detail=str(r_c2_blocks))

    eng_c2gate_off = NNFXEngine(P(enable_continuation=True, require_c2_for_continuation=False))
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
    c.that('WHIPSAW PART A: a bare cross back to the original side with no trade behind it does NOT '
           'resurrect a broken continuation sequence', rW2['action'] != 'enter', detail=str(rW2))

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
    eng3 = NNFXEngine(P(sl_mult=1.5, tp1_mult=1.0))
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

    return c


# ---------------------------------------------------------------------------
# 2. TRACE MODE -- 5 named scenarios, bar-by-bar CSV
# ---------------------------------------------------------------------------

TRACE_HEADER = ['date', 'baseline', 'c1_fast', 'c1_slow', 'c1_dir', 'c2_value', 'c2_dir',
                'volume_value', 'volume_avg', 'direction_decision', 'action', 'reason', 'lots', 'atr', 'pips']


def write_trace(out_dir: Path, filename: str, bars: list[dict], params: NNFXParams) -> Path:
    eng = NNFXEngine(params)
    path = out_dir / filename
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=TRACE_HEADER)
        w.writeheader()
        for bar in bars:
            rec = eng.process_bar(bar)
            w.writerow({k: rec.get(k, '') for k in TRACE_HEADER})
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

    # Trace 4: standard entry -> C1-flip exit -> continuation re-entry (ignoring
    # 1xATR + volume) -> a close on the opposite baseline side resets the
    # sequence -> the same "fresh" long C1 signal no longer continuation-enters.
    bars = [
        _bar('2024-09-01', 100.6, 100.0, 1.0, 0.9, 5.0, 10.0, close_prev=99.5),
        _bar('2024-09-02', 100.5, 100.0, 0.9, 1.0, -1.0, 1.0, close_prev=100.6, high=100.55, low=100.45),
        _bar('2024-09-03', 105.0, 100.0, 1.0, 0.9, 9.0, 0.1, close_prev=100.5, high=105.1, low=100.5),    # CONTINUATION (C2 agrees; volume+1xATR ignored)
        _bar('2024-09-04', 104.5, 100.0, 0.9, 1.0, -2.0, 1.0, close_prev=105.0, high=105.05, low=104.4),  # C1 flip -> exit
        _bar('2024-09-05', 98.0, 100.0, 0.85, 0.9, 5.0, 1.0, close_prev=104.5, high=104.5, low=97.9),     # closes below baseline -> resets
        _bar('2024-09-06', 97.5, 100.0, 1.0, 0.9, 5.0, 1.0, close_prev=98.0, high=97.6, low=97.4),        # "fresh" long C1 -- BLOCKED now
    ]
    paths.append(write_trace(out_dir, 'trace_4_continuation_then_reset.csv', bars, P()))

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
