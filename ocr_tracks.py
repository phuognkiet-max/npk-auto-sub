"""Spatial OCR evidence and conservative subtitle-track selection.

This module does not classify semantic content or erase watermarks by name.
Every detection is retained in a separate observation journal. Geometry and
temporal continuity propose a caption band; uncertain selection is explicitly
review-required. A text change is a new run, however similar the strings are.
"""
from __future__ import annotations

import copy
import math
import json
from pathlib import Path
import unicodedata

from video_sampling import deduplicated_boxes, grouped_lines, reading_order

VERSION = 1
SELECTION_POLICY = 'sustained-caption-band-v2'


def selection_quality_issues(selection):
    """User-facing issues retain warning/review severity instead of dumping JSON.

    ``requires_review`` summarizes unresolved choice; it does not promote a
    nonselected document warning when a different caption band is ambiguous.
    """
    if not isinstance(selection,dict):
        return []
    messages={
        'uncertain_caption_band':'Chưa xác định chắc lớp phụ đề cần đọc. Xem mẫu và chọn lớp chữ hoặc vùng đọc cụ thể.',
        'competing_caption_band':'Có nhiều lớp chữ có thể là phụ đề. Chọn lớp cần dịch hoặc đọc; các lớp khác vẫn được giữ để đối chiếu.',
        'unselected_temporal_band':'Có lớp chữ xuất hiện ở một khoảng thời gian riêng chưa được chọn. Kiểm tra xem phụ đề có đổi vị trí hay đó là chữ nền.',
        'overlapping_text_variants':'Các vùng đọc chồng nhau nhận được chữ khác nhau cho cùng vị trí. Xem mẫu và chỉnh vùng hoặc sửa nội dung; chưa tự bỏ bản nào.',
        'legacy_untracked_prefix':'Phần nhận dạng cũ chưa có bằng chứng từng lớp chữ. Các câu đã lưu vẫn được giữ; nhận dạng lại nếu cần chọn lớp cho toàn video.',
        'no_caption_band':'Chưa chọn được lớp phụ đề từ chữ trên hình. Chọn lớp cần dùng, vẽ vùng đọc cụ thể hoặc nhập tệp phụ đề.',
        'persistent_text_band':'Lớp chữ chính ít thay đổi trong thời gian dài; chưa đủ bằng chứng để xác định đây là phụ đề hay chữ nền.',
    }
    issues=[]
    raw=selection.get('issues')
    for item in raw if isinstance(raw,list) else []:
        if isinstance(item,dict):
            code=item.get('code','unknown')
            severity=item.get('severity')
            if severity not in ('review','warning'):
                severity='review' if selection.get('requires_review') else 'warning'
            if code=='multiline_text_block':
                message=('Có đoạn chữ nhiều dòng chưa xác định là lời cần đọc. Chọn lớp cụ thể trước khi dịch hoặc lồng tiếng.'
                         if severity=='review' else
                         'Có đoạn chữ nhiều dòng ngoài lớp phụ đề đã chọn. Bằng chứng được giữ; đoạn này không được đưa vào lời đọc.')
            else:
                message=str(item.get('message') or messages.get(code) or
                    'Có lớp chữ cần đối chiếu với hình nguồn trước khi dùng làm phụ đề hoặc lời đọc.')
        else:
            code,severity='selection_unknown','review' if selection.get('requires_review') else 'warning'
            message=str(item)
        issues.append({'code':'ocr_'+str(code),'message':message,'severity':severity})
    if selection.get('requires_review') and not any(item['severity']=='review' for item in issues):
        issues.append({'code':'ocr_track_selection','severity':'review',
                       'message':'Chưa xác định rõ lớp chữ cần dùng. Chọn lớp phụ đề và đối chiếu với video trước khi lồng tiếng.'})
    return issues


def _validate_state(state,width,height):
    """Reject malformed evidence before it can change a resumed caption corpus."""
    invalid='Trạng thái lớp OCR của checkpoint không hợp lệ; giữ dữ liệu để khôi phục.'
    def integer(value,minimum=0):
        if type(value) is not int or value < minimum:
            raise ValueError(invalid)
        return value
    def number(value):
        if type(value) not in (int,float) or not math.isfinite(value):
            raise ValueError(invalid)
        return value
    def geometry(box):
        if not isinstance(box,list) or len(box)!=4:
            raise ValueError(invalid)
        x,y,right,bottom=[number(value) for value in box]
        if not 0 <= x < right <= width+1e-5 or not 0 <= y < bottom <= height+1e-5:
            raise ValueError(invalid)
    if not isinstance(state,dict) or type(state.get('version')) is not int or state['version']!=VERSION:
        raise ValueError(invalid)
    samples=integer(state.get('samples_processed'))
    integer(state.get('evidence_bytes'))
    next_id=integer(state.get('next_id'),1)
    tracks=state.get('tracks')
    if not isinstance(tracks,list) or len(tracks)>10000 or next_id!=len(tracks)+1:
        raise ValueError(invalid)
    identifiers=set()
    for track in tracks:
        if not isinstance(track,dict) or not isinstance(track.get('id'),str) or track['id'] in identifiers:
            raise ValueError(invalid)
        identifiers.add(track['id'])
        if track['id']!=f'ocr-track-{len(identifiers)}':
            raise ValueError(invalid)
        geometry(track.get('bbox'))
        if not 0 < number(track.get('line_height')) <= height:
            raise ValueError(invalid)
        integer(track.get('max_lines'),1)
        count=integer(track.get('samples'),1)
        if count>samples or not isinstance(track.get('regions'),list) or not track['regions'] or any(
                not isinstance(region,str) for region in track['regions']):
            raise ValueError(invalid)
        runs=track.get('runs')
        if not isinstance(runs,list) or not runs or len(runs)>count:
            raise ValueError(invalid)
        end,previous_sample=0,-1
        for run in runs:
            if not isinstance(run,dict):
                raise ValueError(invalid)
            start,finish=number(run.get('start')),number(run.get('end'))
            first,last=integer(run.get('first_sample')),integer(run.get('last_sample'))
            if start < end-1e-6 or finish <= start or first <= previous_sample or not first <= last < samples:
                raise ValueError(invalid)
            if not isinstance(run.get('text'),str) or not run['text'].strip() or '\x00' in run['text']:
                raise ValueError(invalid)
            geometry(run.get('bbox'))
            end,previous_sample=finish,last
    if not isinstance(state.get('legacy_prefix'),list):
        raise ValueError(invalid)


def _text_key(text):
    # Punctuation, diacritics, names, numeric formatting and line breaks remain
    # meaningful. Only Unicode composition and surrounding whitespace normalize.
    return '\n'.join(' '.join(line.split()) for line in
                     unicodedata.normalize('NFC', str(text)).strip().splitlines())


def _union(boxes):
    return [min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes)]


def _overlap(a, b):
    return max(0, min(a[2], b[2])-max(a[0], b[0]))*max(0, min(a[3], b[3])-max(a[1], b[1]))


def _temporal_overlap(first, second):
    a, b = 0, 0
    while a < len(first) and b < len(second):
        if first[a]['start'] < second[b]['end'] and second[b]['start'] < first[a]['end']:
            return True
        if first[a]['end'] <= second[b]['start']:
            a += 1
        else:
            b += 1
    return False


def _merged_intervals(intervals):
    result=[]
    for start,end in sorted(intervals):
        if result and start<=result[-1][1]+1e-6:
            result[-1][1]=max(end,result[-1][1])
        else:
            result.append([start,end])
    return result


def _weighted_middle(values):
    total=sum(weight for _,weight in values)
    done=0
    for value,weight in sorted(values):
        done+=weight
        if done>=total/2:
            return value
    return 0.


def _track_metrics(track,width,height,interval):
    """Use all observed runs: the final smoothed box is not a video's evidence."""
    runs=track['runs']
    weights=[max(1,run['last_sample']-run['first_sample']+1) for run in runs]
    med=lambda values:_weighted_middle(list(zip(values,weights)))
    boxes=[run['bbox'] for run in runs]
    centres=[(box[0]+box[2])/(2*width) for box in boxes]
    line_heights=[(box[3]-box[1])/max(1,len(run['text'].splitlines()))
                  for box,run in zip(boxes,runs)]
    ranges=_merged_intervals((run['start'],run['end']) for run in runs)
    duration=sum(end-start for start,end in ranges)
    by_text={}
    for run in runs:
        key=_text_key(run['text'])
        by_text[key]=by_text.get(key,0.)+run['end']-run['start']
    distinct=len(by_text)
    longest=max(by_text.values(),default=0.)
    # This is a geometry/temporal hint, never a semantic fact. A static quote
    # can look like a watermark, so it remains a candidate requiring review.
    persistent=(duration>=max(8.,interval*10) and distinct<=2 and
                longest/max(duration,1e-6)>=.9)
    return {'track':track,'cx':med(centres),'cy':med([(b[1]+b[3])/(2*height) for b in boxes]),
            'bottom':med([b[3]/height for b in boxes]),'width':med([(b[2]-b[0])/width for b in boxes]),
            'line_height':med(line_heights),'duration':duration,'distinct':distinct,
            'persistent':persistent,'ranges':ranges,'bbox':[med([b[i] for b in boxes]) for i in range(4)]}


def _same_caption_slot(first,second,width,height):
    """Join fragmentation after width/font changes, without merging two overlays.

    Coexisting distinct tracks remain separate candidates. Their text cannot be
    declared redundant from geometry alone. This also keeps a document fragment
    or persistent side watermark out of a continuously observed caption band.
    """
    if first['persistent'] or second['persistent']:
        return False
    if _temporal_overlap(first['track']['runs'],second['track']['runs']):
        return False
    a,b=first['line_height'],second['line_height']
    return (.65<=a/max(1,b)<=1/.65 and
            abs(first['bottom']-second['bottom'])*height<=max(.6*max(a,b),.012*height) and
            abs(first['cy']-second['cy'])*height<=max(a,b) and
            abs(first['cx']-second['cx'])*width<=.32*width)


def _caption_candidates(tracks,width,height,interval,samples_processed):
    metrics=[_track_metrics(track,width,height,interval) for track in tracks]
    # A strong, sustained track anchors each group. Avoid chain-merging between
    # increasingly different baselines; every new member must fit the anchor.
    metrics.sort(key=lambda item:(-item['track']['samples'],item['track']['id']))
    groups=[]
    for item in metrics:
        if item['track']['max_lines']>3:
            groups.append([item])
            continue
        group=next((group for group in groups if group[0]['track']['max_lines']<=3 and
                    _same_caption_slot(group[0],item,width,height) and
                    all(not _temporal_overlap(member['track']['runs'],item['track']['runs'])
                        for member in group)),None)
        if group is None:
            groups.append([item])
        else:
            group.append(item)
    candidates=[]
    for group in groups:
        anchor=group[0]
        runs=[(item['track']['id'],index,run) for item in group for index,run in enumerate(item['track']['runs'])]
        timed=sorted(runs,key=lambda value:(value[2]['start'],value[0],value[1]))
        samples=_merged_intervals((run['first_sample'],run['last_sample']+1) for _,_,run in runs)
        support=sum(end-start for start,end in samples)
        intervals=_merged_intervals((run['start'],run['end']) for _,_,run in runs)
        duration=sum(end-start for start,end in intervals)
        fraction=min(1.,support/max(1,samples_processed))
        centred=max(0.,1-abs(anchor['cx']-.5)/.5)
        geometry=3*centred+2*anchor['cy']+anchor['width']
        role='document' if any(item['track']['max_lines']>3 for item in group) else (
            'persistent_overlay_candidate' if anchor['persistent'] else 'caption_candidate')
        score=(9*fraction+math.log1p(support)+geometry+
               min(1.,len({_text_key(run['text']) for _,_,run in runs})/4))
        if role=='persistent_overlay_candidate':
            score-=4.
        if role=='document':
            score-=20.
        # Sample throughout the track rather than only the last frame or a long
        # document page. Content truncation here affects summaries only, never
        # OCR evidence or the rows selected for translation/speech.
        sample_indexes=sorted({0,len(timed)//3,2*len(timed)//3,len(timed)-1})
        content=[]
        for index in sample_indexes:
            identifier,run_index,run=timed[index]
            content.append({'run_id':f'{identifier}:run-{run_index}','start':run['start'],
                            'end':run['end'],'bbox':run['bbox'],'text':run['text'][:600],
                            'text_truncated':len(run['text'])>600})
        reasons=['temporal_support','observed_geometry','changing_text']
        if len(group)>1:
            reasons.append('nonconcurrent_same_caption_slot')
        if role!='caption_candidate':
            reasons.append(role)
        candidates.append({'band_id':anchor['track']['id'],'track_ids':[item['track']['id'] for item in group],
            'score':round(score,6),'sample_support':support,'sample_fraction':round(fraction,6),
            'observed_duration':round(duration,6),'start':intervals[0][0],'end':intervals[-1][1],
            'max_lines':max(item['track']['max_lines'] for item in group),
            'geometry':{'bbox':anchor['bbox'],'center_x':anchor['cx'],'center_y':anchor['cy'],
                        'bottom':anchor['bottom'],'width_fraction':anchor['width'],
                        'line_height':anchor['line_height']},
            'confidence':'strong' if fraction>=.5 and centred>=.7 and role=='caption_candidate' else 'provisional',
            'role_hint':role,'reasons':reasons,'text_samples':content,'intervals':intervals,
            'changing_runs':len({_text_key(run['text']) for _,_,run in runs})})
    candidates.sort(key=lambda item:(-item['score'],item['band_id']))
    return candidates


def frame_components(observations, width, confidence):
    """Baseline fragments and coherent nearby lines, without whole-frame join."""
    accepted = [item for item in observations if item['confidence'] >= confidence and item['text'].strip()]
    boxes = deduplicated_boxes([(*item['bbox'], item['text']) for item in accepted])
    fragments = []
    for line in grouped_lines(boxes):
        row, previous = [], None
        for box in sorted(line['boxes']):
            if previous and box[0]-previous[2] > max(2*line['h'], .04*width):
                fragments.append(row)
                row = []
            row.append(box)
            previous = box
        if row:
            fragments.append(row)
    components = []
    for fragment in sorted(fragments, key=lambda boxes: (_union(boxes)[1], _union(boxes)[0])):
        bbox = _union(fragment)
        line_height = max(1, sum(box[3]-box[1] for box in fragment)/len(fragment))
        target = None
        for component in reversed(components):
            previous = component['bbox']
            horizontal = max(0, min(bbox[2], previous[2])-max(bbox[0], previous[0]))
            if (0 <= bbox[1]-previous[3] <= .8*max(line_height,component['line_height']) and
                    .65 <= line_height/component['line_height'] <= 1/.65 and
                    horizontal >= .35*min(bbox[2]-bbox[0],previous[2]-previous[0])):
                target = component
                break
        if target is None:
            target = {'boxes': [], 'lines': 0, 'line_height': line_height}
            components.append(target)
        target['boxes'].extend(fragment)
        target['lines'] += 1
        target['bbox'] = _union(target['boxes'])
    for component in components:
        component['text'] = reading_order(component['boxes'])
        matching = [item for item in accepted if any(
            _text_key(item['text']) == _text_key(box[4]) and _overlap(item['bbox'],box[:4]) >=
            .8*min((item['bbox'][2]-item['bbox'][0])*(item['bbox'][3]-item['bbox'][1]),
                   (box[2]-box[0])*(box[3]-box[1])) for box in component['boxes'])]
        component['observation_ids'] = [item['id'] for item in matching]
        component['regions'] = sorted({item['region_id'] for item in matching})
        component['confidence'] = sum(item['confidence'] for item in matching)/max(1,len(matching))
        component['overlapping_variants'] = []
        for index,first in enumerate(matching):
            for second in matching[index+1:]:
                a,b=first['bbox'],second['bbox']
                area_a,area_b=(a[2]-a[0])*(a[3]-a[1]),(b[2]-b[0])*(b[3]-b[1])
                if (_text_key(first['text']) != _text_key(second['text']) and
                        _overlap(a,b) >= .85*min(area_a,area_b) and
                        .7 <= (a[3]-a[1])/max(1,b[3]-b[1]) <= 1/.7):
                    component['overlapping_variants'].append([first['id'],second['id']])
        component.pop('boxes')
    return components


class OCRTrackAssembler:
    """Track candidate caption bands and retain every text version with timing."""
    def __init__(self, width, height, interval, confidence=.55, explicit_regions=False, state=None):
        self.width, self.height = float(width), float(height)
        self.interval, self.confidence = float(interval), float(confidence)
        self.explicit_regions = bool(explicit_regions)
        self.state = copy.deepcopy(state) if state else {
            'version': VERSION, 'tracks': [], 'next_id': 1, 'samples_processed': 0,
            'legacy_prefix': [], 'evidence_bytes': 0}
        if self.state.get('version') != VERSION:
            raise ValueError('Phiên bản lớp OCR không tương thích; giữ checkpoint để đối chiếu.')
        if state:
            _validate_state(self.state,self.width,self.height)

    def observe(self, timestamp, observations, range_end):
        components = frame_components(observations, self.width, self.confidence)
        used = set()
        for component in components:
            bbox, line_height = component['bbox'], component['line_height']
            centre = [(bbox[0]+bbox[2])/2,(bbox[1]+bbox[3])/2]
            matches = []
            for track in self.state['tracks']:
                if track['id'] in used or bool(track['max_lines'] > 3) != bool(component['lines'] > 3):
                    continue
                old = track['bbox']
                if not set(track['regions']).intersection(component['regions']):
                    continue
                dx, dy = abs(centre[0]-(old[0]+old[2])/2), abs(centre[1]-(old[1]+old[3])/2)
                if (.5 <= line_height/track['line_height'] <= 2 and
                        dy <= max(line_height,track['line_height'])*1.15 and
                        dx <= max(.15*self.width, (bbox[2]-bbox[0]+old[2]-old[0])*.3)):
                    matches.append((dy+dx*.05, track))
            if matches:
                track = min(matches,key=lambda pair:pair[0])[1]
            else:
                track = {'id':f'ocr-track-{self.state["next_id"]}', 'bbox':bbox,
                         'line_height':line_height, 'regions':component['regions'],
                         'samples':0, 'max_lines':0, 'runs':[]}
                self.state['tracks'].append(track)
                self.state['next_id'] += 1
            used.add(track['id'])
            component['track_id'] = track['id']
            track['samples'] += 1
            track['max_lines'] = max(track['max_lines'],component['lines'])
            track['overlap_conflicts'] = track.get('overlap_conflicts',0)+len(component['overlapping_variants'])
            track['regions'] = sorted(set(track['regions'])|set(component['regions']))
            # Geometry follows a band, not the right edge/length of its text.
            track['bbox'] = [old*.7+new*.3 for old,new in zip(track['bbox'],bbox)]
            track['line_height'] = track['line_height']*.7+line_height*.3
            text, end = _text_key(component['text']), min(range_end,timestamp+self.interval)
            previous = track['runs'][-1] if track['runs'] else None
            if previous and previous['text'] == text and abs(previous['end']-timestamp) <= 1e-6:
                previous['end'] = end
                previous['last_sample'] = self.state['samples_processed']
                previous['bbox'] = bbox
            else:
                track['runs'].append({'start':timestamp,'end':end,'text':text,'bbox':bbox,
                    'first_sample':self.state['samples_processed'],'last_sample':self.state['samples_processed']})
        self.state['samples_processed'] += 1
        return components

    def _score(self, track):
        left, top, right, bottom = track['bbox']
        centre_x, centre_y = (left+right)/(2*self.width), (top+bottom)/(2*self.height)
        centred = max(0,1-abs(centre_x-.5)/.5)
        width = min(1,(right-left)/self.width)
        changing = min(1,len({_text_key(run['text']) for run in track['runs']})/4)
        return 3*centred + 2*centre_y + width + changing + min(1,track['samples']/4)*.2

    def _explicit_selection(self):
        tracks = self.state['tracks']
        candidates = [track for track in tracks if track['max_lines'] <= 3]
        scopes = sorted({region for track in candidates for region in track['regions']}) if self.explicit_regions else [None]
        selected, issues = [], []
        for scope in scopes:
            eligible = [track for track in candidates if scope is None or scope in track['regions']]
            if not eligible:
                continue
            eligible.sort(key=self._score,reverse=True)
            best = eligible[0]
            if best['id'] not in selected:
                selected.append(best['id'])
            # Do not call a heuristic choice semantic certainty. Competing
            # centre bands or low confidence need an explicit narrower region.
            if not self.explicit_regions:
                left,top,right,bottom = best['bbox']
                if self._score(best) < 3.5 or (right-left)/self.width < .08:
                    issues.append({'code':'uncertain_caption_band','track_id':best['id'],'severity':'review'})
            for other in eligible[1:]:
                if self._score(other) >= 4.4 and not _temporal_overlap(best['runs'],other['runs']):
                    issues.append({'code':'unselected_temporal_band','track_id':other['id'],'severity':'review'})
                elif self._score(other) >= self._score(best)-.65:
                    issues.append({'code':'competing_caption_band','track_ids':[best['id'],other['id']],'severity':'review'})
        for track in tracks:
            if track['max_lines'] > 3:
                # A background page does not make a clearly selected caption
                # band unsafe. Keep it as evidence/warning, and require review
                # only when no caption band can be selected at all.
                issues.append({'code':'multiline_text_block','track_id':track['id'],'lines':track['max_lines'],
                               'severity':'warning' if selected else 'review'})
            if track['id'] in selected and track.get('overlap_conflicts'):
                issues.append({'code':'overlapping_text_variants','track_id':track['id'],
                               'count':track['overlap_conflicts'],'severity':'review'})
        if self.state.get('legacy_prefix'):
            issues.append({'code':'legacy_untracked_prefix','severity':'review'})
        if tracks and not selected:
            issues.append({'code':'no_caption_band','severity':'review'})
        return {'version':VERSION,'mode':'explicit_region' if self.explicit_regions else 'automatic_band',
                'selected_tracks':selected,'requires_review':any(issue['severity']=='review' for issue in issues),'issues':issues,
                'raw_observation_file':'ocr-observations.jsonl','track_file':'ocr-tracks.json'}

    def candidate_summaries(self):
        """Geometry/temporal candidates for inspection or a semantic selector.

        ``text_samples`` carries stable zero-based ``track-id:run-N`` references.
        A high confidence means a sustained visual band, not complete dialogue,
        OCR correctness, or proof that a silent interval contains no speech.
        """
        return _caption_candidates(self.state['tracks'],self.width,self.height,self.interval,
                                   self.state['samples_processed'])

    def selection(self):
        if self.explicit_regions:
            return self._explicit_selection()
        candidates=self.candidate_summaries()
        eligible=[candidate for candidate in candidates if candidate['role_hint']!='document']
        selected,issues=[],[]
        certainty='unresolved'
        best=eligible[0] if eligible else None
        if best:
            selected=best['track_ids'][:]
            geometry=best['geometry']
            geometric_score=(3*max(0.,1-abs(geometry['center_x']-.5)/.5)+
                             2*geometry['center_y']+geometry['width_fraction'])
            if (geometric_score<3.5 or (geometry['width_fraction']<.08 and best['sample_fraction']<.5)):
                issues.append({'code':'uncertain_caption_band','track_id':best['band_id'],'severity':'review'})
            if best['role_hint']=='persistent_overlay_candidate':
                issues.append({'code':'persistent_text_band','track_id':best['band_id'],'severity':'review'})
            for other in eligible[1:]:
                if other['role_hint']=='persistent_overlay_candidate':
                    continue
                shape=other['geometry']
                other_geometry=(3*max(0.,1-abs(shape['center_x']-.5)/.5)+
                                2*shape['center_y']+shape['width_fraction'])
                # A few frames of a document/title cannot veto a caption band
                # observed throughout the video. Sustained competing bands or
                # a relocation without temporal overlap remain unresolved.
                significant=other['sample_support']>=max(1.,best['sample_support']*.2)
                temporal_overlap=_temporal_overlap(
                    [{'start':a,'end':b} for a,b in best['intervals']],
                    [{'start':a,'end':b} for a,b in other['intervals']])
                if significant and other_geometry>=4.4 and not temporal_overlap:
                    issues.append({'code':'unselected_temporal_band','track_id':other['band_id'],'severity':'review'})
                elif significant and other['score']>=best['score']-2.5:
                    issues.append({'code':'competing_caption_band','track_ids':[best['band_id'],other['band_id']],
                                   'severity':'review'})
            runner=eligible[1]['score'] if len(eligible)>1 else best['score']-10
            certainty='strong' if (best['sample_fraction']>=.5 and best['score']-runner>=3 and
                                   not any(item['severity']=='review' for item in issues)) else 'provisional'
        for track in self.state['tracks']:
            if track['max_lines']>3:
                issues.append({'code':'multiline_text_block','track_id':track['id'],'lines':track['max_lines'],
                               'severity':'warning' if selected else 'review'})
            if track['id'] in selected and track.get('overlap_conflicts'):
                issues.append({'code':'overlapping_text_variants','track_id':track['id'],
                               'count':track['overlap_conflicts'],'severity':'review'})
        if self.state.get('legacy_prefix'):
            issues.append({'code':'legacy_untracked_prefix','severity':'review'})
        if self.state['tracks'] and not selected:
            issues.append({'code':'no_caption_band','severity':'review'})
        review=any(item['severity']=='review' for item in issues)
        if review:
            certainty='unresolved'
        return {'version':VERSION,'mode':'automatic_band','policy':SELECTION_POLICY,
                'selected_tracks':selected,'excluded_runs':[],'confidence':certainty,
                'requires_review':review,'issues':issues,'candidates':candidates,
                'raw_observation_file':'ocr-observations.jsonl','track_file':'ocr-tracks.json',
                'coverage_complete':False}

    def rows(self,track_ids=None):
        """Assemble only selected tracks, independently of unrelated text changes."""
        selected = set(self.selection()['selected_tracks'] if track_ids is None else track_ids)
        runs = [run for track in self.state['tracks'] if track['id'] in selected for run in track['runs']]
        events = {}
        for index,run in enumerate(runs):
            events.setdefault(run['start'],[[],[]])[0].append(index)
            events.setdefault(run['end'],[[],[]])[1].append(index)
        moments, active_indices = sorted(events), set()
        rows = copy.deepcopy(self.state.get('legacy_prefix',[]))
        for start,end in zip(moments,moments[1:]):
            additions,removals = events[start]
            active_indices.difference_update(removals)
            active_indices.update(additions)
            if end-start <= 1e-8:
                continue
            active = [runs[index] for index in active_indices]
            text = reading_order([(*run['bbox'],run['text']) for run in active]) if active else ''
            if not text:
                continue
            if rows and rows[-1]['text'] == text and abs(rows[-1]['end']-start) <= 1e-6:
                rows[-1]['end'] = end
            else:
                rows.append({'start':start,'end':end,'text':text,'translated':''})
        return rows

    def manifest(self):
        return {'version':VERSION,'selection':self.selection(),'tracks':copy.deepcopy(self.state['tracks']),
                'samples_processed':self.state['samples_processed'],
                'legacy_prefix_rows':len(self.state.get('legacy_prefix',[]))}


def read_manifest(path,project):
    """Read bounded source-bound track evidence, never an arbitrary client path."""
    from subtitle_quality import source_signature
    try:
        with Path(path).open('rb') as stream:
            encoded=stream.read(16*1024*1024+1)
        if len(encoded)>16*1024*1024:
            raise ValueError('Bằng chứng lớp chữ vượt giới hạn an toàn.')
        document=json.loads(encoded.decode('utf-8'))
        from ocr_checkpoint import _finite_tree
        _finite_tree(document)
        if (not isinstance(document,dict) or document.get('source_signature')!=source_signature(project) or
                document.get('source_revision')!=project.get('source_revision')):
            raise ValueError('Lớp chữ thuộc nguồn video trước hoặc thiếu danh tính nguồn.')
        span=document.get('range')
        if (not isinstance(span,list) or len(span)!=2 or any(type(value) not in (float,int) for value in span) or
                not 0<=span[0]<span[1]<=float(project['media']['duration'])+1e-5):
            raise ValueError('Phạm vi lớp chữ không hợp lệ.')
        tracks=document.get('tracks')
        if not isinstance(tracks,list):
            raise ValueError('Thiếu dữ liệu lớp chữ.')
        state={'version':document.get('version'),'tracks':tracks,'next_id':len(tracks)+1,
               'samples_processed':document.get('samples_processed'),'legacy_prefix':[],'evidence_bytes':0}
        _validate_state(state,float(project['media']['width']),float(project['media']['height']))
        if any(run['start']<span[0]-1e-5 or run['end']>span[1]+1e-5
               for track in tracks for run in track['runs']):
            raise ValueError('Mốc lớp chữ nằm ngoài phạm vi đã nhận dạng.')
        return document
    except (OSError,UnicodeError,json.JSONDecodeError,RecursionError,TypeError,KeyError) as exc:
        raise ValueError('Không đọc được bằng chứng lớp chữ; bảng phụ đề hiện tại được giữ.') from exc


def rows_from_manifest(document,project,track_ids):
    if (not isinstance(track_ids,list) or not track_ids or
            any(not isinstance(identifier,str) for identifier in track_ids) or len(track_ids)!=len(set(track_ids))):
        raise ValueError('Chọn ít nhất một lớp chữ cụ thể, không chọn lặp.')
    available={track['id'] for track in document['tracks']}
    if set(track_ids)-available:
        raise ValueError('Lớp chữ đã chọn không có trong lượt nhận dạng này.')
    state={'version':document['version'],'tracks':copy.deepcopy(document['tracks']),
           'next_id':len(document['tracks'])+1,'samples_processed':document['samples_processed'],
           'legacy_prefix':[],'evidence_bytes':0}
    tracker=OCRTrackAssembler(project['media']['width'],project['media']['height'],
        document.get('sampling_interval',.5),state=state)
    return tracker.rows(track_ids)


def auto_selection(document,project):
    """Reevaluate owned raw tracks under the current local selection policy.

    Caller first reads/source-checks the manifest via ``read_manifest``. This
    helper is pure: it neither mutates evidence nor replaces a project table.
    """
    state={'version':document['version'],'tracks':copy.deepcopy(document['tracks']),
           'next_id':len(document['tracks'])+1,'samples_processed':document['samples_processed'],
           'legacy_prefix':[],'evidence_bytes':0}
    tracker=OCRTrackAssembler(project['media']['width'],project['media']['height'],
        document.get('sampling_interval',.5),state=state)
    selection=tracker.selection()
    if document.get('legacy_prefix_rows'):
        selection['issues'].append({'code':'legacy_untracked_prefix','severity':'review'})
        selection.update(requires_review=True,confidence='unresolved')
    return selection


automated_selection=auto_selection


def rows_from_selection(document,project,selection):
    """Apply a validated decision; stable run IDs are zero-based within track.

    Explicit exclusions are only references to existing raw runs. They cannot
    invent text/time or delete evidence. A semantic selector may request these
    references, but the caller must first validate its scope and confidence.
    """
    if not isinstance(selection,dict):
        raise ValueError('Thiếu lựa chọn lớp phụ đề hợp lệ.')
    ids=selection.get('selected_tracks')
    # Reuse strict manual ID validation before doing any filtered assembly.
    rows=rows_from_manifest(document,project,ids)
    excluded=selection.get('excluded_runs',[])
    if (not isinstance(excluded,list) or any(not isinstance(value,str) for value in excluded) or
            len(excluded)!=len(set(excluded))):
        raise ValueError('Danh sách đoạn chữ loại khỏi lời đọc không hợp lệ.')
    known={f'{track["id"]}:run-{index}' for track in document['tracks']
           if track['id'] in ids for index,_ in enumerate(track['runs'])}
    if set(excluded)-known:
        raise ValueError('Đoạn chữ loại khỏi lời đọc không thuộc lớp đã chọn hoặc không còn trong bằng chứng.')
    if not excluded:
        return rows
    excluded=set(excluded)
    filtered=copy.deepcopy(document)
    for track in filtered['tracks']:
        if track['id'] in ids:
            track['runs']=[run for index,run in enumerate(track['runs'])
                           if f'{track["id"]}:run-{index}' not in excluded]
    state={'version':document['version'],'tracks':filtered['tracks'],
           'next_id':len(document['tracks'])+1,'samples_processed':document['samples_processed'],
           'legacy_prefix':[],'evidence_bytes':0}
    # Empty tracks are legitimate only in this transient filtered assembly;
    # raw manifest validation already happened above. Never rewrite evidence.
    tracker=OCRTrackAssembler(project['media']['width'],project['media']['height'],
        document.get('sampling_interval',.5))
    tracker.state=state
    return tracker.rows(ids)
