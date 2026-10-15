"""Real offline subtitle/voice/clean/render and incremental cache acceptance."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import wave
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
out=root/'tests/studio-engine-results';out.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix='npk-engine-',dir=root/'tests') as isolated:
    os.environ['VIETDUB_DATA_DIR']=isolated
    from core import create_project, DATA, DEFAULTS, run, binary
    from subtitle_quality import coverage_record, review_signature
    from worker import pipeline, read_frame
    from job_progress import verify_outputs
    from performance import configure_compute
    import numpy as np
    import shutil
    configure_compute(DEFAULTS)
    project=create_project(str(root/'tests/fixtures/demo.mp4'),'Real offline acceptance')
    project['segments']=[dict(start=.2,end=3.8,text='Hello',translated='Xin chào bạn.'),dict(start=4,end=7.8,text='Goodbye',translated='Hẹn gặp lại.')]
    project['coverage']=coverage_record(project,'ocr',0,8,sampling_interval=1)
    project['params'].update(pipeline_extract='existing',pipeline_translate=False,pipeline_tts=True,pipeline_clean=True,
        use_clean=True,method='box',font_size=25,margin=26,original_gain=0,voice_gain=1,video_encoder='cpu')
    project['regions']=[dict(x=100,y=266,w=445,h=68,start=0,end=8)]
    reports=[]
    for name in ('first','reuse','edit-one'):
        if name=='edit-one':project['segments'][1]['translated']='Chúc bạn vui.'
        folder=DATA/'jobs'/name;folder.mkdir()
        began=time.perf_counter()
        result=pipeline(project,{'id':name,'kind':'pipeline'},folder)
        verification=verify_outputs(result,project,{'kind':'pipeline'},folder)
        assert result['primary_file']=='final.mp4'
        assert not result.get('needs_review')
        if name=='first':assert result['steps']==['tts','clean','render'],result
        if name=='reuse':assert result['steps']==['render'],result
        if name=='edit-one':
            assert result['steps']==['tts','render'],result
            assert result['artifacts']['tts']['cache_hits']==1,result
        reports.append(dict(name=name,steps=result['steps'],reused=result['reused'],seconds=time.perf_counter()-began,
                            cache_hits=result['artifacts'].get('tts',{}).get('cache_hits'),verification=verification))
        print('PASS',name,json.dumps(reports[-1]),flush=True)
        if name=='first':
            with wave.open(str(folder/'voice.wav'),'rb') as audio:
                samples=np.frombuffer(audio.readframes(audio.getnframes()),dtype='<i2')
                assert np.max(np.abs(samples.astype('int32')))>100
            clean=read_frame(folder/'clean.mp4',2);final=read_frame(folder/'final.mp4',2)
            assert np.abs(clean[250:340].astype(float)-final[250:340]).mean()>1,'subtitles not visible'
            for file in ('final.mp4','voice.wav','subtitles.srt','subtitles.ass'):
                shutil.copy2(folder/file,out/file)
            from worker import write_image
            write_image(out/'final-frame.jpg',final)
    (out/'report.json').write_text(json.dumps({'tests':reports,'providers':'No API requests; supplied Vietnamese test text','real_engines':['Piper','FFmpeg cover','ASS burn-in','AAC mix','full video decode']},ensure_ascii=False,indent=2),encoding='utf-8')
