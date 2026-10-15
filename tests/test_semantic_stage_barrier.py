"""Fault injection at the boundary between completed OCR and remote decisions."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from core import DATA, save_srt
from test_auto_subtitle_director import fixture, response
from providers import ProviderError
from subtitle_quality import coverage_record
from stage_journal import load, recover, summary
from job_progress import accept_event
import worker


class SemanticStageBarrierTests(unittest.TestCase):
    def test_gemini_503_cannot_erase_verified_ocr_or_invoke_later_steps(self):
        project,document=fixture();project['params'].update(pipeline_extract='ocr',subtitle_automation='gemini')
        project['segments']=[];original=copy.deepcopy(project)
        from ocr_tracks import rows_from_selection
        rows=rows_from_selection(document,project,document['selection'])
        job={'id':uuid.uuid4().hex,'kind':'pipeline','project_id':project['id'],'source_revision':project['source_revision']}
        def raw_ocr(p,j,folder):
            save_srt(rows,folder/'source.srt',translated=False)
            (folder/'ocr-tracks.json').write_text(json.dumps(document),encoding='utf-8')
            (folder/'ocr-observations.jsonl').write_text('{"fixture":true}\n',encoding='utf-8')
            return {'type':'ocr','files':['source.srt','ocr-tracks.json','ocr-observations.jsonl'],
                'segments':rows,'coverage':coverage_record(p,'ocr',0,8,sampling_interval=.5,ocr_selection=document['selection'])}
        with tempfile.TemporaryDirectory(dir=DATA) as temporary:
            folder=Path(temporary)
            with patch.dict(worker.TASKS,ocr=raw_ocr),patch('worker.emit'), \
                 patch('auto_subtitle_director.request_decision',side_effect=ProviderError('service','HTTP 503',http_status=503)), \
                 patch.dict(worker.TASKS,render=lambda *_:self.fail('render cannot run after semantic failure')):
                with self.assertRaises(ProviderError):worker.pipeline(project,job,folder)
            stored=load(folder)
            self.assertIn('ocr',stored['stages']);self.assertNotIn('auto_subtitles',stored['stages'])
            snapshot,completed=recover(original,folder)
            self.assertEqual(completed,['ocr']);self.assertEqual(snapshot['segments'],rows)
            self.assertEqual(summary(folder)['completed_steps'],['ocr'])
            self.assertTrue((folder/'source.srt').is_file())

    def test_selected_transcript_does_not_overwrite_raw_ocr_file_or_invalidate_hash(self):
        project,document=fixture();project['params'].update(pipeline_extract='ocr',subtitle_automation='gemini')
        project['segments']=[];original=copy.deepcopy(project)
        from ocr_tracks import rows_from_selection
        rows=rows_from_selection(document,project,document['selection'])
        job={'id':uuid.uuid4().hex,'kind':'pipeline','project_id':project['id'],'source_revision':project['source_revision']}
        def raw_ocr(p,j,folder):
            save_srt(rows,folder/'source.srt',translated=False)
            (folder/'ocr-tracks.json').write_text(json.dumps(document),encoding='utf-8')
            (folder/'ocr-observations.jsonl').write_text('{"fixture":true}\n',encoding='utf-8')
            return {'type':'ocr','files':['source.srt','ocr-tracks.json','ocr-observations.jsonl'],
                'segments':rows,'coverage':coverage_record(p,'ocr',0,8,sampling_interval=.5,ocr_selection=document['selection'])}
        value=response(document)
        next(track for track in value['tracks'] if track['role']=='subtitle')['exceptions']=[
            {'run_index':7,'role':'credit','confidence':.99}]
        with tempfile.TemporaryDirectory(dir=DATA) as temporary:
            folder=Path(temporary)
            with patch.dict(worker.TASKS,ocr=raw_ocr,render=lambda *_:(_ for _ in ()).throw(RuntimeError('encoder fixture'))), \
                 patch('worker.emit'),patch('auto_subtitle_director.request_decision',return_value=value):
                with self.assertRaisesRegex(RuntimeError,'encoder fixture'):worker.pipeline(project,job,folder)
            self.assertEqual(summary(folder)['completed_steps'],['ocr','auto_subtitles'])
            self.assertIn('Caption 7',(folder/'source.srt').read_text(encoding='utf-8'))
            self.assertNotIn('Caption 7',(folder/'selected-source.srt').read_text(encoding='utf-8'))
            self.assertEqual(len(recover(original,folder)[0]['segments']),7)

    def test_progress_keeps_semantic_failure_action_and_http_code(self):
        current={'state':'running','stage':'2/6:auto_subtitles'}
        accepted=accept_event(current,{'failure_code':'provider_service','failure_stage':'auto_subtitles',
            'http_status':503,'next_action':'continue_auto_subtitles','message':'HTTP 503'})
        self.assertEqual(accepted['failure_stage'],'auto_subtitles')
        self.assertEqual(accepted['http_status'],503)
        self.assertEqual(accepted['next_action'],'continue_auto_subtitles')
