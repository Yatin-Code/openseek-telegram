"""Channel pool tests use an in-memory Telegram transport; no account is required."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import stat
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest
import httpx

from openseek.app import Core, create_api_app
from openseek.cache import Cache, PROFILE
from openseek.channel_pool import ChannelPoolService
from openseek.channel_index import ChannelIndex
from openseek.config import ConfigError, MiB, Settings
from openseek.contribution import TelegramLibrarian
from openseek.db import DB
from openseek.pool import (ChannelPoolClient, ChannelPoolSync, PoolError,
                           _tile_filename)
from openseek.telegram_mt import ChannelMessage, TelegramMTProtoStore
from openseek.scheduler import tile_count
from openseek.setup import _env_assignment, run_setup


def jpeg(color=(12, 34, 56)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (320, 180), color).save(out, "JPEG", quality=75)
    return out.getvalue()


class MemoryChannel:
    def __init__(self):
        self.channel_id = "-100123456"
        self.settings = SimpleNamespace(librarian="", librarian_key="")
        self.messages: list[ChannelMessage] = []
        self.data: dict[int, bytes] = {}
        self.next_id = 1
        self.fail_next_upload = False
        self.deleted: list[int] = []

    def put_tile(self, data, caption, metadata=None, target=None):
        if self.fail_next_upload:
            self.fail_next_upload = False
            raise PoolError(503, "telegram_unavailable")
        name = "tile.jpg"
        if metadata:
            name = _tile_filename(metadata["pts_ms"], metadata["duration_ms"], metadata["n_tiles"])
        message_id = self.next_id
        self.next_id += 1
        self.messages.append(ChannelMessage(message_id, caption, len(data), name))
        self.data[message_id] = bytes(data)
        return message_id

    def get_tile(self, message_id):
        if message_id not in self.data:
            raise PoolError(404, "telegram_message_not_found")
        return self.data[message_id]

    def scan(self, min_id=0, limit=2000):
        return [message for message in self.messages if message.message_id > min_id][:limit]

    def delete_message(self, message_id, target=None):
        self.deleted.append(message_id)
        self.data.pop(message_id, None)
        self.messages = [message for message in self.messages if message.message_id != message_id]
        return True

    def start(self):
        pass

    def close(self):
        pass


def make_db_cache(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    db = DB(root / "openseek.db")
    cache = Cache(root, db, 64 * MiB, 30)
    return db, cache


def seed(cache: Cache, fp: str, k: int, data: bytes, duration_ms: int = 20_000):
    n_tiles = tile_count(duration_ms)
    cache.set_media(fp, duration_ms, n_tiles)
    cache.put_tile(fp, k, k * 10_000, data)
    return k * 10_000, duration_ms, n_tiles


def test_two_independent_channel_instances_share_a_tile_and_rebuild(tmp_path):
    fp = "a" * 64
    data = jpeg()
    store = MemoryChannel()
    db_a, cache_a = make_db_cache(tmp_path / "a")
    db_b, cache_b = make_db_cache(tmp_path / "b")
    try:
        pts, duration, n_tiles = seed(cache_a, fp, 1, data)
        index_a = ChannelIndex(db_a, store, scan_limit=10, refresh_s=10)
        publisher = ChannelPoolSync(db_a, cache_a, store, index_a)
        publisher.enqueue(fp, 1)
        publisher._publish(fp, 1, pts, duration, n_tiles)
        assert index_a.lookup(fp, PROFILE, 1)["sha256"] == hashlib.sha256(data).hexdigest()

        seed(cache_b, fp, 1, data)
        index_b = ChannelIndex(db_b, store, scan_limit=10, refresh_s=10)
        assert index_b.refresh(force=True)["indexed"] == 1
        client_b = ChannelPoolClient(cache_b, index_b, store)
        assert client_b.get_tile(fp, 1) == (data, pts)
        assert cache_b.tile_bytes(fp, 1) == data

        db_c, _cache_c = make_db_cache(tmp_path / "c")
        try:
            index_c = ChannelIndex(db_c, store, scan_limit=10, refresh_s=10)
            assert index_c.rebuild()["indexed"] == 1
            assert index_c.lookup(fp, PROFILE, 1)["message_id"] == 1
        finally:
            db_c.close()
    finally:
        db_a.close()
        db_b.close()


def test_channel_mode_serves_pool_routes_from_index(tmp_path):
    store = MemoryChannel()
    settings = Settings(data_dir=tmp_path, pool_mode="channel", contribution=True,
                        telegram_api_id=123, telegram_api_hash="a" * 32,
                        telegram_bot_token="123456:test", telegram_channel_id=store.channel_id)
    core = Core(settings, pool_storage=store)
    fp = hashlib.sha256(b"channel mode nvmt source").hexdigest()
    episode_key = hashlib.sha256(b"channel mode episode key").hexdigest()
    tiles = [jpeg((12, 34, 56)), jpeg((56, 34, 12))]
    for k, data in enumerate(tiles):
        digest = hashlib.sha256(data).hexdigest()
        store.put_tile(data, f"openseek:v1:{fp}:{PROFILE}:{k}:{digest}",
                       {"pts_ms": k * 10_000, "duration_ms": 20_000, "n_tiles": 2})
    core.channel_index.refresh(force=True)

    device_token = "A" * 43
    core.auth.register("Channel TV", hashlib.sha256(device_token.encode()).hexdigest())
    app = create_api_app(core)

    async def requests():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://openseek.test") as client:
            headers = {"Authorization": f"Bearer {device_token}"}
            manifest = await client.get(f"/v1/pool/{fp}/manifest?profile={PROFILE}", headers=headers)
            tile = await client.get(f"/v1/pool/{fp}/tiles/1.jpg?profile={PROFILE}", headers=headers)
            full = await client.get(f"/v1/pool/{fp}/nvmt?profile=nvmt-v1-g5&episode_key={episode_key}",
                                    headers=headers)
            sparse = await client.get(f"/v1/pool/{fp}/nvmt?profile=nvmt-v1-g5&slots=0,2", headers=headers)
            candidates = await client.get(f"/v1/pool/episodes/{episode_key}?duration_ms=20000", headers=headers)
            missing = await client.get(f"/v1/pool/{'f' * 64}/nvmt?profile=nvmt-v1-g5", headers=headers)
        return manifest, tile, full, sparse, candidates, missing

    try:
        manifest, tile, full, sparse, candidates, missing = asyncio.run(requests())
        assert manifest.status_code == 200
        assert [item["k"] for item in manifest.json()["tiles"]] == [0, 1]
        assert tile.status_code == 200 and tile.content == tiles[1]
        assert full.status_code == 200
        assert sparse.status_code == 200
        assert full.content == sparse.content
        assert candidates.status_code == 200
        assert [item["fp"] for item in candidates.json()["candidates"]] == [fp]
        assert missing.status_code == 404 and missing.json()["error"] == "not_found"
        assert core.jobs.running_sources() == []
    finally:
        core.stop()


def test_first_writer_wins_and_losing_outbox_is_cleared(tmp_path):
    fp = "b" * 64
    store = MemoryChannel()
    db_a, cache_a = make_db_cache(tmp_path / "a")
    db_b, cache_b = make_db_cache(tmp_path / "b")
    try:
        original = jpeg((1, 2, 3))
        pts, duration, n_tiles = seed(cache_a, fp, 0, original)
        index_a = ChannelIndex(db_a, store)
        sync_a = ChannelPoolSync(db_a, cache_a, store, index_a)
        sync_a.enqueue(fp, 0)
        sync_a._publish(fp, 0, pts, duration, n_tiles)

        conflicting = jpeg((90, 80, 70))
        seed(cache_b, fp, 0, conflicting)
        index_b = ChannelIndex(db_b, store)
        index_b.refresh(force=True)
        sync_b = ChannelPoolSync(db_b, cache_b, store, index_b)
        sync_b.enqueue(fp, 0)
        sync_b._publish(fp, 0, pts, duration, n_tiles)
        assert len(store.messages) == 1
        assert store.get_tile(index_b.lookup(fp, PROFILE, 0)["message_id"]) == original
        assert sync_b.pending() == 0
    finally:
        db_a.close()
        db_b.close()


def test_bad_captions_and_digest_mismatch_are_ignored(tmp_path):
    fp = "c" * 64
    data = jpeg()
    store = MemoryChannel()
    store.messages = [
        ChannelMessage(1, "not a tile caption", len(data), "tile.jpg"),
        ChannelMessage(2, f"openseek:v1:{fp}:{PROFILE}:0:{'0' * 64}", len(data),
                       _tile_filename(0, 10_000, 1)),
        ChannelMessage(3, f"openseek:v1:{fp}:{PROFILE}:0:{hashlib.sha256(data).hexdigest()}", len(data),
                       _tile_filename(0, 10_000, 1)),
    ]
    store.data = {1: data, 2: data, 3: data}
    store.next_id = 4
    db, cache = make_db_cache(tmp_path / "index")
    try:
        index = ChannelIndex(db, store)
        index.refresh(force=True)
        assert index.lookup(fp, PROFILE, 0)["message_id"] == 3
        client = ChannelPoolClient(cache, index, store)
        assert client.get_tile(fp, 0) == (data, 0)
        assert not db.q("SELECT 1 FROM pool_channel_tiles WHERE message_id=1")
        assert not db.q("SELECT 1 FROM pool_channel_tiles WHERE message_id=2")
    finally:
        db.close()


def test_channel_outbox_retries_a_transient_upload_failure(tmp_path):
    fp = "d" * 64
    store = MemoryChannel()
    db, cache = make_db_cache(tmp_path / "retry")
    try:
        data = jpeg()
        pts, duration, n_tiles = seed(cache, fp, 0, data)
        index = ChannelIndex(db, store)
        sync = ChannelPoolSync(db, cache, store, index)
        sync.enqueue(fp, 0)
        store.fail_next_upload = True
        with pytest.raises(PoolError):
            sync._publish(fp, 0, pts, duration, n_tiles)
        assert sync.pending() == 1
        sync._publish(fp, 0, pts, duration, n_tiles)
        assert sync.pending() == 0
        assert index.lookup(fp, PROFILE, 0) is not None
    finally:
        db.close()


def test_pool_mode_compatibility_and_local_only(tmp_path):
    host = Settings.from_env({
        "OPENSEEK_POOL_ENABLED": "1",
        "OPENSEEK_POOL_READ_KEY": "r" * 32,
        "OPENSEEK_POOL_WRITE_KEY": "w" * 32,
        "OPENSEEK_TELEGRAM_BOT_TOKEN": "123456:test",
        "OPENSEEK_TELEGRAM_CHANNEL_ID": "-100123",
    })
    assert host.effective_pool_mode == "host" and host.pool_host_enabled

    channel = Settings.from_env({
        "OPENSEEK_POOL_MODE": "channel",
        "OPENSEEK_TELEGRAM_API_ID": "12345",
        "OPENSEEK_TELEGRAM_API_HASH": "a" * 32,
        "OPENSEEK_TELEGRAM_SESSION_FILE": str(tmp_path / "me.session"),
        "OPENSEEK_TELEGRAM_CHANNEL_ID": "-100456",
        "OPENSEEK_POOL_URL": "not a URL",
        "OPENSEEK_POOL_READ_KEY": "ignored",
        "OPENSEEK_POOL_ENABLED": "invalid but ignored",
    })
    assert channel.effective_pool_mode == "channel"

    local = Settings.from_env({
        "OPENSEEK_POOL_URL": "invalid and ignored",
        "OPENSEEK_POOL_READ_KEY": "short",
        "OPENSEEK_TELEGRAM_API_ID": "invalid and ignored",
    })
    assert local.effective_pool_mode == "local"

    local_core = Core(local.with_(data_dir=tmp_path / "local"))
    try:
        assert local_core.channel_store is None
        assert local_core.pool_client is None and local_core.pool_sync is None
        assert local_core.pool is None
    finally:
        local_core.stop()

    channel_core = Core(channel.with_(data_dir=tmp_path / "channel"), pool_storage=MemoryChannel())
    try:
        assert isinstance(channel_core.pool, ChannelPoolService)
        assert isinstance(channel_core.pool_client, ChannelPoolClient)
    finally:
        channel_core.stop()


def test_pool_channel_writable_override_validation():
    with pytest.raises(ConfigError, match="POOL_CHANNEL_WRITABLE"):
        Settings.from_env({"OPENSEEK_POOL_MODE": "channel", "OPENSEEK_POOL_CHANNEL_WRITABLE": "sometimes"})


class FakeTelethonClient:
    def __init__(self):
        self.messages = {}
        self.next_id = 1
        self.connected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def sign_in(self, **_kw):
        return None

    async def is_user_authorized(self):
        return True

    async def get_entity(self, channel_id):
        return channel_id

    async def get_permissions(self, _entity, _user):
        return SimpleNamespace(send_messages=getattr(self, "can_send", True), post_messages=True)

    async def send_file(self, _entity, file, caption, **_kw):
        source, filename = file
        data = source.read()
        mid = self.next_id
        self.next_id += 1
        doc = SimpleNamespace(size=len(data), data=data,
                               attributes=[SimpleNamespace(file_name=filename)])
        message = SimpleNamespace(id=mid, message=caption, document=doc)
        self.messages[mid] = message
        return message

    async def get_messages(self, _entity, ids):
        return self.messages.get(ids)

    async def iter_messages(self, _entity, min_id=0, limit=None, reverse=False, **_kw):
        values = [m for mid, m in sorted(self.messages.items()) if mid > min_id]
        if not reverse:
            values.reverse()
        for message in values[:limit]:
            yield message

    async def iter_download(self, document, **_kw):
        yield document.data

    async def delete_messages(self, _entity, ids):
        for mid in ids:
            self.messages.pop(mid, None)


def test_mtproto_store_sync_facade_upload_scan_download(tmp_path):
    settings = Settings(data_dir=tmp_path, pool_mode="channel", contribution=True,
                        telegram_api_id=123, telegram_api_hash="a" * 32,
                        telegram_bot_token="123456:test", telegram_channel_id="-100123")
    fake = FakeTelethonClient()
    store = TelegramMTProtoStore(settings, client_factory=lambda *_args, **_kwargs: fake)
    data = jpeg()
    digest = hashlib.sha256(data).hexdigest()
    caption = f"openseek:v1:{'e' * 64}:{PROFILE}:0:{digest}"
    try:
        store.start()
        message_id = store.put_tile(data, caption, {"pts_ms": 0, "duration_ms": 10_000, "n_tiles": 1})
        assert store.get_tile(message_id) == data
        messages = store.scan(0, 10)
        assert messages == [ChannelMessage(message_id, caption, len(data), _tile_filename(0, 10_000, 1))]
    finally:
        store.close()


@pytest.mark.parametrize("override,expected", [("auto", False), ("1", True), ("0", False)])
def test_mtproto_store_allows_read_only_channel_and_reports_write_override(tmp_path, override, expected):
    settings = Settings(data_dir=tmp_path, pool_mode="channel", contribution=True,
                        telegram_api_id=123, telegram_api_hash="a" * 32,
                        telegram_bot_token="123456:test", telegram_channel_id="-100123",
                        pool_channel_writable=override)
    fake = FakeTelethonClient()
    fake.can_send = False
    store = TelegramMTProtoStore(settings, client_factory=lambda *_args, **_kwargs: fake)
    try:
        store.start()
        assert store.detected_can_post is False
        assert store.can_post is expected
        assert store.entity_type == "megagroup"
    finally:
        store.close()


class FakeLibrarianStore:
    def __init__(self, data):
        self.data = data
        self._entity = "channel"
        self._client = FakeTelethonClient()

    async def _download_bounded(self, _document):
        return self.data

    async def _retry(self, operation):
        return await operation()


class FakeEvent:
    def __init__(self, caption, data, filename, *, sender_bot=False):
        doc = SimpleNamespace(size=len(data), attributes=[SimpleNamespace(file_name=filename)])
        self.message = SimpleNamespace(document=doc, message=caption)
        self.sender_id = 456
        self.is_private = True
        self.sender_bot = sender_bot
        self.deleted = False

    async def get_sender(self):
        return SimpleNamespace(bot=self.sender_bot)

    async def delete(self):
        self.deleted = True


def test_librarian_reposts_valid_user_tile_and_rejects_bot_sender(tmp_path):
    fp, data = "f" * 64, jpeg()
    store = FakeLibrarianStore(data)
    db, _cache = make_db_cache(tmp_path / "librarian")
    try:
        index = ChannelIndex(db, MemoryChannel())
        librarian = TelegramLibrarian(store, index)
        caption = f"openseek:v1:{fp}:{PROFILE}:0:{hashlib.sha256(data).hexdigest()}"
        name = _tile_filename(0, 10_000, 1)
        event = FakeEvent(caption, data, name)
        asyncio.run(librarian.handle_message(event, store))
        assert event.deleted
        assert index.lookup(fp, PROFILE, 0) is not None
        assert store._client.next_id == 2

        bot_event = FakeEvent(caption, data, name, sender_bot=True)
        bot_event.sender_id = 789
        asyncio.run(librarian.handle_message(bot_event, store))
        assert bot_event.deleted
        assert store._client.next_id == 2
    finally:
        db.close()


def test_setup_skip_writes_a_separate_private_env_file(tmp_path):
    path = tmp_path / "openseek.env"
    answers = iter(["3"])
    assert run_setup(input_fn=lambda _prompt: next(answers), print_fn=lambda _msg: None,
                     output_path=path) == 0
    assert "OPENSEEK_POOL_ENABLED=0" in path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    original = path.read_bytes()
    assert run_setup(input_fn=lambda _prompt: "3", print_fn=lambda _msg: None,
                     output_path=path) == 1
    assert path.read_bytes() == original


def test_setup_quotes_shell_sensitive_environment_values():
    value = "folder with spaces/'$(touch /tmp/nope)"
    assignment = _env_assignment("OPENSEEK_TELEGRAM_SESSION_FILE", value)
    assert assignment == "OPENSEEK_TELEGRAM_SESSION_FILE='folder with spaces/'\"'\"'$(touch /tmp/nope)'"
