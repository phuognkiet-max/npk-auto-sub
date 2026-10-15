"""Completed v4 evidence is an existing asset, never a v5 decoder checkpoint."""
import copy
import json
import uuid
import unittest
from pathlib import Path
from unittest.mock import patch

import test_pipeline_semantic_recovery as fixtures
from ocr_checkpoint import _signature, signature, validate_resume
from pipeline_resume import recovery_snapshot
from translation_checkpoint import write_checkpoint


class LegacyCompletedOCRAssetTests(unittest.TestCase):
    write = fixtures.CompleteOCRCloudRecoveryTests.write
    recover = fixtures.CompleteOCRCloudRecoveryTests.recover

    def setUp(self):
        fixtures.CompleteOCRCloudRecoveryTests.setUp(self)
        self.checkpoint['signature'] = _signature(self.original, version=4)
        self.manifest['checkpoint_signature'] = self.checkpoint['signature']
        self.write('recognition-checkpoint.json', self.checkpoint)
        self.write('ocr-tracks.json', self.manifest)

    def test_completed_v4_is_reused_without_rehash_or_writing_evidence(self):
        before = {p.name:p.read_bytes() for p in self.folder.iterdir()}
        snapshot, *_ = self.recover()
        provenance = snapshot['coverage']['recognition_provenance']
        self.assertEqual(provenance['checkpoint_version'], 4)
        self.assertEqual(provenance['checkpoint_signature'], self.checkpoint['signature'])
        self.assertNotEqual(provenance['checkpoint_signature'], signature(self.project))
        self.assertEqual(provenance['search_area'], 'legacy_bottom_40_percent_v4')
        self.assertFalse(provenance['full_frame_recognition'])
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.folder.iterdir()})
        with self.assertRaisesRegex(ValueError, 'không khớp'):
            validate_resume(self.folder/'recognition-checkpoint.json', self.project)

    def test_unknown_version_changed_regions_or_source_revision_cannot_claim_v4_asset(self):
        for change in ('signature', 'regions', 'revision'):
            with self.subTest(change=change):
                project = copy.deepcopy(self.project)
                saved = copy.deepcopy(self.checkpoint)
                if change == 'signature':
                    saved['signature'] = 'f'*64
                elif change == 'regions':
                    project['extraction_regions'] = [{'x':0,'y':0,'width':640,'height':360}]
                else:
                    project['source_revision'] = uuid.uuid4().hex
                self.write('recognition-checkpoint.json', saved)
                with self.assertRaises(ValueError):
                    recovery_snapshot(self.row, project, [self.row], self.folder)

    def test_v4_partial_cannot_be_used_as_complete_asset(self):
        self.checkpoint['next_time'] = 2.
        self.checkpoint['decoded_complete'] = True
        self.write('recognition-checkpoint.json', self.checkpoint)
        with self.assertRaises(ValueError):
            self.recover()

    def test_v4_eof_missing_or_replaced_source_is_rejected(self):
        self.checkpoint['decoded_complete'] = False
        self.write('recognition-checkpoint.json', self.checkpoint)
        with self.assertRaises(ValueError):
            self.recover()
        self.checkpoint['decoded_complete'] = True
        self.write('recognition-checkpoint.json', self.checkpoint)
        Path(self.project['media']['path']).write_bytes(b'replaced source identity')
        with self.assertRaisesRegex(ValueError, 'không khớp'):
            self.recover()

    def test_saved_translations_and_manual_values_survive_provider_change(self):
        snapshot, *_ = self.recover()
        self.project['segments'] = copy.deepcopy(snapshot['segments'])
        self.project['coverage'] = copy.deepcopy(snapshot['coverage'])
        self.project['segments'][1].update(translated='Manual translation', speech_enabled=False)
        child = dict(self.row, id=uuid.uuid4().hex, created=4., continuation_from=self.row['id'])
        child_folder = self.data/'jobs'/child['id']
        child_folder.mkdir()
        original = copy.deepcopy(snapshot)
        (child_folder/'input.json').write_text(json.dumps({'project':original, 'job':child}), encoding='utf-8')
        rows = copy.deepcopy(original['segments'])
        for i in range(5):
            rows[i]['translated'] = f'Saved translation {i}'
        write_checkpoint(child_folder/'translation-checkpoint.json', original, rows, list(range(5)))
        self.project['params'].update(provider='router9', translation_model='route/model',
                                      router9_base_url='http://127.0.0.1:20129/v1')
        before = copy.deepcopy(self.project)
        with patch('pipeline_resume._pending_selection', return_value=False):
            recovered, root, restored, missing, _ = recovery_snapshot(child, self.project, [self.row,child], child_folder)
        self.assertEqual(len(recovered['segments']), 8)
        self.assertEqual(recovered['segments'][1], self.project['segments'][1])
        self.assertEqual(recovered['segments'][0]['translated'], 'Saved translation 0')
        self.assertEqual(restored, 4)
        self.assertEqual(missing, [5,6,7])
        self.assertEqual(root, self.row)
        self.assertEqual(before, self.project)
