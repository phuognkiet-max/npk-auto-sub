"""Native Windows shell around our own local application, never around Xtrans."""
import argparse
import atexit
import os
from pathlib import Path
import socket
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent
os.environ.setdefault('HF_HOME', str(ROOT / 'models/huggingface'))
os.environ.setdefault('TORCH_HOME', str(ROOT / 'models/torch'))

def main():
    if sys.stdout is None or sys.stderr is None:
        log_root = Path(os.environ.get('VIETDUB_DATA_DIR', str(ROOT / 'data'))).resolve()
        log_root.mkdir(parents=True, exist_ok=True)
        native_log = (log_root / 'app.log').open('a', encoding='utf-8', buffering=1)
        if sys.stdout is None:
            sys.stdout = native_log
        if sys.stderr is None:
            sys.stderr = native_log
    parser = argparse.ArgumentParser()
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    from core import DATA
    if args.headless and DATA == ROOT / 'data' and os.environ.get('VIETDUB_ALLOW_HEADLESS_USER_DATA') != '1':
        raise RuntimeError('Headless phải dùng VIETDUB_DATA_DIR riêng cho kiểm thử. Không chạy trên dữ liệu người dùng.')
    # Only one owner may run the local job queue at a time.
    import msvcrt
    lock_path = DATA / 'studio.lock'
    lock_path.parent.mkdir(exist_ok=True)
    instance_lock = lock_path.open('a+b')
    instance_lock.seek(0)
    if lock_path.stat().st_size == 0:
        instance_lock.write(b'0')
        instance_lock.flush()
        instance_lock.seek(0)
    try:
        msvcrt.locking(instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        instance_lock.close()
        if not args.headless:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, 'NPK Auto Sub đang chạy. Hãy mở cửa sổ hiện tại trên thanh tác vụ.', 'NPK Auto Sub', 64)
        return
    atexit.register(instance_lock.close)
    import uvicorn
    import server
    if args.port:
        port = args.port
    else:
        with socket.socket() as candidate:
            candidate.bind(('127.0.0.1', 0))
            port = candidate.getsockname()[1]
    server.start_workers()
    server.register_owner(port)
    atexit.register(server.shutdown)
    config = uvicorn.Config(server.app, host='127.0.0.1', port=port, log_level='warning', access_log=False)
    http = uvicorn.Server(config)
    if args.headless:
        print(f'NPK Auto Sub: http://127.0.0.1:{port}', flush=True)
        http.run()
        return
    from windows_identity import ICON, set_process_identity, attach_window_identity
    try:
        set_process_identity()
    except OSError:
        import logging
        logging.getLogger(__name__).warning('Could not set NPK process identity.', exc_info=True)
    import webview
    webview.settings['ALLOW_DOWNLOADS'] = True
    class Bridge:
        def pick_videos(self):
            selected = window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True,
                file_types=('Video (*.mp4;*.mkv;*.mov;*.avi;*.webm;*.m4v)',))
            return list(selected) if selected else []
        def pick_video(self):
            selected = window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False,
                file_types=('Video (*.mp4;*.mkv;*.mov;*.avi;*.webm;*.m4v)',))
            return selected[0] if selected else None
        def pick_audio(self):
            selected = window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False,
                file_types=('Audio (*.wav;*.mp3;*.flac;*.m4a;*.ogg;*.aac)',))
            return selected[0] if selected else None
        def pick_directory(self):
            selected = window.create_file_dialog(webview.FileDialog.FOLDER)
            return selected[0] if selected else None
    thread = threading.Thread(target=http.run, daemon=True)
    thread.start()
    for _ in range(100):
        if http.started:
            break
        time.sleep(.05)
    screen=webview.screens[0]
    # pywebview screen sizes are logical pixels (already account for scaling).
    # Do not create a window taller than a laptop's usable desktop.
    initial_width=max(800,min(1440,screen.width-40))
    initial_height=max(480,min(940,screen.height-80))
    window = webview.create_window('NPK Auto Sub', f'http://127.0.0.1:{port}/#token={server.TOKEN}',
        js_api=Bridge(), width=initial_width, height=initial_height, maximized=True,
        min_size=(min(900,initial_width),min(540,initial_height)), background_color='#090909', confirm_close=True)
    def closed():
        server.shutdown()
        http.should_exit = True
    window.events.closed += closed
    window.events.shown += lambda: attach_window_identity(window)
    storage = DATA / 'webview'
    storage.mkdir(exist_ok=True)
    webview.start(gui='edgechromium', private_mode=False, storage_path=str(storage), icon=str(ICON),
        localization={'global.quitConfirmation': 'Đóng NPK Auto Sub? Tác vụ đang chạy sẽ dừng. Hãy lưu dự án trước khi đóng.'})

if __name__ == '__main__':
    main()
