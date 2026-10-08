"""Complete clipped OCR inputs while keeping every layout and image field unchanged."""
from __future__ import annotations

import math
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
import json
import os
import sys
import time

WHITE_PADDING = 2
import cv2
import numpy as np


def overlap(a: list[int], b: list[int]) -> int:
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def single_threshold(pixels: np.ndarray, box: list[int], other_boxes: list[list[int]],
                     scale: float, threshold: int) -> dict:
    height, width = pixels.shape[:2]
    x1, y1, x2, y2 = map(int, box)
    halo = min(192, max(8, math.ceil(scale * 0.9)))
    search = [max(0, x1-halo), max(0, y1-halo), min(width, x2+halo), min(height, y2+halo)]
    sx1, sy1, sx2, sy2 = search
    gray = cv2.cvtColor(pixels[sy1:sy2, sx1:sx2], cv2.COLOR_RGB2GRAY)
    dark = (gray < threshold).astype(np.uint8)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(dark, connectivity=8)
    local = [x1-sx1, y1-sy1, x2-sx1, y2-sy1]
    lx1, ly1, lx2, ly2 = local
    inside_labels, inside_counts = np.unique(labels[ly1:ly2, lx1:lx2], return_counts=True)
    minimum = max(3, round(scale * scale * 0.0003))
    selected = []
    crossing = []
    for label, n_inside in zip(inside_labels, inside_counts):
        if label == 0 or n_inside < minimum:
            continue
        cx, cy, cw, ch, area = map(int, stats[label])
        selected.append(int(label))
        if cx < lx1 or cy < ly1 or cx+cw > lx2 or cy+ch > ly2:
            crossing.append({'id': int(label), 'box': [sx1+cx, sy1+cy, sx1+cx+cw, sy1+cy+ch],
                             'area': area, 'inside_pixels': int(n_inside)})
    result = {'threshold': threshold, 'search_box': search, 'scale': scale,
              'crossing_components': crossing, 'suspicious': bool(crossing),
              'candidate_box': None, 'reason': 'no_crossing_ink'}
    if not crossing:
        return result
    union = list(box)
    for component in crossing:
        bx1, by1, bx2, by2 = component['box']
        bw, bh = bx2-bx1, by2-by1
        if bx1 <= sx1 or by1 <= sy1 or bx2 >= sx2 or by2 >= sy2:
            result['reason'] = 'crossing_component_hits_search_boundary'
            return result
        if max(bw, bh) > scale * 2.5 or max(bw, bh) / max(1, min(bw, bh)) > 12:
            result['reason'] = 'crossing_component_may_be_border_or_artwork'
            return result
        union = [min(union[0], bx1), min(union[1], by1), max(union[2], bx2), max(union[3], by2)]
    padding = max(2, round(scale * 0.05))
    candidate = [max(0, union[0]-padding), max(0, union[1]-padding),
                 min(width, union[2]+padding), min(height, union[3]+padding)]
    if not (sx1 < candidate[0] < candidate[2] < sx2 and sy1 < candidate[1] < candidate[3] < sy2):
        result['reason'] = 'insufficient_verified_margin'
        return result
    if any(overlap(candidate, other) > overlap(box, other) for other in other_boxes):
        result['reason'] = 'new_overlap_with_other_ocr_block'
        return result
    cx1, cy1, cx2, cy2 = candidate
    candidate_labels = labels[cy1-sy1:cy2-sy1, cx1-sx1:cx2-sx1]
    new_area = np.ones(candidate_labels.shape, dtype=bool)
    new_area[y1-cy1:y2-cy1, x1-cx1:x2-cx1] = False
    extra = candidate_labels[new_area]
    extra_ink = extra[extra != 0]
    unselected_count = int(np.count_nonzero(~np.isin(extra_ink, selected)))
    if unselected_count > max(6, round(scale*scale*0.001)):
        result.update(reason='new_ink_not_connected_to_original_text', unselected_ink_pixels=unselected_count)
        return result
    candidate_dark = dark[cy1-sy1:cy2-sy1, cx1-sx1:cx2-sx1]
    edge = max(1, padding//2)
    if np.any(candidate_dark[:edge]) or np.any(candidate_dark[-edge:]) or np.any(candidate_dark[:, :edge]) or np.any(candidate_dark[:, -edge:]):
        result['reason'] = 'candidate_has_ink_at_outer_margin'
        return result
    white_fraction = float(np.mean(gray[cy1-sy1:cy2-sy1, cx1-sx1:cx2-sx1] >= 220))
    if white_fraction < 0.5:
        result.update(reason='not_a_clear_light_background', white_fraction=white_fraction)
        return result
    result.update(candidate_box=candidate, completed_ink_box=union, reason='bounded_connected_ink_completed',
                  white_fraction=white_fraction, unselected_ink_pixels=unselected_count)
    return result


def propose(pixels: np.ndarray, block: dict, other_boxes: list[list[int]]) -> dict:
    box = list(map(int, block['xyxy']))
    height, width = pixels.shape[:2]
    if not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height):
        return {'suspicious': True, 'candidate_box': None, 'reason': 'invalid_source_box'}
    if abs(float(block.get('angle', 0))) > 1:
        return {'suspicious': False, 'candidate_box': None, 'reason': 'angled_text_not_supported'}
    lines = np.asarray(block.get('lines', []), dtype=float)
    if lines.size:
        widths = np.linalg.norm(lines[:, 1]-lines[:, 0], axis=1)
        heights = np.linalg.norm(lines[:, 2]-lines[:, 1], axis=1)
        scale = float(np.median(np.minimum(widths, heights)))
    else:
        scale = min(box[2]-box[0], box[3]-box[1])
    if not 12 <= scale <= 256:
        return {'suspicious': False, 'candidate_box': None, 'reason': 'unsupported_character_scale'}
    attempts = [single_threshold(pixels, box, other_boxes, scale, threshold) for threshold in (110, 170)]
    result = {'suspicious': any(attempt['suspicious'] for attempt in attempts),
              'candidate_box': None, 'threshold_evidence': attempts}
    if not result['suspicious']:
        result['reason'] = 'no_crossing_ink'
        return result
    if any(attempt['candidate_box'] is None for attempt in attempts):
        result['reason'] = 'candidate_rejected_by_geometry'
        return result
    boxes = [attempt['candidate_box'] for attempt in attempts]
    if max(abs(a-b) for a, b in zip(*boxes)) > max(2, scale*0.04):
        result['reason'] = 'thresholds_disagree_on_extent'
        return result
    candidate = [min(boxes[0][0], boxes[1][0]), min(boxes[0][1], boxes[1][1]),
                 max(boxes[0][2], boxes[1][2]), max(boxes[0][3], boxes[1][3])]
    # The stronger threshold's candidate contains at least as much dark foreground;
    # reject instead of making a new, unverified union with different margins.
    if candidate not in boxes:
        result['reason'] = 'threshold_union_not_individually_verified'
        return result
    result.update(candidate_box=candidate, reason='stable_bounded_completion')
    return result


def source_text(block: dict) -> str:
    """Read serialized OCR text without changing its representation.

    >>> source_text({'text': ['カワ', 'イイ']})
    'カワイイ'
    """
    text = block.get('text', [])
    return text if isinstance(text, str) else ''.join(text)


def review_page(pixels: np.ndarray, blocks: list[dict],
                recognize: Callable[[np.ndarray], str]) -> tuple[list[dict], list[dict]]:
    """Only adopt text when complete crops with zero and two added pixels agree."""
    updated = deepcopy(blocks)
    records = []
    for index, block in enumerate(blocks):
        proposal = propose(pixels, block, [other['xyxy'] for number, other in enumerate(blocks) if number != index])
        before = source_text(block)
        row = {'id': index+1, 'original_text': before, 'text': before,
               'original_box': list(block['xyxy']), 'proposal': proposal,
               'padding_per_side_px': WHITE_PADDING, 'status': 'unchanged',
               'reason': proposal['reason']}
        if proposal['candidate_box'] is not None:
            x1, y1, x2, y2 = proposal['candidate_box']
            completed = pixels[y1:y2, x1:x2]
            padded = np.pad(completed, ((WHITE_PADDING, WHITE_PADDING), (WHITE_PADDING, WHITE_PADDING), (0, 0)),
                            constant_values=255)
            started = time.monotonic()
            padded_text = recognize(padded)
            unpadded_text = recognize(completed)
            row.update(padded_text=padded_text, unpadded_text=unpadded_text,
                       inference_seconds=time.monotonic()-started)
            if padded_text and padded_text == unpadded_text:
                if padded_text != before:
                    updated[index]['text'] = [padded_text] if isinstance(block.get('text'), list) else padded_text
                    row.update(status='corrected', reason='complete_crop_and_2px_agree', text=padded_text)
                else:
                    row['reason'] = 'complete_crop_matches_original_text'
            else:
                row.update(status='needs_review', reason='complete_crop_readings_disagree')
        elif proposal['suspicious']:
            row['status'] = 'needs_review'
        records.append(row)
    restored = deepcopy(updated)
    for original, checked in zip(blocks, restored):
        if 'text' in original:
            checked['text'] = deepcopy(original['text'])
        else:
            checked.pop('text', None)
    if restored != blocks:
        raise ValueError('OCR review changed a field other than source text')
    return updated, records


def save(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    temporary.replace(path)


def review_project(project: Path, audit: Path,
                   recognize: Callable[[np.ndarray], str] | None = None) -> dict:
    """Review a new work copy; preserve original OCR, order and every layout field."""
    from PIL import Image

    raw_path, order_path = audit / 'machine-ocr-project.json', audit / 'reading-order.json'
    raw = json.loads(raw_path.read_text('utf-8'))
    order = json.loads(order_path.read_text('utf-8'))
    if set(raw['pages']) != set(order):
        raise ValueError('OCR and reading-order pages differ')
    old_project = json.loads((project / 'imgtrans_project.json').read_text('utf-8'))
    if old_project != raw:
        raise ValueError('Working project differs from its captured OCR before review')
    original_path, original_order_path = audit / 'machine-ocr-original.json', audit / 'reading-order-original.json'
    if any(path.exists() for path in (original_path, original_order_path, audit / 'ocr-crop-review.json')):
        raise FileExistsError('OCR review already exists; use a fresh preparation attempt')
    updated, updated_order, records = deepcopy(raw), deepcopy(order), {}
    model = None

    def native_recognize(sample: np.ndarray) -> str:
        nonlocal model
        if model is None:
            import torch
            threads = max(1, int(os.environ.get('MANGA_CPU_THREADS', '4')))
            torch.set_num_threads(threads)
            torch.set_num_interop_threads(1)
            torch.manual_seed(20261001)
            app = Path(__file__).resolve().parents[1] / 'BallonsTranslator'
            sys.path.insert(0, str(app))
            from ballontranslator.modules.ocr.ocr_manga import MangaOcr
            model = MangaOcr(str(app / 'data/models/manga-ocr-base'), device='cpu')
        return model(sample)

    recognize = recognize or native_recognize
    for name, blocks in raw['pages'].items():
        if Path(name).name != name:
            raise ValueError('Page path must be a filename')
        rows = updated_order[name]['rows']
        if sorted(row['id'] for row in rows) != list(range(1, len(blocks)+1)):
            raise ValueError('Reading order omitted or duplicated an OCR block')
        for row in rows:
            if row['japanese'] != source_text(blocks[row['id']-1]):
                raise ValueError('Reading order differs from the original OCR text')
        if not blocks:
            records[name] = []
            continue
        with Image.open(project / name) as image:
            pixels = np.asarray(image.convert('RGB'))
        reviewed, records[name] = review_page(pixels, blocks, recognize)
        updated['pages'][name] = reviewed
        # IDs, sequence, geometry, speaker matching and ordering method stay exact.
        for row in rows:
            row['japanese'] = source_text(reviewed[row['id']-1])
    flat = [row for page in records.values() for row in page]
    result = {'policy': 'bounded-source-crop-review-v1', 'padding_per_side_px': WHITE_PADDING,
              'confirmation_padding_per_side_px': 0, 'pages': records,
              'corrected': sum(row['status'] == 'corrected' for row in flat),
              'needs_review': sum(row['status'] == 'needs_review' for row in flat),
              'text_only': True, 'success': True}
    # A partial write cannot become reusable: prepare seals artifacts only after success.
    save(original_path, raw)
    save(original_order_path, order)
    save(raw_path, updated)
    save(order_path, updated_order)
    save(project / 'imgtrans_project.json', updated)
    save(audit / 'ocr-crop-review.json', result)
    return result


def main() -> None:
    import argparse
    import socket

    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--audit', type=Path, required=True)
    args = parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                      QT_API='pyqt6', PYTHONDONTWRITEBYTECODE='1')

    def deny_network(*unused):
        raise RuntimeError('Network is disabled during local OCR review')

    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    cv2.setNumThreads(max(1, int(os.environ.get('MANGA_CPU_THREADS', '4'))))
    result = review_project(args.project.resolve(), args.audit.resolve())
    print(json.dumps({'event': 'ocr_crop_review_finished', 'padding_per_side_px': WHITE_PADDING,
                      'corrected': result['corrected'], 'needs_review': result['needs_review']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
