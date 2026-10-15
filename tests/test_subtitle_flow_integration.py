"""Single-line pages keep speech/content intact across render, save and review.

No model, provider or encoding subprocess is started. Render tests exercise the
actual SRT/ASS writers while replacing acoustic alignment with known fixtures.
"""
import copy
from contextlib import ExitStack
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class SubtitleFlowIntegrationTests(unittest.TestCase):
    def setUp(self):
        import core, worker, server
        self.core, self.worker, self.server = core, worker, server
        self.owned = tempfile.TemporaryDirectory(prefix='npk-subtitle-flow-')
        self.addCleanup(self.owned.cleanup)
        self.root = Path(self.owned.name)
        self.output = self.root / 'render'
        self.output.mkdir()
        source = self.root / 'source.mp4'
        shutil.copyfile(core.ROOT / 'tests/fixtures/demo.mp4', source)
        voice = self.root / 'voice.wav'
        with wave.open(str(voice), 'wb') as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
            audio.writeframes(b'\x00\x00' * (16000 * 8))
        text = 'Chị nhẹ tay thôi! Chị sắp đập Tiểu Uy chết rồi!'
        words = [dict(word=token, start=.5 + .5 * index, end=.9 + .5 * index, probability=.9)
                 for index, token in enumerate(text.split())]
        self.project = {
            'id': 'flow-fixture', 'name': 'Flow fixture', 'media': core.probe(source),
            'regions': [], 'artifacts': {},
            'params': core.DEFAULTS | {'subtitle_flow': 'single', 'subtitle_chunk_words': 4,
                'subtitle_preset': 'clean', 'font_size': 32, 'burn_subtitles': True,
                'voice_path': str(voice), 'original_gain': 0, 'voice_gain': 1,
                'bgm_gain': 0, 'video_encoder': 'cpu', 'use_clean': False},
            'segments': [dict(start=.3, end=7.7, text='姐你轻点 小威都快你拍死了', translated=text,
                              words=[dict(word='姐你轻点', start=.3, end=.8)],
                              caption_words=words, word_timing_source='aligned')],
        }
        from subtitle_quality import coverage_record
        self.project['coverage'] = coverage_record(self.project, 'asr', 0, 8)

    def alignment_fixture(self, project, *_args, **_kwargs):
        return copy.deepcopy(project['segments']), {
            'signature': 'a' * 64, 'model': 'unit-fixture', 'method': 'known-transcript-dtw',
            'aligned_cues': len(project['segments']), 'estimated_cues': 0, 'cache_reused': True,
        }

    def render_without_engines(self):
        class FinishedProcess:
            def __init__(self):
                self.stdout = io.StringIO('out_time_us=8000000\n')
            def wait(self):
                return 0
        with ExitStack() as guards:
            for name in ('ocr', 'asr', 'translation', 'tts', 'clean', 'pipeline'):
                guards.enter_context(patch.object(self.worker, name,
                    side_effect=AssertionError('Style paging must not invoke ' + name)))
            guards.enter_context(patch.object(self.worker, 'emit'))
            alignment = guards.enter_context(patch('speech_word_alignment.align_captions',
                side_effect=self.alignment_fixture))
            encoder = guards.enter_context(patch.object(self.worker.subprocess, 'Popen',
                return_value=FinishedProcess()))
            result = self.worker.render(self.project, {'kind': 'render'}, self.output)
        return result, alignment, encoder

    def test_clean_and_pop_single_flow_align_audio_without_regenerating_voice(self):
        original = copy.deepcopy(self.project)
        for preset in ('clean', 'pop'):
            with self.subTest(preset=preset):
                self.project['params']['subtitle_preset'] = preset
                result, alignment, encoder = self.render_without_engines()
                alignment.assert_called_once()
                self.assertEqual(alignment.call_args.args[1], self.project['params']['voice_path'])
                self.assertEqual(result['word_timing']['aligned_cues'], 1)
                command = encoder.call_args.args[0]
                self.assertIn(self.project['params']['voice_path'], command)
                self.assertEqual(self.project['segments'], original['segments'])
                self.assertEqual(self.project['artifacts'], original['artifacts'])

    def test_per_cue_single_flow_requests_timing_when_global_flow_is_wrap(self):
        self.project['params'].update(subtitle_flow='wrap', subtitle_preset='clean')
        self.project['segments'][0]['subtitle_style'] = {'subtitle_flow': 'single', 'subtitle_chunk_words': 3}
        _result, alignment, _encoder = self.render_without_engines()
        alignment.assert_called_once()

    def test_ass_is_paged_but_plain_srt_and_original_edit_row_are_complete(self):
        import pysubs2
        original = copy.deepcopy(self.project['segments'])
        self.render_without_engines()
        srt = pysubs2.load(str(self.output / 'subtitles.srt'), encoding='utf-8')
        ass = pysubs2.load(str(self.output / 'subtitles.ass'), encoding='utf-8')
        self.assertEqual(len(srt), 1)
        self.assertEqual(srt[0].plaintext, original[0]['translated'])
        self.assertGreater(len(ass), 1)
        self.assertEqual(' '.join(event.plaintext for event in ass).split(), original[0]['translated'].split())
        self.assertTrue(all('\n' not in event.plaintext and r'\N' not in event.text for event in ass))
        self.assertEqual(ass[0].start, 300)
        self.assertEqual(ass[-1].end, 7700)
        for previous, following in zip(ass, ass.events[1:]):
            self.assertLessEqual(previous.end, following.start)
        self.assertEqual(self.project['segments'], original)

    def test_pages_do_not_advance_before_their_actual_first_spoken_word(self):
        from subtitle_flow import caption_chunks
        row = self.project['segments'][0]
        pages = caption_chunks(row, row['translated'], self.project['params'], self.project['media'],
                               measure=lambda text, size: len(text) * size / 2)
        seen = 0
        for index, page in enumerate(pages):
            count = len(page['text'].split())
            self.assertLessEqual(count, 4)
            if index:
                self.assertAlmostEqual(page['start'], row['caption_words'][seen]['start'])
            self.assertEqual(page['word_timing_source'], 'aligned')
            seen += count
        self.assertEqual(seen, len(row['translated'].split()))
        self.assertAlmostEqual(pages[-1]['end'], row['end'])

    def test_flow_font_and_per_cue_style_leave_audio_clean_and_alignment_identities_unchanged(self):
        from speech_word_alignment import caption_timing_key
        clean = self.core.signature(self.project)
        speech = self.worker.speech_signature(self.project)
        timing = caption_timing_key(self.project)
        self.project['params'].update(subtitle_flow='wrap', subtitle_chunk_words=10, font_size=45,
                                      subtitle_preset='pop')
        self.project['segments'][0]['subtitle_style'] = {
            'subtitle_flow': 'single', 'subtitle_chunk_words': 2, 'font_size': 25,
        }
        self.assertEqual(self.core.signature(self.project), clean)
        self.assertEqual(self.worker.speech_signature(self.project), speech)
        self.assertEqual(caption_timing_key(self.project), timing)

    def test_flow_configuration_and_per_cue_override_survive_validation_and_portable_export(self):
        from project_transfer import export_document
        self.project['segments'][0]['subtitle_style'] = {'subtitle_flow': 'wrap', 'subtitle_chunk_words': 8}
        config = self.server.validate_params({'subtitle_flow': 'single', 'subtitle_chunk_words': 4})
        self.assertEqual(config['subtitle_flow'], 'single')
        self.assertEqual(config['subtitle_chunk_words'], 4)
        rows = self.server.validate_segments(self.project['segments'], self.project['media']['duration'])
        self.assertEqual(rows[0]['subtitle_style'], self.project['segments'][0]['subtitle_style'])
        portable = export_document(self.project)
        self.assertEqual(portable['params']['subtitle_flow'], 'single')
        self.assertEqual(portable['params']['subtitle_chunk_words'], 4)
        self.assertEqual(portable['segments'][0]['subtitle_style'], rows[0]['subtitle_style'])
        for bad in ({'subtitle_flow': 'drop_words'}, {'subtitle_chunk_words': 1},
                    {'subtitle_chunk_words': 13}, {'subtitle_chunk_words': True}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.server.validate_params(bad)

    def test_dense_cue_review_never_rewrites_or_shortens_the_subtitle(self):
        from subtitle_quality import inspect_subtitles
        row = self.project['segments'][0]
        row['end'] = 2.
        original = copy.deepcopy(self.project)
        review = inspect_subtitles(self.project)
        self.assertIn('reading', [issue['code'] for issue in review['issues']])
        self.assertEqual(self.project, original)

    def layout_request(self, body, authenticated=True):
        from fastapi.testclient import TestClient
        with TestClient(self.server.app) as client:
            headers = {'x-studio-token': self.server.TOKEN} if authenticated else {}
            return client.post('/api/studio/projects/flow-fixture/caption-layout', json=body, headers=headers)

    def test_layout_api_uses_exact_export_pages_without_writes_or_engine_calls(self):
        from subtitle_flow import caption_chunks
        from speech_word_alignment import caption_timing_key
        self.project['updated'] = 123.
        self.project['segments'][0]['caption_timing_key'] = caption_timing_key(self.project)
        original = copy.deepcopy(self.project)
        body = {'expected_revision': 123., 'params': self.project['params'], 'segments': self.project['segments']}
        with patch.object(self.server, 'project_by_id', return_value=self.project), \
                patch('studio_api.put', side_effect=AssertionError('Read-only layout wrote storage')), \
                patch('speech_word_alignment.align_captions', side_effect=AssertionError('Draft loaded alignment')):
            response = self.layout_request(body)
        self.assertEqual(response.status_code, 200, response.text)
        layout = response.json()
        self.assertEqual(layout['project_id'], self.project['id'])
        self.assertEqual(layout['cues'][0]['source'], 'aligned')
        self.assertEqual(layout['cues'][0]['chunks'], caption_chunks(self.project['segments'][0],
            self.project['segments'][0]['translated'], self.project['params'], self.project['media']))
        self.assertEqual(self.project, original)

    def test_layout_api_rejects_stale_revision_bad_configuration_and_unauthenticated_access(self):
        self.project['updated'] = 123.
        with patch.object(self.server, 'project_by_id', return_value=self.project):
            self.assertEqual(self.layout_request({'expected_revision': 122.}).status_code, 409)
            self.assertEqual(self.layout_request({'expected_revision': 123.,
                'params': {'subtitle_chunk_words': 2.5}}).status_code, 400)
            self.assertEqual(self.layout_request({'expected_revision': 123.}, authenticated=False).status_code, 403)

    def test_layout_api_discards_stale_draft_timings_and_retains_saved_audio_and_text(self):
        from speech_word_alignment import caption_timing_key
        self.project['updated'] = 123.
        self.project['segments'][0]['caption_timing_key'] = caption_timing_key(self.project)
        original = copy.deepcopy(self.project)
        with patch.object(self.server, 'project_by_id', return_value=self.project):
            response = self.layout_request({'expected_revision': 123., 'params': {'voice_speed': 1.2}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['cues'][0]['source'], 'estimated')
        self.assertEqual(' '.join(page['text'] for page in response.json()['cues'][0]['chunks']).split(),
                         self.project['segments'][0]['translated'].split())
        self.assertEqual(self.project, original)


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='npk-subtitle-flow-data-') as isolated:
        os.environ['VIETDUB_DATA_DIR'] = isolated
        unittest.main(verbosity=2)
