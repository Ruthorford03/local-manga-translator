"""One CPU preparation worker plus one local GPU translator, with bounded prefetch."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import ctypes
import json
from pathlib import Path
import threading
import time
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_pipeline as pipeline
from run_magi_batch import HERE, EVENT_CONTEXT, LocalServer, read, save, sha, emit, check_cancel
from run_context_probe import SESSION
import run_context_probe as model_runtime
from sfx_review import verify_completed_review

# 統一記憶體動態安全水位線（6.5 GiB）：
# Sakura 約佔 3.5 GiB，CPU 消字/Magi 約佔 1.5 GiB。
# 當可用記憶體 >= 6.5 GiB 時，Sakura 模型溫熱常駐在顯存中，避免每批反覆卸載加載；
# 若可用記憶體低於 6.5 GiB，自動觸發保險卸載避險。
MEMORY_SAFETY_THRESHOLD = int(6.5 * (1024 ** 3))


def available_memory():
    class MemoryStatus(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in ('total', 'available', 'page_total', 'page_available', 'virtual_total', 'virtual_available', 'extended')]
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError()
    return status.available


class CancelToken:
    def __init__(self, path):
        self.path, self.stop = path, threading.Event()

    def exists(self):
        return self.stop.is_set() or (self.path is not None and self.path.exists())


def checked_images(chunk):
    verification = verify_completed_review(chunk)
    if verification.get('translation_sha256') and sha(chunk / 'translations.json') != verification['translation_sha256']:
        raise ValueError('Completed translation record changed; preserve edits and start a new job')
    for item in verification['images']:
        path = Path(item['path']).resolve()
        if not path.is_relative_to(chunk.resolve()) or sha(path) != item['sha256']:
            raise ValueError('Completed image changed; preserve edited work and start a new job')
    return verification


def run(source, pages, output, cancel_file=None, resume=False, batch_size=12, cpu_threads=4, legacy=None,
        prefetch_threads=2):
    source, output = source.resolve(), output.resolve()
    if not pages or len(set(pages)) != len(pages) or any(Path(name).name != name for name in pages):
        raise ValueError('Pages must be unique filenames inside source')
    if len({Path(name).stem.casefold() for name in pages}) != len(pages):
        raise ValueError('Duplicate page stems; use the folder translator')
    token = CancelToken(cancel_file)
    check_cancel(token)
    identity = {'pages': pages, 'sources': {name: sha(source / name) for name in pages}, 'profile': pipeline.profile()}
    if output.exists():
        if not resume:
            raise FileExistsError('Use a fresh output folder or --resume')
        state = read(output / 'schedule.json')
        if state['identity'] != identity:
            raise ValueError('Sources or settings changed; start a new job')
        batch_size = state['batch_size']
    else:
        output.mkdir(parents=True)
        first_size = min(4, batch_size, len(pages))
        groups = [pages[:first_size]] + [pages[index:index + batch_size] for index in range(first_size, len(pages), batch_size)]
        state = {'identity': identity, 'batch_size': batch_size, 'first_batch_size': first_size, 'cpu_threads': cpu_threads,
                 'prefetch_threads': min(cpu_threads, prefetch_threads),
                 'prefetch_limit': 1, 'chunks': [dict(id=index, pages=group, attempts=[])
                                               for index, group in enumerate(groups, 1)]}
        save(output / 'schedule.json', state)
    completed, failures = set(), {}
    pending = []
    for chunk in state['chunks']:
        if chunk.get('status') == 'completed':
            location = pipeline.safe_path(output, chunk['attempts'][-1])
            pipeline.validate_prepared(source, chunk['pages'], location)
            verification = checked_images(location)
            if set(verification['completed_pages']) != set(chunk['pages']) or verification['failed_pages']:
                raise ValueError('Completed batch checkpoint is inconsistent')
            completed.update(verification['completed_pages'])
            emit('chunk_ready', batch=chunk['id'], output=str(location), reused=True)
        else:
            # Preserve incomplete attempts. Only a sealed preparation can be resumed.
            previous = pipeline.safe_path(output, chunk['attempts'][-1]) if chunk['attempts'] else None
            if previous is None or (previous.exists() and not (previous / '_audit/prepared.json').exists()):
                chunk['attempts'].append(f"batch-{chunk['id']:04d}/attempt-{len(chunk['attempts']) + 1:03d}")
            pending.append(chunk)
    if resume:
        state.setdefault('previous_runs', []).append({key: state[key] for key in
            ('status', 'elapsed_seconds', 'error_type', 'error', 'cpu_threads', 'prefetch_threads') if key in state})
    for key in ('error_type', 'error', 'elapsed_seconds'):
        state.pop(key, None)
    state.update(status='running', completed_pages=sorted(completed), failed_pages={},
                 cpu_threads=cpu_threads, prefetch_threads=min(cpu_threads, prefetch_threads))
    save(output / 'schedule.json', state)
    started = time.monotonic()
    previous_model_emit = model_runtime.emit
    model_runtime.emit = emit
    samples, stop_monitor = [], threading.Event()

    def monitor():
        while not stop_monitor.is_set():
            samples.append({'elapsed_seconds': round(time.monotonic() - started, 2), 'available_bytes': available_memory()})
            stop_monitor.wait(1)

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    server, loaded, future = None, False, None

    def prepare_chunk(chunk, threads):
        EVENT_CONTEXT.data = {'worker': 'cpu', 'batch': chunk['id']}
        emit('cpu_batch_started', pages=len(chunk['pages']), cpu_threads=threads)
        result = pipeline.prepare(source, chunk['pages'], pipeline.safe_path(output, chunk['attempts'][-1]),
                                  token, threads, legacy)
        emit('cpu_batch_finished', pages=len(chunk['pages']))
        return result

    def load_model():
        check_cancel(token)
        emit('model_loading')
        response = SESSION.post(server.base + '/api/generate',
                                json={'model': server.model, 'stream': False, 'keep_alive': '60m',
                                      'options': {'num_ctx': 8192}}, timeout=(5, 180))
        response.raise_for_status()
        if response.json().get('done') is not True:
            raise RuntimeError('Local model did not finish loading')
        check_cancel(token)

    def unload_model():
        response = SESSION.post(server.base + '/api/generate', json={'model': server.model, 'keep_alive': 0}, timeout=(5, 30))
        response.raise_for_status()

    try:
        # Keep the service alive until the CPU worker has exited, including cancellation.
        with ExitStack() as owned:
            pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='manga-prepare')
            try:
                for index, chunk in enumerate(pending):
                    check_cancel(token)
                    if future is not None:
                        location = future.result()
                        future = None
                    else:
                        free = available_memory()
                        # 動態安全水位保護：僅在可用記憶體低於 6.5 GiB 時才主動卸載模型避險；
                        # 記憶體充足時保持溫熱常駐，避免每批反覆卸載加載造成卡頓。
                        if loaded and free < MEMORY_SAFETY_THRESHOLD:
                            emit('model_unloaded_for_safety', available_bytes=free, threshold=MEMORY_SAFETY_THRESHOLD)
                            unload_model()
                            loaded = False
                        location = pool.submit(prepare_chunk, chunk, cpu_threads).result()
                    EVENT_CONTEXT.data = {'worker': 'translate', 'batch': chunk['id']}
                    order = read(location / '_audit/reading-order.json')
                    unresolved = pipeline.unfinished_pages(location, chunk['pages'])
                    if any(order[name]['rows'] for name in unresolved):
                        if server is None:
                            service_dir = output / 'service' / uuid.uuid4().hex
                            service_dir.mkdir(parents=True)
                            state.setdefault('service_runs', []).append(str(service_dir.relative_to(output)))
                            server = owned.enter_context(LocalServer(11436, HERE / 'ollama-models', pipeline.MODEL, service_dir))
                        if not loaded and pipeline.needs_model(location, chunk['pages']):
                            load_model()
                            loaded = True
                    if index + 1 < len(pending):
                        free = available_memory()
                        # OCR/Magi 約需 1.5 GiB。若模型常駐中，需保留安全水位門檻；若未常駐則需 4 GiB。
                        required_free = MEMORY_SAFETY_THRESHOLD if loaded else 4 * 1024 ** 3
                        overlap = free >= required_free
                        emit('prefetch_decision', enabled=overlap, available_bytes=free,
                             next_batch=pending[index + 1]['id'], threshold=required_free)
                        if overlap:
                            budget = min(cpu_threads, prefetch_threads) if loaded else cpu_threads
                            future = pool.submit(prepare_chunk, pending[index + 1], budget)
                    chunk['status'] = 'translating'
                    save(output / 'schedule.json', state)
                    emit('translation_batch_started', pages=len(chunk['pages']))
                    result = pipeline.finish(source, chunk['pages'], location, token, cpu_threads, server)
                    completed.update(result['completed_pages'])
                    failures.update(result['failed_pages'])
                    chunk['status'] = 'partial' if result['failed_pages'] else 'completed'
                    state.update(completed_pages=sorted(completed), failed_pages=failures)
                    save(output / 'schedule.json', state)
                    emit('chunk_ready', batch=chunk['id'], output=str(location), reused=False,
                         elapsed_seconds=time.monotonic() - started)
                    emit('schedule_progress', completed=len(completed), failed=len(failures), total=len(pages))
            finally:
                token.stop.set()
                pool.shutdown(wait=True, cancel_futures=True)
                if loaded and server is not None:
                    try:
                        unload_model()
                        loaded = False
                    except Exception:
                        pass
        state['status'] = 'partial' if failures else 'completed'
        return state
    except BaseException as error:
        state.update(status='cancelled' if isinstance(error, InterruptedError) else 'failed',
                     error_type=type(error).__name__, error=str(error))
        raise
    finally:
        stop_monitor.set()
        watcher.join(3)
        state['elapsed_seconds'] = time.monotonic() - started
        save(output / 'schedule.json', state)
        save(output / 'memory.json', {'samples': samples, 'minimum_available_bytes': min((s['available_bytes'] for s in samples), default=None)})
        emit('schedule_finished', status=state['status'], completed=len(completed), failed=len(failures), total=len(pages))
        model_runtime.emit = previous_model_emit
        EVENT_CONTEXT.data = {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pages-file', type=Path, required=True)
    parser.add_argument('--cancel-file', type=Path)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--legacy-run', type=Path)
    parser.add_argument('--batch-size', type=int, choices=range(1, 17), default=12)
    parser.add_argument('--cpu-threads', type=int, choices=range(1, 9), default=4)
    parser.add_argument('--prefetch-threads', type=int, choices=range(1, 9), default=2)
    args = parser.parse_args()
    result = run(args.source, read(args.pages_file), args.output, args.cancel_file, args.resume,
                 args.batch_size, args.cpu_threads, args.legacy_run, args.prefetch_threads)
    return 0 if result['status'] == 'completed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
