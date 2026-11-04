"""Serverless Vercel entrypoint guards and refresh behavior (F1c)."""
from __future__ import annotations

import asyncio
import importlib

import httpx
import pytest
from fastapi import FastAPI

from openseek.app import Core
from openseek.config import Settings


READ_KEY = "r" * 40
WRITE_KEY = "w" * 40
CRON_SECRET = "c" * 40


class NoSourceProxy:
    def stop(self):
        pass


class ChannelStore:
    def __init__(self):
        self.starts = 0

    def start(self):
        self.starts += 1

    def close(self):
        pass


class ChannelIndex:
    def __init__(self):
        self.refreshes = []

    def refresh(self, force=False):
        self.refreshes.append(force)
        return {"scanned": 0}

    def status(self):
        return {"scanned_at": 1}

    def stop(self):
        pass


class Pool:
    read_key = READ_KEY
    write_key = WRITE_KEY

    def authorize(self, token, write=False):
        return token == (WRITE_KEY if write else READ_KEY)

    def candidates(self, **kwargs):
        return []


class Uploads:
    def drain(self, limit=1):
        return {"processed": 0, "accepted": 0, "failed": 0}


def settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "test-data", profile="vercel", pool_mode="channel",
        supabase_url="https://project.supabase.co", supabase_service_key="service-key",
        telegram_channel_id="-100123456", telegram_bot_token="123456:test-token",
        telegram_api_id=1, telegram_api_hash="api-hash",
        pool_read_key=READ_KEY, pool_write_key=WRITE_KEY, cron_secret=CRON_SECRET)


def request(app, method, path, *, token=None):
    async def go():
        transport = httpx.ASGITransport(app=app, client=("8.8.8.8", 40000))
        async with httpx.AsyncClient(transport=transport, base_url="https://pool.test") as client:
            headers = {"Authorization": "Bearer " + token} if token else {}
            return await client.request(method, path, headers=headers)
    return asyncio.run(go())


def test_vercel_entrypoint_is_fastapi_and_does_not_start_background_workers(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENSEEK_PROFILE", "vercel")
    monkeypatch.setenv("OPENSEEK_POOL_MODE", "channel")
    monkeypatch.setenv("OPENSEEK_TELEGRAM_CHANNEL_ID", "-100123456")
    monkeypatch.setenv("OPENSEEK_TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("OPENSEEK_TELEGRAM_API_ID", "1")
    monkeypatch.setenv("OPENSEEK_TELEGRAM_API_HASH", "api-hash")
    monkeypatch.setenv("OPENSEEK_SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("OPENSEEK_SUPABASE_SERVICE_KEY", "service-key")
    monkeypatch.setenv("OPENSEEK_DATA_DIR", str(tmp_path / "module-data"))

    module = importlib.import_module("openseek.vercel")
    try:
        assert isinstance(module.app, FastAPI)
        response = request(module.app, "GET", "/v1/health")
        assert response.status_code == 200
        assert module.runtime_core.channel_store._thread is None
        assert not module.runtime_core.jobs._reaper.is_alive()
    finally:
        module.runtime_core.stop()


def test_vercel_refresh_runs_only_after_a_valid_pool_or_cron_key(tmp_path):
    cfg = settings(tmp_path)
    core = Core(cfg, proxy=NoSourceProxy())
    store = ChannelStore()
    index = ChannelIndex()
    core.channel_store = store
    core.channel_index = index
    core.pool = Pool()
    core.uploads = Uploads()

    from openseek.vercel import build_app
    app = build_app(cfg, core=core)
    try:
        denied = request(app, "GET", "/v1/pool/candidates?imdb=tt1234567&duration_ms=20000")
        assert denied.status_code == 403
        assert index.refreshes == [] and store.starts == 0

        allowed = request(app, "GET", "/v1/pool/candidates?imdb=tt1234567&duration_ms=20000",
                          token=READ_KEY)
        assert allowed.status_code == 200
        assert index.refreshes == [False] and store.starts == 1

        unauthorized_tick = request(app, "GET", "/v1/internal/tick")
        assert unauthorized_tick.status_code == 401
        assert index.refreshes == [False]

        tick = request(app, "GET", "/v1/internal/tick", token=CRON_SECRET)
        assert tick.status_code == 200
        assert index.refreshes == [False, True]
    finally:
        core.stop()
