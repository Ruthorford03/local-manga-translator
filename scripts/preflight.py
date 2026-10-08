"""Read-only local readiness checks. Does not start services or load models."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / 'tools/local-manga-translation'))
from model_identity import MANIFEST, model_manifest_digest
from prepare_models import digest, valid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--full-hash', action='store_true', help='Hash all downloaded files and Ollama blobs; takes longer')
    args = parser.parse_args()
    errors = []
    versions = {'python': sys.version.split()[0]}
    if sys.platform != 'win32' or sys.version_info[:2] != (3, 12):
        errors.append('Supported setup is Windows x64 with Python 3.12.')
    for name in ('Pillow', 'PyQt6', 'qtpy', 'torch', 'torchvision', 'transformers', 'openai', 'requests',
                 'opencc-python-reimplemented', 'opencv-python', 'jaconv', 'fugashi', 'unidic-lite', 'spacy-pkuseg'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            errors.append('Missing main-runtime package: ' + name)
    magi = ROOT / 'tools/magi-pilot/.venv/Scripts/python.exe'
    if not magi.is_file():
        errors.append('Magi Python environment is missing.')
    else:
        probe = subprocess.run([str(magi), '-I', '-c',
            "import importlib.metadata as m,json; print(json.dumps({p:m.version(p) for p in ['torch','torchvision','transformers','timm','pulp','psutil','shapely','einops']}))"],
            capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        if probe.returncode:
            errors.append('Magi package metadata check failed.')
        else:
            versions['magi'] = json.loads(probe.stdout)
    ollama = Path(os.environ.get('MANGA_OLLAMA_EXE') or (Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/Ollama/ollama.exe'))
    if not ollama.is_file():
        errors.append('Ollama is missing; install it or set MANGA_OLLAMA_EXE.')
    font = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts/msjh.ttc'
    if not font.is_file():
        errors.append('Microsoft JhengHei font is missing; install it through Windows or configure a compatible font.')
    items = json.loads((ROOT / 'docs/MODELS.json').read_text('utf-8'))['files']
    for item in items:
        path = ROOT / item['path']
        ok = valid(path, item) if args.full_hash else path.is_file() and path.stat().st_size == item['bytes']
        if not ok:
            errors.append('Missing or mismatched model file: ' + item['path'])
    try:
        identity = model_manifest_digest(required=True)
        manifest = json.loads(MANIFEST.read_bytes())
        for layer in [manifest['config'], *manifest['layers']]:
            blob = MANIFEST.parents[4] / 'blobs' / layer['digest'].replace(':', '-')
            if not blob.is_file() or blob.stat().st_size != layer['size']:
                errors.append('Ollama blob missing or wrong size: ' + layer['digest'])
            elif args.full_hash and digest(blob) != layer['digest'].split(':', 1)[1]:
                errors.append('Ollama blob failed SHA-256: ' + layer['digest'])
    except (OSError, ValueError, KeyError, TypeError) as error:
        identity = None
        errors.append(str(error))
    print(json.dumps({'ready': not errors, 'full_content_hashes_checked': args.full_hash,
                      'versions': versions, 'model_manifest_sha256': identity, 'errors': errors}, ensure_ascii=False, indent=2))
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
