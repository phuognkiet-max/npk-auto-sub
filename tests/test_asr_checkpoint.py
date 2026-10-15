"""Durable ASR prefix contracts using a fixture Whisper generator and PCM WAV."""
import copy
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock,patch
import wave

from core import DEFAULTS
from asr_checkpoint import signature,validate_resume,write_checkpoint
import worker


def segment(start,end,text,words=None):
    return types.SimpleNamespace(start=start,end=end,text=text,words=words or [])


class ASRCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='npk-asr-prefix-')
        self.addCleanup(self.temporary.cleanup)
        self.folder=Path(self.temporary.name)
        self.source=self.folder/'source.mp4'
        self.source.write_bytes(b'fixture source identity; decoder is mocked')
        self.project={'params':DEFAULTS|{'asr_device':'cpu','extraction_start':1.,'extraction_end':7.},
            'source_revision':'fixture-source','media':{'path':str(self.source),'duration':8.,'audio':True,
                'audio_duration':6.,'audio_start_time':1.,'width':1280,'height':720}}
        self.checkpoint=self.folder/'asr-checkpoint.json'
        self.rows=[{'start':1.,'end':2.,'text':'Xin chào','translated':'',
                    'words':[{'start':1.1,'end':1.4,'word':'Xin','probability':.9},
                             {'start':1.4,'end':1.9,'word':'chào','probability':.8}]}]

    def decode(self,command,**_kwargs):
        seconds=float(command[command.index('-t')+1])
        with wave.open(str(command[-1]),'wb') as audio:
            audio.setparams((1,2,16000,0,'NONE','not compressed'))
            audio.writeframes(b'\0\0'*round(seconds*16000))

    def run_with(self,parts,info=None,job=None):
        model=Mock()
        model.transcribe.return_value=(parts,info or types.SimpleNamespace(language='vi',duration=6.))
        fake=types.ModuleType('faster_whisper')
        fake.WhisperModel=Mock(return_value=model)
        with patch.dict('sys.modules',{'faster_whisper':fake}),patch('worker.emit'), \
                patch('worker.run',side_effect=self.decode) as decoder:
            result=worker.asr(self.project,job or {},self.folder)
        return result,model,decoder,fake

    def test_signature_tracks_recognition_scope_not_later_style_and_translation(self):
        original=signature(self.project)
        p=copy.deepcopy(self.project)
        p['params'].update(font_size=26,voice_id='different',target_language='English')
        self.assertEqual(signature(p),original)
        for field,value in [('extraction_start',1.5),('source_language','zh'),('asr_model','medium'),('asr_compute','int8')]:
            p=copy.deepcopy(self.project)
            p['params'][field]=value
            self.assertNotEqual(signature(p),original)

    def test_atomic_checkpoint_failure_preserves_previous_prefix(self):
        write_checkpoint(self.checkpoint,self.project,self.rows,1,7,2,language='vi')
        previous=self.checkpoint.read_bytes()
        with patch('asr_checkpoint.os.fsync',side_effect=OSError('fixture disk failure')):
            with self.assertRaisesRegex(OSError,'disk failure'):
                write_checkpoint(self.checkpoint,self.project,self.rows,1,7,3,language='vi')
        self.assertEqual(self.checkpoint.read_bytes(),previous)
        self.assertFalse(self.checkpoint.with_suffix('.json.tmp').exists())

    def test_generator_failure_keeps_confirmed_segment_with_absolute_words(self):
        def parts():
            yield segment(0,1,'Xin chào',[types.SimpleNamespace(start=.1,end=.4,word='Xin',probability=.9)])
            raise RuntimeError('fixture interrupted inference')
        model=Mock()
        model.transcribe.return_value=(parts(),types.SimpleNamespace(language='vi',duration=6.))
        fake=types.ModuleType('faster_whisper');fake.WhisperModel=Mock(return_value=model)
        with patch.dict('sys.modules',{'faster_whisper':fake}),patch('worker.emit'),patch('worker.run',side_effect=self.decode):
            with self.assertRaisesRegex(RuntimeError,'interrupted inference'):
                worker.asr(self.project,{},self.folder)
        saved=validate_resume(self.checkpoint,self.project)
        self.assertEqual(saved['confirmed_until'],2.)
        self.assertFalse(saved['recognition_complete'])
        self.assertEqual(saved['rows'][0]['words'][0]['start'],1.1)
        self.assertFalse((self.folder/'source.srt').exists())

    def test_resume_decodes_only_unconfirmed_span_and_offsets_words(self):
        write_checkpoint(self.checkpoint,self.project,self.rows,1,7,2,language='vi')
        result,model,decoder,_=self.run_with(iter([segment(.3,1,'Hẹn gặp lại',[
            types.SimpleNamespace(start=.4,end=.9,word='Hẹn',probability=.95)])]),
            types.SimpleNamespace(language='vi',duration=5.),{'resume_from':'fixture'})
        command=decoder.call_args.args[0]
        self.assertEqual(command[command.index('-ss')+1],'2.0')
        self.assertEqual(command[command.index('-t')+1],'5.0')
        self.assertEqual(result['segments'][0],self.rows[0])
        self.assertEqual(result['segments'][1]['start'],2.3)
        self.assertEqual(result['segments'][1]['words'][0]['start'],2.4)
        self.assertEqual(model.transcribe.call_args.kwargs['language'],'vi')
        self.assertTrue(validate_resume(self.checkpoint,self.project)['recognition_complete'])
        self.assertEqual(result['coverage']['analyzed_start'],1.)
        self.assertEqual(result['coverage']['analyzed_end'],7.)

    def test_completed_checkpoint_republishes_without_model_or_decoder(self):
        write_checkpoint(self.checkpoint,self.project,self.rows,1,7,7,language='vi',complete=True)
        with patch('worker.run') as decoder,patch('worker.emit'),patch.dict('sys.modules',{'faster_whisper':None}):
            result=worker.asr(self.project,{'resume_from':'fixture'},self.folder)
        decoder.assert_not_called()
        self.assertEqual(result['segments'],self.rows)
        self.assertTrue((self.folder/'source.srt').is_file())

    def test_unconfirmed_generator_eof_never_becomes_completed_on_retry(self):
        write_checkpoint(self.checkpoint,self.project,self.rows,1,7,7,language='vi',complete=False)
        previous=self.checkpoint.read_bytes()
        with patch('worker.run') as decoder,patch('worker.emit'):
            with self.assertRaisesRegex(ValueError,'chưa xác nhận Whisper'):
                worker.asr(self.project,{'resume_from':'fixture'},self.folder)
        decoder.assert_not_called()
        self.assertEqual(self.checkpoint.read_bytes(),previous)

    def test_stale_or_missing_source_checkpoint_is_rejected(self):
        write_checkpoint(self.checkpoint,self.project,self.rows,1,7,2,language='vi')
        p=copy.deepcopy(self.project)
        p['params']['source_language']='zh'
        with self.assertRaisesRegex(ValueError,'không khớp'):
            validate_resume(self.checkpoint,p)
        self.source.write_bytes(b'changed source length')
        with self.assertRaisesRegex(ValueError,'không khớp'):
            validate_resume(self.checkpoint,self.project)
        self.source.unlink()
        with self.assertRaisesRegex(ValueError,'video nguồn'):
            validate_resume(self.checkpoint,self.project)

    def test_invalid_timing_words_and_json_are_rejected(self):
        valid=write_checkpoint(self.checkpoint,self.project,self.rows,1,7,2,language='vi')
        mutations=[lambda d:d.update(confirmed_until=True),lambda d:d.update(recognition_complete=1),
            lambda d:d.update(confirmed_until=8),lambda d:d['rows'][0]['words'][0].update(start=0),
            lambda d:d['rows'][0]['words'][0].update(end=3),
            lambda d:d['rows'][0]['words'][1].update(start=1.2),
            lambda d:d['rows'][0].update(text=''),lambda d:d.update(metadata=float('nan'))]
        for mutate in mutations:
            saved=copy.deepcopy(valid);mutate(saved)
            self.checkpoint.write_text(json.dumps(saved),encoding='utf-8')
            with self.subTest(saved=str(saved)[:120]),self.assertRaises(ValueError):
                validate_resume(self.checkpoint,self.project)
        for raw in ['{}','[]','{"version":1,"version":1}','{']:
            self.checkpoint.write_text(raw,encoding='utf-8')
            with self.assertRaises(ValueError):
                validate_resume(self.checkpoint,self.project)

    def test_empty_first_attempt_still_has_resumable_zero_prefix(self):
        fake=types.ModuleType('faster_whisper')
        fake.WhisperModel=Mock(side_effect=RuntimeError('fixture model startup failed'))
        with patch.dict('sys.modules',{'faster_whisper':fake}),patch('worker.emit'):
            with self.assertRaisesRegex(RuntimeError,'startup failed'):
                worker.asr(self.project,{},self.folder)
        saved=validate_resume(self.checkpoint,self.project)
        self.assertEqual(saved['rows'],[])
        self.assertEqual(saved['confirmed_until'],1.)
        self.assertFalse(saved['recognition_complete'])


if __name__=='__main__':
    unittest.main()
