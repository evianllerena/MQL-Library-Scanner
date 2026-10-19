"""A value that differs on the decision bar (the newest bar of the cut history) is repainting.

2026-10-19, F:\True MQL5 full run: the repaint test reported zigzag/fractal indicators as
'newest_bar_only' and the batch ranked them -- gannzigzag (profit factor 23), Since_Last_Fractal,
swingchart, fastzz, CCI_withShift took 5 of the top 10 places on pivots that later vanish.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nnfx_backtest_batch as nb  # noqa: E402

CONTROLS = [{'repaint_control': 'Examples\Custom Moving Average', 'repaint': 'aligned'},
            {'repaint_control': 'NNFXR_Mirror', 'repaint': 'repaints'},
            {'repaint_control_end': 'Examples\Custom Moving Average', 'repaint': 'aligned'}]


class DecisionBarDifference(unittest.TestCase):
    def label(self, verdict):
        v = nb.repaint_verdicts(CONTROLS + [dict({'key': 'k', 'compared': 400}, **verdict)])
        return nb.repaint_label(v, 'k')

    def test_newest_bar_difference_is_gated_like_repainting(self):
        # run() and rescore() skip scoring for any label starting with 'repaints'
        self.assertTrue(self.label({'repaint': 'newest_bar_only', 'diffs': 1, 'deepest': 0}).startswith('repaints'))

    def test_aligned_stays_aligned(self):
        self.assertEqual(self.label({'repaint': 'aligned', 'diffs': 0, 'deepest': -1}), 'aligned')


if __name__ == '__main__':
    unittest.main()
