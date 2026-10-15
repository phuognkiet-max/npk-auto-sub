"""Acoustic grouping is bounded, review-only and does not mutate voice assignments."""
import copy
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import numpy as np
import speaker_groups
import voice_enrollment
from test_vieneu_voice import wav


def vector(index):
    value = np.zeros(192, dtype=np.float32)
    value[index] = 1
    return value


class ClusteringTests(unittest.TestCase):
    def test_same_and_different_speakers_are_reviewable_groups(self):
        result = speaker_groups.cluster_embeddings([vector(0), vector(1), vector(0) * 3])
        self.assertEqual([row['group_index'] for row in result], [0, 1, 0])
        self.assertTrue(all(row['needs_review'] for row in result))
        self.assertTrue(all(row['confidence_type'] == 'cosine_similarity_not_probability' for row in result))

    def test_group_limit_keeps_low_similarity_cue_unassigned(self):
        result = speaker_groups.cluster_embeddings([vector(0), vector(1)], max_groups=1)
        self.assertEqual(result[0]['group_index'], 0)
        self.assertIsNone(result[1]['group_index'])
        self.assertIsNone(result[1]['similarity'])

    def test_invalid_vectors_threshold_and_unbounded_work_are_rejected(self):
        for embeddings, kwargs in [([np.zeros(192)], {}), ([np.ones(3)], {}),
                ([np.full(192, float('nan'))], {}), ([vector(0)] * 201, {}),
                ([], {'threshold': .1}), ([], {'max_groups': 13})]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                speaker_groups.cluster_embeddings(embeddings, **kwargs)


class AnalysisTests(unittest.TestCase):
    def test_signature_is_cheap_and_tracks_source_timing_and_selection(self):
        with tempfile.TemporaryDirectory(prefix='npk-speaker-identity-') as temporary:
            source = Path(temporary) / 'source.wav'
            source.write_bytes(b'fixture')
            project = dict(media={'path': str(source)}, params={}, segments=[
                dict(start=0, end=2, text='one'), dict(start=2, end=4, text='two')])
            with patch.object(voice_enrollment, 'extract_embedding') as extract:
                before = speaker_groups.speaker_groups_signature(project, [0])
                project['segments'][0]['text'] = 'edited translation'
                self.assertEqual(before, speaker_groups.speaker_groups_signature(project, [0]))
                self.assertNotEqual(before, speaker_groups.speaker_groups_signature(project, [1]))
                project['segments'][0]['end'] = 1.9
                self.assertNotEqual(before, speaker_groups.speaker_groups_signature(project, [0]))
                extract.assert_not_called()

    def test_audio_grouping_is_private_and_never_applies_voice_choices(self):
        with tempfile.TemporaryDirectory(prefix='npk-speaker-unit-') as temporary:
            folder = Path(temporary)
            source = folder / 'source.wav'
            source.write_bytes(b'input fixture')
            project = dict(media={'path': str(source)}, params={'workload_mode': 'quiet'}, segments=[
                dict(start=0, end=1, text='ngắn', voice_id='chosen:keep'),
                dict(start=1, end=3, text='một'), dict(start=3, end=5, text='hai'),
                dict(start=5, end=7, text='ba')])
            before = copy.deepcopy(project)
            def run(command, **kwargs):
                wav(command[-1], frames=2 * 16000, rate=16000)
                return types.SimpleNamespace(returncode=0)
            events = []
            core = types.SimpleNamespace(binary=lambda name: 'ffmpeg.exe', NO_WINDOW=0)
            pacer = types.SimpleNamespace(rest=lambda began: None)
            with patch.dict('sys.modules', {'core': core}), \
                    patch.object(voice_enrollment, 'enrollment_status', return_value={'available': True}), \
                    patch.object(voice_enrollment, 'extract_embedding', side_effect=[vector(0), vector(1), vector(0)]), \
                    patch.object(speaker_groups.subprocess, 'run', side_effect=run), \
                    patch('performance.FramePacer', return_value=pacer):
                result = speaker_groups.analyze_speakers(project, folder, progress=events.append)
            self.assertEqual(project, before)
            self.assertFalse(result['automatic_voice_assignment'])
            self.assertEqual(result['analyzed'], 3)
            self.assertEqual([group['segment_indices'] for group in result['groups']], [[1, 3], [2]])
            self.assertEqual(result['skipped'][0]['segment_index'], 0)
            self.assertEqual(events[-1]['progress'], 1)
            self.assertEqual(json.loads((folder / 'speaker-groups.json').read_text(encoding='utf-8'))['analyzed'], 3)
            self.assertFalse(list(folder.glob('npk-speaker-groups-*')))

    def test_selection_bound_is_checked_before_decode(self):
        project = {'segments': [dict(start=0, end=2)], 'media': {'path': 'missing'}, 'params': {}}
        core = types.SimpleNamespace(binary=lambda name: 'ffmpeg.exe', NO_WINDOW=0)
        with tempfile.TemporaryDirectory() as temporary, patch.dict('sys.modules', {'core': core}), \
                patch.object(voice_enrollment, 'enrollment_status', return_value={'available': True}), \
                patch.object(speaker_groups.subprocess, 'run') as run:
            for indices in ([], [0, 0], [True], [1], [0] * 201):
                with self.subTest(indices=str(indices)[:20]), self.assertRaises(ValueError):
                    speaker_groups.analyze_speakers(project, temporary, segment_indices=indices)
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
