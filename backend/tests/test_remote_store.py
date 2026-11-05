"""RemoteChannelStore over PostgREST, exercised with an httpx MockTransport."""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from openseek.cache import PROFILE
from openseek.remote import (RemoteChannelStore, RemotePairingStore, RemoteStoreError,
                             RemoteTelegramSessionLease, SupabaseClient)

KEY = "service-role-key-that-must-not-leak"


class FakePostgREST:
    def __init__(self, payloads=None):
        self.requests: list[dict] = []
        self.payloads = payloads or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        self.requests.append({
            "method": request.method,
            "path": request.url.path,
            "params": dict(request.url.params),
            "multi": request.url.params.multi_items(),
            "headers": dict(request.headers),
            "json": json.loads(body) if body else None,
        })
        for prefix, payload in self.payloads.items():
            if request.url.path == prefix:
                return httpx.Response(200, json=payload, request=request)
        if request.method == "GET":
            return httpx.Response(200, json=[], request=request)
        if request.method == "DELETE":
            return httpx.Response(200, json=[], request=request)
        return httpx.Response(201, json=[], request=request)


def make_store(payloads=None):
    fake = FakePostgREST(payloads)
    client = SupabaseClient("https://project.supabase.co", KEY,
                            transport=httpx.MockTransport(fake))
    return RemoteChannelStore(client), fake, client


def tile(fp="a" * 64, k=0, message_id=1, duration_ms=20_000):
    return {"fp": fp, "profile": PROFILE, "k": k, "pts_ms": k * 10_000,
            "duration_ms": duration_ms, "n_tiles": 2, "size": 100,
            "sha256": hashlib.sha256(bytes([k])).hexdigest(),
            "message_id": message_id, "created": 1.0}


def hints(fp="a" * 64):
    return {"fp": fp, "profile": PROFILE, "imdb": "tt1234567", "season": 0, "episode": 0,
            "duration_ms": 20_000, "size_bytes": 1_000_000, "filename_norm": "movie.2024",
            "n_tiles": 2, "grid": 5_000, "created": 10.0}


def test_upserts_use_merge_duplicates_and_explicit_conflict_targets():
    store, fake, _client = make_store()
    store.set_state("chan", 5, 1.5)
    store.put_hints(hints())
    store.put_episode_package("b" * 64, "a" * 64, PROFILE, 20_000, 2.0)

    state, hint, episode = fake.requests
    assert (state["method"], state["path"]) == ("POST", "/rest/v1/pool_channel_state")
    assert state["params"]["on_conflict"] == "channel_id"
    assert "resolution=merge-duplicates" in state["headers"]["prefer"]
    assert state["headers"]["apikey"] == KEY

    assert (hint["method"], hint["path"]) == ("POST", "/rest/v1/pool_hints")
    assert hint["params"]["on_conflict"] == "fp,profile"
    assert "resolution=merge-duplicates" in hint["headers"]["prefer"]
    assert hint["json"] == [hints()]

    assert (episode["method"], episode["path"]) == ("POST", "/rest/v1/pool_episode_packages")
    assert episode["params"]["on_conflict"] == "episode_key,fp,profile"


def test_selects_use_rest_filters_and_ordering():
    store, fake, _client = make_store()
    store.get_tile("a" * 64, PROFILE, 3)
    store.tiles_for_package("a" * 64, PROFILE)
    store.get_hints("a" * 64, PROFILE)
    store.state("chan")

    tile_get, package_get, hint_get, state_get = fake.requests
    assert (tile_get["method"], tile_get["path"]) == ("GET", "/rest/v1/pool_channel_tiles")
    assert tile_get["params"]["fp"] == f"eq.{'a' * 64}"
    assert tile_get["params"]["profile"] == f"eq.{PROFILE}"
    assert tile_get["params"]["k"] == "eq.3"
    assert tile_get["params"]["limit"] == "1"

    assert package_get["path"] == "/rest/v1/pool_channel_tiles"
    assert package_get["params"]["order"] == "k.asc"
    assert "k" in package_get["params"]["select"]

    assert hint_get["path"] == "/rest/v1/pool_hints"
    assert hint_get["params"]["fp"] == f"eq.{'a' * 64}"
    assert hint_get["params"]["limit"] == "1"

    assert state_get["path"] == "/rest/v1/pool_channel_state"
    assert state_get["params"]["channel_id"] == "eq.chan"


def test_candidate_hints_join_source_profile_to_canonical_channel_tiles():
    fp = "a" * 64
    source_hint = {
        **hints(fp), "profile": "nvmt-v1-g10", "grid": 10_000,
        "verified": 0,
    }
    channel_package = {
        "fp": fp, "profile": PROFILE, "duration_ms": 20_000, "channel_tiles": 2,
    }
    store, fake, _client = make_store({
        "/rest/v1/pool_hints": [source_hint],
        "/rest/v1/pool_packages": [channel_package],
    })

    rows = store.hint_rows_for_candidates(20_000, 3_000, None)

    assert len(rows) == 1
    assert rows[0][0:2] == (fp, "nvmt-v1-g10")
    assert rows[0][11] == 2
    package_request = next(r for r in fake.requests if r["path"] == "/rest/v1/pool_packages")
    assert package_request["params"]["select"] == "fp,profile,duration_ms,channel_tiles"


def test_candidates_join_hints_with_channel_package_counts():
    fp = "a" * 64
    row = {"fp": fp, "profile": PROFILE, "imdb": "tt1234567", "season": 0, "episode": 0,
           "duration_ms": 20_000, "size_bytes": 1_000_000, "filename_norm": "movie.2024",
           "n_tiles": 2, "grid": 5_000, "created": 10.0, "verified": 0}
    store, fake, _client = make_store({
        "/rest/v1/pool_hints": [row],
        "/rest/v1/pool_packages": [{"fp": fp, "profile": PROFILE,
                                     "duration_ms": 20_000, "channel_tiles": 2}],
    })
    rows = store.hint_rows_for_candidates(23_000, 90_000, None)
    assert rows == [(fp, PROFILE, "tt1234567", 0, 0, 20_000, 1_000_000,
                     "movie.2024", 2, 5_000, 10.0, 2, 0)]
    hint_request, package_request = fake.requests
    assert [value for key, value in hint_request["multi"] if key == "duration_ms"] == [
        "gte.0", "lte.113000"]
    assert package_request["path"] == "/rest/v1/pool_packages"
    assert package_request["params"]["fp"] == f"in.({fp})"


def test_deletes_are_filtered_and_report_rowcounts():
    store, fake, _client = make_store({"/rest/v1/pool_channel_tiles": [{"k": 0}]})
    assert store.forget_tile("a" * 64, PROFILE, 0) is True
    fake.payloads["/rest/v1/pool_channel_tiles"] = []
    assert store.forget_tile("a" * 64, PROFILE, 1) is False

    store.clear_channel("chan")
    first, second, *clears = fake.requests
    assert first["method"] == "DELETE"
    assert first["path"] == "/rest/v1/pool_channel_tiles"
    assert first["params"]["fp"] == f"eq.{'a' * 64}"
    assert first["params"]["k"] == "eq.0"
    assert "return=representation" in first["headers"]["prefer"]
    assert [request["path"] for request in clears] == [
        "/rest/v1/pool_channel_tiles", "/rest/v1/pool_hints",
        "/rest/v1/pool_episode_packages", "/rest/v1/pool_channel_state"]
    assert clears[-1]["params"]["channel_id"] == "eq.chan"


def test_supabase_error_raises_remote_store_error_without_the_key():
    def fail(request):
        return httpx.Response(401, json={"code": "PGRST301", "message": "bad jwt"},
                              request=request)

    client = SupabaseClient("https://project.supabase.co", KEY,
                            transport=httpx.MockTransport(fail))
    store = RemoteChannelStore(client)
    with pytest.raises(RemoteStoreError) as excinfo:
        store.get_tile("a" * 64, PROFILE, 0)
    assert excinfo.value.status == 401
    assert excinfo.value.code == "PGRST301"
    assert KEY not in str(excinfo.value)


def test_transport_failure_is_wrapped():
    def fail(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = SupabaseClient("https://project.supabase.co", KEY,
                            transport=httpx.MockTransport(fail))
    store = RemoteChannelStore(client)
    with pytest.raises(RemoteStoreError) as excinfo:
        store.state("chan")
    assert excinfo.value.status == 0
    assert KEY not in str(excinfo.value)


def test_telegram_session_lease_uses_atomic_supabase_rpcs():
    fake = FakePostgREST({
        "/rest/v1/rpc/try_acquire_pool_telegram_session": True,
        "/rest/v1/rpc/release_pool_telegram_session": True,
    })
    client = SupabaseClient("https://project.supabase.co", KEY,
                            transport=httpx.MockTransport(fake))
    lease = RemoteTelegramSessionLease(client, lease_seconds=80)
    owner = "owner-token-that-is-long-enough"

    assert lease.acquire(owner) is True
    assert lease.release(owner) is True
    acquire, release = fake.requests
    assert acquire["path"] == "/rest/v1/rpc/try_acquire_pool_telegram_session"
    assert acquire["json"] == {"p_owner": owner, "p_lease_seconds": 80}
    assert release["path"] == "/rest/v1/rpc/release_pool_telegram_session"
    assert release["json"] == {"p_owner": owner}


def test_remote_pairing_store_persists_devices_and_invites_in_owner_supabase():
    device_id = "a1b2c3d4e5f60718"
    pair_id = "1a2b3c4d5e6f708192a3b4c5d6e7f809"
    device_row = {"device_id": device_id, "name": "Nuvio TV", "token_hash": "f" * 64,
                  "role": "guest", "expires": None, "created": 1.0, "last_seen": 1.0,
                  "contribute": True}
    invite_row = {"pair_id": pair_id, "label": "Nuvio", "role": "guest", "salt": "a" * 32,
                  "iterations": 240000, "k_hex": "b" * 64, "created": 1.0, "expires": 100.0,
                  "max_uses": 1, "uses": 0, "attempts": 0, "revoked": None,
                  "device_days": None, "contribute": True}
    fake = FakePostgREST({
        "/rest/v1/pool_devices": [device_row],
        "/rest/v1/pool_invites": [invite_row],
        "/rest/v1/rpc/use_pool_invite": True,
        "/rest/v1/rpc/revoke_pool_invite": True,
    })
    client = SupabaseClient("https://project.supabase.co", KEY,
                            transport=httpx.MockTransport(fake))
    store = RemotePairingStore(client)

    assert store.auth_rows() == [("f" * 64, device_id, "guest", None)]
    store.add_device(device_id, "Nuvio TV", "f" * 64, "guest", None, pair_id)
    assert store.devices()[0]["device_id"] == device_id
    assert store.can_contribute(device_id)
    assert store.set_contribute(device_id, False)
    assert store.invite(pair_id) == invite_row
    assert store.invite_by_tag("ABCD", 2.0) == pair_id
    assert store.tag_in_use("ABCD", 2.0)
    store.add_invite(pair_id, "Nuvio", "guest", "a" * 32, 240000, "b" * 64,
                     1.0, 100.0, 1, None, "ABCD", True)
    assert store.use_invite(pair_id, 2.0, 5)
    assert store.revoke_invite(pair_id, 2.0)
    assert store.remove_device(device_id)

    assert all(request["path"].startswith(("/rest/v1/pool_", "/rest/v1/rpc/"))
               for request in fake.requests)
    assert any(request["path"] == "/rest/v1/rpc/use_pool_invite" for request in fake.requests)
    assert all(KEY not in json.dumps(request["json"]) for request in fake.requests if request["json"])


def test_telegram_session_lease_reports_contention():
    fake = FakePostgREST({"/rest/v1/rpc/try_acquire_pool_telegram_session": False})
    client = SupabaseClient("https://project.supabase.co", KEY,
                            transport=httpx.MockTransport(fake))
    assert RemoteTelegramSessionLease(client).acquire("owner-token-that-is-long-enough") is False
