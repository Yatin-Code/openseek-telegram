"""QA runner secrets guard: the runner must fail if a known secret value appears
anywhere in its output (P1 acceptance, issue #42).

Known secret values are read from secret stores OUTSIDE the repository:
`~/.config/openseek/qa.env` (the artifact registry's canonical secrets file) and
any `*.env` files under the operator's OpenSeek config directory. Values are held
in memory only; the runner never echoes them and CI never reads them.

Only keys whose NAME is secret-shaped are collected (the naming rule documented
in docs/qa/ARTIFACTS.md: "Secret: write the location only" applies to values
stored under keys carrying TOKEN/KEY/HASH/PASSWORD/SESSION/SECRET/CLIENT_SECRET
words); public identifiers stored by value (base URLs, TLS pins, fingerprints)
stay redact-proof precisely because printing them is allowed.
"""
from __future__ import annotations

import pathlib
import sys

_MIN_LENGTH = 24          # shorter assignments are not load-bearing URLs or keys
_SECRET_WORDS = ("TOKEN", "KEY", "HASH", "PASSWORD", "SESSION", "SECRET")
_REDACTED = "[REDACTED secret leak caught]"


def _secret_files() -> list[pathlib.Path]:
    base = pathlib.Path.home() / ".config" / "openseek"
    files = sorted({base / "qa.env"}) + sorted(set(base.glob("*.env")) - {base / "qa.env"})
    return [f for f in files if f.is_file()]


def collect_known_secrets(paths: list[pathlib.Path] | None = None) -> list[str]:
    """Every value assigned under a secret-shaped key in the operator's secret
    stores, in memory only. Public identifiers are deliberately excluded."""
    values: list[str] = []
    for path in (paths if paths is not None else _secret_files()):
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            if not any(w in name.upper() for w in _SECRET_WORDS):
                continue                # public identifier by value (registry rule)
            value = value.strip().strip('"\'')
            if value and len(value) >= _MIN_LENGTH and value not in values:
                values.append(value)
    return values


def scrub(text: str, known: list[str]) -> tuple[str, bool]:
    """Redact any known secret value. Returns (clean_text, leaked_bool)."""
    leaked = False
    for value in known:
        if value and value in text:
            leaked = True
            text = text.replace(value, _REDACTED)
    return text, leaked


def guard_text(text: str, known: list[str]) -> None:
    """Print `text` safely: replace leaked values with the redaction notice, then fail.
    A leak never reaches an issue body: the line is replaced and the process exits."""
    clean, leaked = scrub(text, known)
    sys.stdout.write(clean)
    if leaked:
        sys.stdout.write(
            "\n(fail) the runner caught a known secret value in its own output; "
            "everything printed above is redacted. Do not paste evidence that has not "
            "come from a guarded run.\n")
        raise SystemExit(2)
