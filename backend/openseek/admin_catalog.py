"""Thin admin catalog/moderation adapters for the browser dashboard.

`create_admin_app` calls `install_admin_catalog(app, core)` before `Guard`.
Library reuses SQLite `ChannelStore`, host `PoolService`, or `RemoteChannelStore`
(existing `pool_packages` view); Telegram stays canonical. Contributors reuse
the CLI's local SQLite tables; on `vercel` that DB is scratch, so unsupported.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from .cache import PROFILE
from .ids import is_fingerprint
from .pool import NVMT_PROFILES, PoolError, PoolService, decode_nvmt
from .remote import RemoteChannelStore, RemoteStoreError
from .store import ChannelStore

log = logging.getLogger("openseek.admin_catalog")

_DEFAULT_LIBRARY_LIMIT = 24
_DEFAULT_CONTRIBUTOR_LIMIT = 50
_MAX_LIMIT = 100
_MAX_OFFSET = 100000
_MAX_SENDER_ID = 2**63 - 1


def _err(status: int, code: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers=headers)


def _pool_unavailable() -> JSONResponse:
    log.warning("admin catalog pool backend unavailable")
    return _err(503, "pool_backend_unavailable", {"Retry-After": "5"})


def _parse_pagination(request: Request, default_limit: int) -> tuple[int, int] | None:
    limit_raw = request.query_params.get("limit")
    offset_raw = request.query_params.get("offset")
    try:
        limit = int(limit_raw) if limit_raw is not None else default_limit
        offset = int(offset_raw) if offset_raw is not None else 0
    except (TypeError, ValueError):
        return None
    return max(1, min(limit, _MAX_LIMIT)), max(0, min(offset, _MAX_OFFSET))


def _library_mode(core) -> tuple[str, object | None]:
    index = getattr(core, "channel_index", None)
    if index is not None:
        store = getattr(index, "store", None)
        if isinstance(store, ChannelStore):
            return "channel_sqlite", store
        if isinstance(store, RemoteChannelStore):
            return "channel_remote", store
        if store is not None and hasattr(store, "catalog_page"):
            return "channel", store
        return "unsupported", None
    if core.settings.effective_pool_mode == "host" and getattr(core, "pool", None) is not None:
        if isinstance(core.pool, PoolService):
            return "host", core.pool
    return "unsupported", None


def _contributors_supported(core) -> bool:
    if core.settings.profile == "vercel":
        return False
    mode = core.settings.effective_pool_mode
    if mode not in ("channel", "host"):
        return False
    if mode == "channel" and getattr(core, "channel_index", None) is None:
        return False
    return not (mode == "host" and getattr(core, "pool", None) is None)


def _host_catalog_page(core, limit: int, offset: int) -> tuple[list[dict], bool]:
    rows = core.db.q(
        "SELECT h.fp,h.profile,h.filename_norm,h.imdb,h.season,h.episode,"
        "h.duration_ms,h.n_tiles,h.created,"
        "EXISTS (SELECT 1 FROM pool_tiles t WHERE t.fp=h.fp AND t.profile=?) OR "
        "EXISTS (SELECT 1 FROM pool_packages p WHERE p.fp=h.fp AND p.profile=h.profile) "
        "FROM pool_hints h ORDER BY h.created DESC, h.fp ASC, h.profile ASC "
        "LIMIT ? OFFSET ?",
        (PROFILE, limit + 1, offset))
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [{
        "fp": row[0], "profile": row[1], "filename": row[2], "imdb": row[3],
        "season": int(row[4]), "episode": int(row[5]), "duration_ms": int(row[6]),
        "n_tiles": int(row[7]), "created": float(row[8]), "thumbnail_available": bool(row[9]),
    } for row in rows]
    return items, has_more


def _parse_sender_id(raw: str) -> int | None:
    if len(raw) <= 19 and raw.isascii() and raw.isdigit():
        value = int(raw)
        if 0 < value <= _MAX_SENDER_ID:
            return value
    return None


def install_admin_catalog(app, core) -> None:
    @app.get("/admin/library")
    async def library_list(request: Request):
        parsed = _parse_pagination(request, _DEFAULT_LIBRARY_LIMIT)
        if parsed is None:
            return _err(400, "bad_request")
        limit, offset = parsed
        mode, store = _library_mode(core)
        if mode == "unsupported":
            return JSONResponse({"supported": False, "items": [], "has_more": False},
                                headers={"Cache-Control": "no-store"})
        try:
            if mode == "host":
                items, has_more = await asyncio.to_thread(_host_catalog_page, core, limit, offset)
            else:
                items, has_more = await asyncio.to_thread(store.catalog_page, limit, offset)
        except RemoteStoreError:
            return _pool_unavailable()
        except PoolError as exc:
            return _err(exc.status, exc.code)
        except Exception as exc:
            log.warning("admin library read failed (%s)", type(exc).__name__)
            return _pool_unavailable()
        return JSONResponse({"supported": True, "items": items, "has_more": has_more},
                            headers={"Cache-Control": "no-store"})

    @app.get("/admin/library/{fp}/thumbnail")
    async def library_thumbnail(request: Request, fp: str):
        if not is_fingerprint(fp):
            return _err(400, "bad_key")
        profile = request.query_params.get("profile", "")
        if profile not in NVMT_PROFILES and profile != PROFILE:
            return _err(400, "bad_profile")
        mode, _store = _library_mode(core)
        pool = getattr(core, "pool", None)
        if mode == "unsupported" or pool is None:
            return _err(404, "not_found")
        try:
            manifest = await asyncio.to_thread(pool.manifest, fp, PROFILE)
        except RemoteStoreError:
            return _pool_unavailable()
        except PoolError as exc:
            if exc.status == 404:
                return _err(404, "not_found")
            if exc.code == "bad_key":
                return _err(400, "bad_key")
            return _err(exc.status, exc.code)
        except Exception as exc:
            log.warning("admin thumbnail manifest failed (%s)", type(exc).__name__)
            return _pool_unavailable()
        if manifest is not None:
            for tile in (manifest.get("tiles") or [])[:8]:
                try:
                    k = int(tile["k"])
                except (KeyError, TypeError, ValueError):
                    continue
                try:
                    got = await asyncio.to_thread(pool.tile, fp, PROFILE, k)
                except RemoteStoreError:
                    return _pool_unavailable()
                except PoolError:
                    continue
                except Exception as exc:
                    log.warning("admin thumbnail tile read failed (%s)", type(exc).__name__)
                    return _pool_unavailable()
                if got is not None:
                    return Response(got[0], media_type="image/jpeg",
                                    headers={"Cache-Control": "private, no-store"})
        if isinstance(pool, PoolService) and profile in NVMT_PROFILES:
            try:
                got = await asyncio.to_thread(pool.package, fp, profile)
            except RemoteStoreError:
                return _pool_unavailable()
            except PoolError:
                return _err(404, "not_found")
            if got is not None:
                try:
                    package_bytes, meta = got
                    _grid, _slots, decoded = decode_nvmt(fp, profile, int(meta["duration_ms"]), package_bytes)
                    if decoded:
                        jpeg = decoded[min(decoded)][1]
                        return Response(jpeg, media_type="image/jpeg",
                                        headers={"Cache-Control": "private, no-store"})
                except (PoolError, Exception):
                    return _err(404, "not_found")
        return _err(404, "not_found")

    @app.get("/admin/contributors")
    async def contributors_list(request: Request):
        parsed = _parse_pagination(request, _DEFAULT_CONTRIBUTOR_LIMIT)
        if parsed is None:
            return _err(400, "bad_request")
        limit, offset = parsed
        if not _contributors_supported(core):
            return {"supported": False, "quotas": [], "blocked": []}
        try:
            quota_rows = await asyncio.to_thread(
                core.db.q,
                "SELECT sender_id, work_key, day, count FROM pool_sender_quota "
                "ORDER BY day DESC, sender_id ASC, work_key ASC LIMIT ? OFFSET ?",
                (limit + 1, offset))
            blocked_rows = await asyncio.to_thread(
                core.db.q,
                "SELECT sender_id, created FROM pool_blocked_senders "
                "ORDER BY created DESC, sender_id ASC LIMIT ? OFFSET ?",
                (limit + 1, offset))
        except Exception as exc:
            log.warning("admin contributors read failed (%s)", type(exc).__name__)
            return _err(503, "moderation_unavailable", {"Retry-After": "5"})
        quotas_has_more = len(quota_rows) > limit
        blocked_has_more = len(blocked_rows) > limit
        quotas = [{"sender_id": int(r[0]), "work_key": r[1], "day": r[2], "count": int(r[3])}
                  for r in quota_rows[:limit]]
        blocked = [{"sender_id": int(r[0]), "created": float(r[1])}
                   for r in blocked_rows[:limit]]
        return {"supported": True, "quotas": quotas, "blocked": blocked,
                "has_more_quotas": quotas_has_more, "has_more_blocked": blocked_has_more,
                "limit": limit, "offset": offset}

    @app.put("/admin/contributors/{sender_id}/block", status_code=204)
    async def contributor_block(sender_id: str):
        parsed = _parse_sender_id(sender_id)
        if parsed is None:
            return _err(400, "bad_request")
        if not _contributors_supported(core):
            return _err(409, "moderation_unsupported")
        try:
            await asyncio.to_thread(core.db.block_pool_sender, parsed)
        except Exception as exc:
            log.warning("admin contributor block failed (%s)", type(exc).__name__)
            return _err(503, "moderation_unavailable", {"Retry-After": "5"})
        return Response(status_code=204)

    @app.delete("/admin/contributors/{sender_id}/block", status_code=204)
    async def contributor_unblock(sender_id: str):
        parsed = _parse_sender_id(sender_id)
        if parsed is None:
            return _err(400, "bad_request")
        if not _contributors_supported(core):
            return _err(409, "moderation_unsupported")
        try:
            await asyncio.to_thread(core.db.unblock_pool_sender, parsed)
        except Exception as exc:
            log.warning("admin contributor unblock failed (%s)", type(exc).__name__)
            return _err(503, "moderation_unavailable", {"Retry-After": "5"})
        return Response(status_code=204)
