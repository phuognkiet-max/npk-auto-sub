"""Small, cached hardware checks; never load an inference model to inspect memory."""
from functools import lru_cache
import csv
import io
import shutil
import subprocess
import time
import psutil
from core import NO_WINDOW

_cache = (0, None)


def hardware_info():
    global _cache
    if _cache[1] is not None and time.monotonic() - _cache[0] < 10:
        return _cache[1]
    ram = psutil.virtual_memory()
    result = {'ram_total_mb': round(ram.total / 1048576), 'ram_available_mb': round(ram.available / 1048576),
              'cpu_threads': psutil.cpu_count(logical=True), 'gpus': [], 'gpu_query_error': ''}
    executable = shutil.which('nvidia-smi')
    if executable:
        try:
            output = subprocess.run([executable, '--query-gpu=name,memory.total,memory.free,utilization.gpu',
                '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=4, creationflags=NO_WINDOW)
            if output.returncode:
                raise ValueError('Không đọc được trạng thái GPU.')
            for values in csv.reader(io.StringIO(output.stdout)):
                if len(values) == 4:
                    name, total, free, usage = [v.strip() for v in values]
                    result['gpus'].append({'name': name, 'total_mb': int(total), 'free_mb': int(free), 'usage': int(usage)})
        except (OSError, ValueError, subprocess.SubprocessError):
            result['gpu_query_error'] = 'Chưa đọc được VRAM; không tự thay thiết bị xử lý.'
    _cache = (time.monotonic(), result)
    return result


def inspect_asr(params, hardware=None):
    """Conservative advisory budgets, not promises about exact VRAM consumption."""
    hardware = hardware or hardware_info()
    model = params.get('asr_model', 'small')
    device = params.get('asr_device', 'cuda')
    compute = params.get('asr_compute', 'auto')
    errors, warnings = [], []
    if device == 'cpu' and compute in ('float16', 'int8_float16'):
        errors.append('Whisper CPU không dùng float16. Chọn auto hoặc int8.')
    if device == 'cuda':
        gpus = hardware.get('gpus', [])
        # Budgets include runtime headroom; the selected compute precision is explicit.
        estimate = {'tiny': 550, 'base': 700, 'small': 1700, 'medium': 3900, 'large-v3': 6500}.get(model, 1700)
        if compute in ('int8', 'int8_float16'):
            estimate = round(estimate * .65 + 150)
        if gpus:
            gpu = gpus[0]
            if estimate > gpu['total_mb']:
                errors.append(f'Whisper {model} cần dự phòng khoảng {estimate} MiB VRAM; GPU chỉ có {gpu["total_mb"]} MiB. '
                              'Chọn model nhỏ hơn, int8 hoặc CPU trước khi chạy.')
            elif estimate > gpu['free_mb']:
                warnings.append(f'VRAM trống {gpu["free_mb"]} MiB thấp hơn mức dự phòng {estimate} MiB cho Whisper {model}. '
                                'Đóng tác vụ GPU khác để giảm nguy cơ hết bộ nhớ.')
        else:
            warnings.append('Chưa đọc được VRAM. Whisper sẽ báo lỗi rõ nếu CUDA không sẵn sàng; không tự đổi sang CPU.')
    if hardware.get('ram_available_mb', 0) < 1500:
        warnings.append('RAM trống dưới 1,5 GiB. Nên đóng ứng dụng nặng trước khi nhận dạng.')
    return {'errors': errors, 'warnings': warnings}
