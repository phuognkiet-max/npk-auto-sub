"""Enrollment publication safety and custom voice catalog without model inference."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import numpy as np
import voice_enrollment as enrollment
import vieneu_voice
from test_vieneu_voice import CatalogFixture, wav


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='npk-enrollment-unit-')
        self.root = Path(self.temporary.name)
        self.folder = self.root / 'job'
        self.library = self.root / 'custom-voices'
        self.folder.mkdir()
        self.paths = patch.object(enrollment, '_library', return_value=self.library)
        self.paths.start()
        self.identifier = 'vieneu:custom:' + uuid.uuid4().hex
        self.create_fixture()

    def tearDown(self):
        self.paths.stop()
        self.temporary.cleanup()

    def create_fixture(self):
        np.savetxt(self.folder / 'speaker.emb.txt', np.ones((1, 192)), delimiter=',')
        np.savetxt(self.folder / 'ref_codes.txt', np.ones((38, 16)), fmt='%d')
        wav(self.folder / 'reference.wav', frames=3 * 48000)
        metadata = dict(schema=1, id=self.identifier, name='Giọng riêng của tôi', reference_duration=3,
            assets_sha256={filename: enrollment._sha256(self.folder / filename)
                          for filename in ('reference.wav', 'ref_codes.txt', 'speaker.emb.txt')})
        (self.folder / 'voice.json').write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
        return metadata

    def test_verified_registration_is_atomic_idempotent_and_discoverable(self):
        self.assertEqual(enrollment.verify_enrollment(self.folder)['id'], self.identifier)
        self.assertEqual(enrollment.register_voice(self.folder), self.identifier)
        self.assertEqual(enrollment.register_voice(self.folder), self.identifier)
        rows = enrollment.custom_voices()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], self.identifier)
        self.assertTrue(rows[0]['is_custom'])
        self.assertFalse(list(self.library.glob('.enroll-*')))

    def test_zero_embedding_and_wrong_code_dimensions_cannot_publish(self):
        for name, content in [('speaker.emb.txt', ','.join(['0'] * 192)), ('ref_codes.txt', '1 2 3\n'),
                              ('ref_codes.txt', ' '.join(['9000'] * 16))]:
            with self.subTest(name=name):
                self.create_fixture()
                (self.folder / name).write_text(content, encoding='utf-8')
                with self.assertRaises(ValueError):
                    enrollment.register_voice(self.folder)
                self.assertFalse(self.library.exists())

    def test_asset_change_hash_missing_and_invalid_ids_rejected(self):
        metadata = self.create_fixture()
        (self.folder / 'reference.wav').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            enrollment.register_voice(self.folder)
        metadata = self.create_fixture()
        metadata['assets_sha256'].pop('reference.wav')
        (self.folder / 'voice.json').write_text(json.dumps(metadata), encoding='utf-8')
        with self.assertRaises(ValueError):
            enrollment.register_voice(self.folder)
        metadata = self.create_fixture()
        metadata['id'] = 'vieneu:custom:../../outside'
        (self.folder / 'voice.json').write_text(json.dumps(metadata), encoding='utf-8')
        with self.assertRaises(ValueError):
            enrollment.register_voice(self.folder)

    def test_corrupt_or_short_reference_cannot_publish_even_with_matching_hash(self):
        metadata = self.create_fixture()
        wav(self.folder / 'reference.wav', frames=48000)
        metadata['assets_sha256']['reference.wav'] = enrollment._sha256(self.folder / 'reference.wav')
        (self.folder / 'voice.json').write_text(json.dumps(metadata), encoding='utf-8')
        with self.assertRaises(ValueError):
            enrollment.register_voice(self.folder)

    def test_bad_names_and_missing_install_are_clear_errors(self):
        for name in ('', 'x' * 81, 'a\nuser'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                enrollment._validate_name(name)
        with patch.object(enrollment, 'MODEL', self.root / 'missing-model'):
            self.assertFalse(enrollment.enrollment_status()['available'])


class CustomCatalogTests(CatalogFixture):
    def test_catalog_can_resolve_valid_private_voice_alongside_25_presets(self):
        library = self.root / 'own-library'
        folder = library / ('a' * 32)
        folder.mkdir(parents=True)
        np.savetxt(folder / 'speaker.emb.txt', np.ones((1, 192)), delimiter=',')
        np.savetxt(folder / 'ref_codes.txt', np.ones((38, 16)), fmt='%d')
        (folder / 'voice.json').write_text(json.dumps(dict(schema=1, id='vieneu:custom:' + 'a' * 32, name='Giọng nhà')),
                                         encoding='utf-8')
        with patch.object(enrollment, '_library', return_value=library):
            rows = vieneu_voice.entries()
            row = vieneu_voice.resolve_voice('vieneu:custom:' + 'a' * 32, require_ready=True)
            self.assertEqual(len(rows), 26)
            self.assertEqual(vieneu_voice._voice_directory(row), folder)
            self.assertEqual(row['name'], 'Giọng nhà')
            old_identity = row['cache_identity']
            (folder / 'speaker.emb.txt').write_text(','.join(['2'] * 192), encoding='utf-8')
            self.assertNotEqual(vieneu_voice.resolve_voice(row['id'])['cache_identity'], old_identity)


if __name__ == '__main__':
    unittest.main()
