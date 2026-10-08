"""Conservative, side-effect-free source direction proposals for manga blocks.

``propose(blocks, rows)`` returns one JSON-serializable decision per source block.
Direction is True for vertical, False for horizontal, and None for insufficient
evidence. The caller must separately verify Magi coverage and shared bubble
geometry before applying inherited directions. No translation text is examined.
"""
from collections import defaultdict
import math
from numbers import Real
import unicodedata


MAX_BLOCKS = 4096
MAX_LINES = 128
MAX_TEXT_LENGTH = 8192
MAX_COORDINATE = 1_000_000
MAX_FONT_SIZE = 4096
MAX_ANGLE = 5
LINE_ASPECT = 1.6


def _number(value, minimum=0, maximum=MAX_COORDINATE):
    return (isinstance(value, Real) and not isinstance(value, bool)
            and minimum <= value <= maximum and math.isfinite(value))


def content(block):
    """Return validated source text, or None for malformed source data."""
    if not isinstance(block, dict):
        return None
    value = block.get('text')
    if isinstance(value, list):
        if not value or len(value) > MAX_LINES or not all(isinstance(x, str) for x in value):
            return None
        value = ''.join(value)
    return value if isinstance(value, str) and len(value) <= MAX_TEXT_LENGTH else None


def letter_count(text):
    return sum(unicodedata.category(char)[0] in ('L', 'N') for char in text)


def _box(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if not all(_number(number) for number in value):
        return None
    x, y, x2, y2 = value
    return tuple(value) if x2 > x and y2 > y else None


def _line_box(line):
    if not isinstance(line, (list, tuple)) or len(line) != 4:
        return None
    if any(not isinstance(point, (list, tuple)) or len(point) != 2
           or not all(_number(number) for number in point) for point in line):
        return None
    xs, ys = zip(*line)
    bounds = _box((min(xs), min(ys), max(xs), max(ys)))
    if bounds is None:
        return None
    # Require a simple, near-axis-aligned quadrilateral. A bounding rectangle
    # alone would hide rotated, skewed or crossed detector polygons.
    axes = []
    area = 0
    for point, next_point in zip(line, line[1:] + line[:1]):
        dx, dy = next_point[0] - point[0], next_point[1] - point[1]
        longer = max(abs(dx), abs(dy))
        if longer == 0 or min(abs(dx), abs(dy)) / longer > math.tan(math.radians(MAX_ANGLE)):
            return None
        axes.append(abs(dy) > abs(dx))
        area += point[0] * next_point[1] - next_point[0] * point[1]
    if any(axes[i] == axes[(i + 1) % 4] for i in range(4)):
        return None
    x, y, x2, y2 = bounds
    if abs(area) / 2 < (x2 - x) * (y2 - y) * 0.85:
        return None
    return bounds


def _geometry(block):
    if content(block) is None or type(block.get('src_is_vertical')) is not bool:
        return None
    box = _box(block.get('xyxy'))
    angle = block.get('angle', 0)
    font = block.get('fontformat')
    if (box is None or not _number(angle, -MAX_ANGLE, MAX_ANGLE)
            or not isinstance(font, dict)
            or not _number(font.get('font_size'), 1, MAX_FONT_SIZE)):
        return None
    lines = block.get('lines')
    if not isinstance(lines, (list, tuple)) or not 1 <= len(lines) <= MAX_LINES:
        return None
    boxes = [_line_box(line) for line in lines]
    if any(bounds is None for bounds in boxes):
        return None
    tolerance = max(2, min(box[2] - box[0], box[3] - box[1]) * 0.1)
    if any(bounds[0] < box[0] - tolerance or bounds[1] < box[1] - tolerance
           or bounds[2] > box[2] + tolerance or bounds[3] > box[3] + tolerance
           for bounds in boxes):
        return None
    return box, boxes, font['font_size']


def own_direction(block):
    """Use confident original line geometry; never infer from block aspect."""
    geometry = _geometry(block)
    text = content(block)
    if geometry is None or letter_count(text) < 2:
        return None
    boxes = geometry[1]
    pieces = block['text']
    if isinstance(pieces, str):
        pieces = [pieces]
    # A missing text-to-line mapping must not turn a series of single letters
    # into false multi-character evidence.
    if len(pieces) != len(boxes):
        return None
    directions = []
    for piece, (x, y, x2, y2) in zip(pieces, boxes):
        if letter_count(piece) < 2:
            continue
        width, height = x2 - x, y2 - y
        if height >= width * LINE_ASPECT:
            directions.append(True)
        elif width >= height * LINE_ASPECT:
            directions.append(False)
        else:
            return None
    return directions[0] if directions and len(set(directions)) == 1 else None


def _compatible(a, b, vertical):
    (ax, ay, ax2, ay2), _, a_font = a
    (bx, by, bx2, by2), _, b_font = b
    scale = max(ax2 - ax, ay2 - ay, a_font, b_font)
    if vertical:
        return abs(ay - by) <= scale and abs((ax + ax2 - bx - bx2) / 2) <= scale * 3
    return abs(ax - bx) <= scale and abs((ay + ay2 - by - by2) / 2) <= scale * 3


def _group_rows(rows, count):
    if not isinstance(rows, (list, tuple)) or len(rows) != count:
        return None
    by_id = {}
    for row in rows:
        if not isinstance(row, dict):
            return None
        source_id, group = row.get('id'), row.get('magi_text_id')
        if (type(source_id) is not int or not 1 <= source_id <= count
                or source_id in by_id):
            return None
        if group is not None and not (
                (type(group) is int and group >= 0)
                or (isinstance(group, str) and 0 < len(group) <= 128 and group.strip())):
            return None
        by_id[source_id] = group
    return by_id


def propose(blocks, rows):
    """Propose source directions without mutating either input collection.

    Rows map each block exactly once using 1-based ``id`` and ``magi_text_id``.
    Malformed mappings abstain for the whole page. Single-letter decisions use
    original, independently confident peers only; inherited results cannot
    recursively become evidence. Unknown, rotated and mixed sources keep their
    current direction. An already clear block reports its own line direction
    even in a mixed group, but only single-letter inheritance can request a
    change. This keeps the repair confined to ambiguous short source text.
    """
    if not isinstance(blocks, (list, tuple)) or len(blocks) > MAX_BLOCKS:
        return []
    mapping = _group_rows(rows, len(blocks))
    known = [own_direction(block) for block in blocks]
    geometries = [_geometry(block) for block in blocks]
    groups = defaultdict(list)
    if mapping is not None:
        for source_id, group in mapping.items():
            if group is not None:
                groups[group].append(source_id - 1)
    proposals = []
    for index, block in enumerate(blocks):
        original = block.get('src_is_vertical') if isinstance(block, dict) else None
        record = {'source_id': index + 1,
                  'original_vertical': original if type(original) is bool else None,
                  'direction': None, 'reason': 'insufficient_direction_evidence',
                  'evidence_ids': [], 'change': False}
        if mapping is None:
            record['reason'] = 'invalid_reading_order'
        elif geometries[index] is None:
            record['reason'] = 'invalid_or_complex_source_geometry'
        elif known[index] is not None:
            record.update(direction=known[index], reason='own_multichar_line_geometry')
        elif letter_count(content(block)) == 1 and len(geometries[index][1]) == 1:
            neighbors = [other for other in groups.get(mapping[index + 1], [])
                         if other != index and known[other] is not None]
            consensus = {known[other] for other in neighbors}
            if len(consensus) > 1:
                record['reason'] = 'mixed_group_requires_review'
            elif len(consensus) == 1:
                vertical = next(iter(consensus))
                supported = [other for other in neighbors
                             if _compatible(geometries[index], geometries[other], vertical)]
                if supported:
                    record.update(direction=vertical,
                                  reason='single_letter_inherits_supported_group_direction',
                                  evidence_ids=sorted(other + 1 for other in supported))
        record['change'] = (record['reason'] == 'single_letter_inherits_supported_group_direction'
                            and record['direction'] != original)
        proposals.append(record)
    return proposals
