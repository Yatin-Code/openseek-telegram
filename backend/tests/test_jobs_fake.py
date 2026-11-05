"""Dashboard extraction runner: accurate frames, retries, limits, cancellation and cache recovery."""
from __future__ import annotations

import io
import threading
import time

import pytest
from PIL import Image

from helpers import make_policy, wait_for
from openseek.cache import Cache
from openseek.config import Settings
from openseek.db import DB
from openseek.fetch import RangeProxy
from openseek.jobs import Busy, JobManager
from openseek.worker import GrabResult, OpenResult, WorkerError

HOST = "media.example.com"


def _jpeg() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (320, 180), (9, 9, 9)).save(b, "JPEG")
    return b.getvalue()


JPEG = _jpeg()


class FakeEngine:
    """Keyframes every `gop_ms`; keyframe mode returns the keyframe at/before t."""

    def __init__(self, duration_ms=3_600_000, gop_ms=2_000, delay=0.01, crash_on=None, log=None):
        self.duration_ms, self.gop_ms, self.delay = duration_ms, gop_ms, delay
        self.crash_on = crash_on if crash_on is not None else {}
        self.opened = False
        self.log = log if log is not None else []
        self.kills = 0

    def is_open(self):
        return self.opened

    def open(self):
        self.opened = True
        return OpenResult(self.duration_ms, 640, 360, "h264", False)

    def grab(self, t_ms, accurate):
        time.sleep(self.delay)
        self.log.append((t_ms, accurate))
        if self.crash_on.get(t_ms, 0) > 0:
            self.crash_on[t_ms] -= 1
            self.opened = False
            raise WorkerError("crash", "eof", transient=True)
        pts = t_ms if accurate else (t_ms // self.gop_ms) * self.gop_ms
        return GrabResult(pts, JPEG)

    def kill(self):
        self.kills += 1
        self.opened = False

    def close(self):
        self.opened = False


@pytest.fixture()
def mgr_factory(origin, tmp_path):
    made = []

    def make(engine_kw=None, **settings_kw):
        db = DB(tmp_path / f"m{len(made)}.db")
        cache = Cache(tmp_path / f"d{len(made)}", db, 1 << 30, 30)
        proxy = RangeProxy().start()
        s = Settings().with_(**{"create_wait_s": 5.0, **settings_kw})
        engines = []

        def factory(src):
            e = FakeEngine(**(engine_kw or {}))
            engines.append(e)
            return e

        m = JobManager(s, make_policy([origin], {HOST: ["127.0.0.1"]}), cache, proxy, engine_factory=factory)
        m.engines = engines
        m.start()
        made.append((m, proxy, db))
        return m

    yield make
    for m, proxy, db in made:
        m.shutdown()
        proxy.stop()
        db.close()


def url(origin, name):
    return f"http://{HOST}:{origin.port}/f/{name}"


def src_of(m, job):
    return m.sources[job.fp]


def test_keyframe_out_of_tolerance_reextracts_accurately(origin, mgr_factory):
    m = mgr_factory({"gop_ms": 12_000, "delay": 0.0})
    job = m.create("generation-accurate", url(origin, "main.mkv"), {})
    src = src_of(m, job)
    wait_for(lambda: any(e.k == 1 for e in src.events))
    ev = next(e for e in src.events if e.k == 1)
    assert ev.accurate and ev.pts_ms == 10_000
    ev0 = next(e for e in src.events if e.k == 0)
    assert not ev0.accurate and ev0.pts_ms == 0
    tiles = m.cache.entry(job.fp).tiles
    assert all(abs(p - k * 10_000) <= 5_000 for k, p in tiles.items())


def test_worker_crash_recovery(origin, mgr_factory):
    m = mgr_factory({"crash_on": {0: 2}, "delay": 0.0})
    job = m.create("generation-retry", url(origin, "main.mkv"), {})
    src = src_of(m, job)
    wait_for(lambda: any(e.k == 0 for e in src.events), timeout=20)
    assert src.restarts >= 2 and src.error is None
    assert m.state(job)[0] in ("ready", "complete")


def test_persistent_crash_fails_generation(origin, mgr_factory):
    m = mgr_factory({"crash_on": {k * 10_000: 99 for k in range(400)}, "delay": 0.0})
    job = m.create("generation-crash", url(origin, "main.mkv"), {})
    wait_for(lambda: m.state(job)[0] == "failed", timeout=60)
    assert m.state(job) == ("failed", "internal")


@pytest.mark.parametrize("duration_ms", [0, 12 * 60 * 60 * 1000 + 1])
def test_invalid_duration_fails_generation(origin, mgr_factory, duration_ms):
    m = mgr_factory({"duration_ms": duration_ms, "delay": 0})
    job = m.create(f"generation-bad-duration-{duration_ms}", url(origin, "main.mkv"), {})
    wait_for(lambda: m.state(job)[0] == "failed")
    assert m.state(job) == ("failed", "bad_duration")


def test_global_source_cap_rejects_another_generation(origin, mgr_factory):
    m = mgr_factory({"delay": 0.05}, max_active_sources=1)
    first = m.create("generation-one", url(origin, "main.mkv"), {})
    wait_for(lambda: src_of(m, first).running)
    with pytest.raises(Busy):
        m.create("generation-two", url(origin, "another.mp4"), {})


def test_delete_stops_worker_and_keeps_cached_tiles(origin, mgr_factory):
    m = mgr_factory({"delay": 0.05})
    job = m.create("generation-cancel", url(origin, "main.mkv"), {})
    src = src_of(m, job)
    wait_for(lambda: len(src.events) >= 2)
    m.delete(job)
    assert not src.running and job.job_id not in m.jobs
    n = len(m.cache.entry(job.fp).tiles)
    time.sleep(0.3)
    assert n >= 2 and len(m.cache.entry(job.fp).tiles) == n


def test_idle_timeout_stops_stale_generation_worker(origin, mgr_factory):
    m = mgr_factory({"delay": 0.05}, idle_timeout_s=0.5)
    job = m.create("generation-idle", url(origin, "main.mkv"), {})
    src = src_of(m, job)
    wait_for(lambda: src.running)
    wait_for(lambda: not src.running, timeout=10)
    assert src.error is None


def test_preflight_errors_become_generation_errors(origin, mgr_factory):
    m = mgr_factory()
    origin.reset(range=False)
    assert m.state(m.create("bad-range", url(origin, "main.mkv"), {})) == ("failed", "range_unsupported")
    origin.reset()
    assert m.state(m.create("no-cues", url(origin, "nocues.mkv"), {})) == ("failed", "no_index")
    assert m.state(m.create("not-video", url(origin, "random.bin"), {})) == ("failed", "not_video")
    assert m.state(m.create("unreachable", url(origin, "nope.mkv"), {})) == ("failed", "source_unreachable")
    origin.reset(expired=True)
    assert m.state(m.create("expired", url(origin, "main.mkv"), {})) == ("failed", "source_expired")
    assert not m.sources


def test_lost_tile_while_decoder_closes_is_regenerated(origin, mgr_factory):
    m = mgr_factory({"duration_ms": 10_000, "delay": 0})
    closing, release = threading.Event(), threading.Event()
    factory = m.engine_factory

    def delayed_close(src):
        engine = factory(src)
        original = engine.close
        if len(m.engines) == 1:
            def close():
                closing.set()
                release.wait(timeout=5)
                original()
            engine.close = close
        return engine

    m.engine_factory = delayed_close
    try:
        job = m.create("generation-recover", url(origin, "main.mkv"), {})
        src = src_of(m, job)
        assert closing.wait(timeout=5)
        m.cache.drop_tile(job.fp, 0)
        wait_for(lambda: 0 not in src.scheduler.done)
    finally:
        release.set()
    wait_for(lambda: len(src.events) >= 2)
    assert 0 in m.cache.entry(job.fp).tiles
