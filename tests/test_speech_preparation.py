"""Measured admission contracts; real PCM fixtures, no provider/model inference."""
import array
import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import wave

from core import DATA, DEFAULTS, ROOT, create_project
from processing_plan import content_requirements
from speech_preparation import cache_items, delivery_report, measured_duration, SpeechPreparationError
from subtitle_quality import coverage_record, export_plan
import worker


def signal(path, duration):
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        values = array.array('h', (int(8000 * math.sin(i * 2 * math.pi * 230 / 48000))
                                  for i in range(round(duration * 48000))))
        audio.writeframes(values.tobytes())


class SpeechPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=DATA, prefix='measured-admission-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.entry = {'id': 'vieneu:fixture-' + uuid.uuid4().hex, 'provider': 'vieneu',
                      'name': 'Fixture', 'sample_rate': 48000}
        self.voice = patch('voices.resolve_voice', return_value=self.entry)
        self.identity = patch('voices.audio_identity', return_value=['fixture-identity', self.entry['id']])
        self.voice.start(); self.identity.start()
        self.addCleanup(self.voice.stop); self.addCleanup(self.identity.stop)

    def project(self, count=2):
        return {'id': uuid.uuid4().hex, 'media': {'path': str(ROOT/'tests/fixtures/demo.mp4'),
                'duration': count * 2, 'width': 1280, 'height': 720, 'audio': True, 'fps': 30},
                'params': DEFAULTS | {'voice_id': self.entry['id'], 'voice_speed': 1,
                    'pipeline_extract': 'existing', 'pipeline_translate': False,
                    'pipeline_tts': True, 'pipeline_clean': False, 'use_clean': False, 'burn_subtitles': False},
                'segments': [{'start': i*2, 'end': i*2+.5, 'text': f'Nguồn {i}',
                              'translated': f'Xin chào bạn {i}.'} for i in range(count)], 'artifacts': {}}

    def seed(self, project, durations):
        items = cache_items(project)
        for item, seconds in zip(items, durations):
            item['path'].parent.mkdir(exist_ok=True)
            signal(item['path'], seconds)
        return cache_items(project)

    def test_report_enumerates_every_measured_failure(self):
        project = self.project(3)
        report = delivery_report(project, self.seed(project, [4.08, .2, 2]))
        self.assertEqual(report['constraint_count'], 2)
        self.assertEqual([i['cue'] for i in report['issues']], [1, 3])
        self.assertAlmostEqual(report['issues'][0]['required_tempo'], 8.16)
        self.assertEqual(report['unknown_count'], 0)

    def test_known_overrun_stops_before_inference_or_timeline_allocation(self):
        project = self.project(3)
        self.seed(project, [4.08, .2, 2])
        with patch('voices.synthesize_vieneu_batch') as infer, patch('numpy.memmap') as timeline, patch('worker.emit'):
            with self.assertRaises(SpeechPreparationError) as caught:
                worker.tts(project, {'id': uuid.uuid4().hex}, self.folder)
        infer.assert_not_called(); timeline.assert_not_called()
        self.assertEqual(caught.exception.report['constraint_count'], 2)
        saved = json.loads((self.folder/'speech-delivery-plan.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['status'], 'edit_required')
        self.assertFalse((self.folder/'voice.wav').exists())

    def test_preflight_uses_the_same_measured_contract(self):
        from preflight import inspect
        project = self.project(2)
        project['coverage'] = coverage_record(project, 'ocr', 0, project['media']['duration'])
        self.seed(project, [4.08, 2])
        with patch('credentials.active_secret', side_effect=AssertionError('No provider required')):
            check = inspect(project, 'pipeline')
        self.assertFalse(check['ok'])
        self.assertEqual(check['delivery']['constraint_count'], 2)
        self.assertIn('8.16', check['errors'][0])

    def test_new_batch_is_bounded_and_stops_before_second_group(self):
        project = self.project(12)
        def synthesize(requests, **kwargs):
            for request in requests:
                signal(request['path'], 2)
        with patch('voices.synthesize_vieneu_batch', side_effect=synthesize) as infer, patch('worker.emit'):
            with self.assertRaises(SpeechPreparationError):
                worker.tts(project, {'id': uuid.uuid4().hex}, self.folder)
        self.assertEqual(infer.call_count, 1)
        self.assertEqual(len(infer.call_args.args[0]), 8)
        self.assertEqual(sum(item['cache_hit'] for item in cache_items(project)), 8)
        self.assertFalse((self.folder/'voice.wav').exists())

    def test_unknown_duration_is_not_reported_as_verified_ready(self):
        project = self.project()
        report = delivery_report(project, cache_items(project))
        self.assertEqual(report['status'], 'needs_measurement')
        self.assertEqual(report['unknown_count'], 2)
        self.assertEqual(report['constraint_count'], 0)

    def test_auto_schedule_waits_for_all_measurements_without_claiming_ready(self):
        project = self.project(3)
        project['params']['tts_auto_schedule'] = True
        report = delivery_report(project, self.seed(project, [2.]))
        self.assertEqual(report['status'], 'needs_measurement')
        self.assertEqual(report['unknown_count'], 2)
        self.assertEqual(report['issues'], [])
        self.assertNotIn('placements', report)

    def test_auto_schedule_measures_second_batch_and_keeps_all_audio(self):
        project = self.project(12)
        project['params']['tts_auto_schedule'] = True
        before = copy.deepcopy(project)
        def synthesize(requests, **kwargs):
            for request in requests:
                signal(request['path'], 1.)
        with patch('voices.synthesize_vieneu_batch', side_effect=synthesize) as infer, patch('worker.emit'):
            worker.tts(project, {'id': uuid.uuid4().hex}, self.folder)
        self.assertEqual(infer.call_count, 2)
        self.assertEqual(project, before)
        report = json.loads((self.folder/'speech-delivery-plan.json').read_text(encoding='utf-8'))
        self.assertEqual(report['measured_count'], 12)
        self.assertEqual(report['unknown_count'], 0)
        self.assertEqual(report['status'], 'measured_ready')
        self.assertTrue((self.folder/'voice.wav').exists())
        placements = report['placements']
        self.assertTrue(all(a['end'] <= b['start'] for a,b in zip(placements, placements[1:])))

    def test_auto_schedule_fails_only_after_full_measurement_when_video_cannot_fit(self):
        project = self.project(3)
        project['params']['tts_auto_schedule'] = True
        report = delivery_report(project, self.seed(project, [4., 4., 4.]))
        self.assertEqual(report['unknown_count'], 0)
        self.assertEqual(report['status'], 'edit_required')
        self.assertIn('chưa cắt lời', report['issues'][0]['message'])

    def test_auto_worker_reserves_actual_atempo_margin_without_resynthesis(self):
        project = self.project(1)
        project['media']['duration'] = 1.405
        project['params']['tts_auto_schedule'] = True
        self.seed(project, [1.68])
        with patch('voices.synthesize_vieneu_batch', side_effect=AssertionError('Must reuse measured audio')), patch('worker.emit'):
            worker.tts(project, {'id': uuid.uuid4().hex}, self.folder)
        with wave.open(str(self.folder/'voice.wav'), 'rb') as audio:
            self.assertAlmostEqual(audio.getnframes()/audio.getframerate(), 1.405, places=4)

    def test_partial_constraint_message_does_not_claim_full_video_checked(self):
        project = self.project(3)
        items = self.seed(project, [2.])
        report = delivery_report(project, items)
        message = str(SpeechPreparationError(report))
        self.assertIn('Mới đo 1/3 câu', message)
        self.assertIn('còn 2 câu chưa đo', message)

    def test_corrupt_payload_invalidates_cached_measurement(self):
        items = self.seed(self.project(1), [.2])
        path = items[0]['path']
        self.assertAlmostEqual(measured_duration(path), .2)
        path.write_bytes(path.read_bytes()[:100])
        self.assertIsNone(measured_duration(path))

    def test_retiming_reuses_raw_identity_and_revalidates(self):
        project = self.project(1)
        before = self.seed(project, [2])[0]
        project['segments'][0]['end'] = 2
        after = cache_items(project)[0]
        self.assertEqual(before['key'], after['key'])
        self.assertEqual(delivery_report(project, [after])['status'], 'measured_ready')

    def test_full_translation_does_not_require_provider_just_to_reuse(self):
        project = self.project()
        project['params']['pipeline_translate'] = True
        self.assertFalse(content_requirements(project)['will_translate'])
        project['segments'][0]['translated'] = ''
        self.assertTrue(content_requirements(project)['will_translate'])

    def test_source_choice_agrees_between_tts_and_export(self):
        from preflight import inspect
        project = create_project(str(ROOT/'tests/fixtures/demo.mp4'), 'Source parity fixture')
        project['params'].update(self.project()['params'], tts_text_source='source')
        project['segments'] = [{'start': 0, 'end': 2, 'text': 'Xin chào.', 'translated': ''}]
        project['coverage'] = coverage_record(project, 'asr', 0, project['media']['duration'])
        self.assertTrue(inspect(project, 'tts')['ok'])
        self.assertEqual(export_plan(project)['subtitles']['errors'], [])
        project['params']['tts_text_source'] = 'translated'
        self.assertFalse(inspect(project, 'tts')['ok'])
        self.assertTrue(export_plan(project)['subtitles']['errors'])

    def test_uncertain_ocr_selection_requires_review_without_dropping_words(self):
        from subtitle_quality import inspect_subtitles
        project = self.project(1)
        project['coverage'] = coverage_record(project, 'ocr', 0, project['media']['duration'],
            ocr_selection={'requires_review': True, 'issues': [{'message': 'Hai lớp chữ cần chọn.'}]})
        before = copy.deepcopy(project['segments'])
        quality = inspect_subtitles(project)
        self.assertTrue(quality['requires_review'])
        self.assertEqual(project['segments'], before)
