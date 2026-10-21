"""A dead controller with MT5 still open must not hold the batch until its ceiling.

2026-10-20: the controller script stopped with 'Abnormal termination' after 72 of 125 jobs while the
terminal stayed open; _wait_batch only ended on the done flag, the terminal exiting, or the ~19 h
ceiling, so the session sat idle for hours.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import nnfx_backtest_batch as nb  # noqa: E402


class _Alive:
    pid = 111

    def poll(self):
        return None


class WaitEndsOnStall(unittest.TestCase):
    def _run(self, on_sleep, timeout=20 * 3600):
        """_wait_batch on a 30 s-per-call clock with MT5 alive; on_sleep(files, n) runs at every sleep."""
        with tempfile.TemporaryDirectory() as d:
            files = Path(d)
            (files / 'nnfx_batch_results.jsonl').write_text('{"info":"loaded"}\n{"key":"a"}\n')
            clock = iter(range(0, 10 ** 9, 30))
            n = {'sleeps': 0}

            def sleep(_s):
                n['sleeps'] += 1
                on_sleep(files, n['sleeps'])

            with mock.patch.object(nb.pb, 'hide_pid'), \
                    mock.patch.object(nb.time, 'sleep', side_effect=sleep), \
                    mock.patch.object(nb.time, 'monotonic', side_effect=lambda: next(clock)), \
                    mock.patch.object(nb, 'stage_event') as ev:
                res = nb._wait_batch(files, _Alive(), timeout=timeout)
            return res, n['sleeps'], [c.args[0] for c in ev.call_args_list]

    def test_no_new_result_for_the_stall_limit_ends_the_wait(self):
        res, sleeps, events = self._run(lambda files, n: None)
        self.assertFalse(res)
        self.assertIn('batch_stalled', events)
        # ~STALL_SECONDS of 30 s ticks, nowhere near the 20 h ceiling (2400 ticks)
        self.assertLess(sleeps, 2 * nb.STALL_SECONDS // 30)

    def test_steady_progress_is_not_a_stall(self):
        def progress(files, n):
            with open(files / 'nnfx_batch_results.jsonl', 'a') as f:
                f.write('{"key":"k%d"}\n' % n)
            if n == 200:   # 200 ticks = well past STALL_SECONDS in total, but never idle that long
                (files / 'nnfx_batch_done.flag').write_text('done')
        res, sleeps, events = self._run(progress)
        self.assertTrue(res)
        self.assertNotIn('batch_stalled', events)
        self.assertEqual(sleeps, 200)

    def test_relaunch_restarts_the_stall_clock(self):
        """LiveUpdate: MT5 exits 450 s in and relaunches; the update gap writes nothing, so the stall is
        counted from the relaunch (~1050 s), not from the last result line (~600 s)."""
        class _ExitsLater:
            pid = 111
            calls = 0

            def poll(self):
                self.calls += 1
                return None if self.calls <= 15 else 0   # 15 passes x 30 s alive

        with tempfile.TemporaryDirectory() as d:
            files = Path(d)
            (files / 'nnfx_batch_results.jsonl').write_text('{"info":"loaded"}\n')
            clock = iter(range(0, 10 ** 9, 30))
            stamps = []

            def ev(name, **_kw):
                if name == 'batch_stalled':
                    stamps.append(next(clock))

            with mock.patch.object(nb, '_runtime_terminals', return_value=[_ExitsLater()]), \
                    mock.patch.object(nb.pb, 'hide_pid'), mock.patch.object(nb.time, 'sleep'), \
                    mock.patch.object(nb.time, 'monotonic', side_effect=lambda: next(clock)), \
                    mock.patch.object(nb, 'stage_event', side_effect=ev):
                self.assertFalse(nb._wait_batch(files, _ExitsLater(), timeout=20 * 3600, rt=files))
            self.assertEqual(len(stamps), 1)
            self.assertGreater(stamps[0], 450 + nb.STALL_SECONDS)


class StallLimitScalesWithJobTimeout(unittest.TestCase):
    def test_default_job_timeout_keeps_the_floor(self):
        self.assertEqual(nb.stall_limit(nb.JOB_TIMEOUT_SECONDS), nb.STALL_SECONDS)

    def test_longer_job_timeouts_outgrow_the_controllers_silent_stretches(self):
        for t in (60, 90, 300, 600):
            self.assertGreater(nb.stall_limit(t), 103 + 6 * t)   # one repaint job
            self.assertGreater(nb.stall_limit(t), 301 + t)       # a symbol's first job

    def test_the_session_passes_it(self):
        src = (ROOT / 'nnfx_backtest_batch.py').read_text(encoding='utf-8')
        self.assertIn('stall_after=stall_limit(job_timeout)', src)


if __name__ == '__main__':
    unittest.main()
