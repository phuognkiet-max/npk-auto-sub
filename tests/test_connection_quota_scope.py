"""Connection scoping is not a key/model rotation or audit-hold exemption."""
import json
from pathlib import Path
import tempfile
import unittest

from execution_admission import admission_status, PROTECTED_PROJECT_ID

NOW = 1791900000
ENDPOINT = 'http://127.0.0.1:20129/v1'


class ConnectionQuotaScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'monitor.json'
        self.project = {'id': PROTECTED_PROJECT_ID, 'source_revision': 'same-source',
                        'params': {'provider': 'router9', 'router9_base_url': ENDPOINT,
                                   'subtitle_automation': 'local', 'pipeline_translate': True}}
        self.monitor = {'project_id': PROTECTED_PROJECT_ID, 'audit_hold': False,
                        'quota_scope': 'unknown', 'last_http_status': 429,
                        'last_attempt_utc': NOW-100, 'earliest_retry_utc': NOW+7100,
                        'quota_connection': {'provider': 'gemini'}}

    def status(self, jobs=(), **kwargs):
        self.path.write_text(json.dumps(self.monitor), encoding='utf-8')
        before = self.path.read_bytes()
        result = admission_status(self.project, jobs, now=NOW, monitor_path=self.path, **kwargs)
        self.assertEqual(self.path.read_bytes(), before)
        return result

    def job(self, provider='gemini', status=429, ago=100, **kwargs):
        config = {'provider': provider, 'subtitle_automation': 'local', 'pipeline_translate': True}
        if provider == 'router9':
            config['router9_base_url'] = ENDPOINT
        return {'id': provider, 'project_id': self.project['id'], 'source_revision': 'same-source',
                'kind': 'pipeline', 'state': 'error', 'created': NOW-ago-20,
                'started': NOW-ago-19, 'ended': NOW-ago, 'http_status': status,
                'quota_scope': 'unknown', 'configuration': config, **kwargs}

    def test_known_gemini_quota_does_not_block_router(self):
        result = self.status([self.job()])
        self.assertTrue(result['execution_allowed'])
        self.assertEqual(result['quota_connection'], {'provider': 'gemini'})
        self.assertFalse(result['quota_connection_applies'])
        self.assertIsNone(result['earliest_retry_utc'])
        self.assertEqual(result['required_connections'], [{'provider': 'router9', 'endpoint': ENDPOINT}])

    def test_same_provider_different_model_remains_blocked(self):
        self.project['params'].update(provider='gemini', translation_model='another-model')
        self.assertEqual(self.status([self.job()])['blocked_code'], 'quota_recovery_required')

    def test_router_endpoint_identity_scopes_quota(self):
        self.monitor['quota_connection'] = {'provider': 'router9', 'endpoint': ENDPOINT}
        self.assertEqual(self.status()['blocked_code'], 'quota_recovery_required')
        self.project['params']['router9_base_url'] = 'http://127.0.0.1:20130/v1'
        self.assertTrue(self.status()['execution_allowed'])

    def test_changing_model_does_not_escape_router_quota(self):
        self.monitor['quota_connection'] = {'provider': 'router9', 'endpoint': ENDPOINT}
        for model in ('route/model-a', 'route/model-b'):
            self.project['params']['translation_model'] = model
            self.assertEqual(self.status()['blocked_code'], 'quota_recovery_required')

    def test_global_audit_hold_still_blocks_unrelated_connection(self):
        self.monitor['audit_hold'] = True
        result = self.status()
        self.assertEqual(result['blocked_code'], 'audit_hold')
        self.assertIn('mọi kết nối', result['blocked_reason'])

    def test_legacy_monitor_remains_fail_closed(self):
        del self.monitor['quota_connection']
        self.assertEqual(self.status([self.job()])['blocked_code'], 'quota_recovery_required')

    def test_unknown_current_provider_cannot_claim_exemption(self):
        del self.project['params']['provider']
        self.assertEqual(self.status()['blocked_code'], 'quota_recovery_required')

    def test_unknown_historical_config_cannot_claim_exemption(self):
        self.assertEqual(self.status([self.job(configuration={})])['blocked_code'], 'quota_recovery_required')
        self.assertEqual(self.status([self.job(kind='ocr', configuration={})])['blocked_code'], 'quota_recovery_required')

    def test_pipeline_failure_stage_identifies_actual_connection(self):
        mixed = {'provider': 'deepseek', 'subtitle_automation': 'gemini',
                 'pipeline_extract': 'ocr', 'pipeline_translate': True}
        self.project['params'].update(provider='deepseek')
        row = self.job(configuration=mixed, failure_stage='auto_subtitles')
        self.assertTrue(self.status([row])['execution_allowed'])
        row['failure_stage'] = 'translate'
        self.assertEqual(self.status([row])['blocked_code'], 'quota_recovery_required')

    def test_mixed_pipeline_needs_director_and_translation(self):
        self.project['params'].update(provider='deepseek', subtitle_automation='gemini', pipeline_extract='ocr')
        result = self.status()
        self.assertEqual(result['blocked_code'], 'quota_recovery_required')
        self.assertEqual(result['required_connections'], [{'provider': 'deepseek'}, {'provider': 'gemini'}])
        self.assertTrue(self.status(remaining_steps=['translate', 'tts'])['execution_allowed'])
        self.assertEqual(self.status(remaining_steps=['auto_subtitles'])['blocked_code'], 'quota_recovery_required')

    def test_standalone_translation_does_not_require_director(self):
        self.project['params'].update(subtitle_automation='gemini', pipeline_extract='ocr')
        self.assertTrue(self.status(kind='translate')['execution_allowed'])
        self.assertEqual(self.status(kind='ocr')['blocked_code'], 'quota_recovery_required')

    def test_unrelated_transient_error_does_not_cool_down_router(self):
        self.assertTrue(self.status([self.job(status=503)])['execution_allowed'])
        result = self.status([self.job(provider='router9', status=503)])
        self.assertEqual(result['blocked_code'], 'provider_cooldown')
        self.assertEqual(result['earliest_retry_utc'], NOW-100+1800)

    def test_router_error_does_not_erase_gemini_quota_for_mixed_pipeline(self):
        self.project['params'].update(subtitle_automation='gemini', pipeline_extract='ocr')
        self.assertEqual(self.status([self.job(), self.job(provider='router9', status=503)])['blocked_code'],
                         'quota_recovery_required')

    def test_recovery_for_gemini_cannot_confirm_router_quota(self):
        self.project['params'].update(subtitle_automation='gemini', pipeline_extract='ocr')
        self.monitor.update(last_attempt_utc=NOW-500, earliest_retry_utc=NOW-1,
                            quota_recovery_confirmed_at=NOW-1)
        self.assertEqual(self.status([self.job(ago=500), self.job(provider='router9', ago=600)])['blocked_code'],
                         'quota_recovery_required')

    def test_project_wide_counts_and_quota_cap_survive_switch(self):
        jobs = [self.job(ago=10000, automatic=True, id=str(i)) for i in range(3)]
        result = self.status(jobs, automatic=True)
        self.assertEqual(result['automatic_attempts_today'], 3)
        self.assertEqual(result['blocked_code'], 'automatic_daily_limit')
        self.assertTrue(self.status(jobs, automatic=False)['execution_allowed'])

    def test_busy_guard_remains_project_wide(self):
        self.assertEqual(self.status([self.job(state='running')])['blocked_code'], 'project_busy')

    def test_invalid_connection_metadata_fails_closed_and_never_leaks(self):
        identities = [None, {}, {'provider': 'made-up'}, {'provider': 'gemini', 'endpoint': ENDPOINT},
                      {'provider': 'router9'}, {'provider': 'router9', 'endpoint': ENDPOINT+'/'},
                      {'provider': 'router9', 'endpoint': 'https://name:PRIVATE_SECRET@example.com/v1'},
                      {'provider': 'gemini', 'key': 'PRIVATE_SECRET'}]
        for identity in identities:
            with self.subTest(identity=identity):
                self.monitor['quota_connection'] = identity
                result = self.status()
                self.assertEqual(result['blocked_code'], 'monitor_invalid')
                self.assertNotIn('PRIVATE_SECRET', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
