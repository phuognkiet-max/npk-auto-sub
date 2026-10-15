"""Automation decisions are evidence references, not invented transcripts."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from core import DEFAULTS, DATA, ROOT, put
from ocr_tracks import OCRTrackAssembler, auto_selection, rows_from_selection
from subtitle_quality import source_signature, digest
from auto_subtitle_director import validate_decision, decide, automation_status, evidence_packet


def fixture():
    project = {'id':uuid.uuid4().hex, 'name':'Auto captions fixture','updated':1., 'source_revision':uuid.uuid4().hex,
        'params':dict(DEFAULTS,subtitle_automation='local',pipeline_extract='existing',pipeline_translate=False,
            pipeline_tts=False,pipeline_clean=False,use_clean=False,voice_gain=0,original_gain=0,burn_subtitles=False),
        'media':{'path':str(ROOT/'tests/fixtures/demo.mp4'),'duration':8.,'width':640,'height':360,'fps':25.,'audio':False},
        'regions':[], 'segments':[], 'artifacts':{}}
    tracker = OCRTrackAssembler(640,360,.5)
    for i in range(16):
        observations = [dict(id=f'{i}:0',region_id='fallback',text=f'Caption {i//2}',confidence=.99,bbox=[140,315,500,340]),
                        dict(id=f'{i}:1',region_id='fallback',text='@channel',confidence=.99,bbox=[10,260,110,275])]
        if 6<=i<=7:
            observations.append(dict(id=f'{i}:2',region_id='fallback',text='A different title',confidence=.99,bbox=[80,220,560,242]))
        tracker.observe(i*.5,observations,8.)
    document = tracker.manifest() | {'source_signature':source_signature(project),
        'source_revision':project['source_revision'],'range':[0.,8.],'sampling_interval':.5,'clipped_samples':[]}
    return project,document


def response(document):
    return {'tracks':[{'track_id':t['id'],'role':'subtitle' if t['id']=='ocr-track-2' else 'watermark',
        'confidence':.99,'exceptions':[]} for t in document['tracks']]}


class AutoSubtitleDirectorTests(unittest.TestCase):
    def test_local_sustained_caption_beats_sparse_centred_document(self):
        project,document=fixture()
        selected=auto_selection(document,project)
        self.assertEqual(selected['selected_tracks'],['ocr-track-2'])
        rows=rows_from_selection(document,project,selected)
        self.assertEqual([r['text'] for r in rows],[f'Caption {i}' for i in range(8)])
        self.assertFalse(selected['requires_review'])
        self.assertFalse(selected['coverage_complete'])

    def test_gemini_selects_full_track_without_rewriting_text_times(self):
        project,document=fixture(); original=copy.deepcopy(document)
        selected=validate_decision(response(document),document)
        rows=rows_from_selection(document,project,selected)
        self.assertEqual([r['text'] for r in rows],[f'Caption {i}' for i in range(8)])
        self.assertEqual(rows[-1]['end'],8.)
        self.assertEqual(document,original)

    def test_run_credit_exclusion_keeps_all_other_words_and_raw_credit(self):
        project,document=fixture(); value=response(document)
        primary=next(t for t in value['tracks'] if t['role']=='subtitle')
        primary['exceptions']=[{'run_index':7,'role':'credit','confidence':.99}]
        selected=validate_decision(value,document)
        self.assertEqual(selected['excluded_runs'],['ocr-track-2:run-7'])
        rows=rows_from_selection(document,project,selected)
        self.assertEqual(len(rows),7)
        self.assertEqual(document['tracks'][1]['runs'][-1]['text'],'Caption 7')

    def test_uncertain_exclusion_never_automatically_removes_the_caption(self):
        project,document=fixture(); value=response(document)
        primary=next(t for t in value['tracks'] if t['role']=='subtitle')
        primary['exceptions']=[{'run_index':3,'role':'credit','confidence':.4}]
        selected=validate_decision(value,document)
        self.assertTrue(selected['requires_review'])
        self.assertNotIn('ocr-track-2:run-3',selected['excluded_runs'])
        self.assertEqual(len(rows_from_selection(document,project,selected)),8)

    def test_missing_duplicate_unknown_rewritten_and_bad_refs_rejected(self):
        project,document=fixture(); good=response(document)
        cases=[{'tracks':good['tracks'][:-1]}, {'tracks':good['tracks']+[good['tracks'][0]]},
            {'tracks':[dict(good['tracks'][0],track_id='arbitrary-file')]},
            {'tracks':[dict(item,rewritten_text='invented') for item in good['tracks']]},
            {'tracks':[dict(item,confidence=float('nan')) for item in good['tracks']]},
            {'tracks':[dict(item,exceptions=[{'run_index':True,'role':'credit','confidence':.99}]) for item in good['tracks']]}]
        for value in cases:
            with self.subTest(value=value),self.assertRaises(ValueError):validate_decision(value,document)

    def test_opt_in_enforced_before_any_gemini_credentials_or_network(self):
        from auto_subtitle_director import request_decision
        project,document=fixture()
        with patch('auto_subtitle_director._secret') as secret,self.assertRaises(ValueError):
            request_decision(project,document)
        secret.assert_not_called()

    def test_gemini_rest_request_has_schema_bounded_images_and_same_selected_model(self):
        import httpx
        from auto_subtitle_director import request_decision, SCHEMA
        project,document=fixture();project['params']['subtitle_automation']='gemini'
        project['params']['translation_model']='gemini-fixture-model'
        captured=[]
        class Client:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def post(self,url,**options):
                captured.append((url,options))
                return httpx.Response(200,json={'candidates':[{'finishReason':'STOP','content':{
                    'parts':[{'text':json.dumps(response(document))}]}}]})
        images=[{'text':'frame 1'},{'inlineData':{'mimeType':'image/jpeg','data':'fixture'}}]
        with patch('auto_subtitle_director._secret',return_value='fixture-key'),patch('httpx.Client',return_value=Client()), \
                patch('auto_subtitle_director.image_parts',return_value=images):
            result=request_decision(project,document)
        self.assertEqual(result,response(document));self.assertEqual(len(captured),1)
        url,options=captured[0]
        self.assertIn('/gemini-fixture-model:generateContent',url)
        self.assertEqual(options['json']['generationConfig']['responseJsonSchema'],SCHEMA)
        self.assertEqual(options['json']['contents'][0]['parts'][-2:],images)
        self.assertNotIn('tools',options['json'])

    def test_cached_gemini_plan_revalidated_without_another_request(self):
        project,document=fixture();project['params']['subtitle_automation']='gemini'
        with tempfile.TemporaryDirectory(dir=DATA) as temporary:
            with patch('auto_subtitle_director.request_decision',return_value=response(document)) as request:
                first=decide(project,document,Path(temporary))
                again=decide(project,document,Path(temporary))
            self.assertEqual(request.call_count,1)
            self.assertEqual(first['segments'],again['segments'])
            saved=json.loads((Path(temporary)/'subtitle-automation.json').read_text(encoding='utf-8'))
            saved['decision']['tracks'].pop()
            (Path(temporary)/'subtitle-automation.json').write_text(json.dumps(saved),encoding='utf-8')
            with patch('auto_subtitle_director.request_decision') as request,self.assertRaises(ValueError):
                decide(project,document,Path(temporary))
            request.assert_not_called()

    def test_worker_inconclusive_keeps_current_table_and_reports_proposal(self):
        import worker
        project,document=fixture();project['segments']=[dict(start=0.,end=8.,text='Manual old row',translated='Kept edit')]
        project['params']['subtitle_automation']='gemini';value=response(document)
        for item in value['tracks']:item['confidence']=.3
        with tempfile.TemporaryDirectory(dir=DATA) as temporary,patch('auto_subtitle_director.request_decision',return_value=value):
            result=worker._automate_ocr_result(project,{'id':uuid.uuid4().hex},Path(temporary),document,
                {'type':'auto_subtitles','files':['ocr-tracks.json'],'coverage':{}})
            self.assertTrue(result['needs_review']);self.assertEqual(result['segments'],project['segments'])
            self.assertEqual(project['segments'][0]['translated'],'Kept edit')
            self.assertTrue(result['coverage']['ocr_selection']['requires_review'])

    def test_saved_inconclusive_decision_does_not_loop_or_override_manual_selection(self):
        from subtitle_quality import digest as evidence_digest
        project,document=fixture()
        project['params']['subtitle_automation']='gemini'
        project['coverage']={'ocr_selection':{'automation_mode':'gemini','requires_review':True,
            'manifest_key':evidence_digest(document)}}
        with patch('auto_subtitle_director.evidence_for_project',return_value=({'id':'a'*32},document)):
            status=automation_status(project)
            self.assertEqual(status['status'],'review');self.assertFalse(status['can_process'])
            project['coverage']['ocr_selection'].update(mode='user_selected_tracks',requires_review=False,automation_mode='manual')
            status=automation_status(project)
            self.assertEqual(status['status'],'selected');self.assertFalse(status['can_process'])

    def test_api_queue_copies_owned_evidence_and_deduplicates_jobs(self):
        from fastapi.testclient import TestClient
        import server
        project,document=fixture();job_id=uuid.uuid4().hex
        folder=DATA/'jobs'/job_id;folder.mkdir()
        (folder/'ocr-tracks.json').write_text(json.dumps(document),encoding='utf-8')
        cached=decide(project,document,folder)['report']
        put('project',project['id'],project)
        put('job',job_id,dict(id=job_id,project_id=project['id'],source_revision=project['source_revision'],kind='ocr',state='done',created=1.))
        with TestClient(server.app) as client:
            headers={'x-studio-token':server.TOKEN}
            body=dict(project_id=project['id'],kind='auto_subtitles',expected_revision=1.,replace_segments=True)
            first=client.post('/api/jobs',json=body,headers=headers)
            self.assertEqual(first.status_code,200,first.text)
            second=client.post('/api/jobs',json=dict(body,request_id=uuid.uuid4().hex),headers=headers)
            self.assertEqual(second.status_code,200,second.text)
            self.assertEqual(first.json()['id'],second.json()['id'])
            self.assertTrue((DATA/'jobs'/first.json()['id']/'ocr-tracks.json').is_file())
            restored=json.loads((DATA/'jobs'/first.json()['id']/'subtitle-automation.json').read_text(encoding='utf-8'))
            self.assertEqual(restored['key'],cached['key'])
        self.assertTrue(automation_status(project)['can_process'])

    def test_packet_bound_cannot_send_arbitrary_large_run_data(self):
        project,document=fixture();document['tracks'][0]['runs'][0]['text']='x'*700000
        with self.assertRaises(ValueError):evidence_packet(document,project)
