"""Re-analyzing an existing library must not mark every file as a duplicate of itself.

scan() preloads the first path per sha256 from the DB; a re-analyzed file (forced, or after a
classifier_version bump) found its own row and got duplicate_of = its own path, so nnfx-map and
the backtest saw zero candidates.
"""
import contextlib
import io
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import engine  # noqa: E402

SRC = '#property indicator_separate_window\n#property indicator_type1 DRAW_LINE\nint OnInit(){return(0);}\n'


class RescanDuplicates(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / 'src'; self.src.mkdir()
        (self.src / 'a.mq5').write_text(SRC, encoding='utf-8')
        (self.src / 'b_copy.mq5').write_text(SRC, encoding='utf-8')          # byte-identical to a.mq5
        (self.src / 'c.mq5').write_text(SRC + '// other\n', encoding='utf-8')
        self.db = str(self.tmp / 'lib.sqlite3')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _scan(self, force):
        with contextlib.redirect_stdout(io.StringIO()):
            engine.scan(Path(self.db), [self.src], force=force)

    def _dups(self):
        c = sqlite3.connect(self.db)
        try:
            return {Path(p).name: (Path(d).name if d else None)
                    for p, d in c.execute('SELECT path, duplicate_of FROM indicators')}
        finally:
            c.close()

    def test_forced_rescan_keeps_originals_and_real_duplicates(self):
        self._scan(False)
        first = self._dups()
        self.assertEqual(sum(v is None for v in first.values()), 2)    # a/b pair counts once, plus c
        self._scan(True)
        again = self._dups()
        self.assertEqual(again, first)
        for name, dup in again.items():
            self.assertNotEqual(name, dup, f'{name} marked a duplicate of itself')


if __name__ == '__main__':
    unittest.main()
