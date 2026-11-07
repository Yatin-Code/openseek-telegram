"""Paired-device bearer token authentication."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import threading
import time
from pathlib import Path

from .db import DB
from .ids import new_device_id

_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


def token_shaped(token: str) -> bool:
    """True when a bearer credential has the paired-device token format."""
    return bool(_TOKEN.fullmatch(token))


class DeviceAuth:
    """Bearer token -> device_id. Only SHA-256(token) is stored; comparisons are constant-time
    over every registered hash."""

    def __init__(self, db: DB, store=None):
        self.db = db
        self.store = store or db
        self._live_store = store is not None
        self._lock = threading.Lock()
        self._hashes: list[tuple[str, str]] = []
        self._roles: dict[str, tuple[str, float | None]] = {}
        if not self._live_store:
            self.reload()

    def reload(self) -> None:
        with self._lock:
            rows = self.store.auth_rows()
            self._hashes = [(h, d) for h, d, _r, _e in rows]
            self._roles = {d: (r, e) for _h, d, r, e in rows}

    def register(self, device_name: str, token_hash: str, role: str = "owner", expires: float | None = None,
                 invite_id: str | None = None) -> str:
        device_id = new_device_id()
        self.store.add_device(device_id, device_name, token_hash, role, expires, invite_id)
        self.reload()
        return device_id

    def role_of(self, device_id: str | None) -> str | None:
        """'owner' | 'guest' for a device that exists and has not expired, else None."""
        if device_id is None:
            return None
        with self._lock:
            r = self._roles.get(device_id)
        if r is None or (r[1] is not None and time.time() > r[1]):
            return None
        return r[0]

    def revoke(self, device_id: str) -> bool:
        ok = self.store.remove_device(device_id)
        self.reload()
        return ok

    def authenticate(self, authorization: str | None) -> str | None:
        if not authorization or not authorization.startswith("Bearer "):
            return None
        token = authorization[7:].strip()
        if not _TOKEN.fullmatch(token):
            return None
        h = hashlib.sha256(token.encode()).hexdigest()
        if self._live_store:
            self.reload()                 # another serverless instance may have added/revoked a device
        found = None
        with self._lock:
            for stored, device_id in self._hashes:
                if hmac.compare_digest(stored, h):
                    found = device_id
        return found if self.role_of(found) is not None else None          # an expired guest is no device at all

    def is_revoked_token(self, token: str) -> bool:
        """True when this well-formed token was revoked (tombstoned), as opposed to never valid."""
        h = hashlib.sha256(token.encode()).hexdigest()
        check = getattr(self.store, "is_token_revoked", None)
        return bool(check(h)) if check is not None else False

    def devices(self) -> list[dict]:
        return self.store.devices()

    def touch_device(self, device_id: str) -> None:
        self.store.touch_device(device_id)

    def can_contribute(self, device_id: str) -> bool:
        return self.store.can_contribute(device_id)

    def set_contribute(self, device_id: str, enabled: bool) -> bool:
        return self.store.set_contribute(device_id, enabled)


def admin_token(data_dir: Path, configured: str = "") -> str:
    """The per-install secret the loopback admin API requires: OPENSEEK_ADMIN_TOKEN, else a random value kept in
    <data>/admin.token (created owner-only). Without it, any local process could pair itself as the owner."""
    if configured:
        if len(configured) < 16:
            raise ValueError("OPENSEEK_ADMIN_TOKEN must be at least 16 characters")
        return configured
    return load_or_create_secret(Path(data_dir) / "admin.token").hex()


def load_or_create_secret(path: Path) -> bytes:
    path = Path(path)
    if path.exists():
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    data = os.urandom(32)
    from .cache import atomic_write
    atomic_write(path, data)
    return data
