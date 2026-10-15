"""Explicit dubbing text and rendered subtitle presets, no inference or API."""
import copy
import hashlib
import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import DATA, DEFAULTS, ROOT, binary, create_project, restored_params, run, save_srt
from speech_delivery import delivery_window, missing_speech_rows, require_speech_text, spoken_text
from subtitle_presets import PRESETS, apply_subtitle_preset
import server
import worker


class MultiVoiceMetadataTests(unittest.TestCase):
    def test_multiple_voices_count_cues_and_never_label_the_whole_video_as_the_last_speaker(self):
        from voice_metadata import dubbing_voice_metadata
        first = {'id': 'voice:first', 'name': 'First speaker'}
        last = {'id': 'voice:last', 'name': 'Last speaker'}
        result = dubbing_voice_metadata([first, last, first, last, last], default=first)
        self.assertEqual(result['voice_mode'], 'multi')
        self.assertIsNone(result['voice_id'])
        self.assertEqual(result['voice_name'], '2 giọng')
        self.assertEqual(result['voices_used'], [
            {'id': 'voice:first', 'name': 'First speaker', 'cue_count': 2},
            {'id': 'voice:last', 'name': 'Last speaker', 'cue_count': 3}])

    def test_single_actual_voice_retains_compatible_id_even_when_default_differs(self):
        from voice_metadata import dubbing_voice_metadata
        result = dubbing_voice_metadata([{'id': 'actual', 'name': 'Actual'}] * 3,
            default={'id': 'default', 'name': 'Default'})
        self.assertEqual(result['voice_mode'], 'single')
        self.assertEqual(result['voice_id'], 'actual')
        self.assertEqual(result['voice_name'], 'Actual')
        self.assertEqual(result['voices_used'][0]['cue_count'], 3)


def cue(**changes):
    return {'start': 0, 'end': 2, 'text': 'Source line', 'translated': 'Translated line',
            'spoken_text': 'Edited line'} | changes


class DubbingTextSelectionTests(unittest.TestCase):
    def test_each_explicit_mode_reads_only_its_selected_field(self):
        row = cue()
        for mode, expected in [('translated', 'Translated line'), ('edited', 'Edited line'),
                               ('source', 'Source line')]:
            params = DEFAULTS | {'tts_text_source': mode}
            self.assertEqual(delivery_window(row, params, 1, 8)['text'], expected)
        self.assertEqual(spoken_text(cue(spoken_text=''), {'tts_text_source': 'edited'}), 'Translated line')

    def test_explicit_missing_text_never_falls_back_to_source(self):
        row = cue(translated='', spoken_text='')
        for mode in ('translated', 'edited'):
            params = DEFAULTS | {'tts_text_source': mode, 'tts_require_translation': False}
            self.assertEqual(spoken_text(row, params), '')
            self.assertEqual(missing_speech_rows([row], params), [0])
            with self.assertRaisesRegex(ValueError, 'thiếu nội dung lồng tiếng'):
                require_speech_text([row], params)
        require_speech_text([row], DEFAULTS | {'tts_text_source': 'source'})
        self.assertEqual(spoken_text(row, {'tts_text_source': 'source'}), 'Source line')

    def test_original_source_needs_an_explicit_choice_and_is_also_checked(self):
        row = cue(text='', translated='Xin chào', spoken_text='Lời đọc')
        with self.assertRaisesRegex(ValueError, 'Phụ đề gốc'):
            require_speech_text([row], DEFAULTS | {'tts_text_source': 'source'})
        with self.assertRaises(ValueError):
            server.validate_params({'tts_text_source': 'automatic-random'})

    def test_translation_selection_ignores_an_edited_override(self):
        row = cue(translated='', spoken_text='An edited alternative')
        self.assertEqual(missing_speech_rows([row], {'tts_text_source': 'translated'}), [0])
        self.assertEqual(missing_speech_rows([row], {'tts_text_source': 'edited'}), [])

    def test_new_projects_select_translation_old_snapshots_keep_historical_fallback(self):
        self.assertEqual(DEFAULTS['tts_text_source'], 'translated')
        with patch('core.settings', return_value=DEFAULTS.copy()):
            project = create_project(str(ROOT / 'tests/fixtures/demo.mp4'), 'Text-source selection')
        self.assertEqual(project['params']['tts_text_source'], 'translated')
        old = restored_params({'voice_speed': 1, 'voice_id': 'piper:vais1000'})
        self.assertNotIn('tts_text_source', old)
        self.assertNotIn('subtitle_preset', old)
        self.assertEqual(spoken_text(cue(), old), 'Edited line')
        self.assertEqual(spoken_text(cue(translated='', spoken_text=''), old), 'Source line')
        self.assertEqual(missing_speech_rows([cue(translated='', spoken_text='')], old), [0])
        old['tts_require_translation'] = False
        self.assertEqual(missing_speech_rows([cue(translated='', spoken_text='')], old), [])

    def test_changing_mode_invalidates_an_artifact_even_with_identical_words(self):
        project = {'media': {'duration': 8}, 'segments': [cue(text='Same', translated='Same', spoken_text='Same')],
                   'params': DEFAULTS.copy()}
        hashes = []
        for mode in ('translated', 'edited', 'source'):
            project['params']['tts_text_source'] = mode
            hashes.append(worker.speech_signature(project))
        self.assertEqual(len(set(hashes)), 3)
        # An unused edited copy must not stale a translation-selected recording.
        project['params']['tts_text_source'] = 'translated'
        baseline = worker.speech_signature(project)
        project['segments'][0]['spoken_text'] = 'Not selected'
        self.assertEqual(worker.speech_signature(project), baseline)

    def test_old_legacy_signature_is_byte_for_byte_compatible(self):
        row = cue(spoken_text='')
        project = {'media': {'duration': 8}, 'segments': [row],
                   'params': {'voice_speed': 1, 'voice_id': 'piper:vais1000'}}
        expected = ['npk-voice-v3', 8, [[0, 2, 'Translated line', None]], 1, 'piper:vais1000']
        digest = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
        self.assertEqual(worker.speech_signature(project), digest)
        project['params'] = restored_params(project['params'])
        self.assertEqual(worker.speech_signature(project), digest)

    def test_worker_refuses_missing_selected_translation_before_loading_a_model(self):
        project = {'media': {'duration': 8}, 'segments': [cue(translated='')], 'params': DEFAULTS.copy()}
        with tempfile.TemporaryDirectory(dir=DATA) as folder, patch('voices.load_voice') as load:
            with self.assertRaisesRegex(ValueError, 'Bản dịch'):
                worker.tts(project, {}, Path(folder))
            load.assert_not_called()

    def test_preflight_uses_selected_text_instead_of_the_old_spoken_fallback(self):
        from preflight import inspect
        project = create_project(str(ROOT / 'tests/fixtures/demo.mp4'), 'Selection preflight')
        project['segments'] = [cue(translated='', spoken_text='Xin chào')]
        with patch('voices.resolve_voice', return_value={'id':'piper:fixture', 'provider':'piper', 'available': True}), \
             patch('voices.audio_identity', return_value=['fixture-admission-identity']):
            self.assertFalse(inspect(project, 'tts')['ok'])
            project['params']['tts_text_source'] = 'edited'
            self.assertTrue(inspect(project, 'tts')['ok'])
            project['params']['tts_text_source'] = 'source'
            check = inspect(project, 'tts')
            self.assertTrue(check['ok'])
            self.assertTrue(any('Phụ đề gốc' in warning for warning in check['warnings']))


class SubtitlePresetTests(unittest.TestCase):
    def test_api_preset_selection_returns_actual_style_and_keeps_deliberate_overrides(self):
        for preset in PRESETS:
            result = server.validate_params({'subtitle_preset': preset})
            self.assertEqual(result, PRESETS[preset] | {'subtitle_preset': preset})
        result = server.validate_params({'subtitle_preset': 'pop', 'font_size': 62, 'font_color': '#123456'})
        self.assertEqual(result['font_size'], 62)
        self.assertEqual(result['font_color'], '#123456')
        self.assertTrue(result['subtitle_bold'])
        with self.assertRaises(ValueError):
            server.validate_params({'subtitle_preset': 'nonexistent'})

    def test_four_presets_have_distinct_real_ass_styles_and_plain_srt(self):
        import pysubs2
        row = {'start': 0, 'end': 2, 'text': 'Xin chào bạn', 'translated': 'Xin chào bạn',
               'words': [{'word': 'Xin', 'start': .1, 'end': .5},
                         {'word': 'chào', 'start': .5, 'end': 1},
                         {'word': 'bạn', 'start': 1, 'end': 1.5}]}
        signatures = set()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for preset in PRESETS:
                params = apply_subtitle_preset(DEFAULTS, preset)
                target = folder / (preset + '.ass')
                save_srt([row], target, ass_style=(params, {'width': 640, 'height': 360}))
                doc = pysubs2.load(str(target))
                self.assertEqual(doc[0].plaintext, 'Xin chào bạn')
                signatures.add(target.read_text(encoding='utf-8'))
                if preset == 'pop':
                    self.assertIn(r'\t(0,140', doc[0].text)
                if preset == 'highlight':
                    self.assertIn(r'\1c&H4DD8FF&', doc[0].text)
                    self.assertEqual(doc.styles['Default'].borderstyle, 3)
                if preset == 'karaoke':
                    self.assertIn(r'\kf', doc[0].text)
                srt = folder / (preset + '.srt')
                save_srt([row], srt)
                self.assertEqual(pysubs2.load(str(srt))[0].text, 'Xin chào bạn')
            self.assertEqual(len(signatures), 4)

    def test_karaoke_translation_without_matching_word_times_uses_bounded_presentation_estimates(self):
        import pysubs2
        row = {'start': 0, 'end': 2, 'text': 'Hello', 'translated': 'Xin chào',
               'words': [{'word': 'Hello', 'start': .1, 'end': 1}]}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'karaoke.ass'
            save_srt([row], path, ass_style=(apply_subtitle_preset(DEFAULTS, 'karaoke'), {'width': 640, 'height': 360}))
            event = pysubs2.load(str(path))[0]
            self.assertEqual(event.plaintext, 'Xin chào')
            durations = [int(value) for value in re.findall(r'\\kf(\d+)', event.text)]
            self.assertEqual(len(durations), 2)
            self.assertTrue(all(value > 0 for value in durations))
            self.assertEqual(sum(durations), 200)

    def test_estimated_karaoke_is_not_saved_as_recognition_and_preserves_wrapping(self):
        from karaoke import estimated_caption
        row = {'start': 1, 'end': 2.73, 'text': 'Nguồn khác'}
        original = copy.deepcopy(row)
        caption = estimated_caption(row, 'Xin chào\nbạn nhé')
        self.assertIn(r'\N', caption)
        durations = [int(value) for value in re.findall(r'\\kf(\d+)', caption)]
        self.assertEqual(sum(durations), 173)
        self.assertTrue(all(value >= 1 for value in durations))
        self.assertEqual(row, original)
        self.assertIsNone(estimated_caption({'start': 0, 'end': .001}, 'Too short for words'))
        self.assertIsNone(estimated_caption({'start': 0, 'end': float('nan')}, 'Invalid'))

    def test_actual_word_timing_takes_priority_over_estimated_weights(self):
        from subtitle_presets import caption_effects
        row = {'start': 0, 'end': 2, 'words': [
            {'word': 'Xin', 'start': .2, 'end': .3}, {'word': 'chào', 'start': .3, 'end': .9}]}
        caption = caption_effects(row, 'Xin chào', apply_subtitle_preset(DEFAULTS, 'karaoke'))
        self.assertIn(r'{\k20}', caption)
        self.assertIn(r'{\kf10}Xin', caption)
        self.assertIn(r'{\kf60}chào', caption)

    def test_estimated_karaoke_visibly_animates_between_two_times(self):
        from PIL import Image
        row = {'start': 0, 'end': 2, 'text': '你好', 'translated': 'Xin chào bạn'}
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            save_srt([row], folder / 'karaoke.ass',
                     ass_style=(apply_subtitle_preset(DEFAULTS, 'karaoke'), {'width': 640, 'height': 360}))
            digests = []
            for time in (.2, 1.2):
                output = folder / (str(time) + '.png')
                run([binary('ffmpeg'), '-v', 'error', '-y', '-f', 'lavfi', '-i',
                     'color=c=0x14202c:s=640x360:d=2:r=10', '-vf', 'subtitles=karaoke.ass',
                     '-ss', str(time), '-frames:v', '1', '-threads', '1', output], cwd=folder, timeout=30)
                with Image.open(output) as image:
                    digests.append(hashlib.sha256(image.convert('RGB').tobytes()).hexdigest())
            self.assertNotEqual(*digests)

    def test_ffmpeg_renders_four_visibly_distinct_presets(self):
        from PIL import Image
        row = {'start': 0, 'end': 2, 'text': 'Xin chào bạn', 'translated': 'Xin chào bạn',
               'words': [{'word': 'Xin', 'start': 0, 'end': .4},
                         {'word': 'chào', 'start': .4, 'end': .9},
                         {'word': 'bạn', 'start': .9, 'end': 1.5}]}
        images = set()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for preset in PRESETS:
                save_srt([row], folder / (preset + '.ass'),
                         ass_style=(apply_subtitle_preset(DEFAULTS, preset), {'width': 640, 'height': 360}))
                output = folder / (preset + '.png')
                run([binary('ffmpeg'), '-v', 'error', '-y', '-f', 'lavfi', '-i',
                     'color=c=0x14202c:s=640x360:d=0.5:r=10', '-vf', 'subtitles=' + preset + '.ass',
                     '-ss', '0.2', '-frames:v', '1', '-threads', '1', output], cwd=folder, timeout=30)
                with Image.open(output) as image:
                    images.add(hashlib.sha256(image.convert('RGB').tobytes()).hexdigest())
            self.assertEqual(len(images), 4)
