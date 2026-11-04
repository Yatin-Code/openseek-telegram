"""Paired-device bearer token handling (unit level)."""
from __future__ import annotations

from openseek.auth import DeviceAuth
from openseek.db import DB
from openseek.pairing import new_token, token_hash


def test_token_hash_only_and_revocation(tmp_path):
    db = DB(tmp_path / "x.db")
    auth = DeviceAuth(db)
    tok = new_token()
    dev = auth.register("Shield", token_hash(tok))
    assert auth.authenticate(f"Bearer {tok}") == dev
    assert auth.authenticate(f"Bearer {new_token()}") is None
    assert auth.authenticate(tok) is None and auth.authenticate("Bearer ") is None
    assert auth.authenticate("Bearer " + tok[:-1] + "!") is None
    db.close()
    raw = (tmp_path / "x.db").read_bytes() + b"".join(p.read_bytes() for p in tmp_path.glob("x.db-*"))
    assert tok.encode() not in raw and token_hash(tok).encode() in raw
    db = DB(tmp_path / "x.db")
    auth = DeviceAuth(db)
    assert auth.authenticate(f"Bearer {tok}") == dev
    assert auth.revoke(dev)
    assert auth.authenticate(f"Bearer {tok}") is None
    db.close()
