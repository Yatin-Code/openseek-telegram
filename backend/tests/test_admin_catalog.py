"""Authenticated admin catalog and contributor moderation over the public admin HTTP seam.

Tests drive real Core and real stores (SQLite ChannelStore / PoolService / RemoteChannelStore)
and fake only external providers (Telegram channel transport, Supabase PostgREST transport).
Admin authentication is preserved through the real Guard; routes come from create_admin_app.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json

import httpx
import pytest
from PIL import Image

from openseek.app import Core, create_admin_app
from openseek.cache import PROFILE
from openseek.config import Settings
from openseek.pool import PoolError
from openseek.remote import RemoteChannelStore, RemoteStoreError, SupabaseClient
from openseek.telegram_mt import ChannelMessage

ADMIN_TOKEN = "synthetic-admin-catalog-token"
READ_KEY = "read-key-0000-openseek-test"
WRITE_KEY = "write-key-1111-openseek-test"
SERVICE_KEY = "service-role-key-that-must-not-leak"


def jpeg(color=(20, 40, 60)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (320, 180), color).save(out, "JPEG", quality=75)
    return out.getvalue()


class MemoryChannel:
    """In-memory Telegram channel transport (fake external provider only)."""

    def __init__(self):
        self.channel_id = "-100123456"
        self.can_post = True
        self.messages: list[ChannelMessage] = []
        self.data: dict[int, bytes] = {}
        self.next_id = 1

    def get_tile(self, message_id):
        if message_id not in self.data:
            raise PoolError(404, "telegram_message_not_found")
        return self.data[message_id]

    def scan(self, min_id=0, limit=2000):
        return [m for m in self.messages if m.message_id > min_id][:limit]

    def start(self):
        pass

    def close(self):
        pass

    def set_message_handler(self, handler):
        pass


def admin_guard(core):
    return create_admin_app(core)


async def admin_request(core, method: str, path: str, *, headers=None, json_body=None):
    guard = admin_guard(core)
    transport = httpx.ASGITransport(app=guard, client=("127.0.0.1", 45000))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8788") as client:
        return await client.request(method, path, headers=headers or {}, json=json_body)


async def admin_get(core, path: str):
    return await admin_request(core, "GET", path, headers={"X-OpenSeek-Admin": core.admin_token})


def channel_core(tmp_path, name="channel"):
    store = MemoryChannel()
    settings = Settings(
        data_dir=tmp_path / name,
        port=8787,
        admin_port=8788,
        admin_token=ADMIN_TOKEN,
        pool_mode="channel",
        telegram_api_id=123,
        telegram_api_hash="a" * 32,
        telegram_bot_token="123456:test",
        telegram_channel_id=store.channel_id,
    )
    core = Core(settings, pool_storage=store)
    return core, store


def seed_channel(core, store, fp: str, *, data: bytes | None = None, created: float = 10.0, message_id: int = 1):
    """Seed one hints-committed package via the real ChannelStore."""
    data = data or jpeg()
    db_store = core.channel_index.store
    digest = hashlib.sha256(data).hexdigest()
    db_store.put_hints({
        "fp": fp, "profile": "nvmt-v1-g10", "imdb": "tt1234567", "season": 1, "episode": 2,
        "duration_ms": 20_000, "size_bytes": 1_000_000, "filename_norm": "film.2024",
        "n_tiles": 2, "grid": 10_000, "created": float(created),
    })
    db_store.record_tiles([{
        "fp": fp, "profile": PROFILE, "k": 0, "pts_ms": 0, "duration_ms": 20_000,
        "n_tiles": 2, "size": len(data), "sha256": digest, "message_id": message_id,
        "created": float(created),
    }], channel_id=core.channel_index.channel_id, last_message_id=message_id, scanned_at=float(created))
    store.data[message_id] = data
    return digest


def test_library_channel_sqlite_lists_hints_committed_without_secrets(tmp_path):
    core, store = channel_core(tmp_path)
    fp = "a" * 64
    seed_channel(core, store, fp, created=10.0)
    try:
        resp = asyncio.run(admin_get(core, "/admin/library?limit=24&offset=0"))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["supported"] is True
        assert body["has_more"] is False
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item == {
            "fp": fp, "profile": "nvmt-v1-g10", "filename": "film.2024",
            "imdb": "tt1234567", "season": 1, "episode": 2, "duration_ms": 20_000,
            "n_tiles": 2, "created": 10.0, "thumbnail_available": True,
        }
        text = resp.text
        for leak in ("file_id", "message_id", "file-1", "http", "token", "supabase"):
            assert leak not in text
    finally:
        core.stop()


def test_library_local_only_unsupported(tmp_path):
    core = Core(Settings(data_dir=tmp_path / "solo", port=8787, admin_port=8788, admin_token=ADMIN_TOKEN))
    try:
        resp = asyncio.run(admin_get(core, "/admin/library?limit=24&offset=0"))
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"supported": False, "items": [], "has_more": False}
    finally:
        core.stop()


def test_library_paging_sort_dedup_and_bounds(tmp_path):
    core, store = channel_core(tmp_path)
    fps = []
    for i in range(3):
        fp = hashlib.sha256(f"pkg-{i}".encode()).hexdigest()
        fps.append(fp)
        seed_channel(core, store, fp, created=float(10 + i), message_id=10 + i)
    # Orphan tile without hints must not appear as a catalog package.
    orphan_fp = hashlib.sha256(b"orphan").hexdigest()
    orphan_data = jpeg()
    core.channel_index.store.record_tiles([{
        "fp": orphan_fp, "profile": PROFILE, "k": 0, "pts_ms": 0, "duration_ms": 20_000,
        "n_tiles": 2, "size": len(orphan_data),
        "sha256": hashlib.sha256(orphan_data).hexdigest(), "message_id": 99, "created": 99.0,
    }], channel_id=core.channel_index.channel_id, last_message_id=99, scanned_at=99.0)
    store.data[99] = orphan_data
    try:
        first = asyncio.run(admin_get(core, "/admin/library?limit=2&offset=0"))
        assert first.status_code == 200, first.text
        b1 = first.json()
        assert b1["supported"] is True and b1["has_more"] is True
        assert len(b1["items"]) == 2
        # Most recent first.
        assert b1["items"][0]["created"] == 12.0
        assert orphan_fp not in [it["fp"] for it in b1["items"]]

        second = asyncio.run(admin_get(core, "/admin/library?limit=2&offset=2"))
        b2 = second.json()
        assert len(b2["items"]) == 1 and b2["has_more"] is False
        assert orphan_fp not in [it["fp"] for it in b2["items"]]

        # Bounds: limit clamped to 1..100, offset clamped to >=0, non-integer rejected.
        clamped = asyncio.run(admin_get(core, "/admin/library?limit=500&offset=-5"))
        assert clamped.status_code == 200, clamped.text
        assert len(clamped.json()["items"]) == 3
        bad = asyncio.run(admin_get(core, "/admin/library?limit=abc"))
        assert bad.status_code == 400
        assert bad.json() == {"error": "bad_request"}
    finally:
        core.stop()


def test_library_requires_admin_auth(tmp_path):
    core, store = channel_core(tmp_path)
    seed_channel(core, store, "a" * 64)
    try:
        unauth = asyncio.run(admin_request(core, "GET", "/admin/library"))
        assert unauth.status_code == 403
        wrong = asyncio.run(admin_request(core, "GET", "/admin/library", headers={"X-OpenSeek-Admin": "wrong"}))
        assert wrong.status_code == 403
    finally:
        core.stop()


def test_thumbnail_returns_first_tile_and_headers(tmp_path):
    core, store = channel_core(tmp_path)
    fp = "a" * 64
    data = jpeg()
    seed_channel(core, store, fp, data=data)
    try:
        resp = asyncio.run(admin_get(core, f"/admin/library/{fp}/thumbnail?profile=nvmt-v1-g10"))
        assert resp.status_code == 200, resp.text
        assert resp.content == data
        assert resp.headers["Content-Type"] == "image/jpeg"
        assert "private" in resp.headers["Cache-Control"]
        assert "no-store" in resp.headers["Cache-Control"]
    finally:
        core.stop()


def test_thumbnail_validates_and_404s(tmp_path):
    core, store = channel_core(tmp_path)
    seed_channel(core, store, "a" * 64)
    try:
        bad_fp = asyncio.run(admin_get(core, "/admin/library/nothex/thumbnail?profile=nvmt-v1-g10"))
        assert bad_fp.status_code in (400, 404)
        bad_profile = asyncio.run(admin_get(core, "/admin/library/" + "a" * 64 + "/thumbnail?profile=bad"))
        assert bad_profile.status_code in (400, 404)
        missing_profile = asyncio.run(admin_get(core, "/admin/library/" + "a" * 64 + "/thumbnail"))
        assert missing_profile.status_code in (400, 404)
        absent = asyncio.run(admin_get(core, "/admin/library/" + "b" * 64 + "/thumbnail?profile=nvmt-v1-g10"))
        assert absent.status_code == 404
    finally:
        core.stop()


def test_thumbnail_requires_admin_auth(tmp_path):
    core, store = channel_core(tmp_path)
    seed_channel(core, store, "a" * 64)
    try:
        resp = asyncio.run(admin_request(core, "GET", "/admin/library/" + "a" * 64 + "/thumbnail?profile=nvmt-v1-g10"))
        assert resp.status_code == 403
    finally:
        core.stop()


def test_contributors_lists_same_db_as_cli_and_bounds(tmp_path):
    core, store = channel_core(tmp_path)
    try:
        core.db.sender_quota_inc(42, "work-abc", "2026-11-10")
        core.db.block_pool_sender(42)
        resp = asyncio.run(admin_get(core, "/admin/contributors"))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["supported"] is True
        assert body["quotas"] == [{"sender_id": 42, "work_key": "work-abc", "day": "2026-11-10", "count": 1}]
        assert len(body["blocked"]) == 1
        assert body["blocked"][0]["sender_id"] == 42

        # Bounded pagination is accepted and capped.
        paged = asyncio.run(admin_get(core, "/admin/contributors?limit=1&offset=0"))
        assert paged.status_code == 200, paged.text
        assert len(paged.json()["quotas"]) <= 1
    finally:
        core.stop()


def test_contributors_block_is_idempotent_and_validates_id(tmp_path):
    core, store = channel_core(tmp_path)
    try:
        for _ in range(2):
            put = asyncio.run(admin_request(core, "PUT", "/admin/contributors/7/block", headers={"X-OpenSeek-Admin": core.admin_token}))
            assert put.status_code == 204
        got = asyncio.run(admin_get(core, "/admin/contributors"))
        assert [b["sender_id"] for b in got.json()["blocked"]] == [7]
        for _ in range(2):
            delete = asyncio.run(admin_request(core, "DELETE", "/admin/contributors/7/block", headers={"X-OpenSeek-Admin": core.admin_token}))
            assert delete.status_code == 204
        got2 = asyncio.run(admin_get(core, "/admin/contributors"))
        assert got2.json()["blocked"] == []

        for bad in ("0", "-5", "abc", "999999999999999999999999999", "9" * 5000):
            resp = asyncio.run(admin_request(core, "PUT", f"/admin/contributors/{bad}/block", headers={"X-OpenSeek-Admin": core.admin_token}))
            assert resp.status_code == 400, bad
    finally:
        core.stop()


def test_contributors_requires_auth_and_unsupported_never_writes(tmp_path):
    # Local-only: unsupported, and block writes must be refused.
    core = Core(Settings(data_dir=tmp_path / "solo2", port=8787, admin_port=8788, admin_token=ADMIN_TOKEN))
    try:
        resp = asyncio.run(admin_get(core, "/admin/contributors"))
        assert resp.status_code == 200, resp.text
        assert resp.json()["supported"] is False
        assert resp.json()["quotas"] == [] and resp.json()["blocked"] == []
        put = asyncio.run(admin_request(core, "PUT", "/admin/contributors/5/block", headers={"X-OpenSeek-Admin": core.admin_token}))
        assert put.status_code in (403, 404, 409)
        assert core.db.blocked_pool_senders() == []
        unauth = asyncio.run(admin_request(core, "GET", "/admin/contributors"))
        assert unauth.status_code == 403
    finally:
        core.stop()

    # Vercel profile is scratch: even with a channel index present it is unsupported.
    core2, store2 = channel_core(tmp_path, name="channel2")
    try:
        from dataclasses import replace

        core2.settings = replace(core2.settings, profile="vercel")
        body = asyncio.run(admin_get(core2, "/admin/contributors")).json()
        assert body["supported"] is False
        put = asyncio.run(admin_request(core2, "PUT", "/admin/contributors/5/block", headers={"X-OpenSeek-Admin": core2.admin_token}))
        assert put.status_code in (403, 404, 409)
        assert core2.db.blocked_pool_senders() == []
    finally:
        core2.stop()


def _remote_core(tmp_path, payloads, *, fail_with=None):
    core, store = channel_core(tmp_path, name="remote-core")

    def transport(request: httpx.Request) -> httpx.Response:
        if fail_with is not None:
            raise fail_with
        if request.url.path == "/rest/v1/pool_hints":
            return httpx.Response(200, json=payloads.get("/rest/v1/pool_hints", []), request=request)
        if request.url.path == "/rest/v1/pool_packages":
            return httpx.Response(200, json=payloads.get("/rest/v1/pool_packages", []), request=request)
        if request.url.path == "/rest/v1/pool_channel_tiles":
            return httpx.Response(200, json=payloads.get("/rest/v1/pool_channel_tiles", []), request=request)
        return httpx.Response(200, json=[], request=request)

    client = SupabaseClient("https://project.supabase.co", SERVICE_KEY, transport=httpx.MockTransport(transport))
    # Replace the SQLite store with the real RemoteChannelStore over the fake transport.
    from openseek.channel_index import ChannelIndex

    remote_store = RemoteChannelStore(client)
    core.channel_index = ChannelIndex(remote_store, store, db=None, channel_id=core.channel_index.channel_id)
    from openseek.channel_pool import ChannelPoolService

    core.pool = ChannelPoolService(core.channel_index, "", "")
    core._remote_client = client
    return core, store, remote_store


def test_library_remote_supported_via_postgrest_seam(tmp_path):
    fp = "a" * 64
    hint = {
        "fp": fp, "profile": "nvmt-v1-g10", "imdb": "tt1234567", "season": 0, "episode": 0,
        "duration_ms": 20_000, "size_bytes": None, "filename_norm": "remote.film",
        "n_tiles": 2, "grid": 10_000, "created": 12.0,
    }
    package = {"fp": fp, "profile": PROFILE, "duration_ms": 20_000, "channel_tiles": 2}
    core, _store, _remote = _remote_core(tmp_path, {"/rest/v1/pool_hints": [hint], "/rest/v1/pool_packages": [package]})
    try:
        resp = asyncio.run(admin_get(core, "/admin/library?limit=24&offset=0"))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["supported"] is True
        assert body["items"] == [{
            "fp": fp, "profile": "nvmt-v1-g10", "filename": "remote.film",
            "imdb": "tt1234567", "season": 0, "episode": 0, "duration_ms": 20_000,
            "n_tiles": 2, "created": 12.0, "thumbnail_available": True,
        }]
        assert SERVICE_KEY not in resp.text
        assert "secret-project" not in resp.text
    finally:
        core.stop()
        core._remote_client.close()


def test_library_remote_provider_failure_is_sanitized(tmp_path):
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "PGRST301", "message": "secret-project-detail"}, request=request)

    core, _store = channel_core(tmp_path, name="remote-fail")
    client = SupabaseClient("https://project.supabase.co", SERVICE_KEY, transport=httpx.MockTransport(failing))
    from openseek.channel_index import ChannelIndex
    from openseek.channel_pool import ChannelPoolService

    remote_store = RemoteChannelStore(client)
    core.channel_index = ChannelIndex(remote_store, _store, db=None, channel_id=core.channel_index.channel_id)
    core.pool = ChannelPoolService(core.channel_index, "", "")
    core._remote_client = client
    try:
        resp = asyncio.run(admin_get(core, "/admin/library?limit=24&offset=0"))
        assert resp.status_code == 503
        assert resp.json() == {"error": "pool_backend_unavailable"}
        assert SERVICE_KEY not in resp.text
        assert "secret-project-detail" not in resp.text
        assert "PGRST301" not in resp.text
    finally:
        core.stop()
        client.close()


def test_thumbnail_remote_provider_failure_is_sanitized(tmp_path):
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "PGRST301", "message": "secret-project-detail"}, request=request)

    core, _store = channel_core(tmp_path, name="remote-thumb-fail")
    client = SupabaseClient("https://project.supabase.co", SERVICE_KEY, transport=httpx.MockTransport(failing))
    from openseek.channel_index import ChannelIndex
    from openseek.channel_pool import ChannelPoolService

    remote_store = RemoteChannelStore(client)
    core.channel_index = ChannelIndex(remote_store, _store, db=None, channel_id=core.channel_index.channel_id)
    core.pool = ChannelPoolService(core.channel_index, "", "")
    core._remote_client = client
    try:
        resp = asyncio.run(admin_get(core, "/admin/library/" + "a" * 64 + "/thumbnail?profile=nvmt-v1-g10"))
        assert resp.status_code == 503
        assert resp.json() == {"error": "pool_backend_unavailable"}
        assert SERVICE_KEY not in resp.text
    finally:
        core.stop()
        client.close()
