"""Worker-local load controls. Never change Windows power plans or GPU clocks."""
import os
import time

PROFILES = {
    'quiet': {'label': 'Êm / giảm tải', 'threads': 1, 'duty': .60},
    'balanced': {'label': 'Cân bằng', 'threads': 2, 'duty': .85},
    'fast': {'label': 'Nhanh', 'threads': 4, 'duty': 1.0},
}

def profile_for(params):
    return PROFILES.get(params.get('workload_mode', 'quiet'), PROFILES['quiet'])

def configure_compute(params):
    profile = profile_for(params)
    for name in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        os.environ[name] = str(profile['threads'])
    os.environ['OPENCV_FFMPEG_THREADS'] = str(profile['threads'])
    import cv2
    cv2.setNumThreads(profile['threads'])
    return profile

def create_ocr(params):
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR(intra_op_num_threads=profile_for(params)['threads'], inter_op_num_threads=1,
                    det_limit_type='min' if params.get('ocr_scale_mode')=='legacy' else 'max')

def ffmpeg_limits(params):
    threads = str(profile_for(params)['threads'])
    return ['-threads', threads, '-filter_threads', threads, '-filter_complex_threads', threads]

class FramePacer:
    """Add idle time, without dropping frames or reducing image resolution."""
    def __init__(self, params, clock=time.perf_counter, sleeper=time.sleep):
        self.duty = profile_for(params)['duty']
        self.clock, self.sleeper = clock, sleeper

    def rest(self, started):
        elapsed = max(0, self.clock() - started)
        delay = elapsed * (1 / self.duty - 1)
        if delay > .001:
            self.sleeper(delay)
        return delay
