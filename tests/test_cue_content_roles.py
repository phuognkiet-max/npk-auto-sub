"""Explicit display-only cues survive editor/portable contracts without text loss.

Use tools/run_tests.py --profile media for actual Node editor commands. Every
API write uses its private test store; no recognition, voice or provider runs.
"""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

import pysubs2
from fastapi.testclient import TestClient
from core import DATA, DEFAULTS, get, put, save_srt
from project_transfer import export_document, restore_document
import server

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node') or 'C:/Users/developer/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'


def cue(**changes):
    return dict(start=1., end=5., text='Thông tin trên hình.', translated='Display text.') | changes


class CueContentRoleTests(unittest.TestCase):
    def setUp(self):
        self.project = dict(id='content-role-private', name='Role fixture', created=1., updated=1.,
            source_revision='fixture-source', media=dict(path=str(DATA/'fixture.mp4'), duration=12.,
            width=100, height=100, fps=25), params=copy.deepcopy(DEFAULTS), segments=[cue()],
            regions=[], extraction_regions=[], artifacts={})
        put('project', self.project['id'], self.project)
        self.client = TestClient(server.app, headers={'x-studio-token': server.TOKEN})

    def save(self, rows):
        return self.client.put('/api/projects/' + self.project['id'], json={
            'expected_revision': get('project', self.project['id'])['updated'], 'segments': rows})

    def test_legacy_role_absence_and_explicit_boolean_are_preserved(self):
        for row in (cue(), cue(speech_enabled=True), cue(speech_enabled=False)):
            self.assertEqual(server.validate_segments([row], 12), [row])

    def test_false_role_can_be_saved_and_reenabled_without_changing_content(self):
        row = cue(speech_enabled=False, spoken_text='Saved separate spoken copy.', character_name='Nhân vật')
        response = self.save([row])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(get('project', self.project['id'])['segments'], [row])
        row['speech_enabled'] = True
        response = self.save([row])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(get('project', self.project['id'])['segments'], [row])

    def test_non_boolean_roles_are_rejected_atomically(self):
        for value in (0, 1, None, 'false', [], {}):
            with self.subTest(value=value):
                response = self.save([cue(speech_enabled=value)])
                self.assertEqual(response.status_code, 400)
                self.assertIn('bật hoặc tắt', response.json()['detail'])
                self.assertEqual(get('project', self.project['id']), self.project)

    def test_portable_roundtrip_keeps_all_roles_and_spoken_overrides(self):
        self.project['segments'] = [cue(speech_enabled=False, spoken_text='Stored, unused copy.'),
                                    cue(start=5., end=8., speech_enabled=True), cue(start=8., end=10.)]
        document = export_document(self.project)
        with patch('core.probe', return_value=copy.deepcopy(self.project['media'])), \
                patch('core.settings', return_value=copy.deepcopy(DEFAULTS)):
            restored = restore_document(document, self.project['media']['path'])
        self.assertEqual(restored['segments'], self.project['segments'])
        self.assertNotIn('speech_enabled', restored['segments'][2])
        self.assertEqual(document['segments'], self.project['segments'])

    def test_display_only_role_does_not_remove_exported_subtitles(self):
        output = DATA / 'display-only-fixture.srt'
        rows = [cue(speech_enabled=False), cue(start=5., end=8., speech_enabled=True)]
        save_srt(rows, output)
        subtitles = pysubs2.load(str(output), encoding='utf-8')
        self.assertEqual([event.text for event in subtitles], ['Display text.', 'Display text.'])

    @unittest.skipUnless(Path(NODE).is_file(), 'Node unavailable')
    def test_actual_editor_commands_preserve_roles_and_block_mixed_merge(self):
        script = r'''
const fs=require('fs'), vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
vm.runInThisContext(source.slice(source.indexOf('function editorInvalidateCaption('), source.indexOf('function renderCues(')));
const first={start:1,end:5,text:'Xin chào bạn',translated:'Hello dear friend',speech_enabled:false};
const second={start:5,end:8,text:'Tiêu đề',translated:'Title',speech_enabled:false};
const original=JSON.stringify([first,second]);
const split=editorSplitCue(first,3), shifted=editorShiftCue(first,1), bounded=editorBoundCue(first,1,3);
const merged=editorMergeCues([first,second]);
let mixedError='', defaultError='';
try { editorMergeCues([first,{...second,speech_enabled:true}]); } catch(e) { mixedError=e.message; }
const legacy={start:8,end:10,text:'Đọc',translated:'Read'};
try { editorMergeCues([first,legacy]); } catch(e) { defaultError=e.message; }
const spoken=editorMergeCues([legacy,{...legacy,start:10,end:12,speech_enabled:true}]);
const legacySplit=editorSplitCue(legacy,9);
console.log(JSON.stringify({split,shifted,bounded,merged,mixedError,defaultError,spoken,legacySplit,
  unchanged:original===JSON.stringify([first,second])}));
'''
        result = subprocess.run([NODE, '-e', script, str(ROOT/'static/studio.js')],
            capture_output=True, text=True, timeout=15, check=True)
        commands = json.loads(result.stdout)
        self.assertTrue(commands['unchanged'])
        for row in commands['split'] + [commands['shifted'], commands['bounded'], commands['merged']]:
            self.assertIs(row['speech_enabled'], False)
            server.validate_segments([row], 12)
        self.assertEqual(commands['merged']['text'], 'Xin chào bạn Tiêu đề')
        self.assertEqual(commands['merged']['translated'], 'Hello dear friend Title')
        self.assertIn('vai khác nhau', commands['mixedError'])
        self.assertIn('vai khác nhau', commands['defaultError'])
        self.assertIs(commands['spoken']['speech_enabled'], True)
        for row in commands['legacySplit']:
            self.assertNotIn('speech_enabled', row)

    @unittest.skipUnless(Path(NODE).is_file(), 'Node unavailable')
    def test_rendered_delivery_controls_expose_role_and_effective_text(self):
        script = r'''
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
global.P={params:{tts_text_source:'translated',voice_speed:1,voice_id:'fixture'}};
global.catalog={voices:[{id:'fixture',name:'Fixture voice'}]};
global.esc=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
vm.runInThisContext(source.slice(source.indexOf('function selectedSpeechText('),source.indexOf('function updateSpeechSourceHints(')));
vm.runInThisContext(source.slice(source.indexOf('function cueSpeechControls('),source.indexOf('function renderCueRows(')));
const row={start:1,end:5,text:'Original',translated:'Display',spoken_text:'Separate'};
const legacy=cueSpeechControls(row,0), off=cueSpeechControls({...row,speech_enabled:false},1);
console.log(JSON.stringify({legacy,off,legacySpeech:selectedSpeechText(row),offSpeech:selectedSpeechText({...row,speech_enabled:false})}));
'''
        result = subprocess.run([NODE, '-e', script, str(ROOT/'static/studio.js')],
            capture_output=True, text=True, timeout=15, check=True)
        controls = json.loads(result.stdout)
        self.assertIn('Lồng tiếng câu này', controls['legacy'])
        self.assertRegex(controls['legacy'], r'id="cueSpeechEnabled0"[^>]*checked')
        self.assertNotRegex(controls['off'], r'id="cueSpeechEnabled1"[^>]*checked')
        self.assertIn('Chỉ hiển thị phụ đề', controls['off'])
        self.assertEqual(controls['legacySpeech'], 'Display')
        self.assertEqual(controls['offSpeech'], '')


if __name__ == '__main__':
    unittest.main()
