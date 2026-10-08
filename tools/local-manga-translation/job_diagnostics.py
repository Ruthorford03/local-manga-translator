"""Read bounded job metadata for the GUI; never load image or translated text data.

Call on issue events/reload, not on the elapsed-time timer. Paths in saved JSON
are untrusted: only existing files under this job (or its source folder) are
returned. A job failure remains distinct from failures of individual pages.
"""
from collections import deque
import json
from pathlib import Path, PureWindowsPath

STAGE_LABELS = {
    'import': '匯入原圖', 'publish': '發布／保存工作資料', 'prepare': '前處理／辨識',
    'translate': '翻譯／審查', 'render': '排版', 'finalize': '完成／保存工作',
    'validate': '檢查工作資料', 'unknown': '階段不明',
}
REASON_LABELS = {
    'line_alignment_recovery': '逐框補救完成，仍需人工審查後才可輸出。',
}
_JSON_LIMIT = 4 * 1024 * 1024
_LOG_LIMIT = 96 * 1024
_IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff', '.gif'}
_STAGES = {'detect-ocr-inpaint': 'prepare', 'detect_ocr_inpaint': 'prepare',
           'magi': 'prepare', 'ocr': 'prepare', 'detect': 'prepare', 'inpaint': 'prepare',
           'translation': 'translate', **{name: name for name in STAGE_LABELS}}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return value if isinstance(value, str) else ''


def _leaf(value):
    return (isinstance(value, str) and bool(value) and value not in ('.', '..')
            and Path(value).name == value and PureWindowsPath(value).name == value
            and ':' not in value and '/' not in value and '\\' not in value)


def _stage(value):
    return _STAGES.get(value, 'unknown') if isinstance(value, str) else 'unknown'


class _Reader:
    def __init__(self, root):
        self.root = root
        self.warnings = []

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def path(self, value, *, base=None, root=None, exists=True):
        if not isinstance(value, (str, Path)) or not str(value):
            return None
        try:
            candidate = Path(value)
            candidate = candidate if candidate.is_absolute() else (base or self.root) / candidate
            candidate = candidate.resolve()
            if not candidate.is_relative_to(root or self.root):
                self.warn('略過範圍外路徑：' + str(value))
                return None
            return candidate if not exists or candidate.is_file() else None
        except (OSError, ValueError, RuntimeError):
            self.warn('無法安全定位路徑：' + str(value))
            return None

    def read(self, path, expected=dict, required=False):
        target = self.path(path)
        if target is None:
            if required:
                self.warn('找不到可讀取的工作資料：' + str(path))
            return expected()

        try:
            with target.open('rb') as stream:
                raw = stream.read(_JSON_LIMIT + 1)
            if len(raw) > _JSON_LIMIT:
                raise ValueError('JSON 超過讀取上限')
            value = json.loads(raw.decode('utf-8-sig'))
            if not isinstance(value, expected):
                raise ValueError('JSON 結構不符')
            return value
        except (OSError, ValueError, UnicodeError, RecursionError) as error:
            self.warn(f'工作資料讀取失敗：{target}（{type(error).__name__}: {error}）')
            return expected()

    def metadata_link(self, value, *, base=None):
        target = self.path(value, base=base)
        if target and target.suffix.lower() not in ('.json', '.log'):
            self.warn('略過非診斷資料路徑：' + str(target))
            return None
        return target

    def events(self, path):
        target = self.path(path)
        if target is None:
            return []
        try:
            with target.open('rb') as stream:
                stream.seek(0, 2)
                start = max(0, stream.tell() - _LOG_LIMIT)
                stream.seek(start)
                raw = stream.read(_LOG_LIMIT)
            lines = raw.decode('utf-8-sig', errors='replace').splitlines()
            if start and lines:
                lines = lines[1:]
            events = []
            for line in lines:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict) and isinstance(value.get('event'), str):
                        events.append(value)
                except (ValueError, RecursionError):
                    pass
            return events[-512:]
        except OSError as error:
            self.warn(f'紀錄尾端讀取失敗：{target}（{type(error).__name__}）')
            return []


def _stage_from_error(error):
    context = _dict(error.get('error_context'))
    stage = _stage(context.get('stage') or error.get('stage'))
    if stage != 'unknown':
        return stage, '保存的 error_context.stage' if context else '保存的錯誤 stage'
    evidence = (_text(context.get('path')) + ' ' + _text(context.get('operation')) + ' '
                + _text(error.get('message'))).lower()
    if 'file-map.json' in evidence:
        return 'publish', '錯誤指向 file-map.json（發布／保存工作資料）'
    for token, stage in (('render.log', 'render'), ('detect-ocr-inpaint', 'prepare'),
                         ('magi-run', 'prepare'), ('magi.log', 'prepare'),
                         ('translation.log', 'translate'), ('status.json', 'finalize')):
        if token in evidence:
            return stage, '依保存的錯誤路徑／操作推斷：' + token
    return 'unknown', '沒有可確認的階段紀錄'


def collect_diagnostics(output, record=None, live_events=(), names=None):
    """Return issues, job_error, read_warnings, output_path and log_path.

    Rows have stable ``key/kind/normalized_name/source_name/page_index/batch/
    stage/stage_evidence/type/reason/message/log_path/details_path/source_path``.
    Unknown names/numbers/paths are None. ``names`` optionally supplies a
    normalized-name -> original-name mapping while a new job is being imported.
    No directory recursion, model imports, or writes occur here.
    """
    root = Path(output).resolve()
    reader = _Reader(root)
    work = root / '_工作資料'
    status_path, mapping_path = work / 'status.json', work / 'file-map.json'
    saved = reader.read(status_path, required=record is None)
    # An explicit GUI snapshot is authoritative, including deliberately absent
    # errors after a fresh/resumed run. Never merge an old saved error into it.
    state = dict(record) if isinstance(record, dict) else saved
    mapping = reader.read(mapping_path, list, required=True)
    schedule_path = work / 'scheduled/schedule.json'
    schedule = reader.read(schedule_path)
    log = reader.path(work / 'pipeline.log')
    disk_events = reader.events(log) if log else []
    current_events = [value for value in deque(live_events or (), maxlen=512) if isinstance(value, dict)]
    events = disk_events + current_events
    source = root.parent
    declared_source = state.get('source', saved.get('source'))
    if declared_source:
        try:
            if Path(declared_source).resolve() != source:
                reader.warn('原圖資料夾與工作位置不一致，停用原圖路徑。')
                source = None
        except (OSError, ValueError, TypeError, RuntimeError):
            source = None
            reader.warn('原圖資料夾紀錄無效，停用原圖路徑。')
    mapped = {}
    for index, entry in enumerate(mapping, 1):
        item = _dict(entry)
        name, original = item.get('normalized_name'), item.get('source_name')
        if not _leaf(name) or not _leaf(original):
            reader.warn('略過無效檔名對照（第 ' + str(index) + ' 筆）。')
            continue
        if name in mapped:
            reader.warn('檔名對照重複：' + name)
            mapped[name] = (None, None)
        else:
            mapped[name] = (original, index)
    for name, original in _dict(names).items():
        if _leaf(name) and _leaf(original) and name not in mapped:
            mapped[name] = (original, None)

    failures, evidence, pending, chunks, verified_completed = {}, {}, {}, {}, set()
    sources = [(state, status_path)] if isinstance(record, dict) else [(schedule, schedule_path), (state, status_path)]
    for data, path in sources:
        for name, error in _dict(data.get('failed_pages')).items():
            if _leaf(name):
                failures[name], evidence[name] = dict(_dict(error)), path
    chunk_list = schedule.get('chunks', [])
    if not isinstance(chunk_list, list):
        reader.warn('批次清單結構不符。')
        chunk_list = []
    event_batches = {str(event.get('batch')) for event in events if event.get('event') in ('page_failed', 'page_review_required')}
    for item in chunk_list[:2048]:
        chunk = _dict(item)
        pages = chunk.get('pages', [])
        if not isinstance(pages, list):
            reader.warn('批次頁面清單結構不符。')
            continue
        batch = chunk.get('id') if isinstance(chunk.get('id'), int) else None
        attempts = chunk.get('attempts', [])
        location = reader.path(attempts[-1], base=work / 'scheduled', exists=False) if isinstance(attempts, list) and attempts else None
        for name in pages:
            if _leaf(name):
                chunks[name] = (batch, location)
        selected = (any(name in failures for name in pages if isinstance(name, str))
                    or chunk.get('status') in ('partial', 'failed') or str(batch) in event_batches)
        if not selected or location is None:
            continue
        verification_path = location / 'verification.json'
        verification = reader.read(verification_path, required=True)
        for name in verification.get('completed_pages', []) if isinstance(verification.get('completed_pages'), list) else []:
            if _leaf(name):
                failures.pop(name, None)
                verified_completed.add(name)
        for name, error in _dict(verification.get('failed_pages')).items():
            if _leaf(name):
                failures[name], evidence[name] = dict(_dict(error)), verification_path
        for name, data in _dict(verification.get('review_required_pages')).items():
            if _leaf(name):
                pending[name] = (dict(_dict(data)), location)
                failures[name] = dict(failures.get(name, {}), type='ReviewRequired', reason=_dict(data).get('reason'))
                evidence[name] = verification_path
    if not chunk_list:
        legacy_path = work / 'run/verification.json'
        legacy = reader.read(legacy_path)
        for name in legacy.get('completed_pages', []) if isinstance(legacy.get('completed_pages'), list) else []:
            if _leaf(name):
                failures.pop(name, None)
                verified_completed.add(name)
        for name, error in _dict(legacy.get('failed_pages')).items():
            if _leaf(name):
                failures[name], evidence[name] = dict(_dict(error)), legacy_path
                chunks[name] = (None, legacy_path.parent)
        for name, data in _dict(legacy.get('review_required_pages')).items():
            if _leaf(name):
                pending[name] = (dict(_dict(data)), legacy_path.parent)

    active_stages, failed_stages, event_context = {}, {}, {}
    live_job, last_batch = None, None
    for event, is_live in [(event, False) for event in disk_events] + [(event, True) for event in current_events]:
        event_name, batch = event.get('event'), event.get('batch')
        token = (str(batch), _text(event.get('worker')))
        if event_name == 'folder_started':
            active_stages.clear()
            failed_stages.clear()
            live_job, last_batch = None, None
        elif event_name == 'chunk_ready' and isinstance(batch, int):
            last_batch = batch
            for key in list(failed_stages):
                if key[0] == str(batch):
                    failed_stages.pop(key)
        elif event_name == 'stage_started':
            active_stages[token] = (_stage(event.get('stage')), batch)
            failed_stages.pop(token, None)
        elif event_name == 'stage_finished':
            active_stages.pop(token, None)
            if isinstance(event.get('returncode'), int) and event['returncode'] != 0:
                failed_stages[token] = (_stage(event.get('stage')), batch)
            else:
                failed_stages.pop(token, None)
        elif event_name in ('page_failed', 'page_review_required'):
            name = event.get('page')
            if not _leaf(name):
                reader.warn('頁面事件缺少安全的 page 欄位。')
                continue
            if not is_live and (name in verified_completed or state.get('status') == 'completed'):
                continue
            error = dict(failures.get(name, {}))
            error.setdefault('type', event.get('type') or event.get('error_type') or ('ReviewRequired' if event_name == 'page_review_required' else 'PageFailure'))
            for key in ('message', 'reason', 'error_context'):
                if event.get(key) and (key != 'reason' or not error.get(key)):
                    error[key] = event[key]
            failures[name] = error
            event_stage = active_stages.get(token, ('translate' if event.get('worker') == 'translate' else 'unknown', batch))[0]
            event_context[name] = {'batch': batch, 'stage': event_stage, 'event': event_name}
        elif event_name == 'folder_failed' and (is_live or state.get('status') in ('failed', 'cancelled')):
            live_job = event

    def row(name, kind, error, details=None):
        original, index = mapped.get(name, (None, None))
        if name is not None and original is None:
            reader.warn('缺少原檔名對照：' + str(name))
        batch, location = chunks.get(name, (None, None))
        context = _dict(error.get('error_context'))
        if isinstance(context.get('batch'), int):
            batch = context['batch']
        if name in event_context and isinstance(event_context[name].get('batch'), int):
            batch = event_context[name]['batch']
        stage, stage_evidence = _stage_from_error(error)
        reason = _text(error.get('reason')) or None
        if kind == 'review' and reason == 'line_alignment_recovery':
            stage, stage_evidence = 'translate', '保存的 review reason: line_alignment_recovery'
        elif stage == 'unknown' and name in event_context:
            stage = event_context[name]['stage']
            stage_evidence = '同頁面／批次的 ' + event_context[name]['event'] + ' 事件'
        detail = reader.metadata_link(details) if details else None
        if name in pending:
            data, base = pending[name]
            detail = reader.metadata_link(data.get('pending_path'), base=base) or detail
        diagnostic_log = log
        if location and stage in ('render', 'prepare'):
            filename = 'render.log' if stage == 'render' else 'detect-ocr-inpaint.log'
            diagnostic_log = reader.path(location / '_audit' / filename) or log
        source_path = reader.path(original, base=source, root=source) if source and original else None
        if source_path and source_path.suffix.lower() not in _IMAGE_SUFFIXES:
            reader.warn('略過非圖片原檔連結：' + str(source_path))
            source_path = None
        message = _text(error.get('message')) or REASON_LABELS.get(reason, reason) or ('等待人工審查。' if kind == 'review' else '沒有保存詳細錯誤。')
        return {'key': kind + ':' + (name or 'job'), 'kind': kind, 'normalized_name': name,
                'source_name': original, 'page_index': index, 'batch': batch, 'stage': stage,
                'stage_evidence': stage_evidence, 'type': _text(error.get('type')) or None,
                'reason': reason, 'message': message, 'log_path': str(diagnostic_log) if diagnostic_log else None,
                'details_path': str(detail) if detail else None, 'source_path': str(source_path) if source_path else None,
                'error_context': context}

    issues = [row(name, 'review' if error.get('type') == 'ReviewRequired' else 'page_failure', error, evidence.get(name))
              for name, error in sorted(failures.items())]
    error = None
    if state.get('error') or state.get('error_context'):
        error = {'type': state.get('error_type'), 'message': state.get('error'), 'error_context': state.get('error_context')}
    elif schedule.get('error') and not isinstance(record, dict) and state.get('status') not in ('running', 'completed'):
        error = {'type': schedule.get('error_type'), 'message': schedule.get('error'), 'error_context': schedule.get('error_context')}
    if live_job and (live_job.get('message') or live_job.get('error_context')):
        error = dict(error or {}, **{key: live_job.get(key) for key in ('message', 'error_context') if live_job.get(key)})
        error['type'] = live_job.get('error_type') or live_job.get('type') or error.get('type')
    job_error = None
    if error:
        context = _dict(error.get('error_context'))
        error['type'] = error.get('type') or context.get('type')
        error['message'] = error.get('message') or context.get('message')
        if state.get('status') == 'cancelled' or error['type'] == 'InterruptedError' or (live_job and live_job.get('cancelled')):
            error = None
    if error:
        job_error = row(None, 'job_failure', error, status_path)
        if job_error['stage'] == 'unknown':
            candidates = list(failed_stages.values()) or list(active_stages.values())
            if len(candidates) == 1 and candidates[0][0] != 'unknown':
                job_error['stage'], job_error['batch'] = candidates[0]
                job_error['stage_evidence'] = ('stage_finished 事件記錄非零結束碼' if failed_stages
                                               else '唯一尚未結束的 stage_started 事件；非精確失敗頁')
        job_error['last_batch'] = last_batch
        job_error['batch_evidence'] = ('保存的 error_context.batch' if isinstance(context.get('batch'), int)
                                       else '最後 chunk_ready 事件；不是精確失敗頁' if last_batch is not None
                                       else '沒有可確認的批次紀錄')
        job_error['secondary_errors'] = state.get('secondary_errors') if isinstance(state.get('secondary_errors'), list) else []
        issues.append(job_error)
    return {'issues': issues, 'job_error': job_error, 'read_warnings': reader.warnings,
            'output_path': str(root), 'log_path': str(log) if log else None}
