"""Rebuildable index for valid tile documents in a Telegram channel.

All channel-table access goes through a `ChannelStore` (SQLite) or
`RemoteChannelStore` (Supabase/PostgREST); Telegram remains the durable store.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import logging
import re
import threading
import time

from .cache import PROFILE
from .db import DB
from .ids import is_fingerprint
from .pool import (MAX_DURATION_MS, MAX_TILE_BYTES, NVMT_PROFILES, PoolError,
                   TILE_INTERVAL_MS, _EPISODE_KEY, _rank_hint_rows, _timing_relation, _tile_metadata_from_filename,
                   encode_nvmt, validate_pool_hints, validate_tile)
from .scheduler import tile_count
from .store import ChannelStore
from .telegram_mt import _HINTS_CAPTION

log = logging.getLogger("openseek.channel_index")
_CAPTION = re.compile(r"^openseek:v1:([0-9a-f]{64}):([^:]+):(\d+):([0-9a-f]{64})$")


def _tile_row(tile: tuple, created: float) -> dict:
    fp, profile, k, pts_ms, duration_ms, n_tiles, size, digest, message_id = tile
    return {"fp": fp, "profile": profile, "k": k, "pts_ms": pts_ms,
            "duration_ms": duration_ms, "n_tiles": n_tiles, "size": size,
            "sha256": digest, "message_id": message_id, "created": created}


class ChannelIndex:
    def __init__(self, store: ChannelStore, telegram, *, db: DB | None = None,
                 channel_id: str | None = None, scan_limit: int = 2000,
                 refresh_s: int = 300, clock=time.time):
        self.store, self.telegram, self.db = store, telegram, db
        self.channel_id = str(channel_id or getattr(telegram, "channel_id", ""))
        self.scan_limit = max(1, int(scan_limit))
        self.refresh_s = max(10, int(refresh_s))
        self.clock = clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""

    def refresh(self, force: bool = False) -> dict:
        with self._lock:
            state = self._state()
            if not force and state and self.clock() - state[1] < self.refresh_s:
                return {"scanned": 0, "indexed": 0, "last_message_id": state[0], "skipped": True}
            try:
                result = self._refresh_once(state[0] if state else 0)
                self.last_error = ""
                return result
            except Exception as exc:
                self.last_error = getattr(exc, "code", type(exc).__name__)
                raise

    def _refresh_once(self, last_message_id: int) -> dict:
        messages = self.telegram.scan(last_message_id, self.scan_limit)
        messages = sorted(messages, key=lambda message: int(message.message_id))
        newest = max((int(message.message_id) for message in messages), default=last_message_id)
        now = self.clock()
        candidates = []
        seen = set()
        for message in messages:
            caption = getattr(message, "caption", "") or ""
            hm = _HINTS_CAPTION.fullmatch(caption)
            if hm:
                try:
                    if len(caption.encode("utf-8")) > 900:
                        raise ValueError("manifest caption too long")
                    raw_b64 = hm.group(1)
                    padded = raw_b64 + ("=" * ((4 - len(raw_b64) % 4) % 4))
                    decoded = json.loads(base64.b64decode(
                        padded, altchars=b"-_", validate=True).decode("utf-8"))
                    self.record_manifest(decoded)
                except Exception as exc:
                    log.debug("ignoring invalid hints message %s (%s)", getattr(message, "message_id", 0), exc)
                continue
            tile = self._parse_message(message)
            if tile is None:
                continue
            fp, profile, k, pts_ms, duration_ms, n_tiles, size, digest, message_id = tile
            key = (fp, profile, k)
            if key in seen:
                continue
            existing = self.store.get_tile(fp, profile, k)
            if existing:
                seen.add(key)
                if existing["sha256"] == digest and existing["duration_ms"] == 0 and duration_ms > 0:
                    candidates.append(_tile_row(tile, now))
                continue
            try:
                data = self.telegram.get_tile(message_id)
                valid_duration = duration_ms or MAX_DURATION_MS
                valid_count = n_tiles or tile_count(valid_duration)
                if len(data) != size or validate_tile(
                        fp, profile, k, pts_ms, valid_duration, valid_count, data) != digest:
                    continue
            except Exception as exc:
                log.debug("ignoring invalid Telegram tile message %d (%s)", message_id, type(exc).__name__)
                continue
            seen.add(key)
            candidates.append(_tile_row(tile, now))
        indexed = self.store.record_tiles(candidates, channel_id=self.channel_id,
                                          last_message_id=newest, scanned_at=now)
        return {"scanned": len(messages), "indexed": indexed, "last_message_id": newest, "skipped": False}

    def rebuild(self) -> dict:
        with self._lock:
            self.store.clear_channel(self.channel_id)
            total_scanned = total_indexed = 0
            last_message_id = 0
            while True:
                try:
                    result = self._refresh_once(last_message_id)
                    self.last_error = ""
                except Exception as exc:
                    self.last_error = getattr(exc, "code", type(exc).__name__)
                    raise
                total_scanned += result["scanned"]
                total_indexed += result["indexed"]
                last_message_id = result["last_message_id"]
                if result["scanned"] < self.scan_limit or last_message_id == 0:
                    break
            return {"scanned": total_scanned, "indexed": total_indexed,
                    "last_message_id": last_message_id, "skipped": False}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="openseek-channel-index", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.refresh_s):
            try:
                self.refresh(force=True)
            except Exception as exc:
                log.warning("Telegram channel refresh failed (%s)", type(exc).__name__)

    def _state(self) -> tuple[int, float] | None:
        return self.store.state(self.channel_id)

    @staticmethod
    def _parse_message(message) -> tuple | None:
        match = _CAPTION.fullmatch(getattr(message, "caption", "") or "")
        if not match:
            return None
        fp, profile, raw_k, digest = match.groups()
        try:
            k = int(raw_k)
            size = int(message.document_size)
            message_id = int(message.message_id)
        except (TypeError, ValueError, AttributeError):
            return None
        if (not is_fingerprint(fp) or profile != PROFILE or not 0 <= k < 1_000_000 or
                size < 125 or size > MAX_TILE_BYTES or message_id <= 0):
            return None
        metadata = _tile_metadata_from_filename(getattr(message, "file_name", ""), k)
        if metadata is None:
            pts_ms, duration_ms, n_tiles = k * TILE_INTERVAL_MS, 0, 0
        else:
            pts_ms, duration_ms, n_tiles = metadata
        return (fp, profile, k, pts_ms, duration_ms, n_tiles, size, digest, message_id)

    def lookup(self, fp: str, profile: str, k: int) -> dict | None:
        if not is_fingerprint(fp) or profile != PROFILE or not isinstance(k, int) or not 0 <= k < 1_000_000:
            raise PoolError(400, "bad_key")
        return self.store.get_tile(fp, profile, k)

    def forget(self, fp: str, profile: str, k: int, message_id: int | None = None) -> bool:
        """Drop a stale or corrupt Telegram reference without affecting a newer winner."""
        if not is_fingerprint(fp) or profile != PROFILE or not isinstance(k, int):
            return False
        return self.store.forget_tile(fp, profile, k, message_id)

    def record_message(self, message) -> bool:
        """Index a just-reposted librarian tile while preserving the first digest."""
        tile = self._parse_message(message)
        if tile is None:
            return False
        fp, profile, k, pts_ms, duration_ms, n_tiles, size, digest, message_id = tile
        return self.store.record_tile({
            "fp": fp, "profile": profile, "k": k, "pts_ms": pts_ms,
            "duration_ms": duration_ms, "n_tiles": n_tiles, "size": size,
            "sha256": digest, "message_id": message_id, "created": self.clock()})

    @contextmanager
    def atomic_recording(self):
        """Group a librarian manifest and its tile references as one index update.

        A remote store without transaction support fails closed before any index row
        is changed rather than risking a partial package record.
        """
        transaction = getattr(self.store, "transaction", None)
        if not callable(transaction):
            raise RuntimeError("channel store cannot atomically record a package")
        with transaction():
            yield

    def record_manifest(self, hints: dict) -> bool:
        """Store validated hints into pool_hints."""
        if not isinstance(hints, dict) or hints.get("v") != 1:
            return False
        fp = hints.get("fp")
        profile = hints.get("profile")
        if not is_fingerprint(fp) or profile not in NVMT_PROFILES:
            return False
        try:
            season = hints.get("season", 0)
            episode = hints.get("episode", 0)
            duration_ms = hints.get("duration_ms")
            size_bytes = hints.get("size_bytes")
            n_tiles = hints.get("n_tiles")
            grid = hints.get("grid")
            created = hints.get("created")
            if (type(season) is not int or type(episode) is not int or
                    type(duration_ms) is not int or
                    (size_bytes is not None and type(size_bytes) is not int) or
                    type(n_tiles) is not int or type(grid) is not int or
                    type(created) not in (int, float) or
                    grid != NVMT_PROFILES[profile] or duration_ms <= 0 or n_tiles <= 0 or
                    n_tiles > (duration_ms + grid - 1) // grid or created < 0):
                return False
            validated = validate_pool_hints(
                hints.get("imdb"),
                season,
                episode,
                duration_ms,
                size_bytes,
                hints.get("filename_norm") or hints.get("filename"),
            )
        except Exception:
            return False
        return self.store.put_hints({
            "fp": fp, "profile": profile, "imdb": validated["imdb"],
            "season": validated["season"], "episode": validated["episode"],
            "duration_ms": validated["duration_ms"], "size_bytes": validated["size_bytes"],
            "filename_norm": validated["filename_norm"], "n_tiles": n_tiles,
            "grid": grid, "created": float(created)})

    def manifest(self, fp: str, profile: str) -> dict | None:
        if not is_fingerprint(fp) or profile != PROFILE:
            raise PoolError(400, "bad_key")
        rows = self.store.tiles_for_package(fp, profile)
        if not rows:
            return None
        known = {(row["duration_ms"], row["n_tiles"]) for row in rows
                 if row["duration_ms"] > 0 and row["n_tiles"] > 0}
        duration_ms, n_tiles = next(iter(known)) if len(known) == 1 else (0, 0)
        return {"fp": fp, "profile": profile, "duration_ms": duration_ms, "n_tiles": n_tiles,
                "tiles": [{"k": row["k"], "pts_ms": row["pts_ms"], "size": row["size"],
                           "sha256": row["sha256"]} for row in rows]}

    def episode_candidates(self, episode_key: str, duration_ms: int, limit: int = 8) -> list[dict]:
        if not _EPISODE_KEY.fullmatch(episode_key or "") or not isinstance(duration_ms, int) or duration_ms <= 0:
            raise PoolError(400, "bad_episode_key")
        hint_key = self.store.episode_hint_key(episode_key, duration_ms)
        if hint_key is not None:
            imdb, season, episode = hint_key
            candidates = self.candidates(duration_ms=duration_ms, imdb=imdb,
                                         season=season, episode=episode, limit=limit)
            for candidate in candidates:
                candidate["duration_delta_ms"] = abs(candidate["duration_ms"] - duration_ms)
            return candidates

        # A manifest-less legacy package still needs the duration-only path.
        tolerance = max(90_000, int(duration_ms * 0.15))
        rows = self.store.episode_packages_for_key(episode_key, duration_ms, tolerance, limit)
        candidates = []
        for fp, profile, source_ms, updated, n_tiles in rows:
            grid = NVMT_PROFILES.get(profile)
            timing = _timing_relation(duration_ms, int(source_ms))
            if grid is None or timing is None:
                continue
            relation, a_num, a_den = timing
            candidates.append({
                "fp": fp, "profile": profile, "duration_ms": int(source_ms),
                "duration_delta_ms": abs(int(source_ms) - duration_ms),
                "n_slots": (int(source_ms) + grid - 1) // grid, "n_tiles": int(n_tiles),
                "score": {"same_length": 100, "speed_24_25": 95, "length_differs": 90}[relation],
                "match": "episode_key+duration", "relation": relation, "approximate": True,
                "a_num": a_num, "a_den": a_den, "b_ms": 0, "source_duration_ms": int(source_ms),
            })
        return candidates

    def candidates(self, *, duration_ms: int, imdb: str | None = None, season: int = 0,
                   episode: int = 0, size_bytes: int | None = None, filename: str | None = None,
                   exclude_fp: str | None = None, limit: int = 8,
                   device_id: str | None = None, work_key: str | None = None) -> list[dict]:
        """Rank channel-backed packages using the same hint policy as the host pool."""
        query = validate_pool_hints(imdb, season, episode, duration_ms, size_bytes, filename)
        filename_norm = query["filename_norm"]
        if not imdb and not filename_norm:
            raise PoolError(400, "missing_hints")
        if exclude_fp is not None and not is_fingerprint(exclude_fp):
            raise PoolError(400, "bad_key")
        tolerance = max(90_000, int(duration_ms * 0.15))
        rows = self.store.hint_rows_for_candidates(duration_ms, tolerance, exclude_fp)
        normalized = []
        for row in rows:
            (fp, profile, row_imdb, row_season, row_episode, source_ms, row_size,
             row_filename, _source_tiles, grid, created, channel_tiles, row_verified) = row
            n_slots = (int(source_ms) + int(grid) - 1) // int(grid)
            normalized.append((fp, profile, row_imdb, row_season, row_episode, source_ms, row_size,
                               row_filename, channel_tiles, grid, created, n_slots, row_verified))
        return _rank_hint_rows(normalized, duration_ms=duration_ms, imdb=imdb, season=season,
                               episode=episode, size_bytes=size_bytes, filename_norm=filename_norm,
                               device_id=device_id, work_key=work_key, db=self.db, limit=limit)

    def package_slots(self, fp: str, profile: str, slots: list[int]) -> tuple[bytes, dict] | None:
        if not slots or len(slots) > 12 or any(not isinstance(slot, int) or slot < 0 for slot in slots):
            raise PoolError(400, "bad_slots")
        grid = NVMT_PROFILES.get(profile)
        if not is_fingerprint(fp) or grid is None:
            raise PoolError(400, "bad_key")
        rows = self.store.tiles_for_package(fp, PROFILE)
        if not rows or any(row["duration_ms"] <= 0 or row["n_tiles"] <= 0 for row in rows):
            return None
        duration_ms, n_tiles = rows[0]["duration_ms"], rows[0]["n_tiles"]
        if (any(row["duration_ms"] != duration_ms or row["n_tiles"] != n_tiles for row in rows) or
                n_tiles != (duration_ms + 9_999) // 10_000 or TILE_INTERVAL_MS % grid):
            raise PoolError(502, "stored_tile_corrupt")
        tile_step = TILE_INTERVAL_MS // grid
        by_slot = {row["k"] * tile_step: row for row in rows}
        selected_rows = {}
        for wanted in dict.fromkeys(slots):
            if wanted in by_slot:
                selected_rows[wanted] = by_slot[wanted]
            else:
                nearest = min(by_slot, key=lambda candidate: abs(candidate - wanted))
                if abs(nearest - wanted) <= 1:
                    selected_rows.setdefault(nearest, by_slot[nearest])
        if not selected_rows:
            return None
        selected = {}
        for slot, row in selected_rows.items():
            data = self.telegram.get_tile(row["message_id"])
            if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise PoolError(502, "stored_tile_corrupt")
            try:
                validate_tile(fp, PROFILE, row["k"], row["pts_ms"], duration_ms, n_tiles, data)
            except PoolError as exc:
                raise PoolError(502, "stored_tile_corrupt") from exc
            selected[slot] = (row["pts_ms"], data)
        n_slots = (duration_ms + grid - 1) // grid
        data = encode_nvmt(grid, n_slots, selected)
        return data, {"duration_ms": duration_ms, "n_slots": n_slots, "n_tiles": len(selected),
                      "sha256": hashlib.sha256(data).hexdigest(), "profile": profile}

    def status(self) -> dict:
        state = self._state()
        counts = self.store.counts()
        return {"channel_id": self.channel_id, "last_message_id": state[0] if state else 0,
                "scanned_at": state[1] if state else 0, "tiles": counts["tiles"],
                "hints": counts["hints"], "episode_packages": counts["episode_packages"],
                "last_error": self.last_error,
                "entity_type": getattr(self.telegram, "entity_type", "unknown"),
                "entity_resolved": getattr(self.telegram, "entity_resolved", None),
                "history_readable": getattr(self.telegram, "history_readable", None),
                "channel_writable": getattr(self.telegram, "can_post", None),
                "detected_channel_writable": getattr(self.telegram, "detected_can_post", None),
                "post_permission_error": getattr(self.telegram, "post_permission_error", "")}
