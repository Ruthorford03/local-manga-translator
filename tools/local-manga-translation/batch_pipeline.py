"""Resumable preparation and rendering phases shared by the local batch schedulers."""
from contextlib import nullcontext
from copy import deepcopy
import csv
import gc
from pathlib import Path
import shutil
import sys
import time
import uuid

from PIL import Image
from opencc import OpenCC

from run_magi_batch import (HERE, ROOT, MODEL_DIGEST, LocalServer, TRANSLATE_SYSTEM,
                            chat, read, save, sha, emit, check_cancel, run_stage,
                            ordered_ocr)
from run_context_probe import _reply_valid, IncompleteResponse
from source_layout import write_render_request
from sfx_review import register_review, verify_completed_review
from translation_recovery import recover_page

MODEL = 'sukinishiro:latest'
POLICY = 'Exact local model output plus OpenCC s2tw; no semantic edits'
FIELDS = ['page', 'source_id', 'reading_order', 'japanese', 'translation', 'font_size', 'small_text_review']


def prepare_config():
    config = read(HERE / 'ballons-sakura-config.json')
    config['module'].update(enable_translate=False, llm_translate_vision=False, llm_translate_summary_memory=False)
    return config


def compatible_legacy_config(actual):
    expected = prepare_config()
    # Ballons saves its UI defaults into --config on exit. Disabled LLM profile
    # selections are not preparation inputs; compare the requested active settings.
    expected['module'].pop('translator_llm_id', None)
    expected['module'].pop('llm_profiles', None)

    def contains(settings, recorded):
        if isinstance(settings, dict):
            return isinstance(recorded, dict) and all(key in recorded and contains(value, recorded[key])
                                                      for key, value in settings.items())
        return settings == recorded

    return contains(expected, actual)


def profile():
    return {'version': 1, 'config': sha(HERE / 'ballons-sakura-config.json'),
            'magi_runner': sha(ROOT / 'tools/magi-pilot/run_probe.py'),
            'layout': sha(HERE / 'sakura_layout.py'), 'bubble_guard': sha(HERE / 'bubble_guard.py'), 'model_digest': MODEL_DIGEST,
            'ocr_crop_review': sha(HERE / 'ocr_crop_review.py'),
            'source_direction': sha(HERE / 'source_direction.py'), 'source_layout': sha(HERE / 'source_layout.py'),
            'translation_system': TRANSLATE_SYSTEM, 'normalization': 's2tw'}


def command(project):
    return [sys.executable, '-B', '-X', 'utf8', str(HERE / 'launch_ballons_sakura.py'),
            '--headless', '--exec_dirs', str(project), '--ldpi', '96',
            '--export-source-txt', '--export-translation-txt']


def safe_path(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Artifact path escapes its work directory')
    return path


def validate_prepared(source, pages, output):
    marker = read(output / '_audit/prepared.json')
    if marker['pages'] != pages or marker['profile'] != profile():
        raise ValueError('Saved preparation uses different pages or settings; start a new job')
    for name, digest in marker['sources'].items():
        if sha(source / name) != digest:
            raise ValueError('Source changed since preparation: ' + name)
    for relative, digest in marker['artifacts'].items():
        if sha(safe_path(output, relative)) != digest:
            raise ValueError('Saved preparation changed: ' + relative)
    return marker


def seal_prepared(source, pages, output):
    audit, project = output / '_audit', output / 'project'
    artifacts = [audit / 'machine-ocr-project.json', audit / 'reading-order.json', audit / 'prepare-config.json']
    for name in ('machine-ocr-original.json', 'reading-order-original.json', 'ocr-crop-review.json',
                 'ocr-crop-review-run.json'):
        if (audit / name).is_file():
            artifacts.append(audit / name)
    order = read(audit / 'reading-order.json')
    for name in pages:
        artifacts.append(project / name)
        if order[name]['rows']:
            artifacts += [project / directory / (Path(name).stem + '.png') for directory in ('mask', 'inpainted')]
            artifacts.append(audit / 'magi' / (Path(name).stem + '.json'))
    marker = {'pages': pages, 'profile': profile(), 'sources': {name: sha(source / name) for name in pages},
              'artifacts': {str(path.relative_to(output)): sha(path) for path in artifacts}}
    save(audit / 'prepared.json', marker)
    return marker


def import_legacy(source, pages, output, legacy):
    """Copy a verified subset of a v0.2 run; never update the old project or replies."""
    old = legacy / '_audit'
    provenance = read(old / 'provenance.json')
    if provenance['model_digest'] != MODEL_DIGEST or not compatible_legacy_config(read(old / 'prepare-config.json')):
        raise ValueError('Earlier job used different model or preparation settings')
    for stage in ('detect-ocr-inpaint', 'magi'):
        if read(old / (stage + '-run.json'))['returncode'] != 0:
            raise ValueError('Earlier preparation did not finish')
    doc, magi, order = read(old / 'machine-ocr-project.json'), read(old / 'magi/run.json'), read(old / 'reading-order.json')
    if not magi['success']:
        raise ValueError('Earlier Magi preparation did not finish')
    for name in pages:
        digest = sha(source / name)
        if provenance['source_sha256'].get(name) != digest or sha(legacy / 'project' / name) != digest:
            raise ValueError('Earlier preparation source mismatch: ' + name)
        if doc['pages'][name]:
            if magi['pages'][name]['source_sha256'] != digest:
                raise ValueError('Earlier Magi source mismatch: ' + name)
            prediction = read(old / 'magi' / (Path(name).stem + '.json'))
            rows, method = ordered_ocr(doc['pages'][name], prediction)
            if order[name] != {'method': method, 'rows': rows}:
                raise ValueError('Earlier reading order mismatch: ' + name)
    project, audit = output / 'project', output / '_audit'
    doc['pages'] = {name: doc['pages'][name] for name in pages}
    doc['directory'] = str(project)
    doc['current_img'] = pages[0]
    if isinstance(doc.get('image_info'), dict):
        doc['image_info'] = {name: doc['image_info'][name] for name in pages if name in doc['image_info']}
    save(audit / 'machine-ocr-project.json', doc)
    save(project / 'imgtrans_project.json', doc)
    save(audit / 'reading-order.json', {name: order[name] for name in pages})
    save(audit / 'prepare-config.json', prepare_config())
    (audit / 'magi').mkdir()
    for name in pages:
        stem = Path(name).stem
        if doc['pages'][name]:
            for directory in ('mask', 'inpainted'):
                (project / directory).mkdir(exist_ok=True)
                shutil.copy2(legacy / 'project' / directory / (stem + '.png'), project / directory / (stem + '.png'))
            shutil.copy2(old / 'magi' / (stem + '.json'), audit / 'magi' / (stem + '.json'))
    save(audit / 'legacy-import.json', {'from': str(legacy), 'pages': pages, 'read_only_source': True,
                                     'response_cache': str(old / 'translation')})
    emit('preparation_reused', pages=len(pages), legacy=True)


def review_ocr(output, cancel_file=None, cpu_threads=4):
    """Review source text in a CPU child without invoking inpaint or layout."""
    audit, project = output / '_audit', output / 'project'
    module = read(audit / 'prepare-config.json')['module']
    if module.get('ocr') != 'manga_ocr' or module.get('enable_ocr') is not True:
        save(audit / 'ocr-crop-review.json', {'success': True, 'text_only': True,
             'padding_per_side_px': 2, 'corrected': 0, 'needs_review': 0,
             'skipped': 'source_manga_ocr_not_enabled'})
        emit('ocr_review_skipped', reason='source_manga_ocr_not_enabled')
        return
    run_stage([sys.executable, '-B', '-X', 'utf8', str(HERE / 'ocr_crop_review.py'),
               '--project', str(project), '--audit', str(audit)], audit, 'ocr-crop-review',
              max(900, len(read(audit / 'machine-ocr-project.json')['pages']) * 30), cancel_file, cpu_threads)
    review = read(audit / 'ocr-crop-review.json')
    if not review.get('success') or not review.get('text_only') or review.get('padding_per_side_px') != 2:
        raise ValueError('Source OCR crop review did not complete with the expected policy')
    emit('ocr_review_ready', corrected=review['corrected'], needs_review=review['needs_review'])


def prepare(source, pages, output, cancel_file=None, cpu_threads=4, legacy=None):
    check_cancel(cancel_file)
    if (output / '_audit/prepared.json').is_file():
        validate_prepared(source, pages, output)
        emit('preparation_reused', pages=len(pages), legacy=False)
        return output
    if output.exists():
        raise FileExistsError('Incomplete preparation needs a fresh attempt directory')
    project, audit = output / 'project', output / '_audit'
    project.mkdir(parents=True)
    audit.mkdir()
    originals = {name: sha(source / name) for name in pages}
    for name in pages:
        check_cancel(cancel_file)
        shutil.copy2(source / name, project / name)
    save(audit / 'provenance.json', {'source': str(source), 'source_sha256': originals, 'pages': pages,
        'model_digest': MODEL_DIGEST, 'manual_semantic_edits': False, 'scene_context': False, 'added_fidelity_prompt': False})
    if legacy is not None:
        import_legacy(source, pages, output, legacy)
    else:
        config_path = audit / 'prepare-config.json'
        save(config_path, prepare_config())
        run_stage(command(project) + ['--config', str(config_path)], audit, 'detect-ocr-inpaint',
                  max(900, len(pages) * 90), cancel_file, cpu_threads)
        doc = read(project / 'imgtrans_project.json')
        if set(doc['pages']) != set(pages):
            raise ValueError('OCR page list does not match requested pages')
        save(audit / 'machine-ocr-project.json', doc)
        text_pages = [name for name in pages if doc['pages'][name]]
        save(audit / 'text-pages.json', text_pages)
        if text_pages:
            run_stage([str(ROOT / 'tools/magi-pilot/.venv/Scripts/python.exe'), '-B', '-X', 'utf8',
                str(ROOT / 'tools/magi-pilot/run_probe.py'), '--source', str(project),
                '--pages-file', str(audit / 'text-pages.json'), '--output', str(audit / 'magi'),
                '--device', 'cpu', '--threads', str(cpu_threads)], audit, 'magi',
                max(900, len(pages) * 30), cancel_file, cpu_threads)
            magi = read(audit / 'magi/run.json')
            if not magi['success']:
                raise RuntimeError('Magi preparation failed')
        order = {}
        for name in pages:
            if not doc['pages'][name]:
                order[name] = {'method': 'No OCR text detected; retain source pixels', 'rows': []}
            else:
                if magi['pages'][name]['source_sha256'] != originals[name]:
                    raise ValueError('Magi source mismatch: ' + name)
                rows, method = ordered_ocr(doc['pages'][name], read(audit / 'magi' / (Path(name).stem + '.json')))
                order[name] = {'method': method, 'rows': rows}
                emit('reading_order_ready', page=name, blocks=len(rows), method=method)
        save(audit / 'reading-order.json', order)
    # Apply the same source-only review to both new OCR and imported work copies.
    # Keep the original OCR/order as evidence; the reviewer never changes their geometry.
    review_ocr(output, cancel_file, cpu_threads)
    marker = seal_prepared(source, pages, output)
    if marker['sources'] != originals:
        raise ValueError('Source changed during preparation')
    emit('preparation_completed', pages=len(pages), output=str(output))
    return output


def payload_for(model, rows):
    original_text = '\n'.join('「' + row['japanese'] + '」' for row in rows)
    return {'model': model, 'stream': True, 'keep_alive': '60m',
            'messages': [{'role': 'system', 'content': TRANSLATE_SYSTEM},
                         {'role': 'user', 'content': '将下面的日文文本翻译成中文：' + original_text}],
            'options': {'num_ctx': 8192, 'num_predict': 2048, 'seed': 20261001,
                        'temperature': 0.1, 'top_p': 0.3, 'repeat_penalty': 1.0, 'frequency_penalty': 0.05}}


def response_cache(translation_dir, legacy, label, payload, expected_lines):
    """Current work stays strict; imported replies must still match reviewed OCR."""
    if (translation_dir / (label + '-response.json')).exists():
        return translation_dir
    if legacy is None:
        return None
    candidate = Path(legacy)
    try:
        if (read(candidate / (label + '-request.json')) == payload and
                read(candidate / 'server.json')['model_manifest_sha256'] == MODEL_DIGEST and
                _reply_valid(read(candidate / (label + '-response.json')), expected_lines)):
            return candidate
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def partial_finish_paths(pages):
    """Only mutable checkpoint files; source images and raw replies stay intact."""
    return ['translations.json', 'translations.csv', 'project/imgtrans_project.json',
            'project/layout-guard.json'] + [relative for name in pages for relative in
            ('project/result/' + Path(name).stem + '.png',
             '_audit/translation/' + Path(name).stem + '-block-recovery.json')]


def begin_partial_finish(output, pages):
    """Save a durable rollback point before extending an existing valid batch."""
    identifier = uuid.uuid4().hex
    snapshot = output / '_audit/finish-checkpoints' / identifier
    snapshot.mkdir(parents=True)
    files = {}
    for relative in partial_finish_paths(pages):
        source = safe_path(output, relative)
        backup = safe_path(snapshot / 'before', relative)
        files[relative] = sha(source) if source.is_file() else None
        if source.is_file():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, backup)
    journal = {'version': 1, 'id': identifier, 'pages': pages, 'files': files,
               'before_verification': sha(output / 'verification.json')}
    save(snapshot / 'journal.json', journal)
    save(output / '_audit/finish-transaction.json', journal)


def recover_partial_finish(output, pages):
    """Recover interrupted writes before trusting the old verification hashes."""
    marker = output / '_audit/finish-transaction.json'
    if not marker.is_file():
        return
    journal = read(marker)
    selected = journal.get('pages', [])
    identifier = journal.get('id', '')
    if (journal.get('version') != 1 or uuid.UUID(identifier).hex != identifier
            or not selected or len(set(selected)) != len(selected) or not set(selected).issubset(pages)
            or set(journal.get('files', {})) != set(partial_finish_paths(selected))):
        raise ValueError('Invalid partial finish checkpoint')
    snapshot = output / '_audit/finish-checkpoints' / identifier
    current = sha(output / 'verification.json')
    if current == journal.get('next_verification'):
        save(snapshot / 'outcome.json', {'status': 'committed'})
        marker.unlink()
        return
    if current != journal['before_verification']:
        raise ValueError('Interrupted finish verification was modified')
    # Validate every backup before restoring any file. A repeated interruption
    # safely repeats these exact replacements and preserves original mtimes.
    for relative, digest in journal['files'].items():
        if digest is not None and sha(safe_path(snapshot / 'before', relative)) != digest:
            raise ValueError('Partial finish backup was modified: ' + relative)
    for relative, digest in journal['files'].items():
        target = safe_path(output, relative)
        if target.is_file() and (digest is None or sha(target) != digest):
            abandoned = safe_path(snapshot / 'uncommitted', relative)
            if not abandoned.exists():
                abandoned.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, abandoned)
        if digest is None:
            target.unlink(missing_ok=True)
        else:
            temporary = target.with_name(target.name + '.finish-restore.tmp')
            shutil.copy2(safe_path(snapshot / 'before', relative), temporary)
            temporary.replace(target)
    save(snapshot / 'outcome.json', {'status': 'rolled_back', 'raw_responses_retained': True})
    marker.unlink()


def commit_partial_finish(output, verification):
    marker = output / '_audit/finish-transaction.json'
    if marker.is_file():
        journal = read(marker)
        snapshot = output / '_audit/finish-checkpoints' / journal['id']
        save(snapshot / 'next-verification.json', verification)
        journal['next_verification'] = sha(snapshot / 'next-verification.json')
        save(marker, journal)
    save(output / 'verification.json', verification)


def saved_result(output, pages):
    """Verify saved page checkpoints before retaining any native pixels or text."""
    recover_partial_finish(output, pages)
    if not (output / 'verification.json').is_file():
        return None
    provisional = read(output / 'verification.json')
    registering = provisional.get('review_registration_pending', [])
    if (not isinstance(registering, list) or len(set(registering)) != len(registering)
            or not set(registering).issubset(pages)):
        raise ValueError('Invalid pending review registration checkpoint')
    # A crash between rendering a draft and registering its review must never
    # turn that draft into an ordinary completed page on resume.
    for name in registering:
        register_review(output, name, 'line_alignment_recovery')
    result = verify_completed_review(output)
    complete = result['completed_pages']
    pending = result.get('review_required_pages', {})
    images = [item['page'] for item in result['images']]
    if (len(set(complete)) != len(complete) or len(set(images)) != len(images)
            or set(complete) != set(images) or set(complete) & set(pending)
            or not (set(complete) | set(pending)).issubset(pages)):
        raise ValueError('Saved page checkpoint is inconsistent')
    return result


def unfinished_pages(output, pages):
    result = saved_result(output, pages)
    frozen = set(result['completed_pages']) | set(result.get('review_required_pages', {})) if result else set()
    return [name for name in pages if name not in frozen]


def needs_model(output, pages):
    """A validated cached reply needs only the service identity, not loaded GPU weights."""
    audit = output / '_audit'
    order = read(audit / 'reading-order.json')
    legacy = read(audit / 'legacy-import.json')['response_cache'] if (audit / 'legacy-import.json').exists() else None
    for name in unfinished_pages(output, pages):
        rows = order[name]['rows']
        if not rows:
            continue
        label = Path(name).stem
        cache = response_cache(audit / 'translation', legacy, label, payload_for(MODEL, rows), len(rows))
        if cache is None:
            return True
        try:
            if (read(cache / 'server.json')['model_manifest_sha256'] != MODEL_DIGEST or
                    read(cache / (label + '-request.json')) != payload_for(MODEL, rows) or
                    not _reply_valid(read(cache / (label + '-response.json')), len(rows))):
                return True
        except (OSError, ValueError, KeyError):
            return True
    return False


def finish(source, pages, output, cancel_file=None, cpu_threads=4, server=None, continue_errors=True):
    started = time.monotonic()
    check_cancel(cancel_file)
    marker = validate_prepared(source, pages, output)
    audit, project = output / '_audit', output / 'project'
    previous = saved_result(output, pages)
    frozen = set(previous['completed_pages']) | set(previous.get('review_required_pages', {})) if previous else set()
    fresh_pages = [name for name in pages if name not in frozen]
    if not fresh_pages:
        check_cancel(cancel_file)
        emit('batch_completed', pages=len(previous['completed_pages']), failed_pages=len(previous['failed_pages']),
             blocks=previous['blocks'], output=str(output), reused=True)
        return previous
    doc, order = read(audit / 'machine-ocr-project.json'), read(audit / 'reading-order.json')
    if frozen:
        retained = read(project / 'imgtrans_project.json')
        for name in frozen:
            doc['pages'][name] = deepcopy(retained['pages'][name])
    translation_dir = audit / 'translation'
    translation_dir.mkdir(exist_ok=True)
    legacy = read(audit / 'legacy-import.json')['response_cache'] if (audit / 'legacy-import.json').exists() else None
    translated = [row for row in read(output / 'translations.json')['items'] if row['page'] in frozen] if previous else []
    failed = {name: value for name, value in previous['failed_pages'].items() if name in frozen} if previous else {}
    complete = [name for name in pages if previous and name in previous['completed_pages']]
    converter, render_pages, recovered_pages = OpenCC('s2tw'), [], []
    has_text = any(order[name]['rows'] for name in fresh_pages)
    context = nullcontext(server) if server or not has_text else LocalServer(11436, HERE / 'ollama-models', MODEL, translation_dir)
    if previous:
        begin_partial_finish(output, fresh_pages)
    try:
        with context as active:
            if has_text:
                identity = read(active.output / 'server.json')
                if identity['model_manifest_sha256'] != MODEL_DIGEST:
                    raise ValueError('Model identity mismatch')
                if (translation_dir / 'server.json').exists() and read(translation_dir / 'server.json')['model_manifest_sha256'] != MODEL_DIGEST:
                    raise ValueError('Cached response model mismatch')
                save(translation_dir / 'server.json', identity)
            for position, name in enumerate(fresh_pages, 1):
                check_cancel(cancel_file)
                rows = order[name]['rows']
                if not rows:
                    complete.append(name)
                    render_pages.append(name)
                    emit('page_translation_finished', page=name, current=position, total=len(pages), blocks=0)
                    continue
                # A failed page cannot leave half-translated blocks.
                blocks, page_items = deepcopy(doc['pages'][name]), []
                try:
                    label = Path(name).stem
                    cache = response_cache(translation_dir, legacy, label, payload_for(active.model, rows), len(rows))
                    try:
                        result = chat(active, payload_for(active.model, rows), label, translation_dir,
                                      reuse_dir=cache, cancel_file=cancel_file, expected_lines=len(rows))
                    except IncompleteResponse:
                        last = read(translation_dir / (label + '-response.json'))
                        metrics = last.get('metrics', {})
                        if (len(rows) <= 1 or last.get('transport', {}).get('failure_kind') != 'line_alignment'
                                or metrics.get('done') is not True or metrics.get('done_reason') != 'stop'):
                            raise
                        emit('page_block_recovery_started', page=name, blocks=len(rows))
                        result = recover_page(active, rows, label, translation_dir, payload_for=payload_for,
                                              chat=chat, read=read, save=save, cancel_file=cancel_file)
                        recovered_pages.append(name)
                        emit('page_block_recovery_completed', page=name, blocks=len(rows), review_required=True)
                    lines = [line.strip().strip('「」') for line in result['content'].splitlines() if line.strip()]
                    from sakura_layout import fitted_layout
                    img_size = None
                    if 'image_info' in doc and name in doc['image_info'] and 'shape' in doc['image_info'][name]:
                        sh = doc['image_info'][name]['shape']
                        img_size = (sh[1], sh[0])
                    elif (project / name).is_file():
                        from PIL import Image as _PIL_Image
                        with _PIL_Image.open(project / name) as _im:
                            img_size = _im.size
                    for index, (row, line) in enumerate(zip(rows, lines), 1):
                        text, block = converter.convert(line), blocks[row['id'] - 1]
                        fitted = fitted_layout(text, block['xyxy'], block['fontformat']['font_size'], image_size=img_size)
                        if fitted:
                            block['translation'] = fitted['translation']
                            block['fontformat']['font_size'] = fitted['font_size']
                            block['_detected_font_size'] = fitted['font_size']
                            block['font_size'] = fitted['font_size']
                            block['fontformat']['vertical'] = fitted['vertical']
                            block['src_is_vertical'] = fitted['vertical']
                            block['_bounding_rect'] = fitted['rect']
                            if 'letter_spacing' in fitted:
                                block['fontformat']['letter_spacing'] = fitted['letter_spacing']
                            if 'line_spacing' in fitted:
                                block['fontformat']['line_spacing'] = fitted['line_spacing']
                            if 'alignment' in fitted:
                                block['fontformat']['alignment'] = fitted['alignment']
                        else:
                            block.update(translation=text, rich_text='')
                        font_size = block['fontformat']['font_size']
                        page_items.append(dict(page=name, source_id=row['id'], reading_order=index, japanese=row['japanese'],
                                               translation=text, font_size=font_size, small_text_review=font_size < 11))
                except (RuntimeError, ValueError) as error:
                    if not continue_errors:
                        raise
                    failed[name] = {'type': type(error).__name__, 'message': str(error)}
                    emit('page_failed', page=name, message=str(error))
                    continue
                doc['pages'][name] = blocks
                translated.extend(page_items)
                complete.append(name)
                render_pages.append(name)
                save(output / 'translations.json', {'policy': POLICY, 'items': translated})
                save(project / 'imgtrans_project.json', doc)
                emit('page_translation_finished', page=name, current=position, total=len(pages), blocks=len(rows))
        save(output / 'translations.json', {'policy': POLICY, 'items': translated})
        # Render only successful pages. Failed source images remain intact in the project.
        render_doc = deepcopy(doc)
        render_doc['pages'] = {name: doc['pages'][name] for name in render_pages}
        # Native startup materializes the selected page before the batch runs.
        # Drop the OCR-stage selection so native loading starts at its own first
        # page (including blank/failed images still on disk). Otherwise an early
        # save changes a later selected page's derived rectangles and input hash.
        render_doc.pop('current_img', None)
        render_project = project
        if frozen and render_pages:
            # Native startup may enumerate every source image in a directory.
            # Render unfinished pages in their own directory so resumed work
            # cannot materialize, save, or redraw a completed/review-pending page.
            render_project = audit / 'resume-render' / uuid.uuid4().hex / 'project'
            render_project.mkdir(parents=True)
            for name in render_pages:
                shutil.copy2(project / name, render_project / name)
                if order[name]['rows']:
                    for directory in ('mask', 'inpainted'):
                        (render_project / directory).mkdir(exist_ok=True)
                        shutil.copy2(project / directory / (Path(name).stem + '.png'),
                                     render_project / directory / (Path(name).stem + '.png'))
            render_doc['directory'] = str(render_project)
        save(render_project / 'imgtrans_project.json', render_doc)
        write_render_request(render_project, render_doc, order)
        if any(order[name]['rows'] for name in render_pages):
            config = prepare_config()
            for stage in ('detect', 'ocr', 'translate', 'inpaint'):
                config['module']['enable_' + stage] = False
            config['let_autolayout_flag'] = True
            save(audit / 'render-config.json', config)
            run_stage(command(render_project) + ['--batch-autolayout', '--config', str(audit / 'render-config.json')], audit, 'render',
                      max(900, len(pages) * 30), cancel_file, cpu_threads)
            rendered = read(render_project / 'imgtrans_project.json')
            for row in translated:
                if row['page'] not in render_pages:
                    continue
                block = rendered['pages'][row['page']][row['source_id'] - 1]
                actual = block['translation']
                if actual.replace('\n', '').replace('\r', '') != row['translation']:
                    raise ValueError('Rendered text mismatch: ' + row['page'])
                row.update(font_size=block['fontformat']['font_size'],
                           small_text_review=block['fontformat']['font_size'] < 12)
            # Keep the native layout in the editable project as well as the PNG.
            doc['pages'].update(rendered['pages'])
            if 'image_info' in rendered:
                doc.setdefault('image_info', {}).update(rendered['image_info'])
            if render_project != project:
                (project / 'result').mkdir(exist_ok=True)
                for name in render_pages:
                    if order[name]['rows']:
                        shutil.copy2(render_project / 'result' / (Path(name).stem + '.png'),
                                     project / 'result' / (Path(name).stem + '.png'))
                guard_path = render_project / 'layout-guard.json'
                if guard_path.is_file():
                    combined = read(project / 'layout-guard.json') if (project / 'layout-guard.json').is_file() else {'version': 1, 'pages': {}}
                    combined['pages'].update(read(guard_path)['pages'])
                    save(project / 'layout-guard.json', combined)
        save(output / 'translations.json', {'policy': POLICY, 'items': translated})
        images = deepcopy(previous['images']) if previous else []
        for name in pages:
            if name in frozen:
                continue
            image_path = project / 'result' / (Path(name).stem + '.png')
            if not order[name]['rows'] or name in failed:
                image_path.parent.mkdir(exist_ok=True)
                with Image.open(project / name) as image:
                    image.save(image_path, format='PNG')
            if name not in complete:
                continue
            with Image.open(image_path) as image, Image.open(source / name) as original:
                if image.size != original.size:
                    raise ValueError('Rendered image size mismatch: ' + name)
                images.append({'page': name, 'path': str(image_path), 'size': list(image.size), 'sha256': sha(image_path)})
        # Preserve all source pages for the editor, with original OCR on failed pages.
        save(project / 'imgtrans_project.json', doc)
        with (output / 'translations.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(translated)
        verification = {'pages': len(pages), 'blocks': len(translated), 'images': images,
            'completed_pages': complete, 'failed_pages': failed, 'model_output_preserved': True,
            'human_semantic_or_punctuation_edits': False,
            'small_text_blocks': [row for row in translated if row['small_text_review']],
            'no_text_detected_pages': [name for name in complete if not order[name]['rows']],
            'reading_order': {name: item['method'] for name, item in order.items()},
            'translation_sha256': sha(output / 'translations.json'),
            'elapsed_seconds': time.monotonic() - started, 'visual_review_pending': True}
        registering = [name for name in recovered_pages if name in complete]
        if registering:
            verification['review_registration_pending'] = registering
        if previous:
            for key in ('review_required_pages', 'review_receipts', 'reviewed_projects', 'preserved_source_regions'):
                if key in previous:
                    verification[key] = deepcopy(previous[key])
        if (project / 'layout-guard.json').is_file():
            verification['layout_guard'] = read(project / 'layout-guard.json')
            records = [dict(page=name, **record) for name, rows in verification['layout_guard']['pages'].items() for record in rows]
            verification['layout_review_blocks'] = [row for row in records if row['status'] in ('needs_review', 'unverified')]
            emit('layout_checked', adjusted=sum(row['status'] == 'adjusted' for row in records),
                 needs_review=sum(row['status'] == 'needs_review' for row in records),
                 unverified=sum(row['status'] == 'unverified' for row in records))
        commit_partial_finish(output, verification)
        recover_partial_finish(output, pages)
        for name in recovered_pages:
            if name in complete:
                register_review(output, name, 'line_alignment_recovery', cancel_file=cancel_file)
                emit('page_review_required', page=name, reason='逐框補救完成；需審查問題區後才輸出')
        verification = verify_completed_review(output)
        emit('batch_completed', pages=len(verification['completed_pages']), failed_pages=len(verification['failed_pages']),
             blocks=len(translated), output=str(output))
        return verification
    finally:
        recover_partial_finish(output, pages)
        unchanged = all(sha(source / name) == digest and sha(project / name) == digest for name, digest in marker['sources'].items())
        save(audit / 'source-integrity.json', {'source_and_project_images_unchanged': unchanged, 'source_sha256': marker['sources']})
        gc.collect()
        if not unchanged:
            raise ValueError('Source image changed during translation')
