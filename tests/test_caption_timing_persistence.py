"""Display-word timing survives style edits but cannot outlive changed speech."""
import copy
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import core
import server
from speech_word_alignment import caption_timing_key
from worker import speech_signature


class CaptionTimingPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.files = tempfile.TemporaryDirectory(prefix='npk-caption-storage-')
        self.addCleanup(self.files.cleanup)
        self.data = Path(self.files.name)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        # The real database and the app's project/queue files are never touched.
        self.patches.enter_context(patch.object(core, 'DATA', self.data))
        self.patches.enter_context(patch.object(server, 'DATA', self.data))
        self.patches.enter_context(patch('voices.resolve_voice', side_effect=AssertionError('Voice engine accessed')))
        self.project = {
            'id': 'caption-timing-storage-fixture', 'name': 'Caption timing fixture',
            'created': 100., 'updated': 100., 'regions': [], 'extraction_regions': [],
            'media': {'path': str(core.ROOT / 'tests/fixtures/demo.mp4'),
                      'duration': 8., 'width': 640, 'height': 360},
            'params': core.DEFAULTS | {'voice_id': 'piper:vais1000', 'tts_text_source': 'translated'},
            'artifacts': {},
            'segments': [{
                'start': 1., 'end': 3., 'text': '你好', 'translated': 'Xin chào',
                'words': [{'word': '你', 'start': 1.1, 'end': 1.5, 'probability': .8},
                          {'word': '好', 'start': 1.8, 'end': 2.4, 'probability': .85}],
                'caption_words': [{'word': 'Xin', 'start': 1.2, 'end': 1.6, 'probability': .9},
                                  {'word': 'chào', 'start': 2., 'end': 2.5, 'probability': .95}],
                'word_timing_source': 'aligned'}],
        }
        self.project['artifacts']['tts'] = {
            'job_id': 'tts-fixture', 'speech_signature': speech_signature(self.project)}
        self.project['segments'][0]['caption_timing_key'] = caption_timing_key(self.project)
        core.put('project', self.project['id'], copy.deepcopy(self.project))

    def update(self, **changes):
        current = core.get('project', self.project['id'])
        return server.update_project(self.project['id'], {'expected_revision': current['updated']} | changes)

    def assert_estimated_without_stale_key(self, row):
        self.assertEqual(row.get('caption_words'), [])
        self.assertEqual(row.get('word_timing_source'), 'estimated')
        self.assertNotIn('caption_timing_key', row)

    def test_schema_roundtrip_keeps_separate_source_and_display_words(self):
        row = self.project['segments'][0]
        validated = server.validate_segments([copy.deepcopy(row)], 8.)[0]
        self.assertEqual(validated, row)
        result = self.update(segments=[validated])
        stored = core.get('project', self.project['id'])
        self.assertEqual(stored['segments'], result['segments'])
        self.assertEqual(stored['segments'][0]['words'], row['words'])
        self.assertEqual(stored['segments'][0]['caption_words'], row['caption_words'])
        self.assertEqual(server.project_by_id(self.project['id'])['segments'], stored['segments'])

    def test_font_only_change_persists_timing_and_does_not_load_a_voice(self):
        row = copy.deepcopy(self.project['segments'][0])
        result = self.update(params={'font_size': 64, 'font_color': '#ABCDEF', 'margin': 55})
        self.assertEqual(result['segments'][0], row)
        self.assertEqual(result['segments'][0]['caption_timing_key'], caption_timing_key(result))
        self.assertEqual(core.get('project', self.project['id'])['segments'][0], row)
        self.assertEqual(result['params']['font_size'], 64)

    def test_subtitle_preset_and_character_label_keep_timing(self):
        result = self.update(params={'subtitle_preset': 'highlight'})
        row = copy.deepcopy(result['segments'][0])
        row['character_name'] = 'Chị Tiểu Manh'
        result = self.update(segments=[row])
        self.assertEqual(result['segments'][0]['caption_words'], self.project['segments'][0]['caption_words'])
        self.assertEqual(result['segments'][0]['caption_timing_key'], caption_timing_key(result))

    def test_speech_speed_change_immediately_clears_derived_timing_only(self):
        source = copy.deepcopy(self.project['segments'][0]['words'])
        result = self.update(params={'voice_speed': 1.2})
        row = result['segments'][0]
        self.assert_estimated_without_stale_key(row)
        self.assert_estimated_without_stale_key(core.get('project', self.project['id'])['segments'][0])
        self.assertEqual(row['words'], source)
        self.assertEqual(row['text'], '你好')

    def test_changed_voice_never_falls_back_to_old_same_language_asr_words(self):
        from subtitle_presets import highlight_word_timings
        same_language = copy.deepcopy(self.project)
        row = same_language['segments'][0]
        row['text'] = 'Xin chào'
        row['words'] = copy.deepcopy(row['caption_words'])
        row['caption_timing_key'] = caption_timing_key(same_language)
        core.put('project', same_language['id'], same_language)
        result = self.update(params={'voice_speed': 1.2})
        changed = result['segments'][0]
        self.assert_estimated_without_stale_key(changed)
        self.assertEqual(changed['words'], row['words'])
        self.assertEqual(highlight_word_timings(changed, changed['translated'])['source'], 'estimated')

    def test_translated_text_and_cue_times_clear_timing_but_keep_chinese_source_words(self):
        for change in ({'translated': 'Xin chào bạn'}, {'start': .9}, {'end': 3.1}):
            with self.subTest(change=change):
                core.put('project', self.project['id'], copy.deepcopy(self.project))
                rows = copy.deepcopy(self.project['segments'])
                rows[0].update(change)
                result = self.update(segments=rows)
                row = core.get('project', self.project['id'])['segments'][0]
                self.assertEqual(result['segments'][0], row)
                self.assertEqual(row['words'], self.project['segments'][0]['words'])
                self.assertEqual(row['text'], '你好')
                self.assert_estimated_without_stale_key(row)

    def test_reading_stale_artifact_filters_display_timings_before_any_preview(self):
        stale = copy.deepcopy(self.project)
        stale['artifacts']['tts']['job_id'] = 'a-new-speech-artifact'
        core.put('project', stale['id'], stale)
        loaded = server.project_by_id(stale['id'])
        self.assert_estimated_without_stale_key(loaded['segments'][0])
        self.assertEqual(loaded['segments'][0]['words'], stale['segments'][0]['words'])
        # Reading filters stale timing in memory; the next normal save commits it.
        result = self.update(params={'font_size': 60})
        self.assert_estimated_without_stale_key(core.get('project', stale['id'])['segments'][0])
        self.assertEqual(result['segments'][0]['words'], stale['segments'][0]['words'])

    def test_schema_rejects_bad_signatures_and_timing_sources(self):
        for bad in ('bad', 'A' * 64, 'a' * 63, None, 2, {}, True):
            row = copy.deepcopy(self.project['segments'][0])
            row['caption_timing_key'] = bad
            with self.subTest(key=bad), self.assertRaisesRegex(ValueError, 'Chữ ký căn từ'):
                server.validate_segments([row], 8.)
        for bad in ('recognition', '', None, 2, True, []):
            row = copy.deepcopy(self.project['segments'][0])
            row['word_timing_source'] = bad
            with self.subTest(source=bad), self.assertRaisesRegex(ValueError, 'Nguồn mốc từ'):
                server.validate_segments([row], 8.)

    def test_schema_rejects_negative_infinite_or_invalid_probability_values(self):
        for field, value in (('start', -1), ('end', 4), ('start', float('nan')),
                             ('end', float('inf')), ('probability', -.1),
                             ('probability', 1.1), ('probability', float('inf')),
                             ('probability', True)):
            row = copy.deepcopy(self.project['segments'][0])
            row['caption_words'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                server.validate_segments([row], 8.)
        row = copy.deepcopy(self.project['segments'][0])
        row['caption_words'].reverse()
        with self.assertRaises(ValueError):
            server.validate_segments([row], 8.)

    def test_caption_words_reject_zero_or_reversed_duration_but_keep_legacy_source_boundaries(self):
        for end in (1.2, 1.1):
            row = copy.deepcopy(self.project['segments'][0])
            row['caption_words'][0]['end'] = end
            with self.subTest(end=end), self.assertRaises(ValueError):
                server.validate_segments([row], 8.)
        row = copy.deepcopy(self.project['segments'][0])
        row['words'][0]['end'] = row['words'][0]['start']
        result = server.validate_segments([row], 8.)[0]
        self.assertEqual(result['words'], row['words'])
        self.assertEqual(result['caption_words'], row['caption_words'])


if __name__ == '__main__':
    unittest.main()
