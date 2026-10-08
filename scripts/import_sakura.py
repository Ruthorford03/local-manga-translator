"""Import the verified local GGUF into this clone's isolated Ollama store.

No download is attempted. Refuses an occupied port and terminates only the
Ollama process created by this command. Requires the main Python environment.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import requests

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / 'tools/local-manga-translation'
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(HERE))
from model_identity import MODEL, MANIFEST, model_manifest_digest
from prepare_models import valid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=11436)
    args = parser.parse_args()
    if MANIFEST.exists():
        print(json.dumps({'already_imported': True, 'manifest_sha256': model_manifest_digest(required=True)}))
        return
    item = next(x for x in json.loads((ROOT / 'docs/MODELS.json').read_text('utf-8'))['files'] if x['group'] == 'sakura')
    if not valid(ROOT / item['path'], item):
        raise ValueError('Download/verify the pinned Sakura GGUF first: scripts/prepare_models.py --download')
    with socket.socket() as reservation:
        reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        reservation.bind(('127.0.0.1', args.port))
    ollama = Path(os.environ.get('MANGA_OLLAMA_EXE') or (Path(os.environ['LOCALAPPDATA']) / 'Programs/Ollama/ollama.exe'))
    if not ollama.is_file():
        raise FileNotFoundError('Install Ollama or set MANGA_OLLAMA_EXE.')
    runtime = HERE / 'runtime'
    runtime.mkdir(exist_ok=True)
    store = HERE / 'ollama-models'
    store.mkdir(exist_ok=True)
    endpoint = f'http://127.0.0.1:{args.port}'
    env = dict(os.environ, OLLAMA_HOST=f'127.0.0.1:{args.port}', OLLAMA_MODELS=str(store),
               OLLAMA_NO_CLOUD='1', OLLAMA_NOPRUNE='1', OLLAMA_VULKAN='1')
    session = requests.Session()
    session.trust_env = False
    with (runtime / 'setup-ollama.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen([str(ollama), 'serve'], env=env, stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic() + 45
            while True:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('Setup Ollama did not start; see runtime/setup-ollama.log')
                try:
                    response = session.get(endpoint + '/api/version', timeout=2)
                    response.raise_for_status()
                    listing = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True,
                                             timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
                    owned = any(len(row) == 5 and row[0] == 'TCP' and row[1] == f'127.0.0.1:{args.port}'
                                and row[2] == '0.0.0.0:0' and row[-1] == str(process.pid)
                                for row in (line.split() for line in listing.stdout.decode('oem', errors='replace').splitlines()))
                    if owned:
                        break
                except requests.RequestException:
                    pass
                time.sleep(0.5)
            subprocess.run([str(ollama), 'create', MODEL, '-f', str(HERE / 'Modelfile.sakura')],
                           cwd=HERE, env=env, check=True, timeout=900,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            print(json.dumps({'imported': MODEL, 'manifest_sha256': model_manifest_digest(required=True)}))
        finally:
            session.close()
            if process.poll() is None:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                               timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
                process.wait(timeout=15)


if __name__ == '__main__':
    main()
