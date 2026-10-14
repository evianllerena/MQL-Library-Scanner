"""P2 classifier quality (engine_core): volume / overbought-oversold evidence and comment stripping."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine_core  # noqa: E402

ONCALC = ('int OnCalculate(const int rates_total,const int prev_calculated,const datetime &time[],'
          'const double &open[],const double &high[],const double &low[],const double &close[],'
          'const long &tick_volume[],const long &volume[],const int &spread[])\n{ return rates_total; }\n')


class VolumeEvidence(unittest.TestCase):
    def test_oncalculate_signature_is_not_volume_use(self):
        self.assertFalse(engine_core.uses_volume(ONCALC))

    def test_indexed_volume_reads_are(self):
        self.assertTrue(engine_core.uses_volume(ONCALC + 'double v=(double)tick_volume[i];'))
        self.assertTrue(engine_core.uses_volume('double v=Volume[i+1];'))           # MQL4 series
        self.assertTrue(engine_core.uses_volume('long v=iVolume(NULL,0,i);'))


class OverboughtOversold(unittest.TestCase):
    def test_unrelated_numbers_are_not_levels(self):
        self.assertFalse(engine_core.has_ob_os_levels('int period=80;\nint shift=20;\n'))

    def test_level_contexts_are(self):
        self.assertTrue(engine_core.has_ob_os_levels('#property indicator_level1 70\n#property indicator_level2 30\n'))
        self.assertTrue(engine_core.has_ob_os_levels('IndicatorSetDouble(INDICATOR_LEVELVALUE,0,80);'))
        self.assertTrue(engine_core.has_ob_os_levels('if(rsi>Overbought) x=1;'))


class StripComments(unittest.TestCase):
    def test_url_in_string_survives(self):
        src = '#property link "https://www.mql5.com"\nint a=1; // note\n/* block */int b=2;\n'
        out = engine_core.strip_comments(src)
        self.assertIn('"https://www.mql5.com"', out)
        self.assertIn('int a=1;', out)
        self.assertNotIn('note', out)
        self.assertNotIn('block', out)
        self.assertIn('int b=2;', out)

    def test_comment_markers_inside_strings_are_kept(self):
        out = engine_core.strip_comments('string s="a /* not a comment */ b"; // gone\n')
        self.assertIn('"a /* not a comment */ b"', out)
        self.assertNotIn('gone', out)


if __name__ == '__main__':
    unittest.main()
