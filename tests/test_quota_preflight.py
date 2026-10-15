"""Connection probes and readiness use the same safe execution decision."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server
from core import DATA
from execution_admission import AdmissionError, PROTECTED_PROJECT_ID


class QuotaPreflightTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory(dir=DATA)))
        self.monitor = self.folder/'monitor.json'
        self.project = {'id': PROTECTED_PROJECT_ID, 'source_revision': 'test-source',
                        'params': {'provider': 'deepseek', 'pipeline_translate': True,
                                   'subtitle_automation': 'local'}}
        self.monitor.write_text(json.dumps({'project_id':PROTECTED_PROJECT_ID,
            'audit_hold':False, 'quota_scope':'unknown', 'last_http_status':429,
            'last_attempt_utc':1, 'quota_connection':{'provider':'gemini'}}))
        self.enterContext(patch('server.project_by_id', return_value=self.project))
        self.enterContext(patch('server.all_items', return_value=[]))
        self.enterContext(patch('execution_admission.default_monitor_path', return_value=self.monitor))

    def test_preflight_includes_admission_and_blocks_before_click(self):
        self.project['params']['provider']='gemini'
        with patch('preflight.inspect', return_value={'ok':True,'errors':[],'warnings':[]}):
            result=server.preflight({'project_id':PROTECTED_PROJECT_ID})
        self.assertFalse(result['ok'])
        self.assertEqual(result['execution_admission']['blocked_code'],'quota_recovery_required')
        self.assertIn(result['execution_admission']['blocked_reason'],result['errors'])

    def test_other_connection_ready_is_not_blocked_by_old_quota(self):
        with patch('preflight.inspect', return_value={'ok':True,'errors':[],'warnings':[]}):
            result=server.preflight({'project_id':PROTECTED_PROJECT_ID})
        self.assertTrue(result['ok'])
        self.assertTrue(result['execution_admission']['execution_allowed'])
        self.assertFalse(result['execution_admission']['quota_connection_applies'])

    def test_probe_checks_requested_provider_before_vault_without_editor_mutation(self):
        before=copy.deepcopy(self.project)
        with patch('server.keyring.get_password') as vault:
            with self.assertRaises(AdmissionError) as caught:
                server.keys_check('gemini', {'project_id':PROTECTED_PROJECT_ID,
                    'mode':'generation','confirm_cost':True,'model':'gemini-test'})
            vault.assert_not_called()
        self.assertEqual(caught.exception.report['blocked_code'],'quota_recovery_required')
        self.assertEqual(self.project,before)

    def test_probe_checks_requested_router_endpoint_without_persisting(self):
        before=copy.deepcopy(self.project)
        self.monitor.write_text(json.dumps({'project_id':PROTECTED_PROJECT_ID,'audit_hold':False,
            'quota_scope':'unknown','quota_connection':{'provider':'router9',
            'endpoint':'http://127.0.0.1:20129/v1'}}))
        with patch('server.keyring.get_password') as vault:
            with self.assertRaises(AdmissionError):
                server.keys_check('router9',{'project_id':PROTECTED_PROJECT_ID,
                    'mode':'generation','confirm_cost':True,'model':'test/route',
                    'base_url':'http://127.0.0.1:20129/v1'})
            vault.assert_not_called()
        self.assertEqual(self.project,before)
