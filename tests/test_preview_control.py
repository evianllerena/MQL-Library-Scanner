"""P2: clear-app may only ever empty the app's own data folder."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import preview_control as pc  # noqa: E402


class ClearAppGuard(unittest.TestCase):
    def test_refuses_a_folder_that_is_not_the_app_data_folder(self):
        with tempfile.TemporaryDirectory() as d:
            keep = Path(d) / 'my_indicators.mq5'
            keep.write_text('source')
            with self.assertRaises(RuntimeError):
                pc.clear_app(str(Path(d) / 'library.sqlite3'))
            self.assertTrue(keep.exists())

    def test_refuses_another_file_name_in_the_app_folder(self):
        with tempfile.TemporaryDirectory() as d:
            app = Path(d) / pc.APP_ID
            app.mkdir()
            (app / 'keep.txt').write_text('x')
            with self.assertRaises(RuntimeError):
                pc.clear_app(str(app / 'other.db'))
            self.assertTrue((app / 'keep.txt').exists())

    def test_clears_the_real_app_data_folder_only(self):
        with tempfile.TemporaryDirectory() as d:
            app = Path(d) / pc.APP_ID
            (app / 'diagnostics').mkdir(parents=True)
            (app / 'library.sqlite3').write_text('')
            (app / 'diagnostics' / 'x.log').write_text('log')
            outside = Path(d) / 'source.mq5'
            outside.write_text('source')
            with mock.patch.object(pc, 'emit'):
                pc.clear_app(str(app / 'library.sqlite3'))
            self.assertEqual(list(app.iterdir()), [])
            self.assertTrue(outside.exists())


if __name__ == '__main__':
    unittest.main()
