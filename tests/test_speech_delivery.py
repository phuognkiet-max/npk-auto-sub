"""Delivery regressions: a dubbing edit must not alter the displayed subtitle.

Run through tests/run_npk_tests.py (or an explicitly isolated data directory).
The synthetic Piper below writes a short, deterministic tone; no model or API
is used. The final alignment and timeline are exercised with bundled FFmpeg.
"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid
import wave
from unittest.mock import patch

import numpy as np

from core import DATA, DEFAULTS, ROOT, create_project, get, put, save_srt
import server
import worker
from project_transfer import export_document, restore_document
from speech_delivery import delivery_window, fit_rate, speech_rate, spoken_text, timing_mode


def cue(**changes):
    return dict(start=0, end=2, text='Original subtitle', translated='Displayed translation') | changes


class SpeechDeliverySchemaTests(unittest.TestCase):
    def setUp(self):
        # Schema and preflight checks need a ready voice identity, never real
        # Piper weights. Keep resolve_voice's normal validation and stat-based
        # cache identity while isolating the catalog to temporary test files.
        self.temp = tempfile.TemporaryDirectory(dir=DATA, prefix='speech-schema-')
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        model, config = folder / 'schema.onnx', folder / 'schema.onnx.json'
        model.write_bytes(b'synthetic-schema-identity-only-not-an-onnx-model')
        config.write_text('{}', encoding='utf-8')
        entry = dict(id='piper:vais1000', name='Synthetic schema voice',
                     model=model, config=config, speaker_id=None)
        catalog = patch('voices.entries', return_value=[entry])
        catalog.start()
        self.addCleanup(catalog.stop)
        no_inference = patch('voices.load_voice', side_effect=AssertionError(
            'Schema tests must not load a voice model.'))
        no_inference.start()
        self.addCleanup(no_inference.stop)

    def test_new_projects_use_natural_but_old_stored_projects_keep_legacy(self):
        self.assertEqual(DEFAULTS['tts_timing_mode'], 'natural')
        with patch('core.settings', return_value=DEFAULTS.copy()):
            project = create_project(str(ROOT / 'tests/fixtures/demo.mp4'), 'Delivery compatibility')
        self.assertEqual(project['params']['tts_timing_mode'], 'natural')
        project['params'].pop('tts_timing_mode')
        put('project', project['id'], project)
        self.assertEqual(server.project_by_id(project['id'])['params']['tts_timing_mode'], 'legacy')
        # Merely opening an old project must not rewrite its saved parameters.
        self.assertNotIn('tts_timing_mode', get('project', project['id'])['params'])
        self.assertEqual(timing_mode({}), 'legacy')

    def test_timing_mode_is_a_validated_choice(self):
        for mode in ('natural', 'legacy'):
            self.assertEqual(server.validate_params({'tts_timing_mode': mode})['tts_timing_mode'], mode)
        with self.assertRaises(ValueError):
            server.validate_params({'tts_timing_mode': 'silently-cut'})

    def test_delivery_fields_survive_save_and_portable_project_roundtrip(self):
        row = cue(spoken_text='A concise spoken version.', speech_rate=.85,
                  pause_before_ms=250, pause_after_ms=500)
        validated = server.validate_segments([row], 8)
        self.assertEqual(validated[0], row)
        project = create_project(str(ROOT / 'tests/fixtures/demo.mp4'), 'Delivery roundtrip')
        project.update(segments=validated)
        project['params']['tts_timing_mode'] = 'natural'
        restored = restore_document(export_document(project), project['media']['path'])
        self.assertEqual(restored['segments'], validated)
        self.assertEqual(restored['params']['tts_timing_mode'], 'natural')

    def test_invalid_delivery_numbers_and_exhausted_pause_budget_are_rejected(self):
        bad = [('speech_rate', .49), ('speech_rate', 2.01), ('speech_rate', float('nan')),
               ('speech_rate', float('inf')), ('pause_before_ms', -1),
               ('pause_before_ms', 2001), ('pause_after_ms', float('nan')),
               ('pause_after_ms', 2001)]
        for field, value in bad:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                server.validate_segments([cue(**{field: value})], 8)
        with self.assertRaises(ValueError):
            server.validate_segments([cue(pause_before_ms=1000, pause_after_ms=1000)], 8)

    def test_invalid_spoken_script_and_pause_types_are_rejected(self):
        for changes in (dict(spoken_text=['not text']), dict(spoken_text='x' * 10001),
                        dict(speech_rate=True), dict(pause_before_ms=True),
                        dict(pause_after_ms=1.5)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                server.validate_segments([cue(**changes)], 8)

    def test_old_portable_document_keeps_legacy_timing(self):
        project = create_project(str(ROOT / 'tests/fixtures/demo.mp4'), 'Old portable document')
        project['segments'] = [cue()]
        document = export_document(project)
        document['params'].pop('tts_timing_mode')
        restored = restore_document(document, project['media']['path'])
        self.assertEqual(restored['params']['tts_timing_mode'], 'legacy')

    def test_explicit_spoken_script_satisfies_tts_preflight(self):
        from preflight import inspect
        project = create_project(str(ROOT / 'tests/fixtures/demo.mp4'), 'Spoken-only completion')
        project['params']['tts_text_source'] = 'edited'
        project['segments'] = [cue(translated='', spoken_text='Xin chào bạn.')]
        check = inspect(project, 'tts')
        self.assertTrue(check['ok'], check['errors'])
        project['segments'][0].pop('spoken_text')
        self.assertFalse(inspect(project, 'tts')['ok'])

    def test_numeric_limits_allow_slow_fast_and_pause_endpoints(self):
        rows = [cue(end=6, speech_rate=.5, pause_before_ms=2000, pause_after_ms=2000),
                cue(end=6, speech_rate=2, pause_before_ms=0, pause_after_ms=0)]
        clean = server.validate_segments(rows, 8)
        self.assertEqual(clean[0]['speech_rate'], .5)
        self.assertEqual(clean[0]['pause_before_ms'], 2000)
        self.assertEqual(clean[1]['speech_rate'], 2)

    def test_spoken_copy_is_separate_from_the_subtitle_file(self):
        import pysubs2
        row = cue(spoken_text='Different narration for listening.')
        with tempfile.TemporaryDirectory(dir=DATA, prefix='speech-subtitle-') as temp:
            path = Path(temp) / 'display.srt'
            save_srt([row], path)
            subtitles = pysubs2.load(str(path))
        self.assertEqual(subtitles.events[0].text, row['translated'])
        self.assertNotIn(row['spoken_text'], subtitles.events[0].text)


class SpeechDeliveryPolicyTests(unittest.TestCase):
    def test_blank_spoken_copy_falls_back_to_translation_then_source(self):
        self.assertEqual(spoken_text(cue(spoken_text='  New spoken line  ')), 'New spoken line')
        self.assertEqual(spoken_text(cue(spoken_text=' \n ')), 'Displayed translation')
        self.assertEqual(spoken_text(cue(spoken_text='', translated=' ')), 'Original subtitle')

    def test_cue_rate_is_an_absolute_override_not_a_multiplier(self):
        self.assertEqual(speech_rate(cue(speech_rate=.8), {'voice_speed': 1.5}), .8)
        self.assertEqual(speech_rate(cue(), {'voice_speed': 1.5}), 1.5)

    def test_reserved_pauses_stay_inside_the_cue_in_both_modes(self):
        for mode in ('natural', 'legacy'):
            with self.subTest(mode=mode):
                window = delivery_window(cue(start=2, end=4, pause_before_ms=250,
                                             pause_after_ms=500),
                                         {'tts_timing_mode': mode, 'voice_speed': 1}, 1, 8)
                self.assertAlmostEqual(window['start'], 2.25)
                self.assertAlmostEqual(window['end'], 3.5)
                self.assertAlmostEqual(window['available'], 1.25)

    def test_natural_fit_preserves_short_speech_and_caps_extra_compression(self):
        self.assertEqual(fit_rate(.5, 1, 'natural', 1), 1)
        self.assertAlmostEqual(fit_rate(1.1, 1, 'natural', 1), 1.1)
        self.assertAlmostEqual(fit_rate(1.15, 1, 'natural', 1), 1.15)
        with self.assertRaises(ValueError):
            fit_rate(1.151, 1, 'natural', 1)

    def test_legacy_fit_keeps_previous_three_times_limit(self):
        self.assertEqual(fit_rate(2, 1, 'legacy', 1), 2)
        self.assertEqual(fit_rate(3, 1, 'legacy', 1), 3)
        with self.assertRaises(ValueError):
            fit_rate(3.01, 1, 'legacy', 1)

    def test_full_translation_overflow_explains_timing_without_shortening_content(self):
        with self.assertRaisesRegex(ValueError, 'Giữ đủ nội dung') as error:
            fit_rate(2, 1, 'natural', 42, preserve_text=True)
        self.assertNotIn('Rút gọn', str(error.exception))

    def test_old_voice_artifact_signature_remains_exactly_compatible(self):
        project = {'media': {'duration': 8}, 'segments': [cue()],
                   'params': {'voice_speed': 1, 'voice_id': 'piper:vais1000'}}
        rows = [[r['start'], r['end'], r.get('translated') or r.get('text', ''), r.get('voice_id')]
                for r in project['segments']]
        payload = ['npk-voice-v3', 8, rows, 1, 'piper:vais1000']
        expected = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        self.assertEqual(worker.speech_signature(project), expected)
        project['params']['tts_timing_mode'] = 'legacy'
        self.assertEqual(worker.speech_signature(project), expected)

    def test_changed_delivery_invalidates_voice_without_changing_subtitles(self):
        project = {'media': {'duration': 8}, 'segments': [cue()],
                   'params': DEFAULTS | {'tts_timing_mode': 'natural', 'tts_text_source': 'edited'}}
        baseline = worker.speech_signature(project)
        changes = [dict(spoken_text='Shorter spoken copy'), dict(speech_rate=.8),
                   dict(pause_before_ms=200), dict(pause_after_ms=200)]
        for change in changes:
            with self.subTest(change=change):
                edited = copy.deepcopy(project)
                edited['segments'][0].update(change)
                self.assertNotEqual(worker.speech_signature(edited), baseline)
                self.assertEqual(edited['segments'][0]['translated'], project['segments'][0]['translated'])
        edited = copy.deepcopy(project)
        edited['params']['tts_timing_mode'] = 'legacy'
        self.assertNotEqual(worker.speech_signature(edited), baseline)


class SyntheticPiper:
    def __init__(self, duration):
        self.duration = duration
        self.calls = []

    def synthesize_wav(self, text, output, syn_config):
        self.calls.append((text, syn_config.length_scale))
        count = round(self.duration * syn_config.length_scale * 22050)
        # Non-silent synthetic signal with energy through the end of the clip.
        samples = (np.sin(np.arange(count) * 2 * np.pi * 220 / 22050) * 10000).astype('<i2')
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(22050)
        output.writeframes(samples.tobytes())


class SpeechDeliveryTimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=DATA, prefix='speech-delivery-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        model, config = self.folder / 'test.onnx', self.folder / 'test.onnx.json'
        model.write_bytes(b'synthetic-model-identity-only')
        config.write_text('{}', encoding='utf-8')
        self.entry = dict(id='synthetic:' + uuid.uuid4().hex, name='Synthetic test voice',
                          model=model, config=config, speaker_id=None)

    def project(self, rows, **params):
        return {'media': {'duration': 4}, 'segments': rows,
                'params': DEFAULTS | {'tts_timing_mode': 'natural', 'tts_text_source': 'edited', 'voice_id': self.entry['id']} | params}

    def run_tts(self, project, voice, name):
        folder = self.folder / name
        folder.mkdir()
        with patch('voices.resolve_voice', return_value=self.entry), \
             patch('voices.load_voice', return_value=(voice, self.entry)), patch('worker.emit'):
            result = worker.tts(project, {}, folder)
        with wave.open(str(folder / 'voice.wav'), 'rb') as audio:
            rate = audio.getframerate()
            samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype='<i2').copy()
        return result, rate, samples

    def test_spoken_override_rate_and_reserved_silence_reach_the_output(self):
        voice = SyntheticPiper(.8)
        row = cue(spoken_text='Short spoken copy', speech_rate=.8,
                  pause_before_ms=250, pause_after_ms=250)
        result, rate, samples = self.run_tts(self.project([row], voice_speed=1.5), voice, 'first')
        self.assertEqual(voice.calls, [('Short spoken copy', 1.25)])
        self.assertEqual(len(samples), 4 * rate)
        voiced = np.flatnonzero(np.abs(samples) > 100)
        self.assertAlmostEqual(voiced[0] / rate, .25, delta=.02)
        self.assertAlmostEqual((voiced[-1] + 1) / rate, 1.25, delta=.025)
        self.assertFalse(np.any(samples[:round(.24 * rate)]))
        self.assertFalse(np.any(samples[round(1.75 * rate):]))
        self.assertEqual(result['cache_hits'], 0)

    def test_over_budget_fails_with_an_action_and_keeps_speech_for_retiming(self):
        voice = SyntheticPiper(1.8)
        row = cue(end=2, pause_before_ms=250, pause_after_ms=250)
        project = self.project([row])
        failed = self.folder / 'too-short'
        failed.mkdir()
        before = set((DATA / 'speech-cache').glob('*.wav'))
        with patch('voices.resolve_voice', return_value=self.entry), \
             patch('voices.load_voice', return_value=(voice, self.entry)), patch('worker.emit'):
            with self.assertRaises(ValueError) as caught:
                worker.tts(project, {}, failed)
        message = str(caught.exception).casefold()
        self.assertTrue(any(word in message for word in ('rút gọn', 'kéo dài', 'tăng thời gian', 'spoken_text')), message)
        self.assertFalse((failed / 'voice.wav').exists())
        self.assertEqual(len(set((DATA / 'speech-cache').glob('*.wav')) - before), 1)
        # Widening the cue reuses the already-created raw speech, rather than
        # regenerating it or silently cutting the overflowing tail.
        project['segments'][0]['end'] = 3
        result, rate, samples = self.run_tts(project, voice, 'retimed')
        self.assertEqual(len(voice.calls), 1)
        self.assertEqual(result['cache_hits'], 1)
        voiced = np.flatnonzero(np.abs(samples) > 100)
        self.assertAlmostEqual((voiced[-1] + 1) / rate, 2.05, delta=.025)

    def test_same_delivery_reuses_cache_but_spoken_text_and_rate_regenerate(self):
        voice = SyntheticPiper(.6)
        project = self.project([cue(spoken_text='First narration')])
        self.run_tts(project, voice, 'initial')
        result, _, _ = self.run_tts(project, voice, 'repeated')
        self.assertEqual(result['cache_hits'], 1)
        self.assertEqual(len(voice.calls), 1)
        project['segments'][0]['spoken_text'] = 'Revised narration'
        result, _, _ = self.run_tts(project, voice, 'new-text')
        self.assertEqual(result['cache_hits'], 0)
        project['segments'][0]['speech_rate'] = 1.1
        result, _, _ = self.run_tts(project, voice, 'new-rate')
        self.assertEqual(result['cache_hits'], 0)
        self.assertEqual(len(voice.calls), 3)

    def test_missing_mode_still_renders_a_legacy_two_times_fit(self):
        voice = SyntheticPiper(1.8)
        project = self.project([cue(end=1)])
        project['params'].pop('tts_timing_mode')
        result, rate, samples = self.run_tts(project, voice, 'legacy')
        self.assertEqual(result['cache_hits'], 0)
        self.assertTrue(np.any(np.abs(samples[:rate]) > 100))
        self.assertFalse(np.any(samples[rate:]))

    def test_overlapping_cue_windows_are_allowed_when_actual_speech_does_not_overlap(self):
        voice = SyntheticPiper(.6)
        project = self.project([cue(end=2), cue(start=1.5, end=3.5, spoken_text='Second narration')])
        _, rate, samples = self.run_tts(project, voice, 'nonoverlap')
        self.assertTrue(np.any(np.abs(samples[:round(.6 * rate)]) > 100))
        self.assertFalse(np.any(samples[round(.7 * rate):round(1.4 * rate)]))
        self.assertTrue(np.any(np.abs(samples[round(1.5 * rate):round(2.1 * rate)]) > 100))

    def test_actual_speech_overlap_requires_editing_instead_of_mixing_voices(self):
        voice = SyntheticPiper(1)
        project = self.project([cue(end=2), cue(start=.5, end=2.5, spoken_text='Second narration')])
        folder = self.folder / 'overlap'
        folder.mkdir()
        with patch('voices.resolve_voice', return_value=self.entry), \
             patch('voices.load_voice', return_value=(voice, self.entry)), patch('worker.emit'):
            with self.assertRaises(ValueError):
                worker.tts(project, {}, folder)
        self.assertFalse((folder / 'voice.wav').exists())
