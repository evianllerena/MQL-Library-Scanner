"""An MT5 LiveUpdate relaunch carries the batch session on; the wait must follow it.

2026-10-18 (build 6272 released): the launched terminal exited ~1 min in, LiveUpdate relaunched the
runtime's terminal with the same /config, and that relaunch finished the extraction -- but
_wait_batch only watched the first process and reported batch_timeout.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import nnfx_backtest_batch as nb  # noqa: E402


class _Exited:
    pid = 111

    def poll(self):
        return 0


class _Proc:
    def __init__(self, pid, name, exe, cmdline):
        self.pid = pid
        self.info = {'pid': pid, 'name': name, 'exe': exe, 'cmdline': cmdline}


class WaitFollowsLiveUpdateRelaunch(unittest.TestCase):
    def test_relaunch_that_finishes_counts_as_finished(self):
        with tempfile.TemporaryDirectory() as d:
            files = Path(d)
            calls = {'n': 0}

            def relaunched(_rt):
                calls['n'] += 1
                if calls['n'] == 3:  # the relaunch writes the done flag, then MT5 stays open
                    (files / 'nnfx_batch_done.flag').write_text('ok')
                return [_Proc(222, 'terminal64.exe', 'x', [])]

            with mock.patch.object(nb, '_runtime_terminals', side_effect=relaunched), \
                    mock.patch.object(nb.pb, 'hide_pid'), mock.patch.object(nb.time, 'sleep'), \
                    mock.patch.object(nb, 'stage_event'):
                self.assertTrue(nb._wait_batch(files, _Exited(), timeout=60, rt=files))

    def _loops(self, terminals, timeout=3600, **kw):
        """Run _wait_batch on a 30 s-per-call clock; return (result, sleeps)."""
        with tempfile.TemporaryDirectory() as d:
            clock = iter(range(0, 100000, 30))
            seq = iter(terminals)
            with mock.patch.object(nb, '_runtime_terminals', side_effect=lambda _rt: next(seq, [])), \
                    mock.patch.object(nb.pb, 'hide_pid'), mock.patch.object(nb.time, 'sleep') as sleep, \
                    mock.patch.object(nb.time, 'monotonic', side_effect=lambda: next(clock)), \
                    mock.patch.object(nb, 'stage_event'):
                res = nb._wait_batch(Path(d), _Exited(), timeout=timeout, rt=Path(d), relaunch_grace=90, **kw)
                return res, sleep.call_count

    def test_no_relaunch_gives_up_after_the_grace_not_before(self):
        res, sleeps = self._loops([])
        self.assertFalse(res)
        self.assertTrue(2 <= sleeps <= 4, sleeps)   # ~90 s of 30 s steps: neither instant nor the full hour

    def test_grace_restarts_when_a_relaunch_appears_after_a_gap(self):
        r = _Proc(9, 'terminal64.exe', 'x', [])
        _, base = self._loops([])
        _, sleeps = self._loops([[], [], [r]])
        self.assertGreater(sleeps, base + 1)

    def test_relaunch_extends_the_budget(self):
        r = _Proc(9, 'terminal64.exe', 'x', [])
        _, short = self._loops([[r]] * 100, timeout=60, relaunch_allowance=0)
        _, longer = self._loops([[r]] * 100, timeout=60, relaunch_allowance=300)
        self.assertGreaterEqual(longer - short, 8)  # 300 s more at 30 s per loop

    def test_runtime_terminals_match_the_runtime_not_the_owners_terminal(self):
        rt = Path(tempfile.gettempdir()) / 'nnfx-rt-test'
        rts = str(rt.resolve())
        procs = [
            _Proc(1, 'terminal64.exe', rts + '\\terminal64.exe', [rts + '\\terminal64.exe', '/portable']),
            _Proc(2, 'terminal64.exe', 'C:\\Users\\x\\AppData\\Roaming\\MetaQuotes\\Terminal\\AE\\liveupdate\\terminal64.exe',
                  ['terminal64.exe', '/update', f'/path:{rts}', '/portable']),
            _Proc(3, 'terminal64.exe', 'C:\\Program Files\\MetaTrader 5\\terminal64.exe',
                  ['C:\\Program Files\\MetaTrader 5\\terminal64.exe']),
            _Proc(4, 'notepad.exe', rts + '\\notepad.exe', []),
            _Proc(5, 'terminal64.exe', rts + '-render-server\\terminal64.exe',          # sibling folder
                  [rts + '-render-server\\terminal64.exe', f'/config:{rts}-render-server\\x.ini']),
            _Proc(6, 'terminal64.exe', 'C:\\x\\terminal64.exe', ['terminal64.exe', f'/path:"{rts}"']),
        ]
        fake_psutil = mock.Mock(process_iter=mock.Mock(return_value=procs))  # CI's Linux job has no psutil
        with mock.patch.dict(sys.modules, {'psutil': fake_psutil}):
            self.assertEqual(sorted(p.pid for p in nb._runtime_terminals(rt)), [1, 2, 6])


if __name__ == '__main__':
    unittest.main()
