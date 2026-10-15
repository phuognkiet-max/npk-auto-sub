"""Conservative local disk forecast, not a promised encoded file size."""
import shutil
import time
import math
from core import DATA


def estimate_space(project, kind):
    media = project.get('media', {})
    seconds = float(media.get('duration', 0))
    if kind == 'separate_audio':
        # Two float32 stereo stems: 4 bytes x 2 channels x 2 stems at 44.1 kHz.
        # Decoding/inference streams in memory; atomic rename adds no disk copy.
        duration = seconds if math.isfinite(seconds) and seconds > 0 else 0
        stems = int(duration * 44100 * 16) + 88
        overhead = max(64 * 1024**2, int(stems * .1))
        return {'required_bytes': stems + overhead + 512 * 1024**2,
                'free_bytes': shutil.disk_usage(DATA).free, 'estimated_output_bytes': stems,
                'note': 'Hai stem WAV float32 stereo 44,1 kHz, có dự phòng; khoảng 2,37 GiB mỗi giờ nguồn.'}
    if kind == 'text_to_speech':
        # Input lives in the private standalone job snapshot, not a video project.
        # Leave room for a slow read, native WAV, working audio and MP3 together.
        seconds = max(seconds, 10, len(str(project.get('speech_text', ''))) / 8)
    source_bytes = int(media.get('bytes', 0))
    params = project.get('params', {})
    voice_ids = [params.get('voice_id', '')] + [row.get('voice_id', '') for row in project.get('segments', [])]
    # Forecast native VieNeu audio without loading a model. Old IDs are local
    # migration aliases only; they never connect to a companion application.
    sample_rate = 48000 if any(str(v).startswith(('vieneu:', 'cit:')) for v in voice_ids) else 22050
    audio = seconds * sample_rate * 8
    video = max(source_bytes * 1.6, seconds * max(1280*720, media.get('width',0)*media.get('height',0)) * .08)
    working = video * (2 if kind=='pipeline' else 1) if kind in ('render','clean','pipeline') else audio
    return {'required_bytes': int(working + audio + 512*1024**2),
            'free_bytes': shutil.disk_usage(DATA).free, 'estimated_output_bytes': int(video),
            'note': 'Ước lượng có dự phòng; kích thước thực phụ thuộc nội dung và chất lượng.'}


class DiskGuard:
    def __init__(self,path,reserve=256*1024**2,clock=time.monotonic):
        self.path,self.reserve,self.clock,self.last=path,reserve,clock,float('-inf')

    def check(self):
        now=self.clock()
        if now-self.last<2:return
        self.last=now
        if shutil.disk_usage(self.path).free<self.reserve:
            raise RuntimeError('Ổ dữ liệu sắp đầy. Đã dừng xử lý để giữ dự án và phần đã lưu; giải phóng chỗ rồi tiếp tục hoặc chạy lại bước này.')
