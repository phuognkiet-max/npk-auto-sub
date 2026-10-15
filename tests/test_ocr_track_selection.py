"""Explicit OCR-track editing API, with owned evidence and revision history."""
from contextlib import ExitStack
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
import storage_manager
from ocr_tracks import OCRTrackAssembler
from subtitle_quality import source_signature


def observation(text,bbox,index):
    return {'id':str(index),'region_id':'fallback','text':text,'bbox':bbox,'confidence':.99}


class OCRTrackSelectionTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.data=Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=core.DATA,prefix='track-selection-')))
        (self.data/'jobs').mkdir()
        for module in (core,server,studio_api,storage_manager):
            self.stack.enter_context(patch.object(module,'DATA',self.data))
        self.source=self.data/'source.mp4';self.source.write_bytes(b'fixture source identity')
        self.project={'id':uuid.uuid4().hex,'name':'Track selection fixture','created':1.,'updated':1.,
            'source_revision':'fixture-source','regions':[],'extraction_regions':[],'params':copy.deepcopy(core.DEFAULTS),
            'media':{'path':str(self.source),'duration':2.,'width':1280,'height':720,'fps':30.,'audio':False},
            'segments':[{'start':0.,'end':1.,'text':'你好','translated':'Xin chào','speaker_id':'alice','voice_id':'piper:vais1000'},
                        {'start':1.,'end':2.,'text':'Sửa tay','translated':'Bản dịch sửa tay'}],
            'artifacts':{},'coverage':{},'subtitle_review':{'signature':'older-review'}}
        core.put('project',self.project['id'],self.project)
        self.job={'id':uuid.uuid4().hex,'kind':'ocr','project_id':self.project['id'],
            'source_revision':self.project['source_revision'],'state':'done','created':2.}
        core.put('job',self.job['id'],self.job)
        self.folder=self.data/'jobs'/self.job['id'];self.folder.mkdir()
        tracker=OCRTrackAssembler(1280,720,1.)
        for index,text in enumerate(['你好','再见']):
            observations=[observation(text,[300,650,980,685],0),
                          observation('Watermark'+str(index),[10,630,160,648],1)]
            observations += [observation('Đoạn tài liệu '+str(row),[220,440+row*26,1100,462+row*26],row+2)
                             for row in range(6)]
            tracker.observe(float(index),observations,2.)
        self.document=tracker.manifest()|{'source_signature':source_signature(self.project),
            'source_revision':self.project['source_revision'],'range':[0.,2.],'sampling_interval':1.}
        self.path=self.folder/'ocr-tracks.json'
        self.path.write_text(json.dumps(self.document,ensure_ascii=False),encoding='utf-8')
        self.caption=next(track['id'] for track in self.document['tracks'] if track['runs'][0]['text']=='你好')
        self.watermark=next(track['id'] for track in self.document['tracks'] if track['runs'][0]['text'].startswith('Watermark'))
        self.document_id=next(track['id'] for track in self.document['tracks'] if track['max_lines']>3)
        self.client=self.stack.enter_context(TestClient(server.app,headers={'x-studio-token':server.TOKEN}))
        self.endpoint='/api/studio/projects/'+self.project['id']+'/ocr-tracks'

    def body(self,ids):
        info=self.client.get(self.endpoint).json()
        return {key:info[key] for key in ('expected_revision','source_revision','source_signature','manifest_key','job_id')}|{'track_ids':ids}

    def test_read_is_nonmutating_and_lists_all_layers_with_real_text(self):
        response=self.client.get(self.endpoint)
        self.assertEqual(response.status_code,200,response.text)
        data=response.json()
        self.assertTrue(data['available'])
        self.assertEqual(len(data['tracks']),3)
        self.assertEqual(core.get('project',self.project['id']),self.project)
        self.assertTrue(next(track for track in data['tracks'] if track['id']==self.caption)['selected'])

    def test_explicit_caption_selection_preserves_exact_match_translation_and_full_history(self):
        response=self.client.post(self.endpoint,json=self.body([self.caption]))
        self.assertEqual(response.status_code,200,response.text)
        result=response.json()
        self.assertEqual([row['text'] for row in result['segments']],['你好','再见'])
        self.assertEqual(result['segments'][0]['translated'],'Xin chào')
        self.assertEqual(result['segments'][0]['voice_id'],'piper:vais1000')
        self.assertEqual(result['segments'][1]['translated'],'')
        self.assertEqual(core.get('project_backup',self.project['id'])['segments'],self.project['segments'])
        self.assertNotIn('subtitle_review',result)
        self.assertEqual(result['coverage']['ocr_selection']['mode'],'user_selected_tracks')
        self.assertEqual(result['coverage']['ocr_selection']['job_id'],self.job['id'])

    def test_user_can_choose_other_layer_but_loading_does_not_do_so(self):
        response=self.client.post(self.endpoint,json=self.body([self.watermark]))
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual([row['text'] for row in response.json()['segments']],['Watermark0','Watermark1'])

    def test_document_is_selected_only_explicitly_and_preserves_every_line(self):
        response=self.client.post(self.endpoint,json=self.body([self.document_id]))
        self.assertEqual(response.status_code,200,response.text)
        rows=response.json()['segments']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['text'],'\n'.join('Đoạn tài liệu '+str(index) for index in range(6)))
        self.assertEqual(rows[0]['end'],2.)

    def test_stale_revision_source_and_manifest_never_replace_existing_table(self):
        body=self.body([self.caption])
        for field,value in [('expected_revision',0.),('expected_revision',True),('source_revision','previous'),
                            ('source_signature','other-source'),('manifest_key','changed')]:
            changed=body|{field:value}
            response=self.client.post(self.endpoint,json=changed)
            self.assertEqual(response.status_code,409,response.text)
            self.assertEqual(core.get('project',self.project['id']),self.project)

    def test_empty_unknown_duplicate_and_malformed_track_selections_fail_without_write(self):
        for ids in ([],['unknown'],[self.caption,self.caption],[{'id':self.caption}],True):
            response=self.client.post(self.endpoint,json=self.body(ids))
            self.assertEqual(response.status_code,400,response.text)
            self.assertEqual(core.get('project',self.project['id']),self.project)

    def test_busy_project_refuses_track_corpus_mutation(self):
        core.put('job','active',{'id':'active','project_id':self.project['id'],'state':'queued','kind':'pipeline','created':3.})
        response=self.client.post(self.endpoint,json=self.body([self.caption]))
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(core.get('project',self.project['id']),self.project)

    def test_unrelated_or_changed_source_evidence_is_not_available(self):
        changed=copy.deepcopy(self.project)
        changed['source_revision']='replacement-source'
        core.put('project',changed['id'],changed)
        response=self.client.get(self.endpoint)
        self.assertEqual(response.status_code,200,response.text)
        self.assertFalse(response.json()['available'])
        self.assertEqual(core.get('project',changed['id']),changed)


if __name__=='__main__':
    unittest.main()
