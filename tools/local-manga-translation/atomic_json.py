"""Atomic JSON replacement with a short, bounded Windows sharing-error retry.

The temporary file is unique and beside its destination. Readers see the old
or new complete file; simultaneous writers still have last-writer-wins behavior.
Only replacement errors with WinError 5, 32, or 33 are retried. Persistent
permissions, disk errors, and locks beyond the retry window remain failures.
"""
import json
from os import close as _close, fdopen as _fdopen, fsync as _fsync
from os import replace as _replace, unlink as _unlink
from pathlib import Path
from tempfile import mkstemp as _mkstemp
from time import sleep as _sleep

_RETRY_WINERRORS = frozenset((5, 32, 33))
_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.2, 1.6)


def save_json(path: Path, data) -> None:
    """Save like the folder's former save(), preserving the original OS error.

Eight replacement attempts have at most 4.35 s of scheduled waiting, in addition
to OS I/O time. The original error object is raised when that budget is spent.
A cleanup failure is attached as a note without replacing the primary error.
"""
    path = Path(path)
    text = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    descriptor, temporary_name = _mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(temporary_name)
    try:
        try:
            stream = _fdopen(descriptor, 'w', encoding='utf-8')
        except BaseException as primary:
            try:
                _close(descriptor)
            except OSError as cleanup_error:
                primary.add_note(f'Temporary JSON descriptor cleanup failed: {cleanup_error}')
            raise
        with stream:
            stream.write(text)
            stream.flush()
            _fsync(stream.fileno())

        first_error = None
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                _replace(temporary, path)
                return
            except OSError as error:
                if getattr(error, 'winerror', None) not in _RETRY_WINERRORS:
                    raise
                if first_error is None:
                    first_error = error
                if attempt == len(_RETRY_DELAYS):
                    first_error.add_note('Atomic JSON replacement failed after 8 attempts (4.35 s scheduled wait).')
                    raise first_error from None
                _sleep(_RETRY_DELAYS[attempt])
    except BaseException as primary:
        try:
            _unlink(temporary)
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            primary.add_note(f'Temporary JSON cleanup failed; retained {temporary}: {cleanup_error}')
        raise
