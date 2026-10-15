"""Test both extraction engines and subtitle-preserving API translation adapters."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding='utf-8')
import json
import tempfile
from unittest.mock import patch
from core import ROOT, DEFAULTS, probe
from worker import asr, ocr, translation

fixture=ROOT/'tests/fixtures/demo.mp4'
report=[]
project={'media':probe(fixture),'regions':[dict(x=140.2,y=285.4,w=360.8,h=48.3,start=0,end=8)],
         'params':DEFAULTS|{'ocr_fps':1,'asr_model':'tiny','asr_device':'cpu'},'segments':[]}
with tempfile.TemporaryDirectory(dir=ROOT/'tests') as tmp:
    folder=Path(tmp)
    result=ocr(project,{},folder)
    assert any('SUBTITLE' in s['text'].upper() for s in result['segments']),result
    report.append({'engine':'RapidOCR','passed':True,'segments':len(result['segments'])})
    result=asr(project,{},folder)
    assert result['segments'],result
    report.append({'engine':'Whisper tiny CPU int8','passed':True,'segments':result['segments']})
    project['segments']=[{'start':0.1,'end':2.1,'text':'你好','translated':''}]
    class FakeResponse:
        status_code=200
        def json(self):
            return {'candidates':[{'content':{'parts':[{'text':'{"lines":[{"id":0,"text":"Xin chào"}]}'}]}}],
                    'choices':[{'message':{'content':'{"lines":[{"id":0,"text":"Xin chào"}]}'}}]}
    for provider in ['gemini','deepseek']:
        project['params']['provider']=provider
        project['params']['translation_model']='gemini-2.5-flash' if provider=='gemini' else 'deepseek-flash'
        with patch('keyring.get_password',return_value='fake-local-test-key'),patch('httpx.Client.post',return_value=FakeResponse()) as post:
            translated=translation(project,{},folder)
            assert translated['segments'][0]['translated']=='Xin chào'
            assert translated['segments'][0]['start']==.1
            assert post.call_count==1
            report.append({'engine':provider+' JSON adapter','passed':True,'network':'mocked; no live API call'})
(ROOT/'tests/extraction-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print('EXTRACTION AND TRANSLATION ADAPTER CHECKS PASSED')
