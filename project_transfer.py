"""Portable edit documents: no keys, models, job paths or executable content."""
import copy
from core import DEFAULTS


def export_document(project):
    params={k:copy.deepcopy(v) for k,v in project['params'].items() if k in DEFAULTS}
    for key in ('export_directory','voice_path','bgm_path'):params[key]=''
    return {'format':'npk-project','version':1,'name':project['name'],
        'media':{k:project['media'].get(k) for k in ('width','height','duration','fps')},
        'params':params,'segments':copy.deepcopy(project['segments']),
        'regions':copy.deepcopy(project['regions']),
        'extraction_regions':copy.deepcopy(project.get('extraction_regions',project['regions'])),
        'note':'Bản chỉnh sửa không kèm video, âm thanh ngoài, model, khóa hay tệp xử lý. Chọn lại nguồn khi khôi phục.'}


def restore_document(document, source):
    import server
    from core import create_project, save_project_version, probe
    if not isinstance(document,dict) or document.get('format')!='npk-project' or document.get('version')!=1:
        raise ValueError('Không phải bản sao dự án NPK được hỗ trợ.')
    from pathlib import Path
    if not Path(source).is_absolute() or str(source).startswith(('\\\\','//')):
        raise ValueError('Chọn video nguồn trên máy để khôi phục dự án.')
    media=probe(source)
    original=document.get('media',{})
    if abs(media['duration']-float(original.get('duration',0)))>.2 or any(media[k]!=original.get(k) for k in ('width','height')):
        raise ValueError('Video được chọn khác thời lượng hoặc kích thước của bản sao dự án.')
    # A portable document produced before natural timing existed keeps the
    # original behavior, just like an old project loaded from the local DB.
    params={'tts_timing_mode':'legacy'} | dict(document.get('params',{}))
    for key in ('export_directory','voice_path','bgm_path'):params[key]=''
    params=server.validate_params(params)
    rows=server.validate_segments(document.get('segments',[]),media['duration'])
    # Validate every field on a not-yet-persisted document first.
    regions=server.validate_regions(document.get('regions',[]),media)
    reading=server.validate_regions(document.get('extraction_regions',[]),media)
    project=create_project(source,str(document.get('name','Dự án khôi phục'))[:200]+' — khôi phục')
    from core import restored_params
    project.update(params=restored_params(params),segments=rows,regions=regions,extraction_regions=reading,artifacts={})
    return save_project_version(project)
