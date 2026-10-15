"""Acoustic caption timing regressions, without a model or real project writes."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speech_word_alignment import (
    ALIGNMENT_VERSION,
    align_captions,
    apply_timings,
    caption_language,
    caption_timing_key,
    display_word_timings,
    prune_stale_timings,
    valid_cached_report,
)


def raw_word(word, start, end, probability=.9):
    return dict(word=word, start=start, end=end, probability=probability)


def project():
    return {
        'params': {'voice_offset': 0, 'font': 'Arial', 'font_size': 32},
        'media': {'duration': 20.},
        'artifacts': {'tts': {'job_id': 'speech-a'}},
        'segments': [dict(start=11., end=15., text='姐你轻点', translated='Chị nhẹ tay thôi!',
                          words=[raw_word('姐', 11., 11.5)], voice_id='female-one',
                          character_name='Em gái')],
    }


class DisplayWordTimingsTests(unittest.TestCase):
    def test_punctuation_case_and_whitespace_preserve_exact_display_text(self):
        result = display_word_timings('“Chị”,\nnhẹ tay thôi!', [
            raw_word(' chị', 0, .2), raw_word(',', .2, .2),
            raw_word(' NHẸ', .3, .5), raw_word(' tay', .5, .7),
            raw_word(' thôi.', .7, 1.),
        ], 11., 15.)
        self.assertEqual([word['word'] for word in result], ['“Chị”,', 'nhẹ', 'tay', 'thôi!'])
        self.assertEqual([(word['start'], word['end']) for word in result],
                         [(11., 11.2), (11.3, 11.5), (11.5, 11.7), (11.7, 12.)])

    def test_unicode_subword_pieces_form_one_display_word(self):
        result = display_word_timings('Tiểu Uy!', [
            raw_word(' Ti', 0, .1), raw_word('ểu', .1, .25), raw_word(' Uy', .25, .5),
        ], 3., 4.)
        self.assertEqual(result[0]['word'], 'Tiểu')
        self.assertEqual((result[0]['start'], result[0]['end']), (3., 3.25))
        self.assertEqual((result[1]['start'], result[1]['end']), (3.25, 3.5))

    def test_different_or_missing_spoken_words_are_rejected(self):
        for words in ([raw_word(' Chị', 0, .2), raw_word(' tay', .2, .4)],
                      [raw_word(' Chị', 0, .2), raw_word(' nhẹ', .2, .4), raw_word(' rất', .4, .6)]):
            with self.subTest(words=words), self.assertRaisesRegex(ValueError, 'khớp'):
                display_word_timings('Chị nhẹ', words, 1., 2.)

    def test_invalid_boundaries_or_confidence_cannot_claim_acoustic_alignment(self):
        invalid = [
            raw_word('Chị', 0, 0), raw_word('Chị', .2, .1),
            raw_word('Chị', float('nan'), .2), raw_word('Chị', 0, float('inf')),
            raw_word('Chị', 0, .2, float('nan')), raw_word('Chị', 0, .2, -.01),
            raw_word('Chị', 0, .2, 1.01),
        ]
        for word in invalid:
            with self.subTest(word=word), self.assertRaises(ValueError):
                display_word_timings('Chị', [word], 1., 2.)

    def test_low_confidence_for_whole_utterance_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'tin cậy'):
            display_word_timings('Chị nhẹ', [raw_word('Chị', 0, .2, .01),
                                           raw_word('nhẹ', .2, .4, .1)], 1., 2.)

    def test_proper_name_low_probability_does_not_discard_good_utterance(self):
        result = display_word_timings('Tiểu Uy chào chị!', [
            raw_word('Tiểu', 0, .2, .9), raw_word('Uy', .2, .4, .01),
            raw_word('chào', .4, .6, .92), raw_word('chị', .6, .8, .93),
        ], 11., 12.)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[1]['probability'], .01)

    def test_laughter_low_probability_keeps_supported_greeting_timings(self):
        result = display_word_timings('Hì hì, chào anh!', [
            raw_word('Hì', 0, .16, .043), raw_word('hì,', .16, .52, .217),
            raw_word('chào', .62, .76, .914), raw_word('anh!', .76, .94, .93),
        ], 138., 139.92)
        self.assertEqual([word['word'] for word in result], ['Hì', 'hì,', 'chào', 'anh!'])
        self.assertEqual(result[2]['start'], 138.62)

    def test_time_outside_cue_or_backward_words_is_rejected(self):
        cases = [
            [raw_word('Chị', -.1, .2), raw_word('nhẹ', .2, .4)],
            [raw_word('Chị', 0, .2), raw_word('nhẹ', .2, 1.1)],
            [raw_word('Chị', .5, .8), raw_word('nhẹ', .1, .2)],
        ]
        for words in cases:
            with self.subTest(words=words), self.assertRaises(ValueError):
                display_word_timings('Chị nhẹ', words, 1., 2.)

    def test_small_numeric_overhang_clamps_to_cue_and_positive_offset_is_applied(self):
        result = display_word_timings('Chị', [raw_word('Chị', -.01, 1.01)], 1., 2.)
        self.assertEqual((result[0]['start'], result[0]['end']), (1., 2.))
        shifted = display_word_timings('Chị', [raw_word('Chị', .1, .2)], 1., 2., offset=.25)
        self.assertEqual((shifted[0]['start'], shifted[0]['end']), (1.35, 1.45))

    def test_no_words_or_only_punctuation_is_not_an_alignment(self):
        for words in ([], [raw_word('!', 0, .2)]):
            with self.subTest(words=words), self.assertRaises(ValueError):
                display_word_timings('!', words, 1., 2.)


class CaptionTimingIdentityTests(unittest.TestCase):
    def test_imported_audio_uses_pure_signature_when_old_synthesis_voice_is_missing(self):
        p = project()
        with tempfile.TemporaryDirectory(prefix='npk-external-word-') as owned:
            voice = Path(owned) / 'external.wav'
            voice.write_bytes(b'owned external audio for identity only')
            p['params']['voice_path'] = str(voice)
            with patch('worker.speech_signature', side_effect=ValueError('old voice removed')), \
                    patch('voices.resolve_voice', side_effect=AssertionError('must not reload missing synthesis voice')):
                first = caption_timing_key(p)
                self.assertEqual(len(first), 64)
                self.assertEqual(caption_timing_key(p), first)
                p['params']['voice_speed'] = 1.1
                self.assertNotEqual(caption_timing_key(p), first)

    def test_missing_synthesis_voice_still_errors_without_imported_audio(self):
        with patch('worker.speech_signature', side_effect=ValueError('old voice removed')):
            with self.assertRaisesRegex(ValueError, 'removed'):
                caption_timing_key(project())

    def test_language_aliases_normalize_and_language_change_invalidates_timing(self):
        p = project()
        with patch('worker.speech_signature', return_value='unchanged-speech'):
            original = caption_timing_key(p)
            for language in ('vi', 'Vietnamese', ' Tiếng Việt '):
                with self.subTest(language=language):
                    p['params']['target_language'] = language
                    self.assertEqual(caption_language(p), 'vi')
                    self.assertEqual(caption_timing_key(p), original)
            p['params']['target_language'] = 'English'
            self.assertEqual(caption_language(p), 'en')
            self.assertNotEqual(caption_timing_key(p), original)

    def test_speech_change_invalidates_timing_even_with_same_subtitle(self):
        p = project()
        with patch('worker.speech_signature', return_value='speech-one'):
            first = caption_timing_key(p)
        with patch('worker.speech_signature', return_value='speech-two'):
            self.assertNotEqual(first, caption_timing_key(p))

    def test_text_time_offset_and_speech_artifact_change_invalidate_timing(self):
        p = project()
        with patch('worker.speech_signature', return_value='unchanged-speech'):
            first = caption_timing_key(p)
            variants = []
            changed = copy.deepcopy(p); changed['segments'][0]['end'] += .5; variants.append(changed)
            changed = copy.deepcopy(p); changed['segments'][0]['translated'] = 'Chị nhẹ tay nhé!'; variants.append(changed)
            changed = copy.deepcopy(p); changed['params']['voice_offset'] = .1; variants.append(changed)
            changed = copy.deepcopy(p); changed['artifacts']['tts']['job_id'] = 'speech-b'; variants.append(changed)
            for changed in variants:
                with self.subTest(changed=changed):
                    self.assertNotEqual(first, caption_timing_key(changed))

    def test_font_only_edit_keeps_existing_speech_timing_key(self):
        p = project()
        with patch('worker.speech_signature', return_value='unchanged-speech'):
            first = caption_timing_key(p)
            p['params'].update(font='New font', font_size=65, color='#ffee00', subtitle_preset='karaoke')
            self.assertEqual(first, caption_timing_key(p))

    def test_external_audio_replacement_changes_identity(self):
        p = project()
        with tempfile.TemporaryDirectory(prefix='npk-word-audio-') as owned:
            voice = Path(owned) / 'voice.wav'
            voice.write_bytes(b'first-audio')
            p['params']['voice_path'] = str(voice)
            with patch('worker.speech_signature', return_value='unchanged-speech'):
                first = caption_timing_key(p)
                voice.write_bytes(b'different-audio-longer')
                self.assertNotEqual(first, caption_timing_key(p))


class CaptionTimingApplicationTests(unittest.TestCase):
    def test_stale_dub_timings_do_not_fall_back_to_same_language_source_recognition(self):
        from subtitle_presets import highlight_word_timings
        p = project()
        original_words = [raw_word('Chị', 11., 11.2), raw_word('nhẹ', 11.2, 11.4),
                          raw_word('tay', 11.4, 11.6), raw_word('thôi!', 11.6, 11.8)]
        p['segments'][0].update(words=copy.deepcopy(original_words),
                                caption_words=copy.deepcopy(original_words),
                                caption_timing_key='old-dub', word_timing_source='aligned')
        with patch('speech_word_alignment.caption_timing_key', return_value='changed-dub-rate'):
            prune_stale_timings(p)
        row = p['segments'][0]
        self.assertEqual(row['words'], original_words)
        timing = highlight_word_timings(row, row['translated'])
        self.assertEqual(timing['source'], 'estimated')
        self.assertNotEqual(timing['words'], original_words)

    def test_missing_voice_does_not_prevent_reading_and_clears_even_orphan_timings(self):
        for fields in (dict(caption_timing_key='old'), {}):
            p = project()
            p['segments'][0].update(caption_words=[raw_word('Chị', 11., 11.2)],
                                    word_timing_source='aligned', **fields)
            source_words = copy.deepcopy(p['segments'][0]['words'])
            with self.subTest(fields=fields), \
                    patch('speech_word_alignment.caption_timing_key', side_effect=ValueError('voice missing')):
                prune_stale_timings(p)
            self.assertEqual(p['segments'][0]['caption_words'], [])
            self.assertNotIn('caption_timing_key', p['segments'][0])
            self.assertEqual(p['segments'][0]['word_timing_source'], 'estimated')
            self.assertEqual(p['segments'][0]['words'], source_words)

    def test_applied_words_do_not_share_references_with_report(self):
        p = project()
        report = {'signature': 'current', 'rows': [dict(index=0, source='aligned',
                                                       words=[raw_word('Chị', 11., 11.2)])]}
        original_report = copy.deepcopy(report)
        result = apply_timings(p, report)
        result[0]['caption_words'][0]['word'] = 'copy edited'
        self.assertEqual(report, original_report)
        report['rows'][0]['words'][0]['start'] = 12.
        self.assertEqual(result[0]['caption_words'][0]['start'], 11.)

    def test_orphan_translated_words_without_signature_are_removed(self):
        p = project()
        p['segments'][0].update(caption_words=[raw_word('Chị', 11., 11.2)],
                                word_timing_source='aligned')
        source_words = copy.deepcopy(p['segments'][0]['words'])
        with patch('speech_word_alignment.caption_timing_key', return_value='current'):
            prune_stale_timings(p)
        self.assertEqual(p['segments'][0]['caption_words'], [])
        self.assertEqual(p['segments'][0]['word_timing_source'], 'estimated')
        self.assertNotIn('caption_timing_key', p['segments'][0])
        self.assertEqual(p['segments'][0]['words'], source_words)

    def test_apply_timings_preserves_source_recognition_and_does_not_modify_input(self):
        p = project()
        original = copy.deepcopy(p)
        translated_words = [raw_word('Chị', 11., 11.2)]
        report = {'signature': 'current', 'rows': [dict(index=0, source='aligned', words=translated_words)]}
        result = apply_timings(p, report)
        self.assertEqual(p, original)
        self.assertEqual(result[0]['words'], original['segments'][0]['words'])
        for field in ('text', 'translated', 'start', 'end', 'voice_id', 'character_name'):
            self.assertEqual(result[0][field], original['segments'][0][field])
        self.assertEqual(result[0]['caption_words'], translated_words)
        self.assertEqual(result[0]['caption_timing_key'], 'current')
        self.assertEqual(result[0]['word_timing_source'], 'aligned')
        result[0]['words'][0]['word'] = 'changed in copy'
        self.assertEqual(p, original)

    def test_stale_translated_timings_clear_without_erasing_source_words(self):
        p = project()
        p['segments'][0].update(caption_words=[raw_word('Chị', 11., 11.2)],
                                caption_timing_key='old', word_timing_source='aligned')
        source_words = copy.deepcopy(p['segments'][0]['words'])
        with patch('speech_word_alignment.caption_timing_key', return_value='new'):
            prune_stale_timings(p)
        self.assertEqual(p['segments'][0]['words'], source_words)
        self.assertEqual(p['segments'][0]['caption_words'], [])
        self.assertNotIn('caption_timing_key', p['segments'][0])
        self.assertEqual(p['segments'][0]['word_timing_source'], 'estimated')

    def test_current_timings_are_retained_and_unrelated_source_rows_are_untouched(self):
        p = project()
        p['segments'][0].update(caption_words=[raw_word('Chị', 11., 11.2)],
                                caption_timing_key='current', word_timing_source='aligned')
        p['segments'].append(dict(start=15., end=16., text='第二句', translated='Câu hai',
                                  words=[raw_word('第二句', 15., 16.)]))
        original = copy.deepcopy(p)
        with patch('speech_word_alignment.caption_timing_key', return_value='current'):
            prune_stale_timings(p)
        self.assertEqual(p, original)


class CaptionTimingCacheTests(unittest.TestCase):
    def setUp(self):
        self.project = project()
        self.report = {
            'version': ALIGNMENT_VERSION, 'signature': 'signature', 'audio_sha256': 'audio-hash',
            'aligned_cues': 1, 'estimated_cues': 0,
            'rows': [dict(index=0, source='aligned', words=[
                raw_word('Chị', 11., 11.2), raw_word('nhẹ', 11.2, 11.4),
                raw_word('tay', 11.4, 11.6), raw_word('thôi!', 11.6, 11.8),
            ])],
        }

    def valid(self, report):
        return valid_cached_report(report, self.project, 'signature', 'audio-hash')

    def test_complete_aligned_or_estimated_cache_is_accepted(self):
        self.assertTrue(self.valid(self.report))
        estimated = copy.deepcopy(self.report)
        estimated.update(aligned_cues=0, estimated_cues=1)
        estimated['rows'][0].update(source='estimated', words=[])
        self.assertTrue(self.valid(estimated))

    def test_wrong_version_signature_audio_or_missing_rows_is_rejected(self):
        variants = [None, [], {}, dict(self.report, rows=[]), dict(self.report, rows={})]
        for field, bad in (('version', 'old-version'), ('signature', 'old-signature'),
                           ('audio_sha256', 'other-audio')):
            variants.append(dict(self.report) | {field: bad})
        for report in variants:
            with self.subTest(report=report):
                self.assertFalse(self.valid(report))

    def test_wrong_row_indices_or_sources_cannot_attach_words_to_another_cue(self):
        for index in (-1, 1, '0', None, False, True):
            altered = copy.deepcopy(self.report)
            altered['rows'][0]['index'] = index
            with self.subTest(index=index):
                self.assertFalse(self.valid(altered))
        altered = copy.deepcopy(self.report)
        altered['rows'][0]['source'] = 'unknown'
        self.assertFalse(self.valid(altered))

    def test_count_metadata_must_agree_with_actual_rows(self):
        for counts in (dict(aligned_cues=0), dict(aligned_cues=2), dict(estimated_cues=1),
                       dict(aligned_cues='1'), dict(estimated_cues=-1),
                       dict(aligned_cues=True), dict(estimated_cues=False)):
            with self.subTest(counts=counts):
                self.assertFalse(self.valid(dict(self.report) | counts))

    def test_bad_cached_confidence_or_boolean_number_is_rejected(self):
        for values in (dict(probability=-.1), dict(probability=1.1),
                       dict(probability=float('nan')), dict(probability=float('inf')),
                       dict(probability=None), dict(probability=True),
                       dict(start=True), dict(end=False)):
            altered = copy.deepcopy(self.report)
            altered['rows'][0]['words'][0].update(values)
            with self.subTest(values=values):
                self.assertFalse(self.valid(altered))

    def test_partially_out_of_bounds_cached_words_are_not_repaired_by_clamping(self):
        for values in (dict(start=10.99), dict(end=15.01)):
            altered = copy.deepcopy(self.report)
            word = altered['rows'][0]['words'][0 if 'start' in values else -1]
            word.update(values)
            with self.subTest(values=values):
                self.assertFalse(self.valid(altered))

    def test_wrong_text_or_partial_word_list_is_rejected(self):
        altered = copy.deepcopy(self.report)
        altered['rows'][0]['words'][1]['word'] = 'lệch'
        self.assertFalse(self.valid(altered))
        altered = copy.deepcopy(self.report)
        altered['rows'][0]['words'].pop()
        self.assertFalse(self.valid(altered))
        altered = copy.deepcopy(self.report)
        altered['rows'][0]['words'] = None
        self.assertFalse(self.valid(altered))

    def test_invalid_word_times_or_estimate_with_acoustic_words_is_rejected(self):
        for times in (dict(start=float('nan')), dict(end=float('inf')),
                      dict(start=11.2, end=11.2), dict(start=16., end=17.),
                      dict(start=11.7, end=11.8)):
            altered = copy.deepcopy(self.report)
            altered['rows'][0]['words'][0].update(times)
            with self.subTest(times=times):
                self.assertFalse(self.valid(altered))
        altered = copy.deepcopy(self.report)
        altered.update(aligned_cues=0, estimated_cues=1)
        altered['rows'][0]['source'] = 'estimated'
        self.assertFalse(self.valid(altered))


class CaptionTimingStartupFallbackTests(unittest.TestCase):
    def test_damaged_offline_model_falls_back_without_caching_failed_runtime(self):
        p = project()
        original = copy.deepcopy(p)
        with tempfile.TemporaryDirectory(prefix='npk-word-startup-') as owned:
            root = Path(owned)
            models, data, output = root / 'models', root / 'data', root / 'output'
            data.mkdir(); output.mkdir()
            snapshot = models / 'whisper/models--Systran--faster-whisper-small/snapshots/test-snapshot'
            snapshot.mkdir(parents=True)
            for name in ('model.bin', 'tokenizer.json', 'config.json'):
                (snapshot / name).write_bytes(b'fixture, never parsed by a real engine')
            audio = root / 'voice.wav'
            audio.write_bytes(b'audio fixture hashed but never decoded')
            with patch('core.DATA', data), patch('core.MODELS', models), \
                    patch('worker.speech_signature', return_value='speech-signature'), \
                    patch('faster_whisper.WhisperModel', side_effect=RuntimeError('broken local model')) as engine, \
                    patch('faster_whisper.audio.decode_audio') as decode:
                rows, report = align_captions(p, audio, output)
                self.assertEqual(report['aligned_cues'], 0)
                self.assertEqual(report['estimated_cues'], 1)
                self.assertEqual(report['rows'][0]['source'], 'estimated')
                self.assertIn('khởi động', report['rows'][0]['reason'])
                self.assertEqual(rows[0]['caption_words'], [])
                self.assertTrue((output / 'speech-word-timings.json').is_file())
                self.assertEqual(list((data / 'word-timing-cache').glob('*.json')), [])
                align_captions(p, audio, output)
                self.assertEqual(engine.call_count, 2, 'A runtime failure must be retried after repair.')
                decode.assert_not_called()
            self.assertEqual(p, original)


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='npk-word-tests-') as isolated:
        os.environ['VIETDUB_DATA_DIR'] = isolated
        unittest.main(verbosity=2)
