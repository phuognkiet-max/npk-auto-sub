"""Opt-in production queue UVR acceptance, with isolated DATA and no API keys."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import traceback

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'tests/audio-separation-queue-results'
PORT=19867
TOKEN='npk-audio-separation-isolated-queue-test'
sys.path.insert(0,str(ROOT))


def serve():
    OUT.mkdir(exist_ok=True)
    temporary=tempfile.TemporaryDirectory(prefix='npk-separation-queue-',dir=ROOT/'tests')
    isolated=Path(temporary.name).resolve()
    assert isolated.parent==(ROOT/'tests').resolve()
    os.environ['VIETDUB_DATA_DIR']=str(isolated)
    os.environ['VIETDUB_TOKEN']=TOKEN
    import credentials
    credentials.keyring.get_password=lambda *args:None
    credentials.keyring.set_password=lambda *args:None
    credentials.keyring.delete_password=lambda *args:None
    import server
    import uvicorn
    from core import DATA,binary,run,get
    audio=ROOT/'tests/audio-separation-results/mixed-npk-speech-and-music.wav'
    if not audio.is_file():raise ValueError('Generate the owned speech/music fixture first.')
    video=OUT/'fixture8s.mp4'
    run([binary('ffmpeg'),'-v','error','-y','-f','lavfi','-i','color=c=navy:s=320x180:r=15:d=8',
         '-i',audio,'-af','apad','-t','8','-c:v','libx264','-preset','ultrafast','-threads','1',
         '-c:a','aac','-b:a','128k',video],timeout=30)
    http=uvicorn.Server(uvicorn.Config(server.app,host='127.0.0.1',port=PORT,log_level='warning',access_log=False))

    @server.app.get('/api/test/info')
    def info():return {'pid':os.getpid(),'data':str(DATA),'video':str(video),'ffmpeg':binary('ffmpeg'),'ffprobe':binary('ffprobe')}

    @server.app.get('/api/test/worker-log/{identifier}')
    def log(identifier:str):
        if not get('job',identifier):return {'log':''}
        path=DATA/'jobs'/identifier/'worker.log'
        return {'log':path.read_text(encoding='utf-8',errors='replace')[-30000:] if path.is_file() else ''}

    @server.app.post('/api/test/stop')
    def stop():http.should_exit=True;return {'stopping':True}

    timer=threading.Timer(300,lambda:setattr(http,'should_exit',True));timer.daemon=True;timer.start()
    try:
        server.start_workers();http.run()
    finally:
        timer.cancel();server.shutdown();temporary.cleanup()
        (OUT/'.queue-cleanup.json').write_text(json.dumps({'data_removed':not isolated.exists(),
            'queue_thread_stopped':server.queue_thread is None or not server.queue_thread.is_alive(),
            'active_worker_cleared':server.active_process is None}),encoding='utf-8')


def acceptance():
    import httpx
    import psutil
    OUT.mkdir(exist_ok=True)
    marker=OUT/'.queue-cleanup.json';marker.unlink(missing_ok=True)
    report={'ok':False,'inference':'Production UVR ONNX CPU queue; no remote API/GPU','checks':[]}
    caught=None;began=time.monotonic()
    with httpx.Client(base_url=f'http://127.0.0.1:{PORT}',headers={'x-studio-token':TOKEN},trust_env=False,timeout=25) as client:
        def request(method,path,**kwargs):
            response=client.request(method,path,**kwargs)
            assert response.is_success,f'{method} {path}: {response.status_code} {response.text}'
            return response.json()
        info=request('GET','/api/test/info');parent=psutil.Process(info['pid'])
        assert Path(info['data']).parent==ROOT/'tests' and Path(info['data']).name.startswith('npk-separation-queue-')
        report['isolated_data']=info['data']
        try:
            project=request('POST','/api/projects',json={'path':info['video'],'name':'NPK isolated UVR queue fixture'})
            project=request('PUT','/api/projects/'+project['id'],json={'expected_revision':project['updated'],
                'params':{'workload_mode':'quiet','voice_gain':.7,'original_gain':.2,'bgm_gain':.15}})
            job=request('POST','/api/jobs',json={'kind':'separate_audio','project_id':project['id']})
            states=['queued'];peak_rss=0;tick=time.monotonic()
            while time.monotonic()-tick<180:
                for child in parent.children(recursive=True):
                    try:peak_rss=max(peak_rss,child.memory_info().rss)
                    except (psutil.NoSuchProcess,psutil.AccessDenied):pass
                job=request('GET','/api/studio/jobs/'+job['id'])
                if job['state'] not in states:states.append(job['state'])
                if job['state'] in {'done','error','cancelled','review'}:break
                time.sleep(.1)
            report.update(job=job,states_seen=states,job_seconds=round(time.monotonic()-tick,3),peak_child_rss_mib=round(peak_rss/1048576,1),
                          worker_log=request('GET','/api/test/worker-log/'+job['id'])['log'])
            assert job['state']=='done',report
            assert job['result']['verification']['files_checked']==3
            assert all(state in states for state in ('queued','running','done'))
            assert job['result']['separation']['provider']=='CPUExecutionProvider'
            report['checks'].append('Real queued → running → verified done with CPUExecutionProvider')
            current=request('GET','/api/studio/projects/'+project['id'])
            assert current['params']['bgm_path']=='' and current['params']['original_gain']==.2
            endpoint='/api/studio/jobs/'+job['id']+'/audio-separation'
            review=request('GET',endpoint);assert review['ready']
            assert review['files']==['instrumentals.wav','vocals.wav']
            for filename in review['files']:
                response=client.get('/media/job/'+job['id']+'/'+filename);response.raise_for_status()
                target=OUT/filename;target.write_bytes(response.content)
                probe=subprocess.run([info['ffprobe'],'-v','error','-show_entries','format=duration','-of','json',str(target)],
                    capture_output=True,text=True,timeout=20,creationflags=0x08000000)
                assert probe.returncode==0,probe.stderr
                duration=float(json.loads(probe.stdout)['format']['duration']);assert abs(duration-8)<.05,duration
                decoded=subprocess.run([info['ffmpeg'],'-v','error','-xerror','-threads','1','-i',str(target),'-f','null','-'],
                    capture_output=True,text=True,timeout=30,creationflags=0x08000000)
                assert decoded.returncode==0,decoded.stderr
                report.setdefault('outputs',{})[filename]={'path':str(target),'bytes':len(response.content),
                    'sha256':hashlib.sha256(response.content).hexdigest(),'duration':duration,'decoded':True}
            report['checks'].append('Both downloadable float WAV stems decode fully and remain eight seconds')
            applied=request('POST',endpoint+'/apply',json={'expected_revision':current['updated']})
            assert applied['params']['bgm_path']==str(Path(info['data'])/'jobs'/job['id']/'instrumentals.wav')
            assert applied['params']['original_gain']==0 and applied['params']['bgm_gain']==1 and applied['params']['voice_gain']==.7
            report['checks'].append('Explicit revision-checked apply chooses owned background stem and preserves voice gain')
            stale=client.post(endpoint+'/apply',json={'expected_revision':current['updated']});assert stale.status_code==409
            with Path(info['video']).open('ab') as output:output.write(b'NPK stale-source acceptance fixture')
            assert request('GET',endpoint)['ready'] is False
            assert client.post(endpoint+'/apply',json={'expected_revision':applied['updated']}).status_code==409
            report['checks'].append('Old revision and changed source are rejected by server with 409')
            report['ok']=True
        except BaseException as error:
            caught=error;report['error']=traceback.format_exc()
        finally:
            try:
                request('POST','/api/test/stop')
                for _ in range(100):
                    if marker.is_file():
                        report['cleanup']=json.loads(marker.read_text(encoding='utf-8'));marker.unlink();break
                    time.sleep(.1)
                assert all(report.get('cleanup',{}).get(key) for key in ('data_removed','queue_thread_stopped','active_worker_cleared'))
            except BaseException as error:
                report['ok']=False;report['cleanup_error']=str(error)
            report['elapsed_seconds']=round(time.monotonic()-began,3)
            (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:report.get(key) for key in ('ok','job_seconds','peak_child_rss_mib','cleanup','error')},ensure_ascii=False),flush=True)
    if caught:raise caught
    assert report['ok'],report


if __name__=='__main__':serve() if '--server' in sys.argv else acceptance()
