"""Two-instance Telegram-backed pool tests using a local Bot API-compatible fake."""
from __future__ import annotations

import hashlib
import io
import json
import asyncio
import time
import threading
from pathlib import Path
from email.parser import BytesParser
from email.policy import default
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from PIL import Image
import pytest

import openseek.app as app_module
from openseek.app import Core, create_api_app
from openseek.cache import Cache, PROFILE
from openseek.config import Settings
from openseek.db import DB
from openseek.engine import Extractor
from openseek.fetch import fingerprint
from openseek.jobs import Source
from openseek.pool import PoolClient, PoolError, PoolService, PoolSync, TelegramBotStore
from openseek.scheduler import tile_count
from openseek.vtt import render

READ_KEY = "r" * 40
WRITE_KEY = "w" * 40


def jpeg(color=(21, 49, 90)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (320, 180), color).save(out, "JPEG", quality=75)
    return out.getvalue()


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
            file_id = parse_qs(body.decode()).get("file_id", [""])[0]
            if file_id not in self.files:
                return FakeResponse(b'{"ok":false}', status=404)
            return FakeResponse(json.dumps({"ok": True, "result": {
                "file_path": f"docs/{file_id}.jpg"}}).encode())
        if "/file/bot" in url.path:
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

    def __call__(self, url, data, headers, timeout):
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
            "http_version": "1.1", "method": "PUT" if data is not None else "GET",
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

        # Its normal OpenSeek VTT references the local HTTP tile endpoint.
        vtt = render("https://receiver.example", "cap-value", "preview-value", 123,
                     "tiles", {1: 10_000}, duration_ms, n_tiles, 5_000)
        assert "/v1/c/cap-value/preview-value/tiles/1.jpg" in vtt
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
                                                         requests=1, bytes=len(original), via="a"),
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
        source.alt = None
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
