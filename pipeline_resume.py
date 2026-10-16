"""Continue verified preparation snapshots without repeating completed work.

Inspection is read-only. Only the queue endpoint creates a private job snapshot;
it never changes the editor's extraction choice or acknowledges review for it.
Pre-render approval is explicit, persisted separately, and bound to the exact
caption, speech, picture and audio identities the editor presented for review.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path


MAX_SNAPSHOT_BYTES = 32 * 1024**2


def read_input(folder, row, project):
    path = Path(folder) / 'input.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise ValueError('Thiếu bản chụp đầu vào của lượt nhận dạng; không tự chạy lại nhận dạng.')
    try:
        with path.open('rb') as stream:
            content = stream.read(MAX_SNAPSHOT_BYTES + 1)
        if len(content) > MAX_SNAPSHOT_BYTES:
            raise ValueError('Bản chụp đầu vào quá lớn.')
        snapshot = json.loads(content.decode('utf-8'))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError('Không đọc được bản chụp nhận dạng đã lưu.') from exc
    if not isinstance(snapshot, dict):
        raise ValueError('Bản chụp đầu vào không hợp lệ.')
    original, original_job = snapshot.get('project'), snapshot.get('job')
    if (not isinstance(original, dict) or not isinstance(original_job, dict) or
            original.get('id') != project.get('id') or original_job.get('id') != row.get('id') or
            original_job.get('kind') != 'pipeline' or original_job.get('project_id') != project.get('id') or
            original.get('source_revision') != project.get('source_revision') or
            original_job.get('source_revision') != row.get('source_revision') or
            not isinstance(original.get('media'), dict) or not isinstance(original.get('params'), dict)):
        raise ValueError('Lượt nhận dạng thuộc video nguồn khác; không dùng lại cho video hiện tại.')
    if any(original['media'].get(key) != project['media'].get(key)
           for key in ('path', 'duration', 'width', 'height')):
        raise ValueError('Video nguồn đã thay đổi so với lượt nhận dạng đã lưu.')
    return original


def write_review_checkpoint(initial_project, recognized_project, job, folder, result):
    """Keep even a structurally invalid table available for manual correction."""
    from translation_checkpoint import write_document, pipeline_input_signature
    from subtitle_quality import source_signature
    document = {'schema_version': 1, 'type': 'pipeline_review',
                'job_id': job['id'], 'project_id': initial_project['id'],
                'source_revision': initial_project.get('source_revision'),
                'source_signature': source_signature(initial_project),
                'input_signature': pipeline_input_signature(initial_project, job.get('segment_indices')),
                'rows': copy.deepcopy(recognized_project.get('segments', [])),
                'coverage': copy.deepcopy(recognized_project.get('coverage', {})),
                'completed_steps': result.get('completed_steps', result.get('steps', [])),
                'remaining_steps': result.get('remaining_steps', []),
                'warnings': result.get('warnings', [])}
    if result.get('review_stage') == 'pre_render':
        document.update(review_stage='pre_render',
            prepared_signature=pre_render_signature(recognized_project),
            prepared_project=copy.deepcopy(recognized_project))
    write_document(Path(folder) / 'pipeline-review.json', document)
    return 'pipeline-review.json'


def pre_render_signature(project):
    """Bind human approval to actual editable contents, never a timestamp.

    Workflow switches are job-local on a render-only continuation. Everything
    affecting source, captions, selected speech, cleanup and audio mix remains
    bound; changing providers or voices also requires a fresh review.
    """
    from artifact_resolver import render_fingerprint
    from subtitle_quality import digest, source_signature
    # Browser form numbers and JSON validation may round-trip 0 as 0.0. Bind
    # approval to numeric values, not incidental JSON number representation.
    # Normalize before nested fingerprints as well as the final digest.
    def canonical_numbers(value):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, dict):
            return {key: canonical_numbers(item) for key, item in value.items()}
        if isinstance(value, list):
            return [canonical_numbers(item) for item in value]
        return value
    project = canonical_numbers(project)
    workflow = {'pipeline_extract', 'pipeline_translate', 'pipeline_tts', 'pipeline_clean',
                'translation_scope', 'subtitle_automation', 'export_name', 'export_directory'}
    params = {key: value for key, value in project.get('params', {}).items() if key not in workflow}
    artifacts = project.get('artifacts') or {}
    selected_assets = {}
    for kind in ('tts', 'clean'):
        used = (kind == 'clean' and params.get('use_clean') or kind == 'tts' and
                float(params.get('voice_gain', 0)) > 0 and not params.get('voice_path'))
        if used:
            artifact = artifacts.get(kind) or {}
            selected_assets[kind] = {'signature': artifact.get('speech_signature' if kind == 'tts' else 'signature'),
                'file_integrity': (artifact.get('verification') or {}).get('file_integrity')}
    # Word timestamps are derived output and are already included in the editor
    # table; they must not be silently replaced after the user heard this plan.
    return digest(['npk-pre-render-review-v1', project.get('id'),
        project.get('source_revision'), source_signature(project), render_fingerprint(project),
        project.get('segments', []), project.get('regions', []),
        project.get('extraction_regions', []), params, selected_assets])


def _pre_render_assets(project):
    """Recheck anchored bytes before approving or consuming an approval."""
    from stage_journal import require_reusable_artifact
    p = project.get('params', {})
    if p.get('use_clean'):
        require_reusable_artifact(project, 'clean')
    if float(p.get('voice_gain', 0)) > 0 and not p.get('voice_path'):
        require_reusable_artifact(project, 'tts')


def pre_render_review_status(project):
    signature = pre_render_signature(project)
    approval = project.get('pre_render_review') or {}
    enabled = project.get('params', {}).get('review_before_render') is True
    approved = (isinstance(approval, dict) and approval.get('schema_version') == 1 and
                approval.get('signature') == signature and approval.get('approved') is True)
    return {'project_id': project['id'], 'expected_revision': project['updated'],
        'enabled': enabled, 'signature': signature, 'approved': approved,
        'requires_review': enabled and not approved,
        'job_id': approval.get('job_id') if approved else None,
        'review_stage': 'pre_render', 'remaining_steps': ['render']}


def require_pre_render_approval(project):
    """An export may only consume an explicit approval for the same contents."""
    if project.get('params', {}).get('review_before_render') is not True:
        return
    status = pre_render_review_status(project)
    if not status['approved']:
        raise ValueError('Cần xem và duyệt kết quả hiện tại trước khi xuất video. Nội dung đã thay đổi thì phải duyệt lại.')
    _pre_render_assets(project)


def prepared_review_snapshot(row, project, folder):
    """Read a completed preparation contract, preserving the editor verbatim."""
    from subtitle_quality import source_signature
    result = row.get('result') or {}
    if (row.get('kind') != 'pipeline' or row.get('state') != 'review' or
            result.get('review_stage') != 'pre_render' or not result.get('needs_review') or
            result.get('remaining_steps') != ['render'] or
            result.get('primary_file') == 'final.mp4' or 'final.mp4' in result.get('files', [])):
        raise ValueError('Chưa có kết quả chuẩn bị hoàn tất để xem và duyệt trước khi xuất.')
    if row.get('source_revision') != project.get('source_revision') or row.get('project_id') != project['id']:
        raise ValueError('Kết quả cần duyệt thuộc nguồn khác; không áp dụng cho video hiện tại.')
    read_input(folder, row, project)
    path = Path(folder) / 'pipeline-review.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise ValueError('Thiếu bản chụp kết quả chuẩn bị cần duyệt.')
    from translation_checkpoint import _unique_object, _invalid_constant, _finite_document
    with path.open(encoding='utf-8') as stream:
        document = json.load(stream, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    _finite_document(document)
    prepared = document.get('prepared_project') if isinstance(document, dict) else None
    if (document.get('schema_version') != 1 or document.get('review_stage') != 'pre_render' or
            document.get('job_id') != row['id'] or document.get('project_id') != project['id'] or
            document.get('source_revision') != project.get('source_revision') or
            document.get('source_signature') != source_signature(project) or
            not isinstance(prepared, dict) or prepared.get('id') != project['id'] or
            prepared.get('source_revision') != project.get('source_revision')):
        raise ValueError('Bản chụp chuẩn bị không khớp dự án/nguồn hiện tại.')
    wanted = pre_render_signature(project)
    if (pre_render_signature(prepared) != wanted or document.get('prepared_signature') != wanted or
            result.get('pre_render_signature') != wanted):
        raise ValueError('Nội dung, giọng, âm thanh hoặc cấu hình đã đổi sau bước chuẩn bị. Chạy chuẩn bị lại rồi xem và duyệt kết quả mới.')
    _pre_render_assets(project)
    return copy.deepcopy(project)


def pre_render_continuation_status(row, project, jobs, folder):
    plan = {'job_id': row['id'], 'project_id': project['id'], 'expected_revision': project['updated'],
        'can_continue': False, 'completed_steps': list((row.get('result') or {}).get('completed_steps', [])),
        'remaining_steps': ['render'], 'requires_review': True, 'review_stage': 'pre_render',
        'enabled': project.get('params', {}).get('review_before_render') is True,
        'signature': None, 'approved': False,
        'errors': [], 'warnings': [], 'reason': '', 'next_action': 'review_before_render'}
    try:
        plan.update(pre_render_review_status(project))
        plan['job_id'] = row['id']
        snapshot = prepared_review_snapshot(row, project, folder)
        if any(item.get('project_id') == project['id'] and item.get('state') in ('queued', 'running')
               for item in jobs):
            raise ValueError('Dự án đang có tác vụ khác. Chờ hoàn tất trước khi duyệt hoặc xuất.')
        if any(item.get('continuation_from') == row['id'] and item.get('state') == 'done' and
               (item.get('result') or {}).get('primary_file') == 'final.mp4' for item in jobs):
            raise ValueError('Kết quả này đã được xuất; mở thành phẩm hoặc chuẩn bị phiên bản mới.')
        from preflight import inspect
        checks = inspect(pre_render_continuation_snapshot(snapshot), 'render', check_credentials=False)
        plan['errors'], plan['warnings'] = checks.get('errors', []), checks.get('warnings', [])
        plan['can_approve'] = bool(checks.get('ok'))
        plan.update(pre_render_review_status(project))
        plan['job_id'] = row['id']
        if plan['errors']:
            plan.update(reason=plan['errors'][0], next_action='fix_configuration')
        elif not plan['approved']:
            plan['reason'] = 'Các bước xử lý đã xong. Xem phụ đề, nghe lời đọc và duyệt trước khi xuất video.'
        else:
            plan.update(can_continue=True, requires_review=False, next_action='export_reviewed_video',
                        reason='Đã duyệt đúng kết quả hiện tại. Chỉ xuất video, không nhận dạng, dịch hoặc tạo giọng lại.')
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
        plan.update(reason=str(exc), can_approve=False)
    return plan


def pre_render_continuation_snapshot(project):
    snapshot = copy.deepcopy(project)
    snapshot['params'].update(pipeline_extract='existing', pipeline_translate=False,
                             pipeline_tts=False, pipeline_clean=False,
                             translation_scope='missing', subtitle_automation='manual')
    return snapshot


def continuation_status(row, project, jobs, folder, *, allow_recovery=True):
    """Validate lineage, current edits and source evidence without model loads."""
    from subtitle_quality import source_signature, inspect_subtitles, clean_ready, speech_ready
    plan = {'job_id': row.get('id') if row else None, 'project_id': project['id'],
            'expected_revision': project['updated'], 'can_continue': False,
            'completed_steps': [], 'remaining_steps': [], 'requires_review': False,
            'errors': [], 'warnings': [], 'reason': '', 'next_action': 'start_pipeline'}
    if not row:
        plan['reason'] = 'Không có quy trình đang dừng để tiếp tục.'
        return plan
    if (row.get('result') or {}).get('review_stage') == 'pre_render' and row.get('state') == 'review':
        return pre_render_continuation_status(row, project, jobs, folder)
    if allow_recovery:
        if row.get('state') in ('error', 'cancelled') and not row.get('continuation_from'):
            return failed_continuation_status(row, project, jobs, folder)
        if row.get('continuation_from') and row.get('state') in ('error', 'cancelled'):
            failed = latest_failed_successor({'id':row['continuation_from']}, project, jobs) or row
            return failed_continuation_status(failed, project, jobs, Path(folder).parent/failed['id'])
        failed = latest_failed_successor(row, project, jobs)
        if failed:
            return failed_continuation_status(failed, project, jobs, Path(folder).parent/failed['id'])
    try:
        result = row.get('result') or {}
        if row.get('kind') != 'pipeline' or row.get('state') != 'review' or not result.get('needs_review'):
            raise ValueError('Quy trình này không đang dừng sau nhận dạng. Không chạy lặp lại.')
        if result.get('primary_file') == 'final.mp4' or 'final.mp4' in result.get('files', []):
            raise ValueError('Quy trình này đã có thành phẩm; mở kết quả thay vì chạy lại.')
        if row.get('project_id') != project['id'] or row.get('source_revision') != project.get('source_revision'):
            raise ValueError('Lượt nhận dạng thuộc video nguồn trước; không tự áp dụng cho video hiện tại.')
        original = read_input(folder, row, project)
        extract = original['params'].get('pipeline_extract')
        completed = result.get('completed_steps', result.get('steps', []))
        if extract not in ('ocr', 'asr') or extract not in completed:
            raise ValueError('Chưa có bằng chứng bước nhận dạng đã hoàn tất.')
        recognized = result.get('segments')
        if not isinstance(recognized, list) or not recognized:
            raise ValueError('Lượt nhận dạng chưa có bảng phụ đề hoàn chỉnh để tiếp tục.')
        coverage = result.get('coverage') or {}
        if coverage.get('source_signature') != source_signature(project) or coverage.get('method') != extract:
            raise ValueError('Tệp video đã đổi hoặc thiếu bằng chứng nhận dạng của đúng video này.')
        artifact = (project.get('artifacts') or {}).get('pipeline') or {}
        same_rows = [(r.get('start'), r.get('end'), r.get('text')) for r in recognized] == [
            (r.get('start'), r.get('end'), r.get('text')) for r in project.get('segments', [])]
        if not (row.get('project_applied') or artifact.get('job_id') == row['id'] or
                (same_rows and project.get('coverage') == coverage)):
            raise ValueError('Kết quả nhận dạng chưa được áp dụng. Mở Chi tiết và áp dụng bảng đã nhận dạng trước.')
        for other in jobs:
            if other.get('id') == row['id'] or other.get('project_id') != project['id']:
                continue
            if other.get('state') in ('queued', 'running'):
                raise ValueError('Dự án đang có tác vụ khác. Chờ hoàn tất trước khi tiếp tục để giữ các chỉnh sửa.')
            recognition = other.get('kind') in ('ocr', 'asr') or (
                other.get('kind') == 'pipeline' and
                other.get('configuration', {}).get('pipeline_extract') in ('ocr', 'asr'))
            if (recognition and other.get('source_revision') == project.get('source_revision') and
                    other.get('state') in ('done', 'review') and other.get('created', 0) > row.get('created', 0)):
                raise ValueError('Dự án đã có lượt nhận dạng mới hơn. Dùng bảng hiện tại hoặc tiếp tục lượt mới nhất.')
            if (other.get('continuation_from') == row['id'] and other.get('state') in ('done', 'review')):
                raise ValueError('Các bước còn lại của lượt này đã được chạy. Mở kết quả hoặc tác vụ tiếp theo.')
        if not project.get('segments'):
            raise ValueError('Bảng phụ đề hiện tại trống. Khôi phục hoặc áp dụng kết quả nhận dạng trước.')
        plan['completed_steps'] = list(dict.fromkeys(completed))
        p = project['params']
        remaining = []
        if p.get('pipeline_translate', True):
            remaining.append('translate')
        if p.get('pipeline_tts', True):
            if not p.get('pipeline_translate', True) and speech_ready(project):
                plan['completed_steps'].append('tts')
            else:
                remaining.append('tts')
        if p.get('pipeline_clean', False) and not clean_ready(project):
            remaining.append('clean')
        elif p.get('use_clean') and clean_ready(project):
            plan['completed_steps'].append('clean')
        remaining.append('render')
        plan['remaining_steps'] = remaining
        quality = inspect_subtitles(project)
        plan['requires_review'] = quality['requires_review']
        plan['errors'] = quality['errors']
        plan['warnings'] = [issue['message'] for issue in quality['issues'] if issue['severity'] != 'error']
        if quality['errors']:
            plan.update(reason='Sửa lỗi phụ đề trước khi tiếp tục: ' + quality['errors'][0], next_action='edit_subtitles')
        elif quality['requires_review']:
            plan.update(reason='Cần kiểm tra phạm vi nhận dạng trước khi tiếp tục.', next_action='review_subtitles_then_resume')
        else:
            from preflight import inspect
            checks = inspect(continuation_snapshot(project), 'pipeline', check_credentials=False)
            plan['errors'] = list(dict.fromkeys(checks.get('errors', [])))
            plan['warnings'] = list(dict.fromkeys(plan['warnings'] + checks.get('warnings', [])))
            if not checks['ok']:
                plan.update(reason='Chưa sẵn sàng chạy các bước còn lại: ' + (plan['errors'][0] if plan['errors'] else 'Kiểm tra cấu hình.'),
                            next_action='fix_configuration')
            else:
                plan.update(can_continue=True, reason='Dùng bảng phụ đề hiện tại để chạy các bước còn lại; không nhận dạng lại.',
                            next_action='continue_pipeline')
    except (ValueError, OSError, KeyError, TypeError) as exc:
        plan.update(reason=str(exc), next_action='inspect_pipeline')
    return plan


def continuation_snapshot(project):
    """All current manual edits/preferences survive; overrides are job-local."""
    snapshot = copy.deepcopy(project)
    snapshot['params']['pipeline_extract'] = 'existing'
    snapshot['params']['translation_scope'] = 'missing'
    return snapshot


def latest_failed_successor(row, project, jobs):
    candidates = [item for item in jobs if item.get('continuation_from') == row.get('id') and
                  item.get('project_id') == project['id'] and
                  item.get('source_revision') == project.get('source_revision') and
                  item.get('state') in ('error', 'cancelled')]
    return max(candidates, key=lambda item: item.get('created', 0), default=None)


def _merge_known_translations(snapshot, saved_rows):
    """Fill exact identities only; never replace a nonempty manual translation."""
    current = snapshot['segments']
    identity = lambda cue: (cue.get('start'), cue.get('end'), cue.get('text'))
    if len(saved_rows) == len(current) and all(identity(a) == identity(b)
                                               for a, b in zip(saved_rows, current)):
        values = [(index, str(saved.get('translated') or '').strip())
                  for index, saved in enumerate(saved_rows)]
    else:
        from collections import Counter
        saved_counts = Counter(identity(cue) for cue in saved_rows)
        current_counts = Counter(identity(cue) for cue in current)
        translated = {}
        for saved in saved_rows:
            text = str(saved.get('translated') or '').strip()
            if text:
                key = identity(saved)
                if saved_counts[key] != 1 or current_counts[key] > 1:
                    raise ValueError('Câu dịch có sẵn bị trùng sau chỉnh sửa; cần kiểm tra thủ công.')
                translated[key] = text
        values = [(index, translated.get(identity(cue), '')) for index, cue in enumerate(current)]
    restored = 0
    for index, text in values:
        if (text and current[index].get('user_edited') is not True and
                not str(current[index].get('translated') or '').strip()):
            current[index]['translated'] = text
            restored += 1
    return restored


def _replay_failed_lineage(root, row, snapshot, jobs, folder):
    """Replay every bound predecessor, including stages absent in the last child.

    A render-only successor can fail before creating a new journal. Earlier
    verified TTS/clean stages and partial translations must still be reachable.
    Every merge revalidates source, language and exact cue identities against
    the current private snapshot; the editor is never modified here.
    """
    from studio_api import translation_original_input, translation_restore_candidates
    from stage_journal import recover
    root_id = root['id']
    chain = [item for item in jobs if item.get('project_id') == snapshot['id'] and
             item.get('source_revision') == snapshot.get('source_revision') and
             item.get('kind') == 'pipeline' and item.get('state') in ('error', 'cancelled') and
             (item.get('id') == root_id or item.get('continuation_from') == root_id)]
    if not any(item.get('id') == row['id'] for item in chain):
        chain.append(row)
    chain.sort(key=lambda item: (item.get('created', 0), item['id']))
    restored = 0
    for prior in chain:
        import re
        if not re.fullmatch(r'[0-9a-f]{32}', prior['id']):
            raise ValueError('Mã tác vụ trong lineage không hợp lệ; không đọc snapshot ngoài thư mục tác vụ.')
        prior_folder = Path(folder).parent/prior['id']
        original = translation_original_input(prior, snapshot)
        if (prior_folder/'stage-journal.json').is_file():
            snapshot, _ = recover(snapshot, prior_folder)
        restored += _merge_known_translations(snapshot, original.get('segments', []))
        if (prior_folder/'translation-checkpoint.json').is_file():
            for index, text in translation_restore_candidates(prior, snapshot):
                if not snapshot['segments'][index].get('translated', '').strip():
                    snapshot['segments'][index]['translated'] = text
                    restored += 1
        if (prior_folder/'stage-journal.json').is_file():
            # Restored translations may make this stage's speech identity
            # applicable; recheck rather than using stale artifact metadata.
            snapshot, _ = recover(snapshot, prior_folder)
    return snapshot, restored


def _validate_recovered_scope(row, original, snapshot):
    selected = row.get('segment_indices')
    if selected is not None:
        identity = lambda cue: (cue.get('start'), cue.get('end'), cue.get('text'))
        if ([identity(cue) for cue in original.get('segments', [])] !=
                [identity(cue) for cue in snapshot.get('segments', [])]):
            raise ValueError('Phạm vi dịch đã chọn đổi vị trí/mốc. Chọn lại các câu cần dịch; không mở rộng ngầm.')
    return selected


def _pending_selection(snapshot):
    if snapshot['params'].get('subtitle_automation', 'manual') not in ('local', 'gemini', 'router9'):
        return False
    from auto_subtitle_director import automation_status
    return bool(automation_status(snapshot).get('can_process'))


def recovery_snapshot(row, project, jobs, folder):
    """Merge verified completed translations into a new job, never the editor."""
    from studio_api import translation_restore_candidates, translation_original_input
    from subtitle_quality import source_signature
    if row.get('kind') != 'pipeline' or row.get('state') not in ('error', 'cancelled'):
        raise ValueError('Không có quy trình tiếp tục bị gián đoạn tương ứng.')
    original = read_input(folder, row, project)
    if not row.get('continuation_from'):
        snapshot, _ = journal_recovery_snapshot(row, project, folder, original=original)
        snapshot, restored = _replay_failed_lineage(row, row, snapshot, jobs, folder)
        selected = _validate_recovered_scope(row, original, snapshot)
        missing = [i for i, cue in enumerate(snapshot['segments']) if not cue.get('translated', '').strip()
                   and (selected is None or i in selected)]
        if not missing and not _pending_selection(snapshot):
            snapshot['params']['pipeline_translate'] = False
        return snapshot, row, restored, missing, selected
    root = next((item for item in jobs if item.get('id') == row['continuation_from']), None)
    if root and root.get('kind') == 'pipeline' and root.get('state') in ('error', 'cancelled'):
        snapshot, _ = journal_recovery_snapshot(root, project, Path(folder).parent/root['id'])
        snapshot, restored = _replay_failed_lineage(root, row, snapshot, jobs, folder)
        selected = _validate_recovered_scope(row, original, snapshot)
        missing = [i for i, cue in enumerate(snapshot['segments']) if not cue.get('translated', '').strip()
                   and (selected is None or i in selected)]
        if not missing and not _pending_selection(snapshot):
            snapshot['params']['pipeline_translate'] = False
        return snapshot, root, restored, missing, selected
    # Also binds translation language/selection to the private job input.
    original = translation_original_input(row, project)
    if original['params'].get('pipeline_extract') != 'existing':
        raise ValueError('Bản chụp tiếp tục chưa xác minh được việc dùng phụ đề có sẵn.')
    if (original.get('coverage') or {}).get('source_signature') != source_signature(project):
        raise ValueError('Tệp nguồn đã đổi sau lượt dịch; không tự dùng checkpoint cũ.')
    root = next((item for item in jobs if item.get('id') == row['continuation_from']), None)
    if not root or root.get('kind') != 'pipeline' or root.get('state') != 'review':
        raise ValueError('Thiếu lượt nhận dạng gốc để kiểm tra quy trình bị gián đoạn.')
    snapshot = continuation_snapshot(project)
    checkpoint = Path(folder)/'translation-checkpoint.json'
    restored = 0
    completed = translation_restore_candidates(row, project) if checkpoint.is_file() else []
    # A second interrupted recovery's input already contains the first job's
    # verified translations. They are deliberately absent from its checkpoint
    # completed_indices; retaining them prevents repeated batches on retry two.
    original_rows = original.get('segments', [])
    current_rows = snapshot['segments']
    identity = lambda cue: (cue.get('start'), cue.get('end'), cue.get('text'))
    if len(original_rows) == len(current_rows) and all(identity(saved) == identity(current)
                                                    for saved, current in zip(original_rows, current_rows)):
        preexisting = [(index, saved['translated'].strip()) for index, saved in enumerate(original_rows)
                       if saved.get('translated', '').strip()]
    else:
        from collections import Counter
        original_counts = Counter(identity(cue) for cue in original_rows)
        current_counts = Counter(identity(cue) for cue in current_rows)
        values = {}
        for saved in original_rows:
            if saved.get('translated', '').strip():
                key = identity(saved)
                if original_counts[key] != 1 or current_counts[key] > 1:
                    raise ValueError('Câu dịch có sẵn trong bản chụp bị trùng sau chỉnh sửa; cần kiểm tra thủ công.')
                values[key] = saved['translated'].strip()
        preexisting = [(index, values[identity(cue)]) for index, cue in enumerate(current_rows)
                       if identity(cue) in values]
    for index, translated in preexisting + completed:
        if (snapshot['segments'][index].get('user_edited') is not True and
                not snapshot['segments'][index].get('translated', '').strip()):
            snapshot['segments'][index]['translated'] = translated
            restored += 1
    selected = row.get('segment_indices')
    if selected is not None:
        identity = lambda cue: (cue.get('start'), cue.get('end'), cue.get('text'))
        if [identity(cue) for cue in original.get('segments', [])] != [identity(cue) for cue in project.get('segments', [])]:
            raise ValueError('Phạm vi dịch đã chọn đổi vị trí/mốc. Chọn lại các câu cần dịch; không mở rộng ngầm.')
    missing = [index for index, cue in enumerate(snapshot['segments'])
               if not cue.get('translated', '').strip() and (selected is None or index in selected)]
    # A fully verified translation must not ask for a key or call the provider
    # again merely because a later TTS/encode step was interrupted.
    if snapshot['params'].get('pipeline_translate', True) and not missing and not _pending_selection(snapshot):
        snapshot['params']['pipeline_translate'] = False
    if (Path(folder)/'stage-journal.json').is_file():
        from stage_journal import recover
        snapshot, _ = recover(snapshot, folder)
    snapshot, replay_restored = _replay_failed_lineage(root, row, snapshot, jobs, folder)
    restored += replay_restored
    missing = [index for index, cue in enumerate(snapshot['segments'])
               if not cue.get('translated', '').strip() and (selected is None or index in selected)]
    if not missing and not _pending_selection(snapshot):
        snapshot['params']['pipeline_translate'] = False
    return snapshot, root, restored, missing, selected


def journal_recovery_snapshot(row, project, folder, *, original=None):
    """Recover an original failed pipeline, including verified stage outputs."""
    from subtitle_quality import source_signature
    original = original or read_input(folder, row, project)
    snapshot, completed = copy.deepcopy(project), []
    if (Path(folder)/'stage-journal.json').is_file():
        from stage_journal import recover
        snapshot, completed = recover(snapshot, folder)
    elif original['params'].get('pipeline_extract', 'existing') != 'existing':
        checkpoint = Path(folder)/'translation-checkpoint.json'
        if not checkpoint.is_file():
            if original['params'].get('pipeline_extract') != 'ocr':
                raise ValueError('Nhận dạng chưa hoàn tất. Mở Chi tiết và tiếp tục checkpoint OCR/ASR; không tự nhận dạng lại.')
            snapshot = _complete_ocr_snapshot(row, project, original, folder)
            completed.append('ocr')
        else:
            from translation_checkpoint import read_checkpoint, validate_pipeline_resume
            recovered = validate_pipeline_resume(read_checkpoint(checkpoint), original, row.get('segment_indices'),
                overwrite_user_edits=row.get('overwrite_user_edits') is True)
            if project.get('segments', []) != original.get('segments', []):
                raise ValueError('Bảng đã được chỉnh sau nhận dạng; áp dụng checkpoint có kiểm tra thay vì ghi đè.')
            snapshot['segments'], snapshot['coverage'] = recovered['segments'], recovered.get('coverage', {})
            completed.append(original['params']['pipeline_extract'])
    if not snapshot.get('segments'):
        raise ValueError('Chưa có bảng phụ đề đã xác minh để tiếp tục các bước sau.')
    if (snapshot.get('coverage') or {}).get('source_signature') != source_signature(project):
        raise ValueError('Thiếu bằng chứng bảng phụ đề thuộc đúng nguồn hiện tại; kiểm tra bảng trước khi tiếp tục.')
    if (original['params'].get('pipeline_extract', 'existing') in ('ocr', 'asr') and
            not any(stage in completed for stage in ('ocr', 'asr')) and not project.get('segments')):
        raise ValueError('Nhận dạng chưa có stage đã kiểm chứng để dùng lại.')
    snapshot['params']['pipeline_extract'] = 'existing'
    snapshot['params']['translation_scope'] = 'missing'
    if all(str(cue.get('translated') or '').strip() for cue in snapshot['segments']) and not _pending_selection(snapshot):
        snapshot['params']['pipeline_translate'] = False
    return snapshot, completed


def _complete_ocr_snapshot(row, project, original, folder):
    """Recover pre-journal RC2 jobs whose decoder finished before cloud failed.

    EOF, the source/config signature, complete track state and its raw log must
    agree. Only a private proposal is constructed; no frame/model/provider is
    touched and no user table or evidence file is written.
    """
    import re
    from ocr_checkpoint import validate_resume, signature, _bounds, _finite_tree, _object, _bad_constant
    from ocr_tracks import read_manifest, auto_selection, rows_from_selection, _validate_state, _text_key
    from subtitle_quality import coverage_record, digest
    from video_sampling import sample_end, sample_tail_tolerance
    base = Path(folder)
    identifier = row.get('id')
    if (not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{32}', identifier) or
            base.name != identifier or base.is_symlink()):
        raise ValueError('Thư mục chứng cứ OCR không thuộc tác vụ cần tiếp tục.')
    for name in ('recognition-checkpoint.json', 'ocr-tracks.json', 'ocr-observations.jsonl', 'source.srt'):
        path = base/name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != base.resolve():
            raise ValueError('Thiếu chứng cứ OCR hoàn chỉnh thuộc tác vụ; giữ dữ liệu để khôi phục.')
    saved = validate_resume(base/'recognition-checkpoint.json', project,
                            completed_legacy_original=original)
    legacy = saved.get('signature') != signature(project)
    # Recheck original configuration too; current output/font choices may
    # change but a different OCR rectangle/rate cannot claim these tracks.
    if not legacy and saved.get('signature') != signature(original):
        raise ValueError('Cấu hình OCR đã đổi so với bản chụp gốc; không tự dùng lớp cũ.')
    start, end, interval = _bounds(project)
    end = sample_end(project['media'], end)
    if not saved.get('decoded_complete') or saved['next_time'] < end-sample_tail_tolerance(project['media'], interval):
        raise ValueError('Nhận dạng chưa xác nhận giải mã hoàn tất. Tiếp tục checkpoint OCR; không tự nhận dạng lại.')
    state = saved.get('ocr_tracks')
    _validate_state(state, project['media']['width'], project['media']['height'])
    if state.get('legacy_prefix'):
        raise ValueError('Checkpoint có phần chữ chưa được theo dõi bằng chứng; cần kiểm tra trước khi tự chọn.')
    document = read_manifest(base/'ocr-tracks.json', project)
    if (document.get('checkpoint_signature') != saved['signature'] or
            document.get('tracks') != state['tracks'] or
            document.get('samples_processed') != state['samples_processed'] or
            document.get('legacy_prefix_rows') != 0 or
            document.get('sampling_interval') != interval or
            document.get('clipped_samples', []) != saved.get('clipped_samples', []) or
            any(abs(a-b) > 1e-5 for a, b in zip(document['range'], (start,end)))):
        raise ValueError('Lớp chữ không khớp checkpoint OCR hoàn tất; giữ chứng cứ để khôi phục.')
    evidence = base/'ocr-observations.jsonl'
    size = evidence.stat().st_size
    if not 0 < size == state['evidence_bytes'] <= 256*1024**2:
        raise ValueError('Nhật ký OCR thiếu hoặc không khớp số byte đã xác nhận.')
    tracks = {track['id']: track for track in state['tracks']}
    cursors, counts = {key:0 for key in tracks}, {key:0 for key in tracks}
    observed = 0
    try:
        with evidence.open('rb') as stream:
            while encoded := stream.readline(2*1024**2 + 1):
                if len(encoded) > 2*1024**2 or not encoded.endswith(b'\n'):
                    raise ValueError('Nhật ký mẫu OCR chưa ghi hoàn tất.')
                sample = json.loads(encoded.decode('utf-8'), object_pairs_hook=_object, parse_constant=_bad_constant)
                _finite_tree(sample)
                if (not isinstance(sample, dict) or type(sample.get('sample_id')) is not int or
                        sample['sample_id'] != observed or type(sample.get('time')) not in (int,float) or
                        abs(sample['time']-(start+observed*interval)) > 1e-5 or
                        not start <= sample['time'] < end):
                    raise ValueError('Thứ tự/mốc mẫu OCR không khớp phạm vi đã xác nhận.')
                observations, components = sample.get('observations'), sample.get('components')
                if not isinstance(observations, list) or not isinstance(components, list):
                    raise ValueError('Mẫu OCR thiếu bằng chứng chữ hoặc lớp chữ.')
                observation_ids = {item['id'] for item in observations if isinstance(item, dict) and isinstance(item.get('id'), str)}
                if len(observation_ids) != len(observations):
                    raise ValueError('Danh tính chữ OCR thiếu hoặc bị lặp trong mẫu.')
                used = set()
                for component in components:
                    key = component.get('track_id') if isinstance(component, dict) else None
                    if key not in tracks or key in used:
                        raise ValueError('Lớp mẫu OCR không có hoặc bị lặp trong checkpoint.')
                    used.add(key)
                    ids = component.get('observation_ids')
                    if not isinstance(ids, list) or not ids or any(item not in observation_ids for item in ids):
                        raise ValueError('Lớp OCR tham chiếu chữ không có trong mẫu gốc.')
                    runs, cursor = tracks[key]['runs'], cursors[key]
                    while cursor < len(runs) and runs[cursor]['last_sample'] < observed:
                        cursor += 1
                    cursors[key] = cursor
                    if cursor >= len(runs):
                        raise ValueError('Lớp OCR chứa mẫu ngoài mốc checkpoint.')
                    run = runs[cursor]
                    if (not run['first_sample'] <= observed <= run['last_sample'] or
                            not isinstance(component.get('text'), str) or _text_key(component['text']) != run['text'] or
                            observed == run['first_sample'] and abs(run['start']-sample['time']) > 1e-5 or
                            observed == run['last_sample'] and (abs(run['end']-min(end,sample['time']+interval)) > 1e-5 or
                                component.get('bbox') != run['bbox'])):
                        raise ValueError('Nội dung hoặc mốc lớp OCR không khớp nhật ký mẫu gốc.')
                    counts[key] += 1
                observed += 1
    except (UnicodeError, json.JSONDecodeError, TypeError, KeyError, RecursionError) as exc:
        raise ValueError('Nhật ký OCR không hợp lệ; giữ dữ liệu để khôi phục.') from exc
    if (observed != state['samples_processed'] or
            any(counts[key] != track['samples'] for key, track in tracks.items()) or
            abs(saved['next_time']-(start+observed*interval)) > 1e-5):
        raise ValueError('Nhật ký OCR không đủ các mẫu checkpoint đã xác nhận.')
    selection = auto_selection(document, project)
    rows = rows_from_selection(document, project, selection) if selection.get('selected_tracks') else []
    if not rows:
        raise ValueError('Chứng cứ OCR hoàn tất nhưng chưa có lớp phụ đề để tiếp tục. Mở tự chọn phụ đề.')
    identities = lambda values: [(cue.get('start'), cue.get('end'), cue.get('text')) for cue in values]
    if project.get('segments', []) == original.get('segments', []):
        pass
    elif identities(project.get('segments', [])) == identities(rows):
        rows = copy.deepcopy(project['segments'])
    else:
        raise ValueError('Bảng đã được chỉnh sau nhận dạng; áp dụng checkpoint có kiểm tra thay vì ghi đè.')
    selection = dict(selection, automation_mode='local', job_id=identifier, manifest_key=digest(document))
    coverage = coverage_record(project, 'ocr', start, end, sampling_interval=interval,
        clipped_samples=saved.get('clipped_samples', []), ocr_selection=selection)
    if legacy:
        # Preserve the applied table/review evidence when its exact OCR rows
        # agree. Do not relabel old bottom-band evidence as a full-frame scan.
        applied = project.get('coverage') or {}
        if (identities(project.get('segments', [])) == identities(rows) and
                applied.get('source_signature') == coverage['source_signature'] and
                applied.get('method') == 'ocr'):
            coverage = copy.deepcopy(applied)
        coverage['recognition_provenance'] = {
            'checkpoint_version':4, 'checkpoint_signature':saved['signature'],
            'job_id':identifier, 'source_revision':project.get('source_revision'),
            'search_area':'explicit_regions_v4' if original.get('extraction_regions', original['regions'])
                          else 'legacy_bottom_40_percent_v4',
            'reused_completed_asset':True, 'full_frame_recognition':False}
    snapshot = copy.deepcopy(project)
    snapshot.update(segments=rows, coverage=coverage)
    snapshot.setdefault('artifacts', {})['ocr'] = {
        'type':'ocr', 'job_id':identifier, 'files':['source.srt','ocr-observations.jsonl','ocr-tracks.json'],
        'coverage':copy.deepcopy(coverage), 'checkpoint_complete':True, 'recognition_count':len(rows)}
    return snapshot


def _failed_root_status(row, snapshot, jobs, folder):
    from preflight import inspect
    from subtitle_quality import inspect_subtitles, speech_ready, clean_ready
    from processing_plan import content_requirements
    from stage_journal import recover
    completed = []
    if (Path(folder)/'stage-journal.json').is_file():
        _, completed = recover(snapshot, folder)
    elif (snapshot.get('artifacts', {}).get('ocr') or {}).get('checkpoint_complete'):
        completed.append('ocr')
    from subtitle_quality import source_signature
    for kind in ('ocr','asr'):
        artifact = (snapshot.get('artifacts') or {}).get(kind) or {}
        coverage = artifact.get('coverage') or {}
        if (kind not in completed and artifact.get('job_id') and
                (artifact.get('checkpoint_complete') or artifact.get('verification')) and
                coverage.get('source_signature') == source_signature(snapshot)):
            completed.append(kind)
    quality = inspect_subtitles(snapshot)
    checks = inspect(snapshot, 'pipeline', check_credentials=False)
    from auto_subtitle_director import automation_status
    auto_pending = bool(automation_status(snapshot).get('can_process'))
    errors = list(dict.fromkeys(checks['errors'] + quality['errors']))
    active = any(item.get('id') != row['id'] and item.get('project_id') == snapshot['id'] and
                 item.get('state') in ('queued', 'running') for item in jobs)
    if active:
        errors.append('Dự án đang có tác vụ khác; chờ hoặc hủy trước khi tiếp tục.')
    remaining = []
    p = snapshot['params']
    if auto_pending:
        remaining.append('auto_subtitles')
    if content_requirements(snapshot)['will_translate'] or auto_pending and p.get('pipeline_translate', True):
        remaining.append('translate')
    if p.get('pipeline_tts', True) and not (not p.get('pipeline_translate') and speech_ready(snapshot)):
        remaining.append('tts')
    if p.get('pipeline_clean', False) and not clean_ready(snapshot):
        remaining.append('clean')
    remaining.append('render')
    requires_review = quality['requires_review'] and not auto_pending
    ready = not errors and not requires_review
    return {'can_continue': ready, 'completed_steps': completed, 'remaining_steps': remaining,
            'requires_review': requires_review, 'errors': errors,
            'warnings': list(dict.fromkeys(checks['warnings'] + [i['message'] for i in quality['issues']
                                          if i['severity'] != 'error'])),
            'delivery': checks.get('delivery'),
            'reason': 'Dùng lại các bước đã kiểm chứng và chạy phần còn thiếu.' if ready else
                      errors[0] if errors else 'Cần kiểm tra phạm vi phụ đề trước khi tiếp tục.',
            'next_action': ('continue_auto_subtitles' if auto_pending else 'continue_pipeline') if ready else 'edit_speech_timing' if (checks.get('delivery') or {}).get('issues')
                           else 'review_subtitles_then_resume' if requires_review else 'fix_configuration',
            'recognition_complete': 'ocr' in completed or 'asr' in completed,
            'recognition_count': len(snapshot.get('segments', []))}


def failed_continuation_status(row, project, jobs, folder):
    plan = {'job_id': row['id'], 'project_id': project['id'], 'expected_revision': project['updated'],
            'can_continue': False, 'completed_steps': [], 'remaining_steps': [], 'requires_review': False,
            'errors': [], 'warnings': [], 'reason': '', 'next_action': 'inspect_pipeline',
            'recovery_job_id': row['id'], 'reused_translation_count': 0,
            'restored_translation_count': 0}
    try:
        snapshot, root, restored, missing, selected = recovery_snapshot(row, project, jobs, folder)
        if root.get('state') == 'review':
            plan = continuation_status(root, snapshot, jobs, Path(folder).parent/root['id'], allow_recovery=False)
        else:
            plan.update(_failed_root_status(row, snapshot, jobs, folder))
        # Recovery fills only blanks, so its insertion count can be zero even
        # when the editor already retains every completed batch. Report the
        # usable translations in the same scope as ``missing``, counted once
        # per cue after checkpoint/input validation and the manual-edit merge.
        scope = None if selected is None else set(selected)
        reused = sum(bool(cue.get('translated', '').strip())
                     for index, cue in enumerate(snapshot['segments'])
                     if scope is None or index in scope)
        plan.update(job_id=row['id'], recovery_job_id=row['id'], continuation_from=root['id'],
                    reused_translation_count=reused, restored_translation_count=restored,
                    missing_translation_count=len(missing), last_error=row.get('message', ''))
        if row.get('message'):
            plan['warnings'] = list(dict.fromkeys(plan['warnings'] + [row['message']]))
        if not missing and project['params'].get('pipeline_translate', True) and 'auto_subtitles' not in plan['remaining_steps']:
            plan['completed_steps'] = list(dict.fromkeys(plan['completed_steps'] + ['translate']))
        if plan['can_continue']:
            if 'auto_subtitles' in plan.get('remaining_steps', []):
                plan.update(reason='Đọc chữ đã hoàn tất và được lưu. Tiếp tục tự xử lý phụ đề từ chứng cứ hiện có, không đọc lại video.',
                            next_action='continue_auto_subtitles')
            else:
                plan.update(reason=f'Dùng lại {reused} câu đã dịch và giữ các sửa tay; chỉ chạy các bước còn thiếu, không nhận dạng lại.',
                            next_action='continue_pipeline')
    except (ValueError, OSError, KeyError, TypeError) as exc:
        plan['reason'] = str(exc)
    return plan


def prepare_continuation(row, project, jobs, folder):
    """Return private work plus its root/recovery IDs after a read-only plan."""
    if (row.get('result') or {}).get('review_stage') == 'pre_render' and row.get('state') == 'review':
        snapshot = prepared_review_snapshot(row, project, folder)
        require_pre_render_approval(snapshot)
        return pre_render_continuation_snapshot(snapshot), row['id'], None, None
    if row.get('state') in ('error', 'cancelled') and not row.get('continuation_from'):
        failed = row
    elif row.get('continuation_from') and row.get('state') in ('error', 'cancelled'):
        failed = latest_failed_successor({'id':row['continuation_from']}, project, jobs) or row
    else:
        failed = latest_failed_successor(row, project, jobs)
    if failed:
        failed_folder = Path(folder) if failed['id'] == row['id'] else Path(folder).parent/failed['id']
        snapshot, root, restored, missing, selected = recovery_snapshot(failed, project, jobs, failed_folder)
        return snapshot, root['id'], failed['id'], selected
    return continuation_snapshot(project), row['id'], None, None
