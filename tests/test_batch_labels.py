"""rescore without the library DB still shows readable candidate names (P1.2 reextract flow)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nnfx_backtest_batch as nb  # noqa: E402


class JobLabel(unittest.TestCase):
    def test_name_from_job_file(self):
        job = {'key': 'a' * 64, 'rel': 'NNFXBacktestBatch\\' + 'a' * 64 + '\\ZZ_Ensign__0b06508a'}
        self.assertEqual(nb.job_label(job), 'ZZ_Ensign__0b06508a')

    def test_hash_prefix_without_rel(self):
        self.assertEqual(nb.job_label({'key': 'abcdef0123456789'}), 'abcdef012345')


if __name__ == '__main__':
    unittest.main()
