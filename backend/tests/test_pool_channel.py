"""Channel pool tests use an in-memory Telegram transport; no account is required."""
from __future__ import annotations

import asyncio
import base64
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
from openseek import cli
import openseek.app as app_module
from openseek.config import ConfigError, MiB, Settings
from openseek.contribution import TelegramLibrarian
from openseek.db import DB
from openseek.pool import (ChannelPoolClient, ChannelPoolSync, PoolError, decode_nvmt,
                           _tile_filename, encode_nvmt)
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
        self.can_post = True
        self.settings = SimpleNamespace(librarian="", librarian_key="")
        self.messages: list[ChannelMessage] = []
        self.data: dict[int, bytes] = {}
        self.next_id = 1
        self.fail_next_upload = False
        self.fail_after_uploads: int | None = None
        self.deleted: list[int] = []

    def put_tile(self, data, caption, metadata=None, target=None):
        if self.fail_next_upload:
            self.fail_next_upload = False
            raise PoolError(503, "telegram_unavailable")
        if self.fail_after_uploads is not None:
            if self.fail_after_uploads == 0:
                self.fail_after_uploads = None
                raise PoolError(503, "telegram_unavailable")
            self.fail_after_uploads -= 1
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

    def post_message(self, text, target=None):
        message_id = self.next_id
        self.next_id += 1
        self.messages.append(ChannelMessage(message_id, text, 0, ""))
        return message_id

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


def test_channel_mode_uploads_nvmt_tiles_and_retry_keeps_first_writer(tmp_path):
    store = MemoryChannel()
    settings = Settings(data_dir=tmp_path, pool_mode="channel", contribution=True,
                        pool_write_key="w" * 40, telegram_api_id=123,
                        telegram_api_hash="a" * 32, telegram_bot_token="123456:test",
                        telegram_channel_id=store.channel_id)
    core = Core(settings, pool_storage=store)
    fp = hashlib.sha256(b"channel upload source").hexdigest()
    image_a, image_b = jpeg((11, 22, 33)), jpeg((33, 22, 11))
    package = encode_nvmt(5_000, 4, {0: (0, image_a), 2: (10_000, image_b)})
    app = create_api_app(core)

    async def put():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://openseek.test") as client:
            return await client.put(
                f"/v1/pool/{fp}/nvmt?profile=nvmt-v1-g5&duration_ms=20000&imdb=tt1234567"
                "&season=0&episode=0&size_bytes=1000000&filename=Film.Name.2024.mkv",
                content=package, headers={"Authorization": f"Bearer {'w' * 40}",
                                          "Content-Type": "application/vnd.openseek.nvmt"})

    try:
        store.fail_after_uploads = 1
        with pytest.raises(PoolError, match="telegram_unavailable"):
            core.pool.upload_package(fp, "nvmt-v1-g5", 20_000, package, imdb="tt1234567",
                                     size_bytes=1_000_000, filename="Film.Name.2024.mkv")
        assert core.db.q("SELECT COUNT(*) FROM pool_channel_tiles")[0][0] == 0
        assert sum(bool(message.document_size) for message in store.messages) == 1
        assert sum(message.caption.startswith("openseek:hints:v1:") for message in store.messages) == 0

        # The retry indexes the first Telegram tile, publishes the missing one,
        # and only then records the manifest and complete local hint row.
        response = asyncio.run(put())
        assert response.status_code == 201, response.text
        assert core.db.q("SELECT COUNT(*) FROM pool_channel_tiles")[0][0] == 2
        hint = core.db.q("SELECT imdb,filename_norm,n_tiles,grid FROM pool_hints WHERE fp=?", (fp,))[0]
        assert hint == ("tt1234567", "film.name.2024", 2, 5_000)
        assert sum(message.caption.startswith("openseek:hints:v1:") for message in store.messages) == 1
        package_read, _ = core.pool.package(fp, "nvmt-v1-g5")
        grid, n_slots, tiles = decode_nvmt(fp, "nvmt-v1-g5", 20_000, package_read)
        assert (grid, n_slots, tiles) == (5_000, 4, {0: (0, image_a), 2: (10_000, image_b)})

        duplicate = asyncio.run(put())
        assert duplicate.status_code == 200 and duplicate.json()["duplicate"] is True
        assert sum(bool(message.document_size) for message in store.messages) == 2
        assert sum(message.caption.startswith("openseek:hints:v1:") for message in store.messages) == 1
    finally:
        core.stop()


class FakeProxyTransport(httpx.AsyncBaseTransport):
    def __init__(self, status=201, body=None, *, error=False):
        self.status, self.body, self.error = status, body, error
        self.requests = []

    async def handle_async_request(self, request):
        data = await request.aread()
        self.requests.append((request, data))
        if self.error:
            raise httpx.ConnectError("proxy test failure", request=request)
        payload = self.body if self.body is not None else {"stored": True, "duplicate": False}
        content = json.dumps(payload).encode("utf-8")
        return httpx.Response(self.status, headers={"content-type": "application/json",
                                                    "x-openseek-duration-ms": "20000"},
                              content=content, request=request)


def make_read_only_proxy_core(tmp_path, *, proxy_url="https://pool.example", librarian=""):
    store = MemoryChannel()
    store.can_post = False
    settings = Settings(data_dir=tmp_path, pool_mode="channel", contribution=True,
                        pool_contrib_url=proxy_url, pool_write_key="w" * 32, pool_timeout_s=2,
                        librarian=librarian, telegram_api_id=123, telegram_api_hash="a" * 32,
                        telegram_bot_token="123456:test", telegram_channel_id=store.channel_id)
    core = Core(settings, pool_storage=store)
    token = "B" * 43
    core.auth.register("Proxy tester", hashlib.sha256(token.encode()).hexdigest())
    return core, token


def patch_pool_proxy_transport(monkeypatch, transport):
    real_httpx = httpx
    monkeypatch.setattr(app_module, "httpx", SimpleNamespace(
        AsyncClient=lambda **kwargs: real_httpx.AsyncClient(transport=transport, **kwargs),
        TimeoutException=real_httpx.TimeoutException,
        HTTPError=real_httpx.HTTPError))


def test_read_only_channel_streams_upload_to_configured_intake(monkeypatch, tmp_path):
    transport = FakeProxyTransport()
    patch_pool_proxy_transport(monkeypatch, transport)
    core, token = make_read_only_proxy_core(tmp_path)
    fp = hashlib.sha256(b"proxy upload").hexdigest()
    payload = b"bounded nvmt package bytes"
    app = create_api_app(core)

    async def put():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://openseek.test") as client:
            return await client.put(
                f"/v1/pool/{fp}/nvmt?profile=nvmt-v1-g5&duration_ms=20000&imdb=tt1234567",
                content=payload,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/vnd.openseek.nvmt"})

    try:
        response = asyncio.run(put())
        assert response.status_code == 201
        assert response.json()["stored"] is True
        assert len(transport.requests) == 1
        proxied, proxied_body = transport.requests[0]
        assert proxied.url.host == "pool.example"
        assert proxied.url.path == f"/v1/pool/{fp}/nvmt"
        assert proxied.url.params["profile"] == "nvmt-v1-g5"
        assert proxied.headers["authorization"] == f"Bearer {'w' * 32}"
        assert proxied.headers["content-type"] == "application/vnd.openseek.nvmt"
        assert "content-length" not in proxied.headers
        assert proxied_body == payload
        assert core.db.q("SELECT COUNT(*) FROM pool_channel_tiles")[0][0] == 0
        assert core.db.q("SELECT COUNT(*) FROM pool_hints")[0][0] == 0
    finally:
        core.stop()


def test_read_only_proxy_upload_appears_after_shared_channel_refresh(monkeypatch, tmp_path):
    host_store = MemoryChannel()
    read_store = MemoryChannel()
    read_store.messages = host_store.messages
    read_store.data = host_store.data
    read_store.can_post = False
    host_settings = Settings(data_dir=tmp_path / "host", pool_mode="channel", contribution=True,
                             pool_write_key="w" * 32, telegram_api_id=123,
                             telegram_api_hash="a" * 32, telegram_bot_token="123456:host",
                             telegram_channel_id=host_store.channel_id)
    read_settings = Settings(data_dir=tmp_path / "reader", pool_mode="channel", contribution=True,
                             pool_contrib_url="https://host.example", pool_write_key="w" * 32,
                             telegram_api_id=123, telegram_api_hash="a" * 32,
                             telegram_bot_token="123456:reader", telegram_channel_id=read_store.channel_id)
    host = Core(host_settings, pool_storage=host_store)
    reader = Core(read_settings, pool_storage=read_store)
    host_app = create_api_app(host)
    reader_app = create_api_app(reader)
    real_httpx = httpx
    monkeypatch.setattr(app_module, "httpx", SimpleNamespace(
        AsyncClient=lambda **kwargs: real_httpx.AsyncClient(
            transport=real_httpx.ASGITransport(app=host_app), **kwargs),
        TimeoutException=real_httpx.TimeoutException,
        HTTPError=real_httpx.HTTPError))
    token = "C" * 43
    reader.auth.register("Read-only contributor", hashlib.sha256(token.encode()).hexdigest())
    fp = hashlib.sha256(b"proxied channel contribution").hexdigest()
    image_a, image_b = jpeg((4, 5, 6)), jpeg((6, 5, 4))
    package = encode_nvmt(5_000, 4, {0: (0, image_a), 2: (10_000, image_b)})

    async def put():
        async with real_httpx.AsyncClient(transport=real_httpx.ASGITransport(app=reader_app),
                                          base_url="https://reader.example") as client:
            return await client.put(
                f"/v1/pool/{fp}/nvmt?profile=nvmt-v1-g5&duration_ms=20000&imdb=tt1234567"
                "&season=0&episode=0&size_bytes=1000000&filename=Movie.Title.2024.mkv",
                content=package, headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/vnd.openseek.nvmt"})

    try:
        response = asyncio.run(put())
        assert response.status_code == 201, response.text
        assert reader.db.q("SELECT COUNT(*) FROM pool_channel_tiles")[0][0] == 0
        assert reader.channel_index.refresh(force=True)["indexed"] == 2
        read_package, _meta = reader.pool.package(fp, "nvmt-v1-g5")
        assert read_package == package
        candidates = reader.pool.candidates(duration_ms=20_000, imdb="tt1234567", season=0,
                                           episode=0, filename="Movie.Title.2024.mkv")
        assert [candidate["fp"] for candidate in candidates] == [fp]
    finally:
        host.stop()
        reader.stop()


@pytest.mark.parametrize("status,error_code", [(307, "contribute_redirect_refused"),
                                                 (503, None), (0, "contribute_unavailable")])
def test_read_only_proxy_relays_failures_without_following_redirects(
        monkeypatch, tmp_path, status, error_code):
    transport = FakeProxyTransport(status=status or 500, error=status == 0,
                                   body={"error": "intake_failed"})
    patch_pool_proxy_transport(monkeypatch, transport)
    core, token = make_read_only_proxy_core(tmp_path)
    fp = hashlib.sha256(f"proxy-failure-{status}".encode()).hexdigest()
    app = create_api_app(core)

    async def put():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://openseek.test") as client:
            return await client.put(f"/v1/pool/{fp}/nvmt?profile=nvmt-v1-g5&duration_ms=20000",
                                    content=b"package", headers={
                                        "Authorization": f"Bearer {token}",
                                        "Content-Type": "application/vnd.openseek.nvmt"})

    try:
        response = asyncio.run(put())
        if status == 503:
            assert response.status_code == 503
            assert response.json() == {"error": "intake_failed"}
        else:
            assert response.status_code == 502
            assert response.json()["error"] == error_code
        assert len(transport.requests) == 1
        assert core.db.q("SELECT COUNT(*) FROM pool_channel_tiles")[0][0] == 0
    finally:
        core.stop()


def test_read_only_channel_without_intake_returns_librarian_url(tmp_path):
    core, token = make_read_only_proxy_core(tmp_path, proxy_url="",
                                             librarian="https://t.me/openseek_librarian")
    fp = hashlib.sha256(b"read-only no intake").hexdigest()
    tile_data = jpeg()
    digest = hashlib.sha256(tile_data).hexdigest()
    core.channel_store.put_tile(tile_data, f"openseek:v1:{fp}:{PROFILE}:0:{digest}",
                                {"pts_ms": 0, "duration_ms": 20_000, "n_tiles": 2})
    core.channel_index.refresh(force=True)
    app = create_api_app(core)

    async def requests():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://openseek.test") as client:
            headers = {"Authorization": f"Bearer {token}"}
            read = await client.get(f"/v1/pool/{fp}/tiles/0.jpg?profile={PROFILE}", headers=headers)
            write = await client.put(f"/v1/pool/{fp}/nvmt?profile=nvmt-v1-g5&duration_ms=20000",
                                     content=b"package", headers={
                                         **headers, "Content-Type": "application/vnd.openseek.nvmt"})
            return read, write

    try:
        read, response = asyncio.run(requests())
        assert read.status_code == 200 and read.content == tile_data
        assert response.status_code == 403
        assert response.json() == {"error": "read_only_channel", "contribute_url": "https://t.me/openseek_librarian"}
        assert core.db.q("SELECT COUNT(*) FROM pool_channel_tiles")[0][0] == 1
    finally:
        core.stop()


def test_channel_manifest_incremental_ordering_malformed_and_rebuild(tmp_path):
    store = MemoryChannel()
    db, _cache = make_db_cache(tmp_path / "node")
    index = ChannelIndex(db, store, scan_limit=10, refresh_s=10)
    data = jpeg()
    first_fp = hashlib.sha256(b"manifest before tile").hexdigest()
    second_fp = hashlib.sha256(b"tile before manifest").hexdigest()

    def hints(fp):
        return {"v": 1, "fp": fp, "profile": "nvmt-v1-g5", "imdb": "tt1234567",
                "season": 0, "episode": 0, "duration_ms": 20_000, "size_bytes": 5_000_000,
                "filename_norm": "sample.movie.2024", "n_tiles": 4, "grid": 5_000,
                "created": 1}

    def add_tile(fp, k=0):
        digest = hashlib.sha256(data).hexdigest()
        return store.put_tile(data, f"openseek:v1:{fp}:{PROFILE}:{k}:{digest}",
                              {"pts_ms": k * 10_000, "duration_ms": 20_000, "n_tiles": 2})

    try:
        # A manifest can arrive first. It is indexed immediately, but without
        # channel tiles it cannot become a usable candidate.
        store.post_message(ChannelPoolService._manifest_caption(hints(first_fp)))
        assert index.refresh(force=True)["scanned"] == 1
        assert db.q("SELECT fp FROM pool_hints") == [(first_fp,)]
        assert db.q("SELECT COUNT(*) FROM pool_channel_tiles")[0][0] == 0
        add_tile(first_fp)
        assert index.refresh(force=True)["indexed"] == 1

        # The reverse order is handled by the next incremental refresh too.
        add_tile(second_fp)
        assert index.refresh(force=True)["indexed"] == 1
        store.post_message(ChannelPoolService._manifest_caption(hints(second_fp)))
        assert index.refresh(force=True)["scanned"] == 1
        assert {row[0] for row in db.q("SELECT fp FROM pool_hints")} == {first_fp, second_fp}

        # Invalid JSON and unrecognized caption versions do not abort scanning.
        store.post_message("openseek:hints:v1:bm90anNvbg")
        store.post_message("openseek:hints:v2:bm90anNvbg")
        assert index.refresh(force=True)["scanned"] == 2
        assert db.q("SELECT COUNT(*) FROM pool_hints")[0][0] == 2

        stale_fp = "f" * 64
        db.x("INSERT INTO pool_hints(fp,profile,duration_ms,n_tiles,grid,created) "
             "VALUES(?,?,?,?,?,?)", (stale_fp, "nvmt-v1-g5", 20_000, 1, 5_000, 2))
        db.x("INSERT INTO pool_episode_packages(episode_key,fp,profile,duration_ms,updated) "
             "VALUES(?,?,?,?,?)", ("a" * 64, stale_fp, "nvmt-v1-g5", 20_000, 2))

        rebuilt = index.rebuild()
        assert rebuilt["indexed"] == 2
        assert {row[0] for row in db.q("SELECT fp FROM pool_hints")} == {first_fp, second_fp}
        assert db.q("SELECT COUNT(*) FROM pool_episode_packages")[0][0] == 0
        assert db.q("SELECT COUNT(*) FROM pool_channel_tiles")[0][0] == 2
    finally:
        db.close()


def test_channel_candidates_rank_movies_and_series_from_manifest_hints(tmp_path):
    store = MemoryChannel()
    db, _cache = make_db_cache(tmp_path / "node")
    index = ChannelIndex(db, store, scan_limit=100, refresh_s=10)
    pool = ChannelPoolService(index)
    image = jpeg()

    def publish(label, duration_ms, imdb, season, episode, filename):
        fp = hashlib.sha256(label.encode()).hexdigest()
        grid = 5_000
        n_slots = (duration_ms + grid - 1) // grid
        tiles = {slot: (slot * grid, image) for slot in range(n_slots)}
        package = encode_nvmt(grid, n_slots, tiles)
        pool.upload_package(fp, "nvmt-v1-g5", duration_ms, package, imdb=imdb,
                            season=season, episode=episode, size_bytes=5_000_000,
                            filename=filename)
        return fp

    try:
        series_fp = publish("series-s02e03", 20_000, "tt1111111", 2, 3, "Show.S02E03.mkv")
        movie_fp = publish("movie-match", 20_000, "tt2222222", 0, 0, "Movie.Title.2024.mkv")
        filename_fp = publish("filename-match", 20_000, "tt9999999", 0, 0,
                              "Movie.Title.2024.mp4")
        nearby_fp = publish("movie-nearby", 24_000, "tt2222222", 0, 0,
                            "Movie.Title.2024.webm")
        outside_fp = publish("movie-outside-cutoff", 120_000, "tt2222222", 0, 0,
                             "Movie.Title.2024.mkv")

        # A valid manifest alone must not be returned as a candidate.
        orphan_fp = hashlib.sha256(b"manifest without tiles").hexdigest()
        orphan = {"v": 1, "fp": orphan_fp, "profile": "nvmt-v1-g5", "imdb": "tt2222222",
                  "season": 0, "episode": 0, "duration_ms": 20_000, "size_bytes": 5_000_000,
                  "filename_norm": "movie.title.2024", "n_tiles": 4, "grid": 5_000,
                  "created": 1}
        store.post_message(ChannelPoolService._manifest_caption(orphan))
        index.refresh(force=True)

        movie_candidates = pool.candidates(duration_ms=20_000, imdb="tt2222222", season=0, episode=0,
                                           size_bytes=5_000_000, filename="Movie.Title.2024.mkv")
        movie_fps = [candidate["fp"] for candidate in movie_candidates]
        assert movie_fps == [movie_fp, nearby_fp, filename_fp]
        assert outside_fp not in movie_fps and orphan_fp not in movie_fps
        assert movie_candidates[0]["score"] == 100
        assert movie_candidates[0]["match"] == "imdb+season+episode+duration"
        assert movie_candidates[1]["relation"] == "length_differs"
        assert movie_candidates[2]["score"] == 75
        assert movie_candidates[0]["n_slots"] == 4
        assert movie_candidates[0]["n_tiles"] == 2

        series_candidates = pool.candidates(duration_ms=20_000, imdb="tt1111111", season=2, episode=3,
                                            filename="Other.Name.mkv")
        assert [candidate["fp"] for candidate in series_candidates] == [series_fp]
        assert series_candidates[0]["score"] == 100
        assert series_candidates[0]["match"] == "imdb+season+episode+duration"
    finally:
        db.close()


def test_two_channel_nodes_converge_on_package_bytes_and_candidate_order(tmp_path):
    store = MemoryChannel()
    settings_a = Settings(data_dir=tmp_path / "node-a", pool_mode="channel", contribution=True,
                          telegram_api_id=123, telegram_api_hash="a" * 32,
                          telegram_bot_token="123456:test", telegram_channel_id=store.channel_id)
    settings_b = settings_a.with_(data_dir=tmp_path / "node-b")
    node_a = Core(settings_a, pool_storage=store)
    node_b = Core(settings_b, pool_storage=store)
    first_fp, second_fp = "a" * 64, "f" * 64
    package = encode_nvmt(5_000, 4, {0: (0, jpeg((1, 2, 3))), 2: (10_000, jpeg((3, 2, 1)))})
    try:
        for fp in (first_fp, second_fp):
            result = node_a.pool.upload_package(
                fp, "nvmt-v1-g5", 20_000, package, imdb="tt1234567", season=0, episode=0,
                size_bytes=1_000_000, filename="Movie.Title.2024.mkv")
            assert result["stored"] is True

        refresh = node_b.channel_index.refresh(force=True)
        assert refresh["indexed"] == 4

        def package_and_candidates(core):
            nvmt, _meta = core.pool.package(first_fp, "nvmt-v1-g5")
            candidates = core.pool.candidates(
                duration_ms=20_000, imdb="tt1234567", season=0, episode=0,
                size_bytes=1_000_000, filename="Movie.Title.2024.mkv")
            return nvmt, candidates

        source_nvmt, source_candidates = package_and_candidates(node_a)
        replica_nvmt, replica_candidates = package_and_candidates(node_b)
        assert replica_nvmt == source_nvmt
        assert [item["fp"] for item in replica_candidates] == [item["fp"] for item in source_candidates]

        rebuilt = node_b.channel_index.rebuild()
        assert rebuilt["indexed"] == 4
        rebuilt_nvmt, rebuilt_candidates = package_and_candidates(node_b)
        assert rebuilt_nvmt == source_nvmt
        assert rebuilt_candidates == source_candidates
        assert node_a.jobs.running_sources() == node_b.jobs.running_sources() == []
    finally:
        node_a.stop()
        node_b.stop()


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


def test_channel_contribution_proxy_config_requires_https_and_write_key():
    base = {"OPENSEEK_POOL_MODE": "channel", "OPENSEEK_TELEGRAM_API_ID": "123",
            "OPENSEEK_TELEGRAM_API_HASH": "a" * 32, "OPENSEEK_TELEGRAM_BOT_TOKEN": "123456:test",
            "OPENSEEK_TELEGRAM_CHANNEL_ID": "-100123"}
    valid = Settings.from_env({**base, "OPENSEEK_POOL_CONTRIB_URL": "https://pool.example",
                               "OPENSEEK_POOL_WRITE_KEY": "w" * 32})
    assert valid.pool_contrib_url == "https://pool.example"
    with pytest.raises(ConfigError, match="POOL_WRITE_KEY"):
        Settings.from_env({**base, "OPENSEEK_POOL_CONTRIB_URL": "https://pool.example"})
    with pytest.raises(ConfigError, match="POOL_CONTRIB_URL"):
        Settings.from_env({**base, "OPENSEEK_POOL_CONTRIB_URL": "http://pool.example",
                           "OPENSEEK_POOL_WRITE_KEY": "w" * 32})


def test_channel_pool_status_reports_history_permissions_and_index_counts(tmp_path):
    store = MemoryChannel()
    store.entity_type = "broadcast"
    store.entity_resolved = True
    store.history_readable = True
    store.detected_can_post = False
    db, _cache = make_db_cache(tmp_path / "node")
    try:
        index = ChannelIndex(db, store)
        status = index.status()
        assert status["entity_type"] == "broadcast"
        assert status["entity_resolved"] is True
        assert status["history_readable"] is True
        assert status["channel_writable"] is True
        assert status["detected_channel_writable"] is False
        assert status["tiles"] == status["hints"] == status["episode_packages"] == 0
    finally:
        db.close()


def test_doctor_reports_live_channel_identity_and_permissions(monkeypatch, capsys, tmp_path):
    settings = Settings(data_dir=tmp_path, pool_mode="channel", telegram_bot_token="123456:test",
                        telegram_channel_id="-100123", pool_channel_writable="auto")
    monkeypatch.setattr(cli.Settings, "from_env", lambda _env: settings)
    monkeypatch.setattr(cli, "_admin", lambda *_args: (200, {
        "mode": "channel", "entity_resolved": True, "history_readable": True,
        "entity_type": "broadcast", "last_message_id": 17, "tiles": 3, "hints": 2,
        "librarian_status": "disabled", "detected_channel_writable": False,
        "channel_writable": False, "contribution_path": "proxy",
    }))

    assert cli.main(["doctor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["telegram_identity"] == "bot"
    assert report["pool"]["entity_resolved"] is True
    assert report["pool"]["history_readable"] is True
    assert report["pool"]["detected_channel_writable"] is False
    assert report["pool"]["last_message_id"] == 17
    assert report["pool"]["librarian_status"] == "disabled"
    assert report["contribution_path"] == "proxy"


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

    async def send_message(self, entity, text, **_kw):
        mid = self.next_id
        self.next_id += 1
        msg = SimpleNamespace(id=mid, message=text, text=text, entity=entity)
        self.messages[mid] = msg
        return msg


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


@pytest.mark.parametrize("override,detected,expected", [("auto", True, True), ("auto", False, False),
                                                          ("1", False, True), ("0", True, False)])
def test_mtproto_store_reports_detected_and_effective_write_permissions(
        tmp_path, override, detected, expected):
    settings = Settings(data_dir=tmp_path, pool_mode="channel", contribution=True,
                        telegram_api_id=123, telegram_api_hash="a" * 32,
                        telegram_bot_token="123456:test", telegram_channel_id="-100123",
                        pool_channel_writable=override)
    fake = FakeTelethonClient()
    fake.can_send = detected
    store = TelegramMTProtoStore(settings, client_factory=lambda *_args, **_kwargs: fake)
    try:
        store.start()
        assert store.detected_can_post is detected
        assert store.can_post is expected
        assert store.entity_type == "megagroup"
        assert store.entity_resolved is True
        assert store.history_readable is True
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
