"""Translate an explicit page list locally, preserving original files and raw model output."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import threading

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / 'tools/magi-pilot'))
from run_context_probe import LocalServer, TRANSLATE_SYSTEM, chat
from opencc import OpenCC
from PIL import Image

from model_identity import model_manifest_digest
MODEL_DIGEST = model_manifest_digest()
EVENT_CONTEXT = threading.local()
PRINT_LOCK = threading.Lock()


def read(path: Path) -> dict:
    return json.loads(path.read_text('utf-8'))


def save(path: Path, value) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    temporary.replace(path)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def emit(event: str, **data) -> None:
    with PRINT_LOCK:
        print(json.dumps({'event': event, **getattr(EVENT_CONTEXT, 'data', {}), **data}, ensure_ascii=False), flush=True)


def check_cancel(cancel_file: Path | None) -> None:
    if cancel_file is not None and cancel_file.exists():
        raise InterruptedError('Translation cancelled by the user')


def run_stage(command: list[str], audit: Path, label: str, timeout: int = 900,
              cancel_file: Path | None = None, cpu_threads: int | None = None) -> None:
    check_cancel(cancel_file)
    started = time.monotonic()
    emit('stage_started', stage=label)
    with (audit / (label + '.log')).open('w', encoding='utf-8') as log:
        env = os.environ.copy()
        if cpu_threads is not None:
            for key in ('MANGA_CPU_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
                env[key] = str(cpu_threads)
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE, stdout=log, env=env,
            stderr=subprocess.STDOUT, text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
        save(audit / (label + '-process.json'), {'pid': process.pid, 'command': command})
        try:
            process.stdin.write('exit\n')
            process.stdin.flush()
            process.stdin.close()
            while process.poll() is None:
                check_cancel(cancel_file)
                if time.monotonic() - started > timeout:
                    raise TimeoutError(label + ' exceeded its time limit')
                time.sleep(0.2)
        except BaseException:
            if process.poll() is None:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                process.wait(timeout=10)
            raise
    result = {'returncode': process.returncode, 'elapsed_seconds': time.monotonic() - started}
    save(audit / (label + '-run.json'), result)
    emit('stage_finished', stage=label, **result)
    if process.returncode:
        raise RuntimeError(label + ' failed; see ' + str(audit / (label + '.log')))


def area(box: list[float]) -> float:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def ordered_ocr(blocks: list[dict], prediction: dict) -> tuple[list[dict], str]:
    rows = []
    for index, block in enumerate(blocks, 1):
        box = block['xyxy']
        scores = []
        for position, candidate in enumerate(prediction['texts']):
            overlap = area([max(box[0], candidate[0]), max(box[1], candidate[1]),
                            min(box[2], candidate[2]), min(box[3], candidate[3])])
            coverage = overlap / max(1, area(box))
            iou = overlap / max(1, area(box) + area(candidate) - overlap)
            scores.append((0.7 * coverage + 0.3 * iou, coverage, iou, position))
        _, coverage, iou, best = max(scores) if scores else (0, 0, 0, -1)
        matched = coverage >= 0.6 or iou >= 0.3
        rows.append({'id': index, 'japanese': ''.join(block['text']), 'bbox': box,
            'magi_text_id': best + 1 if matched else None,
            'coverage': round(coverage, 6), 'iou': round(iou, 6)})
    if all(row['magi_text_id'] is not None for row in rows):
        rows.sort(key=lambda row: (row['magi_text_id'], -row['bbox'][2], row['bbox'][1]))
        return rows, 'Magi text order; ties use right-to-left then top-to-bottom geometry'
    # Keep every OCR block. A partial match must not silently move unmatched text to the end.
    return rows, 'Native OCR order fallback: at least one OCR block lacks a confident Magi text match'


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    page_args = parser.add_mutually_exclusive_group(required=True)
    page_args.add_argument('--pages', nargs='+')
    page_args.add_argument('--pages-file', type=Path)
    parser.add_argument('--cancel-file', type=Path)
    args = parser.parse_args()
    if args.pages_file:
        args.pages = read(args.pages_file)
    if not isinstance(args.pages, list) or not args.pages or not all(isinstance(name, str) for name in args.pages):
        raise ValueError('Pages must be a nonempty list of filenames')
    source, output = args.source.resolve(), args.output.resolve()
    if output.exists():
        raise FileExistsError('Use a fresh output folder')
    if len(set(args.pages)) != len(args.pages) or any(Path(name).name != name for name in args.pages):
        raise ValueError('Pages must be unique filenames within the source folder')
    if len({Path(name).stem.casefold() for name in args.pages}) != len(args.pages):
        raise ValueError('Duplicate page stems; use the folder translator to normalize filenames safely')
    check_cancel(args.cancel_file)
    originals = {name: sha(source / name) for name in args.pages}
    project = output / 'project'
    audit = output / '_audit'
    project.mkdir(parents=True)
    audit.mkdir()
    for name in args.pages:
        shutil.copy2(source / name, project / name)
    save(audit / 'provenance.json', {'source': str(source), 'source_sha256': originals,
        'pages': args.pages, 'model_digest': MODEL_DIGEST, 'manual_semantic_edits': False,
        'scene_context': False, 'added_fidelity_prompt': False, 'script_sha256': sha(Path(__file__))})
    launcher = ROOT / 'tools/local-manga-translation/launch_ballons_sakura.py'
    project_file = project / 'imgtrans_project.json'
    base_command = [sys.executable, '-B', '-X', 'utf8', str(launcher), '--headless',
        '--exec_dirs', str(project), '--ldpi', '96', '--export-source-txt', '--export-translation-txt']
    config = read(HERE / 'ballons-sakura-config.json')
    config['module']['enable_translate'] = False
    config['module']['llm_translate_vision'] = False
    config['module']['llm_translate_summary_memory'] = False
    prepare_config = audit / 'prepare-config.json'
    save(prepare_config, config)
    started = time.monotonic()
    completed = False
    try:
        run_stage(base_command + ['--config', str(prepare_config)], audit, 'detect-ocr-inpaint',
                  timeout=max(900, len(args.pages) * 90), cancel_file=args.cancel_file)
        doc = read(project_file)
        assert set(doc['pages']) == set(args.pages)
        for name in args.pages:
            if not doc['pages'][name]:
                emit('no_text_detected', page=name)
                continue
            for folder in ('inpainted', 'mask'):
                if not (project / folder / (Path(name).stem + '.png')).is_file():
                    raise FileNotFoundError('Missing prepared image: ' + name + '/' + folder)
        shutil.copy2(project_file, audit / 'machine-ocr-project.json')
        text_pages = [name for name in args.pages if doc['pages'][name]]
        save(audit / 'text-pages.json', text_pages)
        if text_pages:
            run_stage([str(ROOT / 'tools/magi-pilot/.venv/Scripts/python.exe'), '-B', '-X', 'utf8',
                str(ROOT / 'tools/magi-pilot/run_probe.py'), '--source', str(project),
                '--pages-file', str(audit / 'text-pages.json'),
                '--output', str(audit / 'magi'), '--device', 'cpu', '--threads', '4'], audit, 'magi',
                timeout=max(900, len(text_pages) * 30), cancel_file=args.cancel_file)
            magi_run = read(audit / 'magi/run.json')
            assert magi_run['success']
        order = {}
        for name in args.pages:
            if name not in text_pages:
                order[name] = {'method': 'No OCR text detected; retain source pixels', 'rows': []}
                continue
            assert magi_run['pages'][name]['source_sha256'] == originals[name]
            rows, method = ordered_ocr(doc['pages'][name], read(audit / 'magi' / (Path(name).stem + '.json')))
            order[name] = {'method': method, 'rows': rows}
            emit('reading_order_ready', page=name, blocks=len(rows), method=method)
        save(audit / 'reading-order.json', order)
        translation_dir = audit / 'translation'
        translation_dir.mkdir()
        converter = OpenCC('s2tw')
        translated = []
        from contextlib import nullcontext
        check_cancel(args.cancel_file)
        with (LocalServer(11436, HERE / 'ollama-models', 'sukinishiro:latest', translation_dir)
              if text_pages else nullcontext()) as server:
            if text_pages:
                assert read(translation_dir / 'server.json')['model_manifest_sha256'] == MODEL_DIGEST
            for page_position, name in enumerate(args.pages, 1):
                check_cancel(args.cancel_file)
                rows = order[name]['rows']
                if not rows:
                    emit('page_translation_finished', page=name, current=page_position, total=len(args.pages), blocks=0)
                    continue
                original_text = '\n'.join('「' + row['japanese'] + '」' for row in rows)
                payload = {'model': server.model, 'stream': True, 'keep_alive': '5m',
                    'messages': [{'role': 'system', 'content': TRANSLATE_SYSTEM},
                                 {'role': 'user', 'content': '将下面的日文文本翻译成中文：' + original_text}],
                    'options': {'num_ctx': 8192, 'num_predict': 2048, 'seed': 20261001,
                        'temperature': 0.1, 'top_p': 0.3, 'repeat_penalty': 1.0, 'frequency_penalty': 0.05}}
                result = chat(server, payload, Path(name).stem, translation_dir, cancel_file=args.cancel_file,
                              expected_lines=len(rows))
                lines = [line.strip().strip('「」') for line in result['content'].strip().splitlines() if line.strip()]
                if len(lines) != len(rows):
                    raise ValueError('Translation line alignment failed: ' + name)
                for position, (row, line) in enumerate(zip(rows, lines), 1):
                    text = converter.convert(line)
                    block = doc['pages'][name][row['id'] - 1]
                    block.update(translation=text, rich_text='')
                    font_size = block['fontformat']['font_size']
                    translated.append({'page': name, 'source_id': row['id'], 'reading_order': position,
                        'japanese': row['japanese'], 'translation': text, 'font_size': font_size,
                        'small_text_review': font_size < 12})
                save(output / 'translations.json', {'policy': 'Exact local model output plus OpenCC s2tw; no semantic edits',
                    'items': translated})
                emit('page_translation_finished', page=name, current=page_position, total=len(args.pages), blocks=len(rows))
        save(output / 'translations.json', {'policy': 'Exact local model output plus OpenCC s2tw; no semantic edits',
                                           'items': translated})
        save(project_file, doc)
        for stage in ('detect', 'ocr', 'translate', 'inpaint'):
            config['module']['enable_' + stage] = False
        config['let_autolayout_flag'] = True
        render_config = audit / 'render-config.json'
        save(render_config, config)
        run_stage(base_command + ['--batch-autolayout', '--config', str(render_config)], audit, 'render',
                  timeout=max(900, len(args.pages) * 30), cancel_file=args.cancel_file)
        rendered_doc = read(project_file)
        for row in translated:
            block = rendered_doc['pages'][row['page']][row['source_id'] - 1]
            actual = block['translation']
            assert actual.replace('\n', '').replace('\r', '') == row['translation']
            row.update(font_size=block['fontformat']['font_size'],
                       small_text_review=block['fontformat']['font_size'] < 12)
        save(output / 'translations.json', {'policy': 'Exact local model output plus OpenCC s2tw; no semantic edits',
                                           'items': translated})
        images = []
        for name in args.pages:
            image_path = project / 'result' / (Path(name).stem + '.png')
            if not doc['pages'][name]:
                image_path.parent.mkdir(exist_ok=True)
                with Image.open(project / name) as original:
                    original.save(image_path, format='PNG')
            with Image.open(image_path) as image, Image.open(source / name) as original:
                assert image.size == original.size
                images.append({'page': name, 'path': str(image_path), 'size': list(image.size), 'sha256': sha(image_path)})
        with (output / 'translations.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=['page', 'source_id', 'reading_order', 'japanese',
                'translation', 'font_size', 'small_text_review'])
            writer.writeheader()
            writer.writerows(translated)
        verification = {'pages': len(args.pages), 'blocks': len(translated), 'images': images,
            'model_output_preserved': True, 'human_semantic_or_punctuation_edits': False,
            'small_text_blocks': [row for row in translated if row['small_text_review']],
            'reading_order': {name: data['method'] for name, data in order.items()},
            'no_text_detected_pages': [name for name in args.pages if not doc['pages'][name]],
            'elapsed_seconds': time.monotonic() - started, 'visual_review_pending': True}
        if (project / 'layout-guard.json').is_file():
            verification['layout_guard'] = read(project / 'layout-guard.json')
            verification['layout_review_blocks'] = [dict(page=name, **row)
                for name, rows in verification['layout_guard']['pages'].items() for row in rows
                if row['status'] in ('needs_review', 'unverified')]
        save(output / 'verification.json', verification)
        completed = True
        emit('batch_completed', pages=len(args.pages), blocks=len(translated), output=str(output))
    finally:
        unchanged = all(sha(source / name) == expected and sha(project / name) == expected
                        for name, expected in originals.items())
        save(audit / 'source-integrity.json', {'source_and_project_images_unchanged': unchanged,
             'pipeline_completed': completed, 'source_sha256': originals})
        assert unchanged


if __name__ == '__main__':
    main()
