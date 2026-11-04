"""Dashboard-only NVMT generation state, packaging, and publication tests."""
from __future__ import annotations

import hashlib
import threading
from types import SimpleNamespace

import pytest

from openseek.cache import Cache
from openseek.db import DB
from openseek.generation import GenerationService, GenerationStateStore
from openseek.pool import decode_nvmt
from test_pool_channel import jpeg


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


def test_invalid_generation_id_is_not_looked_up(generation_env):
    service, _store, _pool, _core, _fp = generation_env
    assert service.status("../../etc/passwd") is None
