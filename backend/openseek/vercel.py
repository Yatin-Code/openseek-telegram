"""Vercel ASGI entrypoint for the OpenSeek channel pool.

The function keeps no background workers. It connects to Telegram on the first pool
request, refreshes one bounded channel-index page when due, and uses Supabase for the
index and staged-upload queue. Local/VPS deployments continue to use ``serve()``.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import threading
from dataclasses import replace
from pathlib import Path

from fastapi.responses import JSONResponse

from .app import Core, Guard, create_api_app
from .config import ConfigError, Settings

log = logging.getLogger("openseek.vercel")
runtime_core: Core | None = None


def build_app(settings: Settings | None = None, *, core: Core | None = None):
    """Build one serverless API app without starting local background services."""
    global runtime_core
    if settings is None:
        settings = Settings.from_env()
        if settings.profile == "vercel" and not os.environ.get("OPENSEEK_DATA_DIR"):
            # Vercel's deployment bundle is read-only. SQLite here is only a scratch
            # store for state not yet moved to Supabase; it must never be treated as
            # durable across function instances.
            settings = replace(settings, data_dir=Path(os.environ.get("TMPDIR", "/tmp")) / "openseek")
    if settings.profile != "vercel":
        raise ConfigError("the Vercel entrypoint requires OPENSEEK_PROFILE=vercel")
    settings.validate()
    core = core or Core(settings)
    runtime_core = core
    if core.channel_index is None or core.channel_store is None:
        raise ConfigError("the Vercel profile requires a Telegram channel pool")

    guarded = create_api_app(core)
    api = guarded.app
    initialization_lock = threading.Lock()
    initialized = False

    async def ensure_channel(force_refresh: bool) -> JSONResponse | None:
        nonlocal initialized

        def connect_and_refresh() -> bool:
            nonlocal initialized
            with initialization_lock:
                if not initialized:
                    start = getattr(core.channel_store, "start", None)
                    if start is not None:
                        start()
                    initialized = True
                core.channel_index.refresh(force=force_refresh)
                return True

        try:
            await asyncio.to_thread(connect_and_refresh)
            return None
        except Exception as exc:
            log.warning("Vercel channel refresh failed (%s)", type(exc).__name__)
            try:
                status = await asyncio.to_thread(core.channel_index.status)
            except Exception:
                status = {}
            if not status.get("scanned_at"):
                return JSONResponse({"error": "pool_index_unavailable"}, status_code=503,
                                    headers={"Cache-Control": "no-store"})
            # Keep serving the last known index through a temporary Telegram outage.
            return None

    @api.middleware("http")
    async def serverless_channel_refresh(request, call_next):
        path = request.url.path
        if path.startswith("/v1/pool/") or path == "/v1/internal/tick":
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            token = token if scheme.lower() == "bearer" else ""
            if path == "/v1/internal/tick":
                tick_secrets = (core.settings.cron_secret, core.admin_token)
                authorized = any(token and value and hmac.compare_digest(token, value)
                                 for value in tick_secrets)
            else:
                authorized = bool(
                    core.pool and
                    (core.pool.authorize(token, write=False) or core.pool.authorize(token, write=True))
                ) or bool(getattr(request.state, "device_id", None))
            if not authorized:
                return await call_next(request)
            # The scheduled tick advances initial backfill one bounded page at a time.
            # Normal pool requests refresh only when the shared index is stale.
            unavailable = await ensure_channel(force_refresh=path == "/v1/internal/tick")
            if unavailable is not None:
                return unavailable
        return await call_next(request)

    # Keep the exported object a FastAPI instance for Vercel's framework detector.
    # Guard is installed outermost, so unauthenticated requests cannot trigger a
    # Telegram scan before the API's normal request checks run.
    api.add_middleware(Guard, core=core)
    return api


# Vercel discovers the ASGI app through this module-level name.
app = build_app()
