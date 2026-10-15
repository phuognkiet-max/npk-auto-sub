"""Publication and checkpoint recovery must preserve newer editor contents."""
from contextlib import ExitStack, closing
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient
import core
import server
import studio_api
from translation_checkpoint import write_checkpoint


class JobIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.data = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='integrity-', dir=core.DATA)))
        (self.data/'jobs').mkdir()
        for module in (core, server, studio_api):
            self.stack.enter_context(patch.object(module, 'DATA', self.data))
        self.client = self.stack.enter_context(TestClient(server.app, headers={'x-studio-token':server.TOKEN}))
        self.project = {'id':uuid.uuid4().hex, 'name':'Integrity fixture', 'created':1., 'updated':2.,
                        'media':core.probe(core.ROOT/'tests/fixtures/demo.mp4'),
                        'params':copy.deepcopy(core.DEFAULTS), 'regions':[], 'extraction_regions':[],
                        'source_revision':uuid.uuid4().hex, 'artifacts':{'tts':{'job_id':'original-voice'}},
                        'segments':[{'start':0.,'end':1.,'text':'Hello','translated':''},
                                    {'start':2.,'end':3.,'text':'Thanks','translated':''}]}
        core.put('project',self.project['id'],self.project)

    def job(self, kind='translate'):
        item={'id':uuid.uuid4().hex,'project_id':self.project['id'],'kind':kind,
              'source_revision':self.project['source_revision'],'project_revision':self.project['updated'],
              'state':'error','created':3.,'started':3.,'ended':4.,'events':[]}
        folder=self.data/'jobs'/item['id'];folder.mkdir()
        core.put('job',item['id'],item)
        (folder/'input.json').write_text(json.dumps({'project':self.project,'job':item},ensure_ascii=False),encoding='utf-8')
        return item,folder

    def checkpoint(self, kind='translate', selected=None):
        item,folder=self.job(kind)
        if selected is not None:
            item['segment_indices']=selected;core.put('job',item['id'],item)
            (folder/'input.json').write_text(json.dumps({'project':self.project,'job':item},ensure_ascii=False),encoding='utf-8')
        rows=copy.deepcopy(self.project['segments']);rows[0]['translated']='Xin chào';rows[1]['translated']='Cảm ơn'
        completed=[0,1] if selected is None else selected
        for index in range(len(rows)):
            if index not in completed:rows[index]['translated']=''
        write_checkpoint(folder/'translation-checkpoint.json',self.project,rows,completed,selected)
        return item,folder

    def measured_translation_attempt(self, item, folder, completed=0):
        """Only execution fixtures get observed history; recovery-only fixtures remain legacy."""
        from provider_usage import TranslationUsage
        selected = item.get('segment_indices', list(range(len(self.project['segments']))))
        pending = [i for i in selected if self.project['params'].get('translation_scope') != 'missing' or
                   not self.project['segments'][i].get('translated', '').strip()]
        usage = TranslationUsage(folder, len(pending), int(self.project['params']['translation_batch']),
                                 self.project['params']['provider'], self.project['params']['translation_model'])
        usage.set_batch(pending[:int(self.project['params']['translation_batch'])], 'PRIVATE fixture prompt; do not log')
        usage.observe({'event':'before_send', 'attempt':1})
        usage.observe({'event':'response', 'http_status':200 if completed else 503, 'elapsed_seconds':.1})
        usage.checkpoint(completed)

    def test_stale_completion_never_replaces_manual_cues_or_current_voice(self):
        item,_=self.job('pipeline')
        edited=server.update_project(self.project['id'],{'expected_revision':2.,'segments':[
            {'start':0.,'end':1.,'text':'Hello','translated':'Bản dịch tự chỉnh'}], 'params':{'voice_speed':1.2}})
        result={'segments':copy.deepcopy(self.project['segments']), 'artifacts':{'tts':{'files':['voice.wav']}}}
        self.assertFalse(server.attach_completed_result(item,result))
        self.assertEqual(core.get('project',self.project['id']),edited)

    def test_artifact_only_completion_advances_revision_but_rebases_draft_without_losing_artifact(self):
        item,_=self.job('tts')
        with patch.object(server.time,'time',return_value=1.):
            self.assertTrue(server.attach_completed_result(item,{'files':['voice.wav']}))
        updated=core.get('project',self.project['id'])
        self.assertGreater(updated['updated'],2.)
        self.assertEqual(updated['artifacts']['tts']['job_id'],item['id'])
        response=self.client.put('/api/projects/'+updated['id'],json={'expected_revision':2.,
            'segments':[{'start':0.,'end':1.,'text':'Hello','translated':'Đang nhập dở'}]})
        self.assertEqual(response.status_code,200,response.text)
        saved=core.get('project',updated['id'])
        self.assertEqual(saved['artifacts']['tts']['job_id'],item['id'])
        self.assertEqual(saved['segments'][0]['translated'],'Đang nhập dở')
        self.assertGreater(saved['updated'],updated['updated'])

    def test_metadata_rebase_cannot_bypass_real_content_or_source_conflict(self):
        for change in ({'name':'Other editor'}, {'params':{'font_size':70}},
                       {'segments':[{'start':0.,'end':1.,'text':'Changed source text','translated':''}]}):
            with self.subTest(change=change):
                core.put('project',self.project['id'],self.project)
                changed=server.update_project(self.project['id'],change|{'expected_revision':2.})
                response=self.client.put('/api/projects/'+self.project['id'],json={'name':'Stale','expected_revision':2.})
                self.assertEqual(response.status_code,409,response.text)
                self.assertEqual(core.get('project',self.project['id']),changed)
        changed=copy.deepcopy(self.project);changed['source_revision']='changed';changed['updated']=3.
        core.save_project_version(changed,self.project)
        self.assertFalse(server.artifact_only_revision_change(changed,2.))
        self.assertFalse(server.artifact_only_revision_change(changed,1.))

    def test_later_old_snapshot_completion_does_not_overwrite_newer_completion(self):
        first,_=self.job('tts');second,_=self.job('tts')
        self.assertTrue(server.attach_completed_result(first,{'files':['voice.wav']}))
        self.assertFalse(server.attach_completed_result(second,{'files':['voice.wav']}))
        self.assertEqual(core.get('project',self.project['id'])['artifacts']['tts']['job_id'],first['id'])

    def test_changed_source_and_deleted_project_keep_job_result_separate(self):
        item,_=self.job('clean')
        changed=copy.deepcopy(self.project);changed['source_revision']=uuid.uuid4().hex
        core.put('project',changed['id'],changed)
        self.assertFalse(server.attach_completed_result(item,{'files':['clean.mp4']}))
        self.assertEqual(core.get('project',changed['id']),changed)
        with closing(core.connect()) as db:
            with db:db.execute("DELETE FROM objects WHERE kind='project' AND id=?",(changed['id'],))
        self.assertFalse(server.attach_completed_result(item,{'files':['clean.mp4']}))

    def test_pipeline_matching_revision_attaches_cues_coverage_and_each_artifact(self):
        item,_=self.job('pipeline')
        rows=copy.deepcopy(self.project['segments']);rows[0]['translated']='Xin chào'
        result={'segments':rows,'coverage':{'analyzed_end':8.},'artifacts':{'tts':{'files':['voice.wav']},'render':{'files':['final.mp4']}}}
        self.assertTrue(server.attach_completed_result(item,result))
        updated=core.get('project',self.project['id'])
        self.assertEqual(updated['segments'],rows)
        self.assertEqual(updated['coverage'],result['coverage'])
        self.assertEqual(updated['artifacts']['tts']['job_id'],item['id'])
        self.assertEqual(updated['artifacts']['render']['job_id'],item['id'])

    def test_restore_counts_only_missing_exact_cues_and_keeps_manual_translation(self):
        item,_=self.checkpoint()
        self.project['segments'][0]['translated']='Tôi tự sửa'
        self.project['updated']=5.;core.put('project',self.project['id'],self.project)
        detail=self.client.get('/api/studio/jobs/'+item['id']).json()
        self.assertTrue(detail['can_restore_translation']);self.assertEqual(detail['restorable_translation_count'],1)
        response=self.client.post('/api/studio/jobs/'+item['id']+'/apply',json={'expected_revision':5.})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual([r['translated'] for r in response.json()['segments']],['Tôi tự sửa','Cảm ơn'])
        detail=self.client.get('/api/studio/jobs/'+item['id']).json()
        self.assertFalse(detail['can_restore_translation']);self.assertEqual(detail['restorable_translation_count'],0)

    def test_selected_restore_cannot_restore_preexisting_translation_outside_scope(self):
        for schema in ('v2','legacy'):
            with self.subTest(schema=schema):
                self.project['segments'][0]['translated']=''
                self.project['segments'][1]['translated']='Translation that existed before selected job'
                core.put('project',self.project['id'],self.project)
                item,folder=self.job();item['segment_indices']=[0];core.put('job',item['id'],item)
                (folder/'input.json').write_text(json.dumps({'project':self.project,'job':item}),encoding='utf-8')
                rows=copy.deepcopy(self.project['segments']);rows[0]['translated']='Xin chào'
                write_checkpoint(folder/'translation-checkpoint.json',self.project,rows,[0],[0])
                if schema=='legacy':
                    (folder/'translation-checkpoint.json').write_text(json.dumps(rows),encoding='utf-8')
                current=copy.deepcopy(self.project);current['segments'][1]['translated']=''
                core.put('project',current['id'],current)
                detail=self.client.get('/api/studio/jobs/'+item['id']).json()
                self.assertEqual(detail['restorable_translation_count'],1)
                response=self.client.post('/api/studio/jobs/'+item['id']+'/apply',json={'expected_revision':current['updated']})
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual([r['translated'] for r in response.json()['segments']],['Xin chào',''])

    def test_legacy_missing_scope_excludes_preexisting_translation_even_after_user_clears_it(self):
        self.project['params']['translation_scope']='missing'
        self.project['segments'][1]['translated']='Preexisting outside missing scope'
        core.put('project',self.project['id'],self.project)
        item,folder=self.job()
        rows=copy.deepcopy(self.project['segments']);rows[0]['translated']='Xin chào'
        (folder/'translation-checkpoint.json').write_text(json.dumps(rows),encoding='utf-8')
        current=copy.deepcopy(self.project);current['segments'][1]['translated']=''
        core.put('project',current['id'],current)
        self.assertEqual(studio_api.translation_restore_candidates(item,current),[(0,'Xin chào')])

    def duplicate_checkpoint(self):
        self.project['segments'][1]=copy.deepcopy(self.project['segments'][0])
        core.put('project',self.project['id'],self.project)
        return self.checkpoint(selected=[0])

    def test_selected_duplicate_identity_uses_exact_index_without_leaking_to_other_duplicate(self):
        item,_=self.duplicate_checkpoint()
        self.assertEqual(studio_api.translation_restore_candidates(item,self.project),[(0,'Xin chào')])
        response=self.client.post('/api/studio/jobs/'+item['id']+'/apply',json={'expected_revision':self.project['updated']})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual([r['translated'] for r in response.json()['segments']],['Xin chào',''])

    def test_changed_table_with_duplicate_identity_rejects_ambiguous_recovery_without_write(self):
        item,_=self.duplicate_checkpoint()
        self.project['segments'].append({'start':4.,'end':5.,'text':'Added cue','translated':''})
        core.put('project',self.project['id'],self.project)
        before=core.get('project',self.project['id'])
        detail=self.client.get('/api/studio/jobs/'+item['id']).json()
        self.assertFalse(detail['can_restore_translation'])
        self.assertIn('câu trùng',detail['translation_recovery_error'])
        response=self.client.post('/api/studio/jobs/'+item['id']+'/apply',json={'expected_revision':self.project['updated']})
        self.assertEqual(response.status_code,400,response.text)
        self.assertEqual(core.get('project',self.project['id']),before)

    def test_unique_identity_can_restore_selected_original_cue_after_reordering(self):
        item,_=self.checkpoint(selected=[0])
        self.project['segments'].reverse();core.put('project',self.project['id'],self.project)
        self.assertEqual(studio_api.translation_restore_candidates(item,self.project),[(1,'Xin chào')])
        response=self.client.post('/api/studio/jobs/'+item['id']+'/apply',json={'expected_revision':self.project['updated']})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual([r['translated'] for r in response.json()['segments']],['','Xin chào'])

    def test_restore_rejects_missing_snapshot_and_wrong_job_binding_without_write(self):
        item,folder=self.checkpoint();before=core.get('project',self.project['id'])
        (folder/'input.json').unlink()
        response=self.client.post('/api/studio/jobs/'+item['id']+'/apply',json={'expected_revision':2.})
        self.assertEqual(response.status_code,400,response.text)
        self.assertFalse(self.client.get('/api/studio/jobs/'+item['id']).json()['can_restore_translation'])
        (folder/'input.json').write_text(json.dumps({'project':self.project,'job':item|{'id':'another-job'}}),encoding='utf-8')
        self.assertEqual(self.client.post('/api/studio/jobs/'+item['id']+'/apply',json={'expected_revision':2.}).status_code,400)
        self.assertEqual(core.get('project',self.project['id']),before)

    def test_corrupt_legacy_checkpoint_does_not_apply_matching_subset(self):
        item,folder=self.checkpoint();before=core.get('project',self.project['id'])
        rows=copy.deepcopy(self.project['segments']);rows[0]['translated']='Forged';rows[1]['text']='Changed other cue'
        (folder/'translation-checkpoint.json').write_text(json.dumps(rows),encoding='utf-8')
        response=self.client.post('/api/studio/jobs/'+item['id']+'/apply',json={'expected_revision':2.})
        self.assertEqual(response.status_code,400,response.text)
        self.assertEqual(core.get('project',self.project['id']),before)

    def test_restore_rejects_changed_target_language_and_explains_why(self):
        item,_=self.checkpoint()
        self.project['params']['target_language']='English';self.project['updated']=5.
        core.put('project',self.project['id'],self.project)
        detail=self.client.get('/api/studio/jobs/'+item['id']).json()
        self.assertFalse(detail['can_restore_translation'])
        self.assertIn('ngôn ngữ',detail['translation_recovery_error'])
        self.assertEqual(detail['restorable_translation_count'],0)
        response=self.client.post('/api/studio/jobs/'+item['id']+'/apply',json={'expected_revision':5.})
        self.assertEqual(response.status_code,400,response.text)
        self.assertEqual(core.get('project',self.project['id']),self.project)

    def test_selected_resume_has_same_selection_in_api_and_copied_snapshot(self):
        item,folder=self.checkpoint(selected=[0])
        self.measured_translation_attempt(item,folder,completed=1)
        with patch('preflight.inspect',return_value={'ok':True}), patch.object(server,'active_key_id',return_value='fixture'), patch.object(server,'active_secret',return_value='fixture-secret'):
            response=self.client.post('/api/jobs',json={'kind':'translate','project_id':self.project['id'],
                'replace_segments':True,'resume_from':item['id'],'segment_indices':[0],'expected_revision':2.})
        self.assertEqual(response.status_code,200,response.text)
        snapshot=json.loads((self.data/'jobs'/response.json()['id']/'input.json').read_text(encoding='utf-8'))
        self.assertEqual(snapshot['job']['segment_indices'],[0])
        self.assertEqual(snapshot['job']['api_usage_lineage']['root_id'],item['id'])

    def test_first_batch_failure_without_checkpoint_retries_only_selected_missing_cues(self):
        self.project['segments']=[{'start':index*.08,'end':index*.08+.04,'text':f'Cue {index}',
            'translated':f'Saved {index}' if index<20 else ''} for index in range(88)]
        core.put('project',self.project['id'],self.project)
        item,folder=self.job();item['segment_indices']=list(range(20,88));core.put('job',item['id'],item)
        (folder/'input.json').write_text(json.dumps({'project':self.project,'job':item}),encoding='utf-8')
        self.measured_translation_attempt(item,folder)
        detail=self.client.get('/api/studio/jobs/'+item['id']).json()
        self.assertFalse(detail['can_resume']);self.assertEqual(detail['retry_segment_indices'],list(range(20,88)))
        with patch('preflight.inspect',return_value={'ok':True}), patch.object(server,'active_key_id',return_value='fixture'), patch.object(server,'active_secret',return_value='fixture-secret'):
            response=self.client.post('/api/jobs',json={'kind':'translate','project_id':self.project['id'],
                'replace_segments':True,'retry_from':item['id'],'expected_revision':2.,'segment_indices':list(range(20,88))})
        self.assertEqual(response.status_code,200,response.text)
        snapshot=json.loads((self.data/'jobs'/response.json()['id']/'input.json').read_text(encoding='utf-8'))
        self.assertEqual(snapshot['job']['segment_indices'],list(range(20,88)))
        self.assertEqual(snapshot['job']['retry_from'],item['id'])
        self.assertEqual(snapshot['job']['api_usage_lineage']['root_id'],item['id'])
        self.assertEqual(snapshot['project']['segments'][:20],self.project['segments'][:20])

    def test_retry_excludes_new_manual_translation_and_rejects_changed_identity(self):
        item,folder=self.job();item['segment_indices']=[0,1];core.put('job',item['id'],item)
        (folder/'input.json').write_text(json.dumps({'project':self.project,'job':item}),encoding='utf-8')
        self.project['segments'][0]['translated']='Tự sửa';core.put('project',self.project['id'],self.project)
        detail=self.client.get('/api/studio/jobs/'+item['id']).json()
        self.assertEqual(detail['retry_segment_indices'],[1])
        self.project['segments'][1]['text']='Edited source';core.put('project',self.project['id'],self.project)
        detail=self.client.get('/api/studio/jobs/'+item['id']).json()
        self.assertNotIn('retry_segment_indices',detail);self.assertIn('không tự mở rộng',detail['translation_retry_error'])
        with patch('preflight.inspect',return_value={'ok':True}), patch.object(server,'active_secret',return_value='fixture-secret'):
            response=self.client.post('/api/jobs',json={'kind':'translate','project_id':self.project['id'],
                'replace_segments':True,'retry_from':item['id'],'expected_revision':2.})
        self.assertEqual(response.status_code,400,response.text)

    def test_no_checkpoint_retry_rejects_tampered_selected_scope(self):
        item,folder=self.job();item['segment_indices']=[1];core.put('job',item['id'],item)
        # The immutable input still says all rows, so a later metadata change is
        # not proof of the original selected scope.
        detail=self.client.get('/api/studio/jobs/'+item['id']).json()
        self.assertNotIn('retry_segment_indices',detail)
        self.assertIn('snapshot',detail['translation_retry_error'])

    def test_invalid_editor_numbers_and_rows_are_400_and_do_not_change_project(self):
        before=core.get('project',self.project['id'])
        for body in ({'segments':[{}]}, {'segments':[None]}, {'segments':[{'start':None,'end':1}]},
                     {'segments':[{'start':True,'end':2}]}, {'regions':[{}]}, {'regions':[None]},
                     {'params':{'font_size':None}}, {'params':{'font_size':True}}):
            with self.subTest(body=body):
                response=self.client.put('/api/projects/'+self.project['id'],json=body|{'expected_revision':2.})
                self.assertEqual(response.status_code,400,response.text)
                self.assertEqual(core.get('project',self.project['id']),before)

    def test_compact_state_preserves_false_applied_flag(self):
        item,_=self.job('tts');item.update(state='done',project_applied=False)
        core.put('job',item['id'],item)
        with patch('hardware_admission.hardware_info',return_value={}):
            response=self.client.get('/api/studio/state').json()
        self.assertIs(next(j for j in response['jobs'] if j['id']==item['id'])['project_applied'],False)


if __name__=='__main__':unittest.main()
