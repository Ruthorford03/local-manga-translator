"""Fresh-batch source direction and transactional fitting through native Qt items.

The batch writer binds source evidence to a render input. Only that unchanged,
plain-text input may propose direction changes; passive/manual projects do not.
Qt imports stay local so the batch scheduler can write the request without Qt.
"""
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

from source_direction import propose

if TYPE_CHECKING:
    import numpy as np
    from ballontranslator.ui.text_engine.item import TextBlkItem
    from ballontranslator.utils.textblock import TextBlock
    from bubble_guard import Ink, Region


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _read(path: Path) -> dict:
    return json.loads(path.read_text('utf-8-sig'))


def write_render_request(project: Path, render_doc: dict, order: dict) -> None:
    """Bind this batch's exact render inputs, image bytes and Magi rows."""
    pages = {}
    for name, blocks in render_doc['pages'].items():
        if Path(name).name != name:
            raise ValueError('Render input must use image basenames')
        pages[name] = {'blocks_sha256': _digest(blocks),
                       'source_sha256': hashlib.sha256((project / name).read_bytes()).hexdigest(),
                       'rows': deepcopy(order[name]['rows'])}
    path = project / 'source-layout-input.json'
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps({'version': 1, 'pages': pages}, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    temporary.replace(path)


@dataclass
class PagePlan:
    blocks: list['TextBlock']
    decisions: list[dict]
    groups: list[tuple[list[int], 'Region']]


class _RollbackError(RuntimeError):
    """A failed restoration must stop rendering, never become needs_review."""


def prepare_layout(blocks: list['TextBlock'], image: 'np.ndarray', project: Path,
                   page: str, writing_override: bool = False) -> PagePlan | None:
    """Decide from untouched source evidence before any scene item is created.

    Persisted rich text and all hashes are checked, because native render-only
    startup clears live rich_text even for a previously edited project.
    """
    if writing_override or image is None or image.ndim != 3 or Path(page).name != page:
        return None
    try:
        manifest = _read(project / 'source-layout-input.json')
        entry = manifest['pages'][page]
        raw = _read(project / 'imgtrans_project.json')['pages'][page]
        if (manifest['version'] != 1 or _digest(raw) != entry['blocks_sha256']
                or hashlib.sha256((project / page).read_bytes()).hexdigest() != entry['source_sha256']
                or len(raw) != len(blocks) or any(b.get('rich_text') for b in raw)):
            return None
        rows = entry['rows']
        if not isinstance(rows, list) or len(rows) != len(raw):
            return None
        ids = [row['id'] for row in rows]
        if any(type(i) is not int for i in ids) or sorted(ids) != list(range(1, len(raw) + 1)):
            return None
        for row in rows:
            block = raw[row['id'] - 1]
            # The group is evidence only if its original source identity still
            # agrees. Unknown/missing Magi matches cannot invent a group.
            if row['bbox'] != block['xyxy'] or row['japanese'] != blocks[row['id'] - 1].get_text():
                return None
        for saved, live in zip(raw, blocks):
            current = live.to_dict()
            if any(current.get(key) != saved.get(key) for key in
                   ('text', 'xyxy', 'lines', 'src_is_vertical', 'angle', 'translation')):
                return None
        decisions = propose(raw, rows)
        if len(decisions) != len(raw) or any(d['reason'] == 'invalid_reading_order' for d in decisions):
            return None
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None

    from bubble_guard import bind_source_regions, source_polygons
    import cv2
    import numpy as np

    saved_blocks = deepcopy(blocks)
    regions, _ = bind_source_regions(saved_blocks, image)
    grouped = defaultdict(list)
    for row in rows:
        if row.get('magi_text_id') is not None:
            grouped[row['magi_text_id']].append(row['id'] - 1)
    groups = []
    for members in grouped.values():
        members.sort()
        if not any(decisions[i]['change'] for i in members):
            continue
        directions = {decisions[i]['direction'] for i in members}
        eligible = 2 <= len(members) <= 4 and None not in directions and len(directions) == 1
        # Confident peers must agree with their actual source direction. This
        # repair never reinterprets multi-character or mixed writing groups.
        eligible &= all(decisions[i]['change'] or
                        decisions[i]['direction'] == raw[i]['src_is_vertical'] for i in members)
        # Use the same match criterion as ordered_ocr, then independently
        # require almost complete source-polygon containment in one bubble.
        eligible &= all((isinstance(row.get('coverage'), (int, float)) and 0.6 <= row['coverage'] <= 1)
                        or (isinstance(row.get('iou'), (int, float)) and 0.3 <= row['iou'] <= 1)
                        for row in rows if row['id'] - 1 in members)
        region = None
        if eligible:
            for candidate in (regions[i] for i in members):
                if candidate is None or candidate.kind not in ('closed_white', 'page_edge_bubble'):
                    continue
                coverage = []
                for i in members:
                    polygons = source_polygons(saved_blocks[i], image.shape[:2])
                    source = np.zeros(image.shape[:2], np.uint8)
                    cv2.fillPoly(source, polygons, 1)
                    h, w = candidate.pixels.shape
                    local = source[candidate.y:candidate.y + h, candidate.x:candidate.x + w]
                    coverage.append(np.count_nonzero(local & candidate.pixels) / max(1, np.count_nonzero(source)))
                if min(coverage) >= 0.98:
                    region = candidate
                    break
        for i in members:
            decisions[i]['group_fit'] = 'pending' if region is not None else 'needs_review'
            if region is None:
                decisions[i]['fit_reason'] = 'no_unambiguous_shared_bubble_group'
        if region is not None:
            groups.append((members, region))
    return PagePlan(saved_blocks, decisions, groups)


def intersection(a: 'Ink', b: 'Ink', clearance: int = 0) -> int:
    """Count overlapping actual glyph/effect pixels, optionally with clearance."""
    import cv2
    import numpy as np
    ax, ay, ap = a.x, a.y, a.pixels
    if clearance:
        ap = cv2.dilate(np.pad(ap.astype(np.uint8), clearance),
                        np.ones((clearance * 2 + 1,) * 2, np.uint8)).astype(bool)
        ax, ay = ax - clearance, ay - clearance
    left, top = max(ax, b.x), max(ay, b.y)
    right = min(ax + ap.shape[1], b.x + b.pixels.shape[1])
    bottom = min(ay + ap.shape[0], b.y + b.pixels.shape[0])
    if right <= left or bottom <= top:
        return 0
    return int(np.count_nonzero(ap[top - ay:bottom - ay, left - ax:right - ax]
                               & b.pixels[top - b.y:bottom - b.y, left - b.x:right - b.x]))


def _joint_fit(candidates: list['TextBlkItem'], region: 'Region', other_inks: list['Ink'],
               centers: list[float]) -> bool:
    """Search bounded displacements across columns/rows, preserving source order."""
    import cv2
    import numpy as np
    from bubble_guard import Ink, Region, outside_pixels, raster_ink

    safe = Region(region.label, region.x, region.y,
                  cv2.erode(region.pixels.astype(np.uint8), np.ones((9, 9), np.uint8),
                            borderType=cv2.BORDER_CONSTANT, borderValue=0).astype(bool))
    vertical = candidates[0].get_fontformat().vertical
    inks = [raster_ink(c) for c in candidates]
    limit = min(128, math.ceil(max(c.get_fontformat().font_size for c in candidates) * 2))
    states = [(0, [], [])]
    for index, ink in enumerate(inks):
        positions = []
        for delta in sorted(range(-limit, limit + 1), key=lambda d: (d * d, d)):
            moved = Ink(ink.x + (delta if vertical else 0), ink.y + (0 if vertical else delta), ink.pixels)
            if (ink.pixels.any() and outside_pixels(moved, safe) == 0
                    and not any(intersection(moved, other, 4) for other in other_inks)):
                positions.append((delta, moved))
        next_states = []
        for cost, deltas, placed in states:
            for delta, moved in positions:
                if any((centers[index] + delta - centers[j] - deltas[j]) *
                       (centers[index] - centers[j]) <= 0
                       or intersection(moved, prior, 4) for j, prior in enumerate(placed)):
                    continue
                next_states.append((cost + delta * delta, deltas + [delta], placed + [moved]))
        # A bounded beam handles up to four members. Failure is review, never
        # permission to shrink, reorder or discard punctuation.
        states = sorted(next_states, key=lambda state: (state[0], state[1]))[:256]
        if not states:
            return False
    for item, delta in zip(candidates, states[0][1]):
        item.moveBy(delta if vertical else 0, 0 if vertical else delta)
    return True


def _snapshot(item: 'TextBlkItem') -> tuple:
    from bubble_guard import capture_block, raster_ink
    formats = []
    block = item.document().begin()
    while block.isValid():
        fragments, iterator = [], block.begin()
        while not iterator.atEnd():
            fragment = iterator.fragment()
            if fragment.isValid():
                fragments.append((fragment.position(), fragment.length(), fragment.charFormat()))
            iterator += 1
        formats.append((block.position(), block.blockFormat(), block.charFormat(), fragments))
        block = block.next()
    # absBoundingRect()'s list form rounds coordinates. Preserve the QRectF
    # too: half-pixel native placement must survive commit and rollback.
    return (capture_block(item), formats, raster_ink(item), item.absBoundingRect(qrect=True),
            deepcopy(item.blk), item.document().defaultFont())


def _same_ink(a: 'Ink', b: 'Ink') -> bool:
    import numpy as np
    return a.x == b.x and a.y == b.y and np.array_equal(a.pixels, b.pixels)


def _restore(item: 'TextBlkItem', snapshot: tuple) -> None:
    from qtpy.QtGui import QTextCursor
    from bubble_guard import raster_ink
    before, formats, ink, rect, model, default_font = snapshot
    item.initTextBlock(deepcopy(before))
    item.load_rich_text_html(before.rich_text)
    item.document().setDefaultFont(default_font)
    cursor = QTextCursor(item.document())
    cursor.beginEditBlock()
    try:
        for position, block_format, char_format, fragments in formats:
            cursor.setPosition(position)
            cursor.setBlockFormat(block_format)
            cursor.setBlockCharFormat(char_format)
            for start, length, original_format in fragments:
                cursor.setPosition(start)
                cursor.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
                cursor.setCharFormat(original_format)
    finally:
        cursor.endEditBlock()
    item.setRect(rect, update_blk_rect=False)
    # setRect/initTextBlock can synchronize display geometry into source xyxy.
    # Restore the original model as well as its exact visible document state.
    item.blk = deepcopy(model)
    item.fontformat = item.blk.fontformat
    if (item.toPlainText() != before.translation or not _same_ink(raster_ink(item), ink)
            or item.get_fontformat().to_serializable_dict() != before.fontformat.to_serializable_dict()):
        raise RuntimeError('Source direction rollback could not restore native text exactly')


def apply_layout(plan: PagePlan | None, items: list['TextBlkItem'], records: list[dict]) -> bool:
    """Commit only verified group candidates; otherwise keep native baseline."""
    if plan is None:
        return False
    from ballontranslator.ui.text_engine.item import TextBlkItem
    from bubble_guard import capture_block, raster_ink, outside_pixels

    changed = False
    for record, decision in zip(records, plan.decisions):
        record['source_direction_proposal'] = deepcopy(decision)
        if decision.get('group_fit') == 'needs_review':
            record.update(status='needs_review', reason=decision['fit_reason'])
    for members, region in plan.groups:
        candidates = []
        accepted = False
        reason = 'no_verified_group_fit'
        try:
            other_inks = [raster_ink(item) for i, item in enumerate(items) if i not in members]
            for i in members:
                block = deepcopy(plan.blocks[i])
                block.src_is_vertical = plan.decisions[i]['direction']
                block.fontformat.vertical = block.src_is_vertical
                if not block.font_family:
                    block.font_family = items[i].get_fontformat().font_family
                candidate = TextBlkItem(block, items[i].idx)
                candidates.append(candidate)
                if abs(candidate.rotation()) > 0.1 or not candidate.geometry_controller.is_neutral():
                    raise ValueError('Complex text transform requires review')
                x1, y1, x2, y2 = block.xyxy
                font = candidate.get_fontformat()
                n = len(candidate.toPlainText().replace('\n', '').replace('\r', ''))
                if font.vertical:
                    width = math.ceil(max(x2 - x1, font.font_size * 1.25))
                    height = min(math.ceil(max(y2 - y1, font.font_size * font.letter_spacing * (n + 0.5))),
                                 region.y + region.pixels.shape[0] - 4 - y1)
                    rect = [(x1 + x2 - width) / 2, y1, width, height]
                else:
                    width = min(math.ceil(max(x2 - x1, font.font_size * font.letter_spacing * (n + 0.5))),
                                region.x + region.pixels.shape[1] - 4 - x1)
                    height = math.ceil(max(y2 - y1, font.font_size * 1.25))
                    rect = [x1, (y1 + y2 - height) / 2, width, height]
                if min(rect[2:]) <= 0:
                    raise ValueError('No source-aligned group extent')
                candidate.setRect(rect)
                if candidate.toPlainText() != plan.blocks[i].translation:
                    raise ValueError('Group candidate changed translation characters')
            axis = 0 if candidates[0].get_fontformat().vertical else 1
            centers = [(plan.blocks[i].xyxy[axis] + plan.blocks[i].xyxy[axis + 2]) / 2 for i in members]
            if not _joint_fit(candidates, region, other_inks, centers):
                continue
            snapshots = [_snapshot(items[i]) for i in members]
            committed = []
            try:
                for i, candidate in zip(members, candidates):
                    committed.append(i)
                    proposal = capture_block(candidate)
                    items[i].initTextBlock(proposal)
                    items[i].setRect(candidate.absBoundingRect(qrect=True))
                    if (items[i].toPlainText() != candidate.toPlainText()
                            or items[i].get_fontformat().to_serializable_dict() != candidate.get_fontformat().to_serializable_dict()
                            or not _same_ink(raster_ink(items[i]), raster_ink(candidate))):
                        raise ValueError('Live group differs from verified candidate')
                accepted = True
                changed = True
            finally:
                if not accepted:
                    restore_error = None
                    for i, snapshot in zip(members, snapshots):
                        if i in committed:
                            try:
                                _restore(items[i], snapshot)
                            except Exception as error:
                                restore_error = error
                    if restore_error is not None:
                        raise _RollbackError('Source direction rollback failed; abort render') from restore_error
            for i, snapshot in zip(members, snapshots):
                before = snapshot[0]
                records[i].pop('reason', None)
                records[i].update(status='adjusted', action='source_direction_group_fit',
                                  before_rect=list(before._bounding_rect), after_rect=list(items[i].absBoundingRect()),
                                  before_font_size=before.fontformat.font_size,
                                  after_font_size=items[i].get_fontformat().font_size,
                                  outside_pixels_after=outside_pixels(raster_ink(items[i]), region))
        except _RollbackError:
            raise
        except (ValueError, RuntimeError) as error:
            reason = str(error)
        finally:
            for i in members:
                records[i]['source_direction_proposal']['group_fit'] = 'accepted' if accepted else 'needs_review'
                if not accepted:
                    records[i].update(status='needs_review', reason=reason)
            for candidate in candidates:
                candidate.deleteLater()

    # Text-on-text collisions are a separate check: both pieces can be inside
    # the same bubble while overlapping each other.
    inks = []
    for record, item in zip(records, items):
        try:
            inks.append(raster_ink(item))
            record['text_collision_ids'] = []
        except (ValueError, RuntimeError):
            inks.append(None)
            record.update(status='unverified', reason='ink_measurement_unavailable')
    for i, ink in enumerate(inks):
        for j in range(i + 1, len(inks)):
            if ink is not None and inks[j] is not None and intersection(ink, inks[j]):
                for a, b in ((i, j), (j, i)):
                    records[a]['text_collision_ids'].append(b + 1)
                    records[a].update(status='needs_review', reason='text_glyph_collision')
    return changed
