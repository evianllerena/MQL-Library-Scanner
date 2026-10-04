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
  exit_direction          <-> ExitDirection()
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


def exit_direction(value: float, zero_reference: float) -> int:
    """Exit indicator side vs its OWN zero_reference (X2; never assumed 0). Same
    centre-line read as c2_direction -- e.g. Momentum's centre line is 100."""
    return c2_direction(value, zero_reference)


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
    # E5 counting convention (NNFX_RULESET_THE_TRUTH.txt SS4 E5 / Decision #5 -- user
    # UNRESOLVED, so a SETTING). 'before_cross' (default, per MASTER_HANDOFF) counts C1's
    # unbroken same-direction run ENDING ON THE CANDLE BEFORE the baseline cross;
    # 'include_cross' counts through the cross candle itself (pre-FIX-4 behavior). Bridge-
    # too-far is a TWO-LINE C1 rule only -- never applied to a zero-cross/single-line C1
    # (the caller passes two_line_c1=False to suppress it; default True preserves behavior).
    bridge_count_from: str = 'before_cross'
    two_line_c1: bool = True
    # --- FIX 4 new entry types (NNFX_RULESET_THE_TRUTH.txt SS4). Each a SETTING, default
    #     ON; turn one off to isolate it in the backtest. ------------------------------
    # E1 (label B) C1-TRIGGERED: C1 produces a FRESH signal this bar while price is ALREADY
    #   on the correct side of the baseline (no fresh cross this bar), within 1xATR, with C2
    #   and volume agreeing. Bridge-too-far is NOT applied to E1: SS4's bridge rule is about
    #   C1 LEADING the baseline cross; here the cross already happened and C1 is the trigger,
    #   so the "bridge" does not map. SOURCE-SILENT on E1xbridge -> defaulted OFF + flagged.
    enable_c1_trigger_entry: bool = True
    # E3 (label B) PULLBACK / 1xATR: a setup that is valid in every respect EXCEPT it closed
    #   >1xATR beyond the baseline is remembered; if a LATER candle closes back WITHIN 1xATR
    #   with everything still agreeing, enter then. Expiry is AGREEMENT-BASED (dropped when
    #   C1 or C2 flips off the setup direction, or price closes back to the wrong side of the
    #   baseline). No fixed bar-count expiry exists in the sources -> none invented.
    enable_pullback_entry: bool = True
    # E4 (label B) ONE-CANDLE RULE: if EXACTLY ONE input lags on an otherwise-valid setup,
    #   wait at most ONE candle for it to agree; price must STILL be within 1xATR on that
    #   second candle (Decision #4). >>> SCOPE IS UNCLEAR IN SOURCES <<< -- implemented for a
    #   lagging C1 OR C2 on the cross bar (symmetric: exactly one of the two confirmations is
    #   one candle late). A lagging BASELINE resolves as a normal standard entry on the actual
    #   cross bar; a failing VOLUME is not a one-candle case (volume must pass to arm).
    #   Default ON, FLAGGED for user confirm.
    enable_one_candle_rule: bool = True
    enable_continuation: bool = True
    # STUB (NNFX_RULESET_THE_TRUTH.txt SS12: "C2 ... full rules ... Implement
    # these as stubbed ... never guessed"): SS7 (continuation) names ONLY C1's
    # fresh signal as the trigger and explicitly lists exactly two rules that
    # are ignored (1xATR-beyond-baseline, the volume filter) -- it says nothing
    # about C2 either way. Defaulting to True (require C2) because NOT checking
    # it would be inventing an unstated third exemption; set False only once
    # VP's exact wording on this is available, per SS12's own instruction.
    require_c2_for_continuation: bool = True
    # X4 (NNFX_RULESET_THE_TRUTH.txt SS5, label B, Decision 2): a close on the
    # wrong side of the baseline closes what is left. False = pre-FIX-1 behavior.
    enable_baseline_exit: bool = True
    # X2 (SS5, label A slot / B mechanics): the exit indicator turning against the
    # trade closes what is left. Only active on bars that carry exit_value; a bar
    # without one (no exit indicator supplied) behaves exactly as before FIX 2.
    enable_exit_indicator: bool = True
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
    # E3: a standard setup was valid but beyond 1xATR; remember its direction until price
    # pulls back in or agreement breaks. E4: exactly one input lagged on the previous bar;
    # remember the direction + the bar index so the grace is exactly ONE candle.
    pending_pullback_dir: int = 0
    pending_onecandle_dir: int = 0
    pending_onecandle_bar: int = -1

    def _pips(self, entry: float, exitp: float, direction: int) -> float:
        return ((exitp - entry) if direction > 0 else (entry - exitp)) / self.params.pip_size

    def _close_all(self, exit_price: float, reason: str) -> dict:
        pos = self.position
        pips = self._pips(pos.entry_price, exit_price, pos.direction)
        self.last_exit_dir = pos.direction
        self.realized_pips += pips
        self.position = None
        return {'action': 'exit', 'reason': reason, 'direction': pos.direction, 'pips': round(pips, 1)}

    def _reset_continuation_if_wrong_side(self, side: int) -> None:
        if self.trend_dir != 0 and side != 0 and side != self.trend_dir:
            self.trend_dir = 0
            self.continuation_ok = False

    def _bridge_too_far(self, direction: int, idx: int) -> bool:
        """E5. Two-line C1 ONLY (suppressed when two_line_c1 is False). 'before_cross'
        counts C1's unbroken run ending on the candle BEFORE the cross (idx-1); the run
        >= bridge_too_far_bars means C1 led the cross by too many candles -> skip."""
        p = self.params
        if not p.enable_bridge_too_far or not p.two_line_c1:
            return False
        end = idx - 1 if p.bridge_count_from == 'before_cross' else idx
        if end < 0:
            return False
        run = c1_direction_run_length(self.c1_history, end, direction, p.bridge_lookback)
        return run >= p.bridge_too_far_bars

    def _open(self, direction: int, bar: dict, reason: str, record: dict) -> dict:
        """Open a fresh (non-continuation) position and arm the continuation tracker.
        Shared by E1/E2/E3/E4 -- all of them establish an 'original entry' SS7 tracks from."""
        p = self.params
        entry = bar['close']
        sl = entry - p.sl_mult * bar['atr'] if direction > 0 else entry + p.sl_mult * bar['atr']
        tp1 = entry + p.tp1_mult * bar['atr'] if direction > 0 else entry - p.tp1_mult * bar['atr']
        self.position = Position(direction, entry, sl, tp1, bar['atr'], is_continuation=False)
        self.trend_dir = direction
        self.continuation_ok = True
        record.update({'action': 'enter', 'reason': reason, 'direction': direction})
        return record

    def process_bar(self, bar: dict) -> dict:
        """bar keys: date, close, high, low, baseline, c1_fast, c1_slow,
        c2_value, c2_zero_reference, volume_value, volume_avg, atr.
        Optional: exit_value + exit_zero_reference (X2 exit indicator)."""
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
        exit_value = bar.get('exit_value')
        exitdir = exit_direction(exit_value, bar['exit_zero_reference']) if exit_value is not None else 0

        record = {
            'date': bar['date'], 'baseline': bar['baseline'],
            'c1_fast': bar['c1_fast'], 'c1_slow': bar['c1_slow'], 'c1_dir': c1dir,
            'c2_value': bar['c2_value'], 'c2_dir': c2dir,
            'volume_value': bar['volume_value'], 'volume_avg': bar['volume_avg'],
            'exit_value': exit_value if exit_value is not None else '', 'exit_dir': exitdir if exit_value is not None else '',
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

            # X2: exit indicator turned against the trade (read vs its own zero_reference).
            # X4: a close on the wrong side of the baseline. Either closes what is left.
            # Both branches return before the continuation bookkeeping below, so they
            # apply its wrong-side reset themselves (SS6: no baseline cross since entry).
            on_close_exit = None
            if p.enable_exit_indicator and pos.half2_open and exitdir != 0 and exitdir != pos.direction:
                on_close_exit = 'exit:exit_indicator'
            elif p.enable_baseline_exit and pos.half2_open and side != 0 and side != pos.direction:
                on_close_exit = 'exit:baseline_cross'
            if on_close_exit:
                rec = self._close_all(bar['close'], on_close_exit)
                if record['action'] == 'exit_half':
                    rec['pips'] = round((record['pips'] + rec['pips']) / 2.0, 1)
                record.update(rec)
                self._reset_continuation_if_wrong_side(side)
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
        self._reset_continuation_if_wrong_side(side)

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

        # --- shared entry conditions for this bar -----------------------------
        within_atr = not beyond_pullback_zone(bar['close'], bar['baseline'], bar['atr'], p.min_beyond_atr)
        vol_ok = volume_passes(bar['volume_value'], bar['volume_avg'], p.volume_threshold_mult)

        # --- E3 PULLBACK resolution: we already committed to waiting on a setup that
        #     was valid but beyond 1xATR. Enter when price closes back WITHIN 1xATR with
        #     everything still agreeing; drop it if agreement breaks; else keep waiting. --
        if self.pending_pullback_dir != 0:
            d = self.pending_pullback_dir
            broke = (side == -d) or (c1dir != 0 and c1dir != d) or (c2dir != 0 and c2dir != d)
            if broke:
                self.pending_pullback_dir = 0
            else:
                if within_atr and side == d and c1dir == d and c2dir == d and vol_ok and not self._bridge_too_far(d, idx):
                    self.pending_pullback_dir = 0
                    return self._open(d, bar, 'enter:pullback', record)
                return record  # still on-side and agreeing but not yet back within 1xATR -> wait

        # --- E4 ONE-CANDLE resolution: exactly one input lagged on the PREVIOUS bar;
        #     the grace is exactly one candle (idx == armed_bar + 1). Enter if the laggard
        #     has caught up AND price is still within 1xATR; otherwise the grace expires. --
        if self.pending_onecandle_dir != 0:
            d = self.pending_onecandle_dir
            is_next_bar = (idx == self.pending_onecandle_bar + 1)
            self.pending_onecandle_dir = 0
            self.pending_onecandle_bar = -1
            if is_next_bar and within_atr and side == d and c1dir == d and c2dir == d and vol_ok and not self._bridge_too_far(d, idx):
                return self._open(d, bar, 'enter:one_candle', record)
            # expired or still not agreeing -> fall through to fresh evaluation

        # --- E2 STANDARD baseline entry (cross-triggered): baseline-cross + C1 + C2 +
        #     Volume + within 1xATR + not bridge-too-far. A valid-but-too-far setup arms
        #     E3; a setup with exactly ONE lagging confirmation arms E4. ----------------
        if cross != 0:
            c1_ok = (c1dir == cross)
            c2_ok = (c2dir == cross)
            if c1_ok and c2_ok:
                if not vol_ok:
                    record.update({'action': 'skip', 'reason': 'skip:volume_filter'}); return record
                if not within_atr:
                    if p.enable_pullback_entry and not self._bridge_too_far(cross, idx):
                        self.pending_pullback_dir = cross
                    record.update({'action': 'skip', 'reason': 'skip:beyond_1xATR'}); return record
                if self._bridge_too_far(cross, idx):
                    record.update({'action': 'skip', 'reason': 'skip:bridge_too_far'}); return record
                return self._open(cross, bar, 'enter:standard', record)
            # exactly one of C1/C2 lags on the cross bar -> one-candle grace (E4)
            if p.enable_one_candle_rule and within_atr and vol_ok and not self._bridge_too_far(cross, idx):
                lagging = (0 if c1_ok else 1) + (0 if c2_ok else 1)
                if lagging == 1:
                    self.pending_onecandle_dir = cross
                    self.pending_onecandle_bar = idx
                    record.update({'action': 'skip', 'reason': 'skip:one_candle_wait'}); return record
            return record

        # --- E1 C1-TRIGGERED entry (no fresh cross this bar): C1 FRESHLY signals while
        #     price is ALREADY on the correct side, within 1xATR, C2 + volume agree.
        #     Bridge-too-far is NOT applied to E1 (see NNFXParams note). ----------------
        if p.enable_c1_trigger_entry:
            d = c1dir
            # A C1 "signal" is a genuine two-line CROSS: C1 must have been on the OPPOSITE
            # side on the previous bar (prev_c1_dir == -d). This excludes a standing C1
            # reading and the first observed bar (prev_c1_dir == 0), where no cross occurred.
            fresh_c1 = (d != 0 and prev_c1_dir == -d)
            if fresh_c1 and side == d and c2dir == d:
                if not vol_ok:
                    record.update({'action': 'skip', 'reason': 'skip:volume_filter'}); return record
                if not within_atr:
                    if p.enable_pullback_entry:
                        self.pending_pullback_dir = d
                    record.update({'action': 'skip', 'reason': 'skip:beyond_1xATR'}); return record
                return self._open(d, bar, 'enter:c1_trigger', record)
        return record
