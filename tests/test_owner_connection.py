"""Queue-owner discovery/CLI tests with private stores and mocked HTTP only."""
import builtins
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

import owner_connection as owner
from tools import continue_pipeline_job as cli


PROJECT = 'a' * 32
JOB = 'b' * 32
NEXT = 'c' * 32
SESSION = 'fixture-local-auth-token-only'


class OwnerConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['VIETDUB_DATA_DIR'])
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)

    def test_private_publication_roundtrip_and_no_secret_identity(self):
        record = owner.publish(self.data, 32123, SESSION)
        self.assertTrue(owner._private_file(self.data / owner.NAME))
        loaded = owner.load(self.data)
        self.assertEqual(loaded, record)
        identity = owner.public_identity(record)
        self.assertNotIn('token', identity)
        self.assertNotIn(SESSION, json.dumps(identity))
        identity.update(healthy=True, thread_alive=True)
        self.assertEqual(owner.validate_live_owner(record, identity), owner.public_identity(record))
        refreshed = owner.refresh(self.data, record)
        self.assertGreaterEqual(refreshed['heartbeat_at'], record['heartbeat_at'])
        owner.remove(self.data, dict(record, instance_id='d' * 32))
        self.assertTrue((self.data / owner.NAME).exists())
        owner.remove(self.data, record)
        self.assertFalse((self.data / owner.NAME).exists())

    def test_dead_pid_reuse_stale_or_unhealthy_owner_rejected(self):
        record = owner.publish(self.data, 32123, SESSION)
        with self.assertRaisesRegex(owner.OwnerUnavailable, 'owner_stale'):
            owner.load(self.data, now=record['heartbeat_at'] + 61)
        fake = dict(record, process_started=record['process_started'] - 10)
        with self.assertRaisesRegex(owner.OwnerUnavailable, 'owner_process_changed'):
            owner._validate(fake)
        identity = owner.public_identity(record) | {'healthy': True, 'thread_alive': False}
        with self.assertRaisesRegex(owner.OwnerUnavailable, 'owner_unhealthy'):
            owner.validate_live_owner(record, identity)
        identity.update(thread_alive=True, instance_id='d' * 32)
        with self.assertRaisesRegex(owner.OwnerUnavailable, 'owner_unhealthy'):
            owner.validate_live_owner(record, identity)

    def test_private_acl_precedes_writing_session(self):
        original = owner._secure_file
        inspected = []
        def secure(path):
            inspected.append(Path(path).read_bytes())
            original(path)
        with patch.object(owner, '_secure_file', secure):
            owner.publish(self.data, 32123, SESSION)
        self.assertEqual(inspected, [b''])
        with patch.object(owner, '_private_file', return_value=False):
            with self.assertRaisesRegex(owner.OwnerUnavailable, 'owner_permissions'):
                owner.load(self.data)

    def test_only_fixed_loopback_validated_and_missing_data_not_created(self):
        record = owner.publish(self.data, 32123, SESSION)
        for field, value in [('host', 'example.org'), ('port', True), ('token', 'short'), ('instance_id', '../bad')]:
            with self.subTest(field=field), self.assertRaises(owner.OwnerUnavailable):
                owner._validate(dict(record, **{field: value}))
        missing = self.data / 'absent'
        with self.assertRaises(owner.OwnerUnavailable):
            owner.load(missing)
        self.assertFalse(missing.exists())


class ContinueCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['VIETDUB_DATA_DIR'])
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.monitor = self.data / 'monitor.json'
        self.project = {'id': PROJECT, 'updated': 10, 'source_revision': 'fixture-source',
                        'media': {'path': 'fixture.mp4', 'duration': 20, 'width': 1280, 'height': 720},
                        'params': {'pipeline_extract': 'existing', 'pipeline_translate': True,
                                   'pipeline_tts': True}, 'segments': []}
        self.row = {'id': JOB, 'project_id': PROJECT, 'source_revision': 'fixture-source',
                    'kind': 'pipeline', 'state': 'error', 'created': time.time() - 10000, 'updated': 11}
        with closing(sqlite3.connect(self.data / 'studio.sqlite3')) as db, db:
            db.execute('CREATE TABLE objects(kind TEXT,id TEXT,value TEXT,PRIMARY KEY(kind,id))')
            db.executemany('INSERT INTO objects VALUES(?,?,?)',
                           [('project', PROJECT, json.dumps(self.project)), ('job', JOB, json.dumps(self.row))])
        folder = self.data / 'jobs' / JOB
        folder.mkdir(parents=True)
        (folder / 'translation-checkpoint.json').write_text(json.dumps({'rows': [
            {'text': 'fixture', 'translated': 'đã dịch'} if i < 5 else {'text': 'fixture', 'translated': ''}
            for i in range(375)]}), encoding='utf-8')
        self.record = None
        self.plan = {'job_id': JOB, 'project_id': PROJECT, 'expected_revision': 10,
                     'can_continue': True, 'content_ready': True, 'remaining_steps': ['translate', 'tts', 'render']}
        self.calls = []
        self.admission = {'execution_allowed': True, 'provider_required': True, 'blocked_code': '',
                          'blocked_reason': '', 'latest_job_id': JOB}

    def owner(self):
        self.record = owner.publish(self.data, 32123, SESSION)

    def response(self, request):
        self.calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        self.assertEqual(request.headers.get('x-studio-token'), SESSION)
        self.assertEqual(request.url.host, '127.0.0.1')
        if request.url.path == '/api/studio/owner':
            return httpx.Response(200, json=owner.public_identity(self.record) | {'healthy': True, 'thread_alive': True})
        if request.method == 'GET':
            return httpx.Response(200, json=self.plan)
        return httpx.Response(200, json={'id': NEXT, 'project_id': PROJECT, 'state': 'queued', 'continuation_from': JOB})

    def inspect(self, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(self.response), follow_redirects=True) as client:
            with patch.object(cli, '_admission', return_value=self.admission):
                return cli.inspect_or_continue(JOB, data_dir=self.data, monitor_path=self.monitor,
                                               client=client, **kwargs)

    def test_default_no_owner_is_read_only_without_server_core_or_vault(self):
        before = {path.relative_to(self.data): path.read_bytes() for path in self.data.rglob('*') if path.is_file()}
        original_import = builtins.__import__
        def forbidden_import(name, *args, **kwargs):
            if name in ('server', 'core', 'credentials'):
                raise AssertionError('CLI imported mutating application module')
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', forbidden_import), patch('keyring.get_password', side_effect=AssertionError('vault accessed')):
            report = self.inspect()
        after = {path.relative_to(self.data): path.read_bytes() for path in self.data.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.calls, [])
        self.assertFalse(report['enqueued'])
        self.assertEqual(report['owner_error'], 'owner_missing')
        self.assertEqual(report['observed_checkpoint_cues'], 375)
        self.assertEqual(report['observed_translated_cues'], 5)

    def test_default_live_owner_only_get_and_enqueues_through_http_when_allowed(self):
        self.owner()
        report = self.inspect()
        self.assertTrue(report['can_continue'])
        self.assertFalse(report['enqueued'])
        self.assertEqual([method for method, _, _ in self.calls], ['GET', 'GET'])
        self.calls.clear()
        report = self.inspect(enqueue=True)
        self.assertTrue(report['enqueued'])
        self.assertEqual([method for method, _, _ in self.calls], ['GET', 'GET', 'GET', 'POST'])
        body = self.calls[-1][2]
        self.assertEqual(body['owner_instance_id'], self.record['instance_id'])
        self.assertEqual(body['expected_revision'], 10)
        self.assertTrue(body['automatic'])
        self.assertEqual(len(body['request_id']), 32)
        self.assertNotIn(SESSION, json.dumps(report))

    def test_hold_and_review_reject_without_post(self):
        self.owner()
        self.admission.update(execution_allowed=False, blocked_code='audit_hold', blocked_reason='Chờ kiểm toán quota.')
        report = self.inspect(enqueue=True)
        self.assertFalse(report['enqueued'])
        self.assertFalse(report['can_continue'])
        self.assertEqual(report['blocked_code'], 'audit_hold')
        self.assertTrue(all(method == 'GET' for method, _, _ in self.calls))
        self.admission.update(execution_allowed=True)
        self.plan.update(can_continue=False, content_ready=False, requires_review=True)
        self.calls.clear()
        report = self.inspect(enqueue=True)
        self.assertFalse(report['enqueued'])
        self.assertTrue(report['requires_review'])
        self.assertTrue(all(method == 'GET' for method, _, _ in self.calls))

    def test_stale_identity_revision_and_redirect_fail_closed_without_post(self):
        self.owner()
        self.plan['expected_revision'] = 20
        report = self.inspect(enqueue=True)
        self.assertEqual(report['owner_error'], 'owner_revision_changed')
        self.assertTrue(all(method == 'GET' for method, _, _ in self.calls))
        self.plan['expected_revision'] = 10
        self.calls.clear()
        def redirect(request):
            self.calls.append((request.method, request.url.path, None))
            return httpx.Response(302, headers={'Location': 'https://example.org/private'})
        with httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=True) as client, patch.object(cli, '_admission', return_value=self.admission):
            report = cli.inspect_or_continue(JOB, data_dir=self.data, monitor_path=self.monitor, client=client, enqueue=True)
        self.assertEqual(report['owner_error'], 'owner_http_302')
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn(SESSION, json.dumps(report))

    def test_monitor_changes_between_inspection_and_post_are_rechecked(self):
        self.owner()
        blocked = self.admission | {'execution_allowed': False, 'blocked_code': 'audit_hold', 'blocked_reason': 'Hold'}
        with httpx.Client(transport=httpx.MockTransport(self.response)) as client, patch.object(cli, '_admission', side_effect=[self.admission, self.admission, blocked]):
            report = cli.inspect_or_continue(JOB, data_dir=self.data, monitor_path=self.monitor, client=client, enqueue=True)
        self.assertFalse(report['enqueued'])
        self.assertEqual(report['blocked_code'], 'audit_hold')
        self.assertTrue(all(method == 'GET' for method, _, _ in self.calls))

    def test_missing_database_never_creates_directory_or_db(self):
        absent = self.data / 'absent'
        with self.assertRaisesRegex(ValueError, 'kho dữ liệu'):
            cli.read_store(absent, JOB)
        self.assertFalse(absent.exists())

    def test_real_shared_admission_has_no_store_vault_import_and_respects_hold(self):
        self.monitor.write_text(json.dumps({'project_id': PROJECT, 'audit_hold': True,
                                          'quota_scope': 'unknown'}), encoding='utf-8')
        before = {path.relative_to(self.data): path.read_bytes() for path in self.data.rglob('*') if path.is_file()}
        original_import = builtins.__import__
        def forbidden_import(name, *args, **kwargs):
            if name in ('server', 'core', 'credentials'):
                raise AssertionError('CLI imported mutating application module')
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', forbidden_import), patch('keyring.get_password', side_effect=AssertionError('vault accessed')):
            report = cli.inspect_or_continue(JOB, data_dir=self.data, monitor_path=self.monitor)
        self.assertEqual(report['blocked_code'], 'audit_hold')
        self.assertEqual(report['observed_translated_cues'], 5)
        self.assertFalse(report['enqueued'])
        after = {path.relative_to(self.data): path.read_bytes() for path in self.data.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        self.owner()
        with httpx.Client(transport=httpx.MockTransport(self.response)) as client:
            report = cli.inspect_or_continue(JOB, data_dir=self.data, monitor_path=self.monitor, client=client, enqueue=True)
        self.assertEqual(report['blocked_code'], 'audit_hold')
        self.assertTrue(all(method == 'GET' for method, _, _ in self.calls))

    def test_terminal_target_itself_sets_cooldown_even_with_stale_monitor(self):
        self.owner()
        stopped = time.time() - 10
        self.row.update(http_status=429, failure_code='provider_quota', ended=stopped, quota_scope='unknown')
        with closing(sqlite3.connect(self.data / 'studio.sqlite3')) as db, db:
            db.execute('UPDATE objects SET value=? WHERE kind=? AND id=?', (json.dumps(self.row), 'job', JOB))
        self.monitor.write_text(json.dumps({'project_id': PROJECT, 'audit_hold': False, 'quota_scope': 'unknown',
                                          'last_attempt_utc': stopped - 10000, 'last_http_status': 429}), encoding='utf-8')
        with httpx.Client(transport=httpx.MockTransport(self.response)) as client:
            report = cli.inspect_or_continue(JOB, data_dir=self.data, monitor_path=self.monitor, client=client, enqueue=True)
        self.assertEqual(report['blocked_code'], 'quota_recovery_required')
        self.assertGreaterEqual(report['earliest_retry_utc'], stopped + 7200)
        self.assertEqual(report['latest_job_id'], JOB)
        self.assertTrue(all(method == 'GET' for method, _, _ in self.calls))

    def test_offline_render_only_may_enqueue_without_lifting_api_hold(self):
        self.owner()
        self.plan['remaining_steps'] = ['render']
        self.monitor.write_text(json.dumps({'project_id': PROJECT, 'audit_hold': True,
                                          'quota_scope': 'unknown'}), encoding='utf-8')
        with httpx.Client(transport=httpx.MockTransport(self.response)) as client:
            report = cli.inspect_or_continue(JOB, data_dir=self.data, monitor_path=self.monitor, client=client, enqueue=True)
        self.assertTrue(report['enqueued'])
        self.assertFalse(report['provider_required'])
        self.assertTrue(json.loads(self.monitor.read_text(encoding='utf-8'))['audit_hold'])

    def test_stale_owner_is_rejected_before_any_http(self):
        self.owner()
        report = self.inspect(enqueue=True, now=self.record['heartbeat_at'] + 61)
        self.assertEqual(report['owner_error'], 'owner_stale')
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
