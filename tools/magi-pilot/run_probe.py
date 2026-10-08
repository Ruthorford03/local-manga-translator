"""Run image-only Magi v2 inference in an isolated, network-blocked process."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import threading
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--output', required=True)
parser.add_argument('--device', choices=['cpu', 'xpu'], default='cpu')
parser.add_argument('--threads', type=int, default=4)
parser.add_argument('--source', type=Path, required=True)
page_args = parser.add_mutually_exclusive_group()
page_args.add_argument('--pages', nargs='+')
page_args.add_argument('--pages-file', type=Path)
args = parser.parse_args()
args.pages = (json.loads(args.pages_file.read_text('utf-8')) if args.pages_file
              else args.pages or ['0004.webp', '0007.webp'])
if not isinstance(args.pages, list) or not args.pages or not all(isinstance(name, str) for name in args.pages):
    raise ValueError('Pages must be a nonempty list of filenames')
source = args.source.resolve()
if len(set(args.pages)) != len(args.pages) or any(Path(name).name != name for name in args.pages):
    raise ValueError('Pages must be unique filenames within the source folder')
if any(not (source / name).is_file() for name in args.pages):
    raise FileNotFoundError('A requested source page is missing')
OUT = Path(args.output).resolve()
if OUT.exists():
    raise FileExistsError('Use a fresh probe output directory')
OUT.mkdir(parents=True)
for key, path in {'HF_HOME': HERE / 'cache/hf', 'TORCH_HOME': HERE / 'cache/torch',
                  'MPLCONFIGDIR': HERE / 'cache/matplotlib'}.items():
    path.mkdir(parents=True, exist_ok=True)
    os.environ[key] = str(path)
os.environ.update(HF_HUB_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                  TRANSFORMERS_OFFLINE='1', PYTHONDONTWRITEBYTECODE='1')
blocked_connections = []


def deny_network(sock, address):
    blocked_connections.append(str(address))
    raise RuntimeError('Network is disabled during this local model probe')


socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
import psutil
process = psutil.Process()
started = time.perf_counter()
resource_samples = []
finished = threading.Event()


def monitor():
    while not finished.wait(0.2):
        mem = process.memory_info()
        resource_samples.append({'elapsed_s': time.perf_counter() - started,
                                 'rss': mem.rss, 'private_bytes': getattr(mem, 'private', None),
                                 'system_available': psutil.virtual_memory().available})


threading.Thread(target=monitor, daemon=True).start()


def event(name, **data):
    item = {'event': name, 'elapsed_s': time.perf_counter() - started, **data}
    with (OUT / 'events.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(item) + '\n')
    print(json.dumps(item), flush=True)


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


record = {'device': args.device, 'torch_threads': args.threads,
          'input_policy': 'Only the requested original page images; no OCR, supplied identities, translations or editorial hints',
          'source_directory': str(source),
          'pages': {}, 'network_guard_enabled': True, 'success': False}
try:
    event('imports_started')
    import torch
    import transformers
    import numpy as np
    from PIL import Image
    from transformers import AutoConfig, AutoModel
    record['versions'] = {'torch': torch.__version__, 'transformers': transformers.__version__}
    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    torch.manual_seed(20261001)
    if args.device == 'xpu' and not torch.xpu.is_available():
        raise RuntimeError('Requested XPU is unavailable; no silent CPU fallback')
    model_dir = HERE / 'models/magiv2'
    config = AutoConfig.from_pretrained(model_dir, trust_remote_code=True, local_files_only=True)
    config.disable_ocr = True
    # The pinned Magi checkpoint contains its backbone weights. Avoid fetching unrelated pretrained assets.
    config.detection_model_config.use_pretrained_backbone = False
    record['runtime_config_overrides'] = {'disable_ocr': True, 'use_pretrained_backbone': False}
    event('model_load_started')
    load_started = time.perf_counter()
    model, loading = AutoModel.from_pretrained(model_dir, config=config, trust_remote_code=True,
                                               local_files_only=True, output_loading_info=True,
                                               torch_dtype=torch.float32)
    (OUT / 'weight-loading.json').write_text(json.dumps(loading, indent=2), 'utf-8')
    missing = loading.get('missing_keys', [])
    unexpected = [key for key in loading.get('unexpected_keys', []) if not key.startswith('ocr_model.')]
    assert not missing and not unexpected and not loading.get('mismatched_keys') and not loading.get('error_msgs'), loading
    model = model.to(args.device).eval()
    record['model_load_seconds'] = time.perf_counter() - load_started
    record['weights'] = {'path': str(model_dir / 'pytorch_model.bin'),
                         'verified_download_record': str(ROOT / 'docs/MODELS.json')}
    event('model_loaded', seconds=record['model_load_seconds'], disabled_ocr_weights_ignored=len(loading.get('unexpected_keys', [])))
    for name in args.pages:
        image_path = source / name
        before = sha(image_path)
        with Image.open(image_path) as original:
            # Matches the official Magi loading example; no content annotations are supplied.
            pixels = np.asarray(original.convert('L').convert('RGB'))
        event('page_started', page=name, dimensions=list(pixels.shape))
        one_started = time.perf_counter()
        torch.manual_seed(20261001)
        with torch.inference_mode():
            result = model.predict_detections_and_associations([pixels])[0]
        if args.device == 'xpu':
            torch.xpu.synchronize()
        elapsed = time.perf_counter() - one_started
        assert before == sha(image_path)
        (OUT / (Path(name).stem + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
        record['pages'][name] = {'source_sha256': before, 'elapsed_seconds': elapsed,
                               'panels': len(result['panels']), 'characters': len(result['characters']),
                               'texts': len(result['texts']), 'speaker_links': len(result['text_character_associations']),
                               'page_local_character_groups': len(set(result['character_cluster_labels'])),
                               'source_unchanged': True}
        event('page_finished', page=name, **record['pages'][name])
    record['success'] = True
except Exception as error:
    record['error'] = {'type': type(error).__name__, 'message': str(error)}
    raise
finally:
    finished.set()
    memory = process.memory_info()
    record.update(elapsed_seconds=time.perf_counter() - started,
                  blocked_connection_attempts=blocked_connections,
                  sampled_peak_rss_bytes=max([s['rss'] for s in resource_samples] + [memory.rss]),
                  process_peak_working_set_bytes=getattr(memory, 'peak_wset', None),
                  minimum_system_available_bytes=min([s['system_available'] for s in resource_samples] + [psutil.virtual_memory().available]))
    (OUT / 'run.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), 'utf-8')
    (OUT / 'resources.json').write_text(json.dumps(resource_samples), 'utf-8')
    event('probe_finished', success=record['success'], peak_rss_bytes=record['sampled_peak_rss_bytes'])
