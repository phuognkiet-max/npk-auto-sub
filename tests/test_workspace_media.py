import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np
from core import DEFAULTS, binary, run, probe, save_srt
from hardware_admission import inspect_asr
from karaoke import timed_caption


def write_tone(path, frequency=220, amplitude=9000, duration=1, active=None, rate=48000):
    samples=np.arange(round(duration*rate))
    data=np.sin(samples*2*np.pi*frequency/rate)*amplitude
    if active is not None:
        data *= ((samples/rate>=active[0]) & (samples/rate<active[1]))
    with wave.open(str(path),'wb') as audio:
        audio.setparams((1,2,rate,0,'NONE','not compressed'))
        audio.writeframes(data.astype('<i2').tobytes())


class WorkspaceMediaTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)

    def hardware(self,free=3500,ram=8000):
        return {'gpus':[{'name':'GTX1650','total_mb':4096,'free_mb':free}],
                'ram_available_mb':ram}

    def test_hardware_blocks_large_4gb_without_switching_device(self):
        params={'asr_device':'cuda','asr_model':'large-v3','asr_compute':'auto'}
        result=inspect_asr(params,self.hardware())
        self.assertTrue(result['errors'])
        self.assertEqual(params['asr_device'],'cuda')
        self.assertEqual(params['asr_model'],'large-v3')
        self.assertFalse(inspect_asr({'asr_device':'cuda','asr_model':'small','asr_compute':'auto'},self.hardware())['errors'])

    def test_hardware_warns_low_free_vram_and_ram(self):
        result=inspect_asr({'asr_device':'cuda','asr_model':'small'},self.hardware(free=500,ram=1000))
        self.assertFalse(result['errors'])
        self.assertTrue(any('VRAM' in x for x in result['warnings']))
        self.assertTrue(any('RAM' in x for x in result['warnings']))
        self.assertTrue(inspect_asr({'asr_device':'cpu','asr_compute':'int8_float16'},self.hardware())['errors'])
        self.assertFalse(inspect_asr({'asr_device':'cpu','asr_compute':'int8'},self.hardware())['errors'])

    def cue(self):
        return {'start':1,'end':3,'text':'Xin chào','translated':'Xin chào',
                'words':[{'word':' Xin','start':1.2,'end':1.5,'probability':.9},
                         {'word':' chào','start':1.7,'end':2.1,'probability':.95}]}

    def test_karaoke_requires_matching_displayed_words_and_valid_times(self):
        row=self.cue()
        caption=timed_caption(row,'Xin chào!')
        self.assertIn(r'{\kf30}Xin',caption)
        self.assertIn(r'{\kf40}chào!',caption)
        self.assertIn(r'{\k20}',caption)
        self.assertIsNone(timed_caption(row,'Hello there'))
        self.assertIsNone(timed_caption(row,'Xin chào bạn'))
        bad=copy.deepcopy(row)
        bad['words'][1]['start']=1.3
        self.assertIsNone(timed_caption(bad,'Xin chào'))
        bad=copy.deepcopy(row)
        bad['words'][1]['end']=4
        self.assertIsNone(timed_caption(bad,'Xin chào'))

    def test_karaoke_ass_roundtrip_and_translation_fallback(self):
        import pysubs2
        row=self.cue()
        # Legacy karaoke only accepts source timings matching the full text.
        params=DEFAULTS|{'subtitle_karaoke':True, 'subtitle_flow':'wrap'}
        media={'width':320,'height':180}
        target=self.root/'karaoke.ass'
        save_srt([row],target,ass_style=(params,media))
        doc=pysubs2.load(str(target),encoding='utf-8')
        self.assertEqual(doc[0].plaintext,'Xin chào')
        self.assertIn(r'\kf',doc[0].text)
        row['translated']='Hello there'
        save_srt([row],target,ass_style=(params,media))
        doc=pysubs2.load(str(target),encoding='utf-8')
        self.assertEqual(doc[0].plaintext,'Hello there')
        self.assertNotIn(r'\kf',doc[0].text)

    def test_validate_words_preserves_bounds_probability_and_rejects_invalid(self):
        from server import validate_segments
        row=self.cue()
        cleaned=validate_segments([row],8)[0]
        self.assertEqual(cleaned['words'][0]['start'],1.2)
        self.assertEqual(cleaned['words'][0]['probability'],.9)
        for key,value in [('start',.5),('end',3.5),('start',float('nan')),('probability',2)]:
            bad=copy.deepcopy(row)
            bad['words'][0][key]=value
            with self.assertRaises(ValueError):
                validate_segments([bad],8)
        bad=copy.deepcopy(row)
        bad['words'].reverse()
        with self.assertRaises(ValueError):
            validate_segments([bad],8)

    def test_narration_two_voices_real_concat_cache_edits_only_one_paragraph(self):
        import narration
        params=DEFAULTS|{'workload_mode':'quiet'}
        segments=[{'text':'Đoạn một','voice_id':'piper:one','rate':1,'pause_after_ms':300},
                  {'text':'Đoạn hai','voice_id':'piper:two','rate':.9,'pause_after_ms':0}]
        calls=[]
        def resolve(voice_id,**kwargs):
            return {'id':voice_id,'name':voice_id,'provider':'piper'}
        def synthesize(voice_id,text,path,params):
            calls.append((voice_id,text))
            write_tone(path,frequency=220 if voice_id=='piper:one' else 440,duration=.4)
        cache=self.root/'data'
        cache.mkdir()
        with patch('narration.DATA',cache), patch('voices.resolve_voice',side_effect=resolve), \
                patch('voices.audio_identity',side_effect=lambda e,p:[e['id']]), \
                patch('voices.synthesize_text',side_effect=synthesize):
            first=narration.create_narration(segments,params,self.root,self.root/'first.wav')
            self.assertEqual(len(calls),2)
            self.assertAlmostEqual(first['duration'],1.1,delta=.01)
            self.assertEqual(first['voice_id'],'multiple')
            second=narration.create_narration(segments,params,self.root,self.root/'second.wav')
            self.assertEqual(second['cache_hits'],2)
            self.assertEqual(len(calls),2)
            changed=copy.deepcopy(segments)
            changed[1]['text']='Sửa đoạn hai'
            third=narration.create_narration(changed,params,self.root,self.root/'third.wav')
            self.assertEqual(third['cache_hits'],1)
            self.assertEqual(calls[-1],('piper:two','Sửa đoạn hai'))
            self.assertEqual(len(calls),3)
            changed[0]['pause_after_ms']=500
            fourth=narration.create_narration(changed,params,self.root,self.root/'fourth.wav')
            self.assertEqual(fourth['cache_hits'],2)
            self.assertEqual(len(calls),3)
            self.assertAlmostEqual(fourth['duration'],1.3,delta=.01)
        with wave.open(str(self.root/'first.wav'),'rb') as audio:
            self.assertEqual(audio.getframerate(),48000)
            audio.setpos(round(.41*48000))
            self.assertEqual(audio.readframes(round(.27*48000)),b'\0\0'*round(.27*48000))

    def test_render_real_ducking_normalization_and_verified_video(self):
        import worker
        from job_progress import verify_outputs
        source=self.root/'source.mp4'
        run([binary('ffmpeg'),'-v','error','-y','-threads','1','-filter_threads','1',
             '-f','lavfi','-i','color=c=navy:s=320x180:r=12:d=8','-an','-c:v','libx264',
             '-threads','1','-pix_fmt','yuv420p',source],timeout=30)
        voice=self.root/'voice.wav'
        bed=self.root/'bed.wav'
        write_tone(voice,frequency=1000,amplitude=12000,duration=8,active=(2,4))
        write_tone(bed,frequency=220,amplitude=4000,duration=8)
        media=probe(source)
        params=DEFAULTS|{'workload_mode':'quiet','video_encoder':'cpu','burn_subtitles':False,'use_clean':False,
                         'voice_path':str(voice),'voice_gain':.7,'original_gain':0,
                         'bgm_path':str(bed),'bgm_gain':.5}
        project={'params':params,'media':media,'segments':[],'regions':[],'artifacts':{}}
        outputs={}
        for label,ducking,normalize in [('plain',False,False),('ducked',True,True)]:
            folder=self.root/label
            folder.mkdir()
            project['params'].update(bgm_ducking=ducking,voice_normalize=normalize)
            with patch('worker.emit'):
                result=worker.render(project,{'kind':'render'},folder)
            verified=verify_outputs(result,project,{'kind':'render'},folder)
            self.assertTrue(verified)
            decoded=run([binary('ffmpeg'),'-v','error','-threads','1','-filter_threads','1','-i',folder/'final.mp4',
                         '-vn','-ac','1','-ar','16000','-f','f32le','pipe:1'],timeout=30)
            outputs[label]=np.frombuffer(decoded.stdout,dtype='<f4')
            self.assertLess(float(np.max(np.abs(outputs[label]))),.99)
        def projection(data,start,end):
            samples=data[round(start*16000):round(end*16000)]
            basis=np.exp(-2j*np.pi*220*np.arange(len(samples))/16000)
            return abs(np.dot(samples,basis))/len(samples)
        self.assertLess(projection(outputs['ducked'],2.6,3.5),projection(outputs['plain'],2.6,3.5)*.7)
        quiet_ratio=projection(outputs['ducked'],.5,1.5)/projection(outputs['plain'],.5,1.5)
        self.assertTrue(.75<quiet_ratio<1.25)


if __name__ == '__main__':
    unittest.main()
