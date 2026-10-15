"""Actual editor command / PUT regressions, with private fixtures only."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from core import DEFAULTS, DATA, get, put
from fastapi.testclient import TestClient
import server

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node') or 'C:/Users/developer/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'


class CueEditorContractTests(unittest.TestCase):
    def setUp(self):
        self.row = dict(start=1., end=5., text='Xin, chào!', translated='Hello, friend!',
            words=[dict(word='Xin', start=1., end=1.5), dict(word='chào', start=3., end=3.5)],
            caption_words=[dict(word='Hello,', start=1., end=1.5), dict(word='friend!', start=3., end=3.5)],
            caption_timing_key='a' * 64, word_timing_source='aligned')
        self.project = dict(id='cue-command-private', name='Fixture', created=1., updated=1.,
            source_revision='fixture-source', media=dict(path=str(DATA/'fixture.mp4'), duration=12., width=100, height=100, fps=25),
            params=copy.deepcopy(DEFAULTS), segments=[copy.deepcopy(self.row)], regions=[], extraction_regions=[], artifacts={})
        put('project', self.project['id'], self.project)
        self.client = TestClient(server.app, headers={'x-studio-token': server.TOKEN})

    def save(self, rows):
        return self.client.put('/api/projects/' + self.project['id'], json={
            'expected_revision': self.project['updated'], 'segments': rows})

    def test_valid_bounds_edit_invalidates_generated_timing_before_validation(self):
        row = copy.deepcopy(self.row)
        row['end'] = 2.
        row['words'] = row['words'][:1]
        with patch('speech_word_alignment.caption_timing_key', return_value='b' * 64):
            response = self.save([row])
        self.assertEqual(response.status_code, 200, response.text)
        saved = get('project', self.project['id'])['segments'][0]
        self.assertEqual(saved['caption_words'], [])
        self.assertNotIn('caption_timing_key', saved)
        self.assertEqual(saved['word_timing_source'], 'estimated')

    def test_matching_identity_does_not_hide_invalid_generated_words(self):
        row = copy.deepcopy(self.row)
        row['end'] = 2.
        row['words'] = row['words'][:1]
        with patch('speech_word_alignment.caption_timing_key', return_value='a' * 64):
            response = self.save([row])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(get('project', self.project['id']), self.project)

    def test_source_words_stay_strict_and_invalid_edit_is_atomic(self):
        row = copy.deepcopy(self.row)
        row['end'] = 2.
        with patch('speech_word_alignment.caption_timing_key', return_value='b' * 64):
            response = self.save([row])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(get('project', self.project['id']), self.project)

    def test_overlong_content_rejected_without_mutation_or_truncation(self):
        for field in ('text', 'translated'):
            row = copy.deepcopy(self.row)
            row[field] = 'x' * 10001
            response = self.save([row])
            self.assertEqual(response.status_code, 400)
            self.assertIn('10.000', response.json()['detail'])
            self.assertEqual(get('project', self.project['id']), self.project)
        clean = server.validate_segments([dict(start=0, end=1, text='x'*10000, translated='y'*10000)], 12)[0]
        self.assertEqual(len(clean['text']), 10000)
        self.assertEqual(len(clean['translated']), 10000)

    @unittest.skipUnless(Path(NODE).is_file(), 'Node unavailable')
    def test_actual_javascript_commands_save_split_shift_and_bounds(self):
        script = r'''
const fs=require('fs'), vm=require('vm');
const code=fs.readFileSync(process.argv[1],'utf8');
vm.runInThisContext(code.slice(code.indexOf('function editorInvalidateCaption('), code.indexOf('function renderCues(')));
const row=JSON.parse(process.argv[2]);
const original=JSON.stringify(row);
const split=editorSplitCue(row,2.5), shifted=editorShiftCue(row,2), bounded=editorBoundCue(row,1,2);
const estimated=editorSplitCue({start:0,end:4,text:'你好朋友',translated:'Xin chào, người bạn!',words:[]},2);
const crossing=editorSplitCue({start:0,end:4,text:'Xin chào',translated:'Xin chào',words:[{word:'Xin',start:0,end:2},{word:'chào',start:2,end:4}]},1);
console.log(JSON.stringify({split,shifted,bounded,estimated,crossing,unchanged:original===JSON.stringify(row)}));
'''
        result = subprocess.run([NODE, '-e', script, str(ROOT/'static/studio.js'), json.dumps(self.row)],
            capture_output=True, text=True, timeout=15, check=True)
        commands = json.loads(result.stdout)
        self.assertTrue(commands['unchanged'])
        self.assertEqual([r['text'] for r in commands['split']], ['Xin,', 'chào!'])
        self.assertEqual([r['translated'] for r in commands['split']], ['Hello,', 'friend!'])
        self.assertEqual(commands['shifted']['words'][0]['start'], 3.)
        self.assertEqual(len(commands['bounded']['words']), 1)
        self.assertEqual(' '.join(r['translated'] for r in commands['estimated']), 'Xin chào, người bạn!')
        self.assertEqual(''.join(r['text'] for r in commands['estimated']), '你好朋友')
        # A word straddling the split belongs to exactly one side and is clipped.
        self.assertEqual(sum(len(r['words']) for r in commands['crossing']), 2)
        for label in ('split', 'shifted', 'bounded', 'estimated', 'crossing'):
            rows = commands[label] if isinstance(commands[label], list) else [commands[label]]
            clean = server.validate_segments(rows, 12)
            self.assertEqual(len(clean), len(rows))
        with patch('speech_word_alignment.caption_timing_key', return_value='b'*64):
            response = self.save(commands['split'])
        self.assertEqual(response.status_code, 200, response.text)


if __name__ == '__main__':
    unittest.main()
