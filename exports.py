"""Publish a verified final video using exclusive creation; never overwrite source."""
from pathlib import Path
import re
import shutil
import os
import tempfile
from core import DATA


def publish_or_keep_local(project, folder, result):
    """A delivery error must not hide a verified, recoverable final video."""
    try:
        result['published'] = publish_verified(project, folder)
    except (OSError, ValueError):
        result['delivery_pending'] = True
        result.setdefault('warnings', []).append(
            'Video thành phẩm đã được giữ trong dự án, nhưng chưa chép được đến nơi xuất. '
            'Dùng Mở thư mục để lấy bản đã có; kiểm dung lượng/quyền ghi hoặc chọn nơi lưu khác. '
            'Không cần xử lý video lại.')
    return result


def publish_verified(project, folder, unique=True):
    p=project['params']
    directory=Path(p['export_directory']) if p.get('export_directory') else DATA/'exports'
    directory.mkdir(exist_ok=True)
    name=re.sub(r'[<>:"/\\|?*\x00-\x1f]','-',p.get('export_name','Video-thanh-pham')).strip(' .')[:120] or 'Video-thanh-pham'
    origin=Path(folder)/'final.mp4'
    if shutil.disk_usage(directory).free < origin.stat().st_size+128*1024**2:
        raise ValueError('Video đã xử lý xong trong thư mục tác vụ nhưng nơi xuất không đủ chỗ. Chọn nơi khác để lưu bản sao.')
    directory=directory.resolve(strict=True)
    handle, temporary=tempfile.mkstemp(prefix='.npk-export-',suffix='.partial',dir=directory)
    pending=Path(temporary).resolve()
    try:
        with os.fdopen(handle,'wb') as output,origin.open('rb') as source:
            shutil.copyfileobj(source,output,1024*1024)
            output.flush()
            os.fsync(output.fileno())
        for i in range(1,10001 if unique else 2):
            target=directory/(name+('' if i==1 else f'-{i}')+'.mp4')
            try:
                # Windows rename fails if the destination exists. Never replace.
                if os.name=='nt':os.rename(pending,target)
                else:os.link(pending,target)
                return {'path':str(target),'bytes':target.stat().st_size}
            except FileExistsError:
                continue
        raise ValueError('Tên video đã tồn tại. Chọn tên khác để giữ bản trước.')
    finally:
        if pending.parent==directory and pending.name.startswith('.npk-export-'):
            pending.unlink(missing_ok=True)
