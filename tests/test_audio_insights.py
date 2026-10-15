import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np
from core import DEFAULTS
from audio_insights import analyze_audio, insights_signature


class AudioInsightsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root/'source.wav'
        rate = 16000
        data = np.zeros(2*rate,dtype=np.int16)
        data[int(.4*rate):int(1.2*rate)] = (np.sin(np.arange(int(.8*rate))*2*np.pi*220/rate)*15000).astype(np.int16)
        with wave.open(str(self.source),'wb') as wav:
            wav.setparams((1,2,rate,0,'NONE','not compressed'))
            wav.writeframes(data.tobytes())
        self.project = {'params': DEFAULTS | {'workload_mode':'fast'},
                        'media':{'path':str(self.source),'audio':True,'duration':2},
                        'segments':[{'start':0,'end':2,'text':'Xin chào bạn hôm nay'}]}
        self.output = self.root/'analysis'
        self.output.mkdir()

    def test_real_ffmpeg_detects_pauses_and_energy_without_emotion_claim(self):
        result = analyze_audio(self.project, {}, self.output)
        report = result['insights']
        prosody = report['cues'][0]['source_prosody']
        self.assertAlmostEqual(prosody['pause_before_ms'],400,delta=25)
        self.assertAlmostEqual(prosody['pause_after_ms'],800,delta=25)
        self.assertAlmostEqual(prosody['silence_ratio'],.6,delta=.025)
        self.assertGreater(prosody['peak_dbfs'],-10)
        self.assertEqual(prosody['confidence'],'heuristic')
        self.assertNotIn('emotion',prosody)
        self.assertTrue(.8 <= prosody['suggested_rate'] <= 1.2)
        self.assertLessEqual(len(report['waveform']),1200)
        self.assertTrue((self.output/'audio-insights.json').is_file())

    def test_silent_cue_has_no_rate_suggestion(self):
        self.project['segments'] = [{'start':1.3,'end':1.9,'text':'Không có âm thật'}]
        report = analyze_audio(self.project, {}, self.output)['insights']
        prosody = report['cues'][0]['source_prosody']
        self.assertEqual(prosody['silence_ratio'],1)
        self.assertIsNone(prosody['suggested_rate'])

    def test_overlapping_cues_measure_independently(self):
        self.project['segments'] = [{'start':0,'end':.8,'text':'Câu một'},
                                    {'start':.4,'end':1.2,'text':'Câu hai'}]
        report = analyze_audio(self.project, {}, self.output)['insights']
        first, second = [r['source_prosody'] for r in report['cues']]
        self.assertAlmostEqual(first['silence_ratio'],.5,delta=.03)
        self.assertLess(second['silence_ratio'],.03)

    def test_same_signature_reuses_report_without_decoding(self):
        analyze_audio(self.project, {}, self.output)
        with patch('audio_insights.subprocess.Popen') as start:
            result = analyze_audio(self.project, {}, self.output)
        start.assert_not_called()
        self.assertTrue(result['reused'])

    def test_source_text_and_timing_change_invalidate_report(self):
        original = insights_signature(self.project)
        changed = copy.deepcopy(self.project)
        changed['segments'][0]['end'] = 1.5
        self.assertNotEqual(insights_signature(changed),original)
        changed = copy.deepcopy(self.project)
        changed['segments'][0]['text'] = 'Lời khác'
        self.assertNotEqual(insights_signature(changed),original)

    def test_invalid_or_truncated_input_is_not_published(self):
        self.project['media']['duration'] = 3
        with self.assertRaises(ValueError):
            analyze_audio(self.project, {}, self.output)
        self.assertFalse((self.output/'audio-insights.json').exists())

    def test_no_audio_and_out_of_range_cue_rejected(self):
        self.project['media']['audio'] = False
        with self.assertRaises(ValueError):
            analyze_audio(self.project, {}, self.output)
        self.project['media']['audio'] = True
        self.project['segments'][0]['end'] = 9
        with self.assertRaises(ValueError):
            analyze_audio(self.project, {}, self.output)

    def test_invalid_cache_is_replaced_by_valid_report(self):
        (self.output/'audio-insights.json').write_text('[]',encoding='utf-8')
        result = analyze_audio(self.project, {}, self.output)
        self.assertEqual(result['insights']['schema_version'],1)
        self.assertIsInstance(json.loads((self.output/'audio-insights.json').read_text(encoding='utf-8')),dict)

    def test_no_cues_still_produces_waveform(self):
        self.project['segments'] = []
        report = analyze_audio(self.project, {}, self.output)['insights']
        self.assertTrue(report['waveform'])
        self.assertEqual(report['cues'],[])


if __name__ == '__main__':
    unittest.main()
