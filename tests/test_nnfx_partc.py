"""P1.6: Part C as a test suite -- every check reported on its own, plus content assertions on the
traces (they used to be eyeball-only). nnfx_selftest.py stays the report writer / CI entry point."""
import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import nnfx_selftest as st  # noqa: E402

# trace file -> (reasons that must appear, reasons that must NOT appear)
TRACE_REASONS = {
    'trace_1_standard_entry_and_c1_flip_exit.csv': ({'enter:standard', 'exit:c1_flip'}, set()),
    'trace_2_bridge_too_far_skip.csv': ({'skip:bridge_too_far'}, {'enter:standard'}),
    'trace_3_bridge_ok_take.csv': ({'enter:standard'}, {'skip:bridge_too_far'}),
    'trace_4_continuation_then_reset.csv': ({'enter:standard', 'exit:c1_flip', 'enter:continuation'}, set()),
    'trace_5_volume_then_pullback_skips.csv': ({'skip:volume_filter', 'skip:beyond_1xATR'}, set()),
    'trace_6_x4_baseline_exit.csv': ({'exit:baseline_cross'}, set()),
    'trace_7_x4_off_control.csv': ({'enter:standard', 'exit:stop_loss'}, {'exit:baseline_cross'}),
    'trace_8_x2_exit_indicator.csv': ({'exit:exit_indicator'}, set()),
    'trace_9_x2_no_exit_indicator_control.csv': ({'enter:standard', 'exit:baseline_cross'}, {'exit:exit_indicator'}),
    'trace_10_volume_filter_skip_then_take.csv': ({'skip:volume_filter', 'enter:standard'}, set()),
    'trace_11_e1_c1_trigger.csv': ({'enter:c1_trigger'}, set()),
    'trace_12_e3_pullback.csv': ({'skip:beyond_1xATR', 'enter:pullback'}, set()),
    'trace_13_e4_one_candle.csv': ({'skip:one_candle_wait', 'enter:one_candle'}, set()),
    'trace_14_e5_before_cross_enters.csv': ({'enter:standard'}, {'skip:bridge_too_far'}),
    'trace_15_vp_c2_continuation.csv': ({'enter:continuation'}, set()),
    'trace_16_lesson11_continuation.csv': ({'enter:continuation'}, set()),
    'trace_17_t4_trail_activation.csv': ({'exit:tp1_half'}, set()),
    'trace_18_n1_news_block_then_enter.csv': ({'skip:news', 'enter:standard'}, set()),
    'trace_19_r1_dd_breaker.csv': ({'skip:dd_breaker', 'exit:stop_loss'}, set()),
}
# traces whose point is a skip FOLLOWED by an entry: (skip reason, entry reason)
SKIP_THEN_ENTER = {
    'trace_10_volume_filter_skip_then_take.csv': ('skip:volume_filter', 'enter:standard'),
    'trace_12_e3_pullback.csv': ('skip:beyond_1xATR', 'enter:pullback'),
    'trace_13_e4_one_candle.csv': ('skip:one_candle_wait', 'enter:one_candle'),
    'trace_18_n1_news_block_then_enter.csv': ('skip:news', 'enter:standard'),
}


class PartCChecks(unittest.TestCase):
    def test_every_check(self):
        for group in (st.unit_tests(), st.golden_cases()):
            for name, ok, detail in group.results:
                with self.subTest(check=name[:120]):
                    self.assertTrue(ok, detail)


class PartCTraces(unittest.TestCase):
    def test_trace_contents(self):
        with tempfile.TemporaryDirectory(prefix='nnfx_traces_') as tmp:
            out = Path(tmp)
            written = {p.name for p in st.build_traces(out)}
            self.assertTrue(set(TRACE_REASONS) <= written, sorted(set(TRACE_REASONS) - written))
            for name, (must, must_not) in TRACE_REASONS.items():
                with self.subTest(trace=name):
                    with open(out / name, encoding='utf-8') as f:
                        rows = list(csv.DictReader(f))
                    self.assertTrue(rows, 'empty trace')
                    order = [r['reason'] for r in rows if r.get('reason')]
                    reasons = set(order)
                    self.assertTrue(must <= reasons, f'missing {must - reasons}')
                    self.assertFalse(must_not & reasons, f'unexpected {must_not & reasons}')
                    if name in SKIP_THEN_ENTER:
                        skip, enter = SKIP_THEN_ENTER[name]
                        self.assertLess(order.index(skip), order.index(enter), f'{skip} must come before {enter}')


if __name__ == '__main__':
    unittest.main()
