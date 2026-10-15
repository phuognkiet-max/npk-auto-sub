"""Isolated real Windows icon/relaunch-property test. No user project is opened."""
import base64
import json
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from windows_identity import ICON, set_process_identity, attach_window_identity, WindowProperties, identity_properties
import webview

out = ROOT / 'tests/icon-results'
out.mkdir(exist_ok=True)
set_process_identity()
image = base64.b64encode((ROOT / 'static/npk-app-icon.png').read_bytes()).decode()
window = webview.create_window('NPK Auto Sub — Kiểm tra icon', width=820, height=530,
    html=f'''<body style="background:#101216;color:#f2f3f5;font:18px Segoe UI;text-align:center;padding:48px">
    <img src="data:image/png;base64,{image}" width="128" height="128">
    <h1 style="color:#ff941f">NPK AUTO SUB</h1>
    <p>Biểu tượng riêng · cửa sổ và thanh tác vụ Windows</p>
    <small>Cửa sổ kiểm thử riêng, không mở dự án hay chạy xử lý video.</small></body>''',
    background_color='#101216', confirm_close=False)
outcome = {}


def check():
    try:
        window.events.shown.wait(15)
        attach_window_identity(window)
        from System import Action
        def inspect():
            hwnd = window.native.Handle.ToInt64()
            with WindowProperties(hwnd) as store:
                saved = {k:store.get(k) for k in identity_properties()}
            assert saved == identity_properties(), saved
            assert window.native.Icon is not None
            assert window.native.Icon.Width >= 16
            outcome.update(ok=True, properties=saved, icon_size=[window.native.Icon.Width, window.native.Icon.Height],
                           window_title=str(window.native.Text))
        window.native.Invoke(Action(inspect))
    except Exception as exc:
        outcome.update(ok=False, error=repr(exc))
    (out / 'native-report.json').write_text(json.dumps(outcome, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(outcome, ensure_ascii=False), flush=True)
    # Allow human/tool visual inspection; closes itself if not closed manually.
    timer = threading.Timer(120 if outcome.get('ok') else 1, window.destroy)
    timer.daemon = True
    timer.start()


webview.start(check, gui='edgechromium', icon=str(ICON), private_mode=True)
assert outcome.get('ok'), outcome
