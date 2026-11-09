"""Serverless Vercel entrypoint guards and refresh behavior (F1c)."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from openseek.app import Core, create_api_app
from openseek.auth import DeviceAuth
from openseek.cache import PROFILE
from openseek.config import Settings
from openseek.pairing import InviteManager, PairingManager, client_proof, derive_key
from openseek.pool import PoolError
from openseek.remote import (RemoteChannelStore, RemotePairingStore,
                             RemoteTelegramSessionLease, RemoteUploadQueueStore,
                             SupabaseClient)
from openseek.remote_admin import RemoteAdminStore
from openseek.tls import valid_spki_pin
from test_admin_catalog import MemoryChannel, jpeg
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

    def package(self, fp, profile):
        data = b"x" * 4_500_001
        return data, {"duration_ms": 20_000, "n_tiles": 2, "profile": profile,
                      "sha256": "0" * 64}


class Uploads:
    def drain(self, limit=1):
        return {"processed": 0, "accepted": 0, "failed": 0}


class SessionLease:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.acquired_by = []
        self.released_by = []

    def acquire(self, owner):
        self.acquired_by.append(owner)
        return self.acquired

    def release(self, owner):
        self.released_by.append(owner)
        return True


class CountingMemoryChannel(MemoryChannel):
    """MemoryChannel external transport that records cold-start lifecycle."""

    def __init__(self):
        super().__init__()
        self.starts = 0
        self.closes = 0
        self.scan_calls = []

    def start(self):
        self.starts += 1

    def close(self):
        self.closes += 1

    def scan(self, min_id=0, limit=2000):
        self.scan_calls.append((min_id, limit))
        return super().scan(min_id, limit)


def library_core(tmp_path, *, acquired=True):
    """Real Core + real ChannelIndex/ChannelPoolService/RemoteChannelStore.

    Fakes only the external boundaries: MemoryChannel for Telegram,
    SupabaseClient transport (MockTransport) for PostgREST, and the
    SessionLease boundary. No production hooks added for tests.
    """
    from openseek.channel_index import ChannelIndex as RealChannelIndex
    from openseek.channel_pool import ChannelPoolService

    cfg = settings(tmp_path)
    memory = CountingMemoryChannel()
    # pool_storage wires Core's channel pool to the fake Telegram transport;
    # profile=vercel makes Core build the real RemoteChannelStore path.
    core = Core(cfg, proxy=NoSourceProxy(), pool_storage=memory)

    fp = "a" * 64
    data = jpeg()
    digest = hashlib.sha256(data).hexdigest()
    memory.data[1] = data
    hint = {"fp": fp, "profile": "nvmt-v1-g10", "imdb": "tt1234567",
            "season": 1, "episode": 2, "duration_ms": 20_000,
            "size_bytes": 1_000_000, "filename_norm": "film.2024",
            "n_tiles": 2, "grid": 10_000, "created": 10.0}
    package = {"fp": fp, "profile": PROFILE, "duration_ms": 20_000,
               "channel_tiles": 2}
    tile = {"fp": fp, "profile": PROFILE, "k": 0, "pts_ms": 0,
            "duration_ms": 20_000, "n_tiles": 2, "size": len(data),
            "sha256": digest, "message_id": 1, "created": 10.0}
    payloads = {"/rest/v1/pool_hints": [hint],
                "/rest/v1/pool_packages": [package],
                "/rest/v1/pool_channel_tiles": [tile]}

    def transport(request: httpx.Request) -> httpx.Response:
        rows = payloads.get(request.url.path, [])
        return httpx.Response(200, json=rows, request=request)

    client = SupabaseClient(cfg.supabase_url, cfg.supabase_service_key,
                            transport=httpx.MockTransport(transport))
    core.supabase.close()
    core.supabase = client
    remote_store = RemoteChannelStore(client)
    # Real ChannelIndex + ChannelPoolService over the real RemoteChannelStore.
    core.channel_index = RealChannelIndex(remote_store, memory, db=None,
                                          channel_id=cfg.telegram_channel_id)
    core.pool = ChannelPoolService(core.channel_index, cfg.pool_read_key,
                                   cfg.pool_write_key)
    # Keep other Supabase-backed stores on the same mocked transport so no
    # real network is touched if touched.
    core.state_store = RemoteAdminStore(client)
    if core.uploads is not None and hasattr(core.uploads, "store"):
        try:
            core.uploads.store = RemoteUploadQueueStore(client)
        except Exception:
            pass
    core.telegram_session_lease = SessionLease(acquired=acquired)
    return cfg, core, fp, memory, data


def settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "test-data", profile="vercel", pool_mode="channel",
        admin_token=ADMIN_TOKEN, public_tls_pin=PUBLIC_PIN,
        supabase_url="https://project.supabase.co", supabase_service_key="service-key",
        telegram_channel_id="-100123456", telegram_session="1abc",
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
    monkeypatch.setenv("OPENSEEK_TELEGRAM_SESSION", "1abc")
    monkeypatch.setenv("OPENSEEK_TELEGRAM_API_ID", "1")
    monkeypatch.setenv("OPENSEEK_TELEGRAM_API_HASH", "api-hash")
    monkeypatch.setenv("OPENSEEK_SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("OPENSEEK_SUPABASE_SERVICE_KEY", "service-key")
    monkeypatch.setenv("OPENSEEK_ADMIN_TOKEN", ADMIN_TOKEN)
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
    core.telegram_session_lease = SessionLease()

    from openseek.vercel import build_app
    app = build_app(cfg, core=core)
    try:
        denied = request(app, "GET", "/v1/pool/candidates?imdb=tt1234567&duration_ms=20000")
        assert denied.status_code == 401
        assert denied.json() == {"error": "unauthorized"}
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
        assert len(core.telegram_session_lease.acquired_by) == 2
        assert core.telegram_session_lease.acquired_by == core.telegram_session_lease.released_by
    finally:
        core.stop()


def test_vercel_refuses_a_second_primary_session_while_a_lease_is_held(tmp_path):
    cfg = settings(tmp_path)
    core = Core(cfg, proxy=NoSourceProxy())
    store = ChannelStore()
    core.channel_store = store
    core.channel_index = ChannelIndex()
    core.pool = Pool()
    core.telegram_session_lease = SessionLease(acquired=False)
    from openseek.vercel import build_app
    app = build_app(cfg, core=core)
    try:
        response = request(app, "GET", "/v1/pool/candidates?imdb=tt1234567&duration_ms=20000",
                           token=READ_KEY)
        assert response.status_code == 503
        assert response.json()["error"] == "telegram_session_busy"
        assert store.starts == 0
        assert core.telegram_session_lease.released_by == []
    finally:
        core.stop()


def test_telegram_session_lease_outlives_the_configured_function_deadline():
    config_path = Path(__file__).parents[1] / "vercel.json"
    max_duration = json.loads(config_path.read_text())["functions"]["openseek/vercel.py"]["maxDuration"]
    lease = RemoteTelegramSessionLease(client=None)

    assert lease.lease_seconds >= max_duration * 2


def test_vercel_rejects_nvmt_response_over_platform_body_limit(tmp_path):
    cfg = settings(tmp_path)
    core = Core(cfg, proxy=NoSourceProxy())
    core.channel_store = ChannelStore()
    core.channel_index = ChannelIndex()
    core.pool = Pool()
    core.telegram_session_lease = SessionLease()
    from openseek.vercel import build_app
    app = build_app(cfg, core=core)
    try:
        response = request(app, "GET", f"/v1/pool/{'a' * 64}/nvmt?profile=nvmt-v1-g10",
                           token=READ_KEY)
        assert response.status_code == 413
        assert response.json() == {"error": "package_chunk_too_large"}
    finally:
        core.stop()


def test_participant_setup_deployment_has_only_a_public_health_route(tmp_path):
    from openseek.vercel import build_app

    cfg = Settings(data_dir=tmp_path / "pending", profile="vercel", pool_mode="channel",
                   setup_pending=True, supabase_url="https://project.supabase.co",
                   supabase_service_key="secret")
    app = build_app(cfg)
    health = request(app, "GET", "/v1/health")
    missing_pool = request(app, "GET", "/v1/pool/candidates")
    assert health.status_code == 200
    assert health.json() == {"service": "openseek", "api": "v1", "setup_pending": True}
    assert missing_pool.status_code == 404


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
    core.state_store = RemoteAdminStore(client)
    pair_store = RemotePairingStore(client)
    core.pairing_store = pair_store
    core.auth = DeviceAuth(core.db, store=pair_store)
    core.uploads.store = RemoteUploadQueueStore(client)
    core.invites = InviteManager(pair_store, core.spki_pin, core.auth.register, iterations=200_000)
    core.pairing = PairingManager(core.spki_pin, core.auth.register, invites=core.invites)
    core.pairing.sharing_enabled = core.sharing_enabled
    core.telegram_session_lease = None
    app = build_app(cfg, core=core)
    try:
        assert core.spki_pin == PUBLIC_PIN and valid_spki_pin(core.spki_pin)
        dashboard = request(app, "GET", "/")
        assert dashboard.status_code == 200
        assert "const PROFILE='vercel'" in dashboard.text
        assert "OPENSEEK_ADMIN_TOKEN" in dashboard.text
        assert "const PAIR_KEY='oskPairing'" in dashboard.text
        route_paths = {route.path for route in create_api_app(core).app.routes}
        assert not any(path.startswith(("/v1/previews", "/v1/lookup", "/v1/c/")) for path in route_paths)
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
        key = derive_key(code, state.json()["pair_id"], state.json()["salt"],
                         state.json()["spki_pin"], state.json()["iterations"])
        name = "Living room TV"
        claimed = request(app, "POST", "/v1/pair/claim", json={
            "pair_id": state.json()["pair_id"], "device_name": name,
            "client_proof": client_proof(key, state.json()["pair_id"], name),
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
    core.telegram_session_lease = SessionLease()
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
    core.telegram_session_lease = SessionLease()
    app = build_app(cfg, core=core)
    try:
        response = request(app, "GET", "/admin/pool/status", owner=ADMIN_TOKEN)
        assert response.status_code == 503
        assert response.json() == {"error": "pool_index_unavailable", "reason": "telegram_access_denied"}
    finally:
        core.stop()


def test_vercel_library_list_refreshes_under_lease_and_requires_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    from openseek.vercel import build_app

    cfg, core, fp, memory, _data = library_core(tmp_path)
    app = build_app(cfg, core=core)
    try:
        denied = request(app, "GET", "/admin/library?limit=24&offset=0")
        assert denied.status_code == 403, denied.text
        assert memory.starts == 0
        assert memory.scan_calls == []
        assert core.telegram_session_lease.acquired_by == []

        wrong = request(app, "GET", "/admin/library?limit=24&offset=0", owner="wrong-owner-secret")
        assert wrong.status_code == 403
        assert memory.starts == 0
        assert memory.scan_calls == []
        assert core.telegram_session_lease.acquired_by == []

        allowed = request(app, "GET", "/admin/library?limit=24&offset=0", owner=ADMIN_TOKEN)
        assert allowed.status_code == 200, allowed.text
        body = allowed.json()
        assert body["supported"] is True
        assert body["has_more"] is False
        assert body["items"] == [{"fp": fp, "profile": "nvmt-v1-g10", "filename": "film.2024",
                                  "imdb": "tt1234567", "season": 1, "episode": 2,
                                  "duration_ms": 20_000, "n_tiles": 2, "created": 10.0,
                                  "thumbnail_available": True}]
        # Cold-start lease: connect, bounded refresh scan, disconnect, release.
        assert memory.starts == 1
        assert len(memory.scan_calls) == 1
        assert memory.closes == 1
        assert len(core.telegram_session_lease.acquired_by) == 1
        assert core.telegram_session_lease.acquired_by == core.telegram_session_lease.released_by

        # A second authorized request cold-starts again after the disconnect.
        again = request(app, "GET", "/admin/library?limit=24&offset=0", owner=ADMIN_TOKEN)
        assert again.status_code == 200, again.text
        assert memory.starts == 2
        assert len(memory.scan_calls) == 2
        assert len(core.telegram_session_lease.acquired_by) == 2
        assert core.telegram_session_lease.acquired_by == core.telegram_session_lease.released_by
    finally:
        core.stop()


def test_vercel_library_thumbnail_refreshes_under_lease_and_requires_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    from openseek.vercel import build_app

    cfg, core, fp, memory, data = library_core(tmp_path)
    app = build_app(cfg, core=core)
    thumb_path = f"/admin/library/{fp}/thumbnail?profile=nvmt-v1-g10"
    try:
        denied = request(app, "GET", thumb_path)
        assert denied.status_code == 403, denied.text
        assert memory.starts == 0
        assert memory.scan_calls == []
        assert core.telegram_session_lease.acquired_by == []

        allowed = request(app, "GET", thumb_path, owner=ADMIN_TOKEN)
        assert allowed.status_code == 200, allowed.text
        assert allowed.content == data
        assert allowed.headers["content-type"] == "image/jpeg"
        assert "private" in allowed.headers["cache-control"]
        assert "no-store" in allowed.headers["cache-control"]
        assert memory.starts == 1
        assert len(memory.scan_calls) == 1
        assert memory.closes == 1
        assert len(core.telegram_session_lease.acquired_by) == 1
        assert core.telegram_session_lease.acquired_by == core.telegram_session_lease.released_by
    finally:
        core.stop()


def test_vercel_library_lease_contention_returns_existing_503(tmp_path, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    from openseek.vercel import build_app

    cfg, core, fp, memory, _data = library_core(tmp_path, acquired=False)
    app = build_app(cfg, core=core)
    try:
        listed = request(app, "GET", "/admin/library?limit=24&offset=0", owner=ADMIN_TOKEN)
        assert listed.status_code == 503, listed.text
        assert listed.json() == {"error": "telegram_session_busy"}
        assert memory.starts == 0
        assert memory.scan_calls == []

        thumb = request(app, "GET", f"/admin/library/{fp}/thumbnail?profile=nvmt-v1-g10",
                        owner=ADMIN_TOKEN)
        assert thumb.status_code == 503, thumb.text
        assert thumb.json() == {"error": "telegram_session_busy"}
        assert memory.starts == 0
        assert memory.scan_calls == []
        assert core.telegram_session_lease.released_by == []
    finally:
        core.stop()


def test_vercel_library_classification_stays_narrow(tmp_path, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    from openseek.vercel import build_app

    cfg, core, fp, memory, _data = library_core(tmp_path)
    app = build_app(cfg, core=core)
    try:
        setup = request(app, "GET", "/admin/setup", owner=ADMIN_TOKEN)
        assert setup.status_code == 200, setup.text
        assert setup.json()["profile"] == "vercel"
        assert memory.starts == 0
        assert memory.scan_calls == []
        assert core.telegram_session_lease.acquired_by == []

        # Contributor moderation reuses local SQLite and must not touch Telegram
        # on the Vercel profile (honestly unsupported, not lease-gated).
        contributors = request(app, "GET", "/admin/contributors", owner=ADMIN_TOKEN)
        assert contributors.status_code == 200, contributors.text
        assert contributors.json()["supported"] is False
        assert memory.starts == 0
        assert memory.scan_calls == []

        # A near-miss prefix without the slash boundary is not a library route.
        near_miss = request(app, "GET", "/admin/libraryfoo", owner=ADMIN_TOKEN)
        assert near_miss.status_code == 404, near_miss.text
        assert memory.starts == 0
        assert memory.scan_calls == []
        assert core.telegram_session_lease.acquired_by == []
    finally:
        core.stop()


def test_vercel_setup_only_page_uses_dashboard_theme_and_preserves_guidance(tmp_path, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    from openseek.vercel import _setup_only_app

    message = 'config <script>alert("x")</script> & "bad"'
    app = _setup_only_app(message)
    try:
        page = request(app, "GET", "/")
        assert page.status_code == 200, page.text
        assert page.headers["cache-control"] == "no-store"
        assert page.headers["content-security-policy"] == (
            "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'")
        text = page.text
        # Original dark palette tokens, one value each.
        for token in ("#0d0e12", "#13151b", "#181b23", "#252a36",
                      "#e9eaf0", "#8b91a1", "#8b7bff", "#5ad1c1"):
            assert token in text, token
        assert "width=device-width" in text
        # Long env names / SPKI pin must not overflow at 390px.
        assert "overflow-wrap:anywhere" in text
        assert "Set up OpenSeek on Vercel" in text
        assert "Set these Production environment variables, then redeploy:" in text
        for guidance in ("OPENSEEK_PROFILE=vercel", "OPENSEEK_POOL_MODE=channel",
                         "OPENSEEK_ADMIN_TOKEN (generated by the setup wizard)",
                         "OPENSEEK_PUBLIC_TLS_PIN (optional deployed HTTPS certificate SPKI pin)",
                         "Telegram user-session, channel, and API settings",
                         "OPENSEEK_SUPABASE_URL and OPENSEEK_SUPABASE_SERVICE_KEY",
                         "Configuration check:",
                         "After redeploying, reopen this page to reach the OpenSeek dashboard."):
            assert guidance in text, guidance
        # Escaped config error, never raw markup.
        assert "<script>alert" not in text
        assert "&lt;script&gt;" in text
        # Setup page is guidance only: no credentials fields or runtime controls
        # and no external assets.
        lowered = text.lower()
        assert "<input" not in lowered
        assert "<form" not in lowered
        assert "<button" not in lowered
        assert "<link" not in lowered
        assert "src=" not in lowered
        assert "href=" not in lowered
        assert "https://" not in lowered
        assert "http://" not in lowered
        assert "cdn" not in lowered

        status = request(app, "GET", "/admin/setup")
        assert status.status_code == 200, status.text
        assert status.json() == {"setup_required": True, "error": message}

        unavailable = request(app, "GET", "/admin/library")
        assert unavailable.status_code == 503, unavailable.text
        assert unavailable.json() == {"error": "setup_required"}
        assert unavailable.headers["cache-control"] == "no-store"
    finally:
        pass
