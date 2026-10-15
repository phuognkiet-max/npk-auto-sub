"""Connection probes recheck admission after scheduler waits, before HTTP."""
import copy
import unittest
from unittest.mock import patch

import httpx
import core
import execution_admission
import providers
import server


class ConnectionProbeAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.project = {'id': 'fixture-probe-project', 'params': copy.deepcopy(core.DEFAULTS)}
        self.body = {'project_id': self.project['id'], 'mode': 'generation', 'confirm_cost': True,
                     'model': 'oc/muse-spark-1.3-contributor-free',
                     'base_url': 'http://127.0.0.1:20129/v1'}

    def test_invalid_model_rejected_before_vault(self):
        with patch('server.keyring.get_password') as vault:
            with self.assertRaises(ValueError):
                server.keys_check('router9', self.body | {'model': 'invalid\nmodel'})
        vault.assert_not_called()

    def test_hold_applied_after_precheck_stops_probe_without_sending_or_recording_success(self):
        calls = []
        original = providers.generate
        blocked = execution_admission.AdmissionError({'execution_allowed': False,
            'blocked_code': 'audit_hold', 'blocked_reason': 'Fixture hold changed during wait'})
        def generate_with_fixture(*args, **options):
            options['client'] = client
            return original(*args, **options)
        with httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request))) as client, \
                patch('server.project_by_id', return_value=self.project), \
                patch('server.all_items', return_value=[]), \
                patch('server.active_key_id', return_value='b' * 32), \
                patch('server.keyring.get_password', return_value='TEST-ONLY'), \
                patch('server.record_check') as record, \
                patch('providers.generate', side_effect=generate_with_fixture), \
                patch('execution_admission.require_admission', side_effect=[None, blocked]) as admission:
            with self.assertRaises(execution_admission.AdmissionError):
                server.keys_check('router9', self.body)
        self.assertEqual(admission.call_count, 2)
        probe = admission.call_args.args[0]
        self.assertEqual(probe['params']['provider'], 'router9')
        self.assertEqual(probe['params']['translation_model'], self.body['model'])
        self.assertEqual(probe['params']['router9_base_url'], self.body['base_url'])
        self.assertFalse(calls)
        record.assert_not_called()
        self.assertEqual(self.project['params'], core.DEFAULTS)
