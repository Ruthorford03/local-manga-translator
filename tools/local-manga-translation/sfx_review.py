"""Hash-bound, explicitly reviewed original-pixel restoration for one manga page.

This module does no detection, translation, or native rendering.  The canonical
project remains evidence; a single-page editable copy records approved changes.
verification.json is the final commit record. Readers must call the shared gate.
All rectangles are integer, half-open [left, top, right, bottom] coordinates.
"""
from copy import deepcopy
import csv
import hashlib
from io import BytesIO, StringIO
import json
import math
import os
from pathlib import Path
import re
import tempfile


VERSION = 1
FIELDS = ['page', 'source_id', 'reading_order', 'japanese', 'translation',
          'font_size', 'small_text_review']
PNG_MODES = {'L', 'LA', 'RGB', 'RGBA'}
# This revision only adds unchanged-preview approval. Original-pixel restoration
# remains compatible with decisions sealed by the immediately preceding version.
LEGACY_RESTORE_IMPLEMENTATIONS = {'a32039e8c69bc72efb3883ae276fbfb48d5286b83a27ec5745659e2f6b3aa009'}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _value_sha(value):
    return _sha(json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False).encode('utf-8'))


def _root(chunk):
    root = Path(chunk).resolve()
    if not root.is_dir():
        raise ValueError('Chunk directory does not exist')
    return root


def _inside(root, value):
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError('Artifact path is missing')
    path = Path(value)
    resolved = path.resolve()
    if resolved.is_relative_to(root) and resolved != root:
        return resolved
    path = (path if path.is_absolute() else root / path).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError('Artifact path escapes the chunk')
    return path


def _page(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+\.png', value)
            or value in ('.png', '..png')):
        raise ValueError('Review page must be a PNG basename')
    return value


def _binding(root, path):
    path = _inside(root, path)
    return {'path': path.relative_to(root).as_posix(), 'sha256': _sha(path.read_bytes())}


def _bound(root, binding):
    if (not isinstance(binding, dict) or not isinstance(binding.get('sha256'), str)
            or not re.fullmatch('[0-9a-f]{64}', binding['sha256'])):
        raise ValueError('Artifact SHA-256 binding is missing')
    path = _inside(root, binding.get('path'))
    if not path.is_file() or _sha(path.read_bytes()) != binding['sha256']:
        raise ValueError(f'Artifact missing or changed: {path.name}')
    return path


def _cancel(cancel_file):
    if cancel_file is not None and (cancel_file.exists() if hasattr(cancel_file, 'exists') else Path(cancel_file).exists()):
        raise InterruptedError('SFX review cancelled')


def _write(path, raw, *, replace=False):
    """Write complete bytes atomically; immutable artifacts allow exact retry only."""
    path = Path(path)
    if not replace and path.exists():
        if path.read_bytes() != raw:
            raise ValueError(f'Immutable review artifact already differs: {path.name}')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.sfx-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != raw:
                    raise ValueError(f'Immutable review artifact already differs: {path.name}')
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _png(raw):
    from PIL import Image
    with Image.open(BytesIO(raw)) as image:
        if image.format != 'PNG' or image.mode not in PNG_MODES:
            raise ValueError('Review requires L/LA/RGB/RGBA PNG pixels')
        if image.width * image.height > 100_000_000 or max(image.size) > 20_000:
            raise ValueError('Review image dimensions exceed the safety bound')
        image.load()
        return image.copy()


def _encode(image):
    stream = BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue()


def _rectangle(value, dimensions):
    if (not isinstance(value, list) or len(value) != 4
            or any(type(number) is not int for number in value)):
        raise ValueError('ROI must contain four integer half-open coordinates')
    left, top, right, bottom = value
    width, height = dimensions
    if not 0 <= left < right <= width or not 0 <= top < bottom <= height:
        raise ValueError('ROI is empty or outside image bounds')
    return tuple(value)


def _ids(value, *, empty=False):
    if (not isinstance(value, list) or len(value) > 512 or (not value and not empty)
            or any(type(item) is not int or not 0 < item <= 2_147_483_647 for item in value)
            or len(set(value)) != len(value)):
        raise ValueError('Source IDs must be unique positive integers')
    return set(value)


def _changed_pixels(left, right):
    from PIL import ImageChops
    if left.size != right.size or left.mode != right.mode:
        raise ValueError('Compared PNG dimensions or modes differ')
    channels = ImageChops.difference(left, right).split()
    difference = channels[0]
    for channel in channels[1:]:
        difference = ImageChops.lighter(difference, channel)
    return difference.point([0] + [255] * 255)


def verify_result(source, base, result, allowed_rectangles):
    """Check all channels, including RGB changes in opaque RGBA images."""
    from PIL import Image, ImageChops, ImageDraw
    if not allowed_rectangles:
        raise ValueError('At least one ROI is required')
    mask = Image.new('L', base.size, 0)
    drawing = ImageDraw.Draw(mask)
    for rect in allowed_rectangles:
        left, top, right, bottom = _rectangle(list(rect), list(base.size))
        drawing.rectangle((left, top, right - 1, bottom - 1), fill=255)
    changed = _changed_pixels(base, result)
    original = _changed_pixels(source, result)
    outside = ImageChops.multiply(changed, ImageChops.invert(mask)).histogram()[255]
    inside = ImageChops.multiply(original, mask).histogram()[255]
    return {'verified': outside == 0 and inside == 0,
            'diff_outside_union_pixels': outside, 'roi_original_mismatch_pixels': inside,
            'roi_recovered_original_exact': inside == 0,
            'changed_pixels_total': changed.histogram()[255],
            'allowed_union_pixels': mask.histogram()[255]}


def _page_rows(document, page):
    items = document.get('items')
    if not isinstance(items, list) or any(not isinstance(row, dict) for row in items):
        raise ValueError('translations.json must have item records')
    return [row for row in items if row.get('page') == page]


def _csv(document):
    stream = StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=FIELDS, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(document['items'])
    return stream.getvalue().encode('utf-8-sig')


def _review_dir(root, page):
    return _inside(root, Path('_audit/sfx-review') / Path(_page(page)).stem)


def _record(pending, root):
    binding = _binding(root, pending)
    return dict(status='pending', pending_path=binding['path'], pending_sha256=binding['sha256'])


def _pending(root, record):
    path = _bound(root, {'path': record.get('pending_path'), 'sha256': record.get('pending_sha256')})
    pending = _read(path)
    if pending.get('version') != VERSION or pending.get('status') != 'pending':
        raise ValueError('Unsupported pending review')
    page = _page(pending.get('page'))
    if path != _review_dir(root, page) / 'pending.json':
        raise ValueError('Pending review belongs to a different page')
    for name, binding in pending['originals'].items():
        if name == 'project':
            live = _read(_inside(root, binding.get('path')))
            if _value_sha(live.get('pages', {}).get(page)) != pending.get('canonical_page_sha256'):
                raise ValueError('Canonical native page blocks changed after review registration')
        else:
            _bound(root, binding)
    for binding in pending['snapshots'].values():
        _bound(root, binding)
    return pending


def _context(root, pending):
    snapshots = pending['snapshots']
    project = _read(_bound(root, snapshots['project']))
    source_rows = _read(_bound(root, snapshots['reading_order']))[pending['page']]['rows']
    translations = _read(_bound(root, snapshots['translations']))
    page = pending['page']
    blocks = project['pages'][page]
    rows = _page_rows(translations, page)
    expected = set(range(1, len(blocks) + 1))
    if not expected or len(expected) > 512:
        raise ValueError('Reviewed page must have 1 to 512 source blocks')
    if _ids([row.get('id') for row in source_rows]) != expected:
        raise ValueError('Reading-order IDs do not cover the native source blocks')
    if _ids([row.get('source_id') for row in rows]) != expected:
        raise ValueError('Translation IDs do not cover the native source blocks')
    for row in rows:
        native_text = blocks[row['source_id'] - 1].get('translation')
        export_text = row.get('translation')
        if (not isinstance(native_text, str) or not isinstance(export_text, str)
                or native_text.replace('\n', '').replace('\r', '') != export_text.replace('\n', '').replace('\r', '')):
            raise ValueError('Native translation differs from the bound export record')
    source = _png(_bound(root, snapshots['source']).read_bytes())
    base = _png(_bound(root, snapshots['base']).read_bytes())
    inpainted = _png(_bound(root, snapshots['inpainted']).read_bytes())
    mask = _png(_bound(root, snapshots['mask']).read_bytes())
    if source.size != base.size or source.mode != base.mode:
        raise ValueError('Source/base PNG dimensions or modes differ')
    if inpainted.size != source.size or inpainted.mode != source.mode or mask.size != source.size:
        raise ValueError('Editable project raster dimensions or modes differ')
    return project, source_rows, rows, blocks, source, base, inpainted, mask


def _records(verification):
    pending = verification.get('review_required_pages', {})
    applied = verification.get('review_receipts', {})
    if not isinstance(pending, dict) or not isinstance(applied, dict) or set(pending) & set(applied):
        raise ValueError('Review ledger has conflicting page states')
    return dict(pending, **applied)


def register_review(chunk, page, reason, *, cancel_file=None):
    """Seal canonical evidence and remove a reviewed page from publishable images."""
    root, page = _root(chunk), _page(page)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('A review reason is required')
    _cancel(cancel_file)
    # A draft may be intentionally held in review_registration_pending. Validate
    # the export hash here without pretending that it is already publishable.
    verification = _read(root / 'verification.json')
    if verification.get('translation_sha256') != _sha((root / 'translations.json').read_bytes()):
        raise ValueError('Cannot register review against changed translations')
    records = _records(verification)
    if page in records:
        _pending(root, records[page])
        return deepcopy(records[page])
    review_dir = _review_dir(root, page)
    pending_path = review_dir / 'pending.json'
    canonical = {'project': root / 'project/imgtrans_project.json',
                 'reading_order': root / '_audit/reading-order.json',
                 'source': root / 'project' / page, 'base': root / 'project/result' / page,
                 'inpainted': root / 'project/inpainted' / page,
                 'mask': root / 'project/mask' / page}
    originals = {name: _binding(root, path) for name, path in canonical.items()}
    if page not in _read(canonical['project']).get('pages', {}):
        raise ValueError('Page is not part of the canonical project')
    matches = [entry for entry in verification.get('images', []) if entry.get('page') == page]
    if len(matches) != 1 or _inside(root, matches[0]['path']) != canonical['base'].resolve():
        raise ValueError('Review requires exactly one canonical rendered draft image')
    if matches[0]['sha256'] != originals['base']['sha256']:
        raise ValueError('Rendered draft hash differs from verification')
    snapshots = {}
    files = dict(canonical, translations=root / 'translations.json',
                 csv=root / 'translations.csv', verification=root / 'verification.json')
    for name, path in files.items():
        _cancel(cancel_file)
        destination = review_dir / 'baseline' / (name + path.suffix)
        _write(destination, _inside(root, path).read_bytes())
        snapshots[name] = _binding(root, destination)
    pending = {'version': VERSION, 'status': 'pending', 'page': page, 'reason': reason,
               'canonical_page_sha256': _value_sha(_read(canonical['project'])['pages'][page]),
               'originals': originals, 'snapshots': snapshots}
    _context(root, pending)
    _write(pending_path, _json(pending))
    record = _record(pending_path, root)
    record['reason'] = reason
    verification.setdefault('review_required_pages', {})[page] = record
    verification['images'] = [entry for entry in verification['images'] if entry['page'] != page]
    verification['completed_pages'] = [name for name in verification.get('completed_pages', []) if name != page]
    verification.setdefault('failed_pages', {})[page] = {'type': 'ReviewRequired', 'reason': reason}
    verification['review_registration_pending'] = [name for name in verification.get('review_registration_pending', []) if name != page]
    _cancel(cancel_file)
    _write(root / 'verification.json', _json(verification), replace=True)
    return deepcopy(record)


def _normal_region(block, row, dimensions):
    """Conservative native/OCR/line bounds plus one font em of ink overhang.

    Unsupported transforms/effects fail closed instead of guessing their extents.
    The whole native block is additionally compared by value in the verifier.
    """
    font = block.get('fontformat', {})
    if (block.get('angle', 0) != 0 or font.get('glyph_slant_angle', 0) != 0
            or font.get('text_transform') or font.get('shadow_radius', 0) != 0
            or any(font.get('shadow_offset', [0, 0])) or font.get('stroke_width', 0) != 0):
        raise ValueError('Protected normal block uses unsupported transforms/effects')
    effects = font.get('text_effects', {})
    if isinstance(effects, dict):
        for effect in effects.get('effects', []):
            if effect.get('enabled', True) and (effect.get('effect_type', effect.get('type')) != 'stroke' or effect.get('width', 0) != 0):
                raise ValueError('Protected normal block has an unsupported text effect')
    rectangles = [block.get('xyxy'), row.get('bbox')]
    native = block.get('_bounding_rect')
    if not isinstance(native, list) or len(native) != 4:
        raise ValueError('Protected normal block has no native text bounds')
    rectangles.append([native[0], native[1], native[0] + native[2], native[1] + native[3]])
    points = [point for line in block.get('lines', []) for point in line]
    if points:
        rectangles.append([min(p[0] for p in points), min(p[1] for p in points),
                           max(p[0] for p in points), max(p[1] for p in points)])
    for rect in rectangles:
        if (not isinstance(rect, list) or len(rect) != 4
                or any(type(n) not in (float, int) or not math.isfinite(n) for n in rect)
                or rect[0] >= rect[2] or rect[1] >= rect[3]):
            raise ValueError('Protected normal geometry is invalid')
    font_size = font.get('font_size')
    if type(font_size) not in (int, float) or not math.isfinite(font_size) or not 0 < font_size <= 2000:
        raise ValueError('Protected normal font size is invalid')
    padding = max(2, math.ceil(font_size))
    return [max(0, math.floor(min(r[0] for r in rectangles)) - padding),
            max(0, math.floor(min(r[1] for r in rectangles)) - padding),
            min(dimensions[0], math.ceil(max(r[2] for r in rectangles)) + padding),
            min(dimensions[1], math.ceil(max(r[3] for r in rectangles)) + padding)]


def _decision_data(root, pending, issue_ids, rois, reviewer, reason):
    if any(not isinstance(value, str) or not value.strip() for value in (reviewer, reason)):
        raise ValueError('Explicit reviewer and review reason are required')
    project, source_rows, rows, blocks, source, *_ = _context(root, pending)
    issues = _ids(issue_ids)
    all_ids = set(range(1, len(blocks) + 1))
    if not issues <= all_ids:
        raise ValueError('Issue ID is not in the canonical page')
    normals = all_ids - issues
    if not isinstance(rois, list) or not 1 <= len(rois) <= 1024:
        raise ValueError('Approved ROI list must be bounded and nonempty')
    allowed = []
    for roi in rois:
        if not isinstance(roi, dict) or type(roi.get('source_id')) is not int or roi['source_id'] not in issues:
            raise ValueError('ROI refers to an unapproved issue ID')
        allowed.append({'source_id': roi['source_id'], 'rect': list(_rectangle(roi.get('rect'), source.size))})
    if {roi['source_id'] for roi in allowed} != issues:
        raise ValueError('Approved ROIs must cover every issue ID')
    source_by_id = {row['id']: row for row in source_rows}
    protected = [{'source_id': sid, 'rect': _normal_region(blocks[sid - 1], source_by_id[sid], source.size)}
                 for sid in sorted(normals)]
    for roi in allowed:
        left, top, right, bottom = roi['rect']
        for normal in protected:
            x0, y0, x1, y1 = normal['rect']
            if left < x1 and right > x0 and top < y1 and bottom > y0:
                raise ValueError('Approved ROI overlaps a protected normal block')
    return {'version': VERSION, 'action': 'restore_original_pixels', 'page': pending['page'],
            'review': {'reviewer': reviewer, 'reason': reason},
            'pending': _binding(root, _review_dir(root, pending['page']) / 'pending.json'),
            'implementation_sha256': _sha(Path(__file__).read_bytes()),
            'source': pending['originals']['source'], 'base': pending['originals']['base'],
            'dimensions': list(source.size), 'mode': source.mode,
            'issue_source_ids': sorted(issues), 'normal_source_ids': sorted(normals),
            'allowed_rois': allowed, 'normal_regions': protected,
            'source_rows_sha256': _value_sha(source_rows),
            'normal_records_sha256': _value_sha([row for row in rows if row['source_id'] in normals]),
            'normal_native_states_sha256': _value_sha([blocks[sid - 1] for sid in sorted(normals)]),
            'page_native_states_sha256': _value_sha(project['pages'][pending['page']])}


def build_decision(chunk, page, issue_ids, rois, *, reviewer, reason):
    """Write a fixed explicit review decision; no classifier can supply approval."""
    root, page = _root(chunk), _page(page)
    verification = verify_completed_review(root)
    record = _records(verification).get(page)
    if record is None or record.get('status') != 'pending':
        raise ValueError('Page must have a pending review before a decision is created')
    pending = _pending(root, record)
    decision = _decision_data(root, pending, issue_ids, rois, reviewer, reason)
    path = _review_dir(root, page) / 'decision.json'
    _write(path, _json(decision))
    return path


def _decision(root, pending, path):
    path = _inside(root, path)
    decision = _read(path)
    expected = _decision_data(root, pending, decision.get('issue_source_ids'),
                              decision.get('allowed_rois'), decision.get('review', {}).get('reviewer'),
                              decision.get('review', {}).get('reason'))
    if decision.get('implementation_sha256') in LEGACY_RESTORE_IMPLEMENTATIONS:
        expected['implementation_sha256'] = decision['implementation_sha256']
    if decision != expected:
        raise ValueError('Decision identity, protection, or evidence binding differs')
    return decision


def _restore(source, base, rectangles):
    result = base.copy()
    for rect in dict.fromkeys(tuple(rect) for rect in rectangles):
        result.paste(source.crop(rect), rect)
    raw = _encode(result)
    report = verify_result(source, base, _png(raw), rectangles)
    if not report['verified']:
        raise ValueError('Original-pixel restoration failed verification')
    return raw, report


def _reviewed_artifacts(root, pending, decision):
    project, _, _, blocks, source, base, inpainted, mask = _context(root, pending)
    page = pending['page']
    directory = _review_dir(root, page)
    edited = directory / 'reviewed-project'
    rects = [roi['rect'] for roi in decision['allowed_rois']]
    output_raw, pixels = _restore(source, base, rects)
    inpaint_raw, inpaint_pixels = _restore(source, inpainted, rects)
    zero = mask.copy()
    for rect in rects:
        zero.paste(0, tuple(rect))
    mask_raw = _encode(zero)
    black = mask.copy()
    black.paste(0, (0, 0, black.width, black.height))
    mask_pixels = verify_result(black, mask, _png(mask_raw), rects)
    if not mask_pixels['verified']:
        raise ValueError('Reviewed mask changes escaped the approved ROIs')
    reviewed_blocks = deepcopy(blocks)
    for sid in decision['issue_source_ids']:
        reviewed_blocks[sid - 1]['translation'] = ''
        reviewed_blocks[sid - 1]['rich_text'] = ''
    reviewed_project = deepcopy(project)
    reviewed_project['directory'] = str(edited)
    reviewed_project['pages'] = {page: reviewed_blocks}
    reviewed_project['current_img'] = page
    reviewed_project['image_info'] = {page: deepcopy(project.get('image_info', {}).get(page, {}))}
    files = {'output': (directory / 'reviewed.png', output_raw),
             'editable_result': (edited / 'result' / page, output_raw),
             'editable_source': (edited / page, _bound(root, pending['snapshots']['source']).read_bytes()),
             'editable_inpainted': (edited / 'inpainted' / page, inpaint_raw),
             'editable_mask': (edited / 'mask' / page, mask_raw),
             'editable_project': (edited / 'imgtrans_project.json', _json(reviewed_project))}
    artifacts = {}
    for name, (path, raw) in files.items():
        _write(_inside(root, path), raw)
        artifacts[name] = _binding(root, path)
    return artifacts, {'output': pixels, 'inpainted': inpaint_pixels, 'mask': mask_pixels}


def _verify_applied(root, pending, decision, receipt, translations):
    if receipt.get('version') != VERSION or receipt.get('status') != 'applied' or receipt.get('page') != pending['page']:
        raise ValueError('Invalid applied review receipt')
    if receipt.get('decision') != _binding(root, _bound(root, receipt.get('decision'))):
        raise ValueError('Decision receipt binding differs')
    if _read(_bound(root, receipt['decision'])) != decision:
        raise ValueError('Receipt refers to a different decision')
    if receipt.get('pending') != decision['pending'] or receipt.get('implementation_sha256') != decision['implementation_sha256']:
        raise ValueError('Receipt evidence or implementation binding differs')
    for binding in receipt.get('artifacts', {}).values():
        _bound(root, binding)
    for binding in receipt.get('transaction_inputs', {}).values():
        _bound(root, binding)
    for binding in receipt.get('transaction_outputs', {}).values():
        _bound(root, binding)
    project, _, rows, blocks, source, base, inpainted, mask = _context(root, pending)
    issues, normals = set(decision['issue_source_ids']), set(decision['normal_source_ids'])
    normal_rows = [row for row in rows if row['source_id'] in normals]
    if _page_rows(translations, pending['page']) != normal_rows:
        raise ValueError('Applied page translation records differ from protected records')
    before_exports = _read(_bound(root, receipt['transaction_inputs']['translations']))
    expected_exports = deepcopy(before_exports)
    expected_exports['items'] = [row for row in expected_exports['items']
                                 if not (row.get('page') == pending['page'] and row.get('source_id') in issues)]
    saved_exports = _read(_bound(root, receipt['transaction_outputs']['translations']))
    if saved_exports != expected_exports:
        raise ValueError('Review transaction changed unrelated translation records')
    if _bound(root, receipt['transaction_outputs']['csv']).read_bytes() != _csv(expected_exports):
        raise ValueError('Review transaction CSV differs from expected records')
    artifacts = receipt['artifacts']
    edited = _read(_bound(root, artifacts['editable_project']))
    expected = deepcopy(project)
    expected['directory'] = str(_review_dir(root, pending['page']) / 'reviewed-project')
    expected['current_img'] = pending['page']
    edited_blocks = deepcopy(blocks)
    for sid in issues:
        edited_blocks[sid - 1]['translation'] = ''
        edited_blocks[sid - 1]['rich_text'] = ''
    expected['pages'] = {pending['page']: edited_blocks}
    expected['image_info'] = {pending['page']: deepcopy(project.get('image_info', {}).get(pending['page'], {}))}
    if edited != expected:
        raise ValueError('Reviewed native project altered protected metadata or IDs')
    if _bound(root, artifacts['editable_source']).read_bytes() != _bound(root, pending['snapshots']['source']).read_bytes():
        raise ValueError('Reviewed source is not the exact original file')
    if _bound(root, artifacts['editable_result']).read_bytes() != _bound(root, artifacts['output']).read_bytes():
        raise ValueError('Editable result and published review pixels differ')
    rectangles = [roi['rect'] for roi in decision['allowed_rois']]
    black = mask.copy()
    black.paste(0, (0, 0, black.width, black.height))
    reports = {'output': verify_result(source, base, _png(_bound(root, artifacts['output']).read_bytes()), rectangles),
               'inpainted': verify_result(source, inpainted, _png(_bound(root, artifacts['editable_inpainted']).read_bytes()), rectangles),
               'mask': verify_result(black, mask, _png(_bound(root, artifacts['editable_mask']).read_bytes()), rectangles)}
    if any(not report['verified'] for report in reports.values()) or receipt.get('pixels') != reports:
        raise ValueError('Receipt pixel invariants failed')
    if receipt.get('issue_source_ids') != decision['issue_source_ids'] or receipt.get('normal_source_ids') != decision['normal_source_ids']:
        raise ValueError('Receipt source IDs differ from the decision')


def verify_completed_review(chunk, verification=None):
    """Shared scheduler/publisher gate. A pending page can never be published."""
    root = _root(chunk)
    verification = deepcopy(_read(root / 'verification.json') if verification is None else verification)
    raw = (root / 'translations.json').read_bytes()
    if verification.get('translation_sha256') != _sha(raw):
        raise ValueError('Translation hash differs; review may be interrupted')
    translations = json.loads(raw.decode('utf-8-sig'))
    records = _records(verification)
    if verification.get('review_registration_pending'):
        raise ValueError('Recovered draft review registration has not completed')
    recovery_dir = root / '_audit/translation'
    if recovery_dir.is_dir():
        for path in recovery_dir.glob('*-block-recovery.json'):
            recovery = _read(path)
            page = path.name.removesuffix('-block-recovery.json') + '.png'
            if recovery.get('accepted') is True and recovery.get('semantic_review_required') is True and page not in records:
                raise ValueError('Recovered draft has no registered semantic review')
    if not records:
        return verification
    images = verification.get('images', [])
    for page, record in records.items():
        pending = _pending(root, record)
        if pending['page'] != page:
            raise ValueError('Review ledger page differs from evidence')
        entries = [entry for entry in images if entry.get('page') == page]
        if record.get('status') == 'pending':
            if page not in verification.get('review_required_pages', {}) or verification.get('failed_pages', {}).get(page, {}).get('type') != 'ReviewRequired':
                raise ValueError('Pending review lacks the required failure state')
            if entries or page in verification.get('completed_pages', []):
                raise ValueError('Pending review page was included as completed')
            original_rows = _page_rows(_read(_bound(root, pending['snapshots']['translations'])), page)
            if _page_rows(translations, page) != original_rows:
                raise ValueError('Pending page translation records have changed')
            continue
        if record.get('status') == 'approved_preview':
            from page_review import verify_approval
            verify_approval(root, pending, record, verification, translations, entries)
            continue
        if record.get('status') != 'applied':
            raise ValueError('Unknown SFX review status')
        if page not in verification.get('review_receipts', {}) or page in verification.get('failed_pages', {}):
            raise ValueError('Applied review has conflicting completion state')
        receipt = _read(_bound(root, {'path': record.get('receipt_path'), 'sha256': record.get('receipt_sha256')}))
        decision = _decision(root, pending, _bound(root, receipt.get('decision')))
        _verify_applied(root, pending, decision, receipt, translations)
        expected_project = str(_review_dir(root, page) / 'reviewed-project')
        expected_regions = {'issue_source_ids': decision['issue_source_ids'],
                            'allowed_rois': decision['allowed_rois'], 'decision': receipt['decision']}
        if (verification.get('reviewed_projects', {}).get(page) != expected_project
                or verification.get('preserved_source_regions', {}).get(page) != expected_regions):
            raise ValueError('Applied review editable-project or preserved-region ledger differs')
        output = receipt['artifacts']['output']
        if (len(entries) != 1 or _inside(root, entries[0]['path']) != _bound(root, output)
                or entries[0].get('sha256') != output['sha256']
                or entries[0].get('size') != decision['dimensions']
                or page not in verification.get('completed_pages', [])):
            raise ValueError('Applied review image differs from the committed receipt')
    if (root / 'translations.csv').read_bytes() != _csv(translations):
        raise ValueError('Review translation CSV differs from the protected export records')
    return verification


def _commit_transaction(root, path, cancel_file):
    transaction = _read(path)
    for binding in transaction['artifacts'].values():
        _bound(root, binding)
    for binding in transaction['before'].values():
        _bound(root, binding)
    before = transaction['before']
    after = transaction['artifacts']
    staged_verification = _read(_bound(root, after['verification']))
    page = transaction['page']
    record = _records(staged_verification).get(page)
    if record is None or record.get('status') != 'applied':
        raise ValueError('Staged verification does not contain the applied review')
    pending = _pending(root, record)
    receipt_path = _bound(root, transaction['receipt'])
    receipt = _read(receipt_path)
    if (transaction['decision'] != receipt.get('decision') or receipt.get('transaction_inputs') != before
            or receipt.get('transaction_outputs') != {key: after[key] for key in ('translations', 'csv')}
            or record.get('receipt_path') != transaction['receipt']['path']
            or record.get('receipt_sha256') != transaction['receipt']['sha256']
            or staged_verification.get('translation_sha256') != after['translations']['sha256']):
        raise ValueError('Staged transaction binding differs from the applied receipt')
    decision = _decision(root, pending, _bound(root, transaction['decision']))
    _verify_applied(root, pending, decision, receipt, _read(_bound(root, after['translations'])))
    current_verification = _sha((root / 'verification.json').read_bytes())
    if current_verification == after['verification']['sha256']:
        return verify_completed_review(root)
    if current_verification != before['verification']['sha256']:
        raise ValueError('Review transaction encountered a different verification ledger')
    for name, filename in (('translations', 'translations.json'), ('csv', 'translations.csv')):
        if _sha((root / filename).read_bytes()) not in {before[name]['sha256'], after[name]['sha256']}:
            raise ValueError('Review transaction encountered externally changed exports')
    # CSV/JSON may temporarily disagree with the old ledger after interruption;
    # the common reader gate refuses that state until this exact transaction resumes.
    for name, filename in (('csv', 'translations.csv'), ('translations', 'translations.json'),
                           ('verification', 'verification.json')):
        _cancel(cancel_file)
        _write(root / filename, _bound(root, after[name]).read_bytes(), replace=True)
    return verify_completed_review(root)


def apply_decision(chunk, decision_path, *, cancel_file=None):
    """Stage complete reviewed artifacts, then commit exports and the ledger last."""
    root = _root(chunk)
    _cancel(cancel_file)
    decision_path = _inside(root, decision_path)
    decision = _read(decision_path)
    page = _page(decision.get('page'))
    verification = _read(root / 'verification.json')
    record = _records(verification).get(page)
    if record is None:
        raise ValueError('A registered pending review is required')
    pending = _pending(root, record)
    decision = _decision(root, pending, decision_path)
    if record.get('status') == 'applied':
        receipt = _read(_bound(root, {'path': record['receipt_path'], 'sha256': record['receipt_sha256']}))
        if receipt['decision'] != _binding(root, decision_path):
            raise ValueError('Page was already applied with a different decision')
        return verify_completed_review(root)
    if record.get('status') != 'pending':
        raise ValueError('Review is not pending')
    transaction_dir = _review_dir(root, page) / 'transactions' / _sha(decision_path.read_bytes())
    transaction_path = transaction_dir / 'transaction.json'
    if transaction_path.exists():
        return _commit_transaction(root, transaction_path, cancel_file)
    verification = verify_completed_review(root, verification)
    artifacts, pixels = _reviewed_artifacts(root, pending, decision)
    _cancel(cancel_file)
    before = {}
    for name, filename in (('translations', 'translations.json'), ('csv', 'translations.csv'),
                           ('verification', 'verification.json')):
        path = transaction_dir / ('before-' + filename)
        _write(path, (root / filename).read_bytes())
        before[name] = _binding(root, path)
    translations = _read(root / 'translations.json')
    issues = set(decision['issue_source_ids'])
    translations['items'] = [row for row in translations['items']
                             if not (row.get('page') == page and row.get('source_id') in issues)]
    after = {}
    for name, filename, raw in (('translations', 'translations.json', _json(translations)),
                                ('csv', 'translations.csv', _csv(translations))):
        path = transaction_dir / ('after-' + filename)
        _write(path, raw)
        after[name] = _binding(root, path)
    receipt = {'version': VERSION, 'status': 'applied', 'page': page,
               'decision': _binding(root, decision_path), 'pending': decision['pending'],
               'implementation_sha256': decision['implementation_sha256'],
               'issue_source_ids': decision['issue_source_ids'],
               'normal_source_ids': decision['normal_source_ids'],
               'artifacts': artifacts, 'pixels': pixels, 'transaction_inputs': before,
               'transaction_outputs': deepcopy(after)}
    receipt_path = transaction_dir / 'receipt.json'
    _write(receipt_path, _json(receipt))
    _verify_applied(root, pending, decision, receipt, translations)
    receipt_binding = _binding(root, receipt_path)
    applied = dict(record, status='applied', receipt_path=receipt_binding['path'],
                   receipt_sha256=receipt_binding['sha256'], issue_source_ids=decision['issue_source_ids'])
    verification['review_required_pages'].pop(page)
    verification.setdefault('review_receipts', {})[page] = applied
    verification.setdefault('reviewed_projects', {})[page] = str(_review_dir(root, page) / 'reviewed-project')
    verification.setdefault('preserved_source_regions', {})[page] = {
        'issue_source_ids': decision['issue_source_ids'], 'allowed_rois': decision['allowed_rois'],
        'decision': receipt['decision']}
    verification['images'].append({'page': page, 'path': str(_bound(root, artifacts['output'])),
                                   'size': decision['dimensions'], 'sha256': artifacts['output']['sha256']})
    verification['images'].sort(key=lambda entry: entry['page'])
    verification['completed_pages'] = sorted(set(verification.get('completed_pages', [])) | {page})
    verification.setdefault('failed_pages', {}).pop(page, None)
    verification['blocks'] = len(translations['items'])
    verification['translation_sha256'] = after['translations']['sha256']
    path = transaction_dir / 'after-verification.json'
    _write(path, _json(verification))
    after['verification'] = _binding(root, path)
    _write(transaction_path, _json({'version': VERSION, 'page': page, 'decision': _binding(root, decision_path),
                                   'before': before, 'artifacts': after, 'receipt': receipt_binding}))
    return _commit_transaction(root, transaction_path, cancel_file)
