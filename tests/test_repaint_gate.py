r"""A value that differs on the decision bar (the newest bar of the cut history) is repainting.

2026-10-19, F:\True MQL5 full run: the repaint test reported zigzag/fractal indicators as
'newest_bar_only' and the batch ranked them -- gannzigzag (profit factor 23), Since_Last_Fractal,
swingchart, fastzz, CCI_withShift took 5 of the top 10 places on pivots that later vanish.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nnfx_backtest_batch as nb  # noqa: E402

CONTROLS = [{'repaint_control': r'Examples\Custom Moving Average', 'repaint': 'aligned'},
            {'repaint_control': 'NNFXR_Mirror', 'repaint': 'repaints'},
            {'repaint_control_end': r'Examples\Custom Moving Average', 'repaint': 'aligned'}]


class DecisionBarDifference(unittest.TestCase):
    def label(self, verdict):
        v = nb.repaint_verdicts(CONTROLS + [dict({'key': 'k', 'compared': 400}, **verdict)])
        return nb.repaint_label(v, 'k')

    def test_newest_bar_difference_is_gated_like_repainting(self):
        # run() and rescore() skip scoring for any label starting with 'repaints'
        self.assertTrue(self.label({'repaint': 'newest_bar_only', 'diffs': 1, 'deepest': 0}).startswith('repaints'))

    def test_aligned_stays_aligned(self):
        self.assertEqual(self.label({'repaint': 'aligned', 'diffs': 0, 'deepest': -1}), 'aligned')


class ControllerRepaintWalk(unittest.TestCase):
    """The MQL5 side runs only inside MetaTrader; these pin the properties the verdicts depend on."""
    src = nb.CONTROLLER_SRC

    def test_every_walked_bar_is_a_decision_bar(self):
        # the walk copy ends with bar t+1 at its open; bar t and t-1 are read fresh and compared with FULL
        self.assertIn('o.high=x.open; o.low=x.open; o.close=x.open;', self.src)
        self.assertIn('CopyBuffer(h1,b,1,2,a)', self.src)
        self.assertIn('CopyBuffer(h2,b,n-1-t,2,f)', self.src)
        self.assertIn('u[1]=OpenOnly(g_rp_rates[t+2]);', self.src)

    def test_fresh_load_per_step(self):
        walk = self.src[self.src.index('for(int k=0;k<RP_WALK && diffs==0;k++)'):]
        self.assertLess(walk.index('LoadCalc("NNFXR_WALK"'), walk.index('IndicatorRelease(h1);                // reloaded'))

    def test_pump_minute_bars_are_cleared_before_the_real_bar(self):
        # custom symbols keep minute bars; the open-only day's pump ticks survived the real bar written
        # over it (close came out equal to open) and MetaTrader's own moving average 'repainted'
        walk = self.src[self.src.index('for(int k=0;k<RP_WALK && diffs==0;k++)'):]
        upd = walk.index('CustomRatesUpdate("NNFXR_WALK",u)')
        self.assertLess(walk.index('CustomTicksDelete("NNFXR_WALK"'), upd)
        self.assertLess(walk.index('CustomRatesDelete("NNFXR_WALK",d,d+86399)'), upd)

    def test_a_difference_found_survives_a_later_failure(self):
        # 'untested_*' is scored; a repainter must not get there because a later step timed out
        self.assertIn('string v=(diffs>0)?"repaints":((stop!="")?stop:', self.src)
        self.assertIn('stop="untested_walk_"+st1; break;', self.src)   # a failing step stops; diffs are kept

    def test_walk_has_its_own_deadline(self):
        # a session killed by its overall timeout loses the end control: every verdict becomes untested
        self.assertIn('uint deadline=GetTickCount()+4*timeout;', self.src)
        self.assertIn('stop="untested_walk_too_slow"', self.src)

    def test_bar_t_is_checked_to_be_the_real_bar(self):
        walk = self.src[self.src.index('for(int k=0;k<RP_WALK && diffs==0;k++)'):]
        self.assertLess(walk.index('WalkBarIs(g_rp_rates[t])'), walk.index('LoadCalc("NNFXR_WALK"'))

    def test_no_newest_bar_exemption(self):
        self.assertNotIn('newest_bar_only', self.src)

    def test_custom_copy_is_emptied_and_exact(self):
        # re-filling a custom symbol keeps stored bars outside the replaced range
        self.assertIn('CustomRatesDelete(name,0,LONG_MAX);', self.src)
        self.assertIn('return(Bars(name,PERIOD_D1)==m);', self.src)


if __name__ == '__main__':
    unittest.main()
