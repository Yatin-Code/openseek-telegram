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
import math
import re
import secrets
import socket
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .cache import PROFILE, TILE_H, TILE_W, jpeg_ok
from .db import DB
from .ids import is_fingerprint
from .scheduler import tile_count

log = logging.getLogger("openseek.pool")
MAX_TILE_BYTES = 256 * 1024
MAX_NVMT_BYTES = 16 * 1024 * 1024
MAX_DURATION_MS = 12 * 60 * 60 * 1000
TILE_INTERVAL_MS = 10_000
NVMT_PROFILES = {"nvmt-v1-g5": 5_000, "nvmt-v1-g10": 10_000}
_EPISODE_KEY = re.compile(r"[0-9a-f]{64}\Z")
_IMDB_ID = re.compile(r"tt\d{1,10}\Z")
_TILE_FILENAME = re.compile(r"^openseek-tile-v1-(\d+)-(\d+)-(\d+)\.jpg$")


def normalize_hint_filename(filename: str | None) -> str | None:
    """Reduce a supplied basename to a bounded, title-like lookup hint."""
    if filename is None:
        return None
    if not isinstance(filename, str) or len(filename.encode("utf-8")) > 200:
        raise PoolError(400, "bad_hints")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in filename):
        raise PoolError(400, "bad_hints")
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    suffix = basename.rsplit(".", 1)[-1].lower() if "." in basename else ""
    video_extensions = {"mkv", "mp4", "avi", "mov", "webm", "m4v", "ts", "wmv", "mpg", "mpeg"}
    stem = basename[:-(len(suffix) + 1)] if suffix in video_extensions else basename
    normalized = unicodedata.normalize("NFKC", stem).lower()
    normalized = re.sub(r"[^a-z0-9]+", ".", normalized).strip(".")[:200].strip(".")
    return normalized or None


def validate_pool_hints(imdb: str | None, season: int, episode: int, duration_ms: int,
                        size_bytes: int | None, filename: str | None) -> dict:
    if imdb is not None and not _IMDB_ID.fullmatch(imdb):
        raise PoolError(400, "bad_hints")
    if (not isinstance(season, int) or not 0 <= season <= 9999 or
            not isinstance(episode, int) or not 0 <= episode <= 9999):
        raise PoolError(400, "bad_hints")
    if not isinstance(duration_ms, int) or not 1 <= duration_ms <= MAX_DURATION_MS:
        raise PoolError(400, "bad_hints")
    if size_bytes is not None and (not isinstance(size_bytes, int) or size_bytes < 0):
        raise PoolError(400, "bad_hints")
    filename_norm = normalize_hint_filename(filename)
    return {"imdb": imdb, "season": season, "episode": episode, "duration_ms": duration_ms,
            "size_bytes": size_bytes, "filename_norm": filename_norm}


def _timing_relation(target_ms: int, source_ms: int) -> tuple[str, int, int] | None:
    delta = abs(target_ms - source_ms)
    if delta > max(90_000, int(target_ms * 0.15)):
        return None
    ratio = target_ms / source_ms
    if delta <= 1000:
        relation = "same_length"
    elif min(abs(ratio - 25 / 24), abs(ratio - 24 / 25)) <= 0.0015 * (25 / 24):
        relation = "speed_24_25"
    else:
        relation = "length_differs"
    divisor = math.gcd(target_ms, source_ms)
    return relation, target_ms // divisor, source_ms // divisor


def _tile_filename(pts_ms: int, duration_ms: int, n_tiles: int) -> str:
    """Carry metadata beside the stable caption so channel-only readers can rebuild it."""
    return f"openseek-tile-v1-{pts_ms}-{duration_ms}-{n_tiles}.jpg"


def _tile_metadata_from_filename(filename: str, k: int) -> tuple[int, int, int] | None:
    match = _TILE_FILENAME.fullmatch(filename or "")
    if not match:
        return None
    pts_ms, duration_ms, n_tiles = map(int, match.groups())
    if (not 0 < duration_ms <= MAX_DURATION_MS or n_tiles != tile_count(duration_ms) or
            not 0 <= k < n_tiles or not 0 <= pts_ms <= duration_ms or
            abs(pts_ms - k * TILE_INTERVAL_MS) > 5000):
        return None
    return pts_ms, duration_ms, n_tiles


def _rank_hint_rows(rows: list[tuple], *, duration_ms: int, imdb: str | None, season: int,
                    episode: int, size_bytes: int | None, filename_norm: str | None,
                    device_id: str | None, work_key: str | None, db, limit: int) -> list[dict]:
    """Apply the shared catalog, timing, correction, and tie-break policy to normalized rows."""
    rejected_fps = (db.get_pool_rejections_for_device(device_id, work_key)
                    if (db is not None and device_id and work_key) else set())
    query_tokens = set((filename_norm or "").split(".")) - {""}
    ranked = []
    for (fp, profile, row_imdb, row_season, row_episode, source_ms, row_size,
         row_filename, n_tiles, grid, created, n_slots, verified) in rows:
        if fp in rejected_fps:
            continue
        relation_data = _timing_relation(duration_ms, int(source_ms))
        if relation_data is None:
            continue
        relation, a_num, a_den = relation_data
        match_parts = []
        score = 0
        same_catalog = bool(imdb and row_imdb == imdb)
        same_episode = same_catalog and int(row_season) == season and int(row_episode) == episode
        if same_episode:
            match_parts.extend(("imdb", "season", "episode", "duration"))
            score = {"same_length": 100, "speed_24_25": 95, "length_differs": 90}[relation]
        elif same_catalog and season == episode == int(row_season) == int(row_episode) == 0:
            match_parts.extend(("imdb", "duration"))
            score = 85
        elif filename_norm and row_filename:
            if filename_norm == row_filename and relation == "same_length":
                match_parts.extend(("filename", "duration"))
                score = 75
            else:
                candidate_tokens = set(str(row_filename).split(".")) - {""}
                overlap = len(query_tokens & candidate_tokens) / max(1, len(query_tokens | candidate_tokens))
                if overlap >= 0.6 and relation == "same_length":
                    match_parts.extend(("filename", "duration"))
                    score = 65
        if score < 65:
            continue
        size_delta = (abs(int(row_size) - size_bytes) / max(1, size_bytes)
                      if row_size is not None and size_bytes is not None else None)
        offset_b = ((db.get_pool_offset(device_id, work_key, fp) or 0)
                    if (db is not None and device_id and work_key) else 0)
        ranked.append({
            "fp": fp, "profile": profile, "duration_ms": int(source_ms), "n_slots": int(n_slots),
            "n_tiles": int(n_tiles), "score": score, "match": "+".join(match_parts),
            "relation": relation, "approximate": True, "a_num": a_num, "a_den": a_den,
            "b_ms": offset_b, "source_duration_ms": int(source_ms),
            "_size_delta": size_delta if size_delta is not None and size_delta <= 0.01 else None,
            "_delta": abs(int(source_ms) - duration_ms), "_created": float(created),
            "_verified": int(verified),
        })
    ranked.sort(key=lambda item: (-item["score"], item["_size_delta"] is None,
                                  item["_size_delta"] if item["_size_delta"] is not None else 0,
                                  item["_delta"], -item["_created"], -item["_verified"],
                                  item["fp"], item["profile"]))
    result = ranked[:max(1, min(int(limit), 8))]
    for candidate in result:
        for key in ("_size_delta", "_delta", "_created", "_verified"):
            candidate.pop(key, None)
    return result


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
    def __init__(self, status: int, code: str, *, duration_ms: int | None = None,
                 retry_after: float | None = None):
        super().__init__(code)
        self.status, self.code, self.retry_after = status, code, retry_after
        self.duration_ms = duration_ms


class _PackageCache:
    """Small process-local LRU for validated NVMT packages fetched from Telegram."""

    def __init__(self, max_bytes: int = 32 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.bytes = 0
        self.items: OrderedDict[tuple[str, str, str], bytes] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key: tuple[str, str, str]) -> bytes | None:
        with self.lock:
            data = self.items.get(key)
            if data is not None:
                self.items.move_to_end(key)
            return data

    def put(self, key: tuple[str, str, str], data: bytes) -> None:
        if not data or len(data) > self.max_bytes:
            return
        with self.lock:
            old = self.items.pop(key, None)
            if old is not None:
                self.bytes -= len(old)
            while self.items and self.bytes + len(data) > self.max_bytes:
                _old_key, old_data = self.items.popitem(last=False)
                self.bytes -= len(old_data)
            self.items[key] = data
            self.bytes += len(data)


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
        except urllib.error.HTTPError as exc:
            if method != "getFile":
                raise PoolError(503, "telegram_unavailable") from exc
            try:
                result = json.loads(exc.read(1024 * 1024))
            except (OSError, ValueError) as parse_exc:
                raise PoolError(503, "telegram_unavailable") from parse_exc
            finally:
                exc.close()
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise PoolError(503, "telegram_unavailable") from exc
        if (isinstance(result, dict) and result.get("ok") is not True and method == "getFile"
                and result.get("error_code") == 400):
            raise PoolError(404, "telegram_file_not_found")
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise PoolError(502, "telegram_error")
        return result["result"]

    def file_exists(self, file_id: str) -> bool:
        """Check Telegram metadata without downloading the document bytes."""
        try:
            self._json("getFile", {"file_id": file_id})
            return True
        except PoolError as exc:
            if exc.code == "telegram_file_not_found":
                return False
            raise

    def put(self, data: bytes, caption: str, metadata: dict | None = None) -> tuple[str, int]:
        filename = "tile.jpg"
        if metadata:
            filename = _tile_filename(int(metadata["pts_ms"]), int(metadata["duration_ms"]),
                                      int(metadata["n_tiles"]))
        return self.put_document(data, caption, filename, "image/jpeg")

    def put_document(self, data: bytes, caption: str, filename: str,
                     content_type: str = "application/octet-stream") -> tuple[str, int]:
        boundary = "openseek-" + secrets.token_hex(16)
        pieces: list[bytes] = []

        def field(name: str, value: str) -> None:
            pieces.extend([f"--{boundary}\r\n".encode(),
                           f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                           value.encode(), b"\r\n"])

        field("chat_id", self.channel_id)
        field("caption", caption)
        pieces.extend([f"--{boundary}\r\n".encode(),
                       f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode(),
                       f"Content-Type: {content_type}\r\n\r\n".encode(), data, b"\r\n",
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

    def get(self, file_id: str, max_bytes: int = MAX_TILE_BYTES) -> bytes:
        result = self._json("getFile", {"file_id": file_id})
        path = result.get("file_path") if isinstance(result, dict) else None
        if not isinstance(path, str) or not path or ".." in Path(path).parts:
            raise PoolError(502, "telegram_error")
        url = f"{self.api_base}/file/bot{self._token}/{urllib.parse.quote(path, safe='/')}"
        req = urllib.request.Request(url, method="GET")
        try:
            with self.opener(req, timeout=self.timeout_s) as response:
                data = response.read(max_bytes + 1)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise PoolError(503, "telegram_unavailable") from exc
        if len(data) > max_bytes:
            raise PoolError(502, "telegram_file_too_large")
        return data

    def get_document(self, file_id: str) -> bytes:
        return self.get(file_id, MAX_NVMT_BYTES)

    def delete_message(self, message_id: int) -> None:
        self._json("deleteMessage", {"chat_id": self.channel_id, "message_id": message_id})


def _nvmt_read32(data: bytes, at: int) -> int:
    return int.from_bytes(data[at:at + 4], "little")


def _nvmt_write32(data: bytearray, at: int, value: int) -> None:
    data[at:at + 4] = int(value).to_bytes(4, "little", signed=False)


def decode_nvmt(fp: str, profile: str, duration_ms: int, data: bytes) -> tuple[int, int, dict[int, tuple[int, bytes]]]:
    """Validate the TV's bounded NVMT v1 package and return (grid, slots, tiles)."""
    grid = NVMT_PROFILES.get(profile)
    if not is_fingerprint(fp) or grid is None:
        raise PoolError(400, "bad_key")
    if not isinstance(duration_ms, int) or not 0 < duration_ms <= MAX_DURATION_MS:
        raise PoolError(400, "bad_duration")
    if not isinstance(data, bytes) or len(data) < 20:
        raise PoolError(400, "bad_package")
    if len(data) > MAX_NVMT_BYTES:
        raise PoolError(413, "package_too_large")
    if data[:4] != b"NVMT" or _nvmt_read32(data, 4) != 1:
        raise PoolError(400, "bad_package")
    package_grid, n_slots, count = (_nvmt_read32(data, 8), _nvmt_read32(data, 12), _nvmt_read32(data, 16))
    expected_slots = (duration_ms + grid - 1) // grid
    if (package_grid != grid or n_slots != expected_slots or count == 0
            or count > n_slots or n_slots > 8640):
        raise PoolError(400, "bad_package_metadata")
    pos, tiles = 20, {}
    for _ in range(count):
        if pos + 12 > len(data):
            raise PoolError(400, "bad_package")
        slot, pts_raw, size = (_nvmt_read32(data, pos), _nvmt_read32(data, pos + 4), _nvmt_read32(data, pos + 8))
        pts_ms = pts_raw if pts_raw < 0x80000000 else pts_raw - 0x100000000
        pos += 12
        if (slot >= n_slots or slot in tiles or size < 125 or size > 200_000
                or pos + size > len(data) or not 0 <= pts_ms <= duration_ms
                or abs(pts_ms - slot * grid) > 10_000):
            raise PoolError(400, "bad_package_tile")
        jpeg = data[pos:pos + size]
        if not jpeg_ok(jpeg):
            raise PoolError(400, "bad_jpeg")
        try:
            with Image.open(io.BytesIO(jpeg)) as image:
                if image.format != "JPEG" or image.size != (TILE_W, TILE_H):
                    raise PoolError(400, "bad_jpeg_dimensions")
                image.verify()
        except (UnidentifiedImageError, OSError, SyntaxError) as exc:
            raise PoolError(400, "bad_jpeg") from exc
        tiles[slot] = (pts_ms, jpeg)
        pos += size
    if pos != len(data) or len(tiles) != count:
        raise PoolError(400, "bad_package")
    return grid, n_slots, tiles


def encode_nvmt(grid: int, n_slots: int, tiles: dict[int, tuple[int, bytes]]) -> bytes:
    data = bytearray(20)
    data[:4] = b"NVMT"
    _nvmt_write32(data, 4, 1)
    _nvmt_write32(data, 8, grid)
    _nvmt_write32(data, 12, n_slots)
    _nvmt_write32(data, 16, len(tiles))
    for slot, (pts_ms, jpeg) in sorted(tiles.items()):
        record = bytearray(12)
        _nvmt_write32(record, 0, slot)
        _nvmt_write32(record, 4, pts_ms)
        _nvmt_write32(record, 8, len(jpeg))
        data.extend(record)
        data.extend(jpeg)
    return bytes(data)


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
        self._package_cache = _PackageCache()

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
            if isinstance(self.storage, TelegramBotStore):
                file_id, message_id = self.storage.put(data, caption, {
                    "pts_ms": pts_ms, "duration_ms": duration_ms, "n_tiles": n_tiles})
            else:
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

    def package(self, fp: str, profile: str) -> tuple[bytes, dict] | None:
        if not is_fingerprint(fp) or profile not in NVMT_PROFILES:
            raise PoolError(400, "bad_key")
        rows = self.db.q("SELECT duration_ms,n_slots,n_tiles,size,sha256,file_id FROM pool_packages "
                         "WHERE fp=? AND profile=?", (fp, profile))
        if not rows:
            return None
        duration_ms, n_slots, n_tiles, size, digest, file_id = rows[0]
        cache_key = (fp, profile, digest)
        data = self._package_cache.get(cache_key)
        if data is None:
            try:
                data = self.storage.get_document(file_id)
            except AttributeError:
                data = self.storage.get(file_id, MAX_NVMT_BYTES)
            if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                raise PoolError(502, "stored_package_corrupt")
            _grid, got_slots, tiles = decode_nvmt(fp, profile, duration_ms, data)
            if got_slots != n_slots or len(tiles) != n_tiles:
                raise PoolError(502, "stored_package_corrupt")
            self._package_cache.put(cache_key, data)
        return data, {"duration_ms": duration_ms, "n_slots": n_slots, "n_tiles": n_tiles,
                      "sha256": digest, "profile": profile}

    def episode_candidates(self, episode_key: str, duration_ms: int, limit: int = 8) -> list[dict]:
        """Keep the legacy route while using hint ranking whenever its key is known."""
        if not _EPISODE_KEY.fullmatch(episode_key) or not isinstance(duration_ms, int) or duration_ms <= 0:
            raise PoolError(400, "bad_episode_key")
        hint_rows = self.db.q(
            "SELECT h.imdb,h.season,h.episode FROM pool_episode_packages e "
            "JOIN pool_hints h ON h.fp=e.fp AND h.profile=e.profile "
            "JOIN pool_packages p ON p.fp=e.fp AND p.profile=e.profile "
            "WHERE e.episode_key=? AND h.imdb IS NOT NULL "
            "ORDER BY ABS(e.duration_ms-?) ASC,e.updated DESC LIMIT 1",
            (episode_key, duration_ms))
        if hint_rows:
            imdb, season, episode = hint_rows[0]
            candidates = self.candidates(duration_ms=duration_ms, imdb=imdb,
                                         season=int(season), episode=int(episode), limit=limit)
            for candidate in candidates:
                candidate["duration_delta_ms"] = abs(candidate["duration_ms"] - duration_ms)
            return candidates

        # Older packages and clients may have an episode key without a v2 manifest.
        # Keep their existing lookup behavior until hint metadata is available.
        # Runtime is only a coarse filter; clients apply the returned timing mapping.
        tolerance = max(90_000, int(duration_ms * 0.15))
        rows = self.db.q(
            "SELECT p.fp,p.profile,p.duration_ms,n.n_slots,n.n_tiles "
            "FROM pool_episode_packages p JOIN pool_packages n ON n.fp=p.fp AND n.profile=p.profile "
            "WHERE p.episode_key=? AND ABS(p.duration_ms-?)<=? "
            "ORDER BY ABS(p.duration_ms-?) ASC,p.updated DESC LIMIT ?",
            (episode_key, duration_ms, tolerance, duration_ms, max(1, min(limit, 8))))
        candidates = []
        for fp, profile, dur, slots, tiles in rows:
            relation_data = _timing_relation(duration_ms, int(dur))
            if relation_data is None:
                continue
            relation, a_num, a_den = relation_data
            candidates.append({"fp": fp, "profile": profile, "duration_ms": dur,
                               "duration_delta_ms": abs(dur - duration_ms), "n_slots": slots,
                               "n_tiles": tiles, "score": {"same_length": 100, "speed_24_25": 95,
                                                              "length_differs": 90}[relation],
                               "match": "episode_key+duration", "relation": relation,
                               "approximate": True, "a_num": a_num, "a_den": a_den,
                               "b_ms": 0, "source_duration_ms": dur})
        return candidates

    def candidates(self, *, duration_ms: int, imdb: str | None = None, season: int = 0,
                   episode: int = 0, size_bytes: int | None = None, filename: str | None = None,
                   exclude_fp: str | None = None, limit: int = 8,
                   device_id: str | None = None, work_key: str | None = None) -> list[dict]:
        """Rank bounded NVMT reuse candidates using only the declared catalog hints."""
        query = validate_pool_hints(imdb, season, episode, duration_ms, size_bytes, filename)
        filename_norm = query["filename_norm"]
        if not imdb and not filename_norm:
            raise PoolError(400, "missing_hints")
        if exclude_fp is not None and not is_fingerprint(exclude_fp):
            raise PoolError(400, "bad_key")
        hint_cols = {r[1] for r in self.db.conn.execute("PRAGMA table_info(pool_hints)").fetchall()}
        has_verified = "verified" in hint_cols
        ver_col = ",h.verified" if has_verified else ""
        rows = self.db.q(
            f"SELECT h.fp,h.profile,h.imdb,h.season,h.episode,h.duration_ms,h.size_bytes,h.filename_norm,"
            f"h.n_tiles,h.grid,h.created,p.n_slots{ver_col} "
            f"FROM pool_hints h JOIN pool_packages p ON p.fp=h.fp AND p.profile=h.profile "
            f"WHERE ABS(h.duration_ms-?)<=? AND (? IS NULL OR h.fp<>?)",
            (duration_ms, max(90_000, int(duration_ms * 0.15)), exclude_fp, exclude_fp))
        normalized = []
        for row in rows:
            if has_verified:
                candidate, verified = row[:-1], row[-1]
            else:
                candidate, verified = row, 0
            fp, profile, row_imdb, row_season, row_episode, source_ms, row_size, row_filename, n_tiles, grid, created, n_slots = candidate
            normalized.append((fp, profile, row_imdb, row_season, row_episode, source_ms, row_size,
                               row_filename, n_tiles, grid, created, n_slots, verified))
        return _rank_hint_rows(normalized, duration_ms=duration_ms, imdb=imdb, season=season,
                               episode=episode, size_bytes=size_bytes, filename_norm=filename_norm,
                               device_id=device_id, work_key=work_key, db=self.db, limit=limit)

    def _package_slots_from_tiles(self, fp: str, profile: str, slots: list[int],
                                  tile_rows: list[tuple]) -> tuple[bytes, dict] | None:
        """Build a sparse NVMT response from individual ten-second tile documents."""
        grid = NVMT_PROFILES[profile]
        tile_step = TILE_INTERVAL_MS // grid
        duration_ms = int(tile_rows[0][2])
        n_slots = (duration_ms + grid - 1) // grid
        n_tiles = int(tile_rows[0][3])
        if TILE_INTERVAL_MS % grid or n_tiles != tile_count(duration_ms):
            raise PoolError(502, "stored_tile_corrupt")
        by_slot = {int(row[0]) * tile_step: row for row in tile_rows}
        if any(int(row[2]) != duration_ms or int(row[3]) != n_tiles for row in tile_rows):
            raise PoolError(502, "stored_tile_corrupt")
        selected_rows: dict[int, tuple] = {}
        for wanted in dict.fromkeys(slots):
            if wanted in by_slot:
                selected_rows[wanted] = by_slot[wanted]
                continue
            nearest = min(by_slot, key=lambda k: abs(k - wanted))
            if abs(nearest - wanted) <= 1:
                selected_rows.setdefault(nearest, by_slot[nearest])
        if not selected_rows:
            return None
        selected: dict[int, tuple[int, bytes]] = {}
        for k, row in selected_rows.items():
            tile_k, pts_ms, _duration, _n_tiles, size, digest, file_id = row
            data = self.storage.get(file_id, MAX_TILE_BYTES)
            if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                raise PoolError(502, "stored_tile_corrupt")
            try:
                validate_tile(fp, PROFILE, tile_k, pts_ms, duration_ms, n_tiles, data)
            except PoolError as exc:
                raise PoolError(502, "stored_tile_corrupt") from exc
            selected[k] = (pts_ms, data)
        partial = encode_nvmt(grid, n_slots, selected)
        return partial, {"duration_ms": duration_ms, "n_slots": n_slots, "n_tiles": len(selected),
                         "sha256": hashlib.sha256(partial).hexdigest(), "profile": profile}

    def package_slots(self, fp: str, profile: str, slots: list[int]) -> tuple[bytes, dict] | None:
        """Return a small NVMT subset for the TV to verify visually before downloading the full pack."""
        if not slots or len(slots) > 12 or any(not isinstance(k, int) or k < 0 for k in slots):
            raise PoolError(400, "bad_slots")
        if not is_fingerprint(fp) or profile not in NVMT_PROFILES:
            raise PoolError(400, "bad_key")
        # The single-tile API stores 320x180 q75 frames every ten seconds.
        # g10 uses each row directly; g5 maps rows to even-numbered NVMT slots.
        tile_rows = self.db.q(
            "SELECT k,pts_ms,duration_ms,n_tiles,size,sha256,file_id FROM pool_tiles "
            "WHERE fp=? AND profile=? ORDER BY k", (fp, PROFILE))
        if tile_rows:
            packages = self.db.q("SELECT duration_ms,n_slots FROM pool_packages WHERE fp=? AND profile=?",
                                 (fp, profile))
            grid = NVMT_PROFILES[profile]
            expected_slots = (int(tile_rows[0][2]) + grid - 1) // grid
            match = not packages or (packages[0][0] == tile_rows[0][2] and packages[0][1] == expected_slots)
            if match:
                try:
                    return self._package_slots_from_tiles(fp, profile, slots, tile_rows)
                except PoolError as exc:
                    if exc.code not in ("stored_tile_corrupt", "telegram_file_not_found"):
                        raise
                    log.warning("pool tile rows unusable; serving sparse from the package")
        got = self.package(fp, profile)
        if got is None:
            return None
        data, meta = got
        grid, n_slots, tiles = decode_nvmt(fp, profile, meta["duration_ms"], data)
        selected: dict[int, tuple[int, bytes]] = {}
        for wanted in dict.fromkeys(slots):
            if wanted in tiles:
                selected[wanted] = tiles[wanted]
                continue
            nearest = min(tiles, key=lambda k: abs(k - wanted))
            if abs(nearest - wanted) <= 1:
                selected.setdefault(nearest, tiles[nearest])
        if not selected:
            return None
        partial = encode_nvmt(grid, n_slots, selected)
        return partial, {**meta, "n_tiles": len(selected), "sha256": hashlib.sha256(partial).hexdigest()}

    def _index_episode_package(self, episode_key: str | None, fp: str, profile: str, duration_ms: int) -> None:
        if episode_key:
            self.db.x("INSERT INTO pool_episode_packages(episode_key,fp,profile,duration_ms,updated) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(episode_key,fp,profile) DO UPDATE SET duration_ms=excluded.duration_ms,updated=excluded.updated",
                       (episode_key, fp, profile, duration_ms, time.time()))

    def index_episode_package(self, fp: str, profile: str, episode_key: str) -> bool:
        if not is_fingerprint(fp) or profile not in NVMT_PROFILES or not _EPISODE_KEY.fullmatch(episode_key):
            raise PoolError(400, "bad_episode_key")
        rows = self.db.q("SELECT duration_ms FROM pool_packages WHERE fp=? AND profile=?", (fp, profile))
        if not rows:
            return False
        self._index_episode_package(episode_key, fp, profile, int(rows[0][0]))
        return True

    def upload_package(self, fp: str, profile: str, duration_ms: int, data: bytes,
                       episode_key: str | None = None, *, imdb: str | None = None,
                       season: int = 0, episode: int = 0, size_bytes: int | None = None,
                       filename: str | None = None) -> dict:
        hints = validate_pool_hints(imdb, season, episode, duration_ms, size_bytes, filename)
        grid, n_slots, incoming = decode_nvmt(fp, profile, duration_ms, data)
        if episode_key is not None and not _EPISODE_KEY.fullmatch(episode_key):
            raise PoolError(400, "bad_episode_key")
        with self._lock:
            rows = self.db.q("SELECT duration_ms,n_slots,n_tiles,size,sha256,file_id,message_id "
                             "FROM pool_packages WHERE fp=? AND profile=?", (fp, profile))
            previous = rows[0] if rows else None
            merged = dict(incoming)
            if previous:
                old_duration, old_slots, old_count, old_size, old_digest, old_file, old_message = previous
                if old_duration != duration_ms or old_slots != n_slots:
                    raise PoolError(409, "media_metadata_conflict", duration_ms=int(old_duration))
                try:
                    old_data = self.storage.get_document(old_file)
                except AttributeError:
                    old_data = self.storage.get(old_file, MAX_NVMT_BYTES)
                if len(old_data) != old_size or hashlib.sha256(old_data).hexdigest() != old_digest:
                    raise PoolError(502, "stored_package_corrupt")
                _old_grid, _old_slots, old_tiles = decode_nvmt(fp, profile, duration_ms, old_data)
                # The first accepted frame for a slot is immutable; concurrent
                # viewers can add coverage without replacing another tile.
                merged = dict(old_tiles)
                for slot, tile in incoming.items():
                    merged.setdefault(slot, tile)
            package = encode_nvmt(grid, n_slots, merged)
            if len(package) > MAX_NVMT_BYTES:
                raise PoolError(413, "package_too_large")
            digest = hashlib.sha256(package).hexdigest()
            if previous and digest == previous[4]:
                self._store_package_metadata(fp, profile, duration_ms, n_slots, len(merged), len(package),
                                             digest, previous[5], previous[6], episode_key, hints, grid)
                return {"stored": False, "duplicate": True, "tiles": len(merged), "sha256": digest}
            caption = f"openseek:nvmt:v1:{fp}:{profile}:{digest}"
            try:
                file_id, message_id = self.storage.put_document(
                    package, caption, f"{fp}.{profile}.nvmt", "application/octet-stream")
            except AttributeError as exc:
                raise PoolError(503, "package_storage_unavailable") from exc
            self._store_package_metadata(fp, profile, duration_ms, n_slots, len(merged), len(package),
                                         digest, file_id, message_id, episode_key, hints, grid)
            self._package_cache.put((fp, profile, digest), package)
            if previous:
                try:
                    self.storage.delete_message(previous[6])
                except (AttributeError, PoolError):
                    pass
        return {"stored": True, "duplicate": False, "tiles": len(merged), "sha256": digest}

    def _store_package_metadata(self, fp: str, profile: str, duration_ms: int, n_slots: int, n_tiles: int,
                                size: int, digest: str, file_id: str, message_id: int,
                                episode_key: str | None, hints: dict, grid: int) -> None:
        """Commit the exact-package row and its optional lookup rows atomically."""
        now = time.time()
        with self.db._lock:
            self.db.conn.execute("BEGIN IMMEDIATE")
            try:
                self.db.conn.execute(
                    "INSERT INTO pool_packages(fp,profile,duration_ms,n_slots,n_tiles,size,sha256,file_id,message_id,created) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fp,profile) DO UPDATE SET "
                    "duration_ms=excluded.duration_ms,n_slots=excluded.n_slots,n_tiles=excluded.n_tiles,"
                    "size=excluded.size,sha256=excluded.sha256,file_id=excluded.file_id,"
                    "message_id=excluded.message_id,created=excluded.created",
                    (fp, profile, duration_ms, n_slots, n_tiles, size, digest, file_id, message_id, now))
                if episode_key:
                    self.db.conn.execute(
                        "INSERT INTO pool_episode_packages(episode_key,fp,profile,duration_ms,updated) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(episode_key,fp,profile) DO UPDATE SET duration_ms=excluded.duration_ms,updated=excluded.updated",
                        (episode_key, fp, profile, duration_ms, now))
                self.db.conn.execute(
                    "INSERT INTO pool_hints(fp,profile,imdb,season,episode,duration_ms,size_bytes,filename_norm,n_tiles,grid,created) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fp,profile) DO UPDATE SET "
                    "imdb=excluded.imdb,season=excluded.season,episode=excluded.episode,duration_ms=excluded.duration_ms,"
                    "size_bytes=excluded.size_bytes,filename_norm=excluded.filename_norm,n_tiles=excluded.n_tiles,"
                    "grid=excluded.grid,created=excluded.created",
                    (fp, profile, hints["imdb"], hints["season"], hints["episode"], duration_ms,
                     hints["size_bytes"], hints["filename_norm"], n_tiles, grid, now))
                self.db.conn.execute("COMMIT")
            except BaseException:
                self.db.conn.execute("ROLLBACK")
                raise

    def purge(self, *, dry_run: bool = True, outbox_older_than_days: int = 30,
              outbox_attempts: int = 5, vacuum: bool = False) -> dict:
        """Remove rows for Telegram documents confirmed missing and stale failed outbox work."""
        if not isinstance(outbox_older_than_days, int) or not 1 <= outbox_older_than_days <= 3650:
            raise PoolError(400, "bad_purge_options")
        if not isinstance(outbox_attempts, int) or not 0 <= outbox_attempts <= 100:
            raise PoolError(400, "bad_purge_options")
        with self._lock:
            tiles = self.db.q("SELECT fp,profile,k,file_id,message_id FROM pool_tiles")
            packages = self.db.q("SELECT fp,profile,file_id,message_id FROM pool_packages")
            invalid_tiles, invalid_packages = [], []
            for row in tiles:
                if not self.storage.file_exists(row[3]):
                    invalid_tiles.append(row)
            for row in packages:
                if not self.storage.file_exists(row[2]):
                    invalid_packages.append(row)
            cutoff = time.time() - outbox_older_than_days * 86400
            before = self.db.path.stat().st_size if self.db.path.exists() else 0
            counts = self.db.purge_pool(
                [(row[0], row[1], row[2]) for row in invalid_tiles],
                [(row[0], row[1]) for row in invalid_packages], cutoff, outbox_attempts,
                dry_run=dry_run, vacuum=vacuum)
            messages_attempted = 0
            if not dry_run:
                for row in [*invalid_tiles, *invalid_packages]:
                    try:
                        self.storage.delete_message(int(row[-1]))
                        messages_attempted += 1
                    except (AttributeError, PoolError, ValueError):
                        log.warning("could not delete Telegram pool message during purge")
            after = self.db.path.stat().st_size if self.db.path.exists() else 0
            return {"dry_run": dry_run, **counts, "telegram_messages_delete_attempted": messages_attempted,
                    "database_bytes_before": before, "database_bytes_after": after,
                    "vacuumed": bool(vacuum and not dry_run)}


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
        self.last_error = ""

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
                self.last_error = type(exc).__name__
                ok = False
            if ok:
                self.last_error = ""
                self.db.x("DELETE FROM pool_outbox WHERE fp=? AND k=?", (fp, k))
            else:
                if not self.last_error:
                    self.last_error = "pool_upload_failed"
                attempts += 1
                delay = min(300, 2 ** min(attempts, 8))
                self.db.x("UPDATE pool_outbox SET attempts=?,retry_after=? WHERE fp=? AND k=?",
                          (attempts, time.time() + delay, fp, k))
                log.info("pool upload deferred tile=%d retry_s=%d", k, delay)


class ChannelPoolClient:
    """Local reader for a channel index; Telegram failures remain cache misses."""

    def __init__(self, cache, index, store, *, max_cache_bytes: int = 16 * 1024 * 1024):
        self.cache, self.index, self.store = cache, index, store
        self.max_cache_bytes = max_cache_bytes
        self._items: OrderedDict[tuple[int, str], bytes] = OrderedDict()
        self._cache_bytes = 0
        self._misses: dict[tuple[str, int], float] = {}
        self._lock = threading.RLock()

    def get_tile(self, fp: str, k: int) -> tuple[bytes, int] | None:
        mark = (fp, k)
        now = time.monotonic()
        with self._lock:
            if self._misses.get(mark, 0) > now:
                return None
        try:
            row = self.index.lookup(fp, PROFILE, k)
        except PoolError:
            return None
        if row is None:
            with self._lock:
                self._misses[mark] = now + 30
            return None
        message_id, digest = row["message_id"], row["sha256"]
        key = (message_id, digest)
        with self._lock:
            data = self._items.get(key)
            if data is not None:
                self._items.move_to_end(key)
        if data is None:
            try:
                data = self.store.get_tile(message_id)
            except PoolError as exc:
                if exc.code in ("telegram_message_not_found", "stored_tile_corrupt", "tile_too_large"):
                    self.index.forget(fp, PROFILE, k, message_id)
                with self._lock:
                    self._misses[mark] = now + (30 if exc.status == 404 else 10)
                return None
            except Exception as exc:
                log.debug("channel tile read failed (%s)", type(exc).__name__)
                with self._lock:
                    self._misses[mark] = now + 10
                return None
            if (len(data) != row["size"] or len(data) > MAX_TILE_BYTES or not jpeg_ok(data) or
                    hashlib.sha256(data).hexdigest() != digest):
                self.index.forget(fp, PROFILE, k, message_id)
                with self._lock:
                    self._misses[mark] = now + 10
                return None
            # Duration metadata is not present on legacy host posts. Use the
            # protocol's maximum valid duration to still validate JPEG type and
            # dimensions; the scheduler uses the canonical tile timestamp.
            duration_ms = row["duration_ms"] or MAX_DURATION_MS
            n_tiles = row["n_tiles"] or tile_count(duration_ms)
            try:
                validate_tile(fp, PROFILE, k, row["pts_ms"], duration_ms, n_tiles, data)
            except PoolError:
                self.index.forget(fp, PROFILE, k, message_id)
                with self._lock:
                    self._misses[mark] = now + 10
                return None
            self._cache_put(key, data)
        try:
            self.cache.put_tile(fp, k, row["pts_ms"], data)
        except (OSError, ValueError):
            # The scheduler can still use and store the bytes itself.
            pass
        with self._lock:
            self._misses.pop(mark, None)
        return data, row["pts_ms"]

    def _cache_put(self, key: tuple[int, str], data: bytes) -> None:
        if not data or len(data) > self.max_cache_bytes:
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._cache_bytes -= len(old)
            while self._items and self._cache_bytes + len(data) > self.max_cache_bytes:
                _old_key, old_data = self._items.popitem(last=False)
                self._cache_bytes -= len(old_data)
            self._items[key] = data
            self._cache_bytes += len(data)

    def manifest(self, fp: str, profile: str) -> dict | None:
        return self.index.manifest(fp, profile)

    def package_slots(self, fp: str, profile: str, slots: list[int]):
        return self.index.package_slots(fp, profile, slots)

    def episode_candidates(self, episode_key: str, duration_ms: int) -> list[dict]:
        return self.index.episode_candidates(episode_key, duration_ms)


class ChannelPoolSync:
    """Durable asynchronous channel publisher, isolated from host-mode HTTP outbox."""

    def __init__(self, db: DB, cache, store, index):
        self.db, self.cache, self.store, self.index = db, cache, store, index
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="openseek-channel-outbox", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def enqueue(self, fp: str, k: int) -> None:
        entry = self.cache.entry(fp)
        if entry is None or not entry.duration_ms or not entry.n_tiles:
            return
        pts_ms = entry.tiles.get(k, k * TILE_INTERVAL_MS)
        self.db.x("INSERT OR IGNORE INTO pool_channel_outbox(fp,k,pts_ms,duration_ms,n_tiles,created) "
                  "VALUES(?,?,?,?,?,?)", (fp, k, pts_ms, entry.duration_ms, entry.n_tiles, time.time()))
        self._wake.set()

    def pending(self) -> int:
        rows = self.db.q("SELECT COUNT(*) FROM pool_channel_outbox")
        return int(rows[0][0]) if rows else 0

    def _run(self) -> None:
        while not self._stop.is_set():
            rows = self.db.q("SELECT fp,k,pts_ms,duration_ms,n_tiles,attempts,retry_after "
                             "FROM pool_channel_outbox ORDER BY created LIMIT 1")
            if not rows:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            fp, k, pts_ms, duration_ms, n_tiles, attempts, retry_after = rows[0]
            wait_s = float(retry_after) - time.time()
            if wait_s > 0:
                self._wake.wait(min(wait_s, 2.0))
                self._wake.clear()
                continue
            try:
                self._publish(fp, int(k), int(pts_ms), int(duration_ms), int(n_tiles))
                self.last_error = ""
            except Exception as exc:
                self.last_error = getattr(exc, "code", type(exc).__name__)
                attempts = int(attempts) + 1
                delay = min(300, 2 ** min(attempts, 8))
                self.db.x("UPDATE pool_channel_outbox SET attempts=?,retry_after=? WHERE fp=? AND k=?",
                          (attempts, time.time() + delay, fp, k))
                log.info("channel upload deferred tile=%d retry_s=%d (%s)",
                         k, delay, type(exc).__name__)

    def status(self) -> dict:
        return {"pending": self.pending(), "last_error": self.last_error}

    def _publish(self, fp: str, k: int, pts_ms: int, duration_ms: int, n_tiles: int) -> None:
        rows = self.db.q("SELECT message_id FROM pool_channel_outbox WHERE fp=? AND k=?", (fp, k))
        message_id = int(rows[0][0]) if rows else 0
        librarian = str(getattr(getattr(self.store, "settings", None), "librarian", "") or "")
        librarian_bot = librarian.startswith("@")
        if message_id:
            self.index.refresh(force=True)
            winner = self.index.lookup(fp, PROFILE, k)
            if winner is None:
                raise PoolError(503, "telegram_index_not_updated")
            if winner["message_id"] != message_id and librarian_bot:
                try:
                    self.store.delete_message(message_id, target=librarian)
                except TypeError:
                    pass
            elif winner["message_id"] != message_id and not librarian:
                self.store.delete_message(message_id)
            self.db.x("DELETE FROM pool_channel_outbox WHERE fp=? AND k=?", (fp, k))
            return
        data = self.cache.tile_bytes(fp, k)
        if data is None:
            self.db.x("DELETE FROM pool_channel_outbox WHERE fp=? AND k=?", (fp, k))
            return
        digest = validate_tile(fp, PROFILE, k, pts_ms, duration_ms, n_tiles, data)
        current = self.index.lookup(fp, PROFILE, k)
        if current is not None:
            if current["sha256"] != digest:
                log.info("channel tile conflict at tile=%d; existing tile kept", k)
            self.db.x("DELETE FROM pool_channel_outbox WHERE fp=? AND k=?", (fp, k))
            return
        caption = f"openseek:v1:{fp}:{PROFILE}:{k}:{digest}"
        metadata = {"pts_ms": pts_ms, "duration_ms": duration_ms, "n_tiles": n_tiles}
        if librarian.startswith("https://"):
            message_id = self._upload_to_http_librarian(librarian, data, caption, metadata)
        elif librarian_bot:
            try:
                message_id = self.store.put_tile(data, caption, metadata, target=librarian)
            except TypeError:
                message_id = self.store.put_tile(data, caption, metadata)
        else:
            try:
                message_id = self.store.put_tile(data, caption, metadata)
            except TypeError:
                message_id = self.store.put_tile(data, caption)
        self.db.x("UPDATE pool_channel_outbox SET message_id=? WHERE fp=? AND k=?", (message_id, fp, k))
        # Scan once so concurrent publishers are resolved by Telegram's
        # ascending message IDs. If this post loses, best-effort delete ours.
        self.index.refresh(force=True)
        winner = self.index.lookup(fp, PROFILE, k)
        if winner is None:
            raise PoolError(503, "telegram_index_not_updated")
        if winner["sha256"] != digest and winner["message_id"] != message_id and not librarian:
            self.store.delete_message(message_id)
        self.db.x("DELETE FROM pool_channel_outbox WHERE fp=? AND k=?", (fp, k))

    def _upload_to_http_librarian(self, base: str, data: bytes, caption: str, metadata: dict) -> int:
        match = re.fullmatch(r"openseek:v1:([0-9a-f]{64}):([^:]+):(\d+):([0-9a-f]{64})", caption)
        if not match:
            raise PoolError(400, "bad_tile")
        query = urllib.parse.urlencode({"pts_ms": metadata["pts_ms"],
                                        "duration_ms": metadata["duration_ms"],
                                        "n_tiles": metadata["n_tiles"]})
        url = f"{base.rstrip('/')}/v1/tiles/{match.group(1)}/{match.group(3)}.jpg?{query}"
        headers = {"Content-Type": "image/jpeg", "X-OpenSeek-Caption": caption}
        key = str(getattr(getattr(self.store, "settings", None), "librarian_key", "") or "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(url, data=data, headers=headers, method="PUT")
        try:
            with _urlopen(request, timeout=5.0) as response:
                result = json.loads(response.read(1024 * 1024))
        except urllib.error.HTTPError as exc:
            if exc.code != 409:
                raise PoolError(503, "librarian_unavailable") from exc
            try:
                result = json.loads(exc.read(1024 * 1024))
            except (OSError, ValueError) as parse_exc:
                raise PoolError(503, "librarian_unavailable") from parse_exc
            finally:
                exc.close()
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise PoolError(503, "librarian_unavailable") from exc
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(message_id, int) or message_id <= 0:
            raise PoolError(502, "bad_librarian_response")
        return message_id

