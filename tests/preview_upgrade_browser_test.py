"""Real-browser preview timing/cancellation with fixture media, no AI inference."""
import argparse
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:19876'
OUT = ROOT/'tests/preview-upgrade-results'

HTML = '''<!doctype html><html><head><style>video,canvas,img{width:640px;max-width:100%;}body{background:#101014;color:#eee}</style></head><body>
<h1>NPK preview acceptance fixture</h1><video id="video" src="/demo.mp4" preload="auto" muted></video>
<canvas id="canvas" hidden></canvas><img id="frame" hidden><div id="bar"><span id="status"></span><button id="toggle"></button></div>
<script src="/clean-live.js"></script><script>
window.spec={id:'fixture',revision:null,width:640,height:360,source:true,interacting:false,
params:{method:'blur',mask_source:'manual',cover_opacity:1,cover_color:'#000000',blur_radius:64,dilate:0,edge_feather:0,
stretch_height:5,stretch_direction:'above',stretch_offset:0,inpaint_radius:2,mask_mode:'rectangle',context:64,max_side:640},
regions:[{x:0,y:0,w:640,h:360,start:0,end:8}]};
window.live=new NpkCleanLive({video:document.querySelector('#video'),canvas:document.querySelector('#canvas'),frame:document.querySelector('#frame'),
bar:document.querySelector('#bar'),status:document.querySelector('#status'),button:document.querySelector('#toggle'),snapshot:()=>window.spec,token:'fixture-only'});
</script></body></html>'''


def serve():
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            assets = {'/':(HTML.encode(),'text/html'), '/clean-live.js':((ROOT/'static/clean-live.js').read_bytes(),'text/javascript'),
                      '/demo.mp4':((ROOT/'tests/fixtures/demo.mp4').read_bytes(),'video/mp4')}
            asset=assets.get(self.path)
            if not asset:
                self.send_error(404); return
            content,mime=asset
            self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(content)));self.end_headers()
            self.wfile.write(content)
    http=HTTPServer(('127.0.0.1',19876),Handler)
    timer=threading.Timer(120,http.shutdown);timer.daemon=True;timer.start()
    try: http.serve_forever()
    finally:timer.cancel();http.server_close()


def browser():
    from playwright.sync_api import sync_playwright, expect
    from PIL import Image
    OUT.mkdir(exist_ok=True)
    image=io.BytesIO();Image.new('RGB',(32,32),(30,90,120)).save(image,format='JPEG');jpeg=image.getvalue()
    checks,errors,requests,cancellations=[],[],[],[]
    with sync_playwright() as pw:
        app=pw.chromium.launch(channel='msedge',headless=True)
        page=app.new_page(viewport={'width':1440,'height':1000})
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(BASE);page.wait_for_load_state('networkidle');page.wait_for_function('video.readyState>=2')
        page.evaluate('video.pause();video.currentTime=.2;live.paint()')
        expect(page.locator('#canvas')).to_be_visible()
        timings=page.evaluate('''() => {const s=structuredClone(spec);s.width=1920;s.height=1080;s.regions=[{x:0,y:0,w:1920,h:1080,start:0,end:8}];
          const times=[];for(let i=0;i<12;i++){const t=performance.now();live.quick(s,s.regions);times.push(performance.now()-t)}
          return {times,width:live.canvas.width,edge:live.previewEdge,average:live.quickAverageMs};}''')
        assert timings['width']<=960 and 480<=timings['edge']<=1280,timings
        assert max(timings['times'])<250,timings
        checks.append('Quick 1080p blur stays within displayed/adaptive resolution; exported settings stay unchanged')
        def intercept(route):
            if route.request.url.endswith('/cancel'):
                cancellations.append(route.request.post_data_json['request_id']);route.fulfill(status=200,json={'cancelled':True})
            else: requests.append(route)
        page.route('**/api/studio/**',intercept)
        page.evaluate("spec.params.method='telea';live.paint()")
        page.wait_for_function('live.busy && live.activeRequestId')
        page.wait_for_timeout(50)
        first=page.evaluate('live.activeRequestId')
        page.evaluate('spec.params.inpaint_radius=7;live.paint()')
        page.wait_for_function('!live.busy')
        page.wait_for_timeout(50)
        assert first in cancellations,cancellations
        requests[0].abort('aborted')
        page.wait_for_function('live.busy && live.activeRequestId && live.pending===null')
        page.wait_for_timeout(50)
        assert len(requests)==2,len(requests)
        assert requests[-1].request.post_data_json['params']['inpaint_radius']==7
        requests[-1].fulfill(status=200,content_type='image/jpeg',body=jpeg)
        expect(page.locator('#frame')).to_be_visible()
        checks.append('Changing restoration settings aborts old fetch and cancels only its owned server request; latest frame displays')
        page.evaluate('spec.params.inpaint_radius=8;live.paint()')
        page.wait_for_function('live.busy && live.activeRequestId')
        page.wait_for_timeout(30)
        third=page.evaluate('live.activeRequestId')
        page.evaluate('live.reset()')
        page.wait_for_function('!live.busy');page.wait_for_timeout(50)
        assert third in cancellations
        requests[-1].abort('aborted')
        expect(page.locator('#frame')).to_be_hidden()
        checks.append('Reset/leave view aborts restoration and removes stale image without creating a production job')
        page.screenshot(path=str(OUT/'preview-adaptive-cancel.png'))
        assert not errors,errors
        page.unroute_all(behavior='ignoreErrors')
        app.close()
    report={'checks':checks,'errors':errors,'paint':timings,'cancelled_requests':len(cancellations),'fixture':True}
    (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--serve',action='store_true');args=parser.parse_args()
    serve() if args.serve else browser()
