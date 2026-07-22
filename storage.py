"""Small, crash-safe JSON persistence helpers used by the web services."""

import json
import logging
import os
import sqlite3
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


class SQLiteDocumentStore:
    """Small SQLite document store with transparent JSON-file migration."""

    def __init__(self, path, logger=None):
        self.path = os.path.abspath(path)
        self.logger = logger or logging.getLogger(__name__)
        self._lock = _lock_for(self.path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS documents ("
                "key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL)"
            )

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def load(self, key, default, migrate_path=None):
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM documents WHERE key = ?", (key,)
            ).fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except json.JSONDecodeError:
                    self.logger.error("Invalid SQLite state document: %s", key)
            migrated = load_json(migrate_path, default, self.logger) if migrate_path else default
            self._save_with_connection(connection, key, migrated)
            return migrated

    def save(self, key, data):
        try:
            with self._lock, self._connect() as connection:
                self._save_with_connection(connection, key, data)
            return True
        except Exception as exc:
            self.logger.warning("Could not save SQLite state %s: %s", key, exc)
            return False

    @staticmethod
    def _save_with_connection(connection, key, data):
        import time
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        connection.execute(
            "INSERT INTO documents(key, payload, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
            "updated_at=excluded.updated_at",
            (key, payload, time.time()),
        )
        connection.commit()

    def healthy(self):
        try:
            with self._lock, self._connect() as connection:
                return connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        except Exception:
            return False


def save_bytes_atomic(path, data, logger=None):
    """Atomically replace a binary file."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temp_path = None
    try:
        with _lock_for(path):
            fd, temp_path = tempfile.mkstemp(prefix=".blob-", dir=directory)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
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
