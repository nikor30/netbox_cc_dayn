"""Runtime-editable settings persisted to a small JSON file.

Values saved in the settings GUI override the environment configuration.
The admin password is stored as a salted PBKDF2 hash, never in plain text;
the NetBox token is stored in the file (mode 0600) and never rendered back
into any page.
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)

PBKDF2_ITERATIONS = 200_000

_ALLOWED_KEYS = {
    "netbox_url",
    "netbox_token",
    "netbox_verify_ssl",
    "admin_username",
    "admin_password_hash",
}


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS
    )
    return f"pbkdf2:{PBKDF2_ITERATIONS}:{salt}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split(":")
        if scheme != "pbkdf2":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        )
        return hmac.compare_digest(candidate.hex(), digest)
    except (ValueError, TypeError):
        return False


class RuntimeSettings:
    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._path = path
        self._data: dict[str, Any] = {}
        self._load()

    def set_path(self, path: Path) -> None:
        """Swap the backing file (used by tests) and reload."""
        with self._lock:
            self._path = path
            self._data = {}
        self._load()

    def _load(self) -> None:
        with self._lock:
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = {k: v for k, v in raw.items() if k in _ALLOWED_KEYS}
            except FileNotFoundError:
                self._data = {}
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("could not read runtime settings %s: %s", self._path, exc)
                self._data = {}

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def save(self, **updates: Any) -> None:
        """Merge non-None updates and write the file atomically (mode 0600)."""
        with self._lock:
            for key, value in updates.items():
                if key not in _ALLOWED_KEYS:
                    raise ValueError(f"unknown runtime setting: {key}")
                if value is not None:
                    self._data[key] = value
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)

    def effective(self, env: Settings) -> Settings:
        """Environment settings with runtime overrides applied on top."""
        overrides: dict[str, Any] = {}
        for key in ("netbox_url", "netbox_token", "netbox_verify_ssl"):
            value = self.get(key)
            if value is not None and value != "":
                overrides[key] = value
        return env.model_copy(update=overrides) if overrides else env
