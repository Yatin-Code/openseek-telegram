"""Telegram DM upload relay for community pools that accept librarian contributions."""
from __future__ import annotations

import base64
import asyncio
import hashlib
import io
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from collections import deque

from .cache import PROFILE
from .channel_index import ChannelIndex
from .db import DB
from .ids import is_fingerprint
from .pool import (MAX_DURATION_MS, MAX_NVMT_BYTES, MAX_TILE_BYTES, NVMT_PROFILES,
                   TILE_INTERVAL_MS, PoolError, _tile_filename,
                   _tile_metadata_from_filename, decode_nvmt, tile_count,
                   validate_pool_hints, validate_tile)
from .telegram_mt import ChannelMessage, TelegramMTProtoStore, _CAPTION, _HINTS_CAPTION

log = logging.getLogger("openseek.contribution")
POOL_LIBRARIAN_MAX_PACKAGE_TILES = int(os.environ.get("OPENSEEK_POOL_LIBRARIAN_MAX_PACKAGE_TILES", "1500"))


class TelegramLibrarian:
    """Validate user-submitted tile documents and repost accepted winners."""

    def __init__(self, store: TelegramMTProtoStore, index: ChannelIndex, *,
                 db: DB | None = None, max_package_tiles: int = POOL_LIBRARIAN_MAX_PACKAGE_TILES,
                 clock=time.monotonic, per_sender_s: float = 1.0, global_per_minute: int = 60):
        self.store, self.index = store, index
        self.db = db if db is not None else getattr(index, "db", None)
        self.max_package_tiles = max_package_tiles
        self.clock = clock
        self.per_sender_s = per_sender_s
        self.global_per_minute = global_per_minute
        self._senders: dict[int, float] = {}
        self._recent: deque[float] = deque()
        self._blocked: dict[int, float] = {}
        self._package_locks: dict[tuple[str, str], list] = {}
        self._package_locks_guard = threading.Lock()

    def attach(self) -> None:
        self.store.set_message_handler(self.handle_message)

    async def handle_message(self, event, store: TelegramMTProtoStore) -> None:
        if not bool(getattr(event, "is_private", False)):
            return
        message = getattr(event, "message", None)
        document = getattr(message, "document", None)
        caption = getattr(message, "message", None) or getattr(message, "text", "") or ""
        match = _CAPTION.fullmatch(caption)
        is_hints = caption.startswith("openseek:hints:v1:")
        raw_b64 = caption[len("openseek:hints:v1:"):] if is_hints else None
        # Ignore ordinary messages and unknown captions. They are not uploads.
        if not document or (not match and not is_hints):
            return
        sender_id = int(getattr(event, "sender_id", 0) or 0)
        if not sender_id or not await self._allow_sender(event, sender_id):
            await self._delete(event)
            return
        if self.db and hasattr(self.db, "is_pool_blocked_sender") and self.db.is_pool_blocked_sender(sender_id):
            await self._delete(event)
            await self._reply_sender(store, sender_id, "Rejected: sender_blocked")
            return

        if is_hints:
            await self._handle_nvmt_package(event, store, sender_id, raw_b64)
            return

        fp, profile, raw_k, digest = match.groups()
        try:
            k = int(raw_k)
            size = int(getattr(document, "size", 0) or 0)
            if size < 125 or size > MAX_TILE_BYTES:
                raise PoolError(413, "tile_too_large")
            file_name = self._file_name(document)
            metadata = _tile_metadata_from_filename(file_name, k)
            if metadata is None:
                pts_ms, duration_ms = k * TILE_INTERVAL_MS, MAX_DURATION_MS
                n_tiles = tile_count(duration_ms)
            else:
                pts_ms, duration_ms, n_tiles = metadata
            data = await store._download_bounded(document)
            if len(data) != size:
                raise PoolError(400, "bad_tile_size")
            got_digest = validate_tile(fp, profile, k, pts_ms, duration_ms, n_tiles, data)
            if got_digest != digest:
                raise PoolError(400, "bad_tile_digest")
        except Exception as exc:
            self._block(sender_id)
            await self._delete(event)
            if isinstance(exc, PoolError):
                log.info("librarian rejected tile (%s)", exc.code)
            else:
                log.info("librarian rejected tile (%s)", type(exc).__name__)
            return

        current = self.index.lookup(fp, PROFILE, k)
        if current is not None:
            try:
                existing = await store._client.get_messages(store._entity, ids=current["message_id"])
            except Exception:
                existing = None
            if existing is not None and await self._valid_existing(store, existing, fp, profile, k):
                await self._delete(event)
                return
            self.index.forget(fp, PROFILE, k, current["message_id"])
        if await self._channel_has_key(store, fp, profile, k):
            await self._delete(event)
            return

        outgoing_name = (_tile_filename(pts_ms, duration_ms, n_tiles)
                         if metadata is not None else "tile.jpg")
        try:
            from telethon.tl.types import DocumentAttributeFilename

            repost = await store._retry(lambda: store._client.send_file(
                store._entity, io.BytesIO(data), caption=caption,
                force_document=True, mime_type="image/jpeg",
                attributes=[DocumentAttributeFilename(outgoing_name)]))
            repost_id = int(repost.id)
            record = ChannelMessage(repost_id, caption, len(data), outgoing_name)
            if not self.index.record_message(record):
                await store._client.delete_messages(store._entity, [repost_id])
                await self._delete(event)
                return
            await self._delete(event)
            log.info("librarian reposted tile=%d", k)
        except Exception as exc:
            log.warning("librarian repost failed (%s)", type(exc).__name__)

    async def _handle_nvmt_package(self, event, store: TelegramMTProtoStore, sender_id: int, raw_b64: str) -> None:
        key = None
        try:
            rem = len(raw_b64) % 4
            padded = raw_b64 + ("=" * ((4 - rem) % 4))
            submitted = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
            fp, profile = submitted.get("fp"), submitted.get("profile")
            if profile == PROFILE:
                profile = "nvmt-v1-g10"
            if is_fingerprint(fp) and profile in NVMT_PROFILES:
                key = (fp, profile)
        except Exception:
            pass
        if key is None:
            return await self._handle_nvmt_package_unlocked(event, store, sender_id, raw_b64)
        async with self._serialize_package(key):
            return await self._handle_nvmt_package_unlocked(event, store, sender_id, raw_b64)

    @asynccontextmanager
    async def _serialize_package(self, key: tuple[str, str]):
        # Count waiters before they block so an entry cannot be removed while another
        # coroutine is queued on its lock. Remove idle keys to bound attacker-driven
        # package-key growth.
        with self._package_locks_guard:
            entry = self._package_locks.get(key)
            if entry is None:
                entry = [asyncio.Lock(), 0]
                self._package_locks[key] = entry
            entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            with self._package_locks_guard:
                entry[1] -= 1
                if entry[1] == 0 and self._package_locks.get(key) is entry:
                    self._package_locks.pop(key, None)

    async def _handle_nvmt_package_unlocked(self, event, store: TelegramMTProtoStore,
                                            sender_id: int, raw_b64: str) -> None:
        message = getattr(event, "message", None)
        document = getattr(message, "document", None)
        mime = getattr(document, "mime_type", "") or ""
        if mime != "application/vnd.openseek.nvmt":
            await self._delete(event)
            await self._reply_sender(store, sender_id, "Rejected: bad_mime_type")
            log.info("librarian rejected package (bad_mime_type)")
            return

        size = int(getattr(document, "size", 0) or 0)
        if size < 20 or size > MAX_NVMT_BYTES:
            await self._delete(event)
            await self._reply_sender(store, sender_id, "Rejected: package_too_large")
            log.info("librarian rejected package (package_too_large)")
            return

        try:
            rem = len(raw_b64) % 4
            padded = raw_b64 + ("=" * ((4 - rem) % 4))
            manifest_dict = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
            if type(manifest_dict.get("v")) is not int or manifest_dict["v"] != 1:
                raise PoolError(400, "unsupported_manifest_version")
            fp = manifest_dict.get("fp")
            profile = manifest_dict.get("profile")
            if profile == PROFILE:
                profile = "nvmt-v1-g10"
                manifest_dict["profile"] = profile
            if not is_fingerprint(fp) or profile not in NVMT_PROFILES:
                raise PoolError(400, "bad_hints")
            imdb = manifest_dict.get("imdb")
            season = int(manifest_dict.get("season", 0))
            episode = int(manifest_dict.get("episode", 0))
            duration_ms = int(manifest_dict.get("duration_ms", 0))
            size_bytes = int(manifest_dict["size_bytes"]) if manifest_dict.get("size_bytes") is not None else None
            filename = manifest_dict.get("filename")
            hints = validate_pool_hints(imdb, season, episode, duration_ms, size_bytes, filename)
        except Exception as exc:
            self._block(sender_id)
            await self._delete(event)
            await self._reply_sender(store, sender_id, "Rejected: malformed_hints")
            log.info("librarian rejected package (malformed_hints: %s)", type(exc).__name__)
            return

        work_key = self._compute_work_key(imdb, season, episode)
        today_utc = time.strftime("%Y-%m-%d", time.gmtime())

        try:
            try:
                data = await store._download_bounded(document, max_bytes=MAX_NVMT_BYTES)
            except TypeError:
                data = await store._download_bounded(document)
            if len(data) > MAX_NVMT_BYTES:
                raise PoolError(413, "package_too_large")
        except Exception as exc:
            await self._delete(event)
            code = exc.code if isinstance(exc, PoolError) else "download_failed"
            await self._reply_sender(store, sender_id, f"Rejected: {code}")
            log.info("librarian package download failed (%s)", code)
            return

        try:
            grid, n_slots, tiles = decode_nvmt(fp, profile, duration_ms, data)
        except Exception as exc:
            self._block(sender_id)
            await self._delete(event)
            code = exc.code if isinstance(exc, PoolError) else "bad_package"
            await self._reply_sender(store, sender_id, f"Rejected: {code}")
            log.info("librarian rejected package (%s)", code)
            return

        max_tiles_allowed = getattr(self, "max_package_tiles", POOL_LIBRARIAN_MAX_PACKAGE_TILES)
        if len(tiles) > n_slots or len(tiles) > max_tiles_allowed:
            self._block(sender_id)
            await self._delete(event)
            await self._reply_sender(store, sender_id, "Rejected: package_too_large")
            log.info("librarian rejected oversized package")
            return

        tile_step = TILE_INTERVAL_MS // grid
        n_channel_tiles = tile_count(duration_ms)
        by_k: dict[int, tuple[int, bytes]] = {}
        for slot, (pts_ms, jpeg_data) in sorted(tiles.items()):
            k = slot // tile_step
            if k not in by_k:
                by_k[k] = (pts_ms, jpeg_data)

        tiles_to_post = []
        for k, (pts_ms, jpeg_data) in sorted(by_k.items()):
            if self.index.lookup(fp, PROFILE, k) is not None:
                continue
            try:
                digest = validate_tile(fp, PROFILE, k, pts_ms, duration_ms, n_channel_tiles, jpeg_data)
                tiles_to_post.append((k, pts_ms, jpeg_data, digest))
            except Exception as exc:
                self._block(sender_id)
                await self._delete(event)
                await self._reply_sender(store, sender_id, "Rejected: bad_tile")
                log.info("librarian rejected invalid tile in package (%s)", type(exc).__name__)
                return

        # Republish only the validated canonical fields. The original caption can
        # contain paths and arbitrary extras, neither of which belong in the pool.
        manifest_record = {
            "v": 1, "fp": fp, "profile": profile,
            "imdb": hints["imdb"], "season": hints["season"],
            "episode": hints["episode"], "duration_ms": hints["duration_ms"],
            "size_bytes": hints["size_bytes"], "n_tiles": len(tiles),
            "grid": grid, "created": int(time.time()),
        }
        if hints["filename_norm"]:
            manifest_record["filename"] = hints["filename_norm"]
        manifest_cap = self._manifest_caption(manifest_record)
        if manifest_cap is None:
            await self._delete(event)
            await self._reply_sender(store, sender_id, "Rejected: manifest_too_large")
            return

        if self.db and work_key and hasattr(self.db, "sender_quota_claim"):
            if not self.db.sender_quota_claim(sender_id, work_key, today_utc, 3):
                await self._delete(event)
                await self._reply_sender(store, sender_id, "Rejected: quota_exceeded")
                log.info("librarian rejected package (quota_exceeded)")
                return

        posted_ids: list[int] = []
        posted_messages: list[ChannelMessage] = []
        manifest_posted_id: int | None = None
        try:
            for k, pts_ms, jpeg_data, digest in tiles_to_post:
                tile_cap = f"openseek:v1:{fp}:{PROFILE}:{k}:{digest}"
                out_name = _tile_filename(pts_ms, duration_ms, n_channel_tiles)
                from telethon.tl.types import DocumentAttributeFilename

                repost = await store._retry(lambda: store._client.send_file(
                    store._entity, io.BytesIO(jpeg_data), caption=tile_cap,
                    force_document=True, mime_type="image/jpeg",
                    attributes=[DocumentAttributeFilename(out_name)]))
                repost_id = int(repost.id)
                posted_ids.append(repost_id)
                posted_messages.append(ChannelMessage(repost_id, tile_cap, len(jpeg_data), out_name))

            manifest_msg = await store._retry(lambda: store._client.send_message(
                store._entity, manifest_cap))
            manifest_posted_id = int(manifest_msg.id)
        except Exception as exc:
            to_delete = list(posted_ids)
            if manifest_posted_id is not None:
                to_delete.append(manifest_posted_id)
            if to_delete:
                try:
                    await store._client.delete_messages(store._entity, to_delete)
                except Exception as del_exc:
                    log.warning("failed to rollback reposted messages (%s)", type(del_exc).__name__)
            if self.db and work_key and hasattr(self.db, "sender_quota_release"):
                self.db.sender_quota_release(sender_id, work_key, today_utc)
            await self._delete(event)
            await self._reply_sender(store, sender_id, "Rejected: repost_failed")
            log.warning("librarian package repost failed (%s)", type(exc).__name__)
            return

        try:
            with self.index.atomic_recording():
                if not self.index.record_manifest(manifest_record):
                    raise ValueError("manifest index rejected record")
                for msg in posted_messages:
                    if not self.index.record_message(msg):
                        raise ValueError("tile index rejected record")
        except Exception:
            to_delete = [*posted_ids, manifest_posted_id]
            try:
                await store._client.delete_messages(store._entity, [mid for mid in to_delete if mid is not None])
            except Exception as del_exc:
                log.warning("failed to rollback unindexed reposts (%s)", type(del_exc).__name__)
            if self.db and work_key and hasattr(self.db, "sender_quota_release"):
                self.db.sender_quota_release(sender_id, work_key, today_utc)
            await self._delete(event)
            await self._reply_sender(store, sender_id, "Rejected: index_failed")
            return

        await self._delete(event)
        await self._reply_sender(store, sender_id, f"Accepted package for {fp[:8]} ({len(posted_messages)} tiles)")
        log.info("librarian accepted and reposted package (%d tiles)", len(posted_messages))

    @staticmethod
    def _manifest_caption(manifest_record: dict) -> str | None:
        def encode(record):
            raw = json.dumps(record, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
            return f"openseek:hints:v1:{encoded}"

        record = dict(manifest_record)
        caption = encode(record)
        while len(caption.encode("utf-8")) > 900 and record.get("filename"):
            shortened = record["filename"][:-1].rstrip(".")
            if shortened:
                record["filename"] = shortened
            else:
                record.pop("filename")
            caption = encode(record)
        return caption if len(caption.encode("utf-8")) <= 900 else None

    @staticmethod
    def _compute_work_key(imdb: str | None, season: int, episode: int) -> str:
        if imdb and season > 0 and episode > 0:
            return hashlib.sha256(f"openseek-episode-v1\nimdb:{imdb}\nseason:{season}\nepisode:{episode}".encode()).hexdigest()
        elif imdb and season == 0 and episode == 0:
            return hashlib.sha256(f"openseek-work-v1\nimdb:{imdb}".encode()).hexdigest()
        return ""

    @staticmethod
    async def _reply_sender(store, sender_id: int, text: str) -> None:
        try:
            client = getattr(store, "_client", None)
            if client and hasattr(client, "send_message"):
                await client.send_message(sender_id, text)
        except Exception:
            pass

    async def _allow_sender(self, event, sender_id: int) -> bool:
        now = self.clock()
        for sender, until in tuple(self._blocked.items()):
            if until <= now:
                self._blocked.pop(sender, None)
        if self._blocked.get(sender_id, 0) > now:
            return False
        try:
            sender = await event.get_sender()
            if bool(getattr(sender, "bot", False)):
                return False
        except Exception:
            return False
        last = self._senders.get(sender_id, 0)
        while self._recent and self._recent[0] < now - 60:
            self._recent.popleft()
        if now - last < self.per_sender_s or len(self._recent) >= self.global_per_minute:
            self._block(sender_id)
            return False
        self._senders[sender_id] = now
        self._recent.append(now)
        if len(self._senders) > 512:
            oldest = min(self._senders, key=self._senders.get)
            self._senders.pop(oldest, None)
        return True

    def _block(self, sender_id: int) -> None:
        self._blocked[sender_id] = self.clock() + 60
        if len(self._blocked) > 512:
            oldest = min(self._blocked, key=self._blocked.get)
            self._blocked.pop(oldest, None)

    async def _channel_has_key(self, store: TelegramMTProtoStore, fp: str, profile: str, k: int) -> bool:
        # The local index is rebuilt before serving. Telegram search covers the
        # short interval before the periodic scanner sees a direct contribution.
        try:
            async for message in store._client.iter_messages(store._entity, search=fp, limit=100):
                caption = getattr(message, "message", None) or getattr(message, "text", "") or ""
                match = _CAPTION.fullmatch(caption)
                if match and match.group(1) == fp and match.group(2) == profile and int(match.group(3)) == k:
                    if await self._valid_existing(store, message, fp, profile, k):
                        return True
        except Exception as exc:
            log.info("librarian duplicate search failed (%s)", type(exc).__name__)
        return False

    async def _valid_existing(self, store, message, fp: str, profile: str, k: int) -> bool:
        document = getattr(message, "document", None)
        caption = getattr(message, "message", None) or getattr(message, "text", "") or ""
        match = _CAPTION.fullmatch(caption)
        if not document or not match or match.group(1) != fp or match.group(2) != profile or int(match.group(3)) != k:
            return False
        try:
            size = int(getattr(document, "size", 0) or 0)
            if size < 125 or size > MAX_TILE_BYTES:
                return False
            name = self._file_name(document)
            metadata = _tile_metadata_from_filename(name, k)
            if metadata is None:
                pts_ms, duration_ms = k * TILE_INTERVAL_MS, MAX_DURATION_MS
                n_tiles = tile_count(duration_ms)
            else:
                pts_ms, duration_ms, n_tiles = metadata
            data = await store._download_bounded(document)
            if len(data) != size or validate_tile(fp, profile, k, pts_ms, duration_ms, n_tiles, data) != match.group(4):
                return False
            self.index.record_message(ChannelMessage(int(message.id), caption, size, name))
            return True
        except Exception:
            return False

    @staticmethod
    def _file_name(document) -> str:
        attrs = getattr(document, "attributes", ()) if document else ()
        return next((getattr(attr, "file_name", "") for attr in attrs
                     if getattr(attr, "file_name", "")), "")

    @staticmethod
    async def _delete(event) -> None:
        try:
            await event.delete()
        except Exception:
            pass
