"""
nnfx_engine.py -- pure NNFX decision logic, mirroring NNFXHarness.mq5 function-
for-function so a reviewer can cross-check this file against the EA source and
NNFX_RULESET_THE_TRUTH.txt directly. No I/O, no market data, no MT5 dependency:
this exists so the ruleset's DECISIONS can be proven correct on hand-built
fixtures (NNFX_BACKTESTER_BUILD_SPEC.txt Part C) before any real batch runs.

Simplification, stated plainly: pip P&L here is computed from the fixture's own
close/high/low, not real tick execution. That is fine for PROVING the rules fire
on the right bar for the right reason (Part C's purpose); it is NOT a substitute
for the real MT5 Strategy Tester accuracy required before any actual batch
(NNFX_BACKTESTER_BUILD_SPEC.txt: "Accuracy = MT5 Strategy Tester on real tick
data; no pure-Python shortcut" -- that requirement is about the real batch in a
later step, not about this rule-proof harness.

Function <-> EA cross-reference (NNFXHarness.mq5):
  c1_direction            <-> C1Direction()
  c2_direction            <-> C2Direction()
  baseline_cross_closed   <-> BaselineCrossClosed()
  baseline_side           <-> BaselineSide()
  volume_passes           <-> VolumePasses()
  c1_direction_run_length <-> C1DirectionRunLength()
  NNFXEngine.process_bar  <-> OnNewDailyBar() + ManageOpenPosition()
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Pure signal functions (NNFX_RULESET_THE_TRUTH.txt SS4)
# ---------------------------------------------------------------------------

def c1_direction(fast: float, slow: float) -> int:
    """Two-line-cross: +1 fast>slow, -1 fast<slow, 0 undecided."""
    if fast > slow: return 1
    if fast < slow: return -1
    return 0


def c2_direction(value: float, zero_reference: float) -> int:
    """Zero-cross vs the candidate's OWN zero_reference (never assumed 0; never
    70/30 / overbought-oversold -- NNFX_RULESET_THE_TRUTH.txt SS4)."""
    if value > zero_reference: return 1
    if value < zero_reference: return -1
    return 0


def baseline_cross_closed(close_now: float, close_prev: float, base_now: float, base_prev: float) -> int:
    """+1/-1 ONLY on the bar the close actually crossed to that side; 0 if it
    was already on that side (that would fire every bar, not just the cross)."""
    now_above = close_now > base_now
    prev_above = close_prev > base_prev
    if now_above and not prev_above: return 1
    if (not now_above) and prev_above: return -1
    return 0


def baseline_side(close_now: float, base_now: float) -> int:
    if close_now > base_now: return 1
    if close_now < base_now: return -1
    return 0


def volume_passes(value: float, avg: float, threshold_mult: float = 1.0) -> bool:
    """Generic, documented Volume/Volatility pass-rule (held identical across
    every candidate during Stage-1 for a fair ranking): current reading >= its
    own N-bar average * threshold. See NNFXHarness.mq5 VolumePasses()."""
    return value >= avg * threshold_mult


def c1_direction_run_length(c1_series: list[tuple[float, float]], end_index: int, direction: int, lookback: int = 60) -> int:
    """Count back from end_index (inclusive) how many consecutive bars C1 has
    held `direction`, unbroken. c1_series[i] = (fast, slow), oldest-to-newest.
    Mirrors NNFXHarness.mq5 C1DirectionRunLength()."""
    count = 0
    i = end_index
    while i >= 0 and count < lookback:
        fast, slow = c1_series[i]
        if c1_direction(fast, slow) != direction:
            break
        count += 1
        i -= 1
    return count


def beyond_pullback_zone(close: float, baseline: float, atr: float, min_beyond_atr: float = 1.0) -> bool:
    """True = price is MORE than min_beyond_atr x ATR beyond the baseline ->
    NO-TRADE zone (standard entries only; continuation ignores this)."""
    return abs(close - baseline) > min_beyond_atr * atr


# ---------------------------------------------------------------------------
# Position / engine state
# ---------------------------------------------------------------------------

@dataclass
class Position:
    direction: int
    entry_price: float
    sl: float
    tp1: float
    atr_at_entry: float
    half1_open: bool = True
    half2_open: bool = True
    tp1_hit: bool = False
    is_continuation: bool = False


@dataclass
class NNFXParams:
    sl_mult: float = 1.5
    tp1_mult: float = 1.0
    min_beyond_atr: float = 1.0
    enable_bridge_too_far: bool = True
    bridge_too_far_bars: int = 7
    bridge_lookback: int = 60
    enable_continuation: bool = True
    # STUB (NNFX_RULESET_THE_TRUTH.txt SS12: "C2 ... full rules ... Implement
    # these as stubbed ... never guessed"): SS7 (continuation) names ONLY C1's
    # fresh signal as the trigger and explicitly lists exactly two rules that
    # are ignored (1xATR-beyond-baseline, the volume filter) -- it says nothing
    # about C2 either way. Defaulting to True (require C2) because NOT checking
    # it would be inventing an unstated third exemption; set False only once
    # VP's exact wording on this is available, per SS12's own instruction.
    require_c2_for_continuation: bool = True
    volume_threshold_mult: float = 1.0
    pip_size: float = 0.0001


@dataclass
class NNFXEngine:
    """Bar-by-bar decision engine. Feed it Bar dicts in chronological order via
    process_bar(); each call returns one trace record. Mirrors
    NNFXHarness.mq5's OnNewDailyBar() + ManageOpenPosition() decision order
    exactly: manage/exit open position -> continuation bookkeeping -> (if flat)
    continuation entry check -> standard entry check."""
    params: NNFXParams = field(default_factory=NNFXParams)
    position: Optional[Position] = None
    trend_dir: int = 0
    continuation_ok: bool = False
    last_exit_dir: int = 0
    last_c1_dir_seen: int = 0
    c1_history: list = field(default_factory=list)  # list of (fast, slow), grows every bar
    realized_pips: float = 0.0

    def _pips(self, entry: float, exitp: float, direction: int) -> float:
        return ((exitp - entry) if direction > 0 else (entry - exitp)) / self.params.pip_size

    def _close_all(self, exit_price: float, reason: str) -> dict:
        pos = self.position
        pips = self._pips(pos.entry_price, exit_price, pos.direction)
        self.last_exit_dir = pos.direction
        self.realized_pips += pips
        self.position = None
        return {'action': 'exit', 'reason': reason, 'direction': pos.direction, 'pips': round(pips, 1)}

    def process_bar(self, bar: dict) -> dict:
        """bar keys: date, close, high, low, baseline, c1_fast, c1_slow,
        c2_value, c2_zero_reference, volume_value, volume_avg, atr."""
        p = self.params
        self.c1_history.append((bar['c1_fast'], bar['c1_slow']))
        idx = len(self.c1_history) - 1

        c1dir = c1_direction(bar['c1_fast'], bar['c1_slow'])
        # Captured BEFORE overwriting, and the overwrite happens unconditionally right
        # here -- every bar, regardless of which branch below returns early -- so a
        # same-direction re-entry right after an exit is correctly recognized as
        # "fresh" even though the position-management code above returns early on
        # the very bar C1 flips (a real bug this exact ordering used to have: see
        # the verification-harness run that caught it).
        prev_c1_dir = self.last_c1_dir_seen
        self.last_c1_dir_seen = c1dir
        c2dir = c2_direction(bar['c2_value'], bar['c2_zero_reference'])
        cross = baseline_cross_closed(bar['close'], bar.get('close_prev', bar['close']), bar['baseline'], bar.get('baseline_prev', bar['baseline']))
        side = baseline_side(bar['close'], bar['baseline'])

        record = {
            'date': bar['date'], 'baseline': bar['baseline'],
            'c1_fast': bar['c1_fast'], 'c1_slow': bar['c1_slow'], 'c1_dir': c1dir,
            'c2_value': bar['c2_value'], 'c2_dir': c2dir,
            'volume_value': bar['volume_value'], 'volume_avg': bar['volume_avg'],
            'direction_decision': cross if cross != 0 else side,
            'action': 'hold', 'reason': '', 'lots': '', 'atr': bar['atr'], 'pips': '',
        }

        # --- manage / hard-exit open position -------------------------------
        if self.position is not None:
            pos = self.position
            # SL check (either half still open uses the same SL until TP1 splits it)
            hit_sl = (bar['low'] <= pos.sl) if pos.direction > 0 else (bar['high'] >= pos.sl)
            if hit_sl:
                rec = self._close_all(pos.sl, 'exit:stop_loss')
                record.update(rec); return record

            # TP1 on half #1 (only before it has already been hit)
            if not pos.tp1_hit:
                hit_tp1 = (bar['high'] >= pos.tp1) if pos.direction > 0 else (bar['low'] <= pos.tp1)
                if hit_tp1:
                    pos.tp1_hit = True
                    pos.half1_open = False
                    # remaining half's stop -> breakeven immediately (NNFX_RULESET_THE_TRUTH.txt SS5)
                    pos.sl = pos.entry_price
                    half_pips = self._pips(pos.entry_price, pos.tp1, pos.direction)
                    record.update({'action': 'exit_half', 'reason': 'exit:tp1_half', 'pips': round(half_pips, 1)})

            # breakeven+trail on the remaining half once TP1 has been hit
            if pos.tp1_hit and pos.half2_open:
                trail = bar['close'] - p.sl_mult * bar['atr'] if pos.direction > 0 else bar['close'] + p.sl_mult * bar['atr']
                improves = trail > pos.sl if pos.direction > 0 else trail < pos.sl
                if improves:
                    pos.sl = trail

            # hard exit: whole remaining position closes immediately on a C1 flip
            if pos.half2_open and c1dir != 0 and c1dir != pos.direction:
                rec = self._close_all(bar['close'], 'exit:c1_flip')
                # If TP1 ALSO fired on this same bar, blend half#1's TP1 pips with half#2's
                # flip-exit pips the same way every other round trip is blended (equal-weighted
                # average -- both halves are equal size). A prior version string-concatenated
                # them ("5.2+3.1") instead of averaging -- caught by a real pilot run, not a
                # hand-built fixture, when TP1 and a flip landed on the identical bar.
                if record['action'] == 'exit_half':
                    rec['pips'] = round((record['pips'] + rec['pips']) / 2.0, 1)
                record.update(rec)
                return record

            if record['action'] == 'exit_half':
                return record

        # --- continuation bookkeeping: reset the moment price closes on the
        #     OPPOSITE side of the baseline from the tracked direction. This is
        #     a STICKY reset -- once broken, only a genuine standard entry (not
        #     just any bare cross) may re-establish a trackable sequence. A bare
        #     cross with no trade (C1/C2 didn't agree, volume failed, etc.) must
        #     NOT resurrect a broken sequence -- see the standard-entry branch
        #     below for where trend_dir/continuation_ok actually get (re)armed.
        #     (A prior version re-armed on ANY cross unconditionally, which
        #     silently undid the reset on the very same bar whenever the reset
        #     bar was itself a fresh cross to the new side -- caught by tracing
        #     a real choppy EURUSD stretch, never by the simpler Part C fixtures.)
        if self.trend_dir != 0 and side != 0 and side != self.trend_dir:
            self.trend_dir = 0
            self.continuation_ok = False

        if self.position is not None:
            return record  # already in a trade; nothing else to evaluate this bar

        # --- CONTINUATION entry: ignores the 1xATR-beyond rule AND the volume
        #     filter; money management is unchanged (NNFX_RULESET_THE_TRUTH.txt SS7) --
        if p.enable_continuation and self.last_exit_dir != 0 and self.continuation_ok and self.trend_dir == self.last_exit_dir:
            fresh_c1_signal = (c1dir != 0 and c1dir != prev_c1_dir and c1dir == self.last_exit_dir)
            c2_ok = (not p.require_c2_for_continuation) or (c2dir == self.last_exit_dir)
            if fresh_c1_signal and c2_ok:
                entry = bar['close']
                sl = entry - p.sl_mult * bar['atr'] if self.last_exit_dir > 0 else entry + p.sl_mult * bar['atr']
                tp1 = entry + p.tp1_mult * bar['atr'] if self.last_exit_dir > 0 else entry - p.tp1_mult * bar['atr']
                self.position = Position(self.last_exit_dir, entry, sl, tp1, bar['atr'], is_continuation=True)
                record.update({'action': 'enter', 'reason': 'enter:continuation', 'direction': self.last_exit_dir})
                self.last_exit_dir = 0
                return record

        # --- STANDARD baseline entry: ALL of baseline-cross, C1, C2, Volume ---
        if cross == 0:
            return record
        if c1dir != cross or c2dir != cross:
            return record
        if not volume_passes(bar['volume_value'], bar['volume_avg'], p.volume_threshold_mult):
            record.update({'action': 'skip', 'reason': 'skip:volume_filter'})
            return record
        if beyond_pullback_zone(bar['close'], bar['baseline'], bar['atr'], p.min_beyond_atr):
            record.update({'action': 'skip', 'reason': 'skip:beyond_1xATR'})
            return record
        if p.enable_bridge_too_far:
            run = c1_direction_run_length(self.c1_history, idx, cross, p.bridge_lookback)
            if run >= p.bridge_too_far_bars:
                record.update({'action': 'skip', 'reason': 'skip:bridge_too_far'})
                return record

        entry = bar['close']
        sl = entry - p.sl_mult * bar['atr'] if cross > 0 else entry + p.sl_mult * bar['atr']
        tp1 = entry + p.tp1_mult * bar['atr'] if cross > 0 else entry - p.tp1_mult * bar['atr']
        self.position = Position(cross, entry, sl, tp1, bar['atr'], is_continuation=False)
        # THIS standard entry's own baseline cross is "the original entry" NNFX_RULESET_THE_TRUTH.txt
        # SS7 tracks from -- only a genuine standard entry may (re)arm a trackable sequence, never a
        # bare cross with no trade behind it (see the reset comment above for why that distinction matters).
        self.trend_dir = cross
        self.continuation_ok = True
        record.update({'action': 'enter', 'reason': 'enter:standard', 'direction': cross})
        return record
