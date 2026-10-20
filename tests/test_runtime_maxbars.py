"""set_runtime_maxbars rewrites the batch runtime's config/common.ini every session.

2026-10-19: written in text mode with '\r\n' joins, Windows turned every line ending into '\r\r\n';
the next read split each into two lines, so the file doubled every session until reading it raised
MemoryError (the session died before compiling anything, and Claude Code killed an earlier one for
memory)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nnfx_backtest_batch as nb  # noqa: E402


class MaxBarsRewrite(unittest.TestCase):
    def test_repeated_sessions_leave_the_file_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            ini = Path(d) / 'config' / 'common.ini'
            ini.parent.mkdir()
            ini.write_bytes('[Common]\r\nLogin=1\r\n[Charts]\r\nMaxBars=100000\r\n'.encode('utf-16'))
            nb.set_runtime_maxbars(d, 6000)
            first = ini.read_bytes()
            for _ in range(5):
                nb.set_runtime_maxbars(d, 6000)
            self.assertEqual(ini.read_bytes(), first)
            self.assertNotIn(b'\r\x00\r\x00', first)
            self.assertEqual(first.decode('utf-16'), '[Common]\r\nLogin=1\r\n[Charts]\r\nMaxBars=6000\r\n')


if __name__ == '__main__':
    unittest.main()
