"""Identifier formats. Every filesystem path component is derived from these validated IDs."""
from __future__ import annotations

import re
import secrets

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX16 = re.compile(r"[0-9a-f]{16}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_INDEX = re.compile(r"(0|[1-9][0-9]{0,5})")


def new_device_id() -> str:
    return secrets.token_hex(8)


def is_invite_id(s: str) -> bool:
    return isinstance(s, str) and _HEX32.fullmatch(s) is not None


def is_device_id(s: str) -> bool:
    return isinstance(s, str) and _HEX16.fullmatch(s) is not None


def is_fingerprint(s: str) -> bool:
    return isinstance(s, str) and _HEX64.fullmatch(s) is not None


def parse_index(s: str, maximum: int = 999_999) -> int | None:
    """Tile index from a path segment: canonical decimal only (no sign, no leading zeros)."""
    if not isinstance(s, str) or _INDEX.fullmatch(s) is None:
        return None
    v = int(s)
    return v if v <= maximum else None
