"""Vercel staged-upload lifecycle and cron-drained publication queue (F1b).

Seam: the public `/v1/pool/uploads*` + `/v1/internal/tick` routes, plus the
`UploadService`/`PublicationWorker` contract they delegate to. Supabase is a real
`SupabaseClient` over `httpx.MockTransport`, so PostgREST/Storage request shapes are
exercised without a network. Telegram publication goes through the existing
`MemoryChannel` fake and the real `ChannelPoolService`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

import openseek.app as app_module
from openseek.app import Core, create_api_app
from openseek.auth import DeviceAuth
from openseek.channel_index import ChannelIndex
from openseek.channel_pool import ChannelPoolService
from openseek.config import Settings
from openseek.db import DB
from openseek.pairing import InviteManager, client_proof
from openseek.pool import MAX_DURATION_MS, MAX_NVMT_BYTES, PoolError, decode_nvmt, encode_nvmt
from openseek.remote import RemoteStoreError, RemoteUploadQueueStore, SupabaseClient
from openseek.remote_admin import RemoteAdminStore
from openseek.store import ChannelStore, UploadQueueStore
from openseek.uploads import UploadService
from test_pool_channel import MemoryChannel, jpeg

READ_KEY = "r" * 40
WRITE_KEY = "w" * 40
CRON_SECRET = "c" * 40
SUPABASE_URL = "https://project.supabase.co"
SERVICE_KEY = "service-role-key-do-not-leak"


# --------------------------------------------------------------------------- Supabase fake
class FakeSupabase:
    """PostgREST tables + Storage objects behind httpx.MockTransport."""

    def __init__(self):
        self.tables = {"pool_uploads": [], "pool_publication_queue": [], "pool_devices": [],
                       "pool_invites": [], "pool_owner_settings": [], "pool_generation_jobs": []}
        self.objects: dict[str, bytes] = {}
        self.sign_calls: list[tuple[str, int]] = []
        self.deleted_objects: list[str] = []
        self.requests: list[tuple[str, str]] = []
        self.delete_failures = 0

    # -- helpers
    def add_object(self, path: str, data: bytes) -> None:
        self.objects[path] = data

    def rows(self, table: str) -> list[dict]:
        return self.tables[table]

    @staticmethod
    def _filter(rows: list[dict], params: list[tuple[str, str]]) -> list[dict]:
        out = rows
        for key, value in params:
            if key in ("select", "order", "limit", "on_conflict", "offset"):
                continue
            if value.startswith("eq."):
                wanted = value[3:]
                out = [r for r in out if (str(r.get(key)).lower() == wanted.lower()
                                          if isinstance(r.get(key), bool) else str(r.get(key)) == wanted)]
            elif value.startswith("in.(") and value.endswith(")"):
                wanted = {item.strip().strip('"') for item in value[4:-1].split(",")}
                out = [r for r in out if str(r.get(key)) in wanted]
            elif value.startswith("neq."):
                wanted = value[4:]
                out = [r for r in out if str(r.get(key)) != wanted]
            elif value == "is.null":
                out = [r for r in out if r.get(key) is None]
            elif value.startswith("lt."):
                wanted = float(value[3:])
                out = [r for r in out if r.get(key) is not None and float(r[key]) < wanted]
            elif value.startswith("lte."):
                wanted = float(value[4:])
                out = [r for r in out if r.get(key) is not None and float(r[key]) <= wanted]
            elif value.startswith("gte."):
                wanted = float(value[4:])
                out = [r for r in out if r.get(key) is not None and float(r[key]) >= wanted]
            elif value.startswith("gt."):
                wanted = float(value[3:])
                out = [r for r in out if r.get(key) is not None and float(r[key]) > wanted]
            else:
                raise AssertionError(f"fake does not support filter {key}={value}")
        return out

    def _ordered(self, rows: list[dict], params: list[tuple[str, str]]) -> list[dict]:
        for key, value in params:
            if key == "order":
                column, _, direction = value.partition(".")
                rows = sorted(rows, key=lambda r: r.get(column, 0),
                              reverse=direction.lower().startswith("desc"))
        for key, value in params:
            if key == "limit":
                rows = rows[:int(value)]
        return rows

    def _table(self, path: str) -> str:
        name = path.rsplit("/", 1)[-1]
        assert name in self.tables, name
        return name

    # -- ASGI transport handler
    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        path = request.url.path
        params = list(request.url.params.multi_items())
        if path == "/rest/v1/rpc/enqueue_pool_publication" and request.method == "POST":
            args = json.loads(request.content or b"{}")
            upload_id = args["p_upload_id"]
            now = float(args["p_now"])
            row = next((r for r in self.tables["pool_uploads"]
                        if r["upload_id"] == upload_id), None)
            if row is None or row["state"] != "pending" or float(row["expires"]) <= now:
                return httpx.Response(200, json=False)
            row.update({"state": "queued", "updated": now, "error": None})
            self.tables["pool_publication_queue"].append({
                "upload_id": upload_id, "fp": row["fp"], "profile": row["profile"],
                "attempts": 0, "next_attempt": now, "state": "queued",
                "created": now, "updated": now, "error": None})
            return httpx.Response(200, json=True)
        if path == "/rest/v1/rpc/record_pool_invite_failure" and request.method == "POST":
            pair_id = json.loads(request.content or b"{}")["p_pair_id"]
            row = next((r for r in self.tables["pool_invites"] if r["pair_id"] == pair_id), None)
            changed = bool(row is not None and row["attempts"] < 5)
            if changed:
                row["attempts"] += 1
            return httpx.Response(200, json=changed)
        if path.startswith("/rest/v1/"):
            table = self._table(path)
            rows = self.tables[table]
            if request.method == "GET":
                matched = self._filter(rows, params)
                headers = {}
                if "count=exact" in request.headers.get("prefer", ""):
                    headers["content-range"] = f"0-0/{len(matched)}"
                return httpx.Response(200, json=self._ordered(matched, params), headers=headers)
            if request.method == "POST":
                payload = json.loads(request.content or b"[]")
                payload = payload if isinstance(payload, list) else [payload]
                conflict = next((v for k, v in params if k == "on_conflict"), None)
                keys = conflict.split(",") if conflict else []
                prefer = request.headers.get("prefer", "")
                out = []
                for row in payload:
                    existing = next((r for r in rows if keys and all(
                        str(r.get(k)) == str(row.get(k)) for k in keys)), None)
                    if existing is not None and "ignore-duplicates" in prefer:
                        continue
                    if existing is not None:
                        existing.update(row)
                        out.append(existing)
                        continue
                    rows.append(dict(row))
                    out.append(rows[-1])
                return httpx.Response(201, json=out if "representation" in prefer else [])
            if request.method == "PATCH":
                updates = json.loads(request.content or b"{}")
                matched = self._filter(rows, params)
                for row in matched:
                    row.update(updates)
                return httpx.Response(200, json=matched)
            if request.method == "DELETE":
                matched = self._filter(rows, params)
                for row in matched:
                    rows.remove(row)
                return httpx.Response(200, json=matched)
            raise AssertionError(request.method)
        if path.startswith("/storage/v1/object/upload/sign/"):
            rest = path[len("/storage/v1/object/upload/sign/"):]
            bucket, _, object_path = rest.partition("/")
            body = json.loads(request.content or b"{}")
            self.sign_calls.append((object_path, int(body.get("expiresIn", 0))))
            return httpx.Response(200, json={
                "url": f"/object/upload/sign/{bucket}/{object_path}?token=staging-token"})
        if path.startswith("/storage/v1/object/"):
            rest = path[len("/storage/v1/object/"):]
            bucket, _, object_path = rest.partition("/")
            if request.method in ("POST", "PUT"):
                self.objects[object_path] = request.content
                return httpx.Response(200, json={"Key": f"{bucket}/{object_path}"})
            if request.method == "HEAD":
                return httpx.Response(200 if object_path in self.objects else 404)
            if request.method == "GET":
                if object_path not in self.objects:
                    return httpx.Response(404, json={"message": "Object not found"})
                return httpx.Response(200, content=self.objects[object_path])
            if request.method == "DELETE":
                if self.delete_failures:
                    self.delete_failures -= 1
                    return httpx.Response(503, json={"message": "temporary storage outage"})
                self.deleted_objects.append(object_path)
                self.objects.pop(object_path, None)
                return httpx.Response(200, json={"message": "Deleted"})
            raise AssertionError(request.method)
        return httpx.Response(404, json={"message": "not found"})


# --------------------------------------------------------------------------- core fixture
class NoSourceProxy:
    def stop(self):
        pass


class Clock:
    def __init__(self, at: float = 1_800_000_000.0):
        self.t = at

    def __call__(self) -> float:
        return self.t


class RecordingPool:
    read_key = READ_KEY
    write_key = WRITE_KEY

    def __init__(self):
        self.calls: list[dict] = []
        self.fail_with: PoolError | None = None

    def authorize(self, given: str | None, write: bool = False) -> bool:
        wanted = self.write_key if write else self.read_key
        return bool(given and wanted and given == wanted)

    def upload_package(self, fp, profile, duration_ms, data, *args, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append({"fp": fp, "profile": profile, "duration_ms": duration_ms,
                           "data": data, **kwargs})
        return {"stored": True, "duplicate": False}


def make_settings(tmp_path, *, profile="vercel", cron_secret=CRON_SECRET) -> Settings:
    return Settings(
        data_dir=tmp_path / "data", profile=profile, pool_mode="channel",
        supabase_url=SUPABASE_URL, supabase_service_key=SERVICE_KEY,
        telegram_channel_id="-100123456", telegram_bot_token="123456:test-token",
        telegram_api_id=1, telegram_api_hash="test-hash",
        pool_read_key=READ_KEY, pool_write_key=WRITE_KEY, cron_secret=cron_secret)


def make_env(tmp_path, *, profile="vercel", cron_secret=CRON_SECRET, real_channel=False):
    settings = make_settings(tmp_path, profile=profile, cron_secret=cron_secret)
    channel = MemoryChannel()
    core = Core(settings, pool_storage=channel, proxy=NoSourceProxy())
    fake = FakeSupabase()
    client = SupabaseClient(SUPABASE_URL, SERVICE_KEY, transport=httpx.MockTransport(fake))
    if real_channel:
        core.channel_index = ChannelIndex(ChannelStore(core.db), channel, scan_limit=100, refresh_s=10)
        core.pool = ChannelPoolService(core.channel_index, READ_KEY, WRITE_KEY)
        pool = core.pool
    else:
        pool = RecordingPool()
        core.pool = pool
    clock = Clock()
    core.uploads = UploadService(settings, client, pool, RemoteUploadQueueStore(client), clock=clock)
    app = create_api_app(core)
    env = SimpleNamespace(core=core, app=app, fake=fake, client=client, pool=pool,
                          channel=channel, clock=clock)
    return env


def make_local_env(tmp_path):
    """Local/VPS host profile: direct PUT only, no staging service."""
    settings = Settings(data_dir=tmp_path / "local", pool_enabled=True,
                        pool_read_key=READ_KEY, pool_write_key=WRITE_KEY,
                        telegram_bot_token="123456:test-token", telegram_channel_id="-100123456")
    core = Core(settings, pool_storage=MemoryChannel(), proxy=NoSourceProxy())
    return SimpleNamespace(core=core, app=create_api_app(core), fake=None, clock=Clock())


@pytest.fixture(autouse=True)
def _inline_threads(monkeypatch):
    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(app_module.asyncio, "to_thread", inline)


def call(env, method: str, path: str, *, body=None, token=WRITE_KEY, params=None,
         headers=None, client_addr=("127.0.0.1", 40000)) -> httpx.Response:
    async def go():
        transport = httpx.ASGITransport(app=env.app, client=client_addr)
        async with httpx.AsyncClient(transport=transport, base_url="https://pool.test") as http_client:
            request_headers = dict(headers or {})
            if token:
                request_headers["Authorization"] = "Bearer " + token
            return await http_client.request(method, path, json=body, headers=request_headers, params=params)
    return asyncio.run(go())


def upload_body(fp: str, *, profile="nvmt-v1-g5", duration_ms=20_000, size_bytes=1234,
                imdb="tt1234567", season=0, episode=0, filename="movie.2024.1080p.mkv") -> dict:
    return {"fp": fp, "profile": profile, "duration_ms": duration_ms,
            "size_bytes": size_bytes, "imdb": imdb, "season": season, "episode": episode,
            "filename": filename}


# --------------------------------------------------------------------------- slice 1: create
def test_local_profile_refuses_staging_and_writes_no_rows(tmp_path):
    env = make_local_env(tmp_path)
    fp = hashlib.sha256(b"local direct put").hexdigest()
    try:
        response = call(env, "POST", "/v1/pool/uploads", body=upload_body(fp))
        assert response.status_code == 400
        assert response.json() == {"error": "staging_unavailable"}
        assert env.core.uploads is None
        assert env.core.db.q("SELECT COUNT(*) FROM pool_uploads")[0][0] == 0
        assert env.core.db.q("SELECT COUNT(*) FROM pool_publication_queue")[0][0] == 0
    finally:
        env.core.stop()


def test_plain_local_profile_also_reports_staging_unavailable(tmp_path):
    core = Core(Settings(data_dir=tmp_path / "plain-local"), proxy=NoSourceProxy())
    try:
        env = SimpleNamespace(core=core, app=create_api_app(core))
        response = call(env, "POST", "/v1/pool/uploads",
                        body=upload_body(hashlib.sha256(b"plain local").hexdigest()), token=None)
        assert response.status_code == 400
        assert response.json() == {"error": "staging_unavailable"}
    finally:
        core.stop()


def test_vercel_create_returns_absolute_signed_target_and_pending_row(tmp_path):
    env = make_env(tmp_path)
    fp = hashlib.sha256(b"vercel staged upload").hexdigest()
    try:
        response = call(env, "POST", "/v1/pool/uploads", body=upload_body(fp))
        assert response.status_code == 201, response.text
        body = response.json()
        assert sorted(body) == ["expires_at", "max_bytes", "put_url", "upload_id"]
        assert body["max_bytes"] == MAX_NVMT_BYTES
        assert body["expires_at"] == pytest.approx(env.clock.t + 900)
        parsed = urlsplit(body["put_url"])
        assert parsed.scheme == "https" and parsed.netloc == "project.supabase.co"
        assert parsed.path.startswith("/storage/v1/object/upload/sign/")
        assert READ_KEY not in body["put_url"] and WRITE_KEY not in body["put_url"]
        assert env.fake.sign_calls and env.fake.sign_calls[0][1] == 900

        rows = env.fake.rows("pool_uploads")
        assert len(rows) == 1 and rows[0]["upload_id"] == body["upload_id"]
        assert rows[0]["state"] == "pending" and rows[0]["fp"] == fp
        assert rows[0]["profile"] == "nvmt-v1-g5"
        assert rows[0]["filename_norm"] == "movie.2024.1080p"
        assert rows[0]["expires"] == pytest.approx(env.clock.t + 900)
        assert env.fake.rows("pool_publication_queue") == []
    finally:
        env.core.stop()


@pytest.mark.parametrize("changes,code", [
    ({"fp": "z" * 63}, "bad_key"),
    ({"fp": "Z" * 64}, "bad_key"),
    ({"profile": "nvmt-v9"}, "bad_profile"),
    ({"duration_ms": 0}, "bad_hints"),
    ({"duration_ms": MAX_DURATION_MS + 1}, "bad_hints"),
    ({"imdb": "not-an-imdb"}, "bad_hints"),
    ({"season": 10_000}, "bad_hints"),
])
def test_create_validation_never_stages(tmp_path, changes, code):
    env = make_env(tmp_path)
    body = upload_body(hashlib.sha256(b"bad create").hexdigest())
    body.update(changes)
    try:
        response = call(env, "POST", "/v1/pool/uploads", body=body)
        assert response.status_code == 400
        assert response.json() == {"error": code}
        assert env.fake.sign_calls == []
        assert env.fake.rows("pool_uploads") == []
    finally:
        env.core.stop()


def test_create_accepts_source_size_larger_than_nvmt_package_limit(tmp_path):
    env = make_env(tmp_path)
    body = upload_body(hashlib.sha256(b"large source file").hexdigest(),
                       size_bytes=MAX_NVMT_BYTES + 1)
    try:
        response = call(env, "POST", "/v1/pool/uploads", body=body)
        assert response.status_code == 201, response.text
        assert env.fake.rows("pool_uploads")[0]["size_bytes"] == MAX_NVMT_BYTES + 1
    finally:
        env.core.stop()


def test_create_requires_pool_write_credentials(tmp_path):
    env = make_env(tmp_path)
    fp = hashlib.sha256(b"no credentials").hexdigest()
    try:
        response = call(env, "POST", "/v1/pool/uploads", body=upload_body(fp), token=None)
        assert response.status_code == 403
        assert env.fake.rows("pool_uploads") == []
    finally:
        env.core.stop()


# --------------------------------------------------------------------------- slice 2: finalize/status/expiry
def create_upload(env, fp: str | None = None, **changes) -> dict:
    fp = fp or hashlib.sha256(b"finalize slice").hexdigest()
    body = upload_body(fp)
    body.update(changes)
    response = call(env, "POST", "/v1/pool/uploads", body=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_finalize_moves_pending_to_queued_once(tmp_path):
    env = make_env(tmp_path)
    created = create_upload(env)
    upload_id = created["upload_id"]
    try:
        response = call(env, "POST", f"/v1/pool/uploads/{upload_id}/finalize")
        assert response.status_code == 202, response.text
        assert response.json() == {"upload_id": upload_id, "state": "queued"}

        upload = env.fake.rows("pool_uploads")[0]
        queue = env.fake.rows("pool_publication_queue")[0]
        assert upload["state"] == "queued" and queue["state"] == "queued"
        assert queue["upload_id"] == upload_id
        assert queue["attempts"] == 0 and queue["next_attempt"] == env.clock.t

        again = call(env, "POST", f"/v1/pool/uploads/{upload_id}/finalize")
        assert again.status_code == 409
        assert again.json() == {"error": "wrong_state"}
        assert len(env.fake.rows("pool_publication_queue")) == 1

        status = call(env, "GET", f"/v1/pool/uploads/{upload_id}")
        assert status.status_code == 200
        assert status.json() == {"upload_id": upload_id, "state": "queued", "error": None}
        assert status.headers["cache-control"] == "no-store"
    finally:
        env.core.stop()


def test_finalize_losing_ttl_race_returns_expired(tmp_path):
    env = make_env(tmp_path)
    created = create_upload(env)
    upload_id = created["upload_id"]
    store = env.core.uploads.store
    original_enqueue = store.enqueue

    def expire_then_finalize(_upload_id, now):
        env.clock.t += 901
        assert store.expire(upload_id, env.clock.t)
        return original_enqueue(_upload_id, now)

    store.enqueue = expire_then_finalize
    try:
        response = call(env, "POST", f"/v1/pool/uploads/{upload_id}/finalize")
        assert response.status_code == 410
        assert response.json() == {"error": "expired"}
        assert env.fake.rows("pool_uploads")[0]["state"] == "expired"
        assert env.fake.rows("pool_publication_queue") == []
    finally:
        env.core.stop()


def test_finalize_and_status_reject_unknown_upload(tmp_path):
    env = make_env(tmp_path)
    try:
        for method, path in (("POST", "/v1/pool/uploads/not-a-known-upload/finalize"),
                             ("GET", "/v1/pool/uploads/not-a-known-upload")):
            response = call(env, method, path)
            assert response.status_code == 404
            assert response.json() == {"error": "unknown_upload"}
    finally:
        env.core.stop()


def test_expiry_marks_pending_upload_expired_and_refuses_finalize(tmp_path):
    env = make_env(tmp_path)
    created = create_upload(env)
    upload_id = created["upload_id"]
    try:
        env.clock.t += 901
        status = call(env, "GET", f"/v1/pool/uploads/{upload_id}")
        assert status.status_code == 200
        assert status.json() == {"upload_id": upload_id, "state": "expired", "error": "expired"}
        assert env.fake.rows("pool_uploads")[0]["state"] == "expired"

        finalize = call(env, "POST", f"/v1/pool/uploads/{upload_id}/finalize")
        assert finalize.status_code == 410
        assert finalize.json() == {"error": "expired"}
        assert env.fake.rows("pool_publication_queue") == []
    finally:
        env.core.stop()


def test_tick_cleans_expired_staging_and_retries_storage_delete(tmp_path):
    env = make_env(tmp_path)
    created = create_upload(env)
    upload_id = created["upload_id"]
    path = f"uploads/{upload_id}.nvmt"
    env.fake.add_object(path, b"temporary")
    env.fake.delete_failures = 2
    try:
        env.clock.t += 901
        first = call(env, "POST", "/v1/internal/tick", token=CRON_SECRET)
        assert first.status_code == 200
        assert env.fake.rows("pool_uploads")[0]["state"] == "expired"
        assert path in env.fake.objects

        second = call(env, "POST", "/v1/internal/tick", token=CRON_SECRET)
        assert second.status_code == 200
        row = env.fake.rows("pool_uploads")[0]
        assert row["state"] == "expired" and row["object_path"] == ""
        assert path not in env.fake.objects
        assert env.pool.calls == []
    finally:
        env.core.stop()


def test_status_requires_the_creating_device_or_a_write_key(tmp_path):
    env = make_env(tmp_path)
    created = create_upload(env)
    try:
        anonymous = call(env, "GET", f"/v1/pool/uploads/{created['upload_id']}", token=None)
        assert anonymous.status_code == 403
        by_key = call(env, "GET", f"/v1/pool/uploads/{created['upload_id']}")
        assert by_key.status_code == 200
    finally:
        env.core.stop()


def test_forwarded_headers_are_allowed_only_on_the_vercel_profile(tmp_path):
    headers = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}
    vercel = make_env(tmp_path)
    try:
        assert call(vercel, "GET", "/v1/health", token=None, headers=headers).status_code == 200
    finally:
        vercel.core.stop()
    local = make_local_env(tmp_path)
    try:
        response = call(local, "GET", "/v1/health", token=None, headers=headers)
        assert response.status_code == 403
        assert response.json() == {"error": "proxy_refused"}
    finally:
        local.core.stop()


def test_vercel_pool_accepts_public_clients_but_keeps_pool_key_auth(tmp_path):
    env = make_env(tmp_path)
    external = ("8.8.8.8", 40000)
    try:
        allowed = call(env, "POST", "/v1/pool/uploads",
                       body=upload_body(hashlib.sha256(b"public Vercel").hexdigest()),
                       client_addr=external)
        assert allowed.status_code == 201
        denied = call(env, "POST", "/v1/pool/uploads",
                      body=upload_body(hashlib.sha256(b"no key").hexdigest()),
                      token=None, client_addr=external)
        assert denied.status_code == 403
    finally:
        env.core.stop()


# --------------------------------------------------------------------------- slice 3: publication drain
def nvmt_bytes(fp: str, duration_ms: int = 20_000, grid: int = 5_000) -> bytes:
    n_slots = (duration_ms + grid - 1) // grid
    tiles = {slot: (slot * grid, jpeg((slot * 20 + 7, 30, 60))) for slot in range(n_slots)}
    return encode_nvmt(grid, n_slots, tiles)


def prepare_queued(env, fp: str, *, body: dict | None = None) -> str:
    body = body or upload_body(fp)
    response = call(env, "POST", "/v1/pool/uploads", body=body)
    assert response.status_code == 201, response.text
    upload_id = response.json()["upload_id"]
    assert call(env, "POST", f"/v1/pool/uploads/{upload_id}/finalize").status_code == 202
    return upload_id


def test_tick_publishes_to_telegram_then_deletes_the_staged_object(tmp_path):
    env = make_env(tmp_path, real_channel=True)
    fp = hashlib.sha256(b"drain success").hexdigest()
    try:
        upload_id = prepare_queued(env, fp)
        staged = nvmt_bytes(fp)
        env.fake.add_object(f"uploads/{upload_id}.nvmt", staged)

        response = call(env, "POST", "/v1/internal/tick", token=CRON_SECRET)
        assert response.status_code == 200, response.text
        assert response.json() == {"processed": 1, "accepted": 1, "failed": 0}

        upload = env.fake.rows("pool_uploads")[0]
        queue = env.fake.rows("pool_publication_queue")[0]
        assert upload["state"] == "accepted" and upload["error"] is None
        assert queue["state"] == "done"
        assert env.fake.rows("pool_publication_queue")[0]["attempts"] == 0
        assert env.fake.objects == {}
        assert f"uploads/{upload_id}.nvmt" in env.fake.deleted_objects

        # Telegram holds the tiles and manifest, and the hints reached the index.
        package = env.pool.package(fp, "nvmt-v1-g5", duration_ms=20_000)
        assert package is not None
        _grid, _slots, tiles = decode_nvmt(fp, "nvmt-v1-g5", 20_000, package[0])
        assert len(tiles) == 2
        assert env.pool.index.store.get_hints(fp, "nvmt-v1-g5")[:6] == (
            "tt1234567", 0, 0, 20_000, 1234, "movie.2024.1080p")
        status = call(env, "GET", f"/v1/pool/uploads/{upload_id}")
        assert status.json() == {"upload_id": upload_id, "state": "accepted", "error": None}
    finally:
        env.core.stop()


def test_worker_rejects_actual_staged_size_when_hint_is_missing(tmp_path):
    env = make_env(tmp_path)
    fp = hashlib.sha256(b"oversized staged package").hexdigest()
    try:
        body = upload_body(fp)
        body.pop("size_bytes")
        upload_id = prepare_queued(env, fp, body=body)
        path = f"uploads/{upload_id}.nvmt"
        env.fake.add_object(path, b"x" * (MAX_NVMT_BYTES + 1))

        response = call(env, "POST", "/v1/internal/tick", token=CRON_SECRET)
        assert response.status_code == 200
        assert response.json() == {"processed": 1, "accepted": 0, "failed": 1}
        upload = env.fake.rows("pool_uploads")[0]
        queue = env.fake.rows("pool_publication_queue")[0]
        assert upload["state"] == "failed"
        assert upload["error"] == "staged_object_too_large"
        assert upload["object_path"] == ""
        assert queue["state"] == "failed"
        assert path not in env.fake.objects
        assert env.pool.calls == []
    finally:
        env.core.stop()


def test_accepted_upload_retries_staging_cleanup_without_republishing(tmp_path):
    env = make_env(tmp_path)
    fp = hashlib.sha256(b"cleanup retry").hexdigest()
    try:
        upload_id = prepare_queued(env, fp)
        path = f"uploads/{upload_id}.nvmt"
        env.fake.add_object(path, b"staged bytes")
        env.pool.calls.clear()
        env.pool.upload_package = lambda *args, **kwargs: env.pool.calls.append({"fp": fp})
        env.fake.delete_failures = 1

        first = call(env, "POST", "/v1/internal/tick", token=CRON_SECRET)
        assert first.status_code == 200
        assert first.json() == {"processed": 1, "accepted": 1, "failed": 0}
        assert env.fake.rows("pool_uploads")[0]["state"] == "accepted"
        assert env.fake.rows("pool_publication_queue")[0]["state"] == "done"
        assert path in env.fake.objects
        assert len(env.pool.calls) == 1

        second = call(env, "POST", "/v1/internal/tick", token=CRON_SECRET)
        assert second.status_code == 200
        assert second.json() == {"processed": 0, "accepted": 0, "failed": 0}
        assert path not in env.fake.objects
        assert env.fake.rows("pool_uploads")[0]["object_path"] == ""
        assert len(env.pool.calls) == 1
    finally:
        env.core.stop()


def test_tick_transient_failure_backs_off_then_fails_after_five_attempts(tmp_path):
    env = make_env(tmp_path, real_channel=True)
    fp = hashlib.sha256(b"drain retries").hexdigest()
    try:
        upload_id = prepare_queued(env, fp)
        path = f"uploads/{upload_id}.nvmt"
        env.fake.add_object(path, nvmt_bytes(fp))

        for attempt in range(1, 6):
            env.clock.t += 301                       # always due, whatever the last backoff was
            env.channel.fail_next_upload = True
            response = call(env, "POST", "/v1/internal/tick", token=CRON_SECRET)
            assert response.status_code == 200
            assert response.json() == {"processed": 1, "accepted": 0, "failed": 1}
            upload = env.fake.rows("pool_uploads")[0]
            queue = env.fake.rows("pool_publication_queue")[0]
            assert queue["attempts"] == attempt
            assert upload["error"] == "telegram_unavailable"
            assert path in env.fake.objects            # never deleted on failure
            if attempt < 5:
                assert upload["state"] == "queued" and queue["state"] == "queued"
                assert queue["next_attempt"] == pytest.approx(env.clock.t + 2 ** attempt)
            else:
                assert upload["state"] == "failed" and queue["state"] == "failed"

        # A terminal failure is not retried by another tick.
        env.clock.t += 301
        response = call(env, "POST", "/v1/internal/tick", token=CRON_SECRET)
        assert response.json() == {"processed": 0, "accepted": 0, "failed": 0}
    finally:
        env.core.stop()


def test_tick_does_not_claim_a_row_before_its_backoff(tmp_path):
    env = make_env(tmp_path)
    fp = hashlib.sha256(b"backoff not due").hexdigest()
    try:
        upload_id = prepare_queued(env, fp)
        env.fake.add_object(f"uploads/{upload_id}.nvmt", b"not a package")
        env.pool.fail_with = PoolError(503, "telegram_unavailable")
        first = call(env, "POST", "/v1/internal/tick", token=CRON_SECRET)
        assert first.json() == {"processed": 1, "accepted": 0, "failed": 1}
        second = call(env, "POST", "/v1/internal/tick", token=CRON_SECRET)
        assert second.json() == {"processed": 0, "accepted": 0, "failed": 0}
    finally:
        env.core.stop()


@pytest.mark.parametrize("token,status", [(None, 401), ("wrong-secret", 401), (CRON_SECRET, 200)])
def test_tick_auth_requires_the_cron_secret(tmp_path, token, status):
    env = make_env(tmp_path)
    try:
        response = call(env, "POST", "/v1/internal/tick", token=token)
        assert response.status_code == status
        if status == 401:
            assert response.json() == {"error": "unauthorized"}
    finally:
        env.core.stop()


def test_vercel_cron_get_authenticates_and_drains(tmp_path):
    env = make_env(tmp_path)
    try:
        response = call(env, "GET", "/v1/internal/tick", token=CRON_SECRET)
        assert response.status_code == 200
        assert response.json() == {"processed": 0, "accepted": 0, "failed": 0}
        assert call(env, "GET", "/v1/internal/tick", token="wrong").status_code == 401
    finally:
        env.core.stop()


def test_tick_accepts_the_admin_token_and_bounds_the_batch(tmp_path):
    env = make_env(tmp_path)
    try:
        assert call(env, "POST", "/v1/internal/tick", token=env.core.admin_token).status_code == 200
        uploads = []
        for index in range(4):
            fp = hashlib.sha256(f"batch {index}".encode()).hexdigest()
            upload_id = prepare_queued(env, fp)
            env.fake.add_object(f"uploads/{upload_id}.nvmt", b"package")
            uploads.append(upload_id)

        many = call(env, "POST", "/v1/internal/tick", token=CRON_SECRET, params={"limit": "99"})
        assert many.json() == {"processed": 3, "accepted": 3, "failed": 0}
        rest = call(env, "POST", "/v1/internal/tick", token=CRON_SECRET)
        assert rest.json() == {"processed": 1, "accepted": 1, "failed": 0}
        assert {row["state"] for row in env.fake.rows("pool_uploads")} == {"accepted"}
        assert call(env, "POST", "/v1/internal/tick", token=CRON_SECRET,
                    params={"limit": "nope"}).status_code == 400
    finally:
        env.core.stop()


def test_remote_store_signed_target_and_storage_object_helpers(tmp_path):
    fake = FakeSupabase()
    fake.add_object("uploads/one.nvmt", b"staged-bytes")
    client = SupabaseClient(SUPABASE_URL, SERVICE_KEY, transport=httpx.MockTransport(fake))
    try:
        signed = client.signed_upload_url("uploads/one.nvmt", 900)
        assert signed.startswith("/object/upload/sign/openseek-staging/uploads/one.nvmt")
        assert client.get_object("uploads/one.nvmt") == b"staged-bytes"
        fake.add_object("uploads/large.nvmt", b"01234567890")
        with pytest.raises(RemoteStoreError) as too_large:
            client.get_object("uploads/large.nvmt", max_bytes=10)
        assert too_large.value.status == 413
        assert too_large.value.code == "object_too_large"
        assert client.object_exists("uploads/one.nvmt") is True
        assert client.object_exists("uploads/missing.nvmt") is False
        client.delete_object("uploads/one.nvmt")
        assert client.object_exists("uploads/one.nvmt") is False
    finally:
        client.close()


def test_remote_queue_store_optimistic_claim_retry_and_complete(tmp_path):
    fake = FakeSupabase()
    client = SupabaseClient(SUPABASE_URL, SERVICE_KEY, transport=httpx.MockTransport(fake))
    store = RemoteUploadQueueStore(client)
    now = 1_800_000_000.0
    row = {"upload_id": "u_remote1", "device_id": "", "fp": "a" * 64,
           "profile": "nvmt-v1-g5", "duration_ms": 20_000, "size_bytes": 10,
           "imdb": "tt1234567", "season": 0, "episode": 0,
           "filename_norm": "movie", "object_path": "uploads/u_remote1.nvmt",
           "state": "pending", "created": now, "updated": now, "expires": now + 900,
           "error": None}
    try:
        store.create_upload(row)
        assert store.get_upload("u_remote1")["state"] == "pending"
        assert store.enqueue("u_remote1", now) is True
        assert store.enqueue("u_remote1", now) is False
        claimed = store.claim(now)
        assert claimed["upload_id"] == "u_remote1" and claimed["state"] == "publishing"
        assert claimed["attempts"] == 0
        assert store.claim(now) is None                    # conditional PATCH: one owner only
        reclaimed = store.claim(now + 301)                 # crashed publishing lease expires
        assert reclaimed["upload_id"] == "u_remote1" and reclaimed["state"] == "publishing"
        store.retry("u_remote1", attempts=1, next_attempt=now + 2, error="telegram_unavailable",
                    terminal=False, now=now)
        assert store.get_upload("u_remote1")["state"] == "queued"
        assert store.claim(now) is None                    # not due yet
        assert store.claim(now + 3)["attempts"] == 1
        store.complete("u_remote1", now + 4)
        assert store.get_upload("u_remote1")["state"] == "accepted"
        assert fake.rows("pool_publication_queue")[0]["state"] == "done"
    finally:
        client.close()


def test_remote_upload_queue_pending_count_includes_active_states(tmp_path):
    fake = FakeSupabase()
    client = SupabaseClient(SUPABASE_URL, SERVICE_KEY, transport=httpx.MockTransport(fake))
    store = RemoteUploadQueueStore(client)
    fake.tables["pool_uploads"].extend([
        {"upload_id": "pending", "state": "pending"},
        {"upload_id": "queued", "state": "queued"},
        {"upload_id": "publishing", "state": "publishing"},
        {"upload_id": "accepted", "state": "accepted"},
    ])
    try:
        assert store.pending_count() == 3
    finally:
        client.close()


def test_remote_admin_store_persists_invites_devices_and_owner_settings(tmp_path):
    fake = FakeSupabase()
    client = SupabaseClient(SUPABASE_URL, SERVICE_KEY, transport=httpx.MockTransport(fake))
    store = RemoteAdminStore(client)
    db = DB(tmp_path / "ephemeral.db")
    auth = DeviceAuth(db, store=store)
    invites = InviteManager(store, "sha256//test-pin", auth.register, iterations=200_000)
    try:
        invite = invites.create("Nuvio", ttl_s=600, role="owner", contribute=False)
        state = invites.start(invite["invite_id"])
        stored = store.invite(invite["invite_id"])
        assert invite["secret"] not in repr(stored)
        key = bytes.fromhex(stored["k_hex"])
        name = "Living room TV"
        claimed = invites.claim(invite["invite_id"], name,
                                client_proof(key, invite["invite_id"], name))

        restarted_auth = DeviceAuth(db, store=RemoteAdminStore(client))
        assert restarted_auth.authenticate("Bearer " + claimed["token"]) == claimed["device_id"]
        assert restarted_auth.devices()[0]["name"] == name
        assert state["pair_id"] == invite["invite_id"]
        assert store.invite(invite["invite_id"])["uses"] == 1

        store.set_setting("sharing_enabled", "1")
        assert store.get_setting("sharing_enabled") == "1"
    finally:
        db.close()
        client.close()


def test_local_queue_store_same_state_machine(tmp_path):
    db = DB(tmp_path / "local-queue.db")
    store = UploadQueueStore(db)
    now = 1_800_000_000.0
    try:
        store.create_upload({"upload_id": "u_local1", "device_id": "dev", "fp": "b" * 64,
                             "profile": "nvmt-v1-g5", "duration_ms": 20_000, "size_bytes": None,
                             "imdb": None, "season": 0, "episode": 0, "filename_norm": None,
                             "object_path": "uploads/u_local1.nvmt", "state": "pending",
                             "created": now, "updated": now, "expires": now + 900, "error": None})
        assert store.pending_count() == 1
        assert store.enqueue("u_local1", now) is True
        assert store.enqueue("u_local1", now) is False
        claimed = store.claim(now)
        assert claimed["state"] == "publishing" and claimed["attempts"] == 0
        assert store.claim(now) is None
        reclaimed = store.claim(now + 301)
        assert reclaimed["state"] == "publishing"
        store.retry("u_local1", attempts=1, next_attempt=now + 2, error="telegram_unavailable",
                    terminal=False, now=now)
        assert store.claim(now) is None
        assert store.claim(now + 3)["attempts"] == 1
        store.complete("u_local1", now + 4)
        assert store.get_upload("u_local1")["state"] == "accepted"
        assert store.pending_count() == 0
        assert db.q("SELECT state FROM pool_publication_queue WHERE upload_id=?",
                    ("u_local1",))[0][0] == "done"
        assert store.expire("u_missing", now) is False
    finally:
        db.close()
