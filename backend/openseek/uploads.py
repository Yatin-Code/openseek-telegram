"""Vercel staged-upload lifecycle and publication queue (F1b).

Supabase Storage is only temporary staging to get around the serverless request-body
limit. Telegram remains the canonical durable package store: an upload is `accepted`
only after `pool.upload_package(...)` succeeds, and the staged object is deleted then.
Local/VPS profiles keep the direct `PUT /v1/pool/{fp}/nvmt` path and never build this
service.

State machine:

    upload:  pending --finalize--> queued --claim--> publishing --success--> accepted
                 \\--expiry--> expired                  \\--failure (1..4)--> queued
                                                       \\--failure (5)----> failed
    queue:   queued  --claim--> publishing --success--> done
                 ^                 \\--failure (1..4)--> queued (next_attempt backoff)
                 \\------------- failure (5) ----------> failed

All transitions are conditional on the expected previous state, so overlapping cron
ticks and duplicate finalize calls cannot publish the same upload twice. Telegram is
also first-writer-wins for every tile/slot, making a duplicate publication harmless.
"""
from __future__ import annotations

import logging
import secrets
import time
from urllib.parse import urlsplit

from .ids import is_fingerprint
from .pool import (MAX_DURATION_MS, MAX_NVMT_BYTES, NVMT_PROFILES, PoolError,
                   validate_pool_hints)

log = logging.getLogger("openseek.uploads")

UPLOAD_TTL_S = 900
MAX_ATTEMPTS = 5
MAX_BACKOFF_S = 300
MAX_DRAIN_BATCH = 3
_UPLOAD_ID_MIN = 8
_UPLOAD_ID_MAX = 64


class UploadError(Exception):
    """A client-visible upload failure. The message never includes a secret or URL."""

    def __init__(self, status: int, code: str):
        self.status = int(status)
        self.code = code
        super().__init__(f"{self.status} {code}")


def _valid_upload_id(upload_id: str) -> bool:
    return (isinstance(upload_id, str) and _UPLOAD_ID_MIN <= len(upload_id) <= _UPLOAD_ID_MAX
            and all(ch.isalnum() or ch in "_-" for ch in upload_id))


class UploadService:
    """Own create/finalize/status and delegate queue draining to `PublicationWorker`."""

    def __init__(self, settings, supabase, pool, store, *, clock=time.time,
                 ttl_s: int = UPLOAD_TTL_S, max_attempts: int = MAX_ATTEMPTS):
        self.settings = settings
        self.supabase = supabase
        self.pool = pool
        self.store = store
        self.clock = clock
        self.ttl_s = int(ttl_s)
        self.max_attempts = int(max_attempts)
        self.worker = PublicationWorker(self)

    # ---------------------------------------------------------------- create
    def create(self, *, device_id: str | None, fp: str, profile: str, duration_ms: int,
               size_bytes: int | None, imdb: str | None, season: int, episode: int,
               filename: str | None) -> dict:
        if not is_fingerprint(fp):
            raise UploadError(400, "bad_key")
        if profile not in NVMT_PROFILES:
            raise UploadError(400, "bad_profile")
        if not isinstance(duration_ms, int) or not 1 <= duration_ms <= MAX_DURATION_MS:
            raise UploadError(400, "bad_hints")
        try:
            hints = validate_pool_hints(imdb, season, episode, duration_ms, size_bytes, filename)
        except PoolError as exc:
            raise UploadError(exc.status, exc.code) from None
        if hints["size_bytes"] is not None and hints["size_bytes"] > MAX_NVMT_BYTES:
            # Refuse before signing or writing a row: the direct PUT has the same ceiling.
            raise UploadError(400, "package_too_large")

        now = self.clock()
        upload_id = "u_" + secrets.token_urlsafe(18)
        object_path = f"uploads/{upload_id}.nvmt"
        put_url = self._signed_target(object_path)
        self.store.create_upload({
            "upload_id": upload_id, "device_id": device_id or "", "fp": fp,
            "profile": profile, "duration_ms": duration_ms,
            "size_bytes": hints["size_bytes"], "imdb": hints["imdb"],
            "season": hints["season"], "episode": hints["episode"],
            "filename_norm": hints["filename_norm"], "object_path": object_path,
            "state": "pending", "created": now, "updated": now,
            "expires": now + self.ttl_s, "error": None})
        return {"upload_id": upload_id, "put_url": put_url,
                "expires_at": now + self.ttl_s, "max_bytes": MAX_NVMT_BYTES}

    def _signed_target(self, object_path: str) -> str:
        """Build the absolute signed Storage URL the client uploads to.

        The path contains the short-lived Storage token; it is returned only in this
        response and is never logged. Redirects are never used: the client PUTs here.
        """
        base = str(self.settings.supabase_url or "").rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme != "https" or not parsed.hostname:
            raise UploadError(503, "staging_unavailable")
        try:
            signed = self.supabase.signed_upload_url(object_path, self.ttl_s)
        except Exception as exc:                       # no URL, key, or token in the message
            log.warning("staging sign failed (%s)", type(exc).__name__)
            raise UploadError(503, "staging_unavailable") from None
        if signed.startswith("https://"):
            signed_parsed = urlsplit(signed)
            if (signed_parsed.scheme == "https" and signed_parsed.netloc == parsed.netloc
                    and signed_parsed.path.startswith("/storage/v1/")):
                return signed
            raise UploadError(503, "staging_unavailable")
        if signed.startswith("/storage/v1/"):
            return base + signed
        return f"{base}/storage/v1/{signed.lstrip('/')}"

    # ---------------------------------------------------------------- finalize / status
    def finalize(self, upload_id: str) -> dict:
        if not _valid_upload_id(upload_id):
            raise UploadError(404, "unknown_upload")
        row = self.store.get_upload(upload_id)
        if row is None:
            raise UploadError(404, "unknown_upload")
        now = self.clock()
        if row["state"] == "expired":
            raise UploadError(410, "expired")
        if row["state"] == "pending" and float(row["expires"]) <= now:
            self.store.expire(upload_id, now)
            raise UploadError(410, "expired")
        if row["state"] != "pending":
            raise UploadError(409, "wrong_state")
        if not self.store.enqueue(upload_id, now):
            # Another finalize (or a worker) won the transition.
            raise UploadError(409, "wrong_state")
        return {"upload_id": upload_id, "state": "queued"}

    def status(self, upload_id: str, *, device_id: str | None = None,
               write_key: bool = False) -> dict:
        if not _valid_upload_id(upload_id):
            raise UploadError(404, "unknown_upload")
        row = self.store.get_upload(upload_id)
        if row is None:
            raise UploadError(404, "unknown_upload")
        if not write_key and (not device_id or row["device_id"] != device_id):
            # A write key sees every upload; a device sees only what it created.
            raise UploadError(403, "forbidden")
        now = self.clock()
        if row["state"] == "pending" and float(row["expires"]) <= now:
            self.store.expire(upload_id, now)
            row = self.store.get_upload(upload_id) or {**row, "state": "expired", "error": "expired"}
        return {"upload_id": upload_id, "state": row["state"], "error": row.get("error")}

    # ---------------------------------------------------------------- cron drain
    def drain(self, limit: int = 1) -> dict:
        return self.worker.drain(limit)


class PublicationWorker:
    """Drain queued uploads to Telegram. Per-item failures never escape as exceptions."""

    def __init__(self, service: UploadService):
        self.service = service

    def drain(self, limit: int = 1) -> dict:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 1
        limit = max(1, min(limit, MAX_DRAIN_BATCH))
        processed = accepted = failed = 0
        for _ in range(limit):
            now = self.service.clock()
            item = self.service.store.claim(now)
            if item is None:
                break
            processed += 1
            if self._publish(item, now):
                accepted += 1
            else:
                failed += 1
        return {"processed": processed, "accepted": accepted, "failed": failed}

    def _publish(self, item: dict, now: float) -> bool:
        upload_id = item["upload_id"]
        # A retry after a crash between Telegram acceptance and the local state update
        # republishes the same package; upload_package's first-writer-wins tile check
        # makes that idempotent, and the final state is still `accepted`.
        try:
            data = self.service.supabase.get_object(item["object_path"])
            self.service.pool.upload_package(
                item["fp"], item["profile"], int(item["duration_ms"]), data,
                imdb=item.get("imdb"), season=int(item.get("season", 0)),
                episode=int(item.get("episode", 0)), size_bytes=item.get("size_bytes"),
                filename=item.get("filename_norm"))
        except PoolError as exc:
            self._failure(item, _code(exc.code, "publication_failed"), now)
            return False
        except Exception as exc:                       # RemoteStoreError or transport
            status = getattr(exc, "status", None)
            if status == 404:
                code = "staged_object_missing"
            elif isinstance(exc, KeyError):
                code = "staged_object_missing"
            else:
                code = "staging_unavailable" if status is not None else "publication_failed"
            log.warning("upload publication failed (%s)", type(exc).__name__)
            self._failure(item, code, now)
            return False

        # Telegram accepted the package. The staged object is disposable now; a
        # cleanup failure must not turn a successful publication into a retry.
        try:
            self.service.supabase.delete_object(item["object_path"])
        except Exception as exc:
            log.warning("staged object cleanup failed (%s)", type(exc).__name__)
        self.service.store.complete(upload_id, now)
        return True

    def _failure(self, item: dict, code: str, now: float) -> None:
        attempts = int(item.get("attempts", 0)) + 1
        terminal = attempts >= self.service.max_attempts
        next_attempt = now + min(MAX_BACKOFF_S, 2 ** attempts)
        self.service.store.retry(
            item["upload_id"], attempts=attempts, next_attempt=next_attempt,
            error=code, terminal=terminal, now=now)


def _code(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback
