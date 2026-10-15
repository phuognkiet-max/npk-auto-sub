"""Fault recovery with verified fixture media, no inference or provider access.

Run with tools/run_tests.py --profile media. Real FFmpeg verifies fixture video;
TTS is a deterministic PCM fixture, and stage engines are explicitly replaced.
"""
from contextlib import ExitStack
import array
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import uuid
import wave

import core
import worker
import server
import studio_api
import preflight
import storage_manager
from fastapi.testclient import TestClient
from speech_delivery import signature_payload
from subtitle_quality import coverage_record, digest, speech_ready, clean_ready
from stage_journal import load, recover
from pipeline_resume import prepare_continuation, continuation_status
from translation_checkpoint import write_checkpoint


class StageContractRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.data = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='stage-contract-', dir=core.DATA)))
        (self.data/'jobs').mkdir()
        for module in (core, worker, server, studio_api, storage_manager):
            self.stack.enter_context(patch.object(module, 'DATA', self.data))
        self.source = self.data/'source.mp4'
        shutil.copyfile(core.ROOT/'tests/fixtures/demo.mp4', self.source)
        media = core.probe(self.source)
        self.p = {'id':uuid.uuid4().hex,'name':'Stage recovery fixture','updated':20.,'created':1.,
            'source_revision':uuid.uuid4().hex,'media':media,'regions':[], 'extraction_regions':[],
            'artifacts':{},'segments':[{'start':0.,'end':media['duration'],'text':'Original','translated':'Đủ lời'}],
            'params':core.DEFAULTS | {'pipeline_extract':'existing','pipeline_translate':False,
                'pipeline_tts':True,'pipeline_clean':False,'use_clean':False,'burn_subtitles':False,
                'voice_gain':1,'original_gain':1,'tts_timing_mode':'natural'}}
        self.p['coverage']=coverage_record(self.p,'asr',0,media['duration'])
        core.put('project',self.p['id'],self.p)
        self.model='fixture-voice-1'
        self.stack.enter_context(patch.object(worker,'speech_signature',side_effect=lambda p:digest([self.model,signature_payload(p)])))
        self.stack.enter_context(patch.object(worker,'emit'))
        self.calls=[]
        self.stack.enter_context(patch.dict(worker.TASKS, {'tts':self.tts,'clean':self.clean,'render':self.render_failure}))
        self.client=self.stack.enter_context(TestClient(server.app,headers={'x-studio-token':server.TOKEN}))

    def tts(self, p, job, folder):
        self.calls.append('tts')
        rate=16000
        with wave.open(str(folder/'voice.wav'),'wb') as out:
            out.setparams((1,2,rate,0,'NONE','not compressed'))
            out.writeframes(array.array('h',[1000,-1000]*round(p['media']['duration']*rate/2)).tobytes())
        return {'type':'tts','files':['voice.wav'],'sample_rate':rate,'speech_signature':worker.speech_signature(p)}

    def clean(self, p, job, folder):
        self.calls.append('clean')
        shutil.copyfile(p['media']['path'],folder/'clean.mp4')
        return {'type':'clean','files':['clean.mp4'],'signature':core.signature(p)}

    def render_failure(self, p, job, folder):
        self.calls.append('render')
        raise RuntimeError('Fixture encoder failure after preceding verified stages')

    def failure(self, project=None, *, parent=None, created=1):
        project=copy.deepcopy(project or self.p)
        row={'id':uuid.uuid4().hex,'project_id':self.p['id'],'project_revision':self.p['updated'],
            'source_revision':self.p['source_revision'],'kind':'pipeline','state':'error','created':created,
            'configuration':{'pipeline_extract':project['params']['pipeline_extract']},'message':'Fixture render failure'}
        if parent:row['continuation_from']=parent['id']
        folder=self.data/'jobs'/row['id'];folder.mkdir()
        (folder/'input.json').write_text(json.dumps({'project':project,'job':row},ensure_ascii=False),encoding='utf-8')
        core.put('job',row['id'],row)
        with self.assertRaisesRegex(RuntimeError,'Fixture encoder failure'):
            worker.pipeline(project,row,folder)
        return row,folder

    def test_tts_done_render_failure_resumes_without_regeneration_or_editor_mutation(self):
        row,folder=self.failure()
        self.assertEqual(self.calls,['tts','render'])
        journal=load(folder)
        self.assertIn('tts',journal['stages']);self.assertNotIn('render',journal['stages'])
        snapshot,done=recover(self.p,folder)
        self.assertEqual(done,['tts']);self.assertTrue(speech_ready(snapshot))
        self.assertEqual(snapshot['artifacts']['tts']['job_id'],row['id'])
        self.assertEqual(core.get('project',self.p['id']),self.p)
        next_snapshot,root_id,recovery_id,scope=prepare_continuation(row,self.p,[row],folder)
        self.assertEqual(root_id,row['id']);self.assertEqual(recovery_id,row['id'])
        self.calls=[]
        child,_=self.failure(next_snapshot,parent=row,created=2)
        self.assertEqual(self.calls,['render'])
        plan=self.client.get('/api/studio/projects/'+self.p['id']+'/pipeline-continuation').json()
        self.assertEqual(plan['job_id'],child['id'])
        self.assertTrue(plan['can_continue'],plan)

    def test_manual_translation_and_model_change_discard_stale_tts_without_overwrite(self):
        row,folder=self.failure()
        changed=copy.deepcopy(self.p);changed['segments'][0]['translated']='Lời tự sửa đầy đủ'
        snapshot,done=recover(changed,folder)
        self.assertEqual(snapshot['segments'],changed['segments'])
        self.assertNotIn('tts',done);self.assertNotIn('tts',snapshot['artifacts'])
        self.model='fixture-voice-2'
        snapshot,done=recover(self.p,folder)
        self.assertNotIn('tts',done)

    def test_corrupt_verified_payload_and_source_replacement_not_reused(self):
        row,folder=self.failure()
        path=folder/'voice.wav';path.write_bytes(path.read_bytes()[:-100])
        snapshot,done=recover(self.p,folder)
        self.assertNotIn('tts',done)
        self.source.write_bytes(b'changed file at the same source path')
        with self.assertRaisesRegex(ValueError,'nguồn khác'):
            recover(self.p,folder)

    def test_style_change_reuses_tts_and_clean_but_not_render(self):
        self.p['params'].update(pipeline_clean=True,method='blur')
        self.p['regions']=[{'x':10,'y':10,'w':100,'h':30,'start':0,'end':self.p['media']['duration']}]
        row,folder=self.failure()
        changed=copy.deepcopy(self.p);changed['params']['font_size']=60
        snapshot,done=recover(changed,folder)
        self.assertEqual(done,['tts','clean'])
        self.assertTrue(speech_ready(snapshot));self.assertTrue(clean_ready(snapshot))
        self.assertEqual(snapshot['params']['font_size'],60)

    def test_twice_failed_children_retain_stage_only_created_by_first_child(self):
        root,rootfolder=self.failure()
        first,_,_,_=prepare_continuation(root,self.p,[root],rootfolder)
        first['params'].update(pipeline_clean=True,method='blur')
        first['regions']=[{'x':10,'y':10,'w':100,'h':30,'start':0,'end':self.p['media']['duration']}]
        # The user selected cleaning after the root stopped; preserve that
        # current preference while both successors run privately.
        self.p['params'].update(pipeline_clean=True,method='blur')
        self.p['regions']=copy.deepcopy(first['regions']);core.put('project',self.p['id'],self.p)
        child1,folder1=self.failure(first,parent=root,created=2)
        second,_,_,_=prepare_continuation(child1,self.p,[root,child1],folder1)
        self.assertTrue(clean_ready(second))
        self.calls=[]
        child2,folder2=self.failure(second,parent=root,created=3)
        self.assertEqual(self.calls,['render'])
        third,_,_,_=prepare_continuation(child2,self.p,[root,child1,child2],folder2)
        self.assertTrue(speech_ready(third))
        self.assertTrue(clean_ready(third),'Must replay the first child clean stage after the second render-only failure')
        self.assertEqual(third['artifacts']['clean']['job_id'],child1['id'])

    def test_failed_root_and_children_keep_partial_translation_checkpoints(self):
        duration=self.p['media']['duration']
        self.p['segments']=[{'start':duration*i/3,'end':duration*(i+1)/3,'text':'Source '+str(i),'translated':''}
                            for i in range(3)]
        self.p['params'].update(pipeline_translate=True,pipeline_tts=False,voice_gain=0,translation_scope='missing')
        core.put('project',self.p['id'],self.p)
        def partial(index):
            def translate(p,job,folder):
                rows=copy.deepcopy(p['segments']);rows[index]['translated']='Saved translation '+str(index)
                write_checkpoint(folder/'translation-checkpoint.json',p,rows,[index])
                raise RuntimeError('Fixture encoder failure after preceding verified stages')
            return translate
        with patch.dict(worker.TASKS,{'translate':partial(0)}):
            root,rootfolder=self.failure()
        first,_,_,_=prepare_continuation(root,self.p,[root],rootfolder)
        self.assertEqual(first['segments'][0]['translated'],'Saved translation 0')
        with patch.dict(worker.TASKS,{'translate':partial(1)}):
            child1,folder1=self.failure(first,parent=root,created=2)
        second,_,_,_=prepare_continuation(child1,self.p,[root,child1],folder1)
        self.assertEqual(second['segments'][1]['translated'],'Saved translation 1')
        second['params']['pipeline_translate']=False
        child2,folder2=self.failure(second,parent=root,created=3)
        manual=copy.deepcopy(self.p);manual['segments'][0]['translated']='User corrected translation'
        third,_,_,_=prepare_continuation(child2,manual,[root,child1,child2],folder2)
        self.assertEqual(third['segments'][0]['translated'],'User corrected translation')
        self.assertEqual(third['segments'][1]['translated'],'Saved translation 1')
        self.assertEqual(third['segments'][2]['translated'],'')
        self.assertEqual(core.get('project',self.p['id']),self.p)


if __name__=='__main__':unittest.main()
