import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont

# 直排括號專用轉換表
VERTICAL_BRACKET_MAP = {
    '（': '︵', '）': '︶', '(': '︵', ')': '︶',
    '「': '﹁', '」': '﹂', '『': '﹃', '』': '﹄',
    '【': '︻', '】': '︼', '《': '︽', '》': '︾',
    '〈': '︿', '〉': '﹀', '〔': '︹', '〕': '︺',
    '［': '﹇', '］': '﹈', '[': '﹇', ']': '﹈',
    '{': '︷', '}': '︸', '｛': '︷', '｝': '︸',
}

# 直排時必須順時針旋轉 90 度的符號集合
ROTATION_CHARS = {
    '…', '⋯', '—', '―', '–', '-', '～', '~', '〜', 'ー', '〰', '＝', '='
}


def normalize_punctuation(text: str, is_vertical: bool = True) -> str:
    """標點符號正規化：將連續英數符號轉為標準全形，直排時轉換專用符號。"""
    # 2個以上的連續句點轉為全形省略號 ……
    text = re.sub(r'\.{2,}', '……', text)
    # 連續減號轉破折號 ——
    text = re.sub(r'-{2,}', '——', text)
    if is_vertical:
        text = "".join(VERTICAL_BRACKET_MAP.get(c, c) for c in text)
    return text

# Candidate fonts (preferred Traditional Chinese)
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msjhbd.ttc",  # 微軟正黑體 粗體
    r"C:\Windows\Fonts\msjh.ttc",    # 微軟正黑體
    r"C:\Windows\Fonts\mingliu.ttc", # 細明體
]

_CACHED_FONT_PATH = None


def get_default_font_path() -> str:
    global _CACHED_FONT_PATH
    if _CACHED_FONT_PATH and Path(_CACHED_FONT_PATH).is_file():
        return _CACHED_FONT_PATH
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            _CACHED_FONT_PATH = candidate
            return candidate
    raise FileNotFoundError("找不到系統繁體中文字型 (如微軟正黑體)")


def _digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def resolve_page_context(output_dir: Path, page_identifier: str) -> Dict[str, Any]:
    """Resolve source, inpainted background, blocks, and published path for any given page."""
    output_dir = Path(output_dir).resolve()
    work_dir = output_dir / "_作業區"
    if not work_dir.exists():
        # Fallback for folder naming with different encoding or name
        work_candidates = [d for d in output_dir.iterdir() if d.is_dir() and d.name.startswith("_")]
        if not work_candidates:
            raise FileNotFoundError(f"找不到工作區目錄: {output_dir}")
        work_dir = work_candidates[0]

    file_map_path = work_dir / "file-map.json"
    if not file_map_path.exists():
        raise FileNotFoundError("找不到 file-map.json")
    file_map = json.loads(file_map_path.read_text("utf-8-sig"))

    target_entry = None
    clean_id = Path(page_identifier).name
    for entry in file_map:
        if clean_id in (entry.get("source_name"), entry.get("normalized_name"), entry.get("output_name")):
            target_entry = entry
            break

    if not target_entry:
        raise ValueError(f"在項目中找不到頁面: {page_identifier}")

    normalized_name = target_entry["normalized_name"]
    source_name = target_entry["source_name"]
    output_name = target_entry["output_name"]

    schedule_path = work_dir / "scheduled/schedule.json"
    schedule = json.loads(schedule_path.read_text("utf-8-sig"))
    target_attempt_dir = None
    for chunk in schedule.get("chunks", []):
        if not chunk.get("attempts"):
            continue
        if normalized_name in chunk.get("pages", []):
            attempt_rel = chunk["attempts"][-1]
            target_attempt_dir = (work_dir / "scheduled" / attempt_rel).resolve()
            break

    if not target_attempt_dir or not target_attempt_dir.exists():
        raise FileNotFoundError(f"找不到頁面 {normalized_name} 所屬的處理批次目錄")

    project_dir = target_attempt_dir / "project"
    inpainted_img_path = project_dir / "inpainted" / normalized_name
    if not inpainted_img_path.is_file():
        # 如果沒有 inpainted，嘗試 project/normalized_name 作為基底
        inpainted_img_path = project_dir / normalized_name

    source_path = work_dir / "inputs" / normalized_name
    if not source_path.is_file():
        status_path = work_dir / "status.json"
        if status_path.is_file():
            st = json.loads(status_path.read_text("utf-8-sig"))
            source_root = Path(st.get("source", ""))
            if (source_root / source_name).is_file():
                source_path = source_root / source_name

    current_published_path = output_dir / output_name

    # 讀取 blocks
    doc_path = project_dir / "imgtrans_project.json"
    blocks = []
    if doc_path.is_file():
        doc = json.loads(doc_path.read_text("utf-8-sig"))
        blocks = doc.get("pages", {}).get(normalized_name, [])

    # 讀取已存在的 overrides (若有)
    overrides_dir = work_dir / "overrides"
    override_file = overrides_dir / f"{Path(normalized_name).stem}.json"
    existing_overrides = {}
    if override_file.is_file():
        try:
            existing_overrides = json.loads(override_file.read_text("utf-8-sig")).get("blocks", {})
        except Exception:
            pass

    return {
        "output_dir": output_dir,
        "work_dir": work_dir,
        "normalized_name": normalized_name,
        "source_name": source_name,
        "output_name": output_name,
        "dimensions": target_entry.get("dimensions"),
        "source_path": source_path,
        "inpainted_path": inpainted_img_path,
        "current_published_path": current_published_path,
        "attempt_dir": target_attempt_dir,
        "blocks": blocks,
        "existing_overrides": existing_overrides,
        "override_file": override_file,
    }


def _optical_metrics(font_size: float, is_vertical: bool, num_lines: int) -> Dict[str, float]:
    """動態光學間距計算：小字緊湊凝聚，大字舒展大器。"""
    if font_size < 18.0:
        char_ratio = 1.06
        letter_spacing = 1.00
    elif font_size < 30.0:
        char_ratio = 1.10
        letter_spacing = 1.03
    else:
        char_ratio = 1.15
        letter_spacing = 1.05

    line_spacing = 1.00 if num_lines <= 1 else (1.22 if is_vertical else 1.20)
    return {
        "char_ratio": char_ratio,
        "letter_spacing": letter_spacing,
        "line_spacing": line_spacing,
    }


def render_page_image(
    context: Dict[str, Any],
    overrides: Optional[Dict[str, Any]] = None,
    font_path: Optional[str] = None
) -> Image.Image:
    """高精度、秒級重繪單頁文字 (PIL-based)，支援手動換行、字級縮放與邊框拖曳。"""
    inpainted_path = context["inpainted_path"]
    if not inpainted_path.is_file():
        raise FileNotFoundError(f"找不到無字底圖: {inpainted_path}")

    base_img = Image.open(inpainted_path).convert("RGBA")
    draw = ImageDraw.Draw(base_img)
    font_file = font_path or get_default_font_path()

    merged_overrides = dict(context.get("existing_overrides", {}))
    if overrides:
        merged_overrides.update(overrides)

    for i, blk in enumerate(context["blocks"]):
        block_id = str(i + 1)
        ovr = merged_overrides.get(block_id, {})

        # 文字內容：優先讀取覆寫文字，次之讀取原本翻譯
        text = ovr.get("translation", blk.get("translation", ""))
        if not text or not str(text).strip():
            continue

        # 座標範圍：優先讀取自訂框，次之讀取原本框
        box = ovr.get("box") or getattr(blk, "_bubble_xyxy", None) or blk.get("xyxy") or [0, 0, 100, 100]
        x0, y0, x1, y1 = map(float, box)
        box_w, box_h = max(10.0, x1 - x0), max(10.0, y1 - y0)

        # 直橫排判定
        is_vertical = ovr.get("vertical")
        if is_vertical is None:
            is_vertical = blk.get("vertical", True) if blk.get("vertical") is not None else (box_h > box_w * 1.15)

        # 基礎字體大小
        base_font_size = float(blk.get("font_size", 24))
        if base_font_size < 14:
            base_font_size = 22.0

        # 字體縮放倍率 (預設 1.0，如使用者點 A+ 為 1.15，A- 為 0.85)
        scale = float(ovr.get("scale", 1.0))
        font_size = max(10, int(round(base_font_size * scale)))

        # 描邊粗細
        stroke_width = max(2, int(round(font_size * 0.12)))

        # 手動換行處理：若使用者有輸入 \n，完全尊重手動換行
        lines = [line.strip() for line in str(text).split("\n") if line.strip()]
        if not lines:
            continue

        metrics = _optical_metrics(font_size, is_vertical, len(lines))
        font = ImageFont.truetype(font_file, font_size)

        if is_vertical:
            # === 直排渲染 (中文直排習慣：多欄時由右至左排列) ===
            col_width = font_size * metrics["line_spacing"]
            total_cols_w = len(lines) * col_width
            start_x = x1 - (box_w - total_cols_w) / 2 - col_width / 2

            for col_idx, raw_line in enumerate(lines):
                line = normalize_punctuation(raw_line, is_vertical=True)
                col_x = start_x - col_idx * col_width
                chars = list(line)

                # 計算每個字符的高度步進 (連續省略號、破折號緊湊化排版)
                heights = []
                for ch in chars:
                    if ch in ('…', '⋯'):
                        heights.append(font_size * 0.85)
                    elif ch in ('—', '―'):
                        heights.append(font_size * 0.95)
                    else:
                        heights.append(font_size * metrics["char_ratio"])

                total_h = sum(heights)
                start_y = y0 + max(0.0, (box_h - total_h) / 2)

                curr_y = start_y
                for ch, h in zip(chars, heights):
                    cy = curr_y + h / 2
                    if ch in ROTATION_CHARS:
                        # 符號順時針旋轉 90 度以符合直排方向 (如省略號、破折號、波浪號)
                        box_dim = int(font_size * 1.5)
                        ch_img = Image.new("RGBA", (box_dim, box_dim), (0, 0, 0, 0))
                        ch_draw = ImageDraw.Draw(ch_img)
                        ch_draw.text(
                            (box_dim / 2, box_dim / 2),
                            ch,
                            font=font,
                            fill=(0, 0, 0, 255),
                            stroke_width=stroke_width,
                            stroke_fill=(255, 255, 255, 255),
                            anchor="mm"
                        )
                        rotated = ch_img.rotate(-90, resample=Image.Resampling.BICUBIC)
                        px = int(col_x - box_dim / 2)
                        py = int(cy - box_dim / 2)
                        base_img.alpha_composite(rotated, (px, py))
                    else:
                        draw.text(
                            (col_x, cy),
                            ch,
                            font=font,
                            fill=(0, 0, 0, 255),
                            stroke_width=stroke_width,
                            stroke_fill=(255, 255, 255, 255),
                            anchor="mm"
                        )
                    curr_y += h
        else:
            # === 橫排渲染 (由上至下排列，每行水平置中) ===
            row_height = font_size * metrics["line_spacing"]
            total_rows_h = len(lines) * row_height
            start_y = y0 + max(0.0, (box_h - total_rows_h) / 2)

            for row_idx, raw_line in enumerate(lines):
                line = normalize_punctuation(raw_line, is_vertical=False)
                row_y = start_y + row_idx * row_height
                center_x = x0 + box_w / 2
                draw.text(
                    (center_x, row_y),
                    line,
                    font=font,
                    fill=(0, 0, 0, 255),
                    stroke_width=stroke_width,
                    stroke_fill=(255, 255, 255, 255),
                    anchor="ma"
                )

    return base_img.convert("RGB")


def validate_published_page(context: Dict[str, Any]) -> None:
    """Allow fast edits only after normal publication or explicit review approval.

    Read current disk state rather than trusting a previously opened GUI view.
    This gate is not a multi-file transaction or a replacement for job locking.
    """
    output_dir = Path(context["output_dir"]).resolve()
    work_dir = Path(context["work_dir"]).resolve()
    page = context["normalized_name"]
    output_name = context["output_name"]
    if (not isinstance(page, str) or not isinstance(output_name, str)
            or any(not name or name in (".", "..") or Path(name).name != name
                   for name in (page, output_name))
            or work_dir.parent != output_dir or not work_dir.name.startswith("_")):
        raise ValueError("精修工作區或頁面路徑無效，未儲存任何內容。")

    mapping = json.loads((work_dir / "file-map.json").read_text("utf-8-sig"))
    status = json.loads((work_dir / "status.json").read_text("utf-8-sig"))
    if (not isinstance(mapping, list) or any(not isinstance(row, dict) for row in mapping)
            or not isinstance(status, dict)
            or status.get("status") not in ("completed", "partial", "failed", "cancelled")
            or not isinstance(status.get("failed_pages"), dict)
            or any(not isinstance(key, str) or not isinstance(value, dict)
                   for key, value in status["failed_pages"].items())):
        raise ValueError("工作狀態缺失、無效或仍在執行，不能直接發布精修頁面。")
    if page in status["failed_pages"]:
        raise ValueError("本頁仍待審查或曾失敗；請先完成原審查／發布流程，再儲存精修。")
    for key in ("review_required_pages", "pending_pages"):
        if key in status:
            pending = status[key]
            if not isinstance(pending, (dict, list)):
                raise ValueError("待審查狀態無效，不能直接發布精修頁面。")
            if isinstance(pending, list) and any(not isinstance(item, str) for item in pending):
                raise ValueError("待審查頁面清單無效，不能直接發布精修頁面。")
            if page in pending:
                raise ValueError("本頁仍待審查；請先完成原審查／發布流程。")

    entries = [row for row in mapping if row.get("normalized_name") == page]
    if (len(entries) != 1 or entries[0].get("output_name") != output_name
            or sum(row.get("output_name") == output_name for row in mapping) != 1):
        raise ValueError("成品對應表不是唯一且一致的頁面記錄，未儲存任何內容。")
    expected = entries[0].get("output_sha256")
    destination = output_dir / output_name
    if (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected)
            or not destination.is_file() or destination.resolve().parent != output_dir
            or _digest_file(destination) != expected.lower()):
        raise ValueError("本頁尚未發布或成品已變更；請先核對原審查／發布結果。")


def save_and_publish_page(
    context: Dict[str, Any],
    overrides: Dict[str, Any],
    rendered_img: Optional[Image.Image] = None
) -> Tuple[Path, str]:
    """Persist fast edits only for a verified, already-published page."""
    validate_published_page(context)
    output_dir = context["output_dir"]
    work_dir = context["work_dir"]
    output_name = context["output_name"]
    normalized_name = context["normalized_name"]

    # 1. 產生渲染圖 (若未傳入)
    if rendered_img is None:
        rendered_img = render_page_image(context, overrides)

    # 2. 儲存覆寫設定到 _作業區/overrides/{stem}.json
    overrides_dir = work_dir / "overrides"
    overrides_dir.mkdir(parents=True, exist_ok=True)
    override_path = overrides_dir / f"{Path(normalized_name).stem}.json"

    # 合併現有與新覆寫
    existing = dict(context.get("existing_overrides", {}))
    existing.update(overrides)
    save_data = {
        "version": 1,
        "page": normalized_name,
        "output_name": output_name,
        "blocks": existing
    }
    override_path.write_text(json.dumps(save_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3. 儲存圖片至輸出目錄
    dest_path = output_dir / output_name
    rendered_img.save(dest_path, quality=95)
    new_sha256 = _digest_file(dest_path)

    # 4. 更新 file-map.json sha256 避免校驗中斷
    file_map_path = work_dir / "file-map.json"
    if file_map_path.is_file():
        file_map = json.loads(file_map_path.read_text("utf-8-sig"))
        for entry in file_map:
            if entry.get("normalized_name") == normalized_name:
                entry["output_sha256"] = new_sha256
                break
        file_map_path.write_text(json.dumps(file_map, ensure_ascii=False, indent=2), encoding="utf-8")

    return dest_path, new_sha256


def build_page_review_view(output_dir: Path, page_identifier: str) -> Dict[str, Any]:
    """構建符合 ReviewDialog 所需的 view 字典，讓任意頁面皆能無縫進入精修工作台。"""
    ctx = resolve_page_context(output_dir, page_identifier)
    regions = []
    overrides = ctx.get("existing_overrides", {})
    for i, blk in enumerate(ctx["blocks"]):
        sid = i + 1
        sid_str = str(sid)
        ovr = overrides.get(sid_str, {})
        text = ovr.get("translation", blk.get("translation", ""))
        ja_list = blk.get("text", [""])
        ja_text = ja_list[0] if isinstance(ja_list, list) and ja_list else str(ja_list)
        box = ovr.get("box") or getattr(blk, "_bubble_xyxy", None) or blk.get("xyxy") or [0, 0, 100, 100]
        x0, y0, x1, y1 = map(float, box)
        regions.append({
            "id": sid,
            "japanese": ja_text,
            "translation": text,
            "source_rect": [x0, y0, x1, y1],
            "target_rect": [x0, y0, x1, y1],
        })

    dims = ctx.get("dimensions") or [1000, 1500]
    return {
        "page": ctx["normalized_name"],
        "source_name": ctx["source_name"],
        "normalized_name": ctx["normalized_name"],
        "output_name": ctx["output_name"],
        "source_path": str(ctx["source_path"]),
        "preview_path": str(ctx["current_published_path"] if ctx["current_published_path"].is_file() else ctx["inpainted_path"]),
        "size": dims,
        "regions": regions,
        "confirmed_ids": [],
        "pending_path": str(ctx["current_published_path"]),
        "pending_sha256": "manual_override",
    }
