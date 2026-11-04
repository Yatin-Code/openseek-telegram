"""Serverless Vercel entrypoint guards and refresh behavior (F1c)."""
from __future__ import annotations

import asyncio
import importlib

import httpx
import pytest
from fastapi import FastAPI

from openseek.app import Core
from openseek.auth import DeviceAuth
from openseek.config import Settings
from openseek.pairing import InviteManager, PairingManager, client_proof, derive_key
from openseek.pool import PoolError
from openseek.remote import RemoteUploadQueueStore, SupabaseClient
from openseek.remote_admin import RemoteAdminStore
from openseek.tls import valid_spki_pin
from test_uploads import FakeSupabase


READ_KEY = "r" * 40
WRITE_KEY = "w" * 40
CRON_SECRET = "c" * 40
ADMIN_TOKEN = "owner-secret-is-long-enough"
PUBLIC_PIN = "sha256//" + "A" * 43 + "="


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
        self.scanned_at = 1
        self.last_error = ""

    def refresh(self, force=False):
        self.refreshes.append(force)
        return {"scanned": 0}

    def status(self):
        return {"scanned_at": self.scanned_at}

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
        admin_token=ADMIN_TOKEN, public_tls_pin=PUBLIC_PIN,
        supabase_url="https://project.supabase.co", supabase_service_key="service-key",
        telegram_channel_id="-100123456", telegram_bot_token="123456:test-token",
        telegram_api_id=1, telegram_api_hash="api-hash",
        pool_read_key=READ_KEY, pool_write_key=WRITE_KEY, cron_secret=CRON_SECRET)


def request(app, method, path, *, token=None, owner=None, json=None, extra_headers=None):
    async def go():
        transport = httpx.ASGITransport(app=app, client=("8.8.8.8", 40000))
        async with httpx.AsyncClient(transport=transport, base_url="https://pool.test") as client:
            headers = {"Authorization": "Bearer " + token} if token else {}
            if owner:
                headers["X-OpenSeek-Admin"] = owner
            headers.update(extra_headers or {})
            return await client.request(method, path, headers=headers, json=json)
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
    monkeypatch.setenv("OPENSEEK_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("OPENSEEK_PUBLIC_TLS_PIN", PUBLIC_PIN)
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


def test_vercel_owner_pairing_uses_durable_state_and_separate_owner_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    from openseek.vercel import build_app

    cfg = settings(tmp_path)
    core = Core(cfg, proxy=NoSourceProxy())
    fake = FakeSupabase()
    client = SupabaseClient(cfg.supabase_url, cfg.supabase_service_key,
                            transport=httpx.MockTransport(fake))
    core.supabase.close()
    core.supabase = client
    store = RemoteAdminStore(client)
    core.state_store = store
    core.auth = DeviceAuth(core.db, store=store)
    core.library.settings_store = store
    core.uploads.store = RemoteUploadQueueStore(client)
    core.invites = InviteManager(store, core.spki_pin, core.auth.register, iterations=200_000)
    core.pairing = PairingManager(core.spki_pin, core.auth.register, invites=core.invites)
    core.pairing.sharing_enabled = core.sharing_enabled
    app = build_app(cfg, core=core)
    try:
        assert core.spki_pin == PUBLIC_PIN and valid_spki_pin(core.spki_pin)
        dashboard = request(app, "GET", "/")
        assert dashboard.status_code == 200
        assert "const PROFILE='vercel'" in dashboard.text
        assert "OPENSEEK_ADMIN_TOKEN" in dashboard.text
        assert "const PAIR_KEY='oskPairing'" in dashboard.text
        core.library.set_enabled(True)
        assert store.get_setting("metadata_index") == "1"
        core.library.set_enabled(False)
        assert request(app, "GET", "/admin/status").status_code == 403
        assert request(app, "GET", "/admin/status", token="x" * 43).status_code == 403
        assert request(app, "GET", "/admin/status", owner=ADMIN_TOKEN,
                       extra_headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 200
        assert request(app, "GET", "/admin/status", owner=ADMIN_TOKEN,
                       extra_headers={"Origin": "https://other.example"}).status_code == 403

        opened = request(app, "POST", "/admin/pair", owner=ADMIN_TOKEN)
        assert opened.status_code == 200, opened.text
        invitation = opened.json()
        duplicate = request(app, "POST", "/admin/pair", owner=ADMIN_TOKEN)
        assert duplicate.status_code == 409
        assert duplicate.json()["error"] == "pairing_already_open"
        assert invitation["spki_pin"] == PUBLIC_PIN
        assert "pool.test" in invitation["join"]
        state = request(app, "POST", "/v1/pair/start", json={"invite": invitation["invite_id"]})
        assert state.status_code == 200, state.text
        assert state.json()["spki_pin"] == PUBLIC_PIN

        code = invitation["code"].replace("-", "")[-12:]
        k = derive_key(code, state.json()["pair_id"], state.json()["salt"],
                       state.json()["spki_pin"], state.json()["iterations"])
        name = "Living room TV"
        claimed = request(app, "POST", "/v1/pair/claim", json={
            "pair_id": state.json()["pair_id"], "device_name": name,
            "client_proof": client_proof(k, state.json()["pair_id"], name),
        })
        assert claimed.status_code == 200, claimed.text
        device_token = claimed.json()["token"]
        assert request(app, "GET", "/v1/pair/status", token=device_token).status_code == 200
        assert request(app, "GET", "/admin/status", token=device_token).status_code == 403
        admin_status = request(app, "GET", "/admin/status", owner=ADMIN_TOKEN)
        assert admin_status.status_code == 200
        assert admin_status.json()["devices"][0]["role"] == "owner"
        assert admin_status.json()["pairing"]["open"] is False

        reopened = request(app, "POST", "/admin/pair", owner=ADMIN_TOKEN)
        assert reopened.status_code == 200
        assert request(app, "GET", "/admin/status", owner=ADMIN_TOKEN).json()["pairing"]["open"] is True
        assert request(app, "DELETE", "/admin/pair", owner=ADMIN_TOKEN).status_code == 204
        assert request(app, "GET", "/admin/status", owner=ADMIN_TOKEN).json()["pairing"]["open"] is False
    finally:
        core.stop()


def test_vercel_pool_status_counts_remote_pending_uploads(tmp_path, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    from openseek.vercel import build_app

    cfg = settings(tmp_path)
    core = Core(cfg, proxy=NoSourceProxy())
    fake = FakeSupabase()
    client = SupabaseClient(cfg.supabase_url, cfg.supabase_service_key,
                            transport=httpx.MockTransport(fake))
    core.supabase.close()
    core.supabase = client
    core.uploads.store = RemoteUploadQueueStore(client)
    core.channel_store = ChannelStore()
    core.channel_index = ChannelIndex()
    fake.tables["pool_uploads"].extend([
        {"upload_id": "pending", "state": "pending"},
        {"upload_id": "publishing", "state": "publishing"},
        {"upload_id": "accepted", "state": "accepted"},
    ])
    app = build_app(cfg, core=core)
    try:
        response = request(app, "GET", "/admin/pool/status", owner=ADMIN_TOKEN)
        assert response.status_code == 200, response.text
        assert response.json()["outbox"] == 2
    finally:
        core.stop()


def test_vercel_dashboard_reports_telegram_permission_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    from openseek.vercel import build_app

    class DeniedChannelStore(ChannelStore):
        def start(self):
            raise PoolError(403, "telegram_access_denied")

    cfg = settings(tmp_path)
    core = Core(cfg, proxy=NoSourceProxy())
    core.channel_store = DeniedChannelStore()
    core.channel_index = ChannelIndex()
    core.channel_index.scanned_at = 0
    app = build_app(cfg, core=core)
    try:
        response = request(app, "GET", "/admin/pool/status", owner=ADMIN_TOKEN)
        assert response.status_code == 503
        assert response.json() == {"error": "pool_index_unavailable", "reason": "telegram_access_denied"}
    finally:
        core.stop()
