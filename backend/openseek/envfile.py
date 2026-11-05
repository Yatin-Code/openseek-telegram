"""Read and write the protected per-install environment file used by browser setup."""
from __future__ import annotations

import os
import re
import shlex
import tempfile
from pathlib import Path

_KEY = re.compile(r"OPENSEEK_[A-Z0-9_]+\Z")


def read_env_file(path: Path) -> dict[str, str]:
    values = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            fields = shlex.split(line, comments=True, posix=True)
        except ValueError:
            continue
        if len(fields) != 1 or "=" not in fields[0]:
            continue
        key, value = fields[0].split("=", 1)
        if _KEY.fullmatch(key):
            values[key] = value
    return values


def write_env_file(path: Path, updates: dict[str, str]) -> None:
    path = Path(path)
    if any(not _KEY.fullmatch(key) for key in updates):
        raise ValueError("bad environment key")
    if any(not isinstance(value, str) or any(ord(ch) < 0x20 and ch != "\t" for ch in value)
           for value in updates.values()):
        raise ValueError("bad environment value")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
    values = read_env_file(path)
    values.update(updates)
    body = "# OpenSeek private per-install settings\n" + "".join(
        f"{key}={shlex.quote(value)}\n" for key, value in sorted(values.items()))
    fd, temporary = tempfile.mkstemp(prefix=".openseek-env-", dir=path.parent)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
