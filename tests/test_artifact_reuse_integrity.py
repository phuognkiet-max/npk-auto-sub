"""Verified artifact reuse rejects changed bytes without decoding during UI polls.

Run through tools/run_tests.py --profile media; every fixture lives in private DATA.
"""
import array
import copy
from contextlib import ExitStack
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import uuid
import wave

import core
import preflight
import resource_budget
import worker
from job_progress import verify_outputs
from stage_journal import artifact_integrity, load, record_reuse, record_stage, require_reusable_artifact
from subtitle_quality import clean_ready, coverage_record, speech_ready


class ArtifactReuseIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.data = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=core.DATA)))
        (self.data / 'jobs').mkdir()
        for module in (core, worker, resource_budget):
            self.stack.enter_context(patch.object(module, 'DATA', self.data))
        source = self.data / 'source.mp4'
        shutil.copyfile(core.ROOT / 'tests/fixtures/demo.mp4', source)
        self.p = {'id': uuid.uuid4().hex, 'source_revision': uuid.uuid4().hex,
            'media': core.probe(source), 'regions': [], 'extraction_regions': [], 'artifacts': {},
            'segments': [{'start': 0., 'end': 8., 'text': 'Source', 'translated': 'Đủ lời'}],
            'params': core.DEFAULTS | {'use_clean': False, 'burn_subtitles': False,
                'pipeline_extract': 'existing', 'pipeline_tts': False, 'pipeline_translate': False,
                'voice_gain': 1, 'original_gain': 0, 'tts_timing_mode': 'natural'}}
        self.p['coverage'] = coverage_record(self.p, 'asr', 0., 8.)
        self.owner_id = uuid.uuid4().hex
        self.owner = self.data / 'jobs' / self.owner_id
        self.owner.mkdir()
        with wave.open(str(self.owner / 'voice.wav'), 'wb') as stream:
            stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            stream.writeframes(array.array('h', [1000, -1000] * 64000).tobytes())
        result = {'files': ['voice.wav'], 'type': 'tts', 'sample_rate': 16000,
                  'speech_signature': worker.speech_signature(self.p)}
        result['verification'] = verify_outputs(result, self.p, {'kind': 'tts'}, self.owner)
        self.p['artifacts']['tts'] = result | {'job_id': self.owner_id}

    def corrupt_voice(self):
        path = self.owner / 'voice.wav'
        before = path.stat()
        payload = bytearray(path.read_bytes())
        payload[-2] ^= 1  # Same-length WAV remains decodable; identity must catch it.
        path.write_bytes(payload)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))

    def test_verified_identity_is_carried_without_media_decode_during_polling(self):
        with patch('job_progress.verify_outputs', side_effect=AssertionError('UI must not decode')):
            self.assertTrue(speech_ready(self.p))
            self.assertTrue(speech_ready(self.p))
            successor = self.data / 'jobs' / uuid.uuid4().hex
            successor.mkdir()
            record_reuse(self.p, self.p, {'id': successor.name}, successor, 'tts', self.p['artifacts']['tts'])
        self.assertEqual(load(successor)['stages']['tts']['files'],
                         self.p['artifacts']['tts']['verification']['file_integrity'])

    def test_same_size_corruption_cannot_be_reanchored_in_successor_or_worker(self):
        original = copy.deepcopy(self.p['artifacts']['tts']['verification'])
        self.assertTrue(speech_ready(self.p))  # Prime the stat-scoped hash cache.
        self.corrupt_voice()
        self.assertFalse(speech_ready(self.p))
        self.assertEqual(artifact_integrity(self.p, 'tts')['status'], 'corrupt')
        successor = self.data / 'jobs' / uuid.uuid4().hex
        successor.mkdir()
        with self.assertRaisesRegex(ValueError, 'payload đã kiểm'):
            record_reuse(self.p, self.p, {'id': successor.name}, successor, 'tts', self.p['artifacts']['tts'])
        with patch('job_progress.verify_outputs', side_effect=AssertionError('Must not migrate known corruption')):
            with self.assertRaisesRegex(ValueError, 'lần kiểm chứng'):
                require_reusable_artifact(self.p, 'tts', allow_legacy_verification=True)
        self.assertFalse((successor / 'stage-journal.json').exists())
        self.assertEqual(self.p['artifacts']['tts']['verification'], original)
        self.assertTrue(any('lần kiểm chứng' in error for error in preflight.inspect(self.p, 'render')['errors']))
        with patch('worker.run', side_effect=AssertionError('Encoder must not read corrupt reused media')):
            with self.assertRaisesRegex(ValueError, 'lần kiểm chứng'):
                worker.render(self.p, {'kind': 'render'}, successor)

    def test_original_ledger_anchors_legacy_pipeline_artifact(self):
        result = copy.deepcopy(self.p['artifacts']['tts'])
        result.pop('job_id')
        result['verification'].pop('file_integrity')
        record_stage(self.p, self.p, {'id': self.owner_id}, self.owner, 'tts', result)
        self.p['artifacts']['tts']['verification'].pop('file_integrity')
        self.assertTrue(speech_ready(self.p))
        self.corrupt_voice()
        self.assertEqual(artifact_integrity(self.p, 'tts')['status'], 'corrupt')
        with self.assertRaises(ValueError):
            require_reusable_artifact(self.p, 'tts', allow_legacy_verification=True)

    def test_unanchored_standalone_is_verified_only_at_worker_admission(self):
        self.p['artifacts']['tts']['verification'].pop('file_integrity')
        old_project = copy.deepcopy(self.p)
        self.assertEqual(artifact_integrity(self.p, 'tts')['status'], 'legacy_unanchored')
        with patch('job_progress.verify_outputs', side_effect=AssertionError('No decoding in preflight')):
            self.assertFalse(speech_ready(self.p))
            plan = preflight.inspect(self.p, 'render')
            self.assertTrue(plan['ok'], plan['errors'])
            self.assertTrue(any('kiểm lại' in message for message in plan['warnings']))
        require_reusable_artifact(self.p, 'tts', allow_legacy_verification=True)
        self.assertTrue(speech_ready(self.p))
        self.assertIn('file_integrity', self.p['artifacts']['tts']['verification'])
        with patch('job_progress.verify_outputs', side_effect=AssertionError('Migration should be durable')):
            self.assertTrue(speech_ready(old_project))

    def test_truncated_legacy_payload_cannot_gain_verified_identity(self):
        artifact = self.p['artifacts']['tts']
        artifact['verification'].pop('file_integrity')
        old = copy.deepcopy(artifact['verification'])
        path = self.owner / 'voice.wav'
        path.write_bytes(path.read_bytes()[:-100])
        with self.assertRaises(ValueError):
            require_reusable_artifact(self.p, 'tts', allow_legacy_verification=True)
        self.assertEqual(artifact['verification'], old)
        self.assertFalse(speech_ready(self.p))

    def test_clean_payload_replacement_is_not_ready_with_matching_config(self):
        shutil.copyfile(self.p['media']['path'], self.owner / 'clean.mp4')
        result = {'type': 'clean', 'files': ['clean.mp4'], 'signature': core.signature(self.p)}
        result['verification'] = verify_outputs(result, self.p, {'kind': 'clean'}, self.owner)
        self.p['artifacts']['clean'] = result | {'job_id': self.owner_id}
        self.assertTrue(clean_ready(self.p))
        (self.owner / 'clean.mp4').write_bytes(b'replaced payload')
        self.assertFalse(clean_ready(self.p))
        self.p['params']['use_clean'] = True
        self.assertTrue(any('chưa có bản còn khớp' in error for error in preflight.inspect(self.p, 'render')['errors']))


if __name__ == '__main__':
    unittest.main()
