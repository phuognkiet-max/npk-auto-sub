"""Durable, source-bound ASR prefixes on the original video's audio clock."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path

MAX_BYTES=32*1024*1024
MAX_ROWS=10000
VERSION=1
INVALID='Checkpoint lời thoại không hợp lệ; giữ phần đã nhận dạng để khôi phục.'


def _number(value):
    if type(value) not in (int,float) or not math.isfinite(value):
        raise ValueError(INVALID)
    return float(value)


def scope(project):
    from audio_inputs import source_audio_span
    p,media=project['params'],project['media']
    if not media.get('audio'):
        raise ValueError('Nguồn không có âm thanh để tiếp tục nhận dạng lời thoại.')
    start=_number(p.get('extraction_start',0))
    end=min(_number(media['duration']),_number(p.get('extraction_end') or media['duration']))
    audio_start,audio_end=source_audio_span(media)
    if start<0 or max(start,audio_start)>=min(end,audio_end):
        raise ValueError('Khoảng nhận dạng lời thoại không có âm thanh nguồn.')
    return start,min(end,audio_end)


def signature(project):
    from subtitle_quality import digest,source_signature
    p,media=project['params'],project['media']
    start,end=scope(project)
    return digest({'version':VERSION,'source':source_signature(project),
        'source_revision':project.get('source_revision'), 'scope':[start,end],
        'audio_clock':[media.get('audio_start_time'),media.get('audio_duration')],
        'language':p.get('source_language','auto'),'model':p.get('asr_model','small'),
        'device':p.get('asr_device','cuda'),'compute':p.get('asr_compute','auto'),
        'vad_filter':True,'word_timestamps':True,'beam_size':5})


def _validate(document,project):
    if (not isinstance(document,dict) or type(document.get('version')) is not int or document['version']!=VERSION or
            document.get('signature')!=signature(project)):
        raise ValueError('Checkpoint lời thoại không khớp nguồn, model, ngôn ngữ hoặc phạm vi hiện tại.')
    start,end=scope(project)
    begin,finish=_number(document.get('start')),_number(document.get('end'))
    confirmed=_number(document.get('confirmed_until'))
    # A refreshed probe can establish an earlier real audio EOF for legacy
    # source metadata, but a checkpoint cannot claim a later requested span.
    if abs(begin-start)>1e-6 or not begin<finish<=end+1e-5 or not begin<=confirmed<=finish+1e-5:
        raise ValueError(INVALID)
    complete=document.get('recognition_complete',False)
    if type(complete) is not bool or (complete and abs(confirmed-finish)>1e-5):
        raise ValueError(INVALID)
    rows=document.get('rows')
    if not isinstance(rows,list) or len(rows)>MAX_ROWS:
        raise ValueError(INVALID)
    previous_end=begin
    for row in rows:
        if not isinstance(row,dict):
            raise ValueError(INVALID)
        a,b=_number(row.get('start')),_number(row.get('end'))
        text=row.get('text')
        if a<previous_end-1e-5 or b<=a or b>confirmed+1e-5 or not isinstance(text,str) or not text.strip() or '\x00' in text:
            raise ValueError(INVALID)
        if not isinstance(row.get('translated',''),str):
            raise ValueError(INVALID)
        previous_word=a
        words=row.get('words',[])
        if not isinstance(words,list) or len(words)>10000:
            raise ValueError(INVALID)
        for word in words:
            if not isinstance(word,dict) or not isinstance(word.get('word'),str) or not word['word'].strip():
                raise ValueError(INVALID)
            wa,wb=_number(word.get('start')),_number(word.get('end'))
            if wa<previous_word-1e-5 or wb<=wa or wb>b+1e-5 or wa<a-1e-5:
                raise ValueError(INVALID)
            if 'probability' in word and not 0<=_number(word['probability'])<=1:
                raise ValueError(INVALID)
            previous_word=wb
        previous_end=b
    if rows and confirmed<previous_end-1e-5:
        raise ValueError(INVALID)
    if not isinstance(document.get('language',''),str):
        raise ValueError(INVALID)
    return document


def _object(pairs):
    result={}
    for key,value in pairs:
        if key in result:
            raise ValueError(INVALID)
        result[key]=value
    return result


def validate_resume(path,project):
    if not Path(project['media']['path']).is_file():
        raise ValueError('Không tìm thấy video nguồn của checkpoint lời thoại.')
    try:
        with Path(path).open('rb') as stream:
            encoded=stream.read(MAX_BYTES+1)
        if len(encoded)>MAX_BYTES:
            raise ValueError('Checkpoint lời thoại vượt giới hạn an toàn; không tự nhận dạng lại từ đầu.')
        document=json.loads(encoded.decode('utf-8'),object_pairs_hook=_object)
        # Reject nonfinite/deep optional metadata as well as timeline fields.
        from ocr_checkpoint import _finite_tree
        try:
            _finite_tree(document)
        except ValueError as exc:
            raise ValueError(INVALID) from exc
        valid=_validate(document,project)
        if not valid['recognition_complete'] and valid['confirmed_until']>=valid['end']-1e-5:
            raise ValueError('Checkpoint lời thoại đã nhận tới cuối âm thanh nhưng chưa xác nhận Whisper kết thúc. '
                             'Giữ các câu đã lưu; không tự nhận dạng lại từ đầu.')
        return valid
    except (OSError,UnicodeError,json.JSONDecodeError,RecursionError,KeyError,TypeError,OverflowError) as exc:
        raise ValueError(INVALID) from exc


def write_checkpoint(path,project,rows,start,end,confirmed_until,*,language='',complete=False):
    document={'version':VERSION,'signature':signature(project),'rows':copy.deepcopy(rows),
              'start':float(start),'end':float(end),'confirmed_until':float(confirmed_until),
              'language':str(language),'recognition_complete':bool(complete)}
    _validate(document,project)
    encoded=json.dumps(document,ensure_ascii=False,allow_nan=False).encode('utf-8')
    if len(encoded)>MAX_BYTES:
        raise ValueError('Checkpoint lời thoại vượt giới hạn; phần đã lưu vẫn được giữ.')
    path=Path(path)
    temporary=path.with_suffix(path.suffix+'.tmp')
    try:
        with temporary.open('wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if temporary.is_file():
            temporary.unlink()
    return document
