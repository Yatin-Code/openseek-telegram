"""Dashboard-only NVMT generation state, packaging, and publication tests."""
from __future__ import annotations

import asyncio
import hashlib
import threading
from types import SimpleNamespace

import httpx
import pytest

from openseek.app import Core, create_admin_app, create_api_app
from openseek.cache import Cache
from openseek.config import Settings
from openseek.db import DB
from openseek.generation import GenerationService, GenerationStateStore
from openseek.netpolicy import DestinationPolicy
from openseek.pool import decode_nvmt
from test_pool_channel import MemoryChannel, jpeg


@pytest.fixture
def generation_env(tmp_path):
    db = DB(tmp_path / "generation.db")
    cache = Cache(tmp_path / "data", db, 1 << 24, 30)
    fp = hashlib.sha256(b"generated source").hexdigest()
    cache.set_media(fp, 20_000, 2)
    cache.put_tile(fp, 0, 0, jpeg())
    cache.put_tile(fp, 1, 10_000, jpeg((40, 50, 60)))
    store = GenerationStateStore(db)
    class Pool:
        def __init__(self):
            self.uploads = []

        def upload_package(self, *args, **kwargs):
            self.uploads.append((args, kwargs))

    pool = Pool()
    core = SimpleNamespace(
        db=db, cache=cache, pool=pool, uploads=None,
        settings=SimpleNamespace(profile="local"),
        channel_store=SimpleNamespace(can_post=True, start=lambda: None),
        jobs=SimpleNamespace(
            snapshot=lambda _job: {"source_duration_ms": 20_000, "tiles_done": 2, "tiles_total": 2},
            state=lambda _job: ("complete", None), delete=lambda _job: None,
            sources={fp: SimpleNamespace(size=12345, cond=threading.Condition(), scheduler=None,
                                         touch=lambda: None)}),
    )
    service = GenerationService(core, store)
    yield service, store, pool, core, fp
    db.close()


def test_generation_state_never_persists_source_url(generation_env):
    _service, store, _pool, core, _fp = generation_env
    store.create("a" * 32, 1.0)
    store.update("a" * 32, state="generating", tiles_done=1, tiles_total=2)
    row = store.get("a" * 32)
    assert row["state"] == "generating"
    assert row["tiles_done"] == 1 and row["tiles_total"] == 2
    assert "url" not in row and "source_url" not in row
    assert "generation_jobs" in {r[0] for r in core.db.q(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def test_generation_packages_valid_nvmt_and_publishes_to_telegram(generation_env):
    service, store, pool, _core, fp = generation_env
    store.create("b" * 32, 1.0)
    job = SimpleNamespace(job_id="generation-test", fp=fp, generation_profile="nvmt-v1-g10",
                          generation_hints={"imdb": "tt1234567", "season": 1,
                                            "episode": 2, "filename": "show.mkv"})
    service.run("b" * 32, job)
    row = store.get("b" * 32)
    assert row["state"] == "complete"
    assert row["package_sha256"]
    assert len(pool.uploads) == 1
    args, kwargs = pool.uploads[0]
    assert args[:3] == (fp, "nvmt-v1-g10", 20_000)
    assert decode_nvmt(fp, "nvmt-v1-g10", 20_000, args[3])[2].keys() == {0, 1}
    assert kwargs["imdb"] == "tt1234567" and kwargs["season"] == 1


@pytest.mark.parametrize(
    ("profile", "duration_ms", "second_pts_ms", "second_slot", "expected_slots"),
    [
        ("nvmt-v1-g10", 12_000, 5_000, 1, {0, 1}),
        ("nvmt-v1-g5", 20_000, 5_000, 2, {0, 2}),
        ("nvmt-v1-g5", 20_000, 15_000, 2, {0, 2}),
    ],
)
def test_generation_run_preserves_scheduled_slots_with_tolerated_pts(
        generation_env, profile, duration_ms, second_pts_ms, second_slot, expected_slots):
    service, store, pool, core, _unused_fp = generation_env
    fp = hashlib.sha256(f"generation slot {profile} {second_pts_ms}".encode()).hexdigest()
    second_jpeg = jpeg((40, 50, 60))
    core.cache.set_media(fp, duration_ms, 2)
    core.cache.put_tile(fp, 0, 0, jpeg())
    core.cache.put_tile(fp, 1, second_pts_ms, second_jpeg)
    core.jobs.sources[fp] = SimpleNamespace(size=12345, touch=lambda: None)
    generation_id = "c" * 32
    store.create(generation_id, 1.0)
    job = SimpleNamespace(
        job_id="generation-offset-test", fp=fp, generation_profile=profile,
        generation_hints={"imdb": "tt1234567", "season": 1,
                          "episode": 2, "filename": "show.mkv"})

    service.run(generation_id, job)

    row = store.get(generation_id)
    assert row["state"] == "complete"
    assert row["tiles_done"] == len(expected_slots)
    assert len(pool.uploads) == 1
    args, _kwargs = pool.uploads[0]
    assert args[:3] == (fp, profile, duration_ms)
    _grid, _slot_count, tiles = decode_nvmt(fp, profile, duration_ms, args[3])
    assert set(tiles) == expected_slots
    assert tiles[second_slot] == (second_pts_ms, second_jpeg)


def test_invalid_generation_id_is_not_looked_up(generation_env):
    service, _store, _pool, _core, _fp = generation_env
    assert service.status("../../etc/passwd") is None


@pytest.mark.parametrize("source_url", [
    "https://127.0.0.1:9/",
    "https://169.254.169.254/latest/meta-data/",
    "https://[::1]:9/",
    "file:///etc/passwd",
])
def test_generation_endpoint_reports_destination_policy_refusals(tmp_path, source_url):
    channel = MemoryChannel()
    settings = Settings(
        data_dir=tmp_path / "generation-endpoint",
        admin_port=8788,
        port=8787,
        admin_token="synthetic-generation-admin-token",
        pool_mode="channel",
        telegram_api_id=123,
        telegram_api_hash="a" * 32,
        telegram_bot_token="123456:test",
        telegram_channel_id=channel.channel_id,
    )
    # Port 9 is allowed here so loopback denials prove the address policy rejected
    # the destination rather than merely disallowing that port.
    core = Core(settings, policy=DestinationPolicy(allowed_ports=(9, 443)), pool_storage=channel)

    async def exercise():
        admin_transport = httpx.ASGITransport(app=create_admin_app(core), client=("127.0.0.1", 45000))
        api_transport = httpx.ASGITransport(app=create_api_app(core), client=("127.0.0.1", 45001))
        async with (httpx.AsyncClient(transport=admin_transport,
                                      base_url="http://127.0.0.1:8788") as admin,
                    httpx.AsyncClient(transport=api_transport,
                                      base_url="https://127.0.0.1:8787") as api):
            rejected = await admin.post(
                "/admin/generations",
                headers={"X-OpenSeek-Admin": core.admin_token},
                json={"source_url": source_url, "profile": "nvmt-v1-g10"},
            )
            health = await api.get("/v1/health")
        return rejected, health

    try:
        rejected, health = asyncio.run(exercise())
        assert rejected.status_code == 400
        assert rejected.json() == {"error": "source_forbidden"}
        assert health.status_code == 200
        assert core.generations.active == {}
        assert core.generations.store.active_count() == 0
        rows = core.generations.recent()
        assert len(rows) == 1
        assert rows[0]["state"] == "failed" and rows[0]["error"] == "source_forbidden"
        assert channel.messages == []
    finally:
        core.stop()
