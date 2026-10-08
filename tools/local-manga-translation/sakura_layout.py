"""Conservative vertical fitting for this opt-in local translation launcher."""
import math
import unicodedata


def _resolution_scale(image_size: tuple[int, int] | None, detected_size: float | None) -> float:
    """Calculate scale factor relative to standard manga resolution (base short side 1050px)."""
    if image_size and len(image_size) == 2:
        w_img, h_img = image_size
        short_side = min(w_img, h_img)
        if short_side > 0:
            return max(1.0, short_side / 1050.0)
    if detected_size and detected_size > 42.0:
        return max(1.0, float(detected_size) / 30.0)
    return 1.0


def _optical_metrics(font_size: float, scale: float, is_vertical: bool, columns_or_lines: int) -> dict:
    """Calculate dynamic optical spacing: tight for small text to avoid sparse gaps, relaxed for large text."""
    pt_norm = font_size / max(0.1, scale)
    if pt_norm < 18.0:
        # 小字嚴禁間距過大：保持緊湊凝聚力
        char_ratio = 1.08
        letter_spacing = 1.00
    elif pt_norm < 30.0:
        # 中等字級：舒適自然
        char_ratio = 1.12
        letter_spacing = 1.03
    else:
        # 大字級：筆劃黑度重，略微舒展防黏連
        char_ratio = 1.16
        letter_spacing = 1.05

    if is_vertical:
        line_spacing = 1.00 if columns_or_lines == 1 else 1.22
    else:
        line_spacing = 1.00 if columns_or_lines == 1 else 1.20

    return {
        'char_ratio': char_ratio,
        'letter_spacing': letter_spacing,
        'line_spacing': line_spacing
    }


def fitted_vertical(text: str, xyxy: list, detected_size: float, image_size: tuple[int, int] | None = None) -> dict | None:
    """Fit CJK graphemes inside the original detected rectangle with optical spacing and vertical centering."""
    source = text.replace('\n', '').replace('\r', '')
    if not source or len(xyxy) != 4:
        return None
    x1, y1, x2, y2 = map(float, xyxy)
    width, height = x2 - x1, y2 - y1
    if min(width, height) < 6:
        return None
    units = []
    for char in source:
        if units and (unicodedata.combining(char) or char in ('\ufe0f', '\u200d') or units[-1].endswith('\u200d')):
            units[-1] += char
        else:
            units.append(char)
    scale = _resolution_scale(image_size, detected_size)
    box_min = min(width, height)
    thresh_large = 150.0 * scale
    thresh_med = 100.0 * scale
    base_cap = 34.0 if box_min >= thresh_large else (30.0 if box_min >= thresh_med else 26.0)
    max_cap = base_cap * scale
    if detected_size and detected_size > 0:
        maximum = min(max(float(detected_size) * 1.05, 16.0 * scale), max_cap)
    else:
        maximum = max_cap

    aspect_ratio = height / max(1.0, width)
    is_tall_bubble = aspect_ratio >= 1.4

    options = []
    for columns in range(1, len(units) + 1):
        rows = math.ceil(len(units) / columns)
        col_w_factor = 1.18 if columns == 1 else (columns * 1.22 + 0.22)

        probe_size = min(maximum, width / col_w_factor, height / (rows * 1.13 + 0.25))
        if probe_size < 1.0:
            continue
        metrics = _optical_metrics(probe_size, scale, is_vertical=True, columns_or_lines=columns)
        char_ratio = metrics['char_ratio']

        size = min(maximum, width / col_w_factor, height / (rows * char_ratio + 0.25))
        size = max(1.0, math.floor(size * 4) / 4)

        text_h = rows * size * char_ratio
        fill_ratio = min(1.0, text_h / height)

        # 空間填充平衡：高長型氣泡嚴防盲目腰斬切成短列而留下大半空白
        if is_tall_bubble:
            penalty = (fill_ratio / 0.55) ** 0.85 if fill_ratio < 0.55 else 1.0
            score = size * penalty
        else:
            remainder = len(units) % columns
            orphan_penalty = 0.95 if (columns > 1 and remainder == 1 and rows > 2) else 1.0
            score = size * orphan_penalty

        options.append((score, size, -columns, rows, char_ratio))

    if not options:
        return None
    score, size, negative_columns, rows, char_ratio = max(options)
    chosen_columns = -negative_columns
    final_metrics = _optical_metrics(size, scale, is_vertical=True, columns_or_lines=chosen_columns)

    # 垂直置中定位：消除下半部單向大片留白，上下留白對稱均勻
    actual_text_h = rows * size * char_ratio
    if actual_text_h < height:
        offset_y = max(0.0, (height - actual_text_h) / 2.0)
        new_y1 = y1 + offset_y
        new_height = height - offset_y
    else:
        new_y1 = y1
        new_height = height

    display = '\n'.join(''.join(units[i:i + rows]) for i in range(0, len(units), rows))
    if display.replace('\n', '') != source:
        raise ValueError('Layout attempted to change words')
    return {
        'translation': display,
        'rect': [x1, new_y1, width, new_height],
        'font_size': size,
        'columns': chosen_columns,
        'rows': rows,
        'letter_spacing': final_metrics['letter_spacing'],
        'line_spacing': final_metrics['line_spacing'],
        'alignment': 1,  # Center
        'small_text_review': size < (11.0 * scale),
        'vertical': True
    }


def fitted_horizontal(text: str, xyxy: list, detected_size: float, image_size: tuple[int, int] | None = None) -> dict | None:
    """Fit CJK graphemes inside the original detected rectangle horizontally with vertical centering."""
    source = text.replace('\n', '').replace('\r', '')
    if not source or len(xyxy) != 4:
        return None
    x1, y1, x2, y2 = map(float, xyxy)
    width, height = x2 - x1, y2 - y1
    if min(width, height) < 6:
        return None
    units = []
    for char in source:
        if units and (unicodedata.combining(char) or char in ('\ufe0f', '\u200d') or units[-1].endswith('\u200d')):
            units[-1] += char
        else:
            units.append(char)
    scale = _resolution_scale(image_size, detected_size)
    box_min = min(width, height)
    thresh_large = 150.0 * scale
    thresh_med = 100.0 * scale
    base_cap = 34.0 if box_min >= thresh_large else (30.0 if box_min >= thresh_med else 26.0)
    max_cap = base_cap * scale
    if detected_size and detected_size > 0:
        maximum = min(max(float(detected_size) * 1.05, 16.0 * scale), max_cap)
    else:
        maximum = max_cap

    options = []
    for lines in range(1, len(units) + 1):
        chars_per_line = math.ceil(len(units) / lines)
        line_h_factor = 1.08 if lines == 1 else 1.20

        probe_size = min(maximum, width / (chars_per_line * 1.08 + 0.25), height / (lines * line_h_factor + 0.25))
        if probe_size < 1.0:
            continue
        metrics = _optical_metrics(probe_size, scale, is_vertical=False, columns_or_lines=lines)

        size = min(maximum, width / (chars_per_line * 1.08 + 0.25), height / (lines * line_h_factor + 0.25))
        size = max(1.0, math.floor(size * 4) / 4)

        remainder = len(units) % chars_per_line
        orphan_penalty = 0.95 if (lines > 1 and remainder == 1 and chars_per_line > 2) else 1.0
        score = size * orphan_penalty

        options.append((score, size, -lines, chars_per_line, line_h_factor))

    if not options:
        return None
    score, size, negative_lines, chars_per_line, line_h_factor = max(options)
    chosen_lines = -negative_lines
    final_metrics = _optical_metrics(size, scale, is_vertical=False, columns_or_lines=chosen_lines)

    # 橫排垂直置中定位
    actual_text_h = chosen_lines * size * line_h_factor
    if actual_text_h < height:
        offset_y = max(0.0, (height - actual_text_h) / 2.0)
        new_y1 = y1 + offset_y
        new_height = height - offset_y
    else:
        new_y1 = y1
        new_height = height

    display = '\n'.join(''.join(units[i:i + chars_per_line]) for i in range(0, len(units), chars_per_line))
    if display.replace('\n', '') != source:
        raise ValueError('Layout attempted to change words')
    return {
        'translation': display,
        'rect': [x1, new_y1, width, new_height],
        'font_size': size,
        'lines': chosen_lines,
        'letter_spacing': final_metrics['letter_spacing'],
        'line_spacing': final_metrics['line_spacing'],
        'alignment': 1,  # Center
        'small_text_review': size < (11.0 * scale),
        'vertical': False
    }


def fitted_layout(text: str, xyxy: list, detected_size: float, image_size: tuple[int, int] | None = None) -> dict | None:
    """Adaptive layout choosing vertical or horizontal fitting based on aspect ratio and readability."""
    if len(xyxy) != 4:
        return None
    x1, y1, x2, y2 = map(float, xyxy)
    width, height = x2 - x1, y2 - y1
    v_fit = fitted_vertical(text, xyxy, detected_size, image_size=image_size)
    h_fit = fitted_horizontal(text, xyxy, detected_size, image_size=image_size)
    if not v_fit:
        return h_fit
    if not h_fit:
        return v_fit
    aspect_ratio = width / max(1.0, height)
    scale = _resolution_scale(image_size, detected_size)
    min_pt_thresh = 12.0 * scale
    if aspect_ratio >= 1.3 or (v_fit['font_size'] < min_pt_thresh and h_fit['font_size'] >= v_fit['font_size'] * 1.2):
        return h_fit
    return v_fit


def install_adaptive_fit() -> None:
    """Fit newly generated blocks using adaptive vertical or horizontal layout."""
    from ballontranslator.ui.text_engine.editing.manager import SceneTextManager
    from ballontranslator.utils.textblock import TextBlock

    original = SceneTextManager.addTextBlock

    def add_fitted(self, blk=None):
        if (isinstance(blk, TextBlock)
                and not blk.rich_text and abs(blk.angle or 0) < 0.1):
            img_size = None
            if hasattr(self, 'imgtrans_proj') and getattr(self.imgtrans_proj, 'img_array', None) is not None:
                img_arr = self.imgtrans_proj.img_array
                img_size = (img_arr.shape[1], img_arr.shape[0])
            bubble_box = getattr(blk, '_bubble_xyxy', blk.xyxy)
            fitted = fitted_layout(blk.translation, bubble_box, blk.font_size, image_size=img_size)
            if fitted:
                if not hasattr(blk, '_bubble_xyxy'):
                    blk._bubble_xyxy = list(blk.xyxy)
                blk.translation = fitted['translation']
                blk._bounding_rect = fitted['rect']
                blk.fontformat.font_size = fitted['font_size']
                blk._detected_font_size = fitted['font_size']
                blk.font_size = fitted['font_size']
                blk.fontformat.vertical = fitted['vertical']
                blk.src_is_vertical = fitted['vertical']
                if 'letter_spacing' in fitted:
                    blk.fontformat.letter_spacing = fitted['letter_spacing']
                if 'line_spacing' in fitted:
                    blk.fontformat.line_spacing = fitted['line_spacing']
                if 'alignment' in fitted:
                    blk.fontformat.alignment = fitted['alignment']
        return original(self, blk)

    SceneTextManager.addTextBlock = add_fitted


install_vertical_fit = install_adaptive_fit


def install_batch_autolayout() -> None:
    """Use Ballons' own layout during the external-translation render stage.

    Ballons normally enables automatic layout only after detection/translation.
    Our batch has already done those stages, so enable it at the same scene
    creation boundary, only while the native page-finished callback is running.
    Passive project loading and the ordinary GUI retain their existing behavior.
    """
    install_adaptive_fit()
    from ballontranslator.ui.mainwindow import MainWindow
    from ballontranslator.ui.text_engine.editing import manager
    from ballontranslator.ui.text_engine.editing.manager import SceneTextManager
    from ballontranslator.utils.config import pcfg
    from ballontranslator.utils.text_processing import LANGSET_CH, half_len
    from bubble_guard import guard_page
    from source_layout import prepare_layout, apply_layout
    import json
    from pathlib import Path

    finish_page = MainWindow.on_pagtrans_finished
    populate = SceneTextManager.populateSceneTextitems
    segment = manager.seg_text
    rendering = False

    def segment_verbatim(text, language):
        words, delimiter = segment(text, language)
        if not rendering or language not in LANGSET_CH or delimiter:
            return words, delimiter
        # Native Chinese segmentation can normalize punctuation when ASCII is
        # present. Keep its word boundaries, measured with the original glyphs.
        source = text.replace('\n', '').replace('\r', '')
        normalized = half_len(source)
        restored, offset = [], 0
        for word in words:
            start = normalized.find(half_len(word), offset)
            if start < 0 or source[offset:start].strip():
                raise ValueError('Native segmentation changed translation characters')
            end = start + len(word)
            restored.append(source[offset:end])
            offset = end
        if source[offset:].strip():
            raise ValueError('Native segmentation lost translation characters')
        if restored:
            restored[-1] += source[offset:]
        return restored, delimiter

    def finish_with_layout(self, page_index):
        nonlocal rendering
        previous, rendering = rendering, True
        try:
            img_size = None
            if hasattr(self, 'imgtrans_proj') and getattr(self.imgtrans_proj, 'img_array', None) is not None:
                img_arr = self.imgtrans_proj.img_array
                img_size = (img_arr.shape[1], img_arr.shape[0])
            blk_list = self.imgtrans_proj.get_blklist_byidx(page_index)
            for blk in blk_list:
                if not blk.rich_text and abs(blk.angle or 0) < 0.1:
                    bubble_box = getattr(blk, '_bubble_xyxy', blk.xyxy)
                    fitted = fitted_layout(blk.translation, bubble_box, blk.font_size, image_size=img_size)
                    if fitted:
                        if not hasattr(blk, '_bubble_xyxy'):
                            blk._bubble_xyxy = list(blk.xyxy)
                        blk.translation = fitted['translation']
                        blk._bounding_rect = fitted['rect']
                        blk.fontformat.font_size = fitted['font_size']
                        blk._detected_font_size = fitted['font_size']
                        blk.font_size = fitted['font_size']
                        blk.fontformat.vertical = fitted['vertical']
                        blk.src_is_vertical = fitted['vertical']
                        if 'letter_spacing' in fitted:
                            blk.fontformat.letter_spacing = fitted['letter_spacing']
                        if 'line_spacing' in fitted:
                            blk.fontformat.line_spacing = fitted['line_spacing']
                        if 'alignment' in fitted:
                            blk.fontformat.alignment = fitted['alignment']
            res = finish_page(self, page_index)
            for blk in blk_list:
                if not blk.rich_text and abs(blk.angle or 0) < 0.1:
                    bubble_box = getattr(blk, '_bubble_xyxy', blk.xyxy)
                    fitted = fitted_layout(blk.translation, bubble_box, blk.font_size, image_size=img_size)
                    if fitted:
                        blk._bounding_rect = fitted['rect']
                        blk.fontformat.font_size = fitted['font_size']
                        blk._detected_font_size = fitted['font_size']
                        blk.font_size = fitted['font_size']
                        if 'letter_spacing' in fitted:
                            blk.fontformat.letter_spacing = fitted['letter_spacing']
                        if 'line_spacing' in fitted:
                            blk.fontformat.line_spacing = fitted['line_spacing']
                        if 'alignment' in fitted:
                            blk.fontformat.alignment = fitted['alignment']
            return res
        finally:
            rendering = previous

    def populate_with_layout(self):
        previous = self.auto_textlayout_flag
        if rendering:
            self.auto_textlayout_flag = pcfg.let_autolayout_flag
        try:
            plan = None
            if rendering and self.auto_textlayout_flag:
                plan = prepare_layout(self.imgtrans_proj.current_block_list(), self.imgtrans_proj.img_array,
                                      Path(self.imgtrans_proj.directory), self.imgtrans_proj.current_img,
                                      pcfg.let_writing_mode_flag == 1)
            result = populate(self)
            if rendering:
                records = guard_page(self.textblk_item_list, self.imgtrans_proj.img_array)
                changed = any(record['status'] == 'adjusted' for record in records)
                changed = apply_layout(plan, self.textblk_item_list, records) or changed
                if changed:
                    self.updateTextBlkList()
                path = Path(self.imgtrans_proj.directory) / 'layout-guard.json'
                report = json.loads(path.read_text('utf-8')) if path.exists() else {'version': 1, 'pages': {}}
                report['pages'][self.imgtrans_proj.current_img] = records
                temporary = path.with_suffix('.json.tmp')
                temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', 'utf-8')
                temporary.replace(path)
            return result
        finally:
            self.auto_textlayout_flag = previous

    MainWindow.on_pagtrans_finished = finish_with_layout
    SceneTextManager.populateSceneTextitems = populate_with_layout
    manager.seg_text = segment_verbatim
