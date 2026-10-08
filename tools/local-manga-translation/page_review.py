"""Human confirmation of a sealed translated preview, without changing its pixels."""
from datetime import datetime
import math
from pathlib import Path

from atomic_json import save_json
import sfx_review as evidence


def _load(chunk, pending_path):
    root = evidence._root(chunk)
    verification = evidence.verify_completed_review(root)
    path = evidence._inside(root, pending_path)
    page = evidence._page(evidence._read(path).get('page'))
    record = evidence._records(verification).get(page)
    if not record or record.get('status') not in ('pending', 'approved_preview'):
        raise ValueError('這一頁沒有可確認的待審中文預覽。')
    pending = evidence._pending(root, record)
    if path != evidence._review_dir(root, page) / 'pending.json':
        raise ValueError('待審紀錄與選取頁面不一致。')
    return root, verification, record, pending


def _rect(value, size, *, xywh=False, angle=0):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in value)):
        raise ValueError('文字框缺少可核對的位置資料。')
    x0, y0, a, b = value
    x1, y1 = (x0 + a, y0 + b) if xywh else (a, b)
    if x1 <= x0 or y1 <= y0:
        raise ValueError('文字框位置資料無效。')
    if angle:
        radians = math.radians(float(angle))
        width, height = x1 - x0, y1 - y0
        dx = (abs(width * math.cos(radians)) + abs(height * math.sin(radians))) / 2
        dy = (abs(width * math.sin(radians)) + abs(height * math.cos(radians))) / 2
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        x0, y0, x1, y1 = cx - dx, cy - dy, cx + dx, cy + dy
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(size[0], x1), min(size[1], y1)
    if x1 <= x0 or y1 <= y0:
        raise ValueError('文字框位於圖片範圍外，無法在預覽中確認。')
    return [x0, y0, x1, y1]


def _progress(root, pending, ids):
    path = evidence._review_dir(root, pending['page']) / 'manual-progress.json'
    binding = evidence._binding(root, path.parent / 'pending.json')
    if not path.exists():
        return {'version': 1, 'page': pending['page'], 'pending': binding,
                'confirmed_ids': [], 'reviewer': 'local-gui-user', 'updated_at': None}
    value = evidence._read(path)
    confirmed = evidence._ids(value.get('confirmed_ids'), empty=True)
    if (value.get('version') != 1 or value.get('page') != pending['page']
            or value.get('pending') != binding or not confirmed <= set(ids)
            or value.get('reviewer') != 'local-gui-user'
            or not isinstance(value.get('updated_at'), str)):
        raise ValueError('已保存的審查進度與目前預覽不一致。')
    return value


def inspect_page(chunk, pending_path):
    """Read-only view model. A page-level recovery requires checking every source ID."""
    root, _, record, pending = _load(chunk, pending_path)
    _, source_rows, rows, blocks, source, *_ = evidence._context(root, pending)
    sources = {row['id']: row for row in source_rows}
    regions = []
    for row in sorted(rows, key=lambda row: (row.get('reading_order', row['source_id']), row['source_id'])):
        sid = row['source_id']
        native = blocks[sid - 1]
        target = native.get('_bounding_rect')
        regions.append({'id': sid, 'japanese': row.get('japanese', ''), 'translation': row['translation'],
                        'source_rect': _rect(sources[sid]['bbox'], source.size),
                        'target_rect': _rect(target if target else native['xyxy'], source.size,
                                             xywh=bool(target), angle=native.get('angle', 0))})
    ids = [region['id'] for region in regions]
    progress = _progress(root, pending, ids)
    if record['status'] == 'approved_preview':
        progress['confirmed_ids'] = ids
    return {'page': pending['page'], 'chunk': str(root),
            'pending_path': str(evidence._review_dir(root, pending['page']) / 'pending.json'),
            'pending_sha256': progress['pending']['sha256'], 'regions': regions,
            'source_path': str(evidence._bound(root, pending['snapshots']['source'])),
            'preview_path': str(evidence._bound(root, pending['snapshots']['base'])),
            'size': list(source.size), 'confirmed_ids': sorted(progress['confirmed_ids']),
            'approved': record['status'] == 'approved_preview'}


def confirm_regions(chunk, pending_path, ids, expected_pending_sha256):
    """Called only for an explicit GUI confirmation, while the folder lock is held."""
    root, _, record, pending = _load(chunk, pending_path)
    view = inspect_page(root, pending_path)
    if view['pending_sha256'] != expected_pending_sha256:
        raise ValueError('待審預覽已變更，請重新開啟後確認。')
    confirmed = evidence._ids(ids)
    all_ids = {region['id'] for region in view['regions']}
    if not confirmed <= all_ids:
        raise ValueError('確認的文字框不屬於本頁。')
    if record['status'] == 'approved_preview':
        return view
    progress = _progress(root, pending, all_ids)
    progress['confirmed_ids'] = sorted(set(progress['confirmed_ids']) | confirmed)
    progress['updated_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
    save_json(evidence._review_dir(root, pending['page']) / 'manual-progress.json', progress)
    return inspect_page(root, pending_path)


def apply_confirmed_page(chunk, pending_path):
    """Publish eligibility is committed last; partial confirmations never pass this gate."""
    root, verification, record, pending = _load(chunk, pending_path)
    if record['status'] == 'approved_preview':
        return verification
    _, _, rows, blocks, source, *_ = evidence._context(root, pending)
    ids = list(range(1, len(blocks) + 1))
    progress = _progress(root, pending, ids)
    if set(progress['confirmed_ids']) != set(ids):
        raise ValueError('仍有文字框尚未人工確認，不能輸出本頁。')
    page = pending['page']
    receipt = {'version': 1, 'status': 'approved_preview', 'page': page,
               'action': 'accept_unchanged_preview', 'pending': progress['pending'],
               'approved_source_ids': ids, 'reviewer': progress['reviewer'],
               'approved_at': progress['updated_at'], 'source': pending['originals']['source'],
               'output': pending['originals']['base'], 'dimensions': list(source.size),
               'page_rows_sha256': evidence._value_sha(rows)}
    path = evidence._review_dir(root, page) / 'preview-approval.json'
    evidence._write(path, evidence._json(receipt))
    binding = evidence._binding(root, path)
    verification['review_required_pages'].pop(page)
    verification.setdefault('review_receipts', {})[page] = dict(
        record, status='approved_preview', receipt_path=binding['path'], receipt_sha256=binding['sha256'])
    verification.setdefault('failed_pages', {}).pop(page, None)
    verification['completed_pages'] = sorted(set(verification.get('completed_pages', [])) | {page})
    verification['images'].append({'page': page, 'path': str(evidence._bound(root, receipt['output'])),
                                   'size': receipt['dimensions'], 'sha256': receipt['output']['sha256']})
    verification['images'].sort(key=lambda entry: entry['page'])
    evidence.verify_completed_review(root, verification)
    save_json(root / 'verification.json', verification)
    return evidence.verify_completed_review(root)


def verify_approval(root, pending, record, verification, translations, entries):
    """Shared scheduler/publisher validation for a human-approved unchanged preview."""
    page = pending['page']
    path = evidence._bound(root, {'path': record.get('receipt_path'), 'sha256': record.get('receipt_sha256')})
    if path != evidence._review_dir(root, page) / 'preview-approval.json':
        raise ValueError('Preview approval receipt belongs to another page')
    receipt = evidence._read(path)
    _, _, rows, blocks, source, *_ = evidence._context(root, pending)
    if (receipt.get('version') != 1 or receipt.get('status') != 'approved_preview'
            or receipt.get('page') != page or receipt.get('action') != 'accept_unchanged_preview'
            or receipt.get('pending') != evidence._binding(root, path.parent / 'pending.json')
            or receipt.get('approved_source_ids') != list(range(1, len(blocks) + 1))
            or receipt.get('reviewer') != 'local-gui-user' or not isinstance(receipt.get('approved_at'), str)
            or receipt.get('source') != pending['originals']['source']
            or receipt.get('output') != pending['originals']['base']
            or receipt.get('dimensions') != list(source.size)
            or receipt.get('page_rows_sha256') != evidence._value_sha(rows)
            or evidence._page_rows(translations, page) != rows
            or page not in verification.get('review_receipts', {})
            or page in verification.get('failed_pages', {})
            or page not in verification.get('completed_pages', [])):
        raise ValueError('Human preview approval differs from the sealed page')
    output = evidence._bound(root, receipt['output'])
    if (len(entries) != 1 or evidence._inside(root, entries[0]['path']) != output
            or entries[0].get('sha256') != receipt['output']['sha256']
            or entries[0].get('size') != receipt['dimensions']):
        raise ValueError('Approved preview output differs from the receipt')
