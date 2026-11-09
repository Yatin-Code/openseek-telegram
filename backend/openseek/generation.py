"""Owner-triggered NVMT extraction and publication.

Generation URLs stay in process memory only. The state stores contain progress and
publication ids, never the source URL, request headers, or media bytes.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import secrets
import threading
import time
from .pool import (MAX_DURATION_MS, MAX_NVMT_BYTES, NVMT_PROFILES, PoolError,
                   decode_nvmt, encode_nvmt)
from .scheduler import GRID_MS
from .uploads import UploadError

log = logging.getLogger("openseek.generation")
_GENERATION_ID = re.compile(r"^[0-9a-f]{32}$")
_STATE_FIELDS = {"state", "fp", "profile", "duration_ms", "tiles_done", "tiles_total",
                 "upload_id", "error", "package_sha256", "created", "updated"}
_COLUMNS = ("generation_id,state,fp,profile,duration_ms,tiles_done,tiles_total,upload_id,error,"
            "package_sha256,created,updated")


class GenerationError(Exception):
    def __init__(self, code: str):
        self.code = code if re.fullmatch(r"[a-z_]+", code or "") else "generation_failed"
        super().__init__(self.code)


class GenerationStateStore:
    """SQLite on local/VPS and PostgREST on Vercel; status only, no source material."""

    def __init__(self, db, remote=None):
        self.db = db
        self.remote = remote

    def create(self, generation_id: str, now: float) -> None:
        row = {"generation_id": generation_id, "state": "generating", "fp": None,
               "profile": None, "duration_ms": None, "tiles_done": 0, "tiles_total": None,
               "upload_id": None, "error": None, "package_sha256": None,
               "created": float(now), "updated": float(now)}
        if self.remote is not None:
            self.remote.upsert("pool_generation_jobs", [row], on_conflict="generation_id")
        else:
            self.db.x(f"INSERT INTO generation_jobs({_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      tuple(row[name] for name in _COLUMNS.split(",")))

    def get(self, generation_id: str) -> dict | None:
        if self.remote is not None:
            rows = self.remote.select("pool_generation_jobs", [
                ("select", _COLUMNS), ("generation_id", f"eq.{generation_id}"), ("limit", "1")])
            row = rows[0] if rows else None
        else:
            rows = self.db.q(f"SELECT {_COLUMNS} FROM generation_jobs WHERE generation_id=?",
                             (generation_id,))
            row = dict(zip(_COLUMNS.split(","), rows[0])) if rows else None
        if row is None:
            return None
        for key in ("duration_ms", "tiles_done", "tiles_total"):
            if row.get(key) is not None:
                row[key] = int(row[key])
        row["created"], row["updated"] = float(row["created"]), float(row["updated"])
        return row

    def update(self, generation_id: str, **values) -> None:
        changes = {key: value for key, value in values.items() if key in _STATE_FIELDS}
        changes["updated"] = time.time()
        if self.remote is not None:
            self.remote.patch("pool_generation_jobs", [("generation_id", f"eq.{generation_id}")], changes)
            return
        assignments = ",".join(f"{key}=?" for key in changes)
        self.db.x(f"UPDATE generation_jobs SET {assignments} WHERE generation_id=?",
                  (*changes.values(), generation_id))

    def recent(self, limit: int = 20) -> list[dict]:
        if self.remote is not None:
            rows = self.remote.select("pool_generation_jobs", [
                ("select", _COLUMNS), ("order", "created.desc"), ("limit", str(limit))])
            return [self._normalized(row) for row in rows]
        rows = self.db.q(f"SELECT {_COLUMNS} FROM generation_jobs ORDER BY created DESC LIMIT ?",
                         (max(1, min(int(limit), 20)),))
        return [self._normalized(dict(zip(_COLUMNS.split(","), row))) for row in rows]

    @staticmethod
    def _normalized(row: dict) -> dict:
        for key in ("duration_ms", "tiles_done", "tiles_total"):
            if row.get(key) is not None:
                row[key] = int(row[key])
        row["created"], row["updated"] = float(row["created"]), float(row["updated"])
        return row

    def cleanup(self, before: float) -> None:
        if self.remote is not None:
            self.remote.delete("pool_generation_jobs", [("created", f"lt.{float(before)}")])
        else:
            self.db.x("DELETE FROM generation_jobs WHERE created<?", (float(before),))

    def active_count(self) -> int:
        if self.remote is not None:
            return self.remote.count("pool_generation_jobs", "generation_id",
                                     [("state", "in.(queued,generating,publishing)")])
        rows = self.db.q("SELECT COUNT(*) FROM generation_jobs WHERE state IN ('queued','generating','publishing')")
        return int(rows[0][0]) if rows else 0


class GenerationService:
    """Runs a bounded full extraction, validates its NVMT, then publishes through Telegram."""

    def __init__(self, core, store: GenerationStateStore):
        self.core, self.store = core, store
        self.lock = threading.RLock()
        self.active: dict[str, object] = {}

    def start(self, source_url: str, profile: str, imdb: str | None, season: int,
              episode: int, filename: str | None) -> tuple[str, object]:
        if profile not in NVMT_PROFILES:
            raise GenerationError("bad_profile")
        now = time.time()
        with self.lock:
            if len(self.active) >= 1 or self.store.active_count() >= 1:
                raise GenerationError("generation_busy")
            generation_id = secrets.token_hex(16)
            self.store.cleanup(now - 86400)
            self.store.create(generation_id, now)
            hints = {"imdb": imdb, "season": season, "episode": episode, "filename": filename}
            try:
                job = self.core.jobs.create(
                    generation_id, source_url, {})
            except Exception as exc:
                code = "source_forbidden" if type(exc).__name__ == "SourceForbidden" else (
                    "busy" if type(exc).__name__ == "Busy" else "source_unavailable")
                self.store.update(generation_id, state="failed", error=code)
                raise
            self.active[generation_id] = job
            # Catalog hints are retained only in this closure and reach the package
            # index after extraction succeeds. No source URL is persisted.
            job.generation_hints = hints
            job.generation_profile = profile
            return generation_id, job

    def run(self, generation_id: str, job) -> None:
        deadline = time.monotonic() + (270 if self.core.settings.profile == "vercel" else 3600)
        try:
            while time.monotonic() < deadline:
                status = self.store.get(generation_id)
                if status is None or status["state"] == "cancelled":
                    return
                source = self.core.jobs.sources.get(job.fp) if job.fp else None
                if source is not None:
                    source.touch()
                snap = self.core.jobs.snapshot(job)
                state, error = self.core.jobs.state(job)
                self.store.update(generation_id, state="generating", fp=job.fp,
                                  duration_ms=snap["source_duration_ms"],
                                  tiles_done=snap["tiles_done"], tiles_total=snap["tiles_total"])
                if state == "failed":
                    raise GenerationError(error or "generation_failed")
                if state == "complete":
                    break
                time.sleep(0.25)
            else:
                raise GenerationError("generation_timeout")

            package, fp, duration_ms, size_bytes, digest = self._package(job)
            self.store.update(generation_id, state="publishing", fp=fp, profile=job.generation_profile,
                              duration_ms=duration_ms, tiles_done=len(decode_nvmt(
                                  fp, job.generation_profile, duration_ms, package)[2]),
                              tiles_total=math.ceil(duration_ms / NVMT_PROFILES[job.generation_profile]),
                              package_sha256=digest)
            self._publish(generation_id, fp, job.generation_profile, duration_ms, package,
                          size_bytes, job.generation_hints)
        except GenerationError as exc:
            self.store.update(generation_id, state="failed", error=exc.code)
        except (PoolError, UploadError) as exc:
            self.store.update(generation_id, state="failed", error=exc.code)
        except Exception as exc:
            log.warning("NVMT generation failed (%s)", type(exc).__name__)
            self.store.update(generation_id, state="failed", error="generation_failed")
        finally:
            self.core.jobs.delete(job)
            with self.lock:
                self.active.pop(generation_id, None)

    def _package(self, job) -> tuple[bytes, str, int, int, str]:
        if not job.fp:
            raise GenerationError("source_unavailable")
        entry = self.core.cache.entry(job.fp)
        if entry is None or not entry.duration_ms or not 0 < entry.duration_ms <= MAX_DURATION_MS:
            raise GenerationError("bad_duration")
        profile = job.generation_profile
        grid = NVMT_PROFILES[profile]
        slots = math.ceil(entry.duration_ms / grid)
        tolerance_ms = int(getattr(getattr(self.core, "settings", None),
                                   "tolerance_ms", GRID_MS // 2))
        package_tiles: dict[int, tuple[int, bytes]] = {}
        total_bytes = 20
        for k, pts_ms in sorted(entry.tiles.items()):
            # The cache key is the scheduled ten-second tile. Keep that identity when
            # mapping to NVMT, even when the decoded PTS is within tolerance of the
            # scheduled timestamp; g5 therefore uses every other profile slot.
            scheduled_ms = k * GRID_MS
            slot, remainder = divmod(scheduled_ms, grid)
            if (remainder or slot >= slots or abs(pts_ms - scheduled_ms) > tolerance_ms or
                    slot in package_tiles):
                continue
            tile = self.core.cache.tile_bytes(job.fp, k)
            if tile is None:
                continue
            total_bytes += 12 + len(tile)
            if total_bytes > MAX_NVMT_BYTES:
                raise GenerationError("package_too_large")
            package_tiles[slot] = (pts_ms, tile)
        if not package_tiles:
            raise GenerationError("no_tiles_generated")
        package = encode_nvmt(grid, slots, package_tiles)
        decode_nvmt(job.fp, profile, entry.duration_ms, package)
        source = self.core.jobs.sources.get(job.fp)
        size_bytes = int(source.size or 0) if source is not None else None
        digest = hashlib.sha256(package).hexdigest()
        return package, job.fp, int(entry.duration_ms), size_bytes, digest

    def _publish(self, generation_id: str, fp: str, profile: str, duration_ms: int,
                 package: bytes, size_bytes: int | None, hints: dict) -> None:
        channel_store = self.core.channel_store
        if channel_store is None:
            # Issue #78 (F3 local/VPS): a host install has no MTProto channel store;
            # its PoolService validates the package, publishes the shared tiles plus
            # the hints manifest and keeps the byte-exact package and index itself.
            pool = self.core.pool
            if not hasattr(pool, "upload_package"):
                raise GenerationError("pool_unavailable")
            pool.upload_package(fp, profile, duration_ms, package, imdb=hints["imdb"],
                                season=hints["season"], episode=hints["episode"],
                                size_bytes=size_bytes, filename=hints["filename"])
            self.store.update(generation_id, state="complete", error=None)
            return
        channel_store.start()
        if getattr(channel_store, "can_post", None) is not True:
            raise GenerationError("read_only_channel")
        if self.core.uploads is not None:
            upload_id = self.core.uploads.stage_package(
                fp=fp, profile=profile, duration_ms=duration_ms, data=package,
                size_bytes=size_bytes, imdb=hints["imdb"], season=hints["season"],
                episode=hints["episode"], filename=hints["filename"])
            self.store.update(generation_id, state="publishing", upload_id=upload_id)
            return
        self.core.pool.upload_package(
            fp, profile, duration_ms, package, imdb=hints["imdb"], season=hints["season"],
            episode=hints["episode"], size_bytes=size_bytes, filename=hints["filename"])
        self.store.update(generation_id, state="complete", error=None)

    def status(self, generation_id: str) -> dict | None:
        if not _GENERATION_ID.fullmatch(generation_id):
            return None
        row = self.store.get(generation_id)
        if row is None:
            return None
        if row["state"] == "publishing" and row.get("upload_id") and self.core.uploads is not None:
            upload = self.core.uploads.store.get_upload(row["upload_id"])
            if upload is not None:
                if upload["state"] == "accepted":
                    self.store.update(generation_id, state="complete", error=None)
                    row["state"], row["error"] = "complete", None
                elif upload["state"] in ("failed", "expired"):
                    error = upload.get("error") or "publication_failed"
                    self.store.update(generation_id, state="failed", error=error)
                    row["state"], row["error"] = "failed", error
        if (row["state"] in ("queued", "generating") and
                time.time() - row["updated"] > 600):
            self.store.update(generation_id, state="failed", error="generation_interrupted")
            row["state"], row["error"] = "failed", "generation_interrupted"
        return row

    def recent(self) -> list[dict]:
        out = []
        for row in self.store.recent():
            current = self.status(row["generation_id"])
            if current is not None:
                out.append(current)
        return out

    def cancel(self, generation_id: str) -> bool:
        row = self.status(generation_id)
        if row is None or row["state"] not in ("queued", "generating"):
            return False
        self.store.update(generation_id, state="cancelled", error=None)
        with self.lock:
            job = self.active.get(generation_id)
        if job is not None:
            self.core.jobs.delete(job)
        return True

    def close(self) -> None:
        with self.lock:
            active = list(self.active.items())
        for generation_id, _job in active:
            self.cancel(generation_id)
