"""Repair only rendered text that crosses a confidently identified white bubble.

Geometry is measured through Ballons' real Qt text item. Source OCR line polygons
bind each item to its enclosing region, including a bubble clipped at one page
edge or a margin caption beside a verified panel line. Uncertain regions remain
unchanged. A compact OCR block with explicit source-direction evidence can be
restored to vertical writing. Translation characters and their order are kept.
"""
from copy import deepcopy
from dataclasses import dataclass
from itertools import combinations
import math
import re

import cv2
import numpy as np
from qtpy.QtCore import QPointF, Qt
from qtpy.QtGui import QImage, QPainter, QTextCursor
from qtpy.QtWidgets import QStyleOptionGraphicsItem

from ballontranslator.ui.text_engine.item import TextBlkItem
from ballontranslator.utils.textblock import TextBlock


@dataclass
class Ink:
    x: int
    y: int
    pixels: np.ndarray


@dataclass
class Region:
    label: int
    x: int
    y: int
    pixels: np.ndarray
    kind: str = 'closed_white'
    frame_boundary: tuple[str, int, int, int] | None = None


def raster_ink(item: TextBlkItem) -> Ink:
    """Measure visible glyph/effect pixels in page coordinates, excluding guides."""
    bounds = item.sceneBoundingRect()
    x, y = math.floor(bounds.left()) - 4, math.floor(bounds.top()) - 4
    width, height = math.ceil(bounds.right()) - x + 4, math.ceil(bounds.bottom()) - y + 4
    if width < 1 or height < 1 or width * height > 16_000_000:
        raise ValueError('Unsupported text render extent')
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
    painter.translate(-x, -y)
    painter.setWorldTransform(item.sceneTransform(), True)
    painter.setOpacity(item.effectiveOpacity())
    try:
        # The geometry/effects owner paints exactly the text layer, without the
        # editor's selection rectangle or order badges.
        item.geometry_controller.paint_item(painter, QStyleOptionGraphicsItem(), None,
                                             super(TextBlkItem, item).paint)
    finally:
        painter.end()
    raw = image.constBits()
    raw.setsize(image.sizeInBytes())
    alpha = np.frombuffer(raw, np.uint8).reshape(height, width, 4)[:, :, 3]
    yy, xx = np.where(alpha > 32)
    if not len(xx):
        return Ink(x, y, np.zeros((0, 0), bool))
    x1, y1, x2, y2 = int(xx.min()), int(yy.min()), int(xx.max()) + 1, int(yy.max()) + 1
    return Ink(x + x1, y + y1, (alpha[y1:y2, x1:x2] > 32).copy())


def source_polygons(block: TextBlock, shape: tuple[int, int]) -> list[np.ndarray]:
    polygons = []
    for line in block.lines:
        try:
            points = np.array(line, dtype=np.float64, copy=True)
        except (TypeError, ValueError):
            continue
        if points.ndim == 2 and points.shape[0] >= 3 and points.shape[1] == 2 and np.isfinite(points).all():
            points[:, 0] = np.clip(points[:, 0], 0, shape[1] - 1)
            points[:, 1] = np.clip(points[:, 1], 0, shape[0] - 1)
            points = np.rint(points).astype(np.int32)
            if cv2.contourArea(points) > 4:
                polygons.append(points)
    return polygons


def margin_region(image: np.ndarray, polygons: list[np.ndarray], bounds) -> Region | None:
    """Find a narrow white page margin separated from art by a long panel line."""
    height, width = image.shape[:2]
    x, y, w, h = bounds
    if h < max(40, w * 2) or w > width * 0.08:
        return None
    if x + w <= width * 0.12:
        side, start, end = 'left', x + w, min(round(width * 0.18), x + w + max(24, w * 2))
    elif x >= width * 0.88:
        side, start, end = 'right', max(round(width * 0.82), x - max(24, w * 2)), x
    else:
        return None
    if end - start < 2:
        return None
    dark = image[y:y + h, start:end, :3].max(axis=2) <= 96
    strong = dark.mean(axis=0) >= 0.9
    runs = np.flatnonzero(strong[:-1] & strong[1:])
    if not len(runs):
        return None
    line = start + int(runs[0] if side == 'left' else runs[-1] + 1)
    x0, x1 = (0, line) if side == 'left' else (line + 1, width)
    white = image[y:y + h, x0:x1, :3].min(axis=2) >= 235
    evidence = np.ones(white.shape, np.uint8)
    cv2.fillPoly(evidence, [poly - (x0, y) for poly in polygons], 0)
    # Ignore only the original OCR characters, not arbitrary artwork or a whole
    # globally connected white component. The rest must be a clean page margin.
    if not evidence.any() or white[evidence > 0].mean() < 0.95:
        return None
    pad = max(w, math.ceil(h * 0.15))
    y0, y1 = max(0, y - pad), min(height, y + h + pad)
    safe = (image[y0:y1, x0:x1, :3].min(axis=2) >= 235).astype(np.uint8)
    cv2.fillPoly(safe, [poly - (x0, y0) for poly in polygons], 1)
    return Region(-1 if side == 'left' else -2, x0, y0, safe.astype(bool),
                  'page_margin', (side, line, y, y + h))


def crosses_margin_frame(ink: Ink, region: Region) -> bool:
    """Artificial ends of the local margin crop must not trigger a repair."""
    side, line, top, bottom = region.frame_boundary
    yy, xx = np.where(ink.pixels)
    xx, yy = xx + ink.x, yy + ink.y
    past_line = xx >= line if side == 'left' else xx <= line
    return bool(np.any(past_line & (yy >= top) & (yy < bottom)))


def bind_regions(items: list[TextBlkItem], image: np.ndarray) -> tuple[list[Region | None], list[tuple[float, float] | None]]:
    return bind_source_regions([item.blk for item in items], image)


def bind_source_regions(blocks: list[TextBlock], image: np.ndarray) -> tuple[list[Region | None], list[tuple[float, float] | None]]:
    """Bind source OCR polygons to a supported white bubble or page margin."""
    white = (image[:, :, :3].min(axis=2) >= 235).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(white, connectivity=4)
    cache, regions, centers = {}, [], []
    height, width = white.shape
    for block in blocks:
        polygons = source_polygons(block, white.shape)
        if not polygons:
            regions.append(None)
            centers.append(None)
            continue
        points = np.concatenate(polygons)
        x, y, w, h = cv2.boundingRect(points)
        centers.append((x + w / 2, y + h / 2))
        anchor = np.zeros((h, w), np.uint8)
        cv2.fillPoly(anchor, [poly - (x, y) for poly in polygons], 1)
        votes = np.bincount(labels[y:y + h, x:x + w][anchor > 0], minlength=count)
        votes[0] = 0
        label = int(votes.argmax())
        sx, sy, sw, sh, area = map(int, stats[label])
        edges = sum((sx == 0, sy == 0, sx + sw >= width, sy + sh >= height))
        # A component clipped on one image edge remains bounded on its other
        # three sides. Global background/two-edge openings remain ambiguous.
        if (not label or votes[label] < np.count_nonzero(anchor) * 0.45 or area < 200
                or edges > 1
                or area > max(20_000, w * h * 15)):
            regions.append(margin_region(image, polygons, (x, y, w, h)) if edges > 1 else None)
            continue
        if label not in cache:
            region = (labels[sy:sy + sh, sx:sx + sw] == label).astype(np.uint8)
            contours, _ = cv2.findContours(region, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            filled = np.zeros_like(region)
            cv2.drawContours(filled, contours, -1, 1, cv2.FILLED)
            cache[label] = (Region(label, sx, sy, filled.astype(bool),
                                   'page_edge_bubble' if edges else 'closed_white')
                            if area >= filled.sum() * 0.7 else None)
        region = cache[label]
        if region is not None:
            for poly in polygons:
                local = np.zeros(region.pixels.shape, np.uint8)
                cv2.fillPoly(local, [poly - (region.x, region.y)], 1)
                # Clipping a source polygon to this region would conceal a wrong
                # match, so compare against its area in original coordinates too.
                expected = cv2.contourArea(poly)
                if np.count_nonzero((local > 0) & region.pixels) < expected * 0.8:
                    region = None
                    break
        regions.append(region)
    return regions, centers


def outside_pixels(ink: Ink, region: Region) -> int:
    yy, xx = np.where(ink.pixels)
    xx, yy = xx + ink.x - region.x, yy + ink.y - region.y
    valid = (xx >= 0) & (yy >= 0) & (xx < region.pixels.shape[1]) & (yy < region.pixels.shape[0])
    inside = np.zeros(len(xx), bool)
    inside[valid] = region.pixels[yy[valid], xx[valid]]
    return int(np.count_nonzero(~inside))


def placement(ink: Ink, safe: Region, desired: tuple[int, int]) -> tuple[int, int] | None:
    """Find the nearest placement whose actual ink is fully in the safe region."""
    h, w = ink.pixels.shape
    if not h or not w or h > safe.pixels.shape[0] or w > safe.pixels.shape[1]:
        return None
    collisions = cv2.matchTemplate((~safe.pixels).astype(np.float32), ink.pixels.astype(np.float32), cv2.TM_CCORR)
    yy, xx = np.where(collisions < 0.5)
    if not len(xx):
        return None
    distance = (xx + safe.x - desired[0]) ** 2 + (yy + safe.y - desired[1]) ** 2
    for index in np.argsort(distance)[:16]:
        x, y = int(xx[index]), int(yy[index])
        if np.all(safe.pixels[y:y + h, x:x + w][ink.pixels]):
            return safe.x + x, safe.y + y
    return None


def largest_rectangle(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    """Return an inscribed rectangle, not the bounding box around the region.

    >>> largest_rectangle(np.ones((3, 4), dtype=bool))
    (0, 0, 4, 3)
    """
    heights = np.zeros(mask.shape[1] + 1, np.int32)
    best, best_area = None, 0
    for y, row in enumerate(mask):
        heights[:-1] = (heights[:-1] + 1) * row
        stack = []
        for x, height in enumerate(heights):
            start = x
            while stack and stack[-1][1] > height:
                start, previous = stack.pop()
                area = (x - start) * previous
                if area > best_area:
                    best, best_area = (start, y - previous + 1, x - start, previous), area
            if not stack or stack[-1][1] < height:
                stack.append((start, int(height)))
    return best


def capture_block(item: TextBlkItem):
    block = deepcopy(item.blk)
    block._bounding_rect = list(item.absBoundingRect())
    block.translation, block.rich_text = item.toPlainText(), item.toHtml()
    block.fontformat = item.get_fontformat().deepcopy()
    return block


def source_vertical_evidence(item: TextBlkItem, image: np.ndarray) -> dict | None:
    """Recognize a vertical ellipsis below a glyph in a compact merged OCR box.

    A short two-column Japanese phrase can have a wider-than-tall union box.
    Only explicit source-pixel evidence may override that box's direction.
    """
    if (item.get_fontformat().vertical or item.blk.src_is_vertical is not False
            or abs(item.rotation()) > 0.1 or not item.geometry_controller.is_neutral()):
        return None
    source = item.blk.get_text()
    if not re.search(r'[\u3041-\u30ff]', source) or not re.search(r'(?:[.．]{3,}|[…⋮︙]+)\s*$', source):
        return None
    polygons = source_polygons(item.blk, image.shape[:2])
    if len(polygons) != 1:
        return None
    x, y, w, h = cv2.boundingRect(polygons[0])
    if min(w, h) < 24 or not 0.75 <= w / h <= 2.5 or w * h > 1_000_000:
        return None
    mask = np.zeros((h, w), np.uint8)
    cv2.fillPoly(mask, [polygons[0] - (x, y)], 1)
    dark = ((image[y:y + h, x:x + w, :3].min(axis=2) < 130) & (mask > 0)).astype(np.uint8)
    _, _, stats, centers = cv2.connectedComponentsWithStats(dark, connectivity=8)
    pieces = [(box, center) for box, center in zip(stats[1:], centers[1:]) if box[4] >= 3]
    if not pieces or len(pieces) > 64:
        return None
    largest = max(box[4] for box, _ in pieces)
    dots = [(box, center) for box, center in pieces
            if 0.65 <= box[2] / box[3] <= 1.55 and box[4] / (box[2] * box[3]) >= 0.5
            and 0.01 * largest <= box[4] <= 0.12 * largest
            and max(box[2:4]) <= min(w, h) * 0.13]
    if not 3 <= len(dots) <= 12:
        return None
    for trio in combinations(dots, 3):
        trio = sorted(trio, key=lambda pair: pair[1][1])
        boxes, positions = zip(*trio)
        diameter = float(np.median([max(box[2:4]) for box in boxes]))
        xs, ys = np.array(positions).T
        gaps = np.diff(ys)
        if (np.ptp(xs) > diameter * 0.5 or min(gaps) < diameter * 1.4
                or max(gaps) > diameter * 4 or max(gaps) / min(gaps) > 1.25
                or max(box[4] for box in boxes) / min(box[4] for box in boxes) > 1.6):
            continue
        dot_area = max(box[4] for box in boxes)
        glyphs = [box for box, _ in pieces if box[4] >= dot_area * 6
                  and min(box[2:4]) >= diameter * 2]
        for glyph in glyphs:
            gx, gy, gw, gh, _ = map(int, glyph)
            below = boxes[0][1] - (gy + gh)
            if abs(xs.mean() - (gx + gw / 2)) > max(diameter, gw * 0.25) or not 0 <= below <= diameter * 2:
                continue
            # A second full-sized glyph to the right distinguishes two vertical
            # columns from isolated decorative dots or small ruby annotations.
            if not any(other[0] >= gx + gw + diameter * 2
                       and gy <= other[1] + other[3] / 2 <= boxes[-1][1] + boxes[-1][3]
                       for other in glyphs):
                continue
            return {'reason': 'source_vertical_ellipsis_and_two_columns',
                    'source_box': [x, y, w, h],
                    'dot_centers': [[round(float(cx + x), 2), round(float(cy + y), 2)] for cx, cy in positions]}
    return None


def safe_region(index: int, region: Region, regions: list[Region | None], centers, inks: list[Ink], padding: int) -> Region:
    safe = cv2.erode(region.pixels.astype(np.uint8), np.ones((padding * 2 + 1,) * 2, np.uint8),
                     borderType=cv2.BORDER_CONSTANT, borderValue=0).astype(bool)
    yy, xx = np.indices(safe.shape)
    cx, cy = centers[index]
    own_distance = (xx + region.x - cx) ** 2 + (yy + region.y - cy) ** 2
    for other, ink in enumerate(inks):
        if other == index:
            continue
        if regions[other] is not None and regions[other].label == region.label:
            ox, oy = centers[other]
            # Connected lobes retain their original text ownership/order.
            safe &= own_distance < (xx + region.x - ox) ** 2 + (yy + region.y - oy) ** 2
        h, w = ink.pixels.shape
        x1, y1 = max(0, ink.x - region.x - padding), max(0, ink.y - region.y - padding)
        x2 = min(safe.shape[1], ink.x - region.x + w + padding)
        y2 = min(safe.shape[0], ink.y - region.y + h + padding)
        if x2 > x1 and y2 > y1:
            safe[y1:y2, x1:x2] = False
    return Region(region.label, region.x, region.y, safe)


def repair(item: TextBlkItem, ink: Ink, safe: Region) -> str | None:
    target = placement(ink, safe, (ink.x, ink.y))
    if target is not None:
        before_pos = QPointF(item.pos())
        accepted = False
        try:
            item.setPos(before_pos + QPointF(target[0] - ink.x, target[1] - ink.y))
            accepted = outside_pixels(raster_ink(item), safe) == 0
            if accepted:
                return 'move'
        except (ValueError, RuntimeError):
            return None
        finally:
            if not accepted:
                item.setPos(before_pos)
    # Complex transforms can preserve their exact appearance via translation,
    # but are not rebuilt or flattened by this bounded fallback.
    if abs(item.rotation()) > 0.1 or not item.geometry_controller.is_neutral():
        return None
    rectangle = largest_rectangle(safe.pixels)
    if rectangle is None or min(rectangle[2:]) < 12:
        return None
    before = capture_block(item)
    x, y, width, height = rectangle
    best_candidate = None
    min_outside = outside_pixels(ink, safe)
    for ratio in (1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55, 0.5, 0.45, 0.4):
        if before.fontformat.font_size * ratio < 11:
            break
        candidate = TextBlkItem(deepcopy(before), item.idx)
        try:
            if ratio != 1.0:
                candidate.setRelFontSize(ratio)
            candidate.setRect([safe.x + x, safe.y + y, width, height])
            trial = raster_ink(candidate)
            target = placement(trial, safe, (ink.x, ink.y))
            if target is None:
                target = (safe.x + x + max(0, (width - trial.pixels.shape[1]) // 2),
                          safe.y + y + max(0, (height - trial.pixels.shape[0]) // 2))
            candidate.setPos(candidate.pos() + QPointF(target[0] - trial.x, target[1] - trial.y))
            curr_outside = outside_pixels(raster_ink(candidate), safe)
            if curr_outside == 0:
                proposal = capture_block(candidate)
                item.initTextBlock(proposal)
                return 'reflow' if ratio == 1.0 else 'reflow_and_shrink'
            elif curr_outside < min_outside:
                min_outside = curr_outside
                best_candidate = capture_block(candidate)
        except (ValueError, RuntimeError):
            pass
        finally:
            candidate.deleteLater()
    if best_candidate is not None and min_outside < outside_pixels(ink, safe):
        item.initTextBlock(best_candidate)
        return 'partial_shrink'
    return None


def restore_source_vertical(item: TextBlkItem, safe: Region) -> str | None:
    before = capture_block(item)
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
    # These breaks were produced by horizontal auto-layout, not the one-line
    # per-block model reply. Remove separators with a cursor to retain each
    # character's formatting, then rewrap this confirmed direction mismatch.
    expected_text = before.translation.replace('\n', '').replace('\r', '')
    candidate, applied = None, False
    try:
        candidate = TextBlkItem(deepcopy(before), item.idx)
        cursor = QTextCursor(candidate.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        while candidate.document().blockCount() > 1:
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cursor.deletePreviousChar()
        candidate.setVertical(True)
        candidate.blk.src_is_vertical = True
        candidate.setRect(list(before._bounding_rect))
        ink = raster_ink(candidate)
        action = 'source_direction'
        if outside_pixels(ink, safe):
            fitted = repair(candidate, ink, safe)
            if fitted is None:
                return None
            action += '_' + fitted
        proposal = capture_block(candidate)
        rectangle = list(proposal._bounding_rect)
        applied = True
        item.initTextBlock(proposal)
        # Switching document layout emits geometry signals while binding the
        # block; restore the measured candidate rectangle after that transition.
        item.setRect(rectangle)
        if (item.get_fontformat().vertical and item.toPlainText() == expected_text
                and outside_pixels(raster_ink(item), safe) == 0):
            applied = False
            return action
    except (ValueError, RuntimeError):
        return None
    finally:
        if applied:
            item.initTextBlock(deepcopy(before))
            item.load_rich_text_html(before.rich_text)
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
            item.setRect(list(before._bounding_rect))
        if candidate is not None:
            candidate.deleteLater()
    return None


def guard_page(items: list[TextBlkItem], image: np.ndarray) -> list[dict]:
    """Repair verified collisions or source-direction errors; preserve other items."""
    if not items:
        return []
    if image is None or image.ndim != 3 or image.shape[2] < 3:
        return [{'source_id': item.idx + 1, 'status': 'unverified', 'reason': 'missing_source_image'} for item in items]
    regions, centers = bind_regions(items, image)
    inks, unavailable = [], set()
    for index, item in enumerate(items):
        try:
            inks.append(raster_ink(item))
        except (ValueError, RuntimeError):
            unavailable.add(index)
            inks.append(Ink(0, 0, np.zeros((0, 0), bool)))
    records = []
    for index, (item, region, ink) in enumerate(zip(items, regions, inks)):
        record = {'source_id': item.idx + 1, 'status': 'unchanged'}
        if index in unavailable:
            record.update(status='unverified', reason='ink_measurement_unavailable')
        elif not ink.pixels.size:
            record.update(status='unverified', reason='no_visible_text')
        elif region is None:
            record.update(status='unverified', reason='no_confident_closed_white_region')
        else:
            outside = outside_pixels(ink, region)
            if region.frame_boundary is not None and not crosses_margin_frame(ink, region):
                outside = 0
            if region.kind != 'closed_white':
                record['region_kind'] = region.kind
            record.update(outside_pixels_before=outside, outside_pixels_after=outside)
            direction = source_vertical_evidence(item, image)
            if outside or direction:
                before_rect, before_size = list(item.absBoundingRect()), item.get_fontformat().font_size
                safe = safe_region(index, region, regions, centers, inks, max(2, round(before_size * 0.1)))
                action = restore_source_vertical(item, safe) if direction else repair(item, ink, safe)
                if direction:
                    record['source_direction'] = dict(direction, before_vertical=False,
                                                     after_vertical=item.get_fontformat().vertical)
                if action:
                    inks[index] = raster_ink(item)
                    record.update(status='adjusted', action=action,
                                  outside_pixels_after=outside_pixels(inks[index], region),
                                  before_rect=before_rect, after_rect=list(item.absBoundingRect()),
                                  before_font_size=before_size, after_font_size=item.get_fontformat().font_size)
                else:
                    record.update(status='needs_review', reason='no_verified_fit', before_rect=before_rect)
        records.append(record)
    return records
