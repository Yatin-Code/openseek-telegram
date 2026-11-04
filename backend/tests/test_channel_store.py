"""Parity tests for the rebuildable channel-index store."""
from __future__ import annotations

import hashlib

from openseek.cache import PROFILE
from openseek.db import DB
from openseek.store import ChannelStore


def tile(fp="a" * 64, k=0, pts_ms=0, duration_ms=20_000, n_tiles=2,
         size=100, sha256=None, message_id=1):
    return {"fp": fp, "profile": PROFILE, "k": k, "pts_ms": pts_ms,
            "duration_ms": duration_ms, "n_tiles": n_tiles, "size": size,
            "sha256": sha256 or hashlib.sha256(bytes([k])).hexdigest(),
            "message_id": message_id, "created": 1.0}


def hints(fp="a" * 64, **overrides):
    row = {"fp": fp, "profile": PROFILE, "imdb": "tt1234567", "season": 0, "episode": 0,
           "duration_ms": 20_000, "size_bytes": 1_000_000, "filename_norm": "movie.2024",
           "n_tiles": 2, "grid": 5_000, "created": 10.0}
    row.update(overrides)
    return row


def test_state_round_trip(tmp_path):
    db = DB(tmp_path / "index.db")
    try:
        store = ChannelStore(db)
        assert store.state("chan") is None
        store.set_state("chan", 5, 1.5)
        assert store.state("chan") == (5, 1.5)
        store.set_state("chan", 9, 2.5)
        assert store.state("chan") == (9, 2.5)
    finally:
        db.close()


def test_record_tiles_is_idempotent_and_fills_missing_metadata(tmp_path):
    db = DB(tmp_path / "index.db")
    try:
        store = ChannelStore(db)
        first = tile(k=0, message_id=1)
        assert store.record_tiles([first], channel_id="chan", last_message_id=1, scanned_at=3.0) == 1
        assert store.state("chan") == (1, 3.0)
        assert store.get_tile(first["fp"], PROFILE, 0)["message_id"] == 1

        # A second scan of the same tile is a no-op.
        assert store.record_tiles([first], channel_id="chan", last_message_id=1, scanned_at=4.0) == 0
        assert store.state("chan") == (1, 4.0)

        # Same digest is accepted; a conflicting digest is rejected.
        assert store.record_tile(first) is True
        assert store.record_tile({**first, "sha256": "f" * 64}) is False

        # Older host uploads can be enriched once by a later filename-bearing post.
        empty = tile(k=1, duration_ms=0, n_tiles=0, message_id=2)
        assert store.record_tiles([empty], channel_id="chan", last_message_id=2, scanned_at=5.0) == 1
        enriched = {**empty, "pts_ms": 10_000, "duration_ms": 20_000, "n_tiles": 2, "message_id": 3}
        assert store.record_tiles([enriched], channel_id="chan", last_message_id=3, scanned_at=6.0) == 0
        got = store.get_tile(empty["fp"], PROFILE, 1)
        assert (got["pts_ms"], got["duration_ms"], got["n_tiles"], got["message_id"]) == (10_000, 20_000, 2, 2)
    finally:
        db.close()


def test_tiles_for_package_and_forget(tmp_path):
    db = DB(tmp_path / "index.db")
    try:
        store = ChannelStore(db)
        store.record_tiles([tile(k=1, message_id=2), tile(k=0, message_id=1)],
                           channel_id="chan", last_message_id=2, scanned_at=1.0)
        rows = store.tiles_for_package("a" * 64, PROFILE)
        assert [row["k"] for row in rows] == [0, 1]
        assert rows[0]["message_id"] == 1 and rows[0]["n_tiles"] == 2

        assert store.forget_tile("a" * 64, PROFILE, 0, message_id=99) is False
        assert store.forget_tile("a" * 64, PROFILE, 0, message_id=1) is True
        assert [row["k"] for row in store.tiles_for_package("a" * 64, PROFILE)] == [1]
    finally:
        db.close()


def test_hints_round_trip_and_candidate_rows(tmp_path):
    db = DB(tmp_path / "index.db")
    try:
        store = ChannelStore(db)
        store.record_tiles([tile(k=0, message_id=1), tile(k=1, message_id=2)],
                           channel_id="chan", last_message_id=2, scanned_at=1.0)
        assert store.get_hints("a" * 64, PROFILE) is None
        assert store.put_hints(hints()) is True
        assert store.get_hints("a" * 64, PROFILE) == (
            "tt1234567", 0, 0, 20_000, 1_000_000, "movie.2024", 2, 5_000)

        # An upsert updates the row instead of duplicating it.
        assert store.put_hints(hints(imdb="tt7654321", filename_norm="other.2024")) is True
        assert store.get_hints("a" * 64, PROFILE)[0] == "tt7654321"

        rows = store.hint_rows_for_candidates(23_000, 90_000, None)
        assert len(rows) == 1
        assert rows[0][0] == "a" * 64 and rows[0][11] == 2 and rows[0][12] == 0
        assert store.hint_rows_for_candidates(23_000, 90_000, "a" * 64) == []
        assert store.hint_rows_for_candidates(200_000, 90_000, None) == []
    finally:
        db.close()


def test_episode_index_round_trip(tmp_path):
    db = DB(tmp_path / "index.db")
    try:
        store = ChannelStore(db)
        store.record_tiles([tile(k=0, message_id=1), tile(k=1, message_id=2)],
                           channel_id="chan", last_message_id=2, scanned_at=1.0)
        store.put_hints(hints())
        episode_key = "b" * 64
        assert store.episode_hint_key(episode_key, 20_000) is None
        store.put_episode_package(episode_key, "a" * 64, PROFILE, 20_000, 4.0)
        assert store.episode_hint_key(episode_key, 20_000) == ("tt1234567", 0, 0)
        assert store.episode_packages_for_key(episode_key, 20_000, 90_000, 8) == [
            ("a" * 64, PROFILE, 20_000, 4.0, 2)]
    finally:
        db.close()


def test_clear_for_rebuild_removes_all_channel_index_tables(tmp_path):
    db = DB(tmp_path / "index.db")
    try:
        store = ChannelStore(db)
        store.record_tiles([tile()], channel_id="chan", last_message_id=1, scanned_at=1.0)
        store.put_hints(hints())
        store.put_episode_package("b" * 64, "a" * 64, PROFILE, 20_000, 1.0)
        store.clear_channel("chan")
        assert store.state("chan") is None
        assert store.tiles_for_package("a" * 64, PROFILE) == []
        assert store.get_hints("a" * 64, PROFILE) is None
        assert store.episode_packages_for_key("b" * 64, 20_000, 90_000, 8) == []
        assert store.counts() == {"tiles": 0, "hints": 0, "episode_packages": 0}
    finally:
        db.close()


def test_put_package_index_writes_hints_and_episode_together(tmp_path):
    db = DB(tmp_path / "index.db")
    try:
        store = ChannelStore(db)
        store.record_tiles([tile(k=0, message_id=1)], channel_id="chan",
                           last_message_id=1, scanned_at=1.0)
        store.put_package_index(hints(), "b" * 64)
        assert store.get_hints("a" * 64, PROFILE) == (
            "tt1234567", 0, 0, 20_000, 1_000_000, "movie.2024", 2, 5_000)
        assert store.episode_hint_key("b" * 64, 20_000) == ("tt1234567", 0, 0)
    finally:
        db.close()


def test_counts(tmp_path):
    db = DB(tmp_path / "index.db")
    try:
        store = ChannelStore(db)
        store.record_tiles([tile(k=0), tile(k=1, message_id=2)],
                           channel_id="chan", last_message_id=2, scanned_at=1.0)
        store.put_hints(hints())
        store.put_episode_package("b" * 64, "a" * 64, PROFILE, 20_000, 1.0)
        assert store.counts() == {"tiles": 2, "hints": 1, "episode_packages": 1}
    finally:
        db.close()
