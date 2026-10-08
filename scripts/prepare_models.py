"""Explicit download or offline verification of the content-pinned model inventory.

No model is executed. --download contacts the listed public model hosts and
downloads about 14.9 GB. Review the model terms before choosing that option.
Existing mismatched files are preserved and reported, never overwritten.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def destination(relative):
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError('Model path escapes repository')
    return path


def valid(path, item):
    return path.is_file() and path.stat().st_size == item['bytes'] and digest(path) == item['sha256']


def download(url, partial, max_bytes):
    request = urllib.request.Request(url, headers={'User-Agent': 'local-manga-translator-model-setup/1'})
    with urllib.request.urlopen(request, timeout=120) as response, partial.open('xb') as stream:
        count = 0
        for chunk in iter(lambda: response.read(4 * 1024 * 1024), b''):
            count += len(chunk)
            if count > max_bytes:
                raise ValueError('Download exceeds expected size; partial file retained')
            stream.write(chunk)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download', action='store_true', help='Explicitly download missing files after reading model terms')
    args = parser.parse_args()
    items = json.loads((ROOT / 'docs/MODELS.json').read_text('utf-8'))['files']
    missing = []
    for item in items:
        target = destination(item['path'])
        if target.exists():
            if not valid(target, item):
                raise ValueError('Existing file does not match inventory; preserved: ' + item['path'])
            print('OK ' + item['path'], flush=True)
            continue
        if not args.download:
            missing.append(item['path'])
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + '.part')
        if partial.exists():
            raise FileExistsError('Prior partial download preserved; move it aside before retrying: ' + str(partial))
        print('Downloading ' + item['path'], flush=True)
        if 'archive_member' in item:
            cache = ROOT / 'cache/model-downloads'
            cache.mkdir(parents=True, exist_ok=True)
            archive = cache / (hashlib.sha256(item['url'].encode()).hexdigest() + '.zip')
            if not archive.exists():
                archive_partial = archive.with_suffix('.zip.part')
                download(item['url'], archive_partial, 256 * 1024 * 1024)
                # Extraction below reads only one allowlisted member, never extractall.
                with zipfile.ZipFile(archive_partial) as z:
                    z.infolist()
                archive_partial.rename(archive)
            with zipfile.ZipFile(archive) as z:
                candidates = [i for i in z.infolist() if Path(i.filename).name == item['archive_member'] and not i.is_dir()]
                if len(candidates) != 1 or candidates[0].file_size != item['bytes']:
                    raise ValueError('Archive member missing, ambiguous, or wrong size')
                with z.open(candidates[0]) as source, partial.open('xb') as output:
                    shutil.copyfileobj(source, output, 4 * 1024 * 1024)
        else:
            download(item['url'], partial, item['bytes'])
        if not valid(partial, item):
            raise ValueError('Downloaded content failed SHA-256; partial retained: ' + item['path'])
        partial.rename(target)
    if missing:
        print(json.dumps({'ready': False, 'missing': missing}, indent=2))
        return 1
    print(json.dumps({'model_files_verified': len(items), 'ollama_import_still_required': True}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
