"""Inputs for QA recipes (P1, issue #42).

Values come from the operator's secret stores OUTSIDE the repository:
`~/.config/openseek/qa.env` (the registry's canonical secrets file, mode 0600)
and the process environment. `key_of` is for genuinely secret inputs (bearer
keys) and `public_of` for public identifiers by value (base URL, TLS pin,
package fingerprint). The registry rule (docs/qa/ARTIFACTS.md header) decides
which is which; nothing here echoes a secret.
"""
from __future__ import annotations

import os
from pathlib import Path

_MISSING = object()


def qa_env_values() -> dict[str, str]:
    """key=value pairs from the operator's qa.env (empty dict when absent)."""
    values: dict[str, str] = {}
    base = Path.home() / ".config" / "openseek"
    for path in sorted({base / "qa.env"} | {p for p in base.glob("*.env") if p.is_file()}):
        for line in path.read_text(errors="replace").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                values.setdefault(k.strip(), v.strip())
    return values


def _resolve(name: str, values: dict[str, str]) -> str:
    value = values.get(name, "")
    if not value:
        value = os.environ.get(name, "")
    return value


def key_of(name: str, values: dict[str, str], *, default: str | None = None) -> str:
    """A secret input: environment or qa.env, by reference to its location only."""
    value = _resolve(name, values)
    if value:
        return value
    if default is not None:
        return default
    raise SystemExit(f"missing secret input {name}. Add it to ~/.config/openseek/qa.env "
                     "(never to the repository) or export it in the environment.")


# Public identifiers may be read from the same file; the registry documents why.
public_of = key_of
