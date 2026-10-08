"""Local folder picker and safe image import/export around the established manga pipeline."""
import argparse
from collections import Counter
from contextlib import contextmanager
import csv
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import traceback
import uuid

from PIL import Image, ImageOps

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from atomic_json import save_json as save
from job_diagnostics import collect_diagnostics, STAGE_LABELS
EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff', '.gif'}


def exception_details(error, context=None):
    details = dict(context or {})
    details.update(getattr(error, 'manga_context', {}))
    details.update(type=type(error).__name__, message=str(error),
                   traceback=''.join(traceback.format_exception(type(error), error, error.__traceback__)),
                   winerror=getattr(error, 'winerror', None), errno=getattr(error, 'errno', None))
    for key in ('filename', 'filename2'):
        if getattr(error, key, None):
            details[key] = str(getattr(error, key))
    details.setdefault('path', details.get('filename2') or details.get('filename'))
    details.setdefault('stage', 'unknown')
    return details


def record_failure(work, record, error, context, *, secondary=False):
    details = exception_details(error, context)
    if secondary:
        record.setdefault('secondary_errors', []).append(details)
    else:
        record.update(status='cancelled' if isinstance(error, InterruptedError) else 'failed',
                      error_type=type(error).__name__, error=str(error), error_context=details)
    # The pipeline log belongs to the child. Keep parent publication/cleanup
    # exceptions in a separate append-only log, including a full traceback.
    try:
        with (work / 'diagnostics.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'time': local_now().isoformat(timespec='seconds'),
                                     'secondary': secondary, **details}, ensure_ascii=False) + '\n')
    except OSError as log_error:
        record.setdefault('diagnostic_warnings', []).append('無法保存診斷紀錄：' + str(log_error))


def finalize_progress(work, record, started, primary_error=None, *, source=None, mapping=None):
    """Save the terminal record without replacing the error that stopped work."""
    def capture(error, context):
        nonlocal primary_error
        record_failure(work, record, error, context, secondary=primary_error is not None)
        if primary_error is None:
            primary_error = error

    if source is not None and mapping is not None:
        try:
            unchanged = all((source / item['source_name']).is_file() and
                            sha(source / item['source_name']) == item['source_sha256'] for item in mapping)
            record['source_modified'] = not unchanged
            if not unchanged:
                raise ValueError('原圖在處理期間被更改，請查看工作資料。')
        except BaseException as error:
            capture(error, {'stage': 'validate', 'operation': 'verify_source'})
    if mapping is not None:
        try:
            save(work / 'file-map.json', mapping)
        except BaseException as error:
            capture(error, {'stage': 'finalize', 'operation': 'save_file_map', 'path': str(work / 'file-map.json')})
    progress = update_progress(record, started, finished=True)
    try:
        save(work / 'status.json', record)
    except BaseException as error:
        capture(error, {'stage': 'finalize', 'operation': 'save_status', 'path': str(work / 'status.json')})
        progress = update_progress(record, started, finished=True)
    if record['status'] in ('failed', 'cancelled'):
        emit('folder_failed', cancelled=record['status'] == 'cancelled',
             message=record.get('error', '翻譯未完成'), error_context=record.get('error_context'),
             secondary_errors=record.get('secondary_errors', []), progress=progress)
        if primary_error is not None:
            primary_error.folder_reported = True
    return primary_error, progress


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def emit(event: str, **data) -> None:
    print(json.dumps({'event': event, **data}, ensure_ascii=False), flush=True)


def local_now() -> datetime:
    return datetime.now().astimezone()


def progress_snapshot(record, elapsed=None, now=None):
    """Describe published pages and this invocation's time, never OCR/model progress."""
    now = local_now() if now is None else now
    elapsed = max(0.0, float(record.get('elapsed_seconds', 0) if elapsed is None else elapsed))
    total = max(0, int(record.get('pages', 0)))
    complete = max(0, int(record.get('completed_pages', 0)))
    initial = max(0, int(record.get('initial_completed_pages', complete)))
    failures = record.get('failed_pages') or {}
    review_count = sum(error.get('type') == 'ReviewRequired' for error in failures.values())
    failed_count = len(failures) - review_count
    remaining, added = max(0, total - complete), max(0, complete - initial)
    estimated_remaining, estimated_at = None, None
    if record.get('status') == 'running' and added and remaining and elapsed > 0 and not failures:
        estimated_remaining = elapsed * remaining / added
        estimated_at = (now + timedelta(seconds=estimated_remaining)).isoformat(timespec='seconds')
    result = {'status': record.get('status', 'idle'), 'pages': total, 'completed_pages': complete,
            'initial_completed_pages': initial, 'newly_completed_pages': added,
            'elapsed_seconds': elapsed, 'started_at': record.get('started_at'),
            'ended_at': record.get('ended_at'), 'completed_at': record.get('completed_at'),
            'estimated_remaining_seconds': estimated_remaining, 'estimated_completion_at': estimated_at,
            'review_required_count': review_count, 'failed_page_count': failed_count,
            'failed_pages': failures}
    for key in ('error', 'error_type', 'error_context', 'secondary_errors', 'diagnostic_warnings', 'operation'):
        if key in record:
            result[key] = record[key]
    return result


def begin_progress(record, *, operation='translation'):
    started = time.monotonic()
    record.update(started_at=local_now().isoformat(timespec='seconds'), ended_at=None, completed_at=None,
                  elapsed_seconds=0.0, initial_completed_pages=record.get('completed_pages', 0),
                  operation=operation)
    for key in ('error_type', 'error', 'error_context', 'secondary_errors', 'diagnostic_warnings'):
        record.pop(key, None)
    record.update(progress_snapshot(record))
    return started


def update_progress(record, started, *, finished=False):
    now = local_now()
    if finished:
        record['ended_at'] = now.isoformat(timespec='seconds')
        record['completed_at'] = record['ended_at'] if record['status'] == 'completed' else None
    record.update(progress_snapshot(record, time.monotonic() - started, now))
    return progress_snapshot(record, now=now)


def duration_text(seconds):
    hours, rest = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(rest, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}'


def timestamp_text(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            return '無時區紀錄'
        return stamp.astimezone().strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return '無紀錄'


def natural_key(path: Path) -> tuple:
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold())
                 for part in re.split(r'(\d+)', path.name))


def source_images(source: Path) -> list[Path]:
    if not source.is_dir() or not source.name:
        raise ValueError('請選擇放有漫畫圖片的資料夾。')
    # Only this folder: never re-import a previous Chinese output or other subfolder.
    pages = sorted((path for path in source.iterdir()
                    if path.is_file() and path.suffix.casefold() in EXTENSIONS), key=natural_key)
    if not pages:
        subdirs = [p.name for p in source.iterdir() if p.is_dir() and not p.name.startswith(('.', '_')) and not p.name.endswith('(中文)')]
        if subdirs:
            raise ValueError(f'所選資料夾「{source.name}」第一層沒有圖片，但包含子資料夾（如 {subdirs[0]} 等）。請選擇包含圖檔的具體章節資料夾。')
        raise ValueError('這個資料夾沒有支援的圖片。請選擇直接放有 JPG、PNG、WebP 等圖片的資料夾。')
    return pages


def next_output(source: Path) -> Path:
    base = source / (source.name + '(中文)')
    candidate, number = base, 2
    while candidate.exists():
        candidate = source / (base.name + '_' + str(number))
        number += 1
    return candidate


def output_names(pages: list[Path]) -> dict[str, str]:
    stems = Counter(path.stem.casefold() for path in pages)
    names, used = {}, set()
    for path in pages:
        stem = path.stem if stems[path.stem.casefold()] == 1 else path.name
        name, number = stem + '.png', 2
        while name.casefold() in used:
            name = stem + '(' + str(number) + ').png'
            number += 1
        names[path.name] = name
        used.add(name.casefold())
    return names


def resume_candidate(source: Path) -> Path | None:
    candidates = []
    for directory in source.iterdir():
        status = directory / '_工作資料/status.json'
        if directory.is_dir() and not directory.is_symlink() and status.is_file():
            try:
                data = json.loads(status.read_text('utf-8'))
                if data.get('status') in ('failed', 'cancelled', 'partial', 'running'):
                    candidates.append((status.stat().st_mtime_ns, directory))
            except (OSError, ValueError):
                continue
    return max(candidates, default=(0, None))[1]


def validate_resume(source, pages, output):
    work = output / '_工作資料'
    mapping = json.loads((work / 'file-map.json').read_text('utf-8')) if (work / 'file-map.json').exists() else []
    expected_names = output_names(pages)
    if ([item['source_name'] for item in mapping] != [page.name for page in pages[:len(mapping)]] or
            len(mapping) > len(pages) or ((work / 'pages.json').exists() and len(mapping) != len(pages))):
        raise ValueError('原圖清單與上次工作不同，請按「開始翻譯」建立新工作。')
    for index, item in enumerate(mapping, 1):
        if item['normalized_name'] != f'{index:06d}.png' or item['output_name'] != expected_names[item['source_name']]:
            raise ValueError('工作檔名對照不一致，無法續跑。')
        if sha(source / item['source_name']) != item['source_sha256']:
            raise ValueError('原圖已更動，請建立新工作：' + item['source_name'])
        if sha(work / 'inputs' / item['normalized_name']) != item['normalized_sha256']:
            raise ValueError('工作圖片已更動，無法續跑：' + item['source_name'])
        destination = output / item['output_name']
        if item.get('output_sha256') and (not destination.is_file() or sha(destination) != item['output_sha256']):
            raise ValueError('中文成品已被修改或移走，請保留編輯並建立新工作：' + item['output_name'])
    return mapping


def publish_chunk(output, mapping, chunk):
    work = output / '_工作資料'
    if not chunk.resolve().is_relative_to((work / 'scheduled').resolve()):
        raise ValueError('批次輸出位置不一致。')
    from sfx_review import verify_completed_review
    verification = verify_completed_review(chunk)
    by_page = {item['normalized_name']: item for item in mapping}
    ledger = work / 'exported-translations.json'
    previous = json.loads(ledger.read_text('utf-8')) if ledger.exists() else {}
    translations = json.loads((chunk / 'translations.json').read_text('utf-8'))['items']
    for image in verification['images']:
        item = by_page[image['page']]
        translated = Path(image['path']).resolve()
        if not translated.is_relative_to(chunk.resolve()) or sha(translated) != image['sha256']:
            raise ValueError('批次圖片檢查失敗：' + image['page'])
        destination = output / item['output_name']
        existing = None
        if destination.exists():
            existing = sha(destination)
            if existing != image['sha256'] and existing != item.get('output_sha256'):
                raise ValueError('成品已有不同內容，已保留該檔案：' + item['output_name'])
        if existing != image['sha256']:
            temporary = work / (item['normalized_name'] + '.export.tmp')
            shutil.copyfile(translated, temporary)
            if destination.exists() and sha(destination) != existing:
                raise ValueError('成品在輸出途中被修改，已保留該檔案：' + item['output_name'])
            temporary.replace(destination)
        item['output_sha256'] = image['sha256']
        previous[image['page']] = [row for row in translations if row['page'] == image['page']]
        for checkpoint, content, operation in ((work / 'file-map.json', mapping, 'save_file_map'),
                                                (ledger, previous, 'save_export_ledger')):
            try:
                save(checkpoint, content)
            except BaseException as error:
                # This is job bookkeeping, not a failed model response for the
                # last image. Preserve that image only as publication context.
                error.manga_context = {'stage': 'publish', 'operation': operation,
                                       'last_page': image['page'], 'path': str(checkpoint)}
                raise
    csv_temp = work / 'translation-export.csv.tmp'
    with csv_temp.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['原檔名', '中文圖片', '文字框', '日文', '繁體中文'])
        writer.writeheader()
        for page in sorted(previous):
            item = by_page[page]
            for row in previous[page]:
                writer.writerow({'原檔名': item['source_name'], '中文圖片': item['output_name'],
                                 '文字框': row['source_id'], '日文': row['japanese'], '繁體中文': row['translation']})
    csv_temp.replace(output / '翻譯對照.csv')
    return len(previous), sum(len(rows) for rows in previous.values())


def verify_published_outputs(output, mapping):
    """Reconcile delivered files without changing files or trusting a ledger count."""
    expected_pages = {f'{index:06d}.png' for index in range(1, len(mapping) + 1)}
    page_counts = Counter(item['normalized_name'] for item in mapping)
    name_counts = Counter(item['output_name'] for item in mapping)
    issues = []
    for page in sorted(expected_pages - page_counts.keys()):
        issues.append({'page': page, 'type': 'missing_mapping'})
    for page, count in page_counts.items():
        if page not in expected_pages or count != 1:
            issues.append({'page': page, 'type': 'invalid_mapping', 'entries': count})
    for name, count in name_counts.items():
        if count != 1:
            issues.append({'output_name': name, 'type': 'duplicate_output', 'entries': count})
    valid_pages = set()
    for item in mapping:
        expected_sha = item.get('output_sha256')
        if not expected_sha:
            continue  # An unproduced page is legitimate in a partial job.
        page, name = item['normalized_name'], item['output_name']
        destination = output / name
        issue = {'page': page, 'output_name': name, 'expected_sha256': expected_sha}
        try:
            if not destination.is_file():
                issue['type'] = 'missing'
            else:
                actual_sha = sha(destination)
                if actual_sha != expected_sha:
                    issue.update(type='changed', actual_sha256=actual_sha)
                elif page in expected_pages and page_counts[page] == 1 and name_counts[name] == 1:
                    valid_pages.add(page)
        except OSError as error:
            issue.update(type='unreadable', error_type=type(error).__name__, error=str(error))
        if 'type' in issue:
            issues.append(issue)
    return {'completed_pages': len(valid_pages), 'published_pages': sorted(valid_pages), 'issues': issues}


def cancel_if_requested(cancel_file: Path | None) -> None:
    if cancel_file is not None and cancel_file.exists():
        raise InterruptedError('已停止翻譯，原圖保留；此次工作資料也已保留。')


def normalize_image(source: Path, destination: Path) -> dict:
    with Image.open(source) as image:
        if getattr(image, 'n_frames', 1) != 1:
            raise ValueError('尚未支援動態／多頁圖片，請先拆成單張圖片：' + source.name)
        image = ImageOps.exif_transpose(image)
        # Flatten transparent pages onto white for the existing RGB manga pipeline.
        if 'A' in image.getbands() or 'transparency' in image.info:
            rgba = image.convert('RGBA')
            rgb = Image.new('RGB', rgba.size, 'white')
            rgb.paste(rgba, mask=rgba.getchannel('A'))
        else:
            rgb = image.convert('RGB')
        rgb.save(destination, format='PNG')
        return {'dimensions': list(rgb.size), 'normalized_sha256': sha(destination)}


@contextmanager
def translation_lock():
    import msvcrt
    runtime = HERE / 'runtime'
    runtime.mkdir(exist_ok=True)
    with (runtime / 'folder-translation.lock').open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError('另一個資料夾正在翻譯，請等待它完成或停止後再開始。') from error
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def translate_folder(source: Path, output: Path | None = None, cancel_file: Path | None = None,
                     resume: bool = False) -> Path:
    source = source.resolve()
    pages = source_images(source)
    if resume and output is None:
        output = resume_candidate(source)
        if output is None:
            raise ValueError('這個資料夾沒有可續跑的工作。')
    output = output.resolve() if output is not None else next_output(source)
    if output.parent != source:
        raise ValueError('輸出資料夾必須位於原圖資料夾內。')
    cancel_if_requested(cancel_file)
    with translation_lock():
        if not resume:
            output.mkdir()
        work = output / '_工作資料'
        inputs = work / 'inputs'
        inputs.mkdir(parents=True, exist_ok=resume)
        mapping = validate_resume(source, pages, output) if resume else []
        previous_record = json.loads((work / 'status.json').read_text('utf-8-sig')) if resume and (work / 'status.json').is_file() else {}
        record = {'status': 'running', 'source': str(source), 'output': str(output),
                  'pages': len(pages), 'source_recursive': False, 'source_modified': False, 'version': '0.3.1',
                  'completed_pages': sum(bool(item.get('output_sha256')) for item in mapping), 'resumed': resume,
                  'failed_pages': previous_record.get('failed_pages', {})}
        started = begin_progress(record)
        primary_error = None
        context = {'stage': 'import', 'operation': 'prepare_job', 'batch': None, 'page': None}
        try:
            if resume and (work / 'status.json').exists():
                shutil.copy2(work / 'status.json', work / ('status-before-resume-' + uuid.uuid4().hex + '.json'))
            save(work / 'status.json', record)
            emit('folder_started', source=str(source), output=str(output), pages=len(pages), completed=record['completed_pages'],
                 progress=progress_snapshot(record))
            names = output_names(pages)
            for index, path in enumerate(pages[len(mapping):], len(mapping) + 1):
                cancel_if_requested(cancel_file)
                name = f'{index:06d}.png'
                context = {'stage': 'import', 'page': name, 'source_name': path.name, 'operation': 'normalize_image'}
                original_hash = sha(path)
                details = normalize_image(path, inputs / name)
                if sha(path) != original_hash:
                    raise RuntimeError('原圖在讀取途中被其他程序更改：' + path.name)
                mapping.append({'source_name': path.name, 'normalized_name': name,
                    'output_name': names[path.name], 'source_sha256': original_hash, **details})
                save(work / 'file-map.json', mapping)
                emit('input_prepared', page=name, source_name=path.name, current=index, total=len(pages))
            if resume:
                for index, item in enumerate(mapping, 1):
                    emit('input_prepared', page=item['normalized_name'], source_name=item['source_name'], current=index, total=len(pages))
            page_file = work / 'pages.json'
            context = {'stage': 'import', 'operation': 'save_pages', 'page': None, 'path': str(page_file)}
            save(page_file, [item['normalized_name'] for item in mapping])
            command = [sys.executable, '-u', '-B', '-X', 'utf8', str(HERE / 'run_magi_scheduled.py'),
                       '--source', str(inputs), '--output', str(work / 'scheduled'), '--pages-file', str(page_file)]
            if resume:
                command.append('--resume')
                legacy = work / 'run'
                if all((legacy / '_audit' / name).is_file() for name in
                       ('machine-ocr-project.json', 'reading-order.json', 'magi/run.json', 'magi-run.json')):
                    command += ['--legacy-run', str(legacy)]
            if cancel_file is not None:
                command += ['--cancel-file', str(cancel_file.resolve())]
            with (work / 'pipeline.log').open('a' if resume else 'w', encoding='utf-8') as log:
                context = {'stage': 'prepare', 'operation': 'start_scheduler', 'page': None}
                process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                    creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    for line in process.stdout:
                        log.write(line)
                        log.flush()
                        print(line, end='', flush=True)
                        try:
                            event = json.loads(line)
                        except ValueError:
                            event = {}
                        if not isinstance(event, dict):
                            event = {}
                        if event.get('event') == 'chunk_ready':
                            chunk = Path(event['output'])
                            context = {'stage': 'publish', 'operation': 'publish_chunk',
                                       'batch': event.get('batch'), 'page': None, 'path': str(chunk)}
                            count, blocks = publish_chunk(output, mapping, chunk)
                            verified = json.loads((chunk / 'verification.json').read_text('utf-8-sig'))
                            for page in verified['completed_pages']:
                                record['failed_pages'].pop(page, None)
                            record['failed_pages'].update(verified['failed_pages'])
                            record.update(completed_pages=count, blocks=blocks)
                            progress = update_progress(record, started)
                            save(work / 'status.json', record)
                            emit('output_published', completed=count, total=len(pages), output=str(output), progress=progress)
                            context = {'stage': 'unknown', 'operation': 'wait_scheduler', 'page': None}
                    returncode = process.wait()
                except BaseException:
                    try:
                        if process.poll() is None:
                            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                            process.wait(timeout=10)
                    except BaseException as cleanup_error:
                        record_failure(work, record, cleanup_error,
                                       {'stage': 'finalize', 'operation': 'stop_scheduler', 'pid': process.pid},
                                       secondary=True)
                    raise
            cancel_if_requested(cancel_file)
            if returncode not in (0, 2):
                # The child may prepare one batch while translating another.
                # Do not assign its process failure to the last visible page.
                context = {'stage': 'unknown', 'operation': 'scheduler_exit', 'returncode': returncode,
                           'page': None, 'path': str(work / 'pipeline.log')}
                raise RuntimeError('翻譯中途停止，請查看詳細紀錄：' + str(work / 'pipeline.log'))
            schedule = json.loads((work / 'scheduled/schedule.json').read_text('utf-8'))
            context = {'stage': 'validate', 'operation': 'verify_published_outputs', 'page': None}
            output_validation = verify_published_outputs(output, mapping)
            record.update(completed_pages=output_validation['completed_pages'], output_validation=output_validation)
            if output_validation['issues']:
                raise ValueError('中文成品有缺失、變更或無法讀取，已保留現有檔案；請查看工作紀錄。')
            if schedule['status'] == 'completed' and not record['completed_pages'] == len(mapping) == len(pages):
                raise RuntimeError('批次完成但成品張數不一致，請查看工作紀錄。')
            record.update(status=schedule['status'], failed_pages=schedule['failed_pages'])
        except BaseException as error:
            primary_error = error
            record_failure(work, record, error, context)
        finally:
            primary_error, progress = finalize_progress(work, record, started, primary_error,
                                                        source=source, mapping=mapping)
        if primary_error is not None:
            raise primary_error.with_traceback(primary_error.__traceback__)
        if record['status'] == 'partial':
            review_count = sum(1 for error in record['failed_pages'].values() if error.get('type') == 'ReviewRequired')
            message = ('部分頁面需審查；正常成品已保留。確認問題區是音效後，按「套用音效審查…」。'
                       if review_count else '部分頁面未完成；已完成的圖片已保留，可按「續跑上次工作」重試。')
            emit('folder_partial', output=str(output), completed=record['completed_pages'], total=len(mapping),
                 message=message, progress=progress)
            return output
        if record['status'] != 'completed':
            raise RuntimeError(record.get('error', '翻譯未完成'))
        emit('folder_completed', output=str(output), pages=len(mapping), blocks=record['blocks'],
             elapsed_seconds=record['elapsed_seconds'], progress=progress)
        return output


def review_output(decision_path: Path) -> Path:
    """A reviewed decision belongs to one existing folder job, never a new job."""
    for parent in decision_path.resolve().parents:
        if parent.name == '_工作資料' and (parent / 'file-map.json').is_file():
            return parent.parent
    raise ValueError('音效審查檔必須位於既有中文資料夾的工作資料內。')


def preview_review_context(output, pending_path):
    """Resolve a pending page against the current attempt and original source."""
    output, pending_path = Path(output).resolve(), Path(pending_path).resolve()
    if review_output(pending_path) != output:
        raise ValueError('待審頁與目前選取的工作不一致。')
    work = output / '_工作資料'
    record = json.loads((work / 'status.json').read_text('utf-8-sig'))
    source = Path(record['source']).resolve()
    if output.parent != source:
        raise ValueError('待審工作的原圖資料夾不一致。')
    schedule = json.loads((work / 'scheduled/schedule.json').read_text('utf-8-sig'))
    page = json.loads(pending_path.read_text('utf-8-sig'))['page']
    for chunk in schedule['chunks']:
        if not chunk.get('attempts') or page not in chunk.get('pages', []):
            continue
        location = (work / 'scheduled' / chunk['attempts'][-1]).resolve()
        if not location.is_relative_to((work / 'scheduled').resolve()):
            raise ValueError('待審批次位置不一致。')
        if pending_path != location / '_audit/sfx-review' / Path(page).stem / 'pending.json':
            continue
        mapping = json.loads((work / 'file-map.json').read_text('utf-8-sig'))
        item = next((row for row in mapping if row['normalized_name'] == page), None)
        if item is None or Path(item['source_name']).name != item['source_name']:
            raise ValueError('待審頁缺少原始檔名對照。')
        if (sha(source / item['source_name']) != item['source_sha256']
                or sha(work / 'inputs' / page) != item['normalized_sha256']):
            raise ValueError('原圖已變更，請重新建立待審預覽。')
        from page_review import inspect_page
        view = inspect_page(location, pending_path)
        view.update(source_name=item['source_name'], output_name=item['output_name'])
        return view
    raise ValueError('這份待審紀錄不是目前批次，請重新選取待審頁。')


def confirm_preview_regions(output, pending_path, ids, expected_pending_sha256):
    from page_review import confirm_regions
    with translation_lock():
        view = preview_review_context(output, pending_path)
        result = confirm_regions(view['chunk'], pending_path, ids, expected_pending_sha256)
        result.update(source_name=view['source_name'], output_name=view['output_name'])
        return result


def apply_sfx_review(output: Path | None, decision_path: Path, *, preview=False) -> Path:
    """Apply one reviewed page and update the existing delivery checkpoints."""
    from sfx_review import apply_decision, verify_completed_review
    decision_path = decision_path.resolve()
    located = review_output(decision_path)
    output = output.resolve() if output is not None else located
    if output != located:
        raise ValueError('音效審查檔與選取的中文資料夾不一致。')
    work = output / '_工作資料'
    with translation_lock():
        record = json.loads((work / 'status.json').read_text('utf-8-sig'))
        source = Path(record['source']).resolve()
        if output.parent != source:
            raise ValueError('既有工作來源與中文資料夾位置不一致。')
        mapping = validate_resume(source, source_images(source), output)
        schedule_path = work / 'scheduled/schedule.json'
        schedule = json.loads(schedule_path.read_text('utf-8-sig'))
        selected = None
        for chunk in schedule['chunks']:
            if not chunk.get('attempts'):
                continue
            location = (work / 'scheduled' / chunk['attempts'][-1]).resolve()
            if not location.is_relative_to((work / 'scheduled').resolve()):
                raise ValueError('批次工作路徑不一致。')
            if decision_path.is_relative_to(location / '_audit/sfx-review'):
                selected = (chunk, location)
                break
        if selected is None:
            raise ValueError('音效審查檔不屬於目前的批次工作。')
        shutil.copy2(work / 'status.json', work / ('status-before-review-' + uuid.uuid4().hex + '.json'))
        record.update(status='running', pages=len(mapping),
                      completed_pages=sum(bool(row.get('output_sha256')) for row in mapping))
        started = begin_progress(record, operation='sfx_review')

        def review_progress(*, finished=False):
            progress = update_progress(record, started, finished=finished)
            # Restoring reviewed pixels does not process any remaining pages,
            # so its publication speed cannot predict a whole-book completion.
            progress.update(operation='sfx_review', estimated_completion_at=None, estimated_remaining_seconds=None)
            record.update(progress)
            return progress

        primary_error = None
        context = {'stage': 'validate', 'operation': 'apply_review', 'batch': selected[0].get('id'), 'page': None}
        try:
            progress = review_progress()
            save(work / 'status.json', record)
            emit('folder_started', source=str(source), output=str(output), pages=len(mapping),
                 completed=record['completed_pages'], progress=progress)
            chunk, location = selected
            if preview:
                from page_review import apply_confirmed_page
                apply_confirmed_page(location, decision_path)
            else:
                apply_decision(location, decision_path)
            result = verify_completed_review(location)
            chunk['status'] = 'partial' if result['failed_pages'] else 'completed'
            completed, failed, verified_chunks = set(), {}, []
            for entry in schedule['chunks']:
                if not entry.get('attempts'):
                    continue
                candidate = (work / 'scheduled' / entry['attempts'][-1]).resolve()
                if not candidate.is_relative_to((work / 'scheduled').resolve()):
                    raise ValueError('批次工作路徑不一致。')
                if (candidate / 'verification.json').is_file():
                    verified = verify_completed_review(candidate)
                    completed.update(verified['completed_pages'])
                    failed.update(verified['failed_pages'])
                    entry['status'] = 'partial' if verified['failed_pages'] else 'completed'
                    verified_chunks.append(candidate)
            record['failed_pages'] = failed
            # Replay every verified batch before marking the job complete.
            # Matching destination PNGs remain intact in publish_chunk.
            for candidate in verified_chunks:
                if preview and candidate != location:
                    continue
                context = {'stage': 'publish', 'operation': 'publish_reviewed_chunk', 'page': None,
                           'path': str(candidate)}
                count, blocks = publish_chunk(output, mapping, candidate)
                record.update(completed_pages=count, blocks=blocks)
                progress = review_progress()
                save(work / 'status.json', record)
                emit('output_published', completed=count, total=len(mapping), output=str(output), progress=progress)
            expected = {item['normalized_name'] for item in mapping}
            published = {item['normalized_name'] for item in mapping
                         if item.get('output_sha256') and (output / item['output_name']).is_file()
                         and sha(output / item['output_name']) == item['output_sha256']}
            fully_published = completed == expected and published == expected and count == len(mapping) and not failed
            schedule.update(completed_pages=sorted(completed), failed_pages=failed,
                            status='completed' if fully_published else 'partial')
            context = {'stage': 'finalize', 'operation': 'save_schedule', 'path': str(schedule_path), 'page': None}
            save(schedule_path, schedule)
            record.update(status=schedule['status'], completed_pages=count, blocks=blocks, failed_pages=failed)
        except BaseException as error:
            primary_error = error
            record_failure(work, record, error, context)
        finally:
            primary_error, progress = finalize_progress(work, record, started, primary_error)
        if primary_error is not None:
            raise primary_error.with_traceback(primary_error.__traceback__)
        if record['status'] == 'completed':
            emit('folder_completed', output=str(output), pages=len(mapping), blocks=blocks, reviewed=True, progress=progress)
        else:
            emit('folder_partial', output=str(output), completed=count, total=len(mapping),
                 message=('本頁人工審查已通過並輸出；仍有其他頁面待處理。' if preview else
                          '已套用本頁音效審查；其餘正常內容保持不變，仍有其他頁面待處理。'), progress=progress)
        return output


def open_gui(initial_source: Path | None = None) -> tuple:
    from PyQt6 import QtCore, QtGui, QtWidgets

    issue_labels = {'review': '待審查', 'page_failure': '頁面失敗', 'job_failure': '工作錯誤'}

    def issue_summary(row):
        if row['kind'] == 'job_failure':
            context = row.get('error_context') or {}
            if context.get('winerror') == 5 or 'WinError 5' in row.get('message', ''):
                return 'Windows 拒絕存取檔案（WinError 5）；請查看完整診斷。'
        return row.get('message') or row.get('reason') or '尚無詳細原因'

    def populate_issues(table, rows):
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            stage = STAGE_LABELS.get(row.get('stage'), '階段不明')
            if row.get('batch') is not None:
                stage = f"第 {row['batch']} 批／{stage}"
            values = [issue_labels.get(row['kind'], row['kind']),
                      str(row['page_index']) if row.get('page_index') is not None else '—',
                      row.get('source_name') or ('整體工作' if row['kind'] == 'job_failure'
                                                else row.get('normalized_name') or '原檔名未確認'),
                      stage, issue_summary(row)]
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, row)
                item.setToolTip(value)
                table.setItem(index, column, item)

    def issue_table(parent):
        table = QtWidgets.QTableWidget(0, 5, parent)
        table.setHorizontalHeaderLabels(['狀態', '頁序', '原始檔名', '批次／階段', '原因'])
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        table.setWordWrap(False)
        table.verticalHeader().hide()
        for column in range(4):
            table.horizontalHeader().setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Stretch)
        table.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        return table

    def safe_open(path, *, image=False, directory=False):
        if not path:
            return False
        candidate = Path(path)
        if directory:
            allowed = candidate.is_dir()
        else:
            allowed = candidate.is_file() and candidate.suffix.casefold() in (
                EXTENSIONS if image else {'.json', '.jsonl', '.log', '.txt', '.csv'})
        return allowed and QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(candidate)))

    class DiagnosticsDialog(QtWidgets.QDialog):
        def __init__(self, owner):
            super().__init__(owner)
            self.owner = owner
            self.rows = []
            self.setWindowTitle('待審查、失敗與工作診斷')
            available = self.screen().availableGeometry()
            self.resize(min(980, available.width() - 40), min(690, available.height() - 60))
            layout = QtWidgets.QVBoxLayout(self)
            note = QtWidgets.QLabel('選取一列查看完整原因。頁序依匯入順序；工作錯誤不計入頁面失敗數。')
            note.setWordWrap(True)
            layout.addWidget(note)
            toolbar = QtWidgets.QHBoxLayout()
            self.filter = QtWidgets.QComboBox()
            for label, kind in (('全部', ''), ('待審查', 'review'), ('頁面失敗', 'page_failure'), ('工作錯誤', 'job_failure')):
                self.filter.addItem(label, kind)
            self.filter.currentIndexChanged.connect(self.reload)
            toolbar.addWidget(self.filter)
            refresh = QtWidgets.QPushButton('重新讀取紀錄')
            refresh.clicked.connect(owner.refresh_diagnostics)
            toolbar.addWidget(refresh)
            toolbar.addStretch()
            copy = QtWidgets.QPushButton('複製全部診斷')
            copy.clicked.connect(owner.copy_diagnostics)
            toolbar.addWidget(copy)
            layout.addLayout(toolbar)
            self.table = issue_table(self)
            self.table.itemSelectionChanged.connect(self.show_selected)
            layout.addWidget(self.table, 2)
            self.detail = QtWidgets.QPlainTextEdit()
            self.detail.setReadOnly(True)
            layout.addWidget(self.detail, 2)
            actions = QtWidgets.QHBoxLayout()
            self.open_source = QtWidgets.QPushButton('開啟原圖')
            self.open_source.clicked.connect(lambda: self.open_selected('source_path', image=True))
            self.open_evidence = QtWidgets.QPushButton('開啟該項紀錄')
            self.open_evidence.clicked.connect(lambda: self.open_selected('details_path'))
            self.open_log = QtWidgets.QPushButton('開啟相關日誌')
            self.open_log.clicked.connect(lambda: self.open_selected('log_path'))
            self.copy_selected = QtWidgets.QPushButton('複製此項診斷')
            self.copy_selected.clicked.connect(lambda: owner.copy_diagnostics(self.selected()))
            self.open_review = QtWidgets.QPushButton('左右對照審查…')
            self.open_review.clicked.connect(lambda: owner.open_visual_review(self.selected()))
            for button in (self.open_source, self.open_evidence, self.open_log, self.copy_selected, self.open_review):
                actions.addWidget(button)
            layout.addLayout(actions)
            close = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
            close.rejected.connect(self.close)
            layout.addWidget(close)
            self.reload()

        def selected(self):
            item = self.table.item(self.table.currentRow(), 0)
            return item.data(QtCore.Qt.ItemDataRole.UserRole) if item is not None else None

        def reload(self):
            selected = self.selected()
            kind = self.filter.currentData()
            self.rows = [row for row in self.owner.diagnostic_report()['issues'] if not kind or row['kind'] == kind]
            populate_issues(self.table, self.rows)
            selected_index = next((i for i, row in enumerate(self.rows) if selected and row['key'] == selected['key']), 0)
            if self.rows:
                self.table.selectRow(selected_index)
            self.show_selected()

        def show_selected(self):
            row = self.selected()
            self.detail.setPlainText(self.owner.diagnostics_text(row) if row else '目前沒有符合篩選條件的問題。')
            for button, key in ((self.open_source, 'source_path'), (self.open_evidence, 'details_path'), (self.open_log, 'log_path')):
                button.setEnabled(bool(row and row.get(key)))
            self.copy_selected.setEnabled(row is not None)
            self.open_review.setEnabled(bool(row and row['kind'] == 'review'
                and self.owner.process.state() == QtCore.QProcess.ProcessState.NotRunning))

        def open_selected(self, key, *, image=False):
            row = self.selected()
            if row and not safe_open(row.get(key), image=image):
                self.detail.appendPlainText('\n無法開啟該檔案，可能已移動或目前無法讀取。')

    class FolderWindow(QtWidgets.QWidget):
        def __init__(self):
            super().__init__()
            self.source = self.output = self.cancel_file = None
            self.names = {}
            self.pending_bytes = b''
            self.closing = False
            self.failed_message = ''
            self.success = False
            self.completion_progress = None
            self.progress_state = {'status': 'idle', 'pages': 0, 'completed_pages': 0, 'failed_pages': {}}
            self.progress_origin = None
            self.issue_events = []
            self._diagnostics = {'issues': [], 'job_error': None, 'read_warnings': []}
            self.diagnostics_dialog = None
            self.review_dialog = None
            self.setWindowTitle('本機漫畫資料夾翻譯')
            available = self.screen().availableGeometry()
            self.resize(min(920, available.width() - 40), min(820, available.height() - 60))
            self.setMinimumWidth(600)
            self.setAcceptDrops(True)
            outer = QtWidgets.QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            self.content_scroll = QtWidgets.QScrollArea()
            self.content_scroll.setWidgetResizable(True)
            self.content_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            body = QtWidgets.QWidget()
            self.content_scroll.setWidget(body)
            outer.addWidget(self.content_scroll)
            layout = QtWidgets.QVBoxLayout(body)
            layout.setContentsMargins(28, 24, 28, 24)
            layout.setSpacing(10)
            title = QtWidgets.QLabel('漫畫資料夾 → 繁體中文')
            title.setFont(QtGui.QFont('Microsoft JhengHei', 18, QtGui.QFont.Weight.Bold))
            layout.addWidget(title)
            layout.addWidget(QtWidgets.QLabel('選擇資料夾並開始翻譯；每批完成即可閱讀，中斷後可續跑。'))
            row = QtWidgets.QHBoxLayout()
            self.path_box = QtWidgets.QLineEdit()
            self.path_box.setReadOnly(True)
            self.path_box.setPlaceholderText('選擇或拖放漫畫資料夾至此視窗')
            self.browse = QtWidgets.QPushButton('選擇資料夾…')
            self.browse.clicked.connect(self.choose_folder)
            row.addWidget(self.path_box, 1)
            row.addWidget(self.browse)
            layout.addLayout(row)
            format_note = QtWidgets.QLabel('JPG / JPEG / PNG / WebP / BMP / TIF / TIFF / GIF（單張靜態圖片）\n只讀取所選資料夾這一層；原圖保留，成品統一為 PNG。')
            format_note.setWordWrap(True)
            layout.addWidget(format_note)
            self.destination = QtWidgets.QLabel('輸出位置：選擇資料夾後顯示')
            self.destination.setWordWrap(True)
            self.destination.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            self.destination.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            layout.addWidget(self.destination)
            self.status = QtWidgets.QLabel('尚未選擇資料夾')
            self.status.setWordWrap(True)
            self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            layout.addWidget(self.status)
            self.progress = QtWidgets.QProgressBar()
            self.progress.setValue(0)
            layout.addWidget(self.progress)
            metrics = QtWidgets.QVBoxLayout()
            metrics.setSpacing(3)
            self.page_count = QtWidgets.QLabel()
            self.page_count.setFont(QtGui.QFont('Microsoft JhengHei', 13, QtGui.QFont.Weight.Bold))
            self.issue_count = QtWidgets.QLabel()
            self.elapsed_label = QtWidgets.QLabel()
            self.eta_label = QtWidgets.QLabel()
            self.finish_label = QtWidgets.QLabel()
            for label in (self.page_count, self.issue_count, self.elapsed_label, self.eta_label, self.finish_label):
                label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
                label.setWordWrap(True)
                metrics.addWidget(label)
            layout.addLayout(metrics)
            self.job_status = QtWidgets.QLabel('工作狀態：尚未開始')
            self.job_status.setWordWrap(True)
            self.job_status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            layout.addWidget(self.job_status)
            self.cpu_status = QtWidgets.QLabel('前處理：尚未開始')
            self.cpu_status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            layout.addWidget(self.cpu_status)
            buttons = QtWidgets.QHBoxLayout()
            self.start = QtWidgets.QPushButton('開始翻譯')
            self.start.setEnabled(False)
            self.start.clicked.connect(self.start_translation)
            self.resume = QtWidgets.QPushButton('續跑上次工作')
            self.resume.setEnabled(False)
            self.resume.clicked.connect(lambda: self.start_translation(resume=True))
            self.stop = QtWidgets.QPushButton('停止')
            self.stop.setEnabled(False)
            self.stop.clicked.connect(self.request_stop)
            self.review = QtWidgets.QPushButton('套用音效審查…')
            self.review.clicked.connect(self.apply_review_decision)
            self.visual_review = QtWidgets.QPushButton('開啟審查視窗…')
            self.visual_review.setEnabled(False)
            self.visual_review.clicked.connect(lambda: self.open_visual_review())
            self.open_result = QtWidgets.QPushButton('開啟中文資料夾')
            self.open_result.setEnabled(False)
            self.open_result.clicked.connect(self.show_result)
            buttons.addWidget(self.start)
            buttons.addWidget(self.resume)
            buttons.addWidget(self.stop)
            buttons.addStretch()
            buttons.addWidget(self.open_result)
            layout.addLayout(buttons)
            self.details = QtWidgets.QCheckBox('顯示詳細紀錄')
            self.details.toggled.connect(self.toggle_log)
            secondary = QtWidgets.QHBoxLayout()
            secondary.addWidget(self.visual_review)
            secondary.addWidget(self.review)
            secondary.addStretch()
            secondary.addWidget(self.details)
            layout.addLayout(secondary)
            issue_header = QtWidgets.QHBoxLayout()
            self.issue_heading = QtWidgets.QLabel('待審查與失敗明細：目前沒有紀錄')
            issue_header.addWidget(self.issue_heading, 1)
            self.diagnostics_button = QtWidgets.QPushButton('查看／複製診斷…')
            self.diagnostics_button.clicked.connect(self.show_diagnostics)
            issue_header.addWidget(self.diagnostics_button)
            self.job_log_button = QtWidgets.QPushButton('開啟工作紀錄')
            self.job_log_button.clicked.connect(self.open_job_log)
            issue_header.addWidget(self.job_log_button)
            layout.addLayout(issue_header)
            self.issue_table = issue_table(self)
            self.issue_table.setMinimumHeight(118)
            self.issue_table.setMaximumHeight(158)
            self.issue_table.cellDoubleClicked.connect(self.open_issue)
            layout.addWidget(self.issue_table)
            self.issue_warning = QtWidgets.QLabel()
            self.issue_warning.setWordWrap(True)
            self.issue_warning.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            self.issue_warning.hide()
            layout.addWidget(self.issue_warning)
            self.log = QtWidgets.QPlainTextEdit()
            self.log.setReadOnly(True)
            self.log.setMaximumBlockCount(2000)
            self.log.hide()
            layout.addWidget(self.log, 1)
            layout.addStretch()
            self.process = QtCore.QProcess(self)
            self.process.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.MergedChannels)
            self.process.readyReadStandardOutput.connect(self.read_output)
            self.process.finished.connect(self.process_finished)
            self.process.errorOccurred.connect(self.process_error)
            self.timer = QtCore.QTimer(self)
            self.timer.setInterval(1000)
            self.timer.timeout.connect(self.refresh_metrics)
            self.timer.start()
            self.diagnostic_timer = QtCore.QTimer(self)
            self.diagnostic_timer.setSingleShot(True)
            self.diagnostic_timer.setInterval(120)
            self.diagnostic_timer.timeout.connect(self.refresh_diagnostics)
            self.refresh_metrics()
            if initial_source is not None:
                self.set_source(initial_source)

        def current_progress(self):
            elapsed = self.progress_state.get('elapsed_seconds', 0)
            if self.progress_origin is not None:
                elapsed += max(0, time.monotonic() - self.progress_origin)
            state = progress_snapshot(self.progress_state, elapsed)
            state['operation'] = self.progress_state.get('operation', 'translation')
            # Publication arrives in batches. Keep that checkpoint's ETA fixed
            # while the clock ticks, rather than treating every second without
            # a new batch as evidence that the per-page rate became slower.
            state['estimated_completion_at'] = None
            state['estimated_remaining_seconds'] = None
            state['eta_expired'] = False
            if (state['status'] == 'running' and not state['failed_pages']
                    and self.progress_state.get('operation') != 'sfx_review'):
                estimate = self.progress_state.get('estimated_completion_at')
                if estimate:
                    try:
                        stamp = datetime.fromisoformat(estimate)
                        if stamp.tzinfo is not None and stamp > local_now():
                            state['estimated_completion_at'] = estimate
                            state['estimated_remaining_seconds'] = self.progress_state.get('estimated_remaining_seconds')
                        else:
                            state['eta_expired'] = True
                    except (TypeError, ValueError):
                        pass
            return state

        def set_progress(self, record, *, live=False):
            self.progress_state = dict(record)
            self.progress_origin = time.monotonic() if live and record.get('status') == 'running' else None
            self.refresh_metrics()

        def diagnostic_report(self):
            return self._diagnostics

        def refresh_diagnostics(self):
            if self.output is None or (not self.output.is_dir() and not self.progress_state.get('error')):
                self._diagnostics = {'issues': [], 'job_error': None, 'read_warnings': []}
            else:
                try:
                    self._diagnostics = collect_diagnostics(self.output, self.progress_state,
                                                            live_events=self.issue_events, names=self.names)
                except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
                    # Showing an error must not prevent stopping/resuming work.
                    self._diagnostics = {'issues': [], 'job_error': None,
                                         'read_warnings': ['診斷資料暫時無法讀取：' + str(error)]}
            selected = self.issue_table.item(self.issue_table.currentRow(), 0)
            selected_key = selected.data(QtCore.Qt.ItemDataRole.UserRole)['key'] if selected else None
            rows = self._diagnostics['issues']
            populate_issues(self.issue_table, rows)
            if rows:
                self.issue_table.selectRow(next((i for i, row in enumerate(rows) if row['key'] == selected_key), 0))
            self.issue_heading.setText(f'待審查與失敗明細：{len(rows)} 項（雙擊查看）' if rows else '待審查與失敗明細：目前沒有紀錄')
            has_review = any(row['kind'] == 'review' for row in rows)
            has_published = bool(self.output and self.output.is_dir() and any(f.suffix.lower() in ('.png', '.jpg', '.webp') for f in self.output.iterdir() if f.is_file()))
            self.visual_review.setEnabled((has_review or has_published)
                and self.process.state() == QtCore.QProcess.ProcessState.NotRunning)
            self.visual_review.setText('開啟審查視窗…' if has_review else '單頁視覺精修工作台…')
            self.job_log_button.setEnabled(bool(self._diagnostics.get('log_path')))
            warnings = self._diagnostics.get('read_warnings', [])
            self.issue_warning.setText('部分診斷資料無法讀取；開啟「查看／複製診斷」查看原因。' if warnings else '')
            self.issue_warning.setVisible(bool(warnings))
            self.refresh_metrics()
            if self.diagnostics_dialog is not None and self.diagnostics_dialog.isVisible():
                self.diagnostics_dialog.reload()

        def diagnostics_text(self, row=None):
            state = self.current_progress()
            lines = ['漫畫資料夾翻譯診斷', '工作狀態：' + state['status'],
                     '來源：' + str(self.source or '尚未選擇'), '輸出：' + str(self.output or '尚未建立'),
                     f"最近確認完成：{state['completed_pages']} / {state['pages']} 頁；待審查 {state['review_required_count']} 頁；頁面失敗 {state['failed_page_count']} 頁"]
            if state['status'] == 'failed':
                lines.append('工作失敗可能發生在檔案發布或存檔；不能由頁面失敗數判斷整體工作是否成功。完成數保留最近確認紀錄。')
            rows = [row] if isinstance(row, dict) else self._diagnostics['issues']
            for issue in rows:
                lines += ['', '狀態：' + issue_labels.get(issue['kind'], issue['kind']),
                          '原始檔名：' + (issue.get('source_name') or '未記錄／工作層級'),
                          '頁序：' + str(issue.get('page_index') or '未指定'),
                          '內部頁名：' + (issue.get('normalized_name') or '未指定'),
                          '批次：' + str(issue.get('batch') or '未確認'),
                          '階段：' + STAGE_LABELS.get(issue.get('stage'), '階段不明'),
                          '階段依據：' + issue.get('stage_evidence', '未記錄'),
                          '原因：' + issue_summary(issue), '錯誤類型：' + str(issue.get('type') or '未記錄'),
                          '原始訊息：' + issue.get('message', '')]
                if issue.get('last_batch') is not None:
                    lines += ['最後批次事件：' + str(issue['last_batch']),
                              '批次依據：' + issue.get('batch_evidence', '不代表已確認的故障頁')]
                for key, label in (('source_path', '原圖'), ('details_path', '對應紀錄'), ('log_path', '相關日誌')):
                    if issue.get(key):
                        lines.append(label + '：' + issue[key])
                context = issue.get('error_context') or {}
                if context:
                    lines += ['完整錯誤資訊：', json.dumps(context, ensure_ascii=False, indent=2)]
                    if context.get('traceback'):
                        lines += ['Traceback：', context['traceback']]
                elif issue['kind'] == 'job_failure':
                    lines.append('舊工作未保存 traceback；已保留原始錯誤與可確認的紀錄。')
                if issue.get('secondary_errors'):
                    lines += ['收尾期間的其他錯誤（不取代原始錯誤）：', json.dumps(issue['secondary_errors'], ensure_ascii=False, indent=2)]
            if self._diagnostics.get('read_warnings'):
                lines += ['', '讀取警告：', *self._diagnostics['read_warnings']]
            return '\n'.join(lines)

        def copy_diagnostics(self, row=None):
            QtWidgets.QApplication.clipboard().setText(self.diagnostics_text(row))

        def open_issue(self, row_index, column=0):
            item = self.issue_table.item(row_index, 0)
            row = item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None
            if row and row['kind'] == 'review':
                self.open_visual_review(row)
            else:
                self.show_diagnostics()

        def open_visual_review(self, selected=None):
            if self.process.state() != QtCore.QProcess.ProcessState.NotRunning or self.output is None:
                return
            if self.review_dialog is not None and self.review_dialog.isVisible():
                self.review_dialog.raise_()
                return
            self.refresh_diagnostics()
            pages = [row for row in self._diagnostics['issues'] if row['kind'] == 'review' and row.get('details_path')]
            output = self.output
            work = next((d for d in output.iterdir() if d.is_dir() and d.name.startswith('_')), None)
            from review_window import ReviewDialog
            from page_fast_renderer import build_page_review_view

            if not pages:
                if not work or not (work / 'file-map.json').is_file():
                    self.status.setText('目前沒有可開啟的待審頁或已翻譯頁面。')
                    return
                file_map = json.loads((work / 'file-map.json').read_text('utf-8-sig'))
                for entry in file_map:
                    if (output / entry['output_name']).is_file():
                        pages.append({
                            'page_index': entry['normalized_name'],
                            'source_name': entry['source_name'],
                            'normalized_name': entry['normalized_name'],
                            'details_path': str(output / entry['output_name']),
                        })
                if not pages:
                    self.status.setText('目前沒有可精修的已發布頁面。')
                    return

            if self.diagnostics_dialog is not None:
                self.diagnostics_dialog.hide()

            def page_loader(path: Path):
                p_str = str(path)
                if p_str.endswith('pending.json'):
                    return preview_review_context(output, path)
                else:
                    return build_page_review_view(output, path.name)

            self.review_dialog = ReviewDialog(
                pages,
                page_loader,
                lambda path, ids, digest: confirm_preview_regions(output, path, ids, digest),
                lambda path: apply_sfx_review(output, path, preview=True),
                self,
                selected_path=selected.get('details_path') if selected else None,
                output_dir=output
            )
            self.review_dialog.jobChanged.connect(self.review_job_changed)
            self.review_dialog.show()
            self.review_dialog.raise_()

        def review_job_changed(self):
            try:
                work = next((d for d in self.output.iterdir() if d.is_dir() and d.name.startswith('_')), self.output / '_工作資料')
                record = json.loads((work / 'status.json').read_text('utf-8-sig'))
                self.issue_events = []
                self.set_progress(record)
                self.resume.setEnabled(resume_candidate(self.source) is not None)
                self.open_result.setEnabled(True)
                self.refresh_diagnostics()
            except (OSError, ValueError, KeyError) as error:
                self.status.setText('無法更新審查後的工作狀態：' + str(error))

        def show_diagnostics(self, *_):
            self.refresh_diagnostics()
            item = self.issue_table.item(self.issue_table.currentRow(), 0)
            selected_key = item.data(QtCore.Qt.ItemDataRole.UserRole)['key'] if item is not None else None
            if self.diagnostics_dialog is None:
                self.diagnostics_dialog = DiagnosticsDialog(self)
            self.diagnostics_dialog.filter.setCurrentIndex(0)
            self.diagnostics_dialog.reload()
            if selected_key:
                for index, row in enumerate(self.diagnostics_dialog.rows):
                    if row['key'] == selected_key:
                        self.diagnostics_dialog.table.selectRow(index)
                        break
            self.diagnostics_dialog.show()
            self.diagnostics_dialog.raise_()
            self.diagnostics_dialog.activateWindow()

        def open_job_log(self):
            if not safe_open(self._diagnostics.get('log_path')):
                self.status.setText('工作紀錄目前無法開啟，請查看診斷中的讀取警告。')

        def refresh_metrics(self):
            state = self.current_progress()
            self.page_count.setText(f"已完成 {state['completed_pages']} / {state['pages']} 頁")
            self.issue_count.setText(f"待審查 {state['review_required_count']} 頁　｜　頁面失敗 {state['failed_page_count']} 頁")
            elapsed_title = '上次記錄用時（非即時）：' if state['status'] == 'unattached_running' else '本次已用時間：'
            self.elapsed_label.setText(elapsed_title + duration_text(state['elapsed_seconds']))
            status = state['status']
            if status == 'running':
                eta = ('套用審查中；不估算其餘頁面' if self.progress_state.get('operation') == 'sfx_review'
                       else '等待審查／重試，暫無法估算' if state['review_required_count'] or state['failed_page_count']
                       else timestamp_text(state['estimated_completion_at']) if state['estimated_completion_at']
                       else '重新估算中…' if state['eta_expired']
                       else '等待最後確認' if state['pages'] and state['completed_pages'] == state['pages'] else '估算中…')
            else:
                eta = '—'
            self.eta_label.setText('預估完成時間：' + eta)
            if status == 'completed':
                ended = '完成時間：' + (timestamp_text(state['completed_at']) if state['completed_at'] else '舊工作未記錄')
            elif status in ('partial', 'failed', 'cancelled'):
                names = {'partial': '部分完成', 'failed': '失敗', 'cancelled': '已停止'}
                ended = f"結束時間：{timestamp_text(state['ended_at'])}（{names[status]}）"
            elif status == 'unattached_running':
                ended = '結束時間：尚未確認（可能仍在其他視窗執行）'
            elif status == 'finishing':
                ended = '完成時間：等待程序正常結束'
            else:
                ended = '完成時間：尚未完成'
            self.finish_label.setText(ended)
            job_error = self._diagnostics.get('job_error')
            if status == 'failed':
                stage = STAGE_LABELS.get((job_error or {}).get('stage'), '階段尚未確認')
                self.job_status.setText('工作失敗：' + stage + '。完成數為最後確認紀錄；請查看下方工作錯誤。')
            else:
                names = {'idle': '尚未開始', 'running': '執行中', 'finishing': '等待程序結束',
                         'partial': '部分完成，仍有頁面待處理', 'completed': '已完成',
                         'cancelled': '已停止', 'unattached_running': '尚未連接最近工作'}
                self.job_status.setText('工作狀態：' + names.get(status, status))
            self.progress.setRange(0, max(1, state['pages']))
            self.progress.setValue(state['completed_pages'])

        def stop_progress(self, status):
            state = self.current_progress()
            state.update(status=status, ended_at=state.get('ended_at') or local_now().isoformat(timespec='seconds'),
                         completed_at=None, estimated_completion_at=None, estimated_remaining_seconds=None)
            self.set_progress(state)

        def prepare_progress(self, *, resume=False, record=None, operation='translation'):
            record = dict(record or {})
            total = record.get('pages', len(source_images(self.source)))
            completed = record.get('completed_pages', 0) if resume else 0
            record.update(status='running', pages=total, completed_pages=completed,
                          initial_completed_pages=completed, started_at=local_now().isoformat(timespec='seconds'),
                          ended_at=None, completed_at=None, elapsed_seconds=0, operation=operation,
                          estimated_completion_at=None, estimated_remaining_seconds=None)
            if not resume:
                record['failed_pages'] = {}
            self.completion_progress = None
            self.set_progress(record, live=True)

        def latest_saved_job(self, source):
            candidates = []
            for directory in source.iterdir():
                path = directory / '_工作資料/status.json'
                if directory.is_dir() and not directory.is_symlink() and path.is_file():
                    try:
                        record = json.loads(path.read_text('utf-8-sig'))
                        if Path(record.get('source', '')).resolve() == source:
                            candidates.append((path.stat().st_mtime_ns, directory, record))
                    except (OSError, ValueError, TypeError):
                        continue
            return max(candidates, key=lambda item: item[0], default=None)

        def toggle_log(self, visible):
            self.log.setVisible(visible)

        def set_source(self, path):
            if self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
                return
            try:
                source = Path(path).resolve()
                pages = source_images(source)
                self.source, self.output = source, next_output(source)
                self.names, self.issue_events = {}, []
                self._diagnostics = {'issues': [], 'job_error': None, 'read_warnings': []}
                self.path_box.setText(str(source))
                self.destination.setText('輸出位置：' + str(self.output))
                self.status.setText(f'找到 {len(pages)} 張圖片，可以開始翻譯。')
                self.start.setEnabled(True)
                self.resume.setEnabled(resume_candidate(source) is not None)
                self.open_result.setEnabled(False)
                self.set_progress({'status': 'idle', 'pages': len(pages), 'completed_pages': 0, 'failed_pages': {}})
                saved = self.latest_saved_job(source)
                if saved is not None:
                    _, self.output, record = saved
                    if record.get('status') == 'running':
                        record = dict(record, status='unattached_running')
                    self.set_progress(record)
                    self.destination.setText('上次輸出：' + str(self.output))
                    self.status.setText(f'找到 {len(pages)} 張圖片。下方顯示上次工作；「開始翻譯」會建立新一輪。')
                    if record.get('status') == 'unattached_running':
                        self.status.setText('下方是最近保存的進度。此視窗尚未連接該工作，請先確認其他翻譯視窗是否仍在執行。')
                    self.open_result.setEnabled(True)
                    if record.get('status') in ('failed', 'cancelled', 'partial', 'completed'):
                        self.cpu_status.setText('前處理：上次工作已結束；詳情見工作紀錄')
                self.refresh_diagnostics()
            except (OSError, ValueError) as error:
                self.source = None
                self.start.setEnabled(False)
                self.resume.setEnabled(False)
                self.status.setText(str(error))

        def choose_folder(self):
            path = QtWidgets.QFileDialog.getExistingDirectory(self, '選擇漫畫圖片資料夾',
                str(self.source or ROOT / 'Test'))
            if path:
                self.set_source(path)

        def dragEnterEvent(self, event):
            if event.mimeData().hasUrls():
                event.acceptProposedAction()

        def dropEvent(self, event):
            if self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
                event.ignore()
                return
            urls = event.mimeData().urls()
            if urls:
                path = Path(urls[0].toLocalFile())
                if path.is_file():
                    path = path.parent
                self.set_source(path)

        def start_translation(self, checked=False, resume=False):
            if self.source is None:
                return
            self.output = resume_candidate(self.source) if resume else next_output(self.source)
            if self.output is None:
                self.status.setText('這個資料夾沒有可續跑的工作。')
                return
            runtime = HERE / 'runtime'
            runtime.mkdir(exist_ok=True)
            self.cancel_file = runtime / ('folder-' + uuid.uuid4().hex + '.cancel')
            self.names, self.pending_bytes = {}, b''
            self.issue_events = []
            self._diagnostics = {'issues': [], 'job_error': None, 'read_warnings': []}
            self.failed_message, self.success = '', False
            saved = json.loads((self.output / '_工作資料/status.json').read_text('utf-8-sig')) if resume else None
            self.prepare_progress(resume=resume, record=saved)
            self.refresh_diagnostics()
            self.destination.setText('輸出位置：' + str(self.output))
            self.status.setText('正在準備圖片…')
            self.log.clear()
            self.browse.setEnabled(False)
            self.start.setEnabled(False)
            self.resume.setEnabled(False)
            self.review.setEnabled(False)
            self.visual_review.setEnabled(False)
            self.stop.setEnabled(True)
            self.open_result.setEnabled(False)
            self.process.setWorkingDirectory(str(ROOT))
            self.cpu_status.setText('前處理：等待開始')
            arguments = ['-u', '-B', '-X', 'utf8', str(Path(__file__).resolve()),
                '--headless', '--source', str(self.source), '--output', str(self.output),
                '--cancel-file', str(self.cancel_file)]
            if resume:
                arguments.append('--resume')
            self.process.start(sys.executable, arguments)

        def apply_review_decision(self):
            filename, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, '選取已確認的音效審查', str(self.output or self.source or ROOT / 'Test'),
                '音效審查 (*.json)')
            if not filename:
                return
            try:
                decision = Path(filename).resolve()
                self.output = review_output(decision)
                record = json.loads((self.output / '_工作資料/status.json').read_text('utf-8-sig'))
                self.source = Path(record['source']).resolve()
                self.path_box.setText(str(self.source))
                self.destination.setText('輸出位置：' + str(self.output))
                self.cancel_file = None
                self.names, self.pending_bytes = {}, b''
                self.issue_events = []
                self._diagnostics = {'issues': [], 'job_error': None, 'read_warnings': []}
                self.failed_message, self.success = '', False
                self.prepare_progress(resume=True, record=record, operation='sfx_review')
                self.refresh_diagnostics()
                self.log.clear()
                for button in (self.browse, self.start, self.resume, self.review, self.visual_review, self.open_result):
                    button.setEnabled(False)
                self.stop.setEnabled(False)
                self.status.setText('正在核對音效審查並局部還原原圖…')
                self.process.setWorkingDirectory(str(ROOT))
                self.process.start(sys.executable, ['-u', '-B', '-X', 'utf8', str(Path(__file__).resolve()),
                    '--headless', '--output', str(self.output), '--apply-sfx-review', str(decision)])
            except (OSError, ValueError, KeyError) as error:
                self.status.setText(str(error))

        def request_stop(self):
            if self.cancel_file is not None and self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
                self.cancel_file.write_text('cancel\n', 'utf-8')
                self.stop.setEnabled(False)
                self.status.setText('停止中，正在結束目前工作並釋放本機模型…')

        def read_output(self):
            self.pending_bytes += bytes(self.process.readAllStandardOutput())
            while b'\n' in self.pending_bytes:
                raw, self.pending_bytes = self.pending_bytes.split(b'\n', 1)
                line = raw.decode('utf-8', errors='replace').strip()
                self.log.appendPlainText(line)
                try:
                    data = json.loads(line)
                    if isinstance(data, dict):
                        self.handle_event(data)
                except (ValueError, TypeError):
                    pass

        def handle_event(self, data):
            event = data.get('event')
            if event == 'folder_started':
                self.issue_events = []
            if event in ('folder_started', 'stage_started', 'stage_finished', 'page_failed',
                         'page_review_required', 'folder_failed', 'page_translation_finished'):
                self.issue_events.append({key: value for key, value in data.items() if key != 'progress'})
                self.issue_events = self.issue_events[-512:]
            snapshot = data.get('progress')
            if isinstance(snapshot, dict):
                if event == 'folder_completed':
                    self.completion_progress = dict(snapshot)
                    self.set_progress(dict(snapshot, status='finishing'))
                else:
                    self.set_progress(snapshot, live=snapshot.get('status') == 'running')
            if event == 'folder_started':
                if not isinstance(snapshot, dict):
                    self.prepare_progress(resume=True, record={'pages': data['pages'],
                        'completed_pages': data.get('completed', 0), 'failed_pages': {}})
            elif event == 'input_prepared':
                self.names[data['page']] = data['source_name']
                self.status.setText(f"準備圖片 {data['current']} / {data['total']}：{data['source_name']}")
            elif event == 'stage_started':
                stages = {'detect-ocr-inpaint': '正在辨識文字與整理對話框…',
                          'magi': '正在判讀漫畫閱讀順序…', 'render': '正在排版並產生中文圖片…'}
                text = stages.get(data['stage'], data['stage'])
                if data.get('worker') == 'cpu':
                    self.cpu_status.setText(f"第 {data.get('batch', 1)} 批前處理：{text}")
                else:
                    self.status.setText(text)
            elif event == 'cpu_batch_finished':
                self.cpu_status.setText(f"第 {data['batch']} 批前處理完成")
            elif event == 'request_started':
                name = data['label'] + '.png'
                action = '正在重試重複輸出的頁面：' if data.get('recovery') == 'repetition_loop' else '正在翻譯：'
                self.status.setText(action + self.names.get(name, name))
            elif event == 'page_translation_finished':
                self.status.setText('已翻譯：' + self.names.get(data['page'], data['page']) + '；正在完成本批排版。')
            elif event == 'page_review_required':
                state = self.current_progress()
                state['failed_pages'] = dict(state['failed_pages'], **{data['page']: {
                    'type': 'ReviewRequired', 'reason': data.get('reason', 'line_alignment_recovery'),
                    'message': data.get('message', ''), 'stage': 'translate', 'batch': data.get('batch')}})
                self.set_progress(state, live=state['status'] == 'running')
                self.status.setText('待審查問題區：' + self.names.get(data['page'], data['page']))
            elif event == 'page_failed':
                state = self.current_progress()
                state['failed_pages'] = dict(state['failed_pages'], **{data['page']: {
                    'type': data.get('error_type', 'PageFailure'), 'message': data.get('message', ''),
                    'error_context': data.get('error_context', {}), 'batch': data.get('batch')}})
                self.set_progress(state, live=state['status'] == 'running')
                self.status.setText('本頁失敗，將繼續其餘頁面：' + self.names.get(data['page'], data['page']))
            elif event == 'request_retry':
                reason = '模型重複輸出，正在調整重試：' if data.get('recovery') == 'repetition_loop' else '回覆不完整，正在重試：'
                self.status.setText(f"{reason}{self.names.get(data['label'] + '.png', data['label'])}（第 {data['attempt'] + 1} 次）")
            elif event == 'model_loading':
                self.status.setText('正在載入本機翻譯模型…')
            elif event == 'output_published':
                if not isinstance(snapshot, dict):
                    state = self.current_progress()
                    state.update(pages=data['total'], completed_pages=data['completed'])
                    self.set_progress(state, live=state['status'] == 'running')
                self.status.setText(f"已輸出 {data['completed']} / {data['total']} 張，可開啟中文資料夾閱讀。")
                self.open_result.setEnabled(True)
            elif event == 'folder_partial':
                self.success = False
                self.completion_progress = None
                if not isinstance(snapshot, dict):
                    self.stop_progress('partial')
                self.failed_message = data['message']
                self.status.setText(data['message'])
            elif event == 'folder_failed':
                self.success = False
                self.completion_progress = None
                self.stop_progress('cancelled' if data.get('cancelled') else 'failed')
                self.failed_message = data['message']
                self.progress_state.update(error=data['message'],
                    error_context=data.get('error_context') or self.progress_state.get('error_context', {}),
                    secondary_errors=data.get('secondary_errors') or self.progress_state.get('secondary_errors', []))
                self.status.setText('工作已停止；下方列出待審查頁面與工作錯誤，雙擊可查看完整原因。')
                self.status.setToolTip(data['message'])
            elif event == 'folder_completed':
                self.success = True
                if self.completion_progress is None:
                    self.completion_progress = self.current_progress()
                    self.completion_progress.update(status='completed', pages=data['pages'], completed_pages=data['pages'],
                        ended_at=local_now().isoformat(timespec='seconds'), completed_at=local_now().isoformat(timespec='seconds'))
                    self.set_progress(dict(self.completion_progress, status='finishing'))
                self.status.setText(f"已輸出 {data['pages']} 張圖片，共 {data['blocks']} 段文字，正在結束程序…")
            if self.cancel_file is not None and self.cancel_file.exists() and not self.success:
                self.status.setText('停止中，正在結束目前工作並釋放本機模型…')
            if event in ('folder_started', 'page_review_required', 'page_failed', 'output_published',
                         'folder_partial', 'folder_failed', 'folder_completed'):
                self.diagnostic_timer.start()

        def process_finished(self, exit_code, exit_status):
            self.read_output()
            normal_exit = exit_status == QtCore.QProcess.ExitStatus.NormalExit
            accepted = normal_exit and exit_code == 0 and self.success and not self.failed_message
            if accepted and self.completion_progress is not None:
                self.set_progress(self.completion_progress)
                self.status.setText(f"完成 {self.progress_state['completed_pages']} / {self.progress_state['pages']} 頁。")
            else:
                self.success = False
                terminal = self.progress_state.get('status')
                keep_terminal = normal_exit and ((terminal == 'partial' and exit_code == 2)
                                                or (terminal == 'cancelled' and exit_code == 130))
                if not keep_terminal:
                    self.stop_progress('failed')
                    if not self.failed_message:
                        self.failed_message = f'翻譯程序未正常完成（返回碼 {exit_code}）。'
                        self.progress_state.update(error=self.failed_message, error_type='ProcessError',
                            error_context={'stage': 'unknown', 'operation': 'process_exit',
                                           'returncode': exit_code, 'message': self.failed_message,
                                           'log_tail': self.log.toPlainText()[-8000:]})
            self.browse.setEnabled(True)
            self.start.setEnabled(self.source is not None)
            self.resume.setEnabled(self.source is not None and resume_candidate(self.source) is not None)
            self.stop.setEnabled(False)
            self.review.setEnabled(True)
            self.open_result.setEnabled(self.output is not None and self.output.is_dir())
            if not accepted:
                self.status.setText('本次工作未完成；請查看下方問題清單與診斷。')
                self.status.setToolTip(self.failed_message)
            self.cpu_status.setText('前處理：本次工作已結束')
            self.diagnostic_timer.stop()
            self.refresh_diagnostics()
            if self.closing:
                self.close()

        def process_error(self, error):
            if error == QtCore.QProcess.ProcessError.FailedToStart:
                self.failed_message = '無法啟動翻譯程序：' + self.process.errorString()
                self.progress_state.update(error=self.failed_message, error_type='ProcessStartError',
                    error_context={'stage': 'prepare', 'operation': 'start_process',
                                   'path': sys.executable, 'message': self.failed_message})
                self.process_finished(-1, QtCore.QProcess.ExitStatus.CrashExit)

        def show_result(self):
            if self.output is not None and self.output.is_dir():
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.output)))

        def closeEvent(self, event):
            if self.review_dialog is not None and self.review_dialog.busy:
                event.ignore()
            elif self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
                self.closing = True
                self.request_stop()
                event.ignore()
            else:
                event.accept()

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    # The offscreen Qt backend does not discover Windows fonts automatically.
    # Register the same installed family explicitly for both native and offscreen runs.
    font_dir = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
    for filename in ('msjh.ttc', 'msjhbd.ttc'):
        if (font_dir / filename).is_file():
            QtGui.QFontDatabase.addApplicationFont(str(font_dir / filename))
    app.setFont(QtGui.QFont('Microsoft JhengHei', 10))
    window = FolderWindow()
    window.show()
    # The regular entry point owns the event loop; the same real window is testable offscreen.
    return app, window


def main() -> int:
    parser = argparse.ArgumentParser(description='本機漫畫資料夾翻譯')
    parser.add_argument('folder', nargs='?', type=Path, help='漫畫資料夾路徑（支援拖曳執行）')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--cancel-file', type=Path)
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--apply-sfx-review', type=Path, help='套用既有工作中已確認的音效審查決策')
    args = parser.parse_args()
    target_source = args.source or args.folder
    if target_source is not None and target_source.is_file():
        target_source = target_source.parent
    if args.headless:
        if target_source is None and args.apply_sfx_review is None:
            parser.error('--headless requires --source or folder path')
        try:
            output = (apply_sfx_review(args.output, args.apply_sfx_review) if args.apply_sfx_review is not None
                      else translate_folder(target_source, args.output, args.cancel_file, args.resume))
            return 0 if json.loads((output / '_工作資料/status.json').read_text('utf-8'))['status'] == 'completed' else 2
        except Exception as error:
            if not getattr(error, 'folder_reported', False):
                emit('folder_failed', cancelled=isinstance(error, InterruptedError), message=str(error),
                     error_context=exception_details(error, {'stage': 'validate', 'operation': 'job_start'}))
            return 130 if isinstance(error, InterruptedError) else 1
    app, window = open_gui(target_source)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
