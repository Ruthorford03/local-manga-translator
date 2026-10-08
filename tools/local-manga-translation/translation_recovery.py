"""Recover aligned draft text after full-page retries, for mandatory review.

The caller decides whether a page exhausted its normal alignment retries.
Structural acceptance here is never semantic approval or permission to publish.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re


MAX_BLOCKS = 32
MAX_SOURCE_LENGTH = 8192


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _check_cancel(cancel_file):
    if cancel_file is not None and (cancel_file.exists() if hasattr(cancel_file, 'exists')
                                    else Path(cancel_file).exists()):
        raise InterruptedError('Translation cancelled by the user')


def _source_rows(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_BLOCKS:
        raise ValueError('Recovery requires 1 to 32 source blocks')
    seen, source = set(), []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Recovery source row must be an object')
        source_id, text, box = row.get('id'), row.get('japanese'), row.get('bbox')
        if type(source_id) is not int or not 0 < source_id <= 2_147_483_647 or source_id in seen:
            raise ValueError('Recovery source IDs must be unique positive integers')
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_SOURCE_LENGTH:
            raise ValueError('Recovery source text is empty or too long')
        if (not isinstance(box, (list, tuple)) or len(box) != 4
                or any(type(number) not in (int, float) or not -1_000_000 <= number <= 1_000_000
                       or not math.isfinite(number) for number in box)
                or box[2] <= box[0] or box[3] <= box[1]):
            raise ValueError('Recovery source bounding box is invalid')
        seen.add(source_id)
        source.append({'id': source_id, 'japanese': text, 'bbox': list(box)})
    return source


def _single_line(result):
    if not isinstance(result, dict) or not isinstance(result.get('content'), str):
        raise ValueError('Recovery response has no text')
    metrics = result.get('metrics')
    if (not isinstance(metrics, dict) or metrics.get('done') is not True
            or metrics.get('done_reason') != 'stop'):
        raise ValueError('Recovery response did not finish normally')
    lines = [line for line in result['content'].splitlines() if line.strip()]
    if len(lines) != 1 or not lines[0].strip().strip('「」').strip():
        raise ValueError('Recovery requires exactly one nonempty translated line')
    # Preserve the model's actual nonempty line, including punctuation and
    # quotes. Normalization remains owned by the existing page caller.
    return lines[0]


def recover_page(server, rows, label, output, *, payload_for, chat, read, save,
                 cancel_file=None):
    """Return a fully assembled page only after every source block validates.

    Existing production payload/chat/read/save functions are passed directly so
    this isolated helper neither imports the page caller nor changes its prompt.
    The assembled content is explicitly documented as several model responses;
    it is never written to the normal full-page response-cache filename.
    """
    source = _source_rows(rows)
    if not isinstance(label, str) or not re.fullmatch(r'[\w.-]{1,100}', label) or label in ('.', '..'):
        raise ValueError('Recovery label must be a safe page basename')
    output = Path(output)
    if not output.is_dir():
        raise ValueError('Recovery audit directory does not exist')
    identity = read(Path(server.output) / 'server.json')
    model_digest = identity.get('model_manifest_sha256')
    if (identity.get('model') != server.model or not isinstance(model_digest, str)
            or not re.fullmatch(r'[0-9a-fA-F]{64}', model_digest)):
        raise ValueError('Recovery model identity is invalid')
    manifest_path = output / (label + '-block-recovery.json')
    manifest = {'version': 1, 'page_label': label, 'status': 'in_progress', 'accepted': False,
                'semantic_review_required': True,
                'method': 'one_source_block_per_existing_model_request',
                'assembled_from_independent_responses': True,
                'model': server.model, 'model_manifest_sha256': model_digest.lower(),
                'source_rows_sha256': _digest(source),
                'source_count': len(source), 'blocks': []}
    save(manifest_path, manifest)
    lines = []
    try:
        for position, row in enumerate(source, 1):
            _check_cancel(cancel_file)
            block_label = f'{label}-block-{row["id"]}'
            payload = payload_for(server.model, [deepcopy(row)])
            result = chat(server, payload, block_label, output, reuse_dir=output,
                          cancel_file=cancel_file, expected_lines=1)
            _check_cancel(cancel_file)
            line = _single_line(result)
            request_path = output / (block_label + '-request.json')
            response_path = output / (block_label + '-response.json')
            if not request_path.is_file() or not response_path.is_file():
                raise ValueError('Recovery raw request/response audit is missing')
            persisted = read(response_path)
            if persisted.get('content') != result['content'] or _single_line(persisted) != line:
                raise ValueError('Recovery response audit differs from returned text')
            record = {'reading_order': position, 'source_id': row['id'], 'bbox': row['bbox'],
                      'source_text_sha256': hashlib.sha256(row['japanese'].encode('utf-8')).hexdigest(),
                      'source_record_sha256': _digest(row),
                      'request_file': request_path.name, 'response_file': response_path.name,
                      'request_sha256': hashlib.sha256(request_path.read_bytes()).hexdigest(),
                      'response_sha256': hashlib.sha256(response_path.read_bytes()).hexdigest(),
                      'response_content_sha256': hashlib.sha256(result['content'].encode('utf-8')).hexdigest()}
            lines.append(line)
            manifest['blocks'].append(record)
            save(manifest_path, manifest)
        _check_cancel(cancel_file)
        assembled = '\n'.join(lines)
        manifest.update(status='accepted', accepted=True,
                        assembled_content_sha256=hashlib.sha256(assembled.encode('utf-8')).hexdigest())
        save(manifest_path, manifest)
        return {'content': assembled, 'recovery': deepcopy(manifest)}
    except Exception as error:
        manifest.update(status='cancelled' if isinstance(error, InterruptedError) else 'failed',
                        accepted=False, error={'type': type(error).__name__, 'message': str(error)})
        try:
            save(manifest_path, manifest)
        except OSError:
            pass  # Preserve the original failure if the audit disk also fails.
        raise
