"""Real local API/storage/ZIP contracts in isolated data; no model/API calls."""
from contextlib import ExitStack
import copy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid
import zipfile

from fastapi.testclient import TestClient
import core
import server
import studio_api
import studio_preferences
import speech_library
import storage_manager
import project_bundle
import process_control
from fixture_hardware import CPU_HARDWARE


class OperationsUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('hardware_admission.hardware_info', return_value=CPU_HARDWARE))
        self.data = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='operations-', dir=core.DATA)))
        for directory in ('jobs', 'frames'):
            (self.data/directory).mkdir()
        for module in (core, server, studio_api, speech_library, storage_manager, project_bundle, process_control):
            self.stack.enter_context(patch.object(module, 'DATA', self.data))
        self.stack.enter_context(patch.object(server, 'active_id', None))
        self.stack.enter_context(patch.object(server, 'active_process', None))
        self.client = self.stack.enter_context(TestClient(server.app, headers={'x-studio-token': server.TOKEN}))

    def project(self):
        media = core.probe(core.ROOT/'tests/fixtures/demo.mp4')
        p = {'id':uuid.uuid4().hex, 'name':'Dự án thử', 'updated':1., 'created':1., 'media':media,
             'params':copy.deepcopy(core.DEFAULTS), 'segments':[{'start':0.,'end':1.,'text':'Hello','translated':'Xin chào'}],
             'regions':[], 'extraction_regions':[], 'artifacts':{}}
        return core.put('project', p['id'], p)

    def speech(self, state='done'):
        identifier = uuid.uuid4().hex
        folder = self.data/'jobs'/identifier
        folder.mkdir()
        (folder/'speech.wav').write_bytes(b'WAV fixture for metadata')
        (folder/'speech.mp3').write_bytes(b'MP3 fixture for metadata')
        (folder/'input.json').write_text('{}')
        job = {'id':identifier, 'kind':'text_to_speech', 'project_id':'text-to-speech', 'created':1., 'state':state,
               'configuration':{'voice_id':'piper:vais1000'},
               'result':{'files':['speech.wav','speech.mp3'], 'duration':2., 'voice_id':'piper:vais1000'}}
        return core.put('job', identifier, job), folder

    def test_preferences_merge_persist_between_origins_and_reject_private_fields(self):
        response = self.client.patch('/api/studio/preferences', json={'tts_text':'Lời đọc cần giữ', 'favorite_voices':['vieneu:Hải Đăng']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.put('/api/studio/preferences', json={'viewer_ratio':60}).status_code, 200)
        with TestClient(server.app, base_url='http://localhost:21567', headers={'x-studio-token':server.TOKEN}) as second:
            stored = second.get('/api/studio/preferences').json()
        self.assertEqual(stored['tts_text'], 'Lời đọc cần giữ')
        self.assertEqual(stored['viewer_ratio'], 60)
        self.assertEqual(self.client.patch('/api/studio/preferences', json={'key':'private'}).status_code, 400)
        self.assertNotIn('key', core.get('ui_preferences', 'main'))

    def test_preferences_bound_nested_script_and_parameters(self):
        data = {'tts_params':{'tts_device':'cpu','workload_mode':'quiet','pronunciation_dictionary':'NPK => en pi kê'},
                'narration_segments':[{'text':'Xin chào', 'voice_id':'piper:vais1000', 'rate':1.1, 'pause_after_ms':500}]}
        self.assertEqual(self.client.patch('/api/studio/preferences', json=data).status_code, 200)
        for bad in ({'tts_params':{'key':'no'}}, {'narration_segments':[{'text':'a'*10001}]}, {'viewer_ratio':True}):
            self.assertEqual(self.client.patch('/api/studio/preferences', json=bad).status_code, 400)

    def test_saved_empty_preferences_are_distinguishable_from_first_migration(self):
        self.assertFalse(self.client.get('/api/studio/preferences').json()['_persisted'])
        self.assertEqual(self.client.patch('/api/studio/preferences',json={'tts_text':'','favorite_voices':[]}).status_code,200)
        stored=self.client.get('/api/studio/preferences').json()
        self.assertTrue(stored['_persisted'])
        self.assertEqual(stored['tts_text'],'')
        self.assertEqual(stored['favorite_voices'],[])
        self.assertEqual(self.client.patch('/api/studio/preferences',json={'_persisted':False}).status_code,400)

    def test_queue_pause_persists_and_partial_reorder_retains_other_jobs(self):
        jobs = [self.speech('queued')[0] for _ in range(3)]
        for index, job in enumerate(jobs):
            job['created'] = index + 1
            core.put('job', job['id'], job)
        self.assertEqual(self.client.post('/api/studio/queue', json={'paused':True}).status_code, 200)
        self.assertTrue(core.get('queue_control', 'main')['paused'])
        response = self.client.post('/api/studio/queue/reorder', json={'ids':[jobs[2]['id']]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['ids'], [jobs[2]['id'], jobs[0]['id'], jobs[1]['id']])
        self.assertLess(core.get('job', jobs[2]['id'])['queue_order'], core.get('job',jobs[0]['id'])['queue_order'])
        state = self.client.get('/api/studio/state').json()
        self.assertTrue(state['queue_health']['paused'])
        self.assertEqual(state['queue_health']['queued_count'], 3)
        self.assertEqual(self.client.post('/api/studio/queue/reorder', json={'ids':[['invalid']]}).status_code, 400)

    def test_queue_outer_loop_survives_database_error_with_backoff(self):
        event = threading.Event()
        calls = []
        def cycle():
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError('database locked')
            event.set()
        health = {'healthy':True, 'last_error':'', 'last_success':None, 'consecutive_errors':0}
        with patch.object(server, 'stop_event', event), patch.object(server, 'queue_runtime', health), patch.object(server, '_worker_loop_run', side_effect=cycle), patch.object(event, 'wait', return_value=False) as wait:
            server.worker_loop()
        self.assertEqual(len(calls), 2)
        wait.assert_called_once_with(.5)
        self.assertEqual(health['consecutive_errors'], 1)

    def test_ambiguous_running_job_never_automatically_replayed(self):
        job, _ = self.speech('running')
        with patch.object(server, '_stop_job_processes', return_value=[123]):
            with self.assertRaises(RuntimeError):
                server._recover_abandoned_jobs()
        self.assertEqual(core.get('job', job['id'])['state'], 'running')
        with patch.object(server, '_stop_job_processes', return_value=[]):
            server._recover_abandoned_jobs()
        saved = core.get('job', job['id'])
        self.assertEqual(saved['state'], 'error')
        self.assertTrue(saved['requires_recovery'])

    def test_speech_library_rename_filter_pagination_and_metadata_only(self):
        a, _ = self.speech()
        b, _ = self.speech()
        r = self.client.post(f'/api/studio/speech/{a["id"]}/rename', json={'name':'Giới thiệu sản phẩm'})
        self.assertEqual(r.status_code, 200, r.text)
        page = self.client.get('/api/studio/speech?query=sản%20phẩm&limit=1').json()
        self.assertEqual(page['total'],1)
        self.assertEqual(page['items'][0]['id'],a['id'])
        self.assertGreater(page['items'][0]['bytes'],0)
        self.assertNotIn('sample_text', page['items'][0])
        self.assertEqual(self.client.get('/api/studio/speech?limit=101').status_code,400)

    def test_speech_delete_trash_restore_preserves_audio(self):
        job, folder = self.speech()
        plan = self.client.post(f'/api/studio/storage/speech/{job["id"]}/plan').json()
        self.assertEqual(plan['action'], 'speech')
        response = self.client.post('/api/studio/storage/execute', json={'action':'speech','project_id':job['id'],'signature':plan['signature'],'confirmed':True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(folder.exists())
        self.assertEqual(self.client.get('/api/studio/speech').json()['total'],0)
        restored = self.client.post(f'/api/studio/storage/trash/{response.json()["trash_id"]}/restore', json={'confirmed':True})
        self.assertEqual(restored.status_code,200,restored.text)
        self.assertEqual((folder/'speech.wav').read_bytes(),b'WAV fixture for metadata')
        self.assertEqual(self.client.get('/api/studio/speech').json()['total'],1)

    def test_speech_used_as_project_audio_cannot_be_deleted(self):
        job, folder = self.speech()
        p = self.project()
        p['params']['voice_path'] = str(folder/'speech.wav')
        core.put('project',p['id'],p)
        plan = storage_manager.preview('speech',job['id'])
        self.assertEqual(plan['targets'],[])
        with self.assertRaises(ValueError):
            storage_manager.execute('speech',job['id'],{'signature':plan['signature'],'confirmed':True})
        self.assertTrue(folder.exists())

    def test_bundle_roundtrip_has_source_checksum_and_creates_new_project(self):
        p = self.project()
        bundle = project_bundle.export_bundle(p)
        with zipfile.ZipFile(bundle) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual({r['role'] for r in manifest['files']},{'project','source'})
            self.assertNotIn('key', archive.read('project.json').decode())
        restored = project_bundle.import_bundle(bundle)
        self.assertNotEqual(restored['id'],p['id'])
        self.assertEqual(restored['segments'],p['segments'])
        self.assertTrue(Path(restored['media']['path']).is_file())
        self.assertEqual(project_bundle._hash(Path(restored['media']['path'])),manifest['source_reference'])
        self.assertEqual(list((self.data/'imports').glob('.staging-*')),[])

    def rewritten_bundle(self, bundle, change):
        output = self.data/(uuid.uuid4().hex + '.zip')
        with zipfile.ZipFile(bundle) as old, zipfile.ZipFile(output,'w') as new:
            entries = {i.filename:old.read(i) for i in old.infolist()}
            change(entries)
            for name, content in entries.items():
                new.writestr(name,content)
        return output

    def test_bundle_traversal_and_checksum_mismatch_leave_no_project_or_payload(self):
        p = self.project()
        bundle = project_bundle.export_bundle(p)
        def bad_manifest(entries):
            manifest = json.loads(entries['manifest.json'])
            manifest['files'][0]['sha256'] = '0'*64
            entries['manifest.json'] = json.dumps(manifest).encode()
        for change in (lambda entries: entries.update({'../outside.txt':b'escape'}),bad_manifest,lambda entries: entries.update({'PROJECT.JSON':entries['project.json']})):
            bad = self.rewritten_bundle(bundle,change)
            with self.assertRaises(ValueError):
                project_bundle.import_bundle(bad)
        self.assertEqual(len(core.all_items('project')),1)
        self.assertFalse((self.data/'outside.txt').exists())
        self.assertEqual(list((self.data/'imports').iterdir()),[])

    def test_bundle_without_source_requires_exact_external_original(self):
        p = self.project()
        bundle = project_bundle.export_bundle(p,include_source=False)
        fake = self.data/'other.mp4'
        fake.write_bytes(Path(p['media']['path']).read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError,'checksum'):
            project_bundle.import_bundle(bundle,str(fake))
        restored = project_bundle.import_bundle(bundle,p['media']['path'])
        self.assertEqual(restored['media']['path'],p['media']['path'])

    def test_bundle_db_failure_rolls_back_owned_payload_without_touching_source(self):
        p = self.project()
        bundle = project_bundle.export_bundle(p)
        with patch.object(project_bundle,'save_project_version',side_effect=sqlite3.OperationalError('locked')):
            with self.assertRaises(sqlite3.OperationalError):
                project_bundle.import_bundle(bundle)
        self.assertEqual(list((self.data/'imports').iterdir()),[])
        self.assertTrue(Path(p['media']['path']).exists())

    def test_bundle_download_and_upload_endpoints_cleanup_uploads(self):
        p = self.project()
        response = self.client.get(f'/api/studio/projects/{p["id"]}/bundle')
        self.assertEqual(response.status_code,200,response.text[:200] if response.status_code != 200 else '')
        self.assertEqual(list((self.data/'bundles').glob('*.npk.zip')),[])
        restored = self.client.post('/api/studio/restore-bundle',files={'file':('backup.npk.zip',response.content,'application/zip')})
        self.assertEqual(restored.status_code,200,restored.text)
        self.assertEqual(list((self.data/'bundles/uploads').iterdir()),[])

    def test_republish_updates_job_and_project_destination(self):
        p = self.project()
        job, folder = self.speech()
        job.update(kind='render',project_id=p['id'],result={'files':['final.mp4'],'primary_file':'final.mp4','delivery_pending':True})
        (folder/'final.mp4').write_bytes(b'Already verified fixture for copying')
        core.put('job',job['id'],job)
        p['artifacts']['render']={'job_id':job['id'],'delivery_pending':True}
        core.put('project',p['id'],p)
        destination = self.data/'exports'
        destination.mkdir()
        response = self.client.post(f'/api/studio/jobs/{job["id"]}/publish',json={'directory':str(destination),'name':'New destination'})
        self.assertEqual(response.status_code,200,response.text)
        result = core.get('job',job['id'])['result']
        self.assertEqual(result['published'],response.json())
        self.assertNotIn('delivery_pending',result)
        self.assertEqual(core.get('project',p['id'])['artifacts']['render']['published'],response.json())

    def test_multi_voice_script_snapshot_and_cache_identity_include_each_segment(self):
        script = [{'text':'Xin chào', 'voice_id':'piper:vais1000','rate':1,'pause_after_ms':500},
                  {'text':'Cảm ơn bạn', 'voice_id':'piper:vais1000','rate':1.2,'pause_after_ms':0}]
        body = {'kind':'text_to_speech', 'sample_text':'', 'narration_segments':script,
                'params':{'voice_id':'piper:vais1000'}}
        response = self.client.post('/api/jobs',json=body)
        self.assertEqual(response.status_code,200,response.text)
        job = response.json()
        snapshot = json.loads((self.data/'jobs'/job['id']/'input.json').read_text(encoding='utf-8'))
        self.assertEqual(snapshot['project']['speech_segments'],script)
        changed = copy.deepcopy(body)
        changed['narration_segments'][0]['pause_after_ms']=600
        second = self.client.post('/api/jobs',json=changed)
        self.assertEqual(second.status_code,200,second.text)
        self.assertNotEqual(job['sample_signature'],second.json()['sample_signature'])

    def test_enrollment_queue_preserves_reference_snapshot_without_video(self):
        reference = self.data/'reference.wav'
        reference.write_bytes(b'Reference path validation fixture; decode happens in worker')
        body = {'kind':'enroll_voice','name':'Giọng riêng thử', 'reference_path':str(reference),
                'reference_start':1, 'reference_duration':5}
        with patch('voice_enrollment.enrollment_status',return_value={'available':True}), patch('voices.provider_status',return_value={'vieneu':{'available':True}}):
            response = self.client.post('/api/jobs',json=body)
            self.assertEqual(response.status_code,200,response.text)
            job = response.json()
            snapshot = json.loads((self.data/'jobs'/job['id']/'input.json').read_text(encoding='utf-8'))
            self.assertEqual(snapshot['project']['reference_path'],str(reference))
            self.assertEqual(snapshot['project']['params']['reference_duration'],5)
            self.assertTrue(job['standalone'])
            self.assertEqual(job['project_id'],'voice-enrollment')
            self.assertEqual(self.client.post('/api/jobs',json=body|{'reference_path':'https://example.com/a.wav'}).status_code,400)
            self.assertEqual(self.client.post('/api/jobs',json=body|{'reference_duration':9}).status_code,400)

    def test_translation_resume_exact_signature_and_old_checkpoint_restore(self):
        from translation_checkpoint import write_checkpoint
        p = self.project()
        p['segments'].append({'start':2.,'end':3.,'text':'Thanks','translated':''})
        p['segments'][0]['translated']=''
        core.put('project',p['id'],p)
        previous, folder = self.speech('error')
        previous.update(kind='translate',project_id=p['id'])
        core.put('job',previous['id'],previous)
        (folder/'input.json').write_text(json.dumps({'project':p, 'job':previous},ensure_ascii=False),encoding='utf-8')
        rows = copy.deepcopy(p['segments'])
        rows[0]['translated']='Xin chào'
        write_checkpoint(folder/'translation-checkpoint.json',p,rows,[0])
        from provider_usage import TranslationUsage
        usage = TranslationUsage(folder, len(rows), 20, p['params']['provider'], p['params']['translation_model'])
        usage.set_batch([0], 'fixture translation')
        usage.observe({'event':'before_send', 'attempt':1})
        usage.observe({'event':'response', 'http_status':200})
        usage.checkpoint(1)
        body = {'kind':'translate','project_id':p['id'],'replace_segments':True,'resume_from':previous['id']}
        with patch('preflight.inspect',return_value={'ok':True}), patch.object(server,'active_key_id',return_value='test-only'), patch.object(server,'active_secret',return_value='fake-test-only-not-a-credential'):
            response = self.client.post('/api/jobs',json=body)
            self.assertEqual(response.status_code,200,response.text)
            self.assertTrue((self.data/'jobs'/response.json()['id']/'translation-checkpoint.json').is_file())
            # Isolate signature validation from the separate busy-project gate.
            queued = core.get('job', response.json()['id'])
            queued.update(state='cancelled', ended=queued['created'])
            core.put('job', queued['id'], queued)
            p['params']['target_language']='English'
            core.put('project',p['id'],p)
            rejected = self.client.post('/api/jobs',json=body)
            self.assertEqual(rejected.status_code,400,rejected.text)
        # Old list schema remains manually recoverable without overwriting an edit.
        (folder/'translation-checkpoint.json').write_text(json.dumps(rows,ensure_ascii=False),encoding='utf-8')
        rejected = self.client.post(f'/api/studio/jobs/{previous["id"]}/apply',json={'expected_revision':p['updated']})
        self.assertEqual(rejected.status_code,400,rejected.text)
        self.assertEqual(core.get('project',p['id'])['segments'][0]['translated'],'')
        p['params']['target_language']=json.loads((folder/'input.json').read_text(encoding='utf-8'))['project']['params']['target_language']
        core.put('project',p['id'],p)
        applied = self.client.post(f'/api/studio/jobs/{previous["id"]}/apply',json={'expected_revision':p['updated']})
        self.assertEqual(applied.status_code,200,applied.text)
        self.assertEqual(applied.json()['segments'][0]['translated'],'Xin chào')

    def test_audio_insights_only_exposes_current_owned_verified_signature(self):
        from audio_insights import insights_signature
        p = self.project()
        job, folder = self.speech()
        current = insights_signature(p)
        document = {'schema_version':1, 'signature':current, 'waveform':[], 'cues':[]}
        target = folder/'audio-insights.json'
        target.write_text(json.dumps(document),encoding='utf-8')
        p['artifacts']['analyze_audio']={'job_id':job['id'], 'signature':current}
        core.put('project',p['id'],p)
        endpoint=f'/api/studio/projects/{p["id"]}/audio-insights'
        self.assertTrue(self.client.get(endpoint).json()['ready'])
        document['signature']='0'*64
        target.write_text(json.dumps(document),encoding='utf-8')
        self.assertFalse(self.client.get(endpoint).json()['ready'])
        document['signature']=current
        target.write_text(json.dumps(document),encoding='utf-8')
        p['segments'][0]['start']=.1
        core.put('project',p['id'],p)
        self.assertFalse(self.client.get(endpoint).json()['ready'])

    def test_preview_cancellation_only_terminates_matching_request(self):
        from types import SimpleNamespace
        import live_clean_preview
        request_id=str(uuid.uuid4())
        running=SimpleNamespace(preview_request_id=request_id,preview_cancelled=False)
        with patch.object(live_clean_preview,'live_process',running), patch.object(live_clean_preview,'kill_preview') as stop:
            response=self.client.post('/api/studio/live-clean/cancel',json={'request_id':str(uuid.uuid4())})
            self.assertEqual(response.status_code,200,response.text)
            stop.assert_not_called()
            self.assertFalse(running.preview_cancelled)
            response=self.client.post('/api/studio/live-clean/cancel',json={'request_id':request_id})
            self.assertEqual(response.status_code,200,response.text)
            stop.assert_called_once_with(running)
            self.assertTrue(running.preview_cancelled)
            self.assertEqual(self.client.post('/api/studio/live-clean/cancel',json={'request_id':'all'}).status_code,400)

    def test_speaker_group_review_rejects_changed_cue_or_tampered_file(self):
        from speaker_groups import speaker_groups_signature
        p=self.project()
        job,folder=self.speech()
        fingerprint=speaker_groups_signature(p,[0])
        document={'schema':1,'source_signature':fingerprint,'segment_indices':[0], 'groups':[], 'suggestions':[]}
        job.update(kind='speaker_groups',project_id=p['id'],result=document)
        core.put('job',job['id'],job)
        target=folder/'speaker-groups.json'
        target.write_text(json.dumps(document),encoding='utf-8')
        endpoint=f'/api/studio/jobs/{job["id"]}/speaker-groups'
        self.assertTrue(self.client.get(endpoint).json()['ready'])
        target.write_text('{}',encoding='utf-8')
        self.assertFalse(self.client.get(endpoint).json()['ready'])
        target.write_text(json.dumps(document),encoding='utf-8')
        p['segments'][0]['end']=1.2
        core.put('project',p['id'],p)
        self.assertFalse(self.client.get(endpoint).json()['ready'])

    def separation_fixture(self):
        import numpy as np
        from audio_separation import separation_signature, source_signature, MODEL_SHA256, FloatWaveWriter
        p=self.project()
        original=self.data/'source.mp4'
        original.write_bytes(Path(p['media']['path']).read_bytes())
        p['media']['path']=str(original)
        p['source_revision']=uuid.uuid4().hex
        core.put('project',p['id'],p)
        job,folder=self.speech()
        frames=round(p['media']['duration']*44100)
        report={'schema_version':1,'signature':separation_signature(p),'source_signature':source_signature(p),
                'model_sha256':MODEL_SHA256,'frames':frames,'sample_rate':44100,'duration':frames/44100}
        for name in ('instrumentals.wav','vocals.wav'):
            writer=FloatWaveWriter(folder/name)
            writer.write(np.full((2,frames),.001,dtype='float32'))
            writer.close()
        (folder/'audio-separation.json').write_text(json.dumps(report),encoding='utf-8')
        job.update(kind='separate_audio',project_id=p['id'],source_revision=p['source_revision'],
                   result={'files':['instrumentals.wav','vocals.wav','audio-separation.json'],'verification':{'files_checked':3},
                           'signature':report['signature'],'source_signature':report['source_signature'],'separation':report})
        core.put('job',job['id'],job)
        return p,job,folder

    def test_separation_apply_is_explicit_atomic_and_protects_selected_stem(self):
        p,job,folder=self.separation_fixture()
        endpoint=f'/api/studio/jobs/{job["id"]}/audio-separation'
        review=self.client.get(endpoint)
        self.assertTrue(review.json()['ready'],review.text)
        self.assertEqual(core.get('project',p['id'])['params']['bgm_path'],'')
        applied=self.client.post(endpoint+'/apply',json={'expected_revision':p['updated']})
        self.assertEqual(applied.status_code,200,applied.text)
        params=applied.json()['params']
        self.assertEqual(params['bgm_path'],str(folder/'instrumentals.wav'))
        self.assertEqual((params['original_gain'],params['bgm_gain']),(0,1))
        self.assertEqual(params['voice_gain'],p['params']['voice_gain'])
        self.assertIn((folder/'instrumentals.wav').resolve(),storage_manager.input_paths())
        plan=storage_manager.preview('cleanup',p['id'])
        self.assertNotIn('jobs/'+job['id'],[target['path'] for target in plan['targets']])
        self.assertIn('jobs/'+job['id'],[item['path'] for item in plan['retained']])
        self.assertEqual(self.client.post(endpoint+'/apply',json={'expected_revision':p['updated']}).status_code,409)

    def test_separation_review_rejects_source_change_trashed_job_or_incomplete_stem(self):
        p,job,folder=self.separation_fixture()
        endpoint=f'/api/studio/jobs/{job["id"]}/audio-separation'
        self.assertTrue(self.client.get(endpoint).json()['ready'])
        job['storage_removed']=uuid.uuid4().hex
        core.put('job',job['id'],job)
        self.assertFalse(self.client.get(endpoint).json()['ready'])
        self.assertEqual(self.client.post(endpoint+'/apply',json={'expected_revision':p['updated']}).status_code,409)
        job.pop('storage_removed')
        core.put('job',job['id'],job)
        path=folder/'instrumentals.wav'
        original=path.read_bytes()
        path.write_bytes(original[:-8])
        self.assertFalse(self.client.get(endpoint).json()['ready'])
        path.write_bytes(original)
        source=Path(p['media']['path'])
        with source.open('ab') as output:output.write(b'changed source fixture')
        self.assertFalse(self.client.get(endpoint).json()['ready'])
        self.assertEqual(self.client.post(endpoint+'/apply',json={'expected_revision':p['updated']}).status_code,409)
        self.assertEqual(core.get('project',p['id'])['params']['bgm_path'],'')
