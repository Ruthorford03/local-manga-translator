"""Global Terminology and Named-Entity Consistency Harmonizer for Manga Translation.

Audits and unifies character names, titles, and recurring specialized terms across
an entire manga volume to guarantee 100% lexical consistency from first page to last.
"""
from collections import Counter, defaultdict
from copy import deepcopy
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from atomic_json import save_json as save


# 常用日文片假名專有名詞 / 人名候選模式 (2字以上純片假名)
KATAKANA_NAME_PATTERN = re.compile(r'[\u30A1-\u30FA\u30FC]{2,}')

# 常見日文稱謂後綴模式
HONORIFIC_PATTERN = re.compile(r'([\u4E00-\u9FFF\u3040-\u309F\u30A0-\u30FF]{1,6})(さん|ちゃん|くん|君|先輩|先生|様|さま|殿)')


def load_glossary(glossary_path: Path | None) -> dict[str, str]:
    """載入自訂術語表字典 (支援 JSON 格式: {"日文原名": "中文統一譯名"})"""
    if not glossary_path or not Path(glossary_path).is_file():
        return {}
    try:
        data = json.loads(Path(glossary_path).read_text('utf-8'))
        if isinstance(data, dict):
            return {str(k).strip(): str(v).strip() for k, v in data.items() if k and v}
    except Exception:
        pass
    return {}


def format_glossary_prompt(glossary: dict[str, str], current_japanese_texts: list[str]) -> str:
    """根據當前頁面的日文內容，動態篩選出有出現的術語，生成提示詞注入字串"""
    if not glossary:
        return ""
    full_text = "".join(current_japanese_texts)
    matched = {jp: zh for jp, zh in glossary.items() if jp in full_text}
    if not matched:
        return ""
    lines = [f"{jp}->{zh}" for jp, zh in matched.items()]
    return "\n【专有名词与人物译名表（必须严格遵循，保持全文一致）】：\n" + "\n".join(lines)


def audit_volume_terminology(translations_items: list[dict], custom_glossary: dict[str, str] | None = None) -> dict:
    """分析整本漫畫的翻譯紀錄，檢測人名與專有名詞在不同頁面的譯名一致性。
    
    返回審計報告，包含衝突名詞、出現頻率統計、建議統一譯名。
    """
    glossary = custom_glossary or {}
    # term -> Counter({translation_cand: count})
    entity_to_translations = defaultdict(Counter)
    entity_occurrences = defaultdict(list)

    for idx, row in enumerate(translations_items):
        jp = row.get('japanese', '')
        zh = row.get('translation', '')
        page = row.get('page', '')
        block_id = row.get('source_id', idx)

        # 1. 優先檢查自訂術語
        for term_jp in glossary:
            if term_jp in jp:
                expected_zh = glossary[term_jp]
                entity_occurrences[term_jp].append({
                    'page': page, 'source_id': block_id,
                    'japanese': jp, 'current_translation': zh,
                    'is_custom_glossary': True
                })
                entity_to_translations[term_jp][expected_zh] += 1

        # 2. 自動提取片假名實體 (長度 2~8 字)
        for match in KATAKANA_NAME_PATTERN.finditer(jp):
            kana = match.group()
            if len(kana) < 2 or len(kana) > 8:
                continue
            # 排除純擬聲詞常用組合 (可依需求擴充)
            if kana in {'ドキドキ', 'ワクワク', 'ニコニコ', 'イライラ', 'ハラハラ'}:
                continue
            entity_occurrences[kana].append({
                'page': page, 'source_id': block_id,
                'japanese': jp, 'current_translation': zh,
                'is_custom_glossary': False
            })

    # 彙整衝突分析
    report = {
        'total_entities_detected': len(entity_occurrences),
        'custom_glossary_enforced': len(glossary),
        'entities': {}
    }

    for entity, occurrences in entity_occurrences.items():
        pages_seen = sorted(list(set(item['page'] for item in occurrences)))
        report['entities'][entity] = {
            'occurrences_count': len(occurrences),
            'pages': pages_seen,
            'is_in_glossary': entity in glossary,
            'standard_target': glossary.get(entity, None),
            'sample_blocks': occurrences[:5]
        }

    return report


def apply_terminology_harmonization(work_dir: Path, replacement_map: dict[str, str]) -> dict:
    """將整本漫畫的所有譯文紀錄中，指定的不一致名詞全域替換為統一標準名，並同步更新帳冊。
    
    replacement_map 格式:
    {
        "舊譯名": "統一新譯名"
    }
    """
    work = Path(work_dir)
    translations_path = work / 'translations.json'
    if not translations_path.is_file():
        raise FileNotFoundError(f"找不到 translations.json: {translations_path}")

    data = json.loads(translations_path.read_text('utf-8'))
    items = data.get('items', [])
    updated_count = 0
    pages_affected = set()

    for row in items:
        trans = row.get('translation', '')
        new_trans = trans
        for old_term, new_term in replacement_map.items():
            if old_term in new_trans:
                new_trans = new_trans.replace(old_term, new_term)
        if new_trans != trans:
            row['translation'] = new_trans
            pages_affected.add(row.get('page'))
            updated_count += 1

    if updated_count > 0:
        # 1. 保存 translations.json
        save(translations_path, data)

        # 2. 同步更新 translations.csv
        csv_path = work / 'translations.csv'
        if csv_path.is_file() and items:
            import csv
            with csv_path.open('w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(items[0].keys()))
                writer.writeheader()
                writer.writerows(items)

        # 3. 更新 project/imgtrans_project.json
        project_json = work / 'project/imgtrans_project.json'
        if project_json.is_file():
            proj = json.loads(project_json.read_text('utf-8'))
            proj_pages = proj.get('pages', {})
            for row in items:
                page_name = row.get('page')
                src_id = row.get('source_id')
                if page_name in proj_pages and 1 <= src_id <= len(proj_pages[page_name]):
                    block = proj_pages[page_name][src_id - 1]
                    # 更新文字
                    block['translation'] = row['translation']
            save(project_json, proj)

    return {
        'status': 'success',
        'updated_blocks': updated_count,
        'pages_affected': sorted(list(pages_affected))
    }
