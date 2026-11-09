"""G7 package-fix regression: a serialized Telethon StringSession must not
fail a filesystem probe or be opened as a session file under Vercel's Python
3.12.

Vercel runs Python 3.12.13, where pathlib.Path.exists() re-raises any OSError
outside pathlib's ignored set. A serialized StringSession is a bare
353-character name (urlsafe base64 of ">B4sH256s" + the version character),
longer than NAME_MAX (255), so the probe in `_looks_like_path` fails with
OSError(ENAMETOOLONG). That error escapes `_session()` into `_pool_error()`,
which translates it to PoolError(503, "telegram_unavailable") — the live
package-read failure on the G7 node after pairing. Python 3.14 exists()
returns False ("Changed in version 3.14" per the pathlib docs: it no longer
raises any OSError; live-verified on 3.14.4), which is why local suites never
reproduced the crash. On 3.13 the docs still document "some kinds of OSError
are raised, and others suppressed", so 3.13 is deliberately not pinned here.

The tests below model the <= 3.12 exists() semantics exactly (os.stat +
ignored errno set, faithful to CPython 3.12 pathlib.Path) instead of relying
on the skip-if-raising 3.14 helper. Nothing here touches the network: the
synthetic session is built with stdlib struct + urlsafe base64 from a
synthetic 256-byte key, and every TelegramClient construction is monkeypatched
out via the store's `_new_client` seam.
"""
from __future__ import annotations

import errno
import ipaddress
import os
import pathlib
import stat
import struct
import sys

import pytest
import telethon
from telethon.sessions import StringSession
from telethon.sessions import string as string_session_module

from openseek import telegram_mt
from openseek.config import Settings

# The model subclasses pathlib.PosixPath and asserts NAME_MAX/os.stat errno
# semantics: POSIX-specific by design, so the whole file stays POSIX-only.
pytestmark = pytest.mark.skipif(os.name != "posix", reason="models CPython 3.12"
                                " POSIX filename semantics (NAME_MAX 255"
                                " components, os.stat errno mapping)")

_RECORDED = object()

# CPython 3.12 pathlib._IGNORED_ERRNOS: exists() maps exactly these to False
# via os.stat, re-raises everything else (and maps ValueError to False).
_PY312_IGNORED_ERRNOS = (errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP)


class _Py312PosixPath(pathlib.PosixPath):
    """pathlib.Path with CPython <= 3.12 exists() semantics (os.stat-based)."""

    def exists(self) -> bool:
        try:
            os.stat(self)
        except OSError as exc:
            if exc.errno in _PY312_IGNORED_ERRNOS:
                return False
            raise
        except ValueError:
            return False
        return True


def _synthetic_session() -> str:
    """A valid serialized StringSession with a synthetic auth key: stdlib
    struct + urlsafe base64 in Telethon 1.45's exact wire format, round-tripped
    through StringSession.decode/save. Never real credentials, never networked."""
    wire = struct.pack(
        ">B4sH256s",
        2,                                  # documentation-style synthetic dc_id
        ipaddress.ip_address("127.0.0.1").packed,
        443,
        bytes(range(256)),                  # synthetic auth key
    )
    value = string_session_module.CURRENT_VERSION + StringSession.encode(wire)
    rebuilt = StringSession(value)
    assert rebuilt.dc_id == 2
    assert rebuilt.server_address == "127.0.0.1"
    assert rebuilt.port == 443
    assert rebuilt.auth_key.key == bytes(range(256))
    assert rebuilt.save() == value
    # The exact shapes that make Vercel's helper treat it as a path candidate
    # and then crash: one bare component longer than NAME_MAX, no separator,
    # no ".session" suffix.
    assert len(value) == len("1" + "A" * 352) and len(value) > 255
    assert "/" not in value and "\\" not in value
    assert not value.endswith(".session")
    return value


def _apply_modelled_py312_path(monkeypatch) -> None:
    monkeypatch.setattr(telegram_mt, "Path", _Py312PosixPath)


def _maybe_model_py312(monkeypatch, model_312: bool) -> None:
    if model_312:
        _apply_modelled_py312_path(monkeypatch)


def _channel_settings(tmp_path, session: str) -> Settings:
    return Settings(data_dir=tmp_path, pool_mode="channel", contribution=True,
                    telegram_api_id=123, telegram_api_hash="a" * 32,
                    telegram_bot_token="123456:test",
                    telegram_channel_id=-1001234567890,
                    telegram_session=session)


def _record_client(store, monkeypatch):
    calls = []

    def record(factory, session):
        calls.append((factory, session))
        return _RECORDED

    monkeypatch.setattr(store, "_new_client", record)
    return calls


def _long_repro() -> str:
    # The parent's live repro: a 353-character bare name, i.e. exactly the
    # length of a serialized IPv4 StringSession (352 payload chars + version).
    return "1" + "A" * 352


# --------------------------------------------------------------------------
# Model fidelity: the modelled <= 3.12 semantics reproduce the live crash, and
# the shipping interpreter (3.14) documents why it hid the regression locally.
# --------------------------------------------------------------------------

def test_modelled_py312_exists_raises_enametoolong_for_bare_long_names(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    name = _long_repro()
    with pytest.raises(OSError) as stat_error:
        os.stat(name)  # the OS fact underlying everything below
    assert stat_error.value.errno == errno.ENAMETOOLONG
    with pytest.raises(OSError) as exists_error:
        _Py312PosixPath(name).exists()
    assert exists_error.value.errno == errno.ENAMETOOLONG
    # short unknown names still simply report "not on disk"
    assert _Py312PosixPath("definitely-missing-short").exists() is False


def test_real_pathlib_documents_the_hiding_below_the_crash(tmp_path, monkeypatch):
    """On <= 3.12 the real pathlib raises here (uv-proven on 3.12.13); on 3.14
    it returns False (documented "Changed in version 3.14" + live-verified on
    3.14.4), which is exactly why local suites stayed green. 3.13 is not
    pinned: its exists() still raises some OSErrors per the same changelog."""
    monkeypatch.chdir(tmp_path)
    if sys.version_info >= (3, 14):
        assert pathlib.Path(_long_repro()).exists() is False
    elif sys.version_info >= (3, 13):
        pytest.skip("3.13 exists() error semantics unpinned: some OSErrors"
                    " still raise per the pathlib changelog; ENAMETOOLONG case"
                    " not verified on 3.13")
    else:
        with pytest.raises(OSError) as excinfo:
            pathlib.Path(_long_repro()).exists()
        assert excinfo.value.errno == errno.ENAMETOOLONG


# --------------------------------------------------------------------------
# RED -> GREEN: the serialized session is recognized as serialized, not as a
# path, and reaches `_new_client` as a real StringSession object.
# --------------------------------------------------------------------------

def test_looks_like_path_treats_serialized_session_as_serialized_under_py312(monkeypatch):
    _apply_modelled_py312_path(monkeypatch)
    assert telegram_mt._looks_like_path(_synthetic_session()) is False


def test_looks_like_path_survives_the_parent_repro_input_under_py312(monkeypatch):
    _apply_modelled_py312_path(monkeypatch)
    assert telegram_mt._looks_like_path(_long_repro()) is False


def test_serialized_session_reaches_new_client_without_creating_a_session_file(
        monkeypatch, tmp_path):
    value = _synthetic_session()
    _apply_modelled_py312_path(monkeypatch)
    store = telegram_mt.TelegramMTProtoStore(_channel_settings(tmp_path, value))
    calls = _record_client(store, monkeypatch)

    assert store._session() is _RECORDED
    assert store._session_file is None  # never resolved onto disk
    [(factory, session)] = calls
    assert factory is telethon.TelegramClient  # real class, never constructed
    assert isinstance(session, StringSession)
    assert session.save() == value
    assert session.dc_id == 2
    assert session.server_address == "127.0.0.1"
    assert session.port == 443


def test_serialized_session_is_translated_neither_to_pool_error_nor_path(
        monkeypatch, tmp_path):
    """The same input, put through the old probe under <= 3.12, translated
    into PoolError(503, "telegram_unavailable"); it must not be, post-fix."""
    value = _synthetic_session()
    _apply_modelled_py312_path(monkeypatch)
    store = telegram_mt.TelegramMTProtoStore(_channel_settings(tmp_path, value))
    calls = _record_client(store, monkeypatch)
    try:
        assert store._session() is _RECORDED
    except telegram_mt.PoolError as exc:
        pytest.fail(f"serialized session surfaced as a pool error: {exc}")
    assert calls  # and the serialized session was handed to the client seam


# --------------------------------------------------------------------------
# Preservation: explicit path hints keep winning (before any filesystem probe),
# and real files still resolve through the path branch under BOTH semantics.
# --------------------------------------------------------------------------

def test_explicit_path_hints_still_win_before_the_filesystem(monkeypatch):
    _apply_modelled_py312_path(monkeypatch)
    assert telegram_mt._looks_like_path("some/missing-dir/file.session") is True
    assert telegram_mt._looks_like_path("node.session") is True
    assert telegram_mt._looks_like_path("C:\\opus\\node.session") is True
    # a short bare string is serialized-shaped too (version char absent -> any
    # invalid session crashes at StringSession() today; the helper keeps
    # classifying it as serialized, as before)
    assert telegram_mt._looks_like_path("bare-token") is False


def test_bare_short_string_is_serialized_under_both_path_semantics(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert telegram_mt._looks_like_path("bare-token") is False
    _apply_modelled_py312_path(monkeypatch)
    assert telegram_mt._looks_like_path("bare-token") is False


@pytest.mark.parametrize("model_312", [False, True],
                         ids=["local-pathlib", "modelled-py312"])
def test_session_filename_suffix_still_secures_and_uses_the_file(
        tmp_path, monkeypatch, model_312):
    _maybe_model_py312(monkeypatch, model_312)
    monkeypatch.chdir(tmp_path)
    store = telegram_mt.TelegramMTProtoStore(_channel_settings(tmp_path, "node.session"))
    calls = _record_client(store, monkeypatch)

    assert store._session() is _RECORDED
    assert store._session_file == pathlib.Path("node.session")
    [(factory, session)] = calls
    assert factory is telethon.TelegramClient
    assert session == str(pathlib.Path("node.session"))
    created = pathlib.Path("node.session")
    assert created.exists()
    assert stat.S_IMODE(os.stat(created).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(created.parent).st_mode) == 0o700


@pytest.mark.parametrize("model_312", [False, True],
                         ids=["local-pathlib", "modelled-py312"])
def test_relative_existing_extensionless_file_still_treated_as_path(
        tmp_path, monkeypatch, model_312):
    _maybe_model_py312(monkeypatch, model_312)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "stored-session").write_bytes(b"")
    store = telegram_mt.TelegramMTProtoStore(_channel_settings(tmp_path, "stored-session"))
    calls = _record_client(store, monkeypatch)

    assert store._session() is _RECORDED
    assert store._session_file == pathlib.Path("stored-session")
    assert calls[0][1] == str(pathlib.Path("stored-session"))


@pytest.mark.parametrize("model_312", [False, True],
                         ids=["local-pathlib", "modelled-py312"])
def test_absolute_existing_path_still_treated_as_path(tmp_path, monkeypatch, model_312):
    _maybe_model_py312(monkeypatch, model_312)
    absolute = tmp_path / "absolute-stored"
    absolute.write_bytes(b"")
    store = telegram_mt.TelegramMTProtoStore(
        _channel_settings(tmp_path, str(absolute)))
    calls = _record_client(store, monkeypatch)

    assert store._session() is _RECORDED
    assert store._session_file == pathlib.Path(str(absolute))
    assert calls[0][1] == str(absolute)
