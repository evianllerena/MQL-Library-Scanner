"""P1.6: the pure scoring code must not drag in the MT5 preview bridge at import time."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ImportChainTest(unittest.TestCase):
    def test_batch_import_does_not_load_preview_bridge(self):
        code = ('import sys; sys.path.insert(0, r"%s"); import nnfx_backtest_batch, nnfx_selftest; '
                'print("preview_bridge" in sys.modules)' % ROOT)
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), 'False')


if __name__ == '__main__':
    unittest.main()
