"""Small, crash-safe JSON persistence helpers used by the web services."""

import json
import logging
import os
import tempfile
import threading


_locks = {}
_locks_guard = threading.Lock()


def _lock_for(path):
    canonical = os.path.abspath(path)
    with _locks_guard:
        return _locks.setdefault(canonical, threading.RLock())


def load_json(path, default, logger=None):
    try:
        with _lock_for(path), open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return default
    except Exception as exc:
        (logger or logging.getLogger(__name__)).warning(
            "Could not load %s: %s", path, exc
        )
        return default


def save_json(path, data, logger=None):
    """Atomically replace *path* so interruption cannot leave partial JSON."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temp_path = None
    try:
        with _lock_for(path):
            fd, temp_path = tempfile.mkstemp(prefix=".json-", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            temp_path = None
        return True
    except Exception as exc:
        (logger or logging.getLogger(__name__)).warning(
            "Could not save %s: %s", path, exc
        )
        return False
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
