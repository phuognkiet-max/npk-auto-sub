"""Original ROI inpainting implementation. Model: LaMa (Apache-2.0)."""
from __future__ import annotations
import cv2
import numpy as np
from performance import profile_for
from subtitle_text import sanitize_subtitle_text, subtitle_text_info

def bounds(region, width, height):
    x, y, w, h = [float(region[k]) for k in ("x", "y", "w", "h")]
    left = max(0, min(width, int(np.floor(x))))
    top = max(0, min(height, int(np.floor(y))))
    right = max(0, min(width, int(np.ceil(x + w))))
    bottom = max(0, min(height, int(np.ceil(y + h))))
    if right <= left or bottom <= top:
        raise ValueError("Vùng chọn rỗng hoặc nằm ngoài khung video.")
    return left, top, right, bottom

def make_mask(frame, regions, params, timestamp):
    h, w = frame.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    for region in regions:
        if timestamp < float(region.get("start", 0)) or timestamp >= float(region.get("end", 1e10)):
            continue
        x1, y1, x2, y2 = bounds(region, w, h)
        roi = frame[y1:y2, x1:x2]
        if params.get("mask_mode") == "glyph":
            hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            white = (hsv[:, :, 1] < 80) & (hsv[:, :, 2] > 175)
            yellow = (hsv[:, :, 0] >= 15) & (hsv[:, :, 0] <= 40) & (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 145)
            core = ((white | yellow) * 255).astype(np.uint8)
            count, labels, stats, _ = cv2.connectedComponentsWithStats(core)
            selected = np.zeros_like(core)
            for i in range(1, count):
                area = stats[i, cv2.CC_STAT_AREA]
                # Reject isolated noise and large flat background components.
                if 5 <= area <= max(100, core.size * 0.15):
                    selected[labels == i] = 255
            mask[y1:y2, x1:x2] |= selected
        else:
            mask[y1:y2, x1:x2] = 255
    radius = int(params.get("dilate", 4))
    if radius > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1))
        mask = cv2.dilate(mask, kernel)
    return mask


def stretch_source(y1, y2, height, strip, direction='above', offset=0):
    strip = max(1, min(int(strip), y2-y1))
    offset = max(0, int(offset))
    candidates = (y1-offset-strip, y2+offset)
    if direction == 'below':
        candidates = candidates[::-1]
    for y in candidates:
        if y >= 0 and y+strip <= height and (y+strip <= y1 or y >= y2):
            return y, strip
    raise ValueError('Không còn dải nền bên cạnh vùng ở khoảng cách đã chọn. Giảm độ dày/khoảng cách hoặc đổi phương pháp.')


def blend_effect(frame, result, mask, params):
    opacity = float(params.get('cover_opacity', 1))
    feather = int(params.get('edge_feather', 0))
    if opacity >= 1 and feather == 0:
        return result
    if opacity <= 0 or not mask.any():
        return frame.copy()
    ys, xs = np.where(mask > 0)
    x1,x2,y1,y2 = xs.min(),xs.max()+1,ys.min(),ys.max()+1
    roi_mask = mask[y1:y2,x1:x2]
    if feather:
        # Fade inside the selected edge; pad zeros even at image boundaries.
        padded = cv2.copyMakeBorder(roi_mask,1,1,1,1,cv2.BORDER_CONSTANT,value=0)
        distance = cv2.distanceTransform(padded,cv2.DIST_C,3)[1:-1,1:-1]
        alpha = np.minimum(1, np.maximum(0,distance-1)/feather) * opacity
    else:
        alpha = (roi_mask > 0).astype(np.float32) * opacity
    mixed = frame.copy()
    alpha = alpha[:,:,None]
    mixed[y1:y2,x1:x2] = np.rint(frame[y1:y2,x1:x2]*(1-alpha)+result[y1:y2,x1:x2]*alpha).clip(0,255).astype(np.uint8)
    return mixed

class Cleaner:
    def __init__(self, model_path, params, progress=lambda **kw: None):
        self.params = params
        self.progress = progress
        self.previous = None
        self.model = None
        if params.get("method", "lama") == "lama":
            import torch
            if not model_path.is_file():
                raise RuntimeError("Chưa có model LaMa. Chạy setup.ps1 trong thư mục VietDubStudio để tải model.")
            self.torch = torch
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            torch.set_num_threads(profile_for(params)['threads'])
            self.model = torch.jit.load(str(model_path), map_location=self.device).eval()
            progress(message=f"LaMa đã nạp · {self.device.upper()}")

    def predict(self, bgr, mask, max_side):
        oh, ow = bgr.shape[:2]
        scale = min(1.0, max_side / max(oh, ow))
        width, height = max(8, round(ow * scale)), max(8, round(oh * scale))
        image = cv2.resize(bgr, (width, height), interpolation=cv2.INTER_AREA)
        small_mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
        image = cv2.copyMakeBorder(image, 0, (-height) % 8, 0, (-width) % 8, cv2.BORDER_REFLECT)
        small_mask = cv2.copyMakeBorder(small_mask, 0, (-height) % 8, 0, (-width) % 8, cv2.BORDER_CONSTANT)
        torch = self.torch
        x = torch.from_numpy(np.ascontiguousarray(image[:, :, ::-1].transpose(2, 0, 1))).float().unsqueeze(0).to(self.device) / 255
        m = torch.from_numpy((small_mask > 0).astype(np.float32)[None, None]).to(self.device)
        with torch.inference_mode():
            output = self.model(x, m)
        if output.ndim != 4 or output.shape[0] != 1 or output.shape[1] != 3 or output.shape[2:] != x.shape[2:]:
            raise RuntimeError('Model phục hồi trả về kích thước ảnh không hợp lệ. Chưa dùng kết quả này.')
        if not torch.isfinite(output).all().item():
            raise RuntimeError('Model phục hồi trả về điểm ảnh không hợp lệ. Chưa dùng kết quả này.')
        rgb = output[0].permute(1, 2, 0).detach().cpu().numpy()[:height, :width]
        output_bgr = np.clip(rgb[:, :, ::-1] * 255, 0, 255).astype(np.uint8)
        return cv2.resize(output_bgr, (ow, oh), interpolation=cv2.INTER_CUBIC)

    def stabilize(self, original, result, mask):
        if not self.params.get('temporal', True):
            self.previous = None
            return result
        # Estimate motion at <=640px, rather than optical flow and float64
        # full-frame differences for every 1080p frame.
        height, width = original.shape[:2]
        scale = min(1., 640 / max(width, height))
        shape = (max(1, round(width * scale)), max(1, round(height * scale)))
        gray = cv2.cvtColor(cv2.resize(original, shape, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        if self.previous is not None:
            prev_gray, prev_result, prev_mask = self.previous
            excluded = cv2.resize(mask | prev_mask, shape, interpolation=cv2.INTER_NEAREST)
            permitted = cv2.bitwise_not(cv2.dilate(excluded, np.ones((7, 7), np.uint8)))
            points = cv2.goodFeaturesToTrack(prev_gray, 100, 0.03, 6, mask=permitted)
            if points is not None and len(points) >= 12:
                found, status, _ = cv2.calcOpticalFlowPyrLK(prev_gray, gray, points, None, maxLevel=2)
                valid = status[:, 0] == 1
                if valid.sum() >= 10:
                    matrix, inliers = cv2.estimateAffinePartial2D(points[valid], found[valid], method=cv2.RANSAC)
                    if matrix is not None and inliers is not None and inliers.mean() > 0.6:
                        warped_gray = cv2.warpAffine(prev_gray, matrix, shape)
                        context = permitted > 0
                        difference = cv2.absdiff(gray, warped_gray)
                        if context.any() and np.median(difference[context]) < 12:
                            scaling = np.diag([shape[0] / width, shape[1] / height, 1.])
                            full_matrix = (np.linalg.inv(scaling) @ np.vstack([matrix, [0, 0, 1]]) @ scaling)[:2]
                            warped = cv2.warpAffine(prev_result, full_matrix, (width, height))
                            warped_mask = cv2.warpAffine(prev_mask, full_matrix, (width, height), flags=cv2.INTER_NEAREST)
                            overlap = (mask > 0) & (warped_mask > 0)
                            result[overlap] = (result[overlap] * 0.8 + warped[overlap] * 0.2).astype(np.uint8)
        self.previous = (gray, result.copy(), mask.copy())
        return result

    def __call__(self, frame, regions, timestamp):
        if self.params.get('method') in ('box', 'blur', 'stretch'):
            mask = np.zeros(frame.shape[:2], dtype=np.uint8)
            result = frame.copy()
            for region in regions:
                if not float(region.get('start',0)) <= timestamp < float(region.get('end',1e10)):
                    continue
                x1,y1,x2,y2 = bounds(region, frame.shape[1], frame.shape[0])
                pad = int(self.params.get('dilate',4))
                x1,y1,x2,y2 = max(0,x1-pad),max(0,y1-pad),min(frame.shape[1],x2+pad),min(frame.shape[0],y2+pad)
                mask[y1:y2,x1:x2] = 255
                method = self.params['method']
                if method == 'box':
                    color = self.params.get('cover_color','#000000').lstrip('#')
                    result[y1:y2,x1:x2] = tuple(int(color[i:i+2],16) for i in (4,2,0))
                elif method == 'blur':
                    radius = max(1,min(int(self.params.get('blur_radius',16)),(x2-x1-1)//2,(y2-y1-1)//2))*2+1
                    patch = frame[y1:y2,x1:x2]
                    for _ in range(3):
                        patch = cv2.blur(patch, (radius,radius))
                    result[y1:y2,x1:x2] = patch
                else:
                    source_y, strip = stretch_source(y1,y2,frame.shape[0],self.params.get('stretch_height',12),
                        self.params.get('stretch_direction','above'),self.params.get('stretch_offset',0))
                    result[y1:y2,x1:x2] = cv2.resize(frame[source_y:source_y+strip,x1:x2],(x2-x1,y2-y1))
            return blend_effect(frame,result,mask,self.params), mask
        mask = make_mask(frame, regions, self.params, timestamp)
        if not mask.any() or float(self.params.get('cover_opacity',1)) <= 0:
            self.previous = None
            return frame.copy(), mask
        ys, xs = np.where(mask > 0)
        context = int(self.params.get("context", 96))
        x1, x2 = max(0, int(xs.min()) - context), min(frame.shape[1], int(xs.max()) + context + 1)
        y1, y2 = max(0, int(ys.min()) - context), min(frame.shape[0], int(ys.max()) + context + 1)
        roi, roi_mask = frame[y1:y2, x1:x2], mask[y1:y2, x1:x2]
        if self.model is None:
            restored = cv2.inpaint(roi, roi_mask, float(self.params.get('inpaint_radius',5)), cv2.INPAINT_TELEA)
        else:
            max_side = int(self.params.get("max_side", 768))
            try:
                restored = self.predict(roi, roi_mask, max_side)
            except self.torch.cuda.OutOfMemoryError:
                self.torch.cuda.empty_cache()
                raise RuntimeError('Card đồ họa không đủ bộ nhớ ở độ chi tiết đã chọn. Giảm giới hạn ảnh xử lý hoặc thu hẹp vùng rồi xem thử lại. Không tự giảm chất lượng bản xuất.') from None
        # Every selected pixel must come from the reconstruction, never a blend
        # with the original subtitle. Outside the mask is bit-for-bit untouched.
        result = frame.copy()
        result[y1:y2, x1:x2][roi_mask > 0] = restored[roi_mask > 0]
        result = self.stabilize(frame, result, mask)
        return blend_effect(frame,result,mask,self.params), mask
