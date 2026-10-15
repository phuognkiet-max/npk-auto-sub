"""One execution decision is safe at inspection, queue creation and dispatch."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from execution_admission import (AdmissionError, PROTECTED_PROJECT_ID, ROOT,
    admission_status, default_monitor_path, provider_required, require_admission)

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc).timestamp()


class ExecutionAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.monitor = Path(self.temp.name) / 'monitor.json'
        self.project = {'id': PROTECTED_PROJECT_ID, 'source_revision': 'source-one',
                        'params': {'pipeline_translate': True, 'subtitle_automation': 'local'}}
        self.state = {'project_id': PROTECTED_PROJECT_ID, 'audit_hold': False,
                      'quota_scope': 'requests_per_minute', 'automatic_attempts_by_local_date': {}}
        self.write()

    def write(self, **changes):
        self.state.update(changes)
        self.monitor.write_text(json.dumps(self.state), encoding='utf-8')

    def job(self, identifier='job', *, status=503, ago=3601, **changes):
        row = {'id': identifier, 'kind': 'pipeline', 'state': 'error',
               'project_id': self.project['id'], 'source_revision': self.project['source_revision'],
               'created': NOW-ago-20, 'started': NOW-ago-19, 'ended': NOW-ago,
               'http_status': status, 'configuration': dict(self.project['params'])}
        row.update(changes)
        return row

    def status(self, jobs=(), **options):
        return admission_status(self.project, list(jobs), now=NOW, monitor_path=self.monitor, **options)

    def test_content_validity_is_not_assumed_by_admission(self):
        report = self.status()
        self.assertTrue(report['execution_allowed'])
        self.assertNotIn('can_continue', report)
        self.assertNotIn('content_ready', report)

    def test_hold_blocks_api_even_after_cooldown_and_recovery(self):
        self.write(audit_hold=True, quota_scope='unknown', quota_recovery_confirmed_at=NOW-1)
        report = self.status([self.job(status=429, ago=9000)])
        self.assertFalse(report['execution_allowed'])
        self.assertEqual(report['blocked_code'], 'audit_hold')

    def test_hold_does_not_block_offline_work_or_touch_monitor(self):
        self.monitor.write_text('malformed', encoding='utf-8')
        for kind in ('tts', 'clean', 'render', 'preview', 'asr'):
            with self.subTest(kind=kind):
                self.assertTrue(self.status(kind=kind)['execution_allowed'])
        self.assertEqual(self.monitor.read_text(), 'malformed')

    def test_remaining_offline_steps_do_not_request_provider(self):
        self.write(audit_hold=True)
        report = self.status(remaining_steps=['tts', 'clean', 'render'])
        self.assertTrue(report['execution_allowed'])
        self.assertFalse(report['provider_required'])

    def test_director_needs_provider_for_ocr_and_selection(self):
        self.project['params'].update(pipeline_translate=False, subtitle_automation='gemini', pipeline_extract='ocr')
        for kind in ('ocr', 'auto_subtitles', 'pipeline'):
            self.assertTrue(provider_required(self.project, kind))
        self.assertFalse(provider_required(self.project, 'asr'))
        self.assertTrue(provider_required(self.project, 'pipeline', ['auto_subtitles']))
        self.assertFalse(provider_required(self.project, 'pipeline', ['render']))

    def test_spoken_optimization_and_existing_gemini_selection_are_provider_work(self):
        self.assertTrue(provider_required(self.project, 'optimize_spoken'))
        self.project['params'].update(pipeline_translate=False, subtitle_automation='gemini', pipeline_extract='existing')
        self.assertTrue(provider_required(self.project, 'pipeline'))
        self.write(audit_hold=True)
        self.assertEqual(self.status(kind='optimize_spoken')['blocked_code'], 'audit_hold')
        self.assertEqual(self.status(kind='pipeline')['blocked_code'], 'audit_hold')

    def test_missing_target_monitor_fails_closed(self):
        self.monitor.unlink()
        self.assertEqual(self.status()['blocked_code'], 'monitor_missing')

    def test_malformed_target_monitor_fails_closed(self):
        for content in ('{', '[]', '{"project_id":null}', '{"project_id":"'+PROTECTED_PROJECT_ID+'","audit_hold":"false"}'):
            with self.subTest(content=content):
                self.monitor.write_text(content)
                self.assertEqual(self.status()['blocked_code'], 'monitor_invalid')

    def test_nan_infinity_or_invalid_timestamp_fails_closed(self):
        for value in (True, -1, float('nan'), float('inf'), 1e100, 'yesterday'):
            with self.subTest(value=value):
                self.write(last_attempt_utc=value)
                self.assertEqual(self.status()['blocked_code'], 'monitor_invalid')

    def test_invalid_unknown_scope_fails_closed(self):
        self.write(quota_scope='all-good')
        self.assertEqual(self.status()['blocked_code'], 'monitor_invalid')

    def test_unrelated_project_does_not_read_protected_hold(self):
        self.write(audit_hold=True, quota_scope='unknown')
        self.project['id'] = 'another-project'
        self.assertTrue(self.status()['execution_allowed'])
        self.monitor.write_text('malformed')
        self.assertTrue(self.status()['execution_allowed'])

    def test_valid_monitor_matching_nondefault_project_still_fails_closed(self):
        self.project['id'] = 'another-project'
        self.write(project_id=self.project['id'], audit_hold='false')
        self.assertEqual(self.status()['blocked_code'], 'monitor_invalid')

    def test_old_monitor_for_different_source_requires_check(self):
        self.write(source_revision='source-zero')
        self.assertEqual(self.status()['blocked_code'], 'monitor_source_changed')

    def test_old_source_jobs_do_not_change_cooldown(self):
        self.assertTrue(self.status([self.job(status=429, ago=10, source_revision='source-zero')])['execution_allowed'])

    def test_manual_newest_attempt_sets_transient_cooldown(self):
        old = self.job('old', ago=4000)
        manual = self.job('manual', ago=100, automatic=False)
        report = self.status([manual, old])
        self.assertEqual(report['blocked_code'], 'provider_cooldown')
        self.assertEqual(report['latest_job_id'], 'manual')
        self.assertEqual(report['earliest_retry_utc'], manual['ended']+1800)

    def test_quota_cooldown_is_at_least_two_hours(self):
        row = self.job(status=429, ago=7199, quota_scope='requests_per_minute')
        self.assertEqual(self.status([row])['blocked_code'], 'provider_cooldown')
        row['ended'] -= 1
        self.assertTrue(self.status([row])['execution_allowed'])

    def test_later_503_never_erases_unknown_429(self):
        quota = self.job('quota', status=429, ago=15000, quota_scope='unknown')
        transient = self.job('new503', status=503, ago=2000)
        self.write(quota_scope='unknown', last_http_status=503, last_attempt_utc=transient['ended'])
        report = self.status([quota, transient])
        self.assertEqual(report['blocked_code'], 'quota_recovery_required')
        self.assertEqual(report['quota_scope'], 'unknown')
        self.assertEqual(report['latest_job_id'], 'new503')
        self.assertEqual(report['earliest_retry_utc'], transient['ended']+7200)

    def test_daily_quota_not_unlocked_by_guessed_cooldown(self):
        self.write(quota_scope='daily', earliest_retry_utc=NOW-3600)
        self.assertEqual(self.status([self.job(status=429, ago=15000)])['blocked_code'], 'quota_recovery_required')

    def test_explicit_recovery_confirmed_after_429_allows_post_cooldown(self):
        self.write(quota_scope='daily', quota_recovery_confirmed_at=NOW-2)
        self.assertTrue(self.status([self.job(status=429, ago=15000)])['execution_allowed'])

    def test_stale_or_future_confirmation_cannot_unlock_new_429(self):
        for value in (NOW-16000, NOW+1):
            with self.subTest(value=value):
                self.write(quota_scope='unknown', quota_recovery_confirmed_at=value)
                self.assertEqual(self.status([self.job(status=429, ago=15000)])['blocked_code'], 'quota_recovery_required')

    def test_new_429_after_recovery_requires_confirmation_again(self):
        self.write(quota_scope='unknown', quota_recovery_confirmed_at=NOW-16000)
        old = self.job('old', status=429, ago=17000)
        new = self.job('new', status=429, ago=15000)
        self.assertEqual(self.status([old, new])['blocked_code'], 'quota_recovery_required')

    def test_network_and_all_transient_statuses_have_thirty_minute_cooldown(self):
        rows = [self.job(status=status, ago=1799) for status in (408,500,502,503,504)]
        rows.append(self.job(status=None, ago=1799, failure_code='provider_timeout'))
        for row in rows:
            with self.subTest(row=row['http_status']):
                self.assertEqual(self.status([row])['blocked_code'], 'provider_cooldown')
                row['ended'] -= 1
                self.assertTrue(self.status([row])['execution_allowed'])

    def test_provider_retry_after_can_extend_cooldown(self):
        row = self.job(ago=3000, retry_after=4000)
        self.assertEqual(self.status([row])['earliest_retry_utc'], row['ended']+4000)

    def test_old_retry_after_deadline_does_not_restart_on_newest_attempt(self):
        old = self.job('old', ago=30000, retry_after=12000)
        latest = self.job('latest', ago=3600)
        report = self.status([old, latest])
        self.assertTrue(report['execution_allowed'])
        self.assertEqual(report['earliest_retry_utc'], latest['ended']+1800)

    def test_monitor_newer_attempt_extends_cooldown(self):
        self.write(last_http_status=429, last_attempt_utc=NOW-100, quota_scope='requests_per_minute')
        report = self.status([self.job(status=429, ago=15000, quota_scope='requests_per_minute')])
        self.assertEqual(report['blocked_code'], 'provider_cooldown')
        self.assertEqual(report['earliest_retry_utc'], NOW-100+7200)

    def test_unknown_monitor_blocks_even_without_job_429_metadata(self):
        self.write(quota_scope='unknown')
        self.assertEqual(self.status()['blocked_code'], 'quota_recovery_required')

    def test_automatic_caps_use_bangkok_day_and_saved_counts(self):
        self.write(automatic_attempts_by_local_date={'2026-10-13':3})
        row = self.job(status=429, ago=15000, quota_scope='requests_per_minute')
        report = self.status([row], automatic=True)
        self.assertEqual(report['blocked_code'], 'automatic_daily_limit')
        self.assertEqual(report['automatic_attempts_today'], 3)
        self.assertEqual(report['earliest_retry_utc'], datetime(2026,10,13,17,0,tzinfo=timezone.utc).timestamp())
        self.assertTrue(self.status([row], automatic=False)['execution_allowed'])

    def test_automatic_jobs_and_ledger_are_not_double_counted(self):
        self.write(automatic_attempts_by_local_date={'2026-10-13':3})
        rows = [self.job(str(index), ago=10000+index*10, automatic=True) for index in range(3)]
        report = self.status(rows, automatic=True)
        self.assertTrue(report['execution_allowed'])
        self.assertEqual(report['automatic_attempts_today'], 3)
        rows.append(self.job('fourth', ago=10500, execution_origin='automation'))
        self.assertEqual(self.status(rows, automatic=True)['blocked_code'], 'automatic_daily_limit')

    def test_third_approved_current_automatic_job_does_not_charge_reservation_twice(self):
        prior = [self.job('first', status=429, ago=15000, automatic=True, quota_scope='requests_per_minute'),
                 self.job('second', status=503, ago=10000, automatic=True)]
        current = self.job('third', state='running', created=NOW-10, started=NOW-9, ended=None,
                           automatic=True, http_status=None)
        self.write(latest_job_id='third', automatic_attempts_by_local_date={'2026-10-13':3})
        report = self.status(prior+[current], automatic=True, current_job_id='third')
        self.assertTrue(report['execution_allowed'])
        self.assertEqual(report['automatic_attempts_today'], 2)
        # After this owner ends, a new fourth attempt gets no reservation credit.
        current.update(state='error', ended=NOW-8000, started=NOW-8020, created=NOW-8021)
        next_report = self.status(prior+[current], automatic=True)
        self.assertEqual(next_report['blocked_code'], 'automatic_daily_limit')
        self.assertEqual(next_report['automatic_attempts_today'], 3)

    def test_new_fourth_owned_reservation_still_exceeds_quota_daily_cap(self):
        prior = [self.job(str(i), status=429 if i == 0 else 503, ago=15000-i*2000,
                          automatic=True, quota_scope='requests_per_minute') for i in range(3)]
        current = self.job('fourth', state='queued', created=NOW-1, started=None, ended=None,
                           automatic=True, http_status=None)
        self.write(latest_job_id='fourth', automatic_attempts_by_local_date={'2026-10-13':4})
        report = self.status(prior+[current], automatic=True, current_job_id='fourth')
        self.assertEqual(report['blocked_code'], 'automatic_daily_limit')
        self.assertEqual(report['automatic_attempts_today'], 3)

    def test_changed_monitor_counter_is_rechecked_before_child_send(self):
        prior = [self.job('first', status=429, ago=15000, automatic=True, quota_scope='requests_per_minute'),
                 self.job('second', ago=10000, automatic=True)]
        current = self.job('third', state='running', created=NOW-10, started=NOW-9, ended=None,
                           automatic=True, http_status=None)
        self.write(latest_job_id='third', automatic_attempts_by_local_date={'2026-10-13':3})
        self.assertTrue(self.status(prior+[current], automatic=True, current_job_id='third')['execution_allowed'])
        self.write(automatic_attempts_by_local_date={'2026-10-13':4})
        report = self.status(prior+[current], automatic=True, current_job_id='third')
        self.assertEqual(report['blocked_code'], 'automatic_daily_limit')

    def test_reservation_credit_requires_exact_active_automatic_owner_and_day(self):
        quota = self.job('quota', status=429, ago=15000, quota_scope='requests_per_minute')
        base = self.job('current', state='running', created=NOW-10, started=NOW-9, ended=None,
                        automatic=True, http_status=None)
        cases = [('other-latest', base, 'another-job'),
                 ('manual', base | {'automatic':False}, 'current'),
                 ('finished', base | {'state':'done'}, 'current'),
                 ('previous-day', base | {'created':NOW-86410, 'started':NOW-86409}, 'current')]
        for name, current, latest in cases:
            with self.subTest(name=name):
                self.write(latest_job_id=latest, automatic_attempts_by_local_date={'2026-10-13':3})
                report = self.status([quota,current], automatic=True, current_job_id='current')
                self.assertEqual(report['blocked_code'], 'automatic_daily_limit')
                self.assertEqual(report['automatic_attempts_today'], 3)

    def test_manual_attempts_do_not_consume_automatic_cap(self):
        rows = [self.job(str(index), ago=10000+index*10) for index in range(5)]
        report = self.status(rows, automatic=True)
        self.assertTrue(report['execution_allowed'])
        self.assertEqual(report['automatic_attempts_today'], 0)

    def test_automatic_daily_count_uses_start_day_not_finish_day(self):
        # 16:59 UTC is 23:59 Bangkok; the call finishes after local midnight.
        now = datetime(2026,10,13,18,0,tzinfo=timezone.utc).timestamp()
        start = datetime(2026,10,13,16,59,tzinfo=timezone.utc).timestamp()
        end = datetime(2026,10,13,17,1,tzinfo=timezone.utc).timestamp()
        row = self.job(automatic=True, created=start-1, started=start, ended=end)
        report = admission_status(self.project, [row], now=now, automatic=True, monitor_path=self.monitor)
        self.assertTrue(report['execution_allowed'])
        self.assertEqual(report['automatic_attempts_today'], 0)

    def test_active_job_blocks_duplicate_but_dispatch_ignores_itself(self):
        row = self.job('owner', state='queued', started=None, ended=None)
        self.assertEqual(self.status([row])['blocked_code'], 'project_busy')
        self.assertTrue(self.status([row], current_job_id='owner')['execution_allowed'])

    def test_job_errors_or_monitor_extra_fields_never_leak_into_report(self):
        secret = 'PRIVATE_SECRET_RAW_PROVIDER_BODY'
        self.write(audit_hold=True, api_key=secret, raw_error=secret)
        report = self.status([self.job(message=secret, credential_ref={'key':secret})])
        self.assertNotIn(secret, json.dumps(report))
        self.assertNotIn('credential_ref', report)

    def test_require_guard_raises_safe_typed_error(self):
        self.write(audit_hold=True)
        with self.assertRaises(AdmissionError) as caught:
            require_admission(self.project, [], now=NOW, monitor_path=self.monitor)
        self.assertEqual(caught.exception.report['blocked_code'], 'audit_hold')
        self.assertEqual(str(caught.exception), caught.exception.report['blocked_reason'])

    def test_private_data_default_path_does_not_read_production(self):
        with patch.dict(os.environ, {'VIETDUB_DATA_DIR':self.temp.name}):
            self.assertEqual(default_monitor_path(), Path(self.temp.name)/'npk-pipeline-monitor-state.json')
        with patch.dict(os.environ, {'VIETDUB_DATA_DIR':str(ROOT/'data')}):
            self.assertEqual(default_monitor_path(), ROOT/'build/npk-pipeline-monitor-state.json')

    def test_no_production_writes_or_provider_imports(self):
        before = self.monitor.read_bytes()
        with patch('builtins.__import__', side_effect=AssertionError('must not import provider or store')):
            report = self.status([self.job(status=429, ago=15000, quota_scope='requests_per_minute')])
        self.assertTrue(report['execution_allowed'])
        self.assertEqual(self.monitor.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
