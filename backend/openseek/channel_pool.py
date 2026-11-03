"""HTTP pool adapter backed by a node's indexed Telegram channel."""
from __future__ import annotations

import hashlib
import secrets
import threading
import time

from .cache import PROFILE
from .channel_index import ChannelIndex
from .ids import is_fingerprint
from .pool import (MAX_DURATION_MS, NVMT_PROFILES, PoolError, TILE_INTERVAL_MS,
                   _EPISODE_KEY, encode_nvmt, validate_tile)
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
        if len(package) > 16 * 1024 * 1024:
            raise PoolError(502, "stored_package_too_large")
        return package, {"duration_ms": duration, "n_slots": n_slots, "n_tiles": len(tiles),
                         "sha256": hashlib.sha256(package).hexdigest(), "profile": profile}

    def package_slots(self, fp: str, profile: str, slots: list[int]) -> tuple[bytes, dict] | None:
        return self.index.package_slots(fp, profile, slots)

    def episode_candidates(self, episode_key: str, duration_ms: int, limit: int = 8) -> list[dict]:
        return self.index.episode_candidates(episode_key, duration_ms, limit)

    def candidates(self, **hints) -> list[dict]:
        # Hint-aware ranking is added with B3/B4 once Telegram manifests rebuild the index.
        return []

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
        raise PoolError(501, "channel_upload_pending")

    def upload_package(self, *args, **kwargs) -> dict:
        raise PoolError(501, "channel_upload_pending")

