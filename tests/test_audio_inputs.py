import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from core import ROOT, create_project
from audio_inputs import inspect_audio, local_audio_path
from server import inspect_project_audio, external_audio, validate_params
from fastapi import HTTPException


class AudioInputTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'tests',prefix='audio-input-')
        self.addCleanup(self.temp.cleanup)
        self.file=Path(self.temp.name)/'voice.wav'
        self.file.write_bytes(b'placeholder for mocked probe')

    def probe_result(self,duration='8'):
        return SimpleNamespace(stdout=json.dumps({'format':{'duration':duration},'streams':[
            {'codec_type':'audio','codec_name':'pcm_s16le','sample_rate':'24000','channels':1}]}).encode())

    def test_only_absolute_local_supported_files(self):
        for value in ['voice.wav','https://example.org/voice.wav',r'\\server\share\voice.wav',r'//server/share/voice.wav']:
            with self.assertRaises(ValueError): local_audio_path(value)
        with self.assertRaises(FileNotFoundError): local_audio_path(self.file.with_name('missing.wav'))
        folder=self.file.parent/'folder.wav';folder.mkdir()
        with self.assertRaises(ValueError): local_audio_path(folder)
        self.assertEqual(local_audio_path(self.file),self.file.resolve())

    def test_measurement_reports_matching_duration(self):
        with patch('audio_inputs.run',return_value=self.probe_result()):
            info=inspect_audio(self.file,8)
        self.assertEqual(info['duration'],8)
        self.assertEqual(info['delta'],0)
        self.assertEqual(info['warnings'],[])

    def test_mismatch_is_a_warning_not_silent_stretch(self):
        for duration in ('4','10'):
            with patch('audio_inputs.run',return_value=self.probe_result(duration)):
                info=inspect_audio(self.file,8)
            self.assertTrue(info['warnings'])
            self.assertEqual(info['duration'],float(duration))

    def test_invalid_probe_or_nonfinite_duration_rejected(self):
        for duration in ('NaN','Infinity','0','-1'):
            with patch('audio_inputs.run',return_value=self.probe_result(duration)):
                with self.assertRaises(ValueError):inspect_audio(self.file,8)
        with patch('audio_inputs.run',side_effect=RuntimeError('bad probe')):
            with self.assertRaises(ValueError):inspect_audio(self.file,8)

    def test_inspection_does_not_change_project(self):
        from core import get
        project=create_project(str(ROOT/'tests/fixtures/demo.mp4'),'Audio test')
        before=get('project',project['id'])
        with patch('audio_inputs.run',return_value=self.probe_result()):
            inspect_project_audio(project['id'],{'path':str(self.file)})
        self.assertEqual(get('project',project['id']),before)

    def test_probe_timeout_is_an_actionable_validation_error(self):
        with patch('audio_inputs.run',side_effect=subprocess.TimeoutExpired('ffprobe',20)):
            with self.assertRaisesRegex(ValueError,'thời gian chờ'):
                inspect_audio(self.file,8)

    def test_media_reads_only_path_selected_in_project(self):
        from core import put
        project=create_project(str(ROOT/'tests/fixtures/demo.mp4'),'Audio test')
        with self.assertRaises(HTTPException):external_audio(project['id'],'not-voice')
        with self.assertRaises(HTTPException):external_audio(project['id'],'voice')
        project['params']['voice_path']=str(self.file)
        put('project',project['id'],project)
        self.assertEqual(Path(external_audio(project['id'],'voice').path),self.file)

    def test_saving_params_rejects_network_path(self):
        with self.assertRaises(ValueError):validate_params({'voice_path':r'\\example\share\voice.wav'})
