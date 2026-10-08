"""Launch the isolated local Sakura backend and BallonsTranslator together."""
import json
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = HERE.parents[1]
APP = ROOT / 'tools/BallonsTranslator'
OLLAMA = Path(os.environ.get('MANGA_OLLAMA_EXE') or (Path(os.environ['LOCALAPPDATA']) / 'Programs/Ollama/ollama.exe'))
MODEL_STORE = HERE / 'ollama-models'
BASE_URL = 'http://127.0.0.1:11436'
MODEL = 'sukinishiro:latest'
from model_identity import model_manifest_digest
MODEL_DIGEST = model_manifest_digest()
DEFAULT_CONFIG = HERE / 'ballons-sakura-config.json'


def check_service() -> bool:
    try:
        response = requests.get(BASE_URL + '/api/version', timeout=2)
    except requests.ConnectionError:
        return False
    response.raise_for_status()
    if not isinstance(response.json().get('version'), str):
        raise RuntimeError('Port 11436 is occupied by an unknown service.')
    return True


def requires_service() -> bool:
    if '--headless' not in sys.argv or '--check' in sys.argv or '--config' not in sys.argv:
        return True
    config = Path(sys.argv[sys.argv.index('--config') + 1])
    module = json.loads(config.read_text('utf-8-sig')).get('module', {})
    # Preparation and render-only stages explicitly disable all LLM features.
    # They must not create a model server that a native Qt crash can orphan.
    return not all(module.get(name) is False for name in
                   ('enable_translate', 'llm_translate_vision', 'llm_translate_summary_memory'))


def main() -> None:
    server = None
    log = None
    try:
        need_service = requires_service()
        if need_service and not check_service():
            if not OLLAMA.is_file() or not MODEL_STORE.is_dir():
                raise RuntimeError('Local Ollama or the prepared model store is missing. No download was attempted.')
            log_dir = HERE / 'runtime'
            log_dir.mkdir(exist_ok=True)
            log = (log_dir / ('ollama-' + time.strftime('%Y%m%d-%H%M%S') + '.log')).open('w', encoding='utf-8')
            env = dict(os.environ, OLLAMA_HOST='127.0.0.1:11436', OLLAMA_MODELS=str(MODEL_STORE),
                       OLLAMA_NOPRUNE='1', OLLAMA_NO_CLOUD='1', OLLAMA_VULKAN='1',
                       OLLAMA_NUM_PARALLEL='1', OLLAMA_MAX_LOADED_MODELS='1',
                       OLLAMA_CONTEXT_LENGTH='4096', OLLAMA_KEEP_ALIVE='60s')
            server = subprocess.Popen([str(OLLAMA), 'serve'], env=env, stdout=log, stderr=log,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + 45
            while True:
                try:
                    if check_service():
                        break
                except requests.Timeout:
                    # An owned server can accept TCP before its API is ready.
                    # Keep startup retries within the existing 45-second bound.
                    pass
                if server.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('Local Ollama could not start. See ' + str(log_dir))
                time.sleep(0.5)
        if need_service:
            response = requests.get(BASE_URL + '/api/tags', timeout=5)
            response.raise_for_status()
            matching = [item for item in response.json()['models'] if item['name'] == MODEL]
            if len(matching) != 1 or matching[0]['digest'] != MODEL_DIGEST:
                raise RuntimeError('The prepared Sakura model is missing from the local service. No download was attempted.')
        if '--check' in sys.argv:
            print(json.dumps({'ready': True, 'model': MODEL, 'endpoint': BASE_URL,
                              'owns_server': server is not None, 'config': str(DEFAULT_CONFIG)}))
            return
        os.chdir(APP)
        sys.path.insert(0, str(APP))
        sys.path.insert(0, str(HERE))
        os.environ.update(HF_HUB_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_IMPLICIT_TOKEN='1',
                          HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', QT_API='pyqt6')
        headless = '--headless' in sys.argv
        batch_autolayout = '--batch-autolayout' in sys.argv
        if batch_autolayout:
            sys.argv.remove('--batch-autolayout')
            if not headless:
                raise ValueError('--batch-autolayout is only for headless batch rendering')
        if headless:
            os.environ['QT_QPA_PLATFORM'] = 'offscreen'
            if os.environ.get('MANGA_CPU_THREADS'):
                import torch
                import cv2
                threads = max(1, int(os.environ['MANGA_CPU_THREADS']))
                torch.set_num_threads(threads)
                torch.set_num_interop_threads(1)
                cv2.setNumThreads(threads)
        from qtpy import QtWidgets
        from qtpy.QtCore import QEvent, QTimer
        from sakura_layout import install_batch_autolayout, install_adaptive_fit
        install_vertical_fit = install_adaptive_fit

        class LocalTranslationApplication(QtWidgets.QApplication):
            def __init__(self, *args):
                super().__init__(*args)
                if batch_autolayout:
                    install_batch_autolayout()
                    install_adaptive_fit()
                else:
                    install_adaptive_fit()

            def quit(self):
                # A render-only headless run can request quit before exec().
                if headless:
                    QTimer.singleShot(0, self._quit_in_event_loop)
                else:
                    super().quit()

            def _quit_in_event_loop(self):
                # Run the app's existing shutdown owner before the event loop
                # stops; plain quit() skips MainWindow.closeEvent in headless mode.
                from ballontranslator.ui.mainwindow import MainWindow
                for window in self.topLevelWidgets():
                    if isinstance(window, MainWindow):
                        window.close()
                self.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                super().quit()

        QtWidgets.QApplication = LocalTranslationApplication
        from ballontranslator.modules.translators import base
        from opencc import OpenCC
        base._CHS2CHT_CONVERTER = OpenCC('s2tw')
        # Optional local audit capture for reproducible acceptance runs only.
        audit_path = os.environ.get('SAKURA_AUDIT_PATH')
        if audit_path:
            import openai
            client_factory = openai.Client

            def audited_client(*args, **kwargs):
                client = client_factory(*args, **kwargs)
                create = client.chat.completions.create

                def audited_create(**request):
                    result = create(**request)
                    record = {'request': request, 'content': result.choices[0].message.content,
                              'finish_reason': result.choices[0].finish_reason}
                    with Path(audit_path).open('a', encoding='utf-8') as audit:
                        audit.write(json.dumps(record, ensure_ascii=False) + '\n')
                    return result

                client.chat.completions.create = audited_create
                return client

            openai.Client = audited_client
        if '--config' not in sys.argv:
            sys.argv += ['--config', str(DEFAULT_CONFIG)]
        sys.argv[0] = 'ballontranslator'
        runpy.run_module('ballontranslator', run_name='__main__')
    finally:
        if server is not None and server.poll() is None:
            # Only terminate this launcher's isolated process and its children.
            subprocess.run(['taskkill', '/PID', str(server.pid), '/T', '/F'],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            server.wait(timeout=10)
        if log is not None:
            log.close()


if __name__ == '__main__':
    main()
