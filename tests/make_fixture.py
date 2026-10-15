"""Generate original synthetic footage for repeatable tests, not user media."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import ROOT, MODELS, binary, run
import wave
from piper import PiperVoice

fixture = ROOT / 'tests/fixtures'
fixture.mkdir(exist_ok=True)
voice = PiperVoice.load(str(MODELS / 'piper/vi_VN-vais1000-medium.onnx'))
with wave.open(str(fixture / 'original.wav'), 'wb') as output:
    voice.synthesize_wav('Xin chào các bạn. Đây là bản thử nghiệm phục hồi phụ đề và lồng tiếng Việt.', output)
run([binary('ffmpeg'), '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=12:duration=8',
     '-i', fixture / 'original.wav', '-vf', "drawtext=fontfile='C\\:/Windows/Fonts/arial.ttf':text='ORIGINAL SUBTITLE':fontcolor=white:fontsize=30:borderw=2:bordercolor=black:x=(w-tw)/2:y=h-65",
     '-t', '8', '-c:v', 'libx264', '-crf', '18', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-af', 'apad', '-movflags', '+faststart', fixture / 'demo.mp4'])
print(fixture / 'demo.mp4')
