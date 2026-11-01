"""Shared, Telegram-backed storage for OpenSeek's already-generated JPEG tiles.

The pool never receives video URLs and never decodes video. Each participating
OpenSeek instance extracts its own frames, then may publish/read individual tiles.
Telegram is blob storage; SQLite is the lookup index.
"""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import logging
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .cache import PROFILE, TILE_H, TILE_W, jpeg_ok
from .db import DB
from .ids import is_fingerprint
from .scheduler import tile_count

log = logging.getLogger("openseek.pool")
MAX_TILE_BYTES = 256 * 1024
MAX_DURATION_MS = 12 * 60 * 60 * 1000
TILE_INTERVAL_MS = 10_000


class _DualStackHTTPSConnection(http.client.HTTPSConnection):
    """Prefer IPv4 but fall back across resolved addresses before timing out a Bot API request.

    Some hosts publish both A and AAAA records while the client network has no working IPv6 route.
    The standard urllib path can spend the full request timeout on that first address. Connect to
    available addresses in family-preference order, with a short per-address connect timeout.
    HTTPS proxies keep the stdlib tunnel behavior.
    """
    def connect(self) -> None:
        if self._tunnel_host:
            return super().connect()
        host = self.host.strip("[]")
        try:
            addresses = socket.getaddrinfo(host, self.port, type=socket.SOCK_STREAM)
        except OSError:
            return super().connect()
        addresses.sort(key=lambda item: 0 if item[0] == socket.AF_INET else 1)
        last_error: OSError | None = None
        for family, socktype, proto, _canonname, sockaddr in addresses:
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                if self.source_address:
                    sock.bind(self.source_address)
                if self.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(min(float(self.timeout), 2.5))
                sock.connect(sockaddr)
                sock.settimeout(self.timeout)
                self.sock = self._context.wrap_socket(sock, server_hostname=host)
                return
            except OSError as exc:
                last_error = exc
                if sock is not None:
                    sock.close()
        if last_error is not None:
            raise last_error
        return super().connect()


class _DualStackHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_DualStackHTTPSConnection, req, context=self._context)


def _urlopen(req, timeout: float = 8.0):
    """urllib opener with bounded IPv4/IPv6 fallback for Telegram and remote pool hosts."""
    return urllib.request.build_opener(_DualStackHTTPSHandler()).open(req, timeout=timeout)


class PoolError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


class TelegramBotStore:
    """Store exact JPEG bytes as Telegram documents so Telegram won't recompress them."""

    def __init__(self, token: str, channel_id: str, timeout_s: float = 8.0,
                 api_base: str = "https://api.telegram.org", opener=None):
        if not token or not channel_id:
            raise ValueError("Telegram bot token and channel ID are required")
        self._token = token
        self.channel_id = channel_id
        self.timeout_s = timeout_s
        self.api_base = api_base.rstrip("/")
        self.opener = opener or _urlopen

    def _api_url(self, method: str) -> str:
        return f"{self.api_base}/bot{self._token}/{method}"

    def _json(self, method: str, params: dict) -> dict:
        body = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(self._api_url(method), data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self.opener(req, timeout=self.timeout_s) as response:
                result = json.loads(response.read(1024 * 1024))
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise PoolError(503, "telegram_unavailable") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise PoolError(502, "telegram_error")
        return result["result"]

    def put(self, data: bytes, caption: str) -> tuple[str, int]:
        boundary = "openseek-" + secrets.token_hex(16)
        pieces: list[bytes] = []

        def field(name: str, value: str) -> None:
            pieces.extend([f"--{boundary}\r\n".encode(),
                           f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                           value.encode(), b"\r\n"])

        field("chat_id", self.channel_id)
        field("caption", caption)
        pieces.extend([f"--{boundary}\r\n".encode(),
                       b'Content-Disposition: form-data; name="document"; filename="tile.jpg"\r\n',
                       b"Content-Type: image/jpeg\r\n\r\n", data, b"\r\n",
                       f"--{boundary}--\r\n".encode()])
        req = urllib.request.Request(self._api_url("sendDocument"), data=b"".join(pieces), method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with self.opener(req, timeout=self.timeout_s) as response:
                result = json.loads(response.read(1024 * 1024))
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise PoolError(503, "telegram_unavailable") from exc
        message = result.get("result") if isinstance(result, dict) and result.get("ok") is True else None
        doc = message.get("document") if isinstance(message, dict) else None
        file_id = doc.get("file_id") if isinstance(doc, dict) else None
        message_id = message.get("message_id") if isinstance(message, dict) else None
        if not isinstance(file_id, str) or not file_id or not isinstance(message_id, int):
            raise PoolError(502, "telegram_error")
        return file_id, message_id

    def get(self, file_id: str) -> bytes:
        result = self._json("getFile", {"file_id": file_id})
        path = result.get("file_path") if isinstance(result, dict) else None
        if not isinstance(path, str) or not path or ".." in Path(path).parts:
            raise PoolError(502, "telegram_error")
        url = f"{self.api_base}/file/bot{self._token}/{urllib.parse.quote(path, safe='/')}"
        req = urllib.request.Request(url, method="GET")
        try:
            with self.opener(req, timeout=self.timeout_s) as response:
                data = response.read(MAX_TILE_BYTES + 1)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise PoolError(503, "telegram_unavailable") from exc
        if len(data) > MAX_TILE_BYTES:
            raise PoolError(502, "telegram_file_too_large")
        return data


def validate_tile(fp: str, profile: str, k: int, pts_ms: int, duration_ms: int,
                  n_tiles: int, data: bytes) -> str:
    if not is_fingerprint(fp) or profile != PROFILE:
        raise PoolError(400, "bad_key")
    if not isinstance(k, int) or not 0 <= k < 1_000_000:
        raise PoolError(400, "bad_tile_index")
    if not isinstance(duration_ms, int) or not 0 < duration_ms <= MAX_DURATION_MS:
        raise PoolError(400, "bad_duration")
    expected_n = tile_count(duration_ms)
    if n_tiles != expected_n or k >= n_tiles:
        raise PoolError(400, "bad_tile_count")
    if not isinstance(pts_ms, int) or not 0 <= pts_ms <= duration_ms or abs(pts_ms - k * TILE_INTERVAL_MS) > 5000:
        raise PoolError(400, "bad_timestamp")
    if len(data) > MAX_TILE_BYTES or not jpeg_ok(data):
        raise PoolError(400, "bad_jpeg")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "JPEG" or image.size != (TILE_W, TILE_H):
                raise PoolError(400, "bad_jpeg_dimensions")
            image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise PoolError(400, "bad_jpeg") from exc
    return hashlib.sha256(data).hexdigest()


class PoolService:
    def __init__(self, db: DB, storage, read_key: str, write_key: str):
        self.db, self.storage = db, storage
        self.read_key, self.write_key = read_key, write_key
        self._lock = threading.RLock()

    def authorize(self, given: str | None, write: bool = False) -> bool:
        wanted = self.write_key if write else self.read_key
        return bool(given and wanted and secrets.compare_digest(given, wanted))

    def upload(self, fp: str, profile: str, k: int, pts_ms: int, duration_ms: int,
               n_tiles: int, data: bytes, contributor: str = "") -> dict:
        digest = validate_tile(fp, profile, k, pts_ms, duration_ms, n_tiles, data)
        with self._lock:
            row = self.db.q("SELECT sha256,duration_ms,n_tiles FROM pool_tiles WHERE fp=? AND profile=? AND k=?",
                            (fp, profile, k))
            if row:
                if row[0][0] != digest:
                    raise PoolError(409, "tile_conflict")
                return {"stored": False, "duplicate": True, "sha256": digest}
            media = self.db.q("SELECT duration_ms,n_tiles FROM pool_tiles WHERE fp=? AND profile=? LIMIT 1",
                              (fp, profile))
            if media and (media[0][0] != duration_ms or media[0][1] != n_tiles):
                raise PoolError(409, "media_metadata_conflict")
            caption = f"openseek:v1:{fp}:{profile}:{k}:{digest}"
            file_id, message_id = self.storage.put(data, caption)
            self.db.x("INSERT INTO pool_tiles(fp,profile,k,pts_ms,duration_ms,n_tiles,size,sha256,file_id,message_id,created) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (fp, profile, k, pts_ms, duration_ms, n_tiles, len(data), digest, file_id, message_id, time.time()))
        return {"stored": True, "duplicate": False, "sha256": digest}

    def manifest(self, fp: str, profile: str) -> dict | None:
        if not is_fingerprint(fp) or profile != PROFILE:
            raise PoolError(400, "bad_key")
        rows = self.db.q("SELECT k,pts_ms,duration_ms,n_tiles,size,sha256 FROM pool_tiles "
                         "WHERE fp=? AND profile=? ORDER BY k", (fp, profile))
        if not rows:
            return None
        duration_ms, n_tiles = rows[0][2], rows[0][3]
        return {"fp": fp, "profile": profile, "duration_ms": duration_ms, "n_tiles": n_tiles,
                "tiles": [{"k": r[0], "pts_ms": r[1], "size": r[4], "sha256": r[5]} for r in rows]}

    def tile(self, fp: str, profile: str, k: int) -> tuple[bytes, dict] | None:
        row = self.db.q("SELECT pts_ms,duration_ms,n_tiles,size,sha256,file_id FROM pool_tiles "
                        "WHERE fp=? AND profile=? AND k=?", (fp, profile, k))
        if not row:
            return None
        pts_ms, duration_ms, n_tiles, size, digest, file_id = row[0]
        data = self.storage.get(file_id)
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise PoolError(502, "stored_tile_corrupt")
        try:
            validate_tile(fp, profile, k, pts_ms, duration_ms, n_tiles, data)
        except PoolError as exc:
            raise PoolError(502, "stored_tile_corrupt") from exc
        if not jpeg_ok(data):
            raise PoolError(502, "stored_tile_corrupt")
        return data, {"pts_ms": pts_ms, "duration_ms": duration_ms, "n_tiles": n_tiles,
                      "sha256": digest, "profile": profile}


class PoolClient:
    """Client-side pool access. A miss or outage is non-fatal; local extraction continues."""

    def __init__(self, url: str, read_key: str, write_key: str = "", timeout_s: float = 2.0,
                 requester=None):
        self.url, self.read_key, self.write_key = url.rstrip("/"), read_key, write_key
        self.timeout_s = timeout_s
        self.requester = requester
        self._misses: dict[tuple[str, int], float] = {}
        self._lock = threading.Lock()

    def _request(self, url: str, key: str, data: bytes | None = None, headers: dict | None = None):
        hs = {"Authorization": f"Bearer {key}", **(headers or {})}
        if self.requester is not None:
            try:
                return self.requester(url, data, hs, self.timeout_s)
            except (OSError, TimeoutError):
                return None
        req = urllib.request.Request(url, data=data, headers=hs, method="PUT" if data is not None else "GET")
        try:
            return _urlopen(req, timeout=self.timeout_s)
        except urllib.error.HTTPError as response:
            return response
        except (OSError, urllib.error.URLError, TimeoutError):
            return None

    def get_tile(self, fp: str, k: int) -> tuple[bytes, int] | None:
        mark = (fp, k)
        now = time.monotonic()
        with self._lock:
            if self._misses.get(mark, 0) > now:
                return None
        url = f"{self.url}/v1/pool/{fp}/tiles/{k}.jpg?profile={urllib.parse.quote(PROFILE)}"
        response = self._request(url, self.read_key)
        if response is None:
            with self._lock:
                self._misses[mark] = now + 10
            return None
        try:
            status = getattr(response, "status", getattr(response, "status_code", 0))
            if status == 404:
                with self._lock:
                    self._misses[mark] = now + 30
                return None
            if status != 200:
                with self._lock:
                    self._misses[mark] = now + 10
                return None
            data = response.read(MAX_TILE_BYTES + 1)
            pts = int(response.headers.get("X-OpenSeek-Pts-Ms", "-1"))
            duration = int(response.headers.get("X-OpenSeek-Duration-Ms", "-1"))
            n_tiles = int(response.headers.get("X-OpenSeek-N-Tiles", "-1"))
            digest = response.headers.get("X-OpenSeek-Sha256", "")
            if (len(data) > MAX_TILE_BYTES or not jpeg_ok(data) or
                    response.headers.get("X-OpenSeek-Profile") != PROFILE or
                    len(digest) != 64 or hashlib.sha256(data).hexdigest() != digest):
                return None
            validate_tile(fp, PROFILE, k, pts, duration, n_tiles, data)
            with self._lock:
                self._misses.pop(mark, None)
            return data, pts
        except (OSError, ValueError, PoolError):
            return None
        finally:
            response.close()

    def publish(self, fp: str, k: int, pts_ms: int, duration_ms: int, n_tiles: int, data: bytes) -> bool:
        if not self.write_key:
            return False
        query = urllib.parse.urlencode({"profile": PROFILE, "pts_ms": pts_ms,
                                        "duration_ms": duration_ms, "n_tiles": n_tiles})
        url = f"{self.url}/v1/pool/{fp}/tiles/{k}.jpg?{query}"
        response = self._request(url, self.write_key, data,
                                 {"Content-Type": "image/jpeg", "Content-Length": str(len(data))})
        if response is None:
            return False
        try:
            status = getattr(response, "status", getattr(response, "status_code", 0))
            if status in (200, 201, 204):
                return True
            if status == 409:
                # First valid tile wins; a conflicting later render is not retried forever.
                return True
            return False
        finally:
            response.close()


class PoolSync:
    """Durable best-effort publisher for locally generated tiles."""

    def __init__(self, db: DB, cache, client: PoolClient):
        self.db, self.cache, self.client = db, cache, client
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="openseek-pool-outbox", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(3.0, self.client.timeout_s + 1))

    def enqueue(self, fp: str, k: int) -> None:
        entry = self.cache.entry(fp)
        if entry is None or not entry.duration_ms or not entry.n_tiles:
            return
        self.db.x("INSERT OR IGNORE INTO pool_outbox(fp,k,pts_ms,duration_ms,n_tiles,created) VALUES(?,?,?,?,?,?)",
                  (fp, k, entry.tiles.get(k, k * TILE_INTERVAL_MS), entry.duration_ms, entry.n_tiles, time.time()))
        self._wake.set()

    def pending(self) -> int:
        row = self.db.q("SELECT COUNT(*) FROM pool_outbox")
        return int(row[0][0]) if row else 0

    def _run(self) -> None:
        while not self._stop.is_set():
            rows = self.db.q("SELECT fp,k,pts_ms,duration_ms,n_tiles,attempts,retry_after FROM pool_outbox "
                             "ORDER BY created LIMIT 1")
            if not rows:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            fp, k, pts, duration, n_tiles, attempts, retry_after = rows[0]
            wait = retry_after - time.time()
            if wait > 0:
                self._wake.wait(min(wait, 2.0))
                self._wake.clear()
                continue
            data = self.cache.tile_bytes(fp, k)
            if data is None:
                self.db.x("DELETE FROM pool_outbox WHERE fp=? AND k=?", (fp, k))
                continue
            try:
                ok = self.client.publish(fp, k, pts, duration, n_tiles, data)
            except Exception as exc:  # network/outbox failures must not stop local extraction
                log.warning("pool upload failed (%s)", type(exc).__name__)
                ok = False
            if ok:
                self.db.x("DELETE FROM pool_outbox WHERE fp=? AND k=?", (fp, k))
            else:
                attempts += 1
                delay = min(300, 2 ** min(attempts, 8))
                self.db.x("UPDATE pool_outbox SET attempts=?,retry_after=? WHERE fp=? AND k=?",
                          (attempts, time.time() + delay, fp, k))
                log.info("pool upload deferred fp=%s tile=%d retry_s=%d", fp[:8], k, delay)

