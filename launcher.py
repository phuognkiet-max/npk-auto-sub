"""Small native launcher; the isolated runtime and original source stay beside it."""
from pathlib import Path
import subprocess
import sys

root = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
python = root / 'runtime/venv/Scripts/pythonw.exe'
if not python.exists():
    import ctypes
    ctypes.windll.user32.MessageBoxW(0, 'Thiếu runtime. Đọc README.md trong thư mục ứng dụng.', 'NPK Auto Sub', 16)
else:
    log_path = root / 'data/app.log'
    log_path.parent.mkdir(exist_ok=True)
    with log_path.open('ab') as log:
        subprocess.Popen([str(python), str(root / 'app.py')], cwd=root, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
