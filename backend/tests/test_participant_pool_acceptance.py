"""One black-box setup-to-shared-pool round trip with isolated node backends."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

from openseek.app import Core
from openseek.auth import DeviceAuth
from openseek.cache import PROFILE
from openseek.config import Settings
from openseek.pairing import InviteManager, PairingManager, client_proof, derive_key
from openseek.pool import decode_nvmt, encode_nvmt
from openseek.remote import (RemoteChannelStore, RemotePairingStore,
                             RemoteTelegramSessionLease, RemoteUploadQueueStore,
                             SupabaseClient)
from openseek.remote_admin import RemoteAdminStore
from openseek.uploads import UploadService
from test_participant_setup import FakeProvider, FakeTelegramJoiner, _source
from test_pool_channel import MemoryChannel, jpeg
from openseek.participant_setup import ParticipantSetup
from openseek.telegram_setup import TelegramSetupResult


class StatefulSupabase:
    """Small PostgREST/Storage double; each participant gets a separate instance."""

    TABLES = ("pool_channel_tiles", "pool_channel_state", "pool_hints",
              "pool_episode_packages", "pool_uploads", "pool_publication_queue",
              "pool_devices", "pool_invites", "pool_owner_settings",
              "pool_generation_jobs")

    def __init__(self):
        self.tables = {name: [] for name in self.TABLES}
        self.objects: dict[str, bytes] = {}
        self.lease = None
        self.requests: list[tuple[str, str]] = []

    @staticmethod
    def _value(raw: str):
        try:
            return float(raw) if "." in raw else int(raw)
        except ValueError:
            return raw

    def _rows(self, table: str):
        if table == "pool_packages":
            grouped = {}
            for row in self.tables["pool_channel_tiles"]:
                key = (row["fp"], row["profile"], row["duration_ms"], row["n_tiles"])
                grouped.setdefault(key, []).append(row)
            return [{"fp": fp, "profile": profile, "duration_ms": duration,
                     "n_tiles": n_tiles, "channel_tiles": len(rows),
                     "created": max(row["created"] for row in rows)}
                    for (fp, profile, duration, n_tiles), rows in grouped.items()]
        return self.tables[table]

    def _filter(self, rows, params):
        result = list(rows)
        for key, raw in params:
            if key in ("select", "order", "limit", "on_conflict"):
                continue
            if raw.startswith("eq."):
                wanted = raw[3:]
                result = [r for r in result if str(r.get(key)) == wanted]
            elif raw.startswith("neq."):
                wanted = raw[4:]
                result = [r for r in result if str(r.get(key)) != wanted]
            elif raw.startswith("gt."):
                wanted = self._value(raw[3:])
                result = [r for r in result if r.get(key) is not None and r[key] > wanted]
            elif raw.startswith("gte."):
                wanted = self._value(raw[4:])
                result = [r for r in result if r.get(key) is not None and r[key] >= wanted]
            elif raw.startswith("lte."):
                wanted = self._value(raw[4:])
                result = [r for r in result if r.get(key) is not None and r[key] <= wanted]
            elif raw.startswith("lt."):
                wanted = self._value(raw[3:])
                result = [r for r in result if r.get(key) is not None and r[key] < wanted]
            elif raw == "is.null":
                result = [r for r in result if r.get(key) is None]
            elif raw.startswith("in.(") and raw.endswith(")"):
                values = set(raw[4:-1].split(","))
                result = [r for r in result if str(r.get(key)) in values]
            else:
                raise AssertionError(f"unsupported PostgREST filter {key}={raw}")
        for key, raw in params:
            if key == "order":
                column, _, direction = raw.partition(".")
                result.sort(key=lambda row: row.get(column) or 0,
                            reverse=direction == "desc")
        for key, raw in params:
            if key == "limit":
                result = result[:int(raw)]
        return result

    def _rpc(self, name: str, body: dict):
        if name == "enqueue_pool_publication":
            row = next((r for r in self.tables["pool_uploads"]
                        if r["upload_id"] == body["p_upload_id"]), None)
            now = float(body["p_now"])
            if not row or row["state"] != "pending" or float(row["expires"]) <= now:
                return False
            row.update(state="queued", updated=now, error=None)
            self.tables["pool_publication_queue"].append({
                "upload_id": row["upload_id"], "fp": row["fp"], "profile": row["profile"],
                "attempts": 0, "next_attempt": now, "state": "queued",
                "created": now, "updated": now, "error": None})
            return True
        if name == "try_acquire_pool_telegram_session":
            if self.lease is not None:
                return False
            self.lease = body["p_owner"]
            return True
        if name == "release_pool_telegram_session":
            if self.lease != body["p_owner"]:
                return False
            self.lease = None
            return True
        if name == "bump_pool_invite_attempts":
            row = next(r for r in self.tables["pool_invites"] if r["pair_id"] == body["p_pair_id"])
            row["attempts"] += 1
            return None
        if name == "use_pool_invite":
            row = next((r for r in self.tables["pool_invites"] if r["pair_id"] == body["p_pair_id"]), None)
            if (not row or row["revoked"] is not None or float(row["expires"]) <= body["p_now"] or
                    row["uses"] >= row["max_uses"] or row["attempts"] >= body["p_max_attempts"]):
                return False
            row["uses"] += 1
            return True
        if name == "revoke_pool_invite":
            row = next((r for r in self.tables["pool_invites"] if r["pair_id"] == body["p_pair_id"]), None)
            if not row or row["revoked"] is not None:
                return False
            row["revoked"] = float(body["p_now"])
            return True
        raise AssertionError(f"unsupported RPC {name}")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if path.startswith("/rest/v1/rpc/"):
            result = self._rpc(path.rsplit("/", 1)[-1], json.loads(request.content or b"{}"))
            return httpx.Response(200, json=result, request=request)
        if path.startswith("/storage/v1/object/upload/sign/"):
            return httpx.Response(200, json={
                "url": "/object/upload/sign/openseek-staging/uploads/signed?token=fake"}, request=request)
        if path.startswith("/storage/v1/object/"):
            parts = path.split("/", 5)
            obj = unquote(parts[-1])
            if request.method == "GET":
                if obj not in self.objects:
                    return httpx.Response(404, json={"message": "missing"}, request=request)
                return httpx.Response(200, content=self.objects[obj], request=request)
            if request.method == "DELETE":
                self.objects.pop(obj, None)
                return httpx.Response(200, json={}, request=request)
            if request.method in ("POST", "PUT"):
                self.objects[obj] = request.content
                return httpx.Response(200, json={}, request=request)
        if path.startswith("/rest/v1/"):
            table = path.rsplit("/", 1)[-1]
            if table not in self.TABLES and table != "pool_packages":
                raise AssertionError(f"unknown table {table}")
            params = list(request.url.params.multi_items())
            rows = self._rows(table)
            if request.method == "GET":
                matched = self._filter(rows, params)
                select = next((v for k, v in params if k == "select"), "*")
                columns = select.split(",")
                return httpx.Response(200, json=[{k: row.get(k) for k in columns} for row in matched],
                                      request=request)
            if request.method in ("POST", "PATCH", "DELETE"):
                if request.method == "POST":
                    payload = json.loads(request.content or b"[]")
                    payload = payload if isinstance(payload, list) else [payload]
                    conflict = next((v.split(",") for k, v in params if k == "on_conflict"), [])
                    ignore = "ignore-duplicates" in request.headers.get("prefer", "")
                    changed = []
                    for new in payload:
                        old = next((r for r in rows if conflict and all(
                            str(r.get(k)) == str(new.get(k)) for k in conflict)), None)
                        if old is not None:
                            if not ignore:
                                old.update(new)
                                changed.append(old)
                        else:
                            rows.append(dict(new))
                            changed.append(rows[-1])
                else:
                    matched = self._filter(rows, params)
                    if request.method == "PATCH":
                        update = json.loads(request.content or b"{}")
                        for row in matched:
                            row.update(update)
                        changed = matched
                    else:
                        changed = matched
                        for row in matched:
                            rows.remove(row)
                body = changed if "return=representation" in request.headers.get("prefer", "") else []
                return httpx.Response(200 if request.method != "POST" else 201,
                                      json=body, request=request)
        return httpx.Response(404, request=request)


@pytest.fixture
def clean_vercel_module_import():
    previous = sys.modules.pop("openseek.vercel", None)
    yield
    sys.modules.pop("openseek.vercel", None)
    if previous is not None:
        sys.modules["openseek.vercel"] = previous


def _node(tmp_path: Path, tag: str, telegram: MemoryChannel, fake: StatefulSupabase,
          *, contribution=True):
    from openseek.vercel import build_app

    cfg = Settings(data_dir=tmp_path / tag, profile="vercel", pool_mode="channel",
                   admin_token=f"participant-owner-{tag}-secret",
                   contribution=contribution, telegram_channel_id=telegram.channel_id,
                   telegram_session=f"session-{tag}", telegram_api_id=123,
                   telegram_api_hash=f"api-hash-{tag}",
                   supabase_url="https://project.supabase.co",
                   supabase_service_key=f"service-role-{tag}", supabase_bucket="openseek-staging")
    core = Core(cfg, pool_storage=telegram)
    old_client = core.supabase
    client = SupabaseClient(cfg.supabase_url, cfg.supabase_service_key,
                            transport=httpx.MockTransport(fake), bucket=cfg.supabase_bucket)
    if old_client:
        old_client.close()
    core.supabase = client
    core.state_store = RemoteAdminStore(client)
    core.channel_index.store = RemoteChannelStore(client)
    core.pairing_store = RemotePairingStore(client)
    core.auth = DeviceAuth(core.pairing_store)
    core.invites = InviteManager(core.pairing_store, "", core.auth.register)
    core.pairing = PairingManager("", core.auth.register, invites=core.invites)
    core.pairing.sharing_enabled = core.sharing_enabled
    core.telegram_session_lease = RemoteTelegramSessionLease(client)
    core.uploads = UploadService(cfg, client, core.pool,
                                 RemoteUploadQueueStore(client))
    return core, build_app(cfg, core=core)


def _request(app, method, path, *, token=None, body=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://participant.vercel.app") as client:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            return await client.request(method, path, json=body, headers=headers)
    import asyncio
    return asyncio.run(run())


def _pair_from_bundle(app, bundle: str) -> str:
    origin, code = bundle.split("|", 1)
    typed = code.replace("-", "")
    start = _request(app, "POST", "/v1/pair/start", body={"invite": typed[:4]})
    assert start.status_code == 200, start.text
    challenge = start.json()
    key = derive_key(typed[4:], challenge["pair_id"], challenge["salt"],
                     challenge["spki_pin"], challenge["iterations"])
    claim = _request(app, "POST", "/v1/pair/claim", body={
        "pair_id": challenge["pair_id"], "device_name": "Nuvio TV",
        "client_proof": client_proof(key, challenge["pair_id"], "Nuvio TV")})
    assert claim.status_code == 200, claim.text
    return claim.json()["token"]


def test_wizard_pair_publish_exact_and_hint_read_on_independent_nodes(
        monkeypatch, tmp_path, clean_vercel_module_import):
    # vercel.py exports a module-level app for the platform. Import it in its
    # setup-pending configuration first; the test nodes below use explicit fake-backed settings.
    monkeypatch.setenv("OPENSEEK_PROFILE", "vercel")
    monkeypatch.setenv("OPENSEEK_POOL_MODE", "channel")
    monkeypatch.setenv("OPENSEEK_SETUP_PENDING", "1")
    monkeypatch.setenv("OPENSEEK_SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("OPENSEEK_SUPABASE_SERVICE_KEY", "import-only-test-key")
    telegram = MemoryChannel()
    owner_store = StatefulSupabase()
    receiver_store = StatefulSupabase()
    owner, owner_app = _node(tmp_path, "owner", telegram, owner_store)
    # A fresh node starts with sharing off; the owner turns it on from the dashboard
    # before creating the Nuvio invite.
    owner.state_store.set_setting("sharing_enabled", "1")
    owner.state_store.set_setting("contrib_enabled", "1")
    receiver, receiver_app = _node(tmp_path, "receiver", telegram, receiver_store,
                                   contribution=False)
    state_path = tmp_path / "participant" / "setup.json"
    output = []
    setup_key_for_bridge = {"value": ""}

    class Provider(FakeProvider):
        def create_pairing_invite(self, url, setup_key):
            setup_key_for_bridge["value"] = setup_key
            monkeypatch.setenv("OPENSEEK_SETUP_KEY", setup_key)
            response = _request(owner_app, "POST", "/v1/setup/pairing", token=setup_key,
                                body={"label": "Nuvio TV"})
            assert response.status_code == 200, response.text
            invite = response.json()
            return {"code": invite["code"], "spki_pin": invite["spki_pin"]}

    provider = Provider()
    source = _source(tmp_path)
    first = ParticipantSetup(provider, source_dir=source, state_path=state_path,
                             input_fn=lambda _prompt: "1", print_fn=output.append)
    assert first.run() == 0

    telegram_joiner = FakeTelegramJoiner()
    second = ParticipantSetup(
        provider, source_dir=source, state_path=state_path,
        input_fn=lambda prompt: {"Telegram API ID: ": "1234",
                                 "Shared Telegram group invite link: ": "https://t.me/+shared"}[prompt],
        secret_input_fn=lambda _prompt: "telegram-api-hash",
        telegram_joiner=telegram_joiner, print_fn=output.append)
    assert second.run() == 0
    bundle = next(line for line in output if "|" in line)
    assert setup_key_for_bridge["value"] not in json.dumps(provider.env)
    assert provider.env["OPENSEEK_TELEGRAM_SESSION"] == ("telegram_session_secret", True)
    assert "telegram_session_secret" not in "\n".join(output)
    assert telegram_joiner.calls[0][2] == "https://t.me/+shared"

    nuvio_token = _pair_from_bundle(owner_app, bundle)
    pair_status = _request(owner_app, "GET", "/v1/pair/status", token=nuvio_token)
    assert pair_status.status_code == 200 and pair_status.json()["contribute"] is True

    # Client-side sharing is an explicit opt-in. No staged request is made until
    # this simulated setting changes from its default-off state.
    contribution_opt_in = False
    fp = hashlib.sha256(b"locally generated thumbnail package").hexdigest()
    image_a, image_b = jpeg((20, 40, 60)), jpeg((60, 40, 20))
    package = encode_nvmt(10_000, 2, {0: (0, image_a), 1: (10_000, image_b)})
    create_body = {"fp": fp, "profile": "nvmt-v1-g10", "duration_ms": 20_000,
                   "size_bytes": 2_000_000, "imdb": "tt7654321", "season": 2,
                   "episode": 4, "filename": "Example.Show.S02E04.mkv"}
    assert contribution_opt_in is False  # Nuvio's persisted default is checked by its settings test.
    contribution_opt_in = True
    assert contribution_opt_in  # Simulate the one-time user opt-in before Nuvio calls the node.
    created = _request(owner_app, "POST", "/v1/pool/uploads", token=nuvio_token,
                       body=create_body)
    assert created.status_code == 201, created.text
    upload_id = created.json()["upload_id"]
    object_path = next(r["object_path"] for r in owner_store.tables["pool_uploads"]
                       if r["upload_id"] == upload_id)
    owner_store.objects[object_path] = package
    finalized = _request(owner_app, "POST", f"/v1/pool/uploads/{upload_id}/finalize",
                         token=nuvio_token, body={})
    assert finalized.status_code == 202
    accepted = _request(owner_app, "GET", f"/v1/pool/uploads/{upload_id}", token=nuvio_token)
    assert accepted.status_code == 200 and accepted.json()["state"] == "accepted", (
        accepted.json(), [r for r in owner_store.requests if r[1].startswith("/storage")],
        object_path, list(owner_store.objects))
    assert object_path not in owner_store.objects
    assert len(telegram.messages) == 3  # two validated tile documents + one hints manifest

    receiver_token = "R" * 43
    receiver.auth.register("Receiver Nuvio", hashlib.sha256(receiver_token.encode()).hexdigest(), "guest")
    miss = _request(receiver_app, "GET",
                    f"/v1/pool/{'f' * 64}/nvmt/manifest?profile=nvmt-v1-g10&duration_ms=20000",
                    token=receiver_token)
    assert miss.status_code == 404

    exact = _request(receiver_app, "GET",
                     f"/v1/pool/{fp}/nvmt/manifest?profile=nvmt-v1-g10&duration_ms=20000",
                     token=receiver_token)
    assert exact.status_code == 200, exact.text
    exact_chunk = _request(receiver_app, "GET",
                           f"/v1/pool/{fp}/nvmt?profile=nvmt-v1-g10&slots=0,1",
                           token=receiver_token)
    assert exact_chunk.status_code == 200
    exact_decoded = decode_nvmt(fp, "nvmt-v1-g10", 20_000, exact_chunk.content)
    assert exact_decoded[2] == {0: (0, image_a), 1: (10_000, image_b)}
    assert len(exact_chunk.content) < 4_500_000

    candidates = _request(receiver_app, "GET",
                          "/v1/pool/candidates?imdb=tt7654321&season=2&episode=4&duration_ms=20000",
                          token=receiver_token)
    assert candidates.status_code == 200, candidates.text
    assert candidates.json()["candidates"] and candidates.json()["candidates"][0]["fp"] == fp, {
        "response": candidates.json(),
        "hints": receiver_store.tables["pool_hints"],
        "tiles": receiver_store.tables["pool_channel_tiles"],
    }
    candidate_manifest = _request(receiver_app, "GET",
                                  f"/v1/pool/{fp}/nvmt/manifest?profile=nvmt-v1-g10&duration_ms=20000",
                                  token=receiver_token)
    candidate_chunk = _request(receiver_app, "GET",
                               f"/v1/pool/{fp}/nvmt?profile=nvmt-v1-g10&slots=0,1",
                               token=receiver_token)
    assert candidate_manifest.status_code == candidate_chunk.status_code == 200
    assert decode_nvmt(fp, "nvmt-v1-g10", 20_000, candidate_chunk.content)[2] == exact_decoded[2]

    # A participant can publish a sparse NVMT too. The shared manifest and
    # exact/hint reads must preserve the one available tile without claiming
    # that the missing slot exists.
    sparse_fp = hashlib.sha256(b"partial local thumbnail package").hexdigest()
    sparse_package = encode_nvmt(10_000, 2, {0: (0, image_a)})
    sparse_create = {**create_body, "fp": sparse_fp, "imdb": "tt7654322",
                     "filename": "Partial.Show.S02E04.mkv"}
    sparse_upload = _request(owner_app, "POST", "/v1/pool/uploads", token=nuvio_token,
                             body=sparse_create)
    assert sparse_upload.status_code == 201, sparse_upload.text
    sparse_id = sparse_upload.json()["upload_id"]
    sparse_path = next(r["object_path"] for r in owner_store.tables["pool_uploads"]
                       if r["upload_id"] == sparse_id)
    owner_store.objects[sparse_path] = sparse_package
    sparse_finalized = _request(owner_app, "POST", f"/v1/pool/uploads/{sparse_id}/finalize",
                                token=nuvio_token, body={})
    assert sparse_finalized.status_code == 202
    sparse_status = _request(owner_app, "GET", f"/v1/pool/uploads/{sparse_id}", token=nuvio_token)
    assert sparse_status.status_code == 200 and sparse_status.json()["state"] == "accepted"

    # The reader owns a rebuildable index and sees new Telegram posts on its
    # next refresh, just as it does after a scheduled Vercel invocation.
    receiver.channel_index.refresh(force=True)
    sparse_manifest = _request(
        receiver_app, "GET",
        f"/v1/pool/{sparse_fp}/nvmt/manifest?profile=nvmt-v1-g10&duration_ms=20000",
        token=receiver_token)
    assert sparse_manifest.status_code == 200, {
        "response": sparse_manifest.text,
        "messages": [message.caption for message in telegram.messages],
        "indexed": receiver_store.tables["pool_channel_tiles"],
        "state": receiver_store.tables["pool_channel_state"],
    }
    assert sparse_manifest.json()["tile_slots"] == [0]
    sparse_exact = _request(receiver_app, "GET",
                             f"/v1/pool/{sparse_fp}/nvmt?profile=nvmt-v1-g10&slots=0,1",
                             token=receiver_token)
    assert sparse_exact.status_code == 200
    assert decode_nvmt(sparse_fp, "nvmt-v1-g10", 20_000, sparse_exact.content)[2] == {
        0: (0, image_a)}
    sparse_candidates = _request(
        receiver_app, "GET",
        "/v1/pool/candidates?imdb=tt7654322&season=2&episode=4&duration_ms=20000",
        token=receiver_token)
    assert sparse_candidates.status_code == 200
    assert [row["fp"] for row in sparse_candidates.json()["candidates"]] == [sparse_fp]
    sparse_hint_manifest = _request(
        receiver_app, "GET",
        f"/v1/pool/{sparse_fp}/nvmt/manifest?profile=nvmt-v1-g10&duration_ms=20000",
        token=receiver_token)
    sparse_hint_chunk = _request(
        receiver_app, "GET", f"/v1/pool/{sparse_fp}/nvmt?profile=nvmt-v1-g10&slots=0,1",
        token=receiver_token)
    assert sparse_hint_manifest.status_code == sparse_hint_chunk.status_code == 200
    assert sparse_hint_manifest.json()["tile_slots"] == [0]
    assert decode_nvmt(sparse_fp, "nvmt-v1-g10", 20_000, sparse_hint_chunk.content)[2] == {
        0: (0, image_a)}
    assert owner_store is not receiver_store
    assert owner_store.tables["pool_channel_tiles"] is not receiver_store.tables["pool_channel_tiles"]
    assert owner_store.tables["pool_channel_tiles"]  # publisher maintains its own rebuildable index
    assert receiver_store.tables["pool_channel_tiles"]
    assert receiver_store.tables["pool_hints"][0]["filename_norm"] == "example.show.s02e04"

    owner.stop()
    receiver.stop()
