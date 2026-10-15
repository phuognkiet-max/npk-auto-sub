"""Real offline engines on an 8s synthetic video; provider response explicitly mocked."""
import os
from pathlib import Path
import sys
import tempfile
import json
import time
import copy
import wave
from unittest.mock import patch

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
out=root/'tests/functional-results'
out.mkdir(exist_ok=True)

with tempfile.TemporaryDirectory(prefix='npk-functional-',dir=root/'tests') as isolated:
    os.environ['VIETDUB_DATA_DIR']=isolated
    from core import DEFAULTS, create_project, DATA
    from voices import entries, synthesize_sample
    from performance import configure_compute
    from worker import clean, pipeline, ocr, read_frame
    from job_progress import verify_outputs
    import psutil
    import numpy as np
    configure_compute(DEFAULTS|{'workload_mode':'quiet'})
    measurements=[]
    for i,entry in enumerate(entries()):
        began=time.perf_counter()
        destination=out/f'voice-{i:02d}.wav'
        result=synthesize_sample(entry['id'],'Xin chào bạn.',destination,DEFAULTS)
        with wave.open(str(destination),'rb') as audio:
            seconds=audio.getnframes()/audio.getframerate()
            samples=np.frombuffer(audio.readframes(audio.getnframes()),dtype='<i2')
        assert seconds>.1 and np.max(np.abs(samples.astype('int32')))>100,entry['id']
        measurements.append({'id':entry['id'],'name':entry['name'],'elapsed':round(time.perf_counter()-began,3),'audio_seconds':round(seconds,3)})
        print('VOICE',i+1,result['voice_name'],seconds,flush=True)
    project=create_project(str(root/'tests/fixtures/demo.mp4'),'Functional synthetic A-Z')
    project['regions']=[{'x':100,'y':270,'w':440,'h':65,'start':1,'end':6}]
    project['params'].update(video_encoder='cpu',workload_mode='quiet',font_size=24,method='box',dilate=0,ocr_fps=1)
    covers=[]
    for method in ('box','blur','stretch'):
        target=out/method;target.mkdir(exist_ok=True)
        project['params']['method']=method
        began=time.perf_counter()
        result=clean(project,{'kind':'clean'},target)
        verify_outputs(result,project,{'kind':'clean'},target)
        before=read_frame(project['media']['path'],2);after=read_frame(target/'clean.mp4',2)
        assert np.abs(before[280:325,110:530].astype('float32')-after[280:325,110:530]).mean()>2,method
        # Effect is time-bound: before its interval, only encode differences remain.
        first=read_frame(project['media']['path'],.2);first_after=read_frame(target/'clean.mp4',.2)
        assert np.abs(first.astype('float32')-first_after).mean()<8,method
        covers.append(result|{'wall_seconds':time.perf_counter()-began})
        print('COVER',method,round(covers[-1]['wall_seconds'],2),flush=True)
    # OCR is a real separate smoke test. Its output supplies the pipeline input.
    project['regions']=[{'x':0,'y':240,'w':640,'h':100,'start':0,'end':8}]
    ocr_folder=out/'ocr';ocr_folder.mkdir(exist_ok=True)
    result=ocr(project,{'kind':'ocr'},ocr_folder)
    assert result['segments'],'OCR produced no synthetic subtitle'
    project['segments']=result['segments']
    print('OCR',len(project['segments']),flush=True)
    project['params'].update(method='box',pipeline_extract='existing',pipeline_translate=True,pipeline_tts=True,
        pipeline_clean=True,original_gain=0,voice_id=entries()[1]['id'])
    target=DATA/'jobs'/'functional-pipeline';target.mkdir(parents=True)
    calls=[]
    def fake_translation(provider,secret,model,prompt,**kwargs):
        inputs=json.loads(prompt.split('Input:\n')[1]);calls.append(inputs)
        return json.dumps({'lines':[{'id':line['id'],'text':'Xin chào bạn.'} for line in inputs]},ensure_ascii=False)
    with patch('credentials.active_secret',return_value='fake-functional-key'),patch('providers.generate',side_effect=fake_translation):
        result=pipeline(project,{'kind':'pipeline','id':'functional-pipeline'},target)
    verification=verify_outputs(result,project,{'kind':'pipeline'},target)
    assert result['steps']==['translate','tts','clean','render']
    assert calls and 'voice.wav' in result['files'] and 'final.mp4' in result['files']
    # Preserve generated results for visual/auditory inspection, not user project data.
    import shutil
    for name in ('final.mp4','voice.wav','subtitles.srt'):
        shutil.copyfile(target/name,out/name)
    report={'voices':measurements,'covers':covers,'ocr_segments':len(project['segments']),
        'pipeline':{'steps':result['steps'],'elapsed_seconds':result['elapsed_seconds'],'verification':verification,
                    'translation':'MOCKED provider response; no real API key or quota used','warnings':result['warnings']}}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('ALL OFFLINE SMOKES PASSED',json.dumps(report['pipeline'],ensure_ascii=False),flush=True)
