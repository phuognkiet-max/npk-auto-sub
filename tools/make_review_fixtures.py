"""Create synthetic review fixtures; never reads user media or invokes TTS/API.

These tones are NOT Vietnamese speech and cannot validate ASR, pronunciation,
speaker identity, or end-to-end dubbing quality. Use for structural/media tests.
"""
import argparse
import array
import math
import os
from pathlib import Path
import shutil
import subprocess
import wave

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffmpeg', default=shutil.which('ffmpeg'))
    args = parser.parse_args()
    ffmpeg = args.ffmpeg
    if not ffmpeg:
        candidates = [ROOT/'runtime/ffmpeg/ffmpeg.exe', ROOT/'runtime/bin/ffmpeg.exe']
        ffmpeg = next((str(p) for p in candidates if p.is_file()), None)
    if not ffmpeg:
        parser.error('Install FFmpeg on PATH or pass --ffmpeg with its executable path.')
    directory = ROOT/'tests/fixtures'
    directory.mkdir(parents=True, exist_ok=True)
    audio, video = directory/'original.wav', directory/'demo.mp4'
    if audio.exists() or video.exists():
        parser.error('Fixture already exists. Preserve it or choose an empty clone; no overwrite.')
    rate = 48000
    signal = array.array('h', (int(6000*math.sin(2*math.pi*240*i/rate))
                              if .1 < (i/rate)%1 < .85 else 0 for i in range(rate*8)))
    with wave.open(str(audio), 'wb') as output:
        output.setparams((1,2,rate,0,'NONE','not compressed'))
        output.writeframes(signal.tobytes())
    font = Path(os.environ.get('WINDIR', 'C:/Windows'))/'Fonts/arial.ttf'
    draw = "drawtext=text='ORIGINAL SUBTITLE':fontcolor=white:fontsize=30:borderw=2:bordercolor=black:x=(w-tw)/2:y=h-65"
    if font.is_file():
        font_value = font.as_posix().replace(':', r'\:')
        draw += ":fontfile='"+font_value+"'"
    subprocess.run([str(ffmpeg),'-v','error','-n','-f','lavfi','-i',
                    'testsrc2=size=640x360:rate=12:duration=8','-i',str(audio),
                    '-vf',draw,'-t','8','-c:v','libx264','-crf','18','-pix_fmt','yuv420p',
                    '-c:a','aac','-movflags','+faststart',str(video)],check=True)
    print('Created synthetic 8-second color-pattern/tone fixtures in tests/fixtures.')


if __name__ == '__main__':
    main()
