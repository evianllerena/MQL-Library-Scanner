"""P2 review LOW items (source checks: no Rust/Node toolchain runs in the unit tests)."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _first(*rel):
    for r in rel:
        p = ROOT / r
        if p.exists():
            return p.read_text(encoding='utf-8')
    raise FileNotFoundError(rel)


class P2ReviewLow(unittest.TestCase):
    def test_insufficient_sample_grouping_uses_in_sample_trades_in_every_sort(self):
        src = _first('src-tauri/src/lib.rs', 'lib.rs')
        self.assertNotIn('CASE WHEN bt.trades>=?', src)
        self.assertEqual(src.count('CASE WHEN COALESCE(bt.is_trades,bt.trades)>=?'), 2)

    def test_asset_scope_exposes_only_selftest_images(self):
        conf = json.loads(_first('src-tauri/tauri.conf.json', 'tauri.conf.json'))
        scope = conf['app']['security']['assetProtocol']['scope']
        self.assertIn('$APPDATA/selftest/*.png', scope)
        self.assertNotIn('$APPDATA/selftest/*', scope)

    def test_backtest_header_click_cycles_back_to_rank(self):
        js = _first('ui/main.js', 'main.js')
        body = re.search(r'function changeBacktestSort\(slot,key\)\{.*?loadBacktestBoard', js, re.S).group(0)
        self.assertIn("b.sortBy='rank'", body)

    def test_dead_preview_timer_and_job_id_removed(self):
        js = _first('ui/preview_ui.js', 'preview_ui.js')
        self.assertNotIn('previewTimer', js)
        self.assertNotIn('function newJobId', js)


if __name__ == '__main__':
    unittest.main()
