"""Real OCR -> controlled 503/retry -> real Piper -> clean -> final video.

Provider transport is synthetic; all image/audio/media engines run locally.
No production DATA/keys are read or modified and no network API is called.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
OUT=ROOT/'tests/pipeline-resilience-results'
OUT.mkdir(exist_ok=True)

with tempfile.TemporaryDirectory(prefix='npk-full-acceptance-',dir=ROOT/'tests') as temporary:
    os.environ['VIETDUB_DATA_DIR']=temporary
    os.environ['HF_HUB_OFFLINE']='1'
    os.environ['TRANSFORMERS_OFFLINE']='1'
    from core import DATA,create_project
    from job_progress import verify_outputs
    from performance import configure_compute
    import providers
    import worker
    import httpx

    project=create_project(str(ROOT/'tests/fixtures/demo.mp4'),'Full native resilience acceptance')
    project['params'].update(workload_mode='fast',pipeline_extract='ocr',pipeline_translate=True,
        pipeline_tts=True,pipeline_clean=True,use_clean=True,method='box',font='Tahoma',font_size=25,
        margin=26,original_gain=0,voice_gain=1,video_encoder='cpu',translation_scope='missing',ocr_fps=2)
    project['regions']=[{'x':150,'y':280,'w':340,'h':55,'start':0,'end':8}]
    configure_compute(project['params'])
    events=[];requests=[];first=True
    def transport(request):
        global first
        body=json.loads(request.content)
        prompt=body['contents'][0]['parts'][0]['text']
        lines=json.loads(prompt.split('Input:\n')[1])
        requests.append({'ids':[r['id'] for r in lines],'body':body})
        if first:
            first=False
            return httpx.Response(503,json={'error':{'message':'synthetic outage'}},request=request)
        output=json.dumps({'lines':[{'id':r['id'],'text':'Chào.'} for r in lines]},ensure_ascii=False)
        return httpx.Response(200,json={'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':output}]}}]},request=request)
    generate=providers.generate
    began=time.perf_counter()
    folder=DATA/'jobs'/'full';folder.mkdir()
    with httpx.Client(transport=httpx.MockTransport(transport)) as client, \
            patch('credentials.active_secret',return_value='mock-transport-only'), \
            patch('providers._wait_retry'),patch('worker.emit',side_effect=lambda **event:events.append(event)):
        def controlled_generate(provider,secret,model,prompt,**kwargs):
            return generate(provider,secret,model,prompt,client=client,**kwargs)
        with patch('providers.generate',side_effect=controlled_generate):
            result=worker.pipeline(project,{'id':'full','kind':'pipeline'},folder)
        verification=verify_outputs(result,project,{'kind':'pipeline'},folder)
    assert result.get('primary_file')=='final.mp4',result
    assert result['steps']==['ocr','translate','tts','clean','render'],result['steps']
    assert result['segments'] and all(r['translated']=='Chào.' for r in result['segments'])
    assert len(requests)==2 and requests[0]['body']==requests[1]['body']
    assert any('503' in e.get('message','') or 'tạm gián đoạn' in e.get('message','') for e in events)
    assert verification['video_frames_decoded'].get('final.mp4')==96,verification
    assert verification['audio_frames_verified'].get('voice.wav',0)>0,verification
    for name in ('source.srt','translated.srt','voice.wav','clean.mp4','final.mp4'):
        shutil.copy2(folder/name,OUT/name)
    worker.write_image(OUT/'final-frame.jpg',worker.read_frame(folder/'final.mp4',2))
    report={'ok':True,'seconds':round(time.perf_counter()-began,3),'steps':result['steps'],
            'cue_count':len(result['segments']),'source_text':[r['text'] for r in result['segments']],
            'provider_requests':len(requests),'same_payload_after_503':True,
            'verification':verification,'engines':['RapidOCR CPU','HTTP MockTransport','Piper CPU','FFmpeg cover','ASS burn','AAC mux'],
            'provider_network_calls':0,'data_isolated':True}
    project['params'].update(pipeline_extract='existing',pipeline_translate=False)
    reuse=DATA/'jobs'/'reuse';reuse.mkdir()
    reused=worker.pipeline(project,{'id':'reuse','kind':'pipeline'},reuse)
    report['reuse_steps']=reused['steps'];report['reused']=reused['reused']
    assert reused['steps']==['render'],reused
    verify_outputs(reused,project,{'kind':'pipeline'},reuse)
    (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)

assert not Path(temporary).exists()
