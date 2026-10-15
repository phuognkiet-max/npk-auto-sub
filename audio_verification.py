"""Read complete PCM16 speech payloads before a job can publish them.

WAV headers describe intended frames; they are not proof that those bytes were
written. Reads stay bounded even when a damaged header declares a huge file.
UVR's float32 stems use their separate, stricter format contract.
"""
import array
import math
import sys
import wave


def verify_pcm_speech(path, *, expected_duration=None, expected_rate=None):
    try:
        with wave.open(str(path), 'rb') as audio:
            frames, rate = audio.getnframes(), audio.getframerate()
            channels, width = audio.getnchannels(), audio.getsampwidth()
            if frames <= 0 or rate <= 0 or channels not in (1, 2) or width != 2 or audio.getcomptype() != 'NONE':
                raise ValueError('Tệp giọng cần WAV PCM16 có dữ liệu âm thanh hợp lệ.')
            if expected_rate is not None and rate != expected_rate:
                raise ValueError('Tần số WAV không khớp bản giọng đã báo cáo.')
            if expected_duration is not None:
                duration = float(expected_duration)
                if not math.isfinite(duration) or duration <= 0 or abs(frames - round(duration * rate)) > 1:
                    raise ValueError('Thời lượng WAV không khớp toàn bộ khoảng giọng đã tạo.')
            remaining, peak = frames, 0
            frame_bytes = channels * width
            while remaining:
                block = audio.readframes(min(remaining, max(1, min(rate * 10, 262144))))
                if not block or len(block) % frame_bytes:
                    raise ValueError('WAV bị thiếu dữ liệu âm thanh; chưa công bố giọng bị cắt.')
                count = len(block) // frame_bytes
                if count > remaining:
                    raise ValueError('Dữ liệu WAV vượt số mẫu đã báo cáo.')
                remaining -= count
                samples = array.array('h', block)
                if sys.byteorder != 'little':
                    samples.byteswap()
                peak = max(peak, max(map(abs, samples), default=0))
            if peak < 3:
                raise ValueError('Tệp giọng hoàn toàn im lặng; chưa công bố hoàn tất.')
            return {'frames': frames, 'sample_rate': rate, 'channels': channels,
                    'duration': frames / rate, 'peak': peak}
    except (wave.Error, EOFError, OSError) as exc:
        raise ValueError('Không đọc được WAV hoàn chỉnh; kiểm tra bản giọng trước khi xuất.') from exc
