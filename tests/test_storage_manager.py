import copy
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import core
import server
import storage_manager as sm


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='npk-storage-', dir=core.DATA))
        self.data = Path(self.temp)
        for directory in ('jobs', 'frames'):
            (self.data / directory).mkdir()
        for module in (core, server, sm):
            self.stack.enter_context(patch.object(module, 'DATA', self.data))
        self.stack.enter_context(patch.object(server, 'active_id', None))
        self.p = self.project()

    def project(self, name='Dự án thử'):
        identifier = uuid.uuid4().hex
        p = dict(id=identifier, name=name, updated=1.0, created=1.0, params=copy.deepcopy(core.DEFAULTS),
                 media={'path':str(core.ROOT/'tests/fixtures/demo.mp4'), 'duration':8}, artifacts={},
                 segments=[dict(start=0,end=1,text='Hello',translated='Xin chào')], regions=[])
        return core.put('project', identifier, p)

    def job(self, owner=None, name='clean.mp4', content=b'generated', state='done'):
        identifier = uuid.uuid4().hex
        folder = self.data/'jobs'/identifier
        folder.mkdir()
        (folder/name).write_bytes(content)
        j = dict(id=identifier,project_id=(owner or self.p)['id'],kind='clean',state=state,created=1.0,result={})
        core.put('job',identifier,j)
        return j, folder

    def run_plan(self, action='delete'):
        plan = sm.preview(action, None if action == 'cache' else self.p['id'])
        body = {'signature':plan['signature']}
        if action != 'delete':
            body['confirmed'] = True
        return sm.execute(action, plan['project_id'], body)

    def test_preview_is_readonly_and_counts_only_owned_targets(self):
        j, folder = self.job(content=b'abc')
        plan = sm.preview('cleanup',self.p['id'])
        self.assertEqual(plan['bytes'],3)
        self.assertEqual(len(plan['targets']),1)
        self.assertTrue(folder.exists())
        self.assertEqual(core.all_items('storage_trash'),[])

    def test_delete_restore_preserves_subtitles_history_and_source(self):
        j, folder = self.job()
        old = copy.deepcopy(self.p)
        self.p['updated'] = 2.0
        core.save_project_version(self.p,old)
        outcome = self.run_plan()
        self.assertIsNone(core.get('project',self.p['id']))
        self.assertFalse(folder.exists())
        self.assertEqual(outcome['freed_bytes'],0)
        self.assertEqual(len(core.all_items('storage_trash')),1)
        sm.restore(outcome['trash_id'])
        restored = core.get('project',self.p['id'])
        self.assertEqual(restored['segments'],self.p['segments'])
        self.assertEqual(len(core.all_items('project_revision')),1)
        self.assertTrue(folder.exists())
        self.assertTrue(Path(self.p['media']['path']).is_file())
        self.assertGreater(restored['updated'],2)

    def test_cleanup_keeps_current_and_historical_artifacts(self):
        a, fa = self.job()
        b, fb = self.job()
        c, fc = self.job()
        old = copy.deepcopy(self.p)
        old['artifacts'] = {'clean':{'job_id':a['id']}}
        self.p['artifacts'] = {'clean':{'job_id':b['id']}}
        self.p['updated'] = 2
        core.save_project_version(self.p,old)
        plan = sm.preview('cleanup',self.p['id'])
        self.assertEqual([t['path'] for t in plan['targets']],['jobs/'+c['id']])
        self.run_plan('cleanup')
        self.assertTrue(fa.exists() and fb.exists())
        self.assertFalse(fc.exists())
        self.assertEqual(core.get('project',self.p['id'])['artifacts'],self.p['artifacts'])

    def test_delete_keeps_shared_artifacts_and_final_video(self):
        shared, sf = self.job()
        final, ff = self.job(name='final.mp4')
        other = self.project('Bản sao')
        other['artifacts'] = {'clean':{'job_id':shared['id']}}
        core.put('project',other['id'],other)
        plan = sm.preview('delete',self.p['id'])
        self.assertEqual(plan['targets'],[])
        self.run_plan()
        self.assertTrue(sf.exists() and ff.exists())
        self.assertIsNotNone(core.get('project',other['id']))

    def test_delete_keeps_source_or_audio_imported_from_own_job(self):
        job, folder = self.job(name='input-video.mp4')
        self.p['media']['path'] = str(folder/'input-video.mp4')
        core.put('project',self.p['id'],self.p)
        self.run_plan()
        self.assertTrue((folder/'input-video.mp4').exists())

    def test_changed_project_is_still_rejected_without_confirmation(self):
        self.job()
        plan = sm.preview('delete',self.p['id'])
        self.p['updated'] = 3
        core.put('project',self.p['id'],self.p)
        with self.assertRaisesRegex(ValueError,'đã đổi'):
            sm.execute('delete',self.p['id'],dict(signature=plan['signature']))

    def test_changed_file_invalidates_plan(self):
        _, folder = self.job()
        plan = sm.preview('cleanup',self.p['id'])
        (folder/'clean.mp4').write_bytes(b'changed file')
        with self.assertRaisesRegex(ValueError,'đã đổi'):
            sm.execute('cleanup',self.p['id'],dict(confirmed=True,signature=plan['signature']))
        self.assertTrue(folder.exists())

    def test_active_queue_and_cancelled_worker_block_mutations(self):
        j, _ = self.job(state='queued')
        self.assertIsNotNone(sm.preview('delete',self.p['id'])['blocked'])
        with self.assertRaisesRegex(ValueError,'dừng hẳn'):
            self.run_plan()
        j['state'] = 'cancelled'
        core.put('job',j['id'],j)
        with patch.object(server,'active_id',j['id']):
            with self.assertRaisesRegex(ValueError,'dừng hẳn'):
                self.run_plan()

    def test_purge_requires_fresh_snapshot_but_no_confirmation_or_name(self):
        j, folder = self.job(content=b'abc')
        outcome = self.run_plan()
        identifier = outcome['trash_id']
        plan = sm.purge_preview(identifier)
        with self.assertRaises(ValueError):
            sm.purge(identifier,dict(signature='stale'))
        result = sm.purge(identifier,dict(signature=plan['signature']))
        self.assertEqual(result['removed_bytes'],3)
        self.assertFalse((self.data/'trash'/identifier).exists())
        self.assertTrue((core.ROOT/'runtime').is_dir())
        with self.assertRaises(ValueError):
            sm.restore(identifier)

    def test_new_input_pointing_into_trash_blocks_purge(self):
        j, _ = self.job()
        identifier = self.run_plan()['trash_id']
        p = self.project('Dùng lại')
        p['params']['voice_path'] = str(self.data/'trash'/identifier/'jobs'/j['id']/'clean.mp4')
        core.put('project',p['id'],p)
        with self.assertRaisesRegex(ValueError,'đang dùng'):
            sm.purge_preview(identifier)

    def test_restore_conflict_never_overwrites_new_files(self):
        j, folder = self.job()
        identifier = self.run_plan()['trash_id']
        folder.mkdir()
        (folder/'new.txt').write_text('keep',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'không ghi đè'):
            sm.restore(identifier)
        self.assertEqual((folder/'new.txt').read_text(),'keep')

    def test_interrupted_move_is_recoverable(self):
        a, af = self.job()
        b, bf = self.job()
        original = sm.os.rename
        calls = []
        def failing(src,dst):
            calls.append(src)
            if len(calls) == 2:
                raise PermissionError('locked fixture')
            return original(src,dst)
        with patch.object(sm.os,'rename',side_effect=failing):
            with self.assertRaisesRegex(ValueError,'gián đoạn'):
                self.run_plan()
        entry = core.all_items('storage_trash')[0]
        self.assertEqual(entry['state'],'attention')
        self.assertIsNotNone(core.get('project',self.p['id']))
        sm.restore(entry['id'])
        self.assertTrue(af.exists() and bf.exists())

    def test_cache_cleanup_protects_custom_audio_and_ignores_other_files(self):
        cache = self.data/'speech-cache'
        cache.mkdir()
        (cache/'generated.wav').write_bytes(b'abc')
        (cache/'imported.wav').write_bytes(b'source')
        (cache/'private.txt').write_bytes(b'keep')
        self.p['params']['voice_path'] = str(cache/'imported.wav')
        core.put('project',self.p['id'],self.p)
        outcome = self.run_plan('cache')
        self.assertEqual(outcome['moved_bytes'],3)
        self.assertTrue((cache/'private.txt').exists() and (cache/'imported.wav').exists())

    def test_traversal_and_unknown_ids_are_rejected(self):
        for path in ('../models', '..', str(core.ROOT), 'jobs/../../models'):
            with self.assertRaises(ValueError):
                sm.owned(path)
        with self.assertRaises(ValueError):
            sm.preview('delete','../models')

    def test_symlink_payload_is_not_followed(self):
        j, folder = self.job()
        link = folder/'outside'
        try:
            os.symlink(core.ROOT/'models',link,target_is_directory=True)
        except OSError:
            self.skipTest('Windows symbolic-link privilege unavailable')
        plan = sm.preview('delete',self.p['id'])
        self.assertEqual(plan['targets'],[])

    def test_foreign_file_added_to_trash_is_preserved(self):
        self.job()
        identifier = self.run_plan()['trash_id']
        foreign = self.data/'trash'/identifier/'do-not-delete.txt'
        foreign.write_text('keep')
        with self.assertRaisesRegex(ValueError,'ngoài danh sách'):
            sm.purge_preview(identifier)
        self.assertTrue(foreign.exists())

    def test_http_requires_auth_and_does_not_accept_client_paths(self):
        from fastapi.testclient import TestClient
        with TestClient(server.app) as client:
            self.assertEqual(client.get('/api/studio/storage/overview').status_code,403)
            response = client.post('/api/studio/storage/execute',headers={'x-studio-token':server.TOKEN},
                json=dict(action='delete',project_id='../models',confirmed=True,signature='invented',targets=[str(core.ROOT)]))
            self.assertEqual(response.status_code,400)
        self.assertIsNotNone(core.get('project',self.p['id']))

    def test_last_copy_can_clean_shared_data_after_original_is_purged(self):
        j, folder = self.job()
        other = self.project('Bản sao cuối')
        other['artifacts'] = {'clean': {'job_id': j['id']}}
        core.put('project', other['id'], other)
        identifier = self.run_plan()['trash_id']
        pp = sm.purge_preview(identifier)
        sm.purge(identifier, dict(confirmed=True, signature=pp['signature'], name=pp['name']))
        self.assertTrue(folder.exists())
        self.p = other
        self.run_plan()
        self.assertFalse(folder.exists())

    def test_partial_permanent_delete_is_retryable_without_deleting_new_data(self):
        _, folder = self.job()
        (folder/'second.txt').write_bytes(b'second')
        identifier = self.run_plan()['trash_id']
        pp = sm.purge_preview(identifier)
        base = self.data/'trash'/identifier
        def partial(path):
            next(path.rglob('second.txt')).unlink()
            raise PermissionError('locked')
        with patch.object(sm.shutil,'rmtree',side_effect=partial):
            with self.assertRaises(PermissionError):
                sm.purge(identifier, dict(confirmed=True, signature=pp['signature'], name=pp['name']))
        pp = sm.purge_preview(identifier)
        self.assertEqual(pp['state'], 'purging')
        sm.purge(identifier, dict(confirmed=True, signature=pp['signature'], name=pp['name']))
        self.assertFalse(base.exists())

    def test_revision_advances_when_wall_clock_has_not_advanced(self):
        before = self.p['updated']
        with patch('server.time.time', return_value=before):
            updated = server.update_project(self.p['id'], dict(expected_revision=before, name='New'))
        self.assertGreater(updated['updated'], before)

    def test_shared_auditions_are_cleanable_but_defaults_audio_is_protected(self):
        j, folder = self.job(name='sample.wav')
        j.update(kind='voice_preview', project_id='voice-preview')
        core.put('job', j['id'], j)
        plan = sm.preview('cache')
        self.assertEqual(len(plan['targets']), 1)
        core.put('settings', 'main', {'bgm_path': str(folder/'sample.wav')})
        plan = sm.preview('cache')
        self.assertEqual(plan['targets'], [])
