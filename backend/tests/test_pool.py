"""Two-instance Telegram-backed pool tests using a local Bot API-compatible fake."""
from __future__ import annotations

import hashlib
import io
import json
import asyncio
import httpx
import time
import threading
import urllib.error
from pathlib import Path
from email.parser import BytesParser
from email.policy import default
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from PIL import Image
import pytest

import openseek.app as app_module
from openseek.app import Core, create_admin_app, create_api_app
from openseek.cache import Cache, PROFILE
from openseek.config import Settings
from openseek.db import DB
from openseek.engine import Extractor
from openseek.fetch import fingerprint
from openseek.jobs import Source
from openseek.pool import (MAX_NVMT_BYTES, PoolClient, PoolError, PoolService,
                           PoolSync, TelegramBotStore, decode_nvmt, encode_nvmt)
from openseek.scheduler import tile_count

READ_KEY = "r" * 40
WRITE_KEY = "w" * 40


def jpeg(color=(21, 49, 90)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (320, 180), color).save(out, "JPEG", quality=75)
    return out.getvalue()


def nvmt_package(grid: int, duration_ms: int, tiles: dict[int, tuple[int, bytes]]) -> bytes:
    slots = (duration_ms + grid - 1) // grid
    return encode_nvmt(grid, slots, tiles)


class FakeResponse:
    def __init__(self, data: bytes, status=200, headers=None):
        self.data, self.status, self.headers = data, status, headers or {}

    def read(self, size=-1):
        return self.data if size < 0 else self.data[:size]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class HeaderMap(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


class FakeTelegramBotAPI:
    """Implements sendDocument, getFile, and the Bot API file download endpoint."""
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.messages = 0
        self.get_file_calls = 0
        self.download_calls = 0
        self.deleted_messages: list[int] = []

    def urlopen(self, req, timeout=8.0):
        from urllib.parse import urlparse
        url = urlparse(req.full_url)
        body = req.data or b""
        if url.path.endswith("/sendDocument"):
            mime = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + req.get_header("Content-type").encode() +
                b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
            part = next(p for p in mime.iter_parts()
                        if p.get_param("name", header="content-disposition") == "document")
            data = part.get_payload(decode=True)
            self.messages += 1
            file_id = f"file-{self.messages}"
            self.files[file_id] = data
            return FakeResponse(json.dumps({"ok": True, "result": {
                "message_id": self.messages,
                "document": {"file_id": file_id, "file_size": len(data)}}}).encode())
        if url.path.endswith("/getFile"):
            self.get_file_calls += 1
            file_id = parse_qs(body.decode()).get("file_id", [""])[0]
            if file_id not in self.files:
                raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", None,
                                              io.BytesIO(b'{"ok":false,"error_code":400}'))
            return FakeResponse(json.dumps({"ok": True, "result": {
                "file_path": f"docs/{file_id}.jpg"}}).encode())
        if url.path.endswith("/deleteMessage"):
            self.deleted_messages.append(int(parse_qs(body.decode()).get("message_id", ["0"])[0]))
            return FakeResponse(b'{"ok":true,"result":true}')
        if "/file/bot" in url.path:
            self.download_calls += 1
            file_id = url.path.rsplit("/", 1)[-1].removesuffix(".jpg")
            if file_id in self.files:
                return FakeResponse(self.files[file_id], headers={"Content-Type": "image/jpeg"})
        return FakeResponse(b'{"ok":false}', status=404)


class InProcessPoolAPI:
    """HTTP-shaped test transport for the same routes and headers used by PoolClient."""
    def __init__(self, service: PoolService):
        self.service = service

    def __call__(self, url, data, headers, timeout):
        parsed = urlsplit(url)
        path = parsed.path.split("/")
        key = headers.get("Authorization", "").removeprefix("Bearer ")
        query = parse_qs(parsed.query)
        if len(path) < 5 or path[1:3] != ["v1", "pool"]:
            return FakeResponse(b'{"error":"not_found"}', 404)
        fp = path[3]
        if data is not None:
            if not self.service.authorize(key, write=True):
                return FakeResponse(b'{"error":"forbidden"}', 403)
            try:
                k = int(path[5][:-4])
                result = self.service.upload(fp, query["profile"][0], k, int(query["pts_ms"][0]),
                                             int(query["duration_ms"][0]), int(query["n_tiles"][0]), data)
            except PoolError as exc:
                return FakeResponse(json.dumps({"error": exc.code}).encode(), exc.status)
            return FakeResponse(json.dumps(result).encode(), 200 if result["duplicate"] else 201)
        if not self.service.authorize(key):
            return FakeResponse(b'{"error":"forbidden"}', 403)
        if path[4] == "manifest":
            try:
                result = self.service.manifest(fp, query["profile"][0])
            except PoolError as exc:
                return FakeResponse(json.dumps({"error": exc.code}).encode(), exc.status)
            if result is None:
                return FakeResponse(b'{"error":"not_found"}', 404)
            return FakeResponse(json.dumps(result).encode())
        if path[4] == "tiles":
            k = int(path[5][:-4])
            try:
                got = self.service.tile(fp, query["profile"][0], k)
            except PoolError as exc:
                return FakeResponse(json.dumps({"error": exc.code}).encode(), exc.status)
            if got is None:
                return FakeResponse(b'{"error":"not_found"}', 404)
            tile, meta = got
            return FakeResponse(tile, headers={
                "X-OpenSeek-Pts-Ms": str(meta["pts_ms"]),
                "X-OpenSeek-Duration-Ms": str(meta["duration_ms"]),
                "X-OpenSeek-N-Tiles": str(meta["n_tiles"]),
                "X-OpenSeek-Profile": meta["profile"],
                "X-OpenSeek-Sha256": meta["sha256"],
            })
        return FakeResponse(b'{"error":"not_found"}', 404)


class ASGIPoolAPI:
    """Invoke the real OpenSeek ASGI middleware and routes without opening a socket."""
    def __init__(self, app):
        self.app = app

    def __call__(self, url, data, headers, timeout, method=None):
        parsed = urlsplit(url)
        body = data or b""
        sent = []
        first_receive = True
        request_headers = [(k.lower().encode(), str(v).encode()) for k, v in headers.items()]
        request_headers.append((b"host", b"pool.test"))
        if data is not None:
            request_headers.append((b"content-length", str(len(body)).encode()))

        async def receive():
            nonlocal first_receive
            if first_receive:
                first_receive = False
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": method or ("PUT" if data is not None else "GET"),
            "scheme": "https", "path": parsed.path, "raw_path": parsed.path.encode(),
            "query_string": parsed.query.encode(), "root_path": "", "headers": request_headers,
            "client": ("127.0.0.1", 45678), "server": ("pool.test", 443), "state": {},
        }
        asyncio.run(self.app(scope, receive, send))
        start = next(message for message in sent if message["type"] == "http.response.start")
        response_body = b"".join(message.get("body", b"") for message in sent
                                  if message["type"] == "http.response.body")
        response_headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
        return FakeResponse(response_body, start["status"], HeaderMap(response_headers))


def test_two_instances_share_generated_tile_via_telegram_and_http_contract(tmp_path):
    tg = FakeTelegramBotAPI()
    bot_store = TelegramBotStore("123456:test", "-100123", opener=tg.urlopen)
    pool_db = DB(tmp_path / "pool.sqlite")
    pool = PoolService(pool_db, bot_store, READ_KEY, WRITE_KEY)
    pool_api = InProcessPoolAPI(pool)

    contributor_db = DB(tmp_path / "contributor.sqlite")
    contributor_cache = Cache(tmp_path / "contributor-cache", contributor_db, 1 << 20, 30)
    contributor_client = PoolClient("https://pool.invalid", READ_KEY, WRITE_KEY, requester=pool_api)
    publisher = PoolSync(contributor_db, contributor_cache, contributor_client)

    receiver_db = DB(tmp_path / "receiver.sqlite")
    receiver_cache = Cache(tmp_path / "receiver-cache", receiver_db, 1 << 20, 30)
    receiver_client = PoolClient("https://pool.invalid", READ_KEY, requester=pool_api)
    try:
        fp = hashlib.sha256(b"same exact video bytes on both OpenSeek hosts").hexdigest()
        duration_ms, n_tiles = 20_000, tile_count(20_000)
        tile_bytes = jpeg()

        # Host A generated this tile from its local source and queues only its key
        # durably; the publisher reads the JPEG from that host's ordinary cache.
        contributor_cache.set_media(fp, duration_ms, n_tiles)
        contributor_cache.put_tile(fp, 1, 10_000, tile_bytes)
        publisher.enqueue(fp, 1)
        publisher.start()
        deadline = time.time() + 5
        while publisher.pending() and time.time() < deadline:
            time.sleep(0.01)
        assert publisher.pending() == 0
        assert tg.messages == 1
        assert next(iter(tg.files.values())) == tile_bytes

        # Host B retrieves the exact file/profile/slot and writes it into its own
        # local cache. No source video is sent to the pool and no frame is decoded.
        shared = receiver_client.get_tile(fp, 1)
        assert shared == (tile_bytes, 10_000)
        receiver_cache.set_media(fp, duration_ms, n_tiles)
        receiver_cache.put_tile(fp, 1, shared[1], shared[0])

        assert receiver_cache.tile_bytes(fp, 1) == tile_bytes

        # The manifest keeps partial coverage and has no title or source URL.
        manifest = pool.manifest(fp, PROFILE)
        assert manifest == {"fp": fp, "profile": PROFILE, "duration_ms": duration_ms,
                            "n_tiles": n_tiles, "tiles": [{"k": 1, "pts_ms": 10_000,
                                                             "size": len(tile_bytes),
                                                             "sha256": hashlib.sha256(tile_bytes).hexdigest()}]}
        assert "title" not in json.dumps(manifest).lower()
    finally:
        publisher.stop()
        pool_db.close()
        contributor_db.close()
        receiver_db.close()


def test_two_instances_through_real_asgi_pool_routes(tmp_path, monkeypatch):
    """Exercise Guard, FastAPI routes, Bot API adapter, outbox, and receiver cache together."""
    tg = FakeTelegramBotAPI()

    class NoSourceProxy:
        def stop(self):
            pass

    # This environment lets Python threads run but hangs while asyncio.run shuts down its
    # default executor. Keep the route's storage operation inline in this socket-free test.
    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(app_module.asyncio, "to_thread", inline_to_thread)
    core = Core(Settings(data_dir=tmp_path / "pool-host", pool_enabled=True,
                         pool_read_key=READ_KEY, pool_write_key=WRITE_KEY,
                         telegram_bot_token="123456:test", telegram_channel_id="-100123"),
                pool_storage=TelegramBotStore("123456:test", "-100123", opener=tg.urlopen),
                proxy=NoSourceProxy())
    api = ASGIPoolAPI(create_api_app(core))
    writer = PoolClient("https://pool.test", READ_KEY, WRITE_KEY, requester=api)
    reader = PoolClient("https://pool.test", READ_KEY, requester=api)
    contributor_db = DB(tmp_path / "contributor.sqlite")
    contributor_cache = Cache(tmp_path / "contributor-cache", contributor_db, 1 << 20, 30)
    sync = PoolSync(contributor_db, contributor_cache, writer)
    receiver_db = DB(tmp_path / "receiver.sqlite")
    receiver_cache = Cache(tmp_path / "receiver-cache", receiver_db, 1 << 20, 30)
    try:
        fp = hashlib.sha256(b"real route exact source bytes").hexdigest()
        duration_ms, n_tiles = 30_000, tile_count(30_000)
        original = jpeg((111, 42, 9))

        # Exercise Source._run's local-generation -> cache -> outbox path with a fake decoder.
        contributor_cache.set_media(fp, duration_ms, n_tiles)
        decoder = SimpleNamespace(
            is_open=lambda: True,
            close=lambda: None,
            grab=lambda t_ms, accurate: SimpleNamespace(pts_ms=t_ms, jpeg=original,
                                                         requests=1, bytes=len(original)),
        )
        budget = SimpleNamespace(requests=0, used=0)
        proxy_source = SimpleNamespace(last_error=None, throttle_seconds=0.0)
        job_mgr = SimpleNamespace(settings=SimpleNamespace(tolerance_ms=5_000),
                                  pool_client=writer, pool_sync=sync, cache=contributor_cache,
                                  clock=time.monotonic, bump=lambda: None)
        source = object.__new__(Source)
        source.mgr = job_mgr
        source.fp = fp
        source.scheduler = None
        source.stop_flag = False
        source.engines = [decoder]
        source.n_workers = 1
        source.proxy_src = proxy_source
        source.upstream = SimpleNamespace(budget=budget)
        source.cond = threading.Condition()
        source.events = []
        source.active_tasks = {}
        source._crashes = 0
        source._unreachable = 0
        source._tile_failures = {}
        tasks = [(True, SimpleNamespace(k=2, t_ms=20_000, cls="P1")), (False, None)]
        source._next_task = lambda worker_idx: tasks.pop(0)
        source._finish = lambda worker_idx, task, ok, permanent=False: None
        source._run()

        sync.start()
        deadline = time.time() + 5
        while sync.pending() and time.time() < deadline:
            time.sleep(0.01)
        assert sync.pending() == 0
        assert tg.messages == 1
        assert next(iter(tg.files.values())) == original

        # A second OpenSeek uses the real GET tile route and stores the result locally.
        got = reader.get_tile(fp, 2)
        assert got == (original, 20_000)
        receiver_cache.set_media(fp, duration_ms, n_tiles)
        receiver_cache.put_tile(fp, 2, got[1], got[0])
        assert receiver_cache.tile_bytes(fp, 2) == original

        # Exercise the actual manifest route and the bearer check on the actual ASGI stack.
        manifest_url = f"https://pool.test/v1/pool/{fp}/manifest?profile={PROFILE}"
        manifest_response = api(manifest_url, None, {"Authorization": f"Bearer {READ_KEY}"}, 2)
        assert manifest_response.status == 200
        manifest = json.loads(manifest_response.read())
        assert [tile["k"] for tile in manifest["tiles"]] == [2]
        forbidden = api(manifest_url, None, {"Authorization": "Bearer wrong"}, 2)
        assert forbidden.status == 403
        assert "video" not in json.dumps(manifest).lower()
    finally:
        sync.stop()
        contributor_db.close()
        receiver_db.close()
        core.stop()


def test_pool_auth_validation_first_writer_conflict_and_variant_isolation(tmp_path):
    tg = FakeTelegramBotAPI()
    db = DB(tmp_path / "pool.sqlite")
    pool = PoolService(db, TelegramBotStore("123456:test", "-100123", opener=tg.urlopen), READ_KEY, WRITE_KEY)
    api = InProcessPoolAPI(pool)
    client = PoolClient("https://pool.invalid", READ_KEY, WRITE_KEY, requester=api)
    try:
        fp_a = hashlib.sha256(b"encode A").hexdigest()
        fp_b = hashlib.sha256(b"encode B, different duration or bytes").hexdigest()
        duration_ms, n_tiles = 20_000, tile_count(20_000)
        a = jpeg((1, 2, 3))
        b = jpeg((4, 5, 6))

        bad_key = PoolClient("https://pool.invalid", "wrong", "wrong", requester=api)
        assert not bad_key.publish(fp_a, 0, 0, duration_ms, n_tiles, a)
        assert not client.publish(fp_a, 0, 0, duration_ms, n_tiles, b"not-a-jpeg")
        assert client.publish(fp_a, 0, 0, duration_ms, n_tiles, a)
        # The first valid immutable tile wins; a divergent rerender cannot poison it.
        assert client.publish(fp_a, 0, 0, duration_ms, n_tiles, b)
        assert client.get_tile(fp_a, 0) == (a, 0)
        # Similar titles/episodes/lengths do not merge different exact fingerprints.
        assert client.publish(fp_b, 0, 0, duration_ms, n_tiles, b)
        assert client.get_tile(fp_b, 0) == (b, 0)
        assert tg.messages == 2

        with __import__("pytest").raises(PoolError) as error:
            pool.upload(fp_a, "wrong-profile", 0, 0, duration_ms, n_tiles, a)
        assert error.value.code == "bad_key"
        with __import__("pytest").raises(PoolError) as error:
            pool.upload(hashlib.sha256(b"bad timestamp").hexdigest(), PROFILE, 0, 9_000,
                        duration_ms, n_tiles, a)
        assert error.value.code == "bad_timestamp"
    finally:
        db.close()


def test_nvmt_package_validation_merge_and_asgi_download(tmp_path, monkeypatch):
    """TV-sized NVMT packages merge by exact file/profile and survive the real HTTP routes."""
    tg = FakeTelegramBotAPI()

    class NoSourceProxy:
        def stop(self):
            pass

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(app_module.asyncio, "to_thread", inline_to_thread)
    core = Core(Settings(data_dir=tmp_path / "pool-host", pool_enabled=True,
                         pool_read_key=READ_KEY, pool_write_key=WRITE_KEY,
                         telegram_bot_token="123456:test", telegram_channel_id="-100123"),
                pool_storage=TelegramBotStore("123456:test", "-100123", opener=tg.urlopen),
                proxy=NoSourceProxy())
    api = ASGIPoolAPI(create_api_app(core))
    fp = hashlib.sha256(b"one exact TV video").hexdigest()
    duration_ms, grid = 20_000, 5_000
    a, b, replacement = jpeg((21, 49, 90)), jpeg((90, 49, 21)), jpeg((5, 6, 7))

    def put(tiles, *, profile="nvmt-v1-g5", duration=duration_ms, content_type="application/vnd.openseek.nvmt"):
        data = nvmt_package(grid, duration, tiles)
        return api(f"https://pool.test/v1/pool/{fp}/nvmt?profile={profile}&duration_ms={duration}",
                   data, {"Authorization": f"Bearer {WRITE_KEY}", "Content-Type": content_type}, 2)

    try:
        # Two TVs can add coverage without replacing the first accepted frame for a slot.
        first = put({0: (0, a), 2: (10_000, a)})
        assert first.status == 201, first.read()
        second = put({0: (0, replacement), 1: (5_000, b)})
        assert second.status == 201, second.read()
        assert tg.messages == 2

        url = f"https://pool.test/v1/pool/{fp}/nvmt?profile=nvmt-v1-g5"
        got = api(url, None, {"Authorization": f"Bearer {READ_KEY}"}, 2)
        assert got.status == 200
        assert got.headers["content-type"] == "application/vnd.openseek.nvmt"
        assert got.headers["x-openseek-duration-ms"] == str(duration_ms)
        assert got.headers["x-openseek-profile"] == "nvmt-v1-g5"
        assert got.headers["x-openseek-sha256"] == hashlib.sha256(got.data).hexdigest()
        assert decode_nvmt(fp, "nvmt-v1-g5", duration_ms, got.data)[2] == {
            0: (0, a), 1: (5_000, b), 2: (10_000, a)}

        duplicate = put({0: (0, a), 1: (5_000, b), 2: (10_000, a)})
        assert duplicate.status == 200
        assert json.loads(duplicate.data)["duplicate"] is True

        # Anonymous reads are rejected on the private API; writes need the write key.
        assert api(url, None, {}, 2).status in (401, 403)
        denied = api(f"https://pool.test/v1/pool/{fp}/nvmt?profile=nvmt-v1-g5&duration_ms={duration_ms}",
                     nvmt_package(grid, duration_ms, {3: (15_000, b)}),
                     {"Authorization": f"Bearer {READ_KEY}", "Content-Type": "application/vnd.openseek.nvmt"}, 2)
        assert denied.status == 403
        assert json.loads(denied.data)["error"] == "forbidden"

        # A valid paired guest without the contribution permission gets a
        # distinguishable error so the TV can show it beside the setting.
        guest_token = "A" * 43
        core.auth.register("Read only", hashlib.sha256(guest_token.encode()).hexdigest(), role="guest")
        guest_denied = api(
            f"https://pool.test/v1/pool/{fp}/nvmt?profile=nvmt-v1-g5&duration_ms={duration_ms}",
            nvmt_package(grid, duration_ms, {3: (15_000, b)}),
            {"Authorization": f"Bearer {guest_token}", "Content-Type": "application/vnd.openseek.nvmt"}, 2)
        assert guest_denied.status == 403
        assert json.loads(guest_denied.data)["error"] == "not_contribute"

        # Invalid metadata, mismatched profile, non-NVMT bytes, and >16 MiB are refused.
        assert put({0: (0, a)}, profile="nvmt-v1-g10").status == 400
        # The package itself is valid, but a different duration cannot overwrite this exact key.
        conflict = put({0: (0, a)}, duration=25_000)
        assert conflict.status == 409
        assert conflict.headers["x-openseek-duration-ms"] == str(duration_ms)
        assert json.loads(conflict.data)["error"] == "media_metadata_conflict"
        bad_type = put({0: (0, a)}, content_type="image/jpeg")
        assert bad_type.status == 400
        with pytest.raises(PoolError, match="bad_package"):
            decode_nvmt(fp, "nvmt-v1-g5", duration_ms, b"not an NVMT package")
        with pytest.raises(PoolError, match="package_too_large"):
            decode_nvmt(fp, "nvmt-v1-g5", duration_ms, b"x" * (MAX_NVMT_BYTES + 1))
    finally:
        core.stop()


def test_nvmt_hints_upload_and_ranked_candidate_route(tmp_path, monkeypatch):
    tg = FakeTelegramBotAPI()

    class NoSourceProxy:
        def stop(self):
            pass

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(app_module.asyncio, "to_thread", inline_to_thread)
    storage = TelegramBotStore("123456:test", "-100123", opener=tg.urlopen)
    core = Core(Settings(data_dir=tmp_path / "pool-hints", pool_enabled=True,
                         pool_read_key=READ_KEY, pool_write_key=WRITE_KEY,
                         telegram_bot_token="123456:test", telegram_channel_id="-100123"),
                pool_storage=storage, proxy=NoSourceProxy())
    api = ASGIPoolAPI(create_api_app(core))
    first_fp = hashlib.sha256(b"film encode one").hexdigest()
    other_fp = hashlib.sha256(b"film encode two").hexdigest()
    wrong_fp = hashlib.sha256(b"different film").hexdigest()
    image = jpeg()
    duration = 20_000
    try:
        for fp, imdb, filename in (
            (first_fp, "tt1234567", "Film.Name.2024.1080p.mkv"),
            (other_fp, "tt1234567", "Film.Name.2024.2160p.mp4"),
            (wrong_fp, "tt7654321", "Other.Film.2024.mkv"),
        ):
            result = api(
                f"https://pool.test/v1/pool/{fp}/nvmt?profile=nvmt-v1-g5&duration_ms={duration}"
                f"&imdb={imdb}&season=0&episode=0&size_bytes=1000000&filename={filename}",
                nvmt_package(5_000, duration, {0: (0, image)}),
                {"Authorization": f"Bearer {WRITE_KEY}", "Content-Type": "application/vnd.openseek.nvmt"}, 2)
            assert result.status == 201, result.read()

        assert core.db.q("SELECT filename_norm FROM pool_hints WHERE fp=?", (first_fp,))[0][0] == "film.name.2024.1080p"
        found = api(
            "https://pool.test/v1/pool/candidates?imdb=tt1234567&season=0&episode=0&duration_ms=20500"
            "&size_bytes=1000000&filename=Film.Name.2024.2160p.mkv",
            None, {"Authorization": f"Bearer {READ_KEY}"}, 2)
        assert found.status == 200
        candidates = json.loads(found.data)["candidates"]
        assert [candidate["fp"] for candidate in candidates] == [other_fp, first_fp]
        assert candidates[0]["score"] == 100
        assert candidates[0]["relation"] == "same_length"
        assert candidates[0]["approximate"] is True
        assert candidates[0]["a_num"] == 41 and candidates[0]["a_den"] == 40

        excluded = api(
            f"https://pool.test/v1/pool/candidates?imdb=tt1234567&season=0&episode=0"
            f"&duration_ms=20500&exclude_fp={first_fp}",
            None, {"Authorization": f"Bearer {READ_KEY}"}, 2)
        assert [candidate["fp"] for candidate in json.loads(excluded.data)["candidates"]] == [other_fp]
    finally:
        core.stop()


def test_episode_variant_index_and_sparse_sample_routes(tmp_path, monkeypatch):
    """Different encodes can be discovered by episode key and sampled before full reuse."""
    tg = FakeTelegramBotAPI()

    class NoSourceProxy:
        def stop(self):
            pass

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(app_module.asyncio, "to_thread", inline_to_thread)
    core = Core(Settings(data_dir=tmp_path / "pool-host", pool_enabled=True,
                         pool_read_key=READ_KEY, pool_write_key=WRITE_KEY,
                         telegram_bot_token="123456:test", telegram_channel_id="-100123"),
                pool_storage=TelegramBotStore("123456:test", "-100123", opener=tg.urlopen),
                proxy=NoSourceProxy())
    api = ASGIPoolAPI(create_api_app(core))
    episode_key = hashlib.sha256(b"stable, client-side episode key").hexdigest()
    fp_old = hashlib.sha256(b"first encode").hexdigest()
    fp_new = hashlib.sha256(b"second encode").hexdigest()
    duration_old, duration_new, grid = 20_000, 60_000, 5_000
    old_tiles = {0: (0, jpeg((10, 30, 50))), 1: (5_000, jpeg((30, 50, 70))),
                 2: (10_000, jpeg((50, 70, 90))), 3: (15_000, jpeg((70, 90, 110)))}
    new_tiles = {0: (0, jpeg((11, 31, 51))), 2: (10_000, jpeg((31, 51, 71))),
                 8: (40_000, jpeg((51, 71, 91))), 11: (55_000, jpeg((71, 91, 111)))}
    headers = {"Authorization": f"Bearer {WRITE_KEY}",
               "Content-Type": "application/vnd.openseek.nvmt"}

    def put(fp, duration, tiles, with_episode_key):
        query = f"profile=nvmt-v1-g5&duration_ms={duration}"
        if with_episode_key:
            query += f"&episode_key={episode_key}"
        return api(f"https://pool.test/v1/pool/{fp}/nvmt?{query}",
                   nvmt_package(grid, duration, tiles), headers, 2)

    try:
        assert put(fp_old, duration_old, old_tiles, False).status == 201
        # A read by an updated client backfills the episode index for an older package.
        old_url = (f"https://pool.test/v1/pool/{fp_old}/nvmt?profile=nvmt-v1-g5"
                   f"&episode_key={episode_key}")
        assert api(old_url, None, {"Authorization": f"Bearer {READ_KEY}"}, 2).status == 200

        # New contributions are indexed as they are uploaded.
        assert put(fp_new, duration_new, new_tiles, True).status == 201
        candidates = api(f"https://pool.test/v1/pool/episodes/{episode_key}?duration_ms={duration_new}",
                         None, {"Authorization": f"Bearer {READ_KEY}"}, 2)
        assert candidates.status == 200
        entries = json.loads(candidates.data)["candidates"]
        assert [entry["fp"] for entry in entries] == [fp_new, fp_old]
        assert entries[0]["n_tiles"] == len(new_tiles)
        assert [entry["duration_delta_ms"] for entry in entries] == [0, duration_new - duration_old]
        assert entries[0]["score"] == 100
        assert entries[0]["match"] == "episode_key+duration"
        assert entries[0]["relation"] == "same_length"

        # Once this legacy key can be resolved to v2 hints, the route delegates
        # to the shared ranker and still includes the legacy duration delta.
        core.db.x(
            "UPDATE pool_hints SET imdb=?,season=?,episode=?,size_bytes=?,filename_norm=? "
            "WHERE fp=? AND profile=?",
            ("tt1234567", 1, 2, 1_000_000, "series.s01e02", fp_new, "nvmt-v1-g5"),
        )
        ranked = api(f"https://pool.test/v1/pool/episodes/{episode_key}?duration_ms={duration_new}",
                     None, {"Authorization": f"Bearer {READ_KEY}"}, 2)
        ranked_entries = json.loads(ranked.data)["candidates"]
        assert [entry["fp"] for entry in ranked_entries] == [fp_new]
        assert ranked_entries[0]["score"] == 100
        assert ranked_entries[0]["match"] == "imdb+season+episode+duration"
        assert ranked_entries[0]["relation"] == "same_length"
        assert ranked_entries[0]["duration_delta_ms"] == 0

        # Candidate verification fetches only the requested sparse set, not the full NVMT.
        sparse_url = (f"https://pool.test/v1/pool/{fp_new}/nvmt?profile=nvmt-v1-g5"
                      "&slots=0,2,8,11")
        sparse = api(sparse_url, None, {"Authorization": f"Bearer {READ_KEY}"}, 2)
        assert sparse.status == 200
        assert int(sparse.headers["x-openseek-n-tiles"]) == len(new_tiles)
        assert decode_nvmt(fp_new, "nvmt-v1-g5", duration_new, sparse.data)[2] == new_tiles
    finally:
        core.stop()


def test_cold_g10_sparse_request_fetches_tile_documents_not_merged_package(tmp_path, monkeypatch):
    tg = FakeTelegramBotAPI()

    class NoSourceProxy:
        def stop(self):
            pass

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(app_module.asyncio, "to_thread", inline_to_thread)
    core = Core(Settings(data_dir=tmp_path / "pool-host", pool_enabled=True,
                         pool_read_key=READ_KEY, pool_write_key=WRITE_KEY,
                         telegram_bot_token="123456:test", telegram_channel_id="-100123"),
                pool_storage=TelegramBotStore("123456:test", "-100123", opener=tg.urlopen),
                proxy=NoSourceProxy())
    api = ASGIPoolAPI(create_api_app(core))
    fp = hashlib.sha256(b"sparse single tile route source").hexdigest()
    duration_ms, grid = 40_000, 10_000
    tiles = {k: (k * grid, jpeg((20 + k, 50 + k, 80 + k))) for k in range(4)}
    try:
        # Keep the old merged pack available as a fallback, then clear the LRU
        # so any accidental sparse call to package() must hit Telegram.
        assert core.pool.upload_package(fp, "nvmt-v1-g10", duration_ms,
                                        nvmt_package(grid, duration_ms, tiles))["stored"]
        for k, (pts_ms, data) in tiles.items():
            url = (f"https://pool.test/v1/pool/{fp}/tiles/{k}.jpg?profile={PROFILE}"
                   f"&pts_ms={pts_ms}&duration_ms={duration_ms}&n_tiles=4")
            uploaded = api(url, data, {"Authorization": f"Bearer {WRITE_KEY}",
                                      "Content-Type": "image/jpeg"}, 2)
            assert uploaded.status == 201
        with core.pool._package_cache.lock:
            core.pool._package_cache.items.clear()
            core.pool._package_cache.bytes = 0

        before_get, before_download = tg.get_file_calls, tg.download_calls
        sparse_url = (f"https://pool.test/v1/pool/{fp}/nvmt?profile=nvmt-v1-g10"
                      "&slots=0,1,2,3")
        sparse = api(sparse_url, None, {"Authorization": f"Bearer {READ_KEY}"}, 2)
        assert sparse.status == 200
        assert decode_nvmt(fp, "nvmt-v1-g10", duration_ms, sparse.data)[2] == tiles
        assert tg.get_file_calls - before_get == len(tiles)
        assert tg.download_calls - before_download == len(tiles)

        g5_tiles = {k * 2: (k * grid, data) for k, (_pts, data) in tiles.items()}
        assert core.pool.upload_package(fp, "nvmt-v1-g5", duration_ms,
                                        nvmt_package(5_000, duration_ms, g5_tiles))["stored"]
        before_get, before_download = tg.get_file_calls, tg.download_calls
        sparse_g5 = api(
            f"https://pool.test/v1/pool/{fp}/nvmt?profile=nvmt-v1-g5&slots=0,1,2,3,4,5,6,7",
            None, {"Authorization": f"Bearer {READ_KEY}"}, 2)
        assert sparse_g5.status == 200
        assert decode_nvmt(fp, "nvmt-v1-g5", duration_ms, sparse_g5.data)[2] == g5_tiles
        assert tg.get_file_calls - before_get == len(tiles)
        assert tg.download_calls - before_download == len(tiles)
    finally:
        core.stop()


def test_sparse_request_falls_back_to_the_merged_package_on_bad_tile_rows(tmp_path, monkeypatch):
    tg = FakeTelegramBotAPI()

    class NoSourceProxy:
        def stop(self):
            pass

    core = Core(Settings(data_dir=tmp_path / "pool-host", pool_enabled=True,
                         pool_read_key=READ_KEY, pool_write_key=WRITE_KEY,
                         telegram_bot_token="123456:test", telegram_channel_id="-100123"),
                pool_storage=TelegramBotStore("123456:test", "-100123", opener=tg.urlopen),
                proxy=NoSourceProxy())
    api = ASGIPoolAPI(create_api_app(core))
    fp = hashlib.sha256(b"sparse fallback over bad tile rows").hexdigest()
    duration_ms, grid = 40_000, 10_000
    tiles = {k: (k * grid, jpeg((40 + k, 60 + k, 90 + k))) for k in range(4)}
    try:
        assert core.pool.upload_package(fp, "nvmt-v1-g10", duration_ms,
                                        nvmt_package(grid, duration_ms, tiles))["stored"]
        # A stale tile row (wrong sha) must not hard-fail the sparse route.
        core.db.x("INSERT INTO pool_tiles VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (fp, PROFILE, 0, 0, duration_ms, 4, 99, "f" * 64, "fake-blob", 201, time.time()))
        with core.pool._package_cache.lock:
            core.pool._package_cache.items.clear()
            core.pool._package_cache.bytes = 0
        before_get, before_download = tg.get_file_calls, tg.download_calls
        sparse = api(f"https://pool.test/v1/pool/{fp}/nvmt?profile=nvmt-v1-g10&slots=0,1,2,3",
                     None, {"Authorization": f"Bearer {READ_KEY}"}, 2)
        assert sparse.status == 200
        assert decode_nvmt(fp, "nvmt-v1-g10", duration_ms, sparse.data)[2] == tiles
        # Fallback path: one failed tile getFile + one merged-package fetch.
        assert tg.get_file_calls - before_get == 2
        assert tg.download_calls - before_download == 1
    finally:
        core.stop()


def test_admin_pool_purge_is_dry_run_by_default_and_sweeps_orphans(tmp_path):
    tg = FakeTelegramBotAPI()

    class NoSourceProxy:
        def stop(self):
            pass

    core = Core(Settings(data_dir=tmp_path / "pool-host", admin_port=8788,
                         admin_token="test-admin-token", pool_enabled=True,
                         pool_read_key=READ_KEY, pool_write_key=WRITE_KEY,
                         telegram_bot_token="123456:test", telegram_channel_id="-100123"),
                pool_storage=TelegramBotStore("123456:test", "-100123", opener=tg.urlopen),
                proxy=NoSourceProxy())

    async def purge(body):
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_admin_app(core), client=("127.0.0.1", 45000)),
                base_url="http://127.0.0.1:8788") as client:
            return await client.post("/admin/pool/purge", headers={"X-OpenSeek-Admin": core.admin_token},
                                     json=body)

    try:
        now = time.time()
        live_tile = jpeg()
        tg.files["live-tile"] = live_tile
        stale_tile_fp = hashlib.sha256(b"stale tile").hexdigest()
        live_tile_fp = hashlib.sha256(b"live tile").hexdigest()
        stale_package_fp = hashlib.sha256(b"stale package").hexdigest()
        core.db.x("INSERT INTO pool_tiles VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (stale_tile_fp, PROFILE, 0, 0, 10_000, 1, 100, "0" * 64, "missing-tile", 101, now))
        core.db.x("INSERT INTO pool_tiles VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (live_tile_fp, PROFILE, 0, 0, 10_000, 1, len(live_tile),
                   hashlib.sha256(live_tile).hexdigest(), "live-tile", 102, now))
        core.db.x("INSERT INTO pool_packages VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (stale_package_fp, "nvmt-v1-g10", 10_000, 1, 1, 200, "1" * 64,
                   "missing-package", 103, now))
        core.db.x("INSERT INTO pool_episode_packages VALUES(?,?,?,?,?)",
                  ("2" * 64, stale_package_fp, "nvmt-v1-g10", 10_000, now))
        core.db.x("INSERT INTO pool_episode_packages VALUES(?,?,?,?,?)",
                  ("3" * 64, live_tile_fp, "nvmt-v1-g10", 10_000, now))
        core.db.x("INSERT INTO pool_outbox VALUES(?,?,?,?,?,?,?,?)",
                  ("4" * 64, 0, 0, 10_000, 1, now - 60 * 86400, 6, 0))
        core.db.x("INSERT INTO pool_outbox VALUES(?,?,?,?,?,?,?,?)",
                  ("5" * 64, 0, 0, 10_000, 1, now - 60 * 86400, 5, 0))
        for i in range(128):
            core.db.x("INSERT INTO pool_outbox VALUES(?,?,?,?,?,?,?,?)",
                      (f"{i + 6:064x}", 0, 0, 10_000, 1, now - 60 * 86400, 8, 0))
        core.db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before_size = core.db.path.stat().st_size

        dry = asyncio.run(purge({}))
        assert dry.status_code == 200
        assert dry.json()["dry_run"] is True
        assert dry.json()["pool_tiles"] == 1
        assert dry.json()["pool_packages"] == 1
        assert dry.json()["pool_episode_packages"] == 2
        assert dry.json()["pool_outbox"] == 129
        assert core.db.q("SELECT COUNT(*) FROM pool_packages")[0][0] == 1

        done = asyncio.run(purge({"dry_run": False, "vacuum": True}))
        assert done.status_code == 200
        result = done.json()
        assert result["dry_run"] is False and result["vacuumed"] is True
        assert result["pool_packages"] == 1 and result["pool_outbox"] == 129
        assert core.db.q("SELECT COUNT(*) FROM pool_tiles")[0][0] == 1
        assert core.db.q("SELECT COUNT(*) FROM pool_packages")[0][0] == 0
        assert core.db.q("SELECT COUNT(*) FROM pool_episode_packages")[0][0] == 0
        assert core.db.q("SELECT COUNT(*) FROM pool_outbox")[0][0] == 1
        assert set(tg.deleted_messages) == {101, 103}
        assert core.db.path.stat().st_size < before_size
    finally:
        core.stop()


def test_real_sample_decode_to_telegram_pool_and_second_instance(tmp_path, monkeypatch):
    """Optional real-media smoke test for /home/yat/sample.mkv through the pool HTTP routes."""
    sample = Path("/home/yat/sample.mkv")
    if not sample.is_file():
        pytest.skip("local sample.mkv is not present")

    async def inline_to_thread(function, *args, **kwargs):
        # Avoid this workspace's asyncio default-executor shutdown hang in socket-free ASGI tests.
        return function(*args, **kwargs)

    monkeypatch.setattr(app_module.asyncio, "to_thread", inline_to_thread)
    size = sample.stat().st_size
    with sample.open("rb") as file:
        head = file.read(65_536)
        file.seek(max(0, size - 65_536))
        tail = file.read(65_536)
    fp = fingerprint(size, head, tail)

    class LocalReader(io.RawIOBase):
        def __init__(self):
            self.file = sample.open("rb")
            self.size = size
            self.accurate_mode = False

        def readable(self):
            return True

        def seekable(self):
            return True

        def tell(self):
            return self.file.tell()

        def seek(self, offset, whence=0):
            return self.file.seek(offset, whence)

        def readinto(self, buf):
            return self.file.readinto(buf)

        def configure_for_bitrate(self, rate):
            pass

        def close(self):
            self.file.close()
            super().close()

    file_reader = LocalReader()
    extractor = Extractor(file_reader, "matroska", max_dim=8192, max_duration_s=12 * 3600)
    try:
        info = extractor.info()
        pts_ms, tile_bytes = extractor.grab(10_000, accurate=False, tolerance_ms=5_000)
    finally:
        extractor.close()
        file_reader.close()
    k = round(pts_ms / 10_000)
    duration_ms, n_tiles = info.duration_ms, tile_count(info.duration_ms)

    tg = FakeTelegramBotAPI()

    class NoSourceProxy:
        def stop(self):
            pass

    core = Core(Settings(data_dir=tmp_path / "pool", pool_enabled=True,
                         pool_read_key=READ_KEY, pool_write_key=WRITE_KEY,
                         telegram_bot_token="123456:test", telegram_channel_id="-100123"),
                pool_storage=TelegramBotStore("123456:test", "-100123", opener=tg.urlopen),
                proxy=NoSourceProxy())
    api = ASGIPoolAPI(create_api_app(core))
    publisher = PoolClient("https://pool.test", READ_KEY, WRITE_KEY, requester=api)
    viewer = PoolClient("https://pool.test", READ_KEY, requester=api)
    contributor_db = DB(tmp_path / "contributor.sqlite")
    contributor_cache = Cache(tmp_path / "contributor-cache", contributor_db, 1 << 20, 30)
    sync = PoolSync(contributor_db, contributor_cache, publisher)
    viewer_db = DB(tmp_path / "viewer.sqlite")
    viewer_cache = Cache(tmp_path / "viewer-cache", viewer_db, 1 << 20, 30)
    try:
        contributor_cache.set_media(fp, duration_ms, n_tiles)
        contributor_cache.put_tile(fp, k, pts_ms, tile_bytes)
        sync.enqueue(fp, k)
        sync.start()
        deadline = time.time() + 5
        while sync.pending() and time.time() < deadline:
            time.sleep(0.01)
        assert sync.pending() == 0 and tg.messages == 1

        viewer_cache.set_media(fp, duration_ms, n_tiles)
        shared = viewer.get_tile(fp, k)
        assert shared == (tile_bytes, pts_ms)
        viewer_cache.put_tile(fp, k, shared[1], shared[0])
        assert viewer_cache.tile_bytes(fp, k) == tile_bytes
        assert info.container == "matroska" and info.codec == "hevc"
    finally:
        sync.stop()
        contributor_db.close()
        viewer_db.close()
        core.stop()


def test_pool_verified_tie_break(tmp_path):
    """Verified package wins tie-break against unverified candidate with the same score."""
    db = DB(tmp_path / "verified_test.sqlite")
    tg = FakeTelegramBotAPI()
    storage = TelegramBotStore("123456:test", "-100123", opener=tg.urlopen)
    svc = PoolService(db, storage, READ_KEY, WRITE_KEY)
    fp1 = "1" * 64
    fp2 = "2" * 64
    duration = 20_000
    image = jpeg()
    pkg = nvmt_package(5_000, duration, {0: (0, image)})
    try:
        svc.upload_package(fp2, "nvmt-v1-g5", duration, pkg, imdb="tt1234567", season=0, episode=0)
        svc.upload_package(fp1, "nvmt-v1-g5", duration, pkg, imdb="tt1234567", season=0, episode=0)
        db.x("UPDATE pool_hints SET created=1 WHERE fp IN (?,?)", (fp1, fp2))

        # Both have score 100.
        candidates = svc.candidates(duration_ms=duration, imdb="tt1234567", season=0, episode=0)
        assert len(candidates) == 2
        assert candidates[0]["score"] == 100 and candidates[1]["score"] == 100

        # Mark fp2 as verified.
        assert db.set_hint_verified(fp2, "nvmt-v1-g5", True)
        assert db.is_hint_verified(fp2, "nvmt-v1-g5")

        candidates_after = svc.candidates(duration_ms=duration, imdb="tt1234567", season=0, episode=0)
        assert candidates_after[0]["fp"] == fp2
        assert candidates_after[1]["fp"] == fp1

        # Unverify restores normal ordering.
        assert db.set_hint_verified(fp2, "nvmt-v1-g5", False)
        assert not db.is_hint_verified(fp2, "nvmt-v1-g5")
    finally:
        db.close()


def test_pool_device_offset_and_rejection_isolation(tmp_path, monkeypatch):
    """Per-device offsets and rejections are isolated to the calling device and work key."""
    class NoSourceProxy:
        def stop(self):
            pass

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(app_module.asyncio, "to_thread", inline_to_thread)
    tg = FakeTelegramBotAPI()
    storage = TelegramBotStore("123456:test", "-100123", opener=tg.urlopen)
    core = Core(Settings(data_dir=tmp_path / "pool-corrections", pool_enabled=True,
                         pool_read_key=READ_KEY, pool_write_key=WRITE_KEY,
                         telegram_bot_token="123456:test", telegram_channel_id="-100123"),
                pool_storage=storage, proxy=NoSourceProxy())
    api = ASGIPoolAPI(create_api_app(core))
    fp1 = hashlib.sha256(b"release 1").hexdigest()
    fp2 = hashlib.sha256(b"release 2").hexdigest()
    duration = 20_000
    pkg = nvmt_package(5_000, duration, {0: (0, jpeg())})
    work_key = hashlib.sha256(b"openseek-work-v1\nimdb:tt1234567").hexdigest()
    token_a, token_b = "A" * 43, "B" * 43
    dev_a = core.auth.register("device A", hashlib.sha256(token_a.encode()).hexdigest(), role="guest")
    dev_b = core.auth.register("device B", hashlib.sha256(token_b.encode()).hexdigest(), role="guest")

    try:
        for fp in (fp1, fp2):
            res = api(f"https://pool.test/v1/pool/{fp}/nvmt?profile=nvmt-v1-g5&duration_ms={duration}"
                      f"&imdb=tt1234567&season=0&episode=0",
                      pkg, {"Authorization": f"Bearer {WRITE_KEY}", "Content-Type": "application/vnd.openseek.nvmt"}, 2)
            assert res.status == 201

        # Query candidates for dev_a
        r = api(f"https://pool.test/v1/pool/candidates?imdb=tt1234567&season=0&episode=0&duration_ms={duration}"
                f"&work_key={work_key}", None, {"Authorization": f"Bearer {token_a}"}, 2)
        assert r.status == 200
        items = json.loads(r.data)["candidates"]
        assert len(items) == 2
        assert all(c["b_ms"] == 0 for c in items)

        # Set offset for dev_a on fp1
        put_off = api(f"https://pool.test/v1/pool/candidates/{fp1}/offset",
                      json.dumps({"work_key": work_key, "b_ms": 1500}).encode(),
                      {"Authorization": f"Bearer {token_a}", "Content-Type": "application/json"}, 2)
        assert put_off.status == 200

        spoofed = api(f"https://pool.test/v1/pool/candidates/{fp1}/offset?device_id={dev_a}",
                      json.dumps({"work_key": work_key, "b_ms": 9999}).encode(),
                      {"Authorization": f"Bearer {READ_KEY}", "Content-Type": "application/json"}, 2)
        assert spoofed.status == 401
        assert core.db.get_pool_offset(dev_a, work_key, fp1) == 1500

        # dev_a sees b_ms = 1500 on fp1
        r_a = api(f"https://pool.test/v1/pool/candidates?imdb=tt1234567&season=0&episode=0&duration_ms={duration}"
                  f"&work_key={work_key}", None, {"Authorization": f"Bearer {token_a}"}, 2)
        items_a = json.loads(r_a.data)["candidates"]
        c1_a = next(c for c in items_a if c["fp"] == fp1)
        assert c1_a["b_ms"] == 1500

        # dev_b sees b_ms = 0 (isolated)
        r_b = api(f"https://pool.test/v1/pool/candidates?imdb=tt1234567&season=0&episode=0&duration_ms={duration}"
                  f"&work_key={work_key}", None, {"Authorization": f"Bearer {token_b}"}, 2)
        items_b = json.loads(r_b.data)["candidates"]
        c1_b = next(c for c in items_b if c["fp"] == fp1)
        assert c1_b["b_ms"] == 0

        # Reject fp1 for dev_a
        put_rej = api(f"https://pool.test/v1/pool/candidates/{fp1}/reject",
                      json.dumps({"work_key": work_key, "days": 7}).encode(),
                      {"Authorization": f"Bearer {token_a}", "Content-Type": "application/json"}, 2)
        assert put_rej.status == 200

        # dev_a no longer receives fp1
        r_a_rej = api(f"https://pool.test/v1/pool/candidates?imdb=tt1234567&season=0&episode=0&duration_ms={duration}"
                      f"&work_key={work_key}", None, {"Authorization": f"Bearer {token_a}"}, 2)
        items_a_rej = json.loads(r_a_rej.data)["candidates"]
        assert len(items_a_rej) == 1
        assert items_a_rej[0]["fp"] == fp2

        # dev_b still receives both
        r_b_rej = api(f"https://pool.test/v1/pool/candidates?imdb=tt1234567&season=0&episode=0&duration_ms={duration}"
                      f"&work_key={work_key}", None, {"Authorization": f"Bearer {token_b}"}, 2)
        assert len(json.loads(r_b_rej.data)["candidates"]) == 2

        # Clear rejection for dev_a
        del_rej = api(f"https://pool.test/v1/pool/candidates/{fp1}/reject",
                      json.dumps({"work_key": work_key}).encode(),
                      {"Authorization": f"Bearer {token_a}", "Content-Type": "application/json"}, 2,
                      method="DELETE")
        assert del_rej.status == 204

        # fp1 is visible again
        r_a_restored = api(f"https://pool.test/v1/pool/candidates?imdb=tt1234567&season=0&episode=0&duration_ms={duration}"
                           f"&work_key={work_key}", None, {"Authorization": f"Bearer {token_a}"}, 2)
        assert len(json.loads(r_a_restored.data)["candidates"]) == 2

        # Clear offset
        del_off = api(f"https://pool.test/v1/pool/candidates/{fp1}/offset",
                      json.dumps({"work_key": work_key}).encode(),
                      {"Authorization": f"Bearer {token_a}", "Content-Type": "application/json"}, 2,
                      method="DELETE")
        assert del_off.status == 204
        r_a_no_off = api(f"https://pool.test/v1/pool/candidates?imdb=tt1234567&season=0&episode=0&duration_ms={duration}"
                         f"&work_key={work_key}", None, {"Authorization": f"Bearer {token_a}"}, 2)
        c1_no_off = next(c for c in json.loads(r_a_no_off.data)["candidates"] if c["fp"] == fp1)
        assert c1_no_off["b_ms"] == 0
    finally:
        core.stop()
