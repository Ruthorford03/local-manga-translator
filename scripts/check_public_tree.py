"""Check Git-tracked release files for common accidental data disclosure.

This conservative check supplements human review; it is not a universal secret
detector. It reads tracked files only, so installed ignored models are excluded.
"""
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'private user path': re.compile(r'[A-Za-z]:[\\/]+Users[\\/]+(?!Public(?:[\\/]|$))[^\\/\s"<>]+', re.I),
    'GitHub token': re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})'),
    'Hugging Face token': re.compile(r'hf_[A-Za-z0-9]{30,}'),
    'OpenAI-like secret': re.compile(r'sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}'),
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
}
BLOCKED_PARTS = {'.venv', 'venv', 'ollama-models', 'runtime', '__pycache__', '.env', 'ballontrans_pylibs_win'}
BLOCKED_SUFFIXES = {'.gguf', '.safetensors', '.pth', '.pt', '.onnx', '.pkl', '.npz', '.log', '.bak', '.exe', '.dll', '.pyd', '.pyc', '.part'}


def main():
    paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode('utf-8').split('\0')
    paths = [p for p in paths if p]
    if not paths:
        raise RuntimeError('Stage the candidate release before checking.')
    issues = []
    total = 0
    for relative in paths:
        file = ROOT / relative
        if file.is_symlink() or not file.is_file():
            issues.append({'path': relative, 'reason': 'symlink or missing file'})
            continue
        data = file.read_bytes()
        total += len(data)
        if BLOCKED_PARTS.intersection(Path(relative).parts) or file.suffix.lower() in BLOCKED_SUFFIXES:
            issues.append({'path': relative, 'reason': 'runtime/model/private file class'})
        if len(data) > 20 * 1024 * 1024:
            issues.append({'path': relative, 'reason': 'unexpected large release file'})
        try:
            contents = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            continue
        for reason, pattern in PATTERNS.items():
            if pattern.search(contents):
                # Never emit the matched secret value.
                issues.append({'path': relative, 'reason': reason})
    print(json.dumps({'files': len(paths), 'bytes': total, 'issues': issues}, ensure_ascii=False, indent=2))
    return int(bool(issues))


if __name__ == '__main__':
    raise SystemExit(main())
