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

    def test_mqlrates_fields_applied_volume_and_volume_handles_are(self):
        self.assertTrue(engine_core.uses_volume('double v=(double)rates[i].tick_volume;'))
        self.assertTrue(engine_core.uses_volume('long v=r.real_volume;'))
        self.assertTrue(engine_core.uses_volume('input ENUM_APPLIED_VOLUME InpVol=VOLUME_TICK;'))
        self.assertTrue(engine_core.uses_volume('h=iForce(_Symbol,_Period,13,MODE_SMA,PRICE_CLOSE);'))
        self.assertTrue(engine_core.uses_volume('h=iBWMFI(_Symbol,_Period,0);'))


class OverboughtOversold(unittest.TestCase):
    def test_unrelated_numbers_are_not_levels(self):
        self.assertFalse(engine_core.has_ob_os_levels('int period=80;\nint shift=20;\n'))

    def test_level_contexts_are(self):
        self.assertTrue(engine_core.has_ob_os_levels('#property indicator_level1 70\n#property indicator_level2 30\n'))
        self.assertTrue(engine_core.has_ob_os_levels('IndicatorSetDouble(INDICATOR_LEVELVALUE,0,80);\n'
                                                     'IndicatorSetDouble(INDICATOR_LEVELVALUE,1,20);'))
        self.assertTrue(engine_core.has_ob_os_levels('if(rsi>Overbought) x=1;'))

    def test_a_single_level_is_not(self):
        # a lone 20 is usually an ADX / trend-strength line
        self.assertFalse(engine_core.has_ob_os_levels('#property indicator_level1 20\n'))
        self.assertFalse(engine_core.has_ob_os_levels('#property indicator_level1 80\n'))

    def test_words_inside_identifiers_are(self):
        self.assertTrue(engine_core.has_ob_os_levels('input double Overbought_Level=75;'))
        self.assertTrue(engine_core.has_ob_os_levels('input double OverboughtLevel=75;'))
        self.assertTrue(engine_core.has_ob_os_levels('input double InpOversold=25;'))


class ClassifierVersion(unittest.TestCase):
    def test_bumped_so_existing_libraries_are_reanalyzed(self):
        # engine.unchanged() skips files whose stored classifier_version matches
        self.assertEqual(engine_core.Analysis.classifier_version, 'evidence-v6')


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


class BuiltinCalls(unittest.TestCase):
    def test_prefilter_keeps_exact_semantics(self):
        import tempfile
        code = ('double iRSI(int p){ return 0; }\n'            # a DEFINITION named like a built-in: not a call
                'int OnInit(){ int h=iMA(NULL,0,14,0,MODE_SMA,PRICE_CLOSE); double c=MQL4_iCCI(NULL,0,14,0,0);'
                ' double r=iRSI(5); return 0; }\n')
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'x.mq5'
            f.write_text(code, encoding='utf-8')
            stds = engine_core.analyze(f).standard_indicators
        self.assertIn('iMA', stds)
        self.assertIn('MQL4_iCCI', stds)
        self.assertIn('iRSI', stds)            # called once besides its own definition
        self.assertNotIn('iStochastic', stds)


if __name__ == '__main__':
    unittest.main()
