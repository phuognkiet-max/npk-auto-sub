"""Per-paragraph publication survives later failure, with synthetic PCM only."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import narration
from core import DEFAULTS


def signal(path):
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        output.writeframes(b'\xd2\x04' * 4800)


class NarrationRecoveryTests(unittest.TestCase):
    def test_late_failure_promotes_only_good_paragraph_and_retry_reuses_it(self):
        entry = dict(id='vieneu:fixture', name='Fixture', provider='vieneu')
        calls = []
        def generate(batch, params=None, progress=None):
            calls.append([item['text'] for item in batch])
            if len(calls) == 1:
                signal(batch[0]['path'])
                Path(batch[1]['path']).write_bytes(b'partial WAV')
                raise ValueError('late broker failure')
            for item in batch:
                signal(item['path'])
            return [{'engine_metadata_complete': True} for item in batch]
        with tempfile.TemporaryDirectory(prefix='npk-paragraph-recovery-') as temporary:
            folder = Path(temporary)
            with patch.object(narration, 'DATA', folder), patch('voices.resolve_voice', return_value=entry), \
                    patch('voices.audio_identity', return_value=['fixture-identity']), \
                    patch('voices.synthesize_vieneu_batch', side_effect=generate):
                rows = [dict(text='Một.'), dict(text='Hai.')]
                with self.assertRaisesRegex(ValueError, 'late broker'):
                    narration.paragraph_audio(rows, DEFAULTS, folder)
                self.assertEqual(len(list((folder/'speech-cache').glob('*.wav'))), 1)
                paths, metadata, hits = narration.paragraph_audio(rows, DEFAULTS, folder)
                self.assertEqual(hits, 1)
                self.assertEqual(calls, [['Một.', 'Hai.'], ['Hai.']])
                self.assertTrue(all(narration.valid_wave(path) for path in paths))
                self.assertTrue(all(info['lexical_verification'] == 'unverified' for info in metadata))
                self.assertFalse(list(folder.glob('*.part.wav')))

    def test_identity_change_never_promotes_recovered_pcm_to_old_cache(self):
        entry = dict(id='vieneu:fixture', name='Fixture', provider='vieneu')
        def generate(batch, params=None, progress=None):
            signal(batch[0]['path'])
            raise ValueError('late failure')
        with tempfile.TemporaryDirectory(prefix='npk-paragraph-identity-') as temporary:
            folder = Path(temporary)
            with patch.object(narration, 'DATA', folder), patch('voices.resolve_voice', return_value=entry), \
                    patch('voices.audio_identity', side_effect=[['old'], ['old'], ['new']]), \
                    patch('voices.synthesize_vieneu_batch', side_effect=generate), self.assertRaisesRegex(ValueError, 'late failure'):
                narration.paragraph_audio([dict(text='Một.')], DEFAULTS, folder)
            self.assertFalse(list((folder/'speech-cache').glob('*.wav')))


if __name__ == '__main__':
    unittest.main()
