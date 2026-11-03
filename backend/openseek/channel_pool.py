"""HTTP pool adapter backed by a node's indexed Telegram channel."""
from __future__ import annotations

import hashlib
import base64
import json
import secrets
import threading
import time

from .cache import PROFILE
from .channel_index import ChannelIndex
from .ids import is_fingerprint
from .pool import (MAX_DURATION_MS, MAX_NVMT_BYTES, NVMT_PROFILES, PoolError,
                   TILE_INTERVAL_MS, _EPISODE_KEY, _tile_filename, decode_nvmt, encode_nvmt,
                   validate_pool_hints, validate_tile)
from .scheduler import tile_count


class ChannelPoolService:
    """Expose the same read contract as PoolService over channel-indexed tile documents."""

    def __init__(self, index: ChannelIndex, read_key: str = "", write_key: str = ""):
        self.index = index
        self.store = index.store
        self.read_key = read_key
        self.write_key = write_key
        self._lock = threading.RLock()

    def authorize(self, given: str | None, write: bool = False) -> bool:
        wanted = self.write_key if write else self.read_key
        return bool(given and wanted and secrets.compare_digest(given, wanted))

    def manifest(self, fp: str, profile: str) -> dict | None:
        return self.index.manifest(fp, profile)

    def tile(self, fp: str, profile: str, k: int) -> tuple[bytes, dict] | None:
        if not is_fingerprint(fp) or profile != PROFILE:
            raise PoolError(400, "bad_key")
        row = self.index.lookup(fp, profile, k)
        if row is None:
            return None
        try:
            data = self.store.get_tile(row["message_id"])
        except PoolError as exc:
            if exc.status == 404:
                self.index.forget(fp, profile, k, row["message_id"])
            raise
        if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            self.index.forget(fp, profile, k, row["message_id"])
            raise PoolError(502, "stored_tile_corrupt")
        duration = row["duration_ms"] or MAX_DURATION_MS
        n_tiles = row["n_tiles"] or tile_count(duration)
        try:
            validate_tile(fp, profile, k, row["pts_ms"], duration, n_tiles, data)
        except PoolError as exc:
            self.index.forget(fp, profile, k, row["message_id"])
            raise PoolError(502, "stored_tile_corrupt") from exc
        return data, {"pts_ms": row["pts_ms"], "duration_ms": row["duration_ms"],
                      "n_tiles": row["n_tiles"], "sha256": row["sha256"], "profile": profile}

    def package(self, fp: str, profile: str) -> tuple[bytes, dict] | None:
        if not is_fingerprint(fp) or profile not in NVMT_PROFILES:
            raise PoolError(400, "bad_key")
        manifest = self.index.manifest(fp, PROFILE)
        if manifest is None or manifest["duration_ms"] <= 0 or manifest["n_tiles"] <= 0:
            return None
        duration = int(manifest["duration_ms"])
        grid = NVMT_PROFILES[profile]
        n_slots = (duration + grid - 1) // grid
        tile_step = TILE_INTERVAL_MS // grid
        tiles: dict[int, tuple[int, bytes]] = {}
        for item in manifest["tiles"]:
            k = int(item["k"])
            slot = k * tile_step
            got = self.tile(fp, PROFILE, k)
            if got is None:
                continue
            data, meta = got
            if meta["duration_ms"] == 0:
                continue
            if meta["duration_ms"] != duration or slot >= n_slots:
                raise PoolError(502, "stored_tile_corrupt")
            tiles[slot] = (int(meta["pts_ms"]), data)
        if not tiles:
            return None
        package = encode_nvmt(grid, n_slots, tiles)
        if len(package) > MAX_NVMT_BYTES:
            raise PoolError(502, "stored_package_too_large")
        return package, {"duration_ms": duration, "n_slots": n_slots, "n_tiles": len(tiles),
                         "sha256": hashlib.sha256(package).hexdigest(), "profile": profile}

    def package_slots(self, fp: str, profile: str, slots: list[int]) -> tuple[bytes, dict] | None:
        return self.index.package_slots(fp, profile, slots)

    def episode_candidates(self, episode_key: str, duration_ms: int, limit: int = 8) -> list[dict]:
        return self.index.episode_candidates(episode_key, duration_ms, limit)

    def candidates(self, **hints) -> list[dict]:
        return self.index.candidates(**hints)

    def index_episode_package(self, fp: str, profile: str, episode_key: str) -> bool:
        if (not is_fingerprint(fp) or profile not in NVMT_PROFILES or
                not _EPISODE_KEY.fullmatch(episode_key or "")):
            raise PoolError(400, "bad_episode_key")
        manifest = self.index.manifest(fp, PROFILE)
        if manifest is None or manifest["duration_ms"] <= 0:
            return False
        duration_ms = int(manifest["duration_ms"])
        self.index.db.x(
            "INSERT INTO pool_episode_packages(episode_key,fp,profile,duration_ms,updated) VALUES(?,?,?,?,?) "
            "ON CONFLICT(episode_key,fp,profile) DO UPDATE SET duration_ms=excluded.duration_ms,updated=excluded.updated",
            (episode_key, fp, profile, duration_ms, time.time()))
        return True

    def upload(self, *args, **kwargs) -> dict:
        fp, profile, k, pts_ms, duration_ms, n_tiles, data = args[:7]
        if getattr(self.store, "can_post", None) is not True:
            raise PoolError(403, "read_only_channel")
        digest = validate_tile(fp, profile, k, pts_ms, duration_ms, n_tiles, data)
        existing = self.index.lookup(fp, profile, k)
        if existing is not None:
            if existing["sha256"] != digest:
                raise PoolError(409, "tile_conflict")
            return {"stored": False, "duplicate": True, "sha256": digest}
        caption = f"openseek:v1:{fp}:{profile}:{k}:{digest}"
        metadata = {"pts_ms": pts_ms, "duration_ms": duration_ms, "n_tiles": n_tiles}
        message_id = self.store.put_tile(data, caption, metadata)
        from .telegram_mt import ChannelMessage
        message = ChannelMessage(int(message_id), caption, len(data),
                                 _tile_filename(pts_ms, duration_ms, n_tiles))
        self.index.record_message(message)
        return {"stored": True, "duplicate": False, "sha256": digest}

    def upload_package(self, fp: str, profile: str, duration_ms: int, data: bytes,
                       episode_key: str | None = None, *, imdb: str | None = None,
                       season: int = 0, episode: int = 0, size_bytes: int | None = None,
                       filename: str | None = None) -> dict:
        if getattr(self.store, "can_post", None) is not True:
            raise PoolError(403, "read_only_channel")
        hints = validate_pool_hints(imdb, season, episode, duration_ms, size_bytes, filename)
        grid, n_slots, source_tiles = decode_nvmt(fp, profile, duration_ms, data)
        if episode_key is not None and not _EPISODE_KEY.fullmatch(episode_key):
            raise PoolError(400, "bad_episode_key")
        if len(data) > MAX_NVMT_BYTES:
            raise PoolError(413, "package_too_large")
        if not source_tiles:
            raise PoolError(400, "empty_package")
        self.index.refresh(force=True)
        channel_count = (duration_ms + TILE_INTERVAL_MS - 1) // TILE_INTERVAL_MS
        selected: dict[int, tuple[int, bytes]] = {}
        used_slots: set[int] = set()
        for k in range(channel_count):
            target_ms = k * TILE_INTERVAL_MS
            slot = min(source_tiles, key=lambda item: (abs(source_tiles[item][0] - target_ms), item))
            if slot in used_slots or abs(source_tiles[slot][0] - target_ms) > grid // 2:
                continue
            if slot >= n_slots:
                continue
            used_slots.add(slot)
            selected[k] = source_tiles[slot]
        if not selected:
            raise PoolError(400, "no_channel_tiles")
        pending_messages = []
        stored = 0
        for k, (pts_ms, tile_data) in selected.items():
            digest = validate_tile(fp, PROFILE, k, pts_ms, duration_ms, channel_count, tile_data)
            existing = self.index.lookup(fp, PROFILE, k)
            if existing is not None:
                # Immutable first-writer-wins for every channel tile key.
                continue
            caption = f"openseek:v1:{fp}:{PROFILE}:{k}:{digest}"
            metadata = {"pts_ms": pts_ms, "duration_ms": duration_ms, "n_tiles": channel_count}
            message_id = self.store.put_tile(tile_data, caption, metadata)
            from .telegram_mt import ChannelMessage
            pending_messages.append(ChannelMessage(int(message_id), caption, len(tile_data),
                                                    _tile_filename(pts_ms, duration_ms, channel_count)))
            stored += 1

        # If publication was interrupted earlier, refresh above made those tiles
        # visible and this retry only publishes missing keys.
        for message in pending_messages:
            self.index.record_message(message)
        created = int(time.time())
        manifest = {"v": 1, "fp": fp, "profile": profile, "imdb": hints["imdb"],
                    "season": hints["season"], "episode": hints["episode"],
                    "duration_ms": duration_ms, "size_bytes": hints["size_bytes"],
                    "filename_norm": hints["filename_norm"], "n_tiles": len(source_tiles),
                    "grid": grid, "created": created}
        old_hints = self.index.db.q(
            "SELECT imdb,season,episode,duration_ms,size_bytes,filename_norm,n_tiles,grid "
            "FROM pool_hints WHERE fp=? AND profile=?", (fp, profile))
        same_hints = bool(old_hints and old_hints[0] == (
            hints["imdb"], hints["season"], hints["episode"], duration_ms,
            hints["size_bytes"], hints["filename_norm"], len(source_tiles), grid))
        if stored or not same_hints:
            caption = self._manifest_caption(manifest)
            post = getattr(self.store, "post_message", None)
            if post is None:
                raise PoolError(503, "telegram_manifest_unavailable")
            post(caption)
        now = float(created)
        with self.index.db._lock:
            self.index.db.conn.execute("BEGIN IMMEDIATE")
            try:
                self.index.db.conn.execute(
                    "INSERT INTO pool_hints(fp,profile,imdb,season,episode,duration_ms,size_bytes,filename_norm,n_tiles,grid,created) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fp,profile) DO UPDATE SET "
                    "imdb=excluded.imdb,season=excluded.season,episode=excluded.episode,"
                    "duration_ms=excluded.duration_ms,"
                    "size_bytes=excluded.size_bytes,filename_norm=excluded.filename_norm,n_tiles=excluded.n_tiles,"
                    "grid=excluded.grid,created=excluded.created",
                    (fp, profile, hints["imdb"], hints["season"], hints["episode"], duration_ms,
                     hints["size_bytes"], hints["filename_norm"], len(source_tiles), grid, now))
                if episode_key:
                    self.index.db.conn.execute(
                        "INSERT INTO pool_episode_packages(episode_key,fp,profile,duration_ms,updated) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(episode_key,fp,profile) DO UPDATE SET duration_ms=excluded.duration_ms,updated=excluded.updated",
                        (episode_key, fp, profile, duration_ms, now))
                self.index.db.conn.execute("COMMIT")
            except BaseException:
                self.index.db.conn.execute("ROLLBACK")
                raise
        return {"stored": stored > 0, "duplicate": stored == 0, "tiles": len(source_tiles),
                "channel_tiles": len(selected), "sha256": hashlib.sha256(data).hexdigest()}

    @staticmethod
    def _manifest_caption(manifest: dict) -> str:
        while True:
            encoded = base64.urlsafe_b64encode(
                json.dumps(manifest, separators=(",", ":"), ensure_ascii=True).encode()).decode().rstrip("=")
            caption = f"openseek:hints:v1:{encoded}"
            if len(caption.encode()) <= 900:
                return caption
            filename = manifest.get("filename")
            if not filename:
                raise PoolError(400, "manifest_too_large")
            manifest["filename"] = filename[:max(0, len(filename) - 20)]

