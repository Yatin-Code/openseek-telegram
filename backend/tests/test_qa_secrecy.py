from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.qa.secrecy import collect_known_secrets, guard_text


def test_environment_only_secret_is_collected_and_redacted(capsys):
    value = "synthetic-environment-only-secret-value"
    known = collect_known_secrets(paths=[], environ={"OPENSEEK_POOL_WRITE_KEY": value})
    assert known == [value]

    with pytest.raises(SystemExit) as caught:
        guard_text(f"recipe output included {value}", known)
    assert caught.value.code == 2
    output = capsys.readouterr().out
    assert value not in output
    assert "[REDACTED secret leak caught]" in output
