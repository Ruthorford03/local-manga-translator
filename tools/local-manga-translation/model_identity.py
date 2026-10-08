"""Bind the portable installation to the tested Sakura layers, not a user's path.

Ollama manifests include an installation-specific `from` field. Its complete
digest is still recorded for resume checks, while the model/template/parameters
are verified independently against the tested installation.
"""
import hashlib
import json
from pathlib import Path

MODEL = 'sukinishiro:latest'
EXPECTED_CONFIG = 'sha256:41e214526470533834dd53fa2b1e984356b83c4bff9d08e5b31f770c2d681fa9'
EXPECTED_LAYERS = {
    'application/vnd.ollama.image.model': 'sha256:2c1fc22a43c15cc42cf1443822115b979c2f9143671f5369df0158d9fc184b77',
    'application/vnd.ollama.image.template': 'sha256:e3b2721cec511e3a627ae0993753abbf5c72cdf4cfcdbc8f5c33ce15c4c4e04b',
    'application/vnd.ollama.image.params': 'sha256:84c00191330dff8465eca653b1f90c269bab993c96edac067e0fcbccda0e8b69',
}
MANIFEST = Path(__file__).resolve().parent / 'ollama-models/manifests/registry.ollama.ai/library/sukinishiro/latest'


def model_manifest_digest(path: Path = MANIFEST, *, required: bool = False) -> str | None:
    """Return this installation's identity; reject a different model recipe.

    Missing setup permits source inspection/import. Execution checks still fail
    on a missing manifest, and preflight passes required=True for a clear error.
    This does not hash the 12 GB weight; the setup verifier does that separately.
    """
    if not path.is_file():
        if required:
            raise FileNotFoundError('Sakura is not installed. Follow docs/SETUP.zh-TW.md.')
        return None
    raw = path.read_bytes()
    manifest = json.loads(raw)
    layers = manifest.get('layers', [])
    actual = {layer.get('mediaType'): layer.get('digest') for layer in layers}
    if (len(layers) != len(EXPECTED_LAYERS) or actual != EXPECTED_LAYERS
            or manifest.get('config', {}).get('digest') != EXPECTED_CONFIG):
        raise ValueError('Sakura model/template/parameters differ from the tested recipe. See docs/SETUP.zh-TW.md.')
    return hashlib.sha256(raw).hexdigest()
