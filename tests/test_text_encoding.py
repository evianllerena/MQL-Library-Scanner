"""Regression tests for P0.1 / P0.4: one canonical decoder for MQL sources and logs.

Run:  python -m unittest discover -s tests -v      (from the repo root; no MT4/MT5 needed)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mql_text  # noqa: E402
import preview_bridge  # noqa: E402

FIX = ROOT / 'tests' / 'fixtures' / 'encoding'
UTF8 = FIX / 'sample_utf8.mq5'
UTF16 = FIX / 'sample_utf16le.mq5'
LOG16 = FIX / 'metaeditor_utf16le.log'


class DecoderTests(unittest.TestCase):
    def test_utf8_source_is_not_mojibake(self):
        text = mql_text.read_mql_text(UTF8)
        self.assertIn('#property indicator_separate_window', text)
        self.assertIn('Комментарий', text)

    def test_utf16le_bom_source(self):
        text = mql_text.read_mql_text(UTF16)
        self.assertIn('#property indicator_separate_window', text)
        self.assertIn('注释', text)
        self.assertNotIn('﻿', text)
        self.assertNotIn('\x00', text)

    def test_both_encodings_decode_identically(self):
        self.assertEqual(mql_text.read_mql_text(UTF8), mql_text.read_mql_text(UTF16))

    def test_bomless_utf16(self):
        raw = UTF8.read_bytes().decode('utf-8')
        self.assertEqual(mql_text.decode_mql_bytes(raw.encode('utf-16-le')), raw)
        self.assertEqual(mql_text.decode_mql_bytes(raw.encode('utf-16-be')), raw)

    def test_utf8_bom_and_legacy_codepages(self):
        self.assertEqual(mql_text.decode_mql_bytes(b'\xef\xbb\xbf#property strict'), '#property strict')
        self.assertEqual(mql_text.decode_mql_bytes('// café'.encode('cp1252')), '// café')
        self.assertEqual(mql_text.decode_mql_bytes(b'// \x81'), '// \x81')  # undefined in cp1252 -> latin1

    def test_log_reader(self):
        self.assertIn('Result: 0 errors', mql_text.read_log_text(LOG16))
        self.assertFalse(preview_bridge.compile_has_errors(mql_text.read_log_text(LOG16)))

    def test_missing_file(self):
        self.assertEqual(mql_text.read_mql_text(FIX / 'does-not-exist.mq5'), '')


class PreviewBridgeTests(unittest.TestCase):
    def test_separate_window_detected_for_both_encodings(self):
        for p in (UTF8, UTF16):
            with self.subTest(p=p.name):
                self.assertIn('indicator_separate_window', preview_bridge.read_text(p).lower())

    def test_same_source_ignores_crlf(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'ea.mq5'
            p.write_bytes(b'line1\r\nline2\r\n')
            self.assertTrue(preview_bridge._same_source(p, 'line1\nline2\n'))
            self.assertFalse(preview_bridge._same_source(p, 'line1\nlineX\n'))
            self.assertFalse(preview_bridge._same_source(Path(d) / 'missing.mq5', 'x'))

    def test_static_fast_fail_sees_utf8_source(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'mt4ish.mq5'
            p.write_text('double x=MarketInfo(Symbol(),MODE_BID);\n', encoding='utf-8')
            self.assertIsNotNone(preview_bridge._mt5_static_fast_fail(p))


if __name__ == '__main__':
    unittest.main()
