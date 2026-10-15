"""A settled, translated continuation needs neither cloud quota nor a model."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import core
import execution_admission as admission
import worker


class OfflinePipelineAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory(dir=core.DATA)))
        self.source = self.folder/'source.mp4'
        self.source.write_bytes(b'private source fixture')
        self.p = dict(id=uuid.uuid4().hex, name='Offline fixture', updated=1., source_revision='one',
            media=dict(path=str(self.source), duration=20., width=1280, height=720, fps=25., audio=True),
            regions=[], artifacts={}, segments=[dict(start=0., end=2., text='Source', translated='Bản dịch')],
            params=core.DEFAULTS | dict(provider='router9', translation_model='old/model',
                router9_base_url='http://127.0.0.1:20129/v1', pipeline_extract='existing',
                pipeline_translate=True, subtitle_automation='router9', pipeline_tts=False,
                pipeline_clean=False, use_clean=False, burn_subtitles=False, voice_gain=0))
        self.job = dict(id=uuid.uuid4().hex, kind='pipeline', project_revision=1.)
        self.monitor = self.folder/'monitor.json'
        self.monitor.write_text(json.dumps(dict(project_id=self.p['id'], audit_hold=True,
            quota_scope='unknown', quota_connection=dict(provider='router9', endpoint=self.p['params']['router9_base_url']))))
        self.enterContext(patch('execution_admission.default_monitor_path', return_value=self.monitor))
        self.status = dict(available=True, mode='router9', status='selected', can_process=False,
                           requires_review=False, manifest_key='verified-evidence')
        self.enterContext(patch('auto_subtitle_director.automation_status', side_effect=lambda p: self.status))

    def test_settled_translated_pipeline_does_not_require_saved_model_or_quota(self):
        original = copy.deepcopy(self.p)
        changed = copy.deepcopy(self.p)
        changed['params']['translation_model'] = 'new/model'
        with patch('core.get', return_value=changed), patch('core.all_items', return_value=[]), \
                patch('credentials.active_secret') as vault:
            self.assertEqual(worker.pipeline_provider_steps(self.p, 'pipeline'), [])
            worker.require_execution_admission(self.p, self.job)
        self.assertEqual(self.p, original)
        vault.assert_not_called()

    def test_pending_director_still_blocks_under_hold(self):
        self.p['params']['pipeline_translate'] = False
        self.status.update(status='can_process', can_process=True)
        with patch('core.get', return_value=self.p), patch('core.all_items', return_value=[]):
            self.assertEqual(worker.pipeline_provider_steps(self.p, 'pipeline'), ['auto_subtitles'])
            with self.assertRaises(admission.AdmissionError) as caught:
                worker.require_execution_admission(self.p, self.job)
        self.assertEqual(caught.exception.report['blocked_code'], 'audit_hold')

    def test_unavailable_or_malformed_evidence_never_exempts_director(self):
        self.p['params']['pipeline_translate'] = False
        for status in ({}, dict(available=False), self.status | {'can_process': None},
                       self.status | {'status': 'can_process'}, self.status | {'mode': 'gemini'}):
            self.status = status
            with patch('core.get', return_value=self.p), patch('core.all_items', return_value=[]):
                self.assertIsNone(worker.pipeline_provider_steps(self.p, 'pipeline'))
                with self.assertRaises(admission.AdmissionError):
                    worker.require_execution_admission(self.p, self.job)

    def test_future_ocr_and_missing_translation_keep_provider_work(self):
        self.p['params']['pipeline_extract'] = 'ocr'
        self.assertEqual(worker.pipeline_provider_steps(self.p, 'pipeline'), ['ocr', 'auto_subtitles', 'translate'])
        self.p['params']['pipeline_extract'] = 'existing'
        self.p['segments'][0]['translated'] = ''
        self.assertEqual(worker.pipeline_provider_steps(self.p, 'pipeline'), ['translate'])

    def test_preflight_derives_steps_instead_of_trusting_client(self):
        import server
        with patch('server.project_by_id', return_value=self.p), patch('server.all_items', return_value=[]), \
                patch('preflight.inspect', return_value=dict(ok=True, errors=[], warnings=[])):
            result = server.preflight(dict(project_id=self.p['id'], kind='pipeline', remaining_steps=['translate']))
            self.assertTrue(result['execution_admission']['execution_allowed'])
            self.status.update(status='can_process', can_process=True)
            result = server.preflight(dict(project_id=self.p['id'], kind='pipeline', remaining_steps=[]))
            self.assertEqual(result['execution_admission']['blocked_code'], 'audit_hold')

    def test_enqueue_complete_pipeline_avoids_vault_and_fresh_budget(self):
        import server
        core.put('project', self.p['id'], self.p)
        with patch('preflight.inspect', return_value=dict(ok=True, errors=[], warnings=[])), \
                patch('server.active_secret') as vault:
            job = server.create_job(dict(project_id=self.p['id'], kind='pipeline',
                                         expected_revision=1., replace_segments=True))
        self.assertNotIn('credential_ref', job)
        self.assertNotIn('director_credential_ref', job)
        self.assertNotIn('api_usage_lineage', job)
        vault.assert_not_called()
