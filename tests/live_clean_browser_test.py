"""Live preview pixels and interaction lifecycle on an isolated real app server."""
import atexit
import json
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright, expect

BASE='http://127.0.0.1:19864'
HEADERS={'x-studio-token':'npk-studio-test'}
OUT=Path(__file__).parent/'live-clean-ui-results'
OUT.mkdir(exist_ok=True)
checks, errors, jobs, frames=[],[],[],[]

def stop():
    try: httpx.post(BASE+'/api/test/stop',headers=HEADERS,json={},timeout=3)
    except httpx.HTTPError: pass
atexit.register(stop)

with sync_playwright() as pw:
    browser=pw.chromium.launch(channel='msedge',headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1000})
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('request',lambda r:jobs.append(r.post_data_json) if r.url.endswith('/api/jobs') and r.method=='POST' else
            frames.append(r.post_data_json) if r.url.endswith('/live-clean-frame') else None)
    page.goto(BASE+'/studio#token=npk-studio-test');page.wait_for_load_state('networkidle')
    expect(page.locator('#projectName')).to_contain_text('Dự án kiểm thử')
    identifier=page.evaluate('P.id');url=BASE+'/api/studio/projects/'+identifier
    p=page.request.get(url,headers=HEADERS).json()
    region=dict(x=100,y=265,w=420,h=75,start=0,end=8)
    assert page.request.put(BASE+'/api/projects/'+identifier,headers=HEADERS,data={
        'expected_revision':p['updated'],'regions':[region],
        'params':dict(method='box',cover_color='#000000',cover_opacity=1,dilate=0,edge_feather=0)}).ok
    page.reload();page.wait_for_load_state('networkidle')
    page.locator('[data-area="clean"]').click()
    expect(page.locator('#cleanLiveCanvas')).to_be_visible()
    page.wait_for_function('video.readyState>=2')

    def mean(x=180,y=285,w=200,h=30):
        return page.evaluate('''([x,y,w,h]) => {
          const d=document.querySelector('#cleanLiveCanvas').getContext('2d').getImageData(x,y,w,h).data;
          let sum=0;for(let i=0;i<d.length;i+=4) sum+=d[i]+d[i+1]+d[i+2];return sum/(w*h*3);
        }''',[x,y,w,h])

    assert mean()<2
    checks.append('Entering Chữ gốc immediately applies the effect to viewer pixels, without a preview button or job')
    slider=page.get_by_role('slider',name='Độ đậm',exact=True)
    box=slider.bounding_box()
    page.mouse.move(box['x']+box['width']-6,box['y']+box['height']/2);page.mouse.down()
    page.mouse.move(box['x']+box['width']*.3,box['y']+box['height']/2,steps=5)
    page.wait_for_function('cleanLiveSnapshot().params.cover_opacity < .5 && !cleanLive.canvas.hidden')
    page.wait_for_function('cleanLive.ctx.getImageData(200,290,1,1).data[1]>20')
    assert mean()>20
    assert page.evaluate('P.params.cover_opacity')==1
    assert not jobs and not frames
    page.screenshot(path=str(OUT/'01-held-slider-live.png'),full_page=True)
    page.mouse.up();expect(page.locator('#saved')).to_have_text('Đã lưu')
    assert page.request.get(url,headers=HEADERS).json()['params']['cover_opacity']<.5
    checks.append('Holding and moving a slider changes pixels before mouse-up; history/save commit only on release')

    page.locator('#cleanLiveToggle').click();expect(page.locator('#cleanLiveCanvas')).to_be_hidden()
    page.locator('#cleanLiveToggle').click();expect(page.locator('#cleanLiveCanvas')).to_be_visible()
    checks.append('One-click source comparison really hides effects, then restores live preview')

    # Start with no saved region; effect must appear in the unsaved rectangle.
    page.evaluate('change(p=>{p.regions=[];p.params.cover_opacity=1}, {render:true})')
    page.locator('#drawRegion').click();r=page.locator('#regions').bounding_box()
    page.mouse.move(r['x']+r['width']*.15,r['y']+r['height']*.72);page.mouse.down()
    page.mouse.move(r['x']+r['width']*.75,r['y']+r['height']*.94,steps=5)
    expect(page.locator('#regionDraft')).to_be_visible();expect(page.locator('#cleanLiveCanvas')).to_be_visible()
    assert mean()<2
    assert page.evaluate('P.regions.length')==0
    page.keyboard.press('Escape');page.mouse.up()
    expect(page.locator('#cleanLiveCanvas')).to_be_hidden()
    assert page.evaluate('P.regions.length')==0
    checks.append('Drawing previews the effect while held; Escape removes both draft rectangle and effect without saving')

    page.evaluate('change(p=>p.regions=[{x:100,y:265,w:420,h:75,start:0,end:2}],{render:true})')
    expect(page.locator('#cleanLiveCanvas')).to_be_visible()
    page.evaluate('seek(3)');page.wait_for_function('!video.seeking')
    expect(page.locator('#cleanLiveCanvas')).to_be_hidden()
    page.evaluate('seek(0)');expect(page.locator('#cleanLiveCanvas')).to_be_visible()
    page.locator('#regionList [data-select-region="0"]').click()
    page.locator('#panel .clean-region-actions [data-region-command="delete"]').click();expect(page.locator('#cleanLiveCanvas')).to_be_hidden()
    page.locator('#undo').click();expect(page.locator('#cleanLiveCanvas')).to_be_visible()
    checks.append('Seek respects region time; delete clears effect immediately; undo restores it')

    # Verify blur and stretch produce different live pixels on the same paused frame.
    page.locator('.param[data-key="method"]').select_option('blur')
    page.wait_for_function('!cleanLive.canvas.hidden')
    page.evaluate('cleanLive.paint()');a=page.locator('#cleanLiveCanvas').screenshot()
    page.get_by_role('slider',name='Làm mờ',exact=True).fill('48')
    page.evaluate('cleanLive.paint()');b=page.locator('#cleanLiveCanvas').screenshot()
    assert a!=b
    page.locator('.param[data-key="method"]').select_option('stretch')
    page.get_by_role('slider',name='Khoảng cách',exact=True).fill('30')
    page.evaluate('cleanLive.paint()');c=page.locator('#cleanLiveCanvas').screenshot()
    assert c!=a and c!=b
    assert not jobs and not frames
    checks.append('Blur strength and stretch offset change paused-frame pixels without backend work')

    page.locator('.param[data-key="method"]').select_option('telea')
    expect(page.locator('#cleanLiveFrame')).to_be_visible(timeout=20000)
    expect(page.locator('#cleanLiveStatus')).to_contain_text('đã cập nhật')
    assert len(frames)==1 and not jobs
    expect(page.locator('#progressDetails')).to_be_hidden()
    page.screenshot(path=str(OUT/'02-real-restoration.png'),full_page=True)
    checks.append('Restoration automatically computes one real frame after idle, with no job history/files or progress drawer')

    # Hold the old response while editing. Only the latest settings may display.
    import cv2
    import numpy as np
    delayed=[]
    page.route('**/live-clean-frame',lambda route: delayed.append(route))
    page.get_by_role('slider',name='Bán kính',exact=True).fill('3')
    with page.expect_request('**/live-clean-frame'):
        page.evaluate('cleanLive.paint()')
    page.wait_for_timeout(100)  # Allow the interception callback to run after request observation.
    assert len(delayed)==1
    page.get_by_role('slider',name='Bán kính',exact=True).fill('7')
    page.evaluate('cleanLive.paint()')
    jpeg=cv2.imencode('.jpg',np.zeros((16,16,3),np.uint8))[1].tobytes()
    with page.expect_request('**/live-clean-frame'):
        delayed[0].fulfill(status=200,content_type='image/jpeg',body=jpeg)
    page.wait_for_timeout(100)
    expect(page.locator('#cleanLiveFrame')).to_be_hidden()
    assert len(delayed)==2 and delayed[1].request.post_data_json['params']['inpaint_radius']==7
    delayed[1].fulfill(status=200,content_type='image/jpeg',body=jpeg)
    expect(page.locator('#cleanLiveFrame')).to_be_visible()
    page.unroute('**/live-clean-frame')
    checks.append('Late restoration replies are discarded; rapid edits coalesce into one latest request')

    # Explicit errors must hide previous results, not leave them looking current.
    page.route('**/live-clean-frame',lambda route: route.fulfill(status=409,json={'detail':'Đang có tác vụ xử lý video.'}))
    page.get_by_role('slider',name='Bán kính',exact=True).fill('8')
    expect(page.locator('#cleanLiveFrame')).to_be_hidden()
    expect(page.locator('#cleanLiveStatus')).to_contain_text('Đang có tác vụ',timeout=10000)
    assert page.locator('#cleanLiveBar').get_attribute('data-state')=='error'
    page.unroute('**/live-clean-frame')
    page.locator('.param[data-key="method"]').select_option('blur')
    page.evaluate('change(p=>p.regions[0].end=8,{render:true})')
    page.locator('#play').click()
    page.wait_for_function('video.currentTime>.2 && !cleanLive.canvas.hidden')
    before_play=page.locator('#cleanLiveCanvas').screenshot()
    page.wait_for_function('video.currentTime>.8')
    after_play=page.locator('#cleanLiveCanvas').screenshot()
    assert before_play!=after_play
    page.locator('#play').click()
    checks.append('Lightweight live effects follow playback instead of freezing the previous still frame')
    # Test worst-case large blur paint latency without touching project metadata.
    timings=page.evaluate('''() => {
      const s=cleanLiveSnapshot();s.width=1920;s.height=1080;s.params.blur_radius=64;
      const r=[{x:0,y:0,w:1920,h:1080,start:0,end:8}],times=[];
      for(let i=0;i<8;i++){const t=performance.now();cleanLive.quick(s,r);times.push(performance.now()-t)}
      cleanLive.refresh();return times;
    }''')
    assert max(timings)<250,timings
    checks.append('Full-frame 1080p blur uses a bounded quick-preview resolution; measured paints stay below 250 ms')
    page.locator('[data-area="subtitles"]').click()
    expect(page.locator('#cleanLiveBar')).to_be_hidden();expect(page.locator('#cleanLiveFrame')).to_be_hidden()
    checks.append('Busy/error responses hide stale restoration; leaving Chữ gốc removes all preview overlays')
    page.locator('[data-area="clean"]').click()
    for width in (1920,1280,1024,720,400):
        page.set_viewport_size({'width':width,'height':1000})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),width
    assert not errors,errors
    browser.close()
(OUT/'browser-results.json').write_text(json.dumps({'checks':checks,'errors':errors,'full_frame_blur_ms':timings},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'checks':checks,'errors':errors},ensure_ascii=False),flush=True)
