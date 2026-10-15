"""Complete native OCR survives a cloud failure, even before stage journals."""
from contextlib import ExitStack
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import core
import studio_api
from ocr_checkpoint import signature
from ocr_tracks import OCRTrackAssembler
from pipeline_resume import recovery_snapshot, failed_continuation_status
from subtitle_quality import source_signature, coverage_record, digest


class CompleteOCRCloudRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.data = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='npk-semantic-recovery-')))
        (self.data/'jobs').mkdir()
        for module in (core, studio_api):
            self.stack.enter_context(patch.object(module, 'DATA', self.data))
        for target in ('worker.create_ocr', 'worker.read_frame', 'auto_subtitle_director.request_decision'):
            self.stack.enter_context(patch(target, side_effect=AssertionError('Recovery must not decode/infer/call provider')))
        source = self.data/'source.mp4'
        source.write_bytes(b'only the local source identity is inspected')
        self.project = {'id':uuid.uuid4().hex, 'name':'Cloud outage fixture', 'updated':2., 'created':1.,
            'source_revision':uuid.uuid4().hex, 'segments':[], 'regions':[], 'extraction_regions':[], 'artifacts':{},
            'media':{'path':str(source), 'duration':8., 'video_duration':8., 'width':640, 'height':360, 'fps':25., 'audio':True},
            'params':dict(core.DEFAULTS, subtitle_automation='gemini', pipeline_extract='ocr',
                pipeline_translate=True, pipeline_tts=False, pipeline_clean=False, use_clean=False,
                voice_gain=0, burn_subtitles=False)}
        self.original = copy.deepcopy(self.project)
        self.row = {'id':uuid.uuid4().hex, 'project_id':self.project['id'], 'source_revision':self.project['source_revision'],
            'kind':'pipeline', 'state':'error', 'created':3., 'message':'HTTP 503 after native OCR', 'result':None}
        self.folder = self.data/'jobs'/self.row['id']
        self.folder.mkdir()
        self.write('input.json', {'project':self.original, 'job':self.row})
        tracker = OCRTrackAssembler(640,360,.5)
        evidence = self.folder/'ocr-observations.jsonl'
        with evidence.open('wb') as stream:
            for index in range(16):
                observations = [dict(id=f'{index}:0', region_id='fallback', text=f'Caption {index//2}',
                    confidence=.99, bbox=[140,315,500,340]), dict(id=f'{index}:1', region_id='fallback',
                    text='@watermark', confidence=.99, bbox=[10,260,110,275])]
                components = tracker.observe(index*.5, observations, 8.)
                stream.write((json.dumps(dict(sample_id=index, time=index*.5, observations=observations,
                    components=components))+'\n').encode('utf-8'))
            tracker.state['evidence_bytes'] = stream.tell()
        rows = tracker.rows()
        self.checkpoint = {'signature':signature(self.project), 'rows':rows[:-1], 'current':rows[-1],
            'next_time':8., 'clipped_samples':[], 'decoded_complete':True, 'ocr_tracks':tracker.state}
        self.manifest = tracker.manifest() | {'source_signature':source_signature(self.project),
            'source_revision':self.project['source_revision'], 'range':[0.,8.], 'sampling_interval':.5,
            'checkpoint_signature':self.checkpoint['signature'], 'clipped_samples':[]}
        self.write('recognition-checkpoint.json', self.checkpoint)
        self.write('ocr-tracks.json', self.manifest)
        (self.folder/'source.srt').write_text('native fixture table', encoding='utf-8')

    def write(self, name, document):
        (self.folder/name).write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')

    def recover(self):
        return recovery_snapshot(self.row, self.project, [self.row], self.folder)

    def test_complete_rc2_cloud_failure_recovers_all_rows_in_private_snapshot(self):
        before = copy.deepcopy(self.project)
        evidence_before = {path.name:path.read_bytes() for path in self.folder.iterdir()}
        snapshot, root, restored, missing, selected = self.recover()
        self.assertEqual([row['text'] for row in snapshot['segments']], [f'Caption {i}' for i in range(8)])
        self.assertEqual(snapshot['params']['pipeline_extract'], 'existing')
        self.assertEqual(snapshot['params']['subtitle_automation'], 'gemini')
        self.assertEqual(snapshot['coverage']['ocr_selection']['automation_mode'], 'local')
        self.assertEqual(snapshot['artifacts']['ocr']['job_id'], self.row['id'])
        self.assertTrue(snapshot['artifacts']['ocr']['checkpoint_complete'])
        self.assertEqual((root, restored, missing, selected), (self.row, 0, list(range(8)), None))
        self.assertEqual(self.project, before)
        self.assertEqual({path.name:path.read_bytes() for path in self.folder.iterdir()}, evidence_before)
        self.assertFalse((self.folder/'stage-journal.json').exists())

    def test_automatic_pending_can_resolve_review_before_translation(self):
        quality = {'requires_review':True, 'errors':[], 'issues':[{'severity':'review','message':'raw ambiguity'}]}
        with patch('preflight.inspect', return_value={'ok':True,'errors':[],'warnings':[]}), \
                patch('auto_subtitle_director.automation_status', return_value={'can_process':True}), \
                patch('subtitle_quality.inspect_subtitles', return_value=quality):
            plan = failed_continuation_status(self.row, self.project, [self.row], self.folder)
        self.assertTrue(plan['can_continue'], plan)
        self.assertFalse(plan['requires_review'])
        self.assertTrue(plan['recognition_complete'])
        self.assertEqual(plan['recognition_count'], 8)
        self.assertEqual(plan['completed_steps'], ['ocr'])
        self.assertEqual(plan['remaining_steps'], ['auto_subtitles','translate','render'])

    def test_decoder_incomplete_or_changed_source_or_config_is_never_reused(self):
        self.checkpoint['decoded_complete'] = False
        self.write('recognition-checkpoint.json', self.checkpoint)
        with self.assertRaisesRegex(ValueError, 'giải mã'):
            self.recover()
        self.checkpoint['decoded_complete'] = True
        self.write('recognition-checkpoint.json', self.checkpoint)
        self.project['params']['ocr_fps'] = 4.
        with self.assertRaisesRegex(ValueError, 'không khớp'):
            self.recover()
        self.project['params']['ocr_fps'] = 2.
        Path(self.project['media']['path']).write_bytes(b'different source')
        with self.assertRaisesRegex(ValueError, 'không khớp'):
            self.recover()

    def test_inconsistent_manifest_or_truncated_log_is_not_promoted_to_complete(self):
        self.manifest['tracks'][1]['runs'][0]['text'] = 'Unbound text'
        self.write('ocr-tracks.json', self.manifest)
        with self.assertRaisesRegex(ValueError, 'không khớp'):
            self.recover()
        self.manifest['tracks'][1]['runs'][0]['text'] = 'Caption 0'
        self.write('ocr-tracks.json', self.manifest)
        evidence = self.folder/'ocr-observations.jsonl'
        evidence.write_bytes(evidence.read_bytes()[:-4])
        with self.assertRaisesRegex(ValueError, 'số byte'):
            self.recover()

    def test_same_size_changed_raw_content_is_rejected(self):
        evidence = self.folder/'ocr-observations.jsonl'
        raw = evidence.read_bytes()
        raw = raw.replace(b'Caption 0', b'Caption X', 2)
        evidence.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, 'không khớp'):
            self.recover()

    def test_current_editor_edits_and_selected_scope_are_never_overwritten(self):
        self.project['segments'] = [{'start':0., 'end':8., 'text':'Manually entered', 'translated':'Manual'}]
        before = copy.deepcopy(self.project)
        with self.assertRaisesRegex(ValueError, 'đã được chỉnh'):
            self.recover()
        self.assertEqual(self.project, before)
        self.project['segments'] = []
        self.row['segment_indices'] = [0]
        self.write('input.json', {'project':self.original, 'job':self.row})
        # Old empty input cannot justify applying a selected index to a newly
        # reconstructed eight-row table.
        with self.assertRaisesRegex(ValueError, 'Phạm vi dịch'):
            self.recover()

    def test_structural_errors_or_active_job_still_block_automatic_continue(self):
        with patch('preflight.inspect', return_value={'ok':True,'errors':[],'warnings':[]}), \
                patch('auto_subtitle_director.automation_status', return_value={'can_process':True}), \
                patch('subtitle_quality.inspect_subtitles', return_value={'requires_review':False,
                    'errors':['Invalid cue time'], 'issues':[]}):
            plan = failed_continuation_status(self.row, self.project, [self.row], self.folder)
        self.assertFalse(plan['can_continue'])
        self.assertIn('Invalid cue time', plan['errors'])
        active = {'id':uuid.uuid4().hex, 'project_id':self.project['id'], 'state':'running'}
        with patch('preflight.inspect', return_value={'ok':True,'errors':[],'warnings':[]}), \
                patch('auto_subtitle_director.automation_status', return_value={'can_process':True}):
            plan = failed_continuation_status(self.row, self.project, [self.row,active], self.folder)
        self.assertFalse(plan['can_continue'])
        self.assertIn('tác vụ khác', plan['reason'])

    def record_native_stage(self):
        from stage_journal import record_stage
        raw_rows = self.checkpoint['rows'] + [self.checkpoint['current']]
        coverage = coverage_record(self.project, 'ocr', 0., 8., ocr_selection=self.manifest['selection'])
        result = {'type':'ocr', 'segments':raw_rows, 'coverage':coverage,
            'files':['source.srt','ocr-tracks.json','ocr-observations.jsonl'],
            'verification':{'fixture':'native evidence validated'}}
        record_stage(self.original, self.original, self.row, self.folder, 'ocr', result)
        return dict(copy.deepcopy(self.project), segments=copy.deepcopy(raw_rows), coverage=coverage)

    def test_new_journal_keeps_raw_ocr_recoverable_before_cloud_selection(self):
        from stage_journal import recover
        raw = self.record_native_stage()
        snapshot, completed = recover(self.project, self.folder)
        self.assertEqual(snapshot['segments'], raw['segments'])
        self.assertEqual(completed, ['ocr'])
        self.assertEqual(snapshot['artifacts']['ocr']['job_id'], self.row['id'])
        self.assertEqual(snapshot['artifacts']['ocr']['files'], ['source.srt','ocr-tracks.json','ocr-observations.jsonl'])

    def test_new_journal_recovers_selected_corpus_transition_and_preserves_manual_values(self):
        from stage_journal import record_stage, recover
        raw = self.record_native_stage()
        selected = copy.deepcopy(raw['segments'][:-1])
        selection = dict(self.manifest['selection'], automation_mode='gemini',
            manifest_key=digest(self.manifest), requires_review=False)
        coverage = coverage_record(self.project, 'ocr', 0., 8., ocr_selection=selection)
        self.write('subtitle-automation.json', {'fixture':'semantic decision references only'})
        (self.folder/'selected-source.srt').write_text('selected fixture table', encoding='utf-8')
        result = {'type':'auto_subtitles','segments':selected,'coverage':coverage,
            'files':['selected-source.srt','subtitle-automation.json','ocr-tracks.json'],
            'verification':{'fixture':'selection references validated'}}
        record_stage(self.original, raw, self.row, self.folder, 'auto_subtitles', result)
        snapshot, completed = recover(self.project, self.folder)
        self.assertEqual(snapshot['segments'], selected)
        self.assertEqual(completed, ['ocr','auto_subtitles'])
        self.assertEqual(snapshot['artifacts']['ocr']['job_id'], self.row['id'])
        self.assertEqual(snapshot['artifacts']['auto_subtitles']['job_id'], self.row['id'])
        current = copy.deepcopy(snapshot)
        current['segments'][2].update(translated='Manual translation', speech_enabled=False, voice_id='vieneu:fixture')
        again, completed = recover(current, self.folder)
        self.assertEqual(again['segments'], current['segments'])
        self.assertIn('auto_subtitles', completed)
        self.assertEqual((self.folder/'source.srt').read_text(encoding='utf-8'), 'native fixture table')

    def test_explicit_manual_track_selection_survives_raw_and_inconclusive_auto_replay(self):
        from stage_journal import record_stage, recover
        from ocr_tracks import rows_from_manifest
        from auto_subtitle_director import automation_status
        from subtitle_quality import coverage_record
        from test_auto_subtitle_director import response
        raw = self.record_native_stage()
        document = self.manifest
        chosen = document['tracks'][0]['id']
        manual_rows = rows_from_manifest(document, self.project, [chosen])
        selection = {'mode':'user_selected_tracks','selected_tracks':[chosen],
            'requires_review':False,'manifest_key':digest(document),'job_id':self.row['id']}
        current = copy.deepcopy(self.project)
        current.update(segments=manual_rows,
            coverage=coverage_record(current,'ocr',0.,8.,ocr_selection=selection))
        unresolved = response(document)
        for track in unresolved['tracks']:
            track['confidence'] = .25
        saved = {'version':1,'key':'fixture','mode':'gemini','selection':{'requires_review':True},
            'decision':unresolved,'requires_review':True,'cue_count':0,'model':'fixture'}
        (self.folder/'subtitle-automation.json').write_text(json.dumps(saved),encoding='utf-8')
        proposed = copy.deepcopy(raw['segments'][:-1])
        (self.folder/'selected-source.srt').write_text('inconclusive proposal',encoding='utf-8')
        record_stage(self.original,raw,self.row,self.folder,'auto_subtitles',
            {'type':'auto_subtitles','segments':proposed,'coverage':coverage_record(self.project,'ocr',0.,8.,
                ocr_selection={'automation_mode':'gemini','requires_review':True,'manifest_key':digest(document)}),
             'files':['selected-source.srt','subtitle-automation.json','ocr-tracks.json'],
             'verification':{'fixture':'proposal cached and verified'}})
        before = copy.deepcopy(current)
        recovered, completed = recover(current,self.folder)
        self.assertEqual(recovered['segments'],before['segments'])
        self.assertEqual(recovered['coverage'],before['coverage'])
        self.assertNotIn('auto_subtitles',completed)
        with patch('auto_subtitle_director.evidence_for_project',return_value=(self.row,document)):
            self.assertFalse(automation_status(recovered)['can_process'])
        self.assertEqual(current,before)

    def test_pending_selection_does_not_disable_requested_translation(self):
        from pipeline_resume import journal_recovery_snapshot
        from subtitle_quality import coverage_record
        raw = self.record_native_stage()
        current = copy.deepcopy(raw)
        for cue in current['segments']:
            cue['translated'] = 'Đã dịch câu đầy đủ'
        pending = dict(self.manifest['selection'],automation_mode='gemini',manifest_key=digest(self.manifest))
        current['coverage'] = coverage_record(current,'ocr',0.,8.,ocr_selection=pending)
        with patch('pipeline_resume._pending_selection',return_value=True):
            snapshot, completed = journal_recovery_snapshot(self.row,current,self.folder)
        self.assertEqual(completed,['ocr'])
        self.assertTrue(snapshot['params']['pipeline_translate'])


if __name__ == '__main__':
    unittest.main()
