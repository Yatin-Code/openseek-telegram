"""Dashboard NVMT extraction jobs and the per-source runner.

* A **job** is one explicit dashboard generation request.
* A **source** is one media fingerprint. Cached tiles can be reused across generation jobs;
  a source owns bounded worker subprocesses and in-flight frames.
* The runner thread of a source pulls tasks from its `Scheduler`, drives the worker, applies
  the ±tolerance rule (keyframe first; accurate re-extract if the keyframe is too far), publishes
  tiles to the cache, and recovers from worker crashes/timeouts with backoff.
"""
from __future__ import annotations

import concurrent.futures
import bisect
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .cache import Cache
from .config import Settings
from .fetch import Budget, Preflight, ProxySource, RangeProxy, TokenBucket, Upstream, UpstreamError
from .netpolicy import Denied, DestinationPolicy
from .pool import MAX_DURATION_MS
from .scheduler import Scheduler, Task, tile_count
from .worker import WorkerEngine, WorkerError

log = logging.getLogger("openseek.jobs")

FATAL_UPSTREAM = {"source_expired", "budget_exceeded", "range_unsupported", "source_forbidden"}
MAX_CRASHES = 6               # consecutive worker crashes/timeouts before the source fails
MAX_TILE_FAILURES = 3         # failures of one tile before it is skipped permanently


class Busy(Exception):
    pass


class SourceForbidden(Exception):
    pass


@dataclass
class Job:
    job_id: str
    fp: str | None = None
    error: str | None = None
    deleted: bool = False


@dataclass
class TileEvent:
    k: int
    pts_ms: int
    at: float
    accurate: bool
    seconds: float
    requests: int = 0
    bytes: int = 0
    keyframe_seconds: float = 0.0
    accurate_seconds: float = 0.0
    throttle_seconds: float = 0.0


class Source:
    def __init__(self, mgr: "JobManager", fp: str):
        self.mgr = mgr
        self.fp = fp
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.upstream: Upstream | None = None
        self.proxy_src: ProxySource | None = None
        self.proxy_cap: str | None = None
        self.fmt: str | None = None
        self.size: int | None = None
        self.scheduler: Scheduler | None = None
        self.kf_pts: list[int] = []                # multi-range prefetch: keyframe times (ms) ...
        self.kf_pos: list[int] = []                # ... and their byte positions, from the container index
        self.kf_block_starts: set[int] = set()
        self.mr_blocks = self.MR_WINDOW_BLOCKS
        self.error: str | None = None
        self.jobs: set[str] = set()
        self.thread: threading.Thread | None = None
        self.extra_threads: list[threading.Thread] = []
        self.stop_flag = False
        self.n_workers = max(1, int(mgr.settings.workers_per_source))
        self.engines: list = [None] * self.n_workers
        self.active_tasks: dict[int, Task] = {}
        self.background = False
        self.last_activity = mgr.clock()
        self.events: list[TileEvent] = []
        self.restarts = 0
        self._tile_failures: dict[int, int] = {}
        self._crashes = 0
        self._unreachable = 0
        e = mgr.cache.entry(fp)
        if e is not None and e.n_tiles:
            self.scheduler = mgr.new_scheduler(e.n_tiles, set(e.tiles))

    # ---------------------------------------------------------------- state
    @property
    def running(self) -> bool:
        return (self.thread is not None and self.thread.is_alive()) or any(
            t.is_alive() for t in self.extra_threads
        )

    def touch(self) -> None:
        self.last_activity = self.mgr.clock()

    def _sync_background(self) -> None:
        self.background = bool(self.active_tasks)

    # ---------------------------------------------------------------- multi-range prefetch (default off)
    MR_BLOCK = 256 * 1024
    MR_WINDOW_BLOCKS = 5          # default: covers the reader's 4- and 5-block first reads on high-bitrate files

    def _attach_planner(self) -> None:
        ps = self.proxy_src
        if ps is None or not self.mgr.settings.multirange_prefetch or not self.kf_pts:
            return
        ps.planner = self._plan_windows
        ps.cancelled = lambda: self.stop_flag

    def _load_kf_index(self, eng, duration_ms: int = 0) -> None:
        """Worker 0 reads the container's keyframe index once (it loads the Cues; one extra seek in the worker)."""
        if not self.mgr.settings.multirange_prefetch or self.kf_pts:
            return
        if duration_ms and self.size:                      # the reader's first read: seek_blocks (+1 past half a block)
            bps = self.size / (duration_ms / 1000.0)
            self.mr_blocks = 5 if bps >= 2_500_000 else 3 if bps >= 200_000 else 2
        else:
            self.mr_blocks = self.MR_WINDOW_BLOCKS
        try:
            idx = eng.keyframe_index()
        except (WorkerError, AttributeError):
            return
        if len(idx) < 2:
            return
        with self.cond:
            self.kf_pts = [t for t, _ in idx]
            self.kf_pos = [p for _, p in idx]
            self.kf_block_starts = {(p // self.MR_BLOCK) * self.MR_BLOCK for p in self.kf_pos}
            self._attach_planner()

    def _window_for(self, t_ms: int) -> tuple[int, int] | None:
        """The bytes the reader will ask for first when it seeks to the keyframe at or before t_ms."""
        i = bisect.bisect_right(self.kf_pts, t_ms) - 1
        if i < 0 or self.size is None:
            return None
        start = (self.kf_pos[i] // self.MR_BLOCK) * self.MR_BLOCK
        end = min(self.size, start + self.mr_blocks * self.MR_BLOCK) - 1
        return (start, end) if end >= start else None

    def _plan_windows(self, start: int, end: int) -> list[tuple[int, int]]:
        """Called by the proxy for a background keyframe read: the windows of the next tiles worth fetching with it."""
        if start not in self.kf_block_starts or self.scheduler is None or self.proxy_src is None:
            return []
        n_extra = self.mgr.settings.multirange_batch - 1
        with self.cond:
            tasks = self.scheduler.lookahead(n_extra + 2)
        out: list[tuple[int, int]] = []
        for t in tasks:
            if len(out) >= n_extra:
                continue
            w = self._window_for(t.t_ms)
            if w is None or w[0] <= end and start <= w[1]:
                continue
            if not self.proxy_src.has_or_pending(*w) and w not in out:
                out.append(w)
        return out

    def offer_upstream(self, up: Upstream, pre: Preflight) -> bool:
        """Adopt a freshly preflighted upstream unless a runner is already using a working one."""
        with self.lock:
            if self.upstream is not None and self.running:
                return False
            old_cap, old_up = self.proxy_cap, self.upstream
            self.upstream, self.fmt, self.size = up, pre.fmt, pre.size
            self.proxy_src = ProxySource(up, pre.size, [(0, pre.head), (pre.tail_offset, pre.tail)],
                                         background=lambda: self.background,
                                         bucket=self.mgr.bucket,
                                         shared_cache=((self.n_workers > 1 or self.mgr.settings.multirange_prefetch)
                                                       and pre.size >= 16 * 1024 * 1024))
            self._attach_planner()
            self.proxy_cap = self.mgr.proxy.register(self.proxy_src)
            if self.error in ("source_expired", "source_unreachable"):
                self.error = None      # a fresh link revives a source whose link had expired
        if old_cap:
            self.mgr.proxy.unregister(old_cap)
        if old_up is not None:
            old_up.close()
        return True

    def tile_lost(self, k: int) -> None:
        with self.cond:
            if self.scheduler is not None:
                self.scheduler.mark_missing(k)
            self.cond.notify_all()

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        with self.lock:
            if self.running or self.error is not None or self.upstream is None:
                return False
            if self.scheduler is not None and self.scheduler.complete_all:
                return False
            self.stop_flag = False
            self._crashes = 0
            self._unreachable = 0
            self.extra_threads = []
            self.thread = threading.Thread(target=self._run, args=(0,),
                                           name=f"openseek-src-{self.fp[:8]}-0", daemon=True)
            self.thread.start()
            return True

    def _ensure_extra_workers(self) -> None:
        with self.cond:
            if self.n_workers <= 1 or self.stop_flag or self.error is not None:
                return
            if self.scheduler is None or self.scheduler.complete_all or self.scheduler.n <= 1:
                return
            alive_extra = [t for t in self.extra_threads if t.is_alive()]
            if alive_extra:
                return
            self.extra_threads = []
            for idx in range(1, self.n_workers):
                t = threading.Thread(target=self._run, args=(idx,),
                                     name=f"openseek-src-{self.fp[:8]}-{idx}", daemon=True)
                self.extra_threads.append(t)
                t.start()

    def stop(self, reason: str, wait: bool = True) -> None:
        """Stop the runner and kill all workers immediately (in-flight frames are abandoned)."""
        with self.cond:
            self.stop_flag = True
            engs = [e for e in self.engines if e is not None]
            threads = ([self.thread] if self.thread is not None else []) + list(self.extra_threads)
            self.cond.notify_all()
        for eng in engs:
            eng.kill()
        cur = threading.current_thread()
        if wait:
            for t in threads:
                if t is not None and t is not cur:
                    t.join(timeout=10)
        if self.upstream is not None:
            self.upstream.drop_connection()
        log.info("source %s.. stopped (%s)", self.fp[:8], reason)

    def fail(self, code: str) -> None:
        with self.cond:
            if self.error is None:
                self.error = code
                log.warning("source %s.. failed: %s", self.fp[:8], code)
            self.stop_flag = True
            self.cond.notify_all()

    # ---------------------------------------------------------------- runner
    def _upstream_error(self) -> str | None:
        ps = self.proxy_src
        return ps.last_error.code if ps is not None and ps.last_error is not None else None

    def _finish(self, worker_idx: int, task: Task | None, ok: bool, permanent: bool = False) -> None:
        with self.cond:
            self.active_tasks.pop(worker_idx, None)
            self._sync_background()
            if task is not None and self.scheduler is not None:
                self.scheduler.complete(task, ok=ok, permanent_failure=permanent)
            self.cond.notify_all()

    def _next_task(self, worker_idx: int) -> tuple[bool, Task | None]:
        """(keep_running, task). Blocks briefly while there is nothing to do."""
        with self.cond:
            if self.stop_flag or self.error is not None:
                return False, None
            if self.scheduler is None:
                if worker_idx == 0:
                    return True, None                   # worker 0 opens the engine to learn duration
                self.cond.wait(timeout=0.2)
                return True, None
            task = self.scheduler.next()
            if task is None:
                if self.scheduler.complete_all:
                    return False, None
                self.cond.wait(timeout=1.0)
                return True, None
            self.active_tasks[worker_idx] = task
            self._sync_background()
            return True, task

    def _run(self, worker_idx: int = 0) -> None:
        mgr = self.mgr
        tol = mgr.settings.tolerance_ms
        try:
            if worker_idx == 0 and self.scheduler is not None:
                self._ensure_extra_workers()
            while True:
                keep, task = self._next_task(worker_idx)
                if not keep:
                    return
                if self.scheduler is not None and task is None:
                    continue
                eng = self.engines[worker_idx]
                if eng is None or not eng.is_open():
                    with self.cond:
                        crashes_now = self._crashes
                    if crashes_now:
                        with self.cond:   # restart backoff: 0.5 s, 1 s, 2 s ... capped at 8 s
                            self.cond.wait(timeout=min(8.0, 0.25 * (2 ** crashes_now)))
                    if self.stop_flag or not self._open_engine(worker_idx):
                        self._finish(worker_idx, task, ok=False)
                        if self.error is not None or self.stop_flag:
                            return
                        with self.cond:
                            self._crashes += 1
                            self.restarts += 1
                            crashes_now = self._crashes
                        if crashes_now >= MAX_CRASHES:
                            self.fail("internal")
                            return
                        continue
                    eng = self.engines[worker_idx]
                if task is None:
                    continue
                if self.stop_flag:
                    self._finish(worker_idx, task, ok=False)
                    return
                # Reuse an exact-file tile from the shared pool before decoding a
                # frame on this host. A miss or service outage falls back locally.
                if mgr.pool_client is not None:
                    shared = mgr.pool_client.get_tile(self.fp, task.k)
                    if shared is not None:
                        shared_jpeg, shared_pts = shared
                        if abs(shared_pts - task.t_ms) <= tol:
                            try:
                                mgr.cache.put_tile(self.fp, task.k, shared_pts, shared_jpeg)
                            except (ValueError, OSError):
                                pass
                            else:
                                self._finish(worker_idx, task, ok=True)
                                continue
                t0 = time.perf_counter()
                if self.proxy_src is not None:
                    self.proxy_src.last_error = None
                accurate = False
                budget = self.upstream.budget
                requests_before, bytes_before = budget.requests, budget.used
                throttle_before = self.proxy_src.throttle_seconds
                keyframe_seconds = accurate_seconds = 0.0
                worker_reqs = worker_bytes = 0
                try:
                    res = eng.grab(task.t_ms, accurate=False)
                    keyframe_seconds = time.perf_counter() - t0
                    worker_reqs += getattr(res, "requests", 0)
                    worker_bytes += getattr(res, "bytes", 0)
                    if abs(res.pts_ms - task.t_ms) > tol:
                        accurate = True     # keyframe too far from the grid point (long GOP)
                        accurate_start = time.perf_counter()
                        res = eng.grab(task.t_ms, accurate=True)
                        accurate_seconds = time.perf_counter() - accurate_start
                        worker_reqs += getattr(res, "requests", 0)
                        worker_bytes += getattr(res, "bytes", 0)
                except WorkerError as e:
                    if self.stop_flag:
                        self._finish(worker_idx, task, ok=False)
                        return
                    up = self._upstream_error()
                    if up in FATAL_UPSTREAM:
                        self._finish(worker_idx, task, ok=False)
                        self.fail(up)
                        return
                    if up == "source_unreachable":
                        with self.cond:
                            self._unreachable += 1
                            unreach_now = self._unreachable
                        if unreach_now >= 3:
                            self._finish(worker_idx, task, ok=False)
                            self.fail("source_unreachable")
                            return
                    with self.cond:
                        n = self._tile_failures.get(task.k, 0) + 1
                        self._tile_failures[task.k] = n
                    self._finish(worker_idx, task, ok=False, permanent=n >= MAX_TILE_FAILURES)
                    if e.code in ("not_video", "unsupported_codec"):
                        self.fail(e.code)
                        return
                    if e.transient:
                        with self.cond:
                            self._crashes += 1
                            self.restarts += 1
                            crashes_now = self._crashes
                        log.warning("worker %s on %s..; restarting (%d)", e.code, self.fp[:8], crashes_now)
                        if crashes_now >= MAX_CRASHES:
                            self.fail("internal")
                            return
                    continue
                with self.cond:
                    self._crashes = 0
                    self._unreachable = 0
                if abs(res.pts_ms - task.t_ms) > tol:
                    self._finish(worker_idx, task, ok=False, permanent=True)   # never publish out of tolerance
                    continue
                try:
                    mgr.cache.put_tile(self.fp, task.k, res.pts_ms, res.jpeg)
                except (ValueError, OSError) as e:
                    log.warning("could not store tile: %s", type(e).__name__)
                    self._finish(worker_idx, task, ok=False, permanent=True)
                    continue
                if mgr.pool_sync is not None:
                    mgr.pool_sync.enqueue(self.fp, task.k)
                self._finish(worker_idx, task, ok=True)
                with self.cond:
                    ev_reqs = (budget.requests - requests_before) if self.n_workers == 1 else worker_reqs
                    ev_bytes = (budget.used - bytes_before) if self.n_workers == 1 else worker_bytes
                    self.events.append(TileEvent(task.k, res.pts_ms, mgr.clock(), accurate,
                                                 time.perf_counter() - t0,
                                                 ev_reqs,
                                                 ev_bytes,
                                                 keyframe_seconds, accurate_seconds,
                                                 self.proxy_src.throttle_seconds - throttle_before))
                    event = self.events[-1]
                    log.debug("tile k=%d seconds=%.3f requests=%d received_bytes=%d "
                              "keyframe_seconds=%.3f accurate_seconds=%.3f",
                              event.k, event.seconds, event.requests, event.bytes,
                              event.keyframe_seconds, event.accurate_seconds)
                    if len(self.events) > 5000:
                        del self.events[:1000]
        except Exception:  # noqa: BLE001
            log.exception("runner crashed for %s..", self.fp[:8])
            self.fail("internal")
        finally:
            with self.cond:
                eng, self.engines[worker_idx] = self.engines[worker_idx], None
                self.active_tasks.pop(worker_idx, None)
                self._sync_background()
            if eng is not None:
                eng.close()

    def _open_engine(self, worker_idx: int = 0) -> bool:
        mgr = self.mgr
        eng = mgr.engine_factory(self)
        with self.cond:
            old, self.engines[worker_idx] = self.engines[worker_idx], eng
            if self.stop_flag:
                self.engines[worker_idx] = None
        if old is not None:
            old.close()
        if self.engines[worker_idx] is None:
            return False
        if self.proxy_src is not None:
            self.proxy_src.last_error = None
        try:
            info = eng.open()
        except WorkerError as e:
            up = self._upstream_error()
            if up in FATAL_UPSTREAM:
                self.fail(up)
            elif e.code in ("not_video", "unsupported_codec"):
                self.fail(e.code)
            elif not e.transient and up is not None:
                self.fail(up)
            eng.close()
            with self.cond:
                if self.engines[worker_idx] is eng:
                    self.engines[worker_idx] = None
            return False
        if not 0 < info.duration_ms <= MAX_DURATION_MS:
            eng.close()
            with self.cond:
                if self.engines[worker_idx] is eng:
                    self.engines[worker_idx] = None
            self.fail("bad_duration")
            return False
        n = tile_count(info.duration_ms)
        with self.cond:
            if self.scheduler is None or self.scheduler.n != n:
                e = mgr.cache.entry(self.fp)
                self.scheduler = mgr.new_scheduler(n, set(e.tiles) if e else set())
            self.cond.notify_all()
        mgr.cache.set_media(self.fp, info.duration_ms, n)
        if worker_idx == 0:
            self._load_kf_index(eng, info.duration_ms)
            self._ensure_extra_workers()
        return True


class JobManager:
    pool_client = None                # exact-file NVMT tile lookup
    pool_sync = None                  # durable JPEG tile publisher for the shared pool

    def __init__(self, settings: Settings, policy: DestinationPolicy, cache: Cache, proxy: RangeProxy,
                 engine_factory: Callable[[Source], object] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.policy = policy
        self.cache = cache
        self.proxy = proxy
        self.clock = clock
        self.bucket = TokenBucket(settings.bg_mbps * 125_000)
        self.request_bucket = (TokenBucket(settings.upstream_rps, burst=1)
                               if settings.upstream_rps else None)
        self.engine_factory = engine_factory or self._default_engine
        self.jobs: dict[str, Job] = {}
        self.sources: dict[str, Source] = {}
        self.reserved = 0
        self.lock = threading.RLock()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="openseek-preflight")
        self._stop = threading.Event()
        self._reaper = threading.Thread(target=self._reap_loop, name="openseek-reaper", daemon=True)
        self._last_evict = 0.0
        cache.on_tile_lost = self._tile_lost

    def start(self) -> None:
        self._reaper.start()

    def _plain_engine(self, src: Source):
        s = self.settings
        return WorkerEngine(self.proxy.url(src.proxy_cap), src.size, src.fmt, s.open_timeout_s, s.frame_timeout_s,
                            s.max_dimension, s.max_duration_s, s.tolerance_ms,
                            nearest_kf=s.nearest_keyframe)

    def _default_engine(self, src: Source):
        return self._plain_engine(src)

    def new_scheduler(self, n: int, done: set[int]) -> Scheduler:
        return Scheduler(n, done, max_inflight=self.settings.workers_per_source)

    def _tile_lost(self, fp: str, k: int) -> None:
        """Cache callback (may run under cache locks): hand off to a thread, then regenerate."""
        def work():
            with self.lock:
                src = self.sources.get(fp)
                if src is None:
                    return
            with src.lock:
                completed = src.scheduler is not None and src.scheduler.complete_all
                runners = ([src.thread] if src.thread is not None else []) + list(src.extra_threads)
            src.tile_lost(k)
            # A completed runner may still be closing its decoder. Starting now
            # would see running=True and lose this regeneration wakeup.
            if completed:
                cur = threading.current_thread()
                for r in runners:
                    if r is not None and r is not cur:
                        r.join(timeout=10)
            with self.lock:
                if self._stop.is_set():
                    return
                if any(job_id in self.jobs for job_id in src.jobs):
                    self._ensure_running(src, reserved_slot=True)
        threading.Thread(target=work, name="openseek-regen", daemon=True).start()

    # ---------------------------------------------------------------- capacity
    def running_sources(self) -> list[Source]:
        return [s for s in self.sources.values() if s.running]

    def forget_source(self, fp: str) -> bool:
        """Drop an idle in-memory source after its cache entry was deleted."""
        with self.lock:
            src = self.sources.get(fp)
            if src is not None and src.running:
                return False
            if src is None:
                for job_id, j in list(self.jobs.items()):
                    if j.fp == fp:
                        j.deleted = True
                        self.jobs.pop(job_id, None)
                return True
            self.sources.pop(fp, None)
            for job_id, j in list(self.jobs.items()):
                if j.fp == fp:
                    j.deleted = True
                    self.jobs.pop(job_id, None)
        if src.proxy_cap:
            self.proxy.unregister(src.proxy_cap)
        if src.upstream is not None:
            src.upstream.close()
        return True

    # ---------------------------------------------------------------- create
    def create(self, job_id: str, url: str, headers: dict[str, str]) -> Job:
        try:
            self.policy.vet(url)              # syntax + resolve + address check, before any I/O
        except Denied:
            raise SourceForbidden() from None
        with self.lock:
            if job_id in self.jobs:
                raise ValueError("duplicate job id")
            if len(self.running_sources()) + self.reserved >= self.settings.max_active_sources:
                raise Busy()
            self.reserved += 1
            job = Job(job_id)
            self.jobs[job_id] = job
        fut = self.pool.submit(self._preflight_and_attach, job, url, headers)
        try:
            fut.result(timeout=self.settings.create_wait_s)
        except concurrent.futures.TimeoutError:
            pass
        return job

    def _capture_source(self, up: Upstream, size: int) -> None:
        """Diagnostics only (default off): keep the resolved CDN link in one owner-only file; never logged."""
        p = Path(self.settings.capture_source_file)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(up.final.url())
        p.with_name(p.name + ".info.json").write_text(json.dumps({"size": size}))

    def _preflight_and_attach(self, job: Job, url: str, headers: dict[str, str]) -> None:
        up = Upstream(self.policy, url, headers,
                      Budget(self.settings.job_bytes, request_bucket=self.request_bucket),
                      self.settings.upstream_timeout_s)
        err = None
        pre = None
        try:
            pre = up.preflight()
            if pre.fmt is None:
                err = "not_video"
            elif pre.fmt == "matroska" and not pre.has_index:
                err = "no_index"
            if err is None and self.settings.capture_source_file and up.final is not None:
                self._capture_source(up, pre.size)
        except UpstreamError as e:
            err = e.code if e.code != "internal" else "internal"
            # Reasons are internal fixed codes; never print arbitrary exception text.
            safe_reason = e.reason if e.reason.replace("_", "").isalnum() else "other"
            log.warning("preflight failed code=%s reason=%s", err, safe_reason)
        except Exception:  # noqa: BLE001
            log.exception("preflight crashed")
            err = "internal"
        with self.lock:
            self.reserved -= 1
            if err is not None or job.deleted:
                up.close()
                job.error = err if not job.deleted else job.error
                return
            fp = pre.fp
            job.fp = fp
            src = self.sources.get(fp)
            if src is None:
                self.cache.ensure(fp)
                src = Source(self, fp)
                self.sources[fp] = src
            src.jobs.add(job.job_id)
            if not src.offer_upstream(up, pre):
                up.close()
            src.touch()
            self._ensure_running(src, reserved_slot=True)

    def _ensure_running(self, src: Source, reserved_slot: bool = False) -> None:
        if src.running or src.error is not None:
            return
        running = [s for s in self.running_sources() if not s.stop_flag]
        if not reserved_slot and len(running) + self.reserved >= self.settings.max_active_sources:
            return
        src.start()

    def delete(self, job: Job) -> None:
        with self.lock:
            job.deleted = True
            self.jobs.pop(job.job_id, None)
            src = self.sources.get(job.fp) if job.fp else None
            if src is not None:
                src.jobs.discard(job.job_id)
                remaining = [job_id for job_id in src.jobs if job_id in self.jobs]
            else:
                remaining = []
        if src is not None and not remaining:
            src.stop("cancel")

    # ---------------------------------------------------------------- status
    def state(self, job: Job) -> tuple[str, str | None]:
        if job.error:
            return "failed", job.error
        if job.fp is None:
            return "preparing", None
        src = self.sources.get(job.fp)
        if src is not None and src.error:
            return "failed", src.error
        e = self.cache.entry(job.fp)
        tiles = len(e.tiles) if e else 0
        n = e.n_tiles if e else None
        sch = src.scheduler if src else None
        if n and (tiles >= n or (sch is not None and sch.complete_all)):
            return "complete", None
        if tiles:
            return "ready", None
        return "preparing", None

    def snapshot(self, job: Job) -> dict:
        state, err = self.state(job)
        e = self.cache.entry(job.fp) if job.fp else None
        return {"state": state, "error": err,
                "source_duration_ms": e.duration_ms if e else None,
                "tiles_done": len(e.tiles) if e else 0, "tiles_total": e.n_tiles if e else None}

    # ---------------------------------------------------------------- housekeeping
    def _reap_loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.reap()
            except Exception:  # noqa: BLE001
                log.exception("reaper error")

    def reap(self) -> None:
        now = self.clock()
        idle = []
        with self.lock:
            for s in self.sources.values():
                if s.running and now - s.last_activity > self.settings.idle_timeout_s:
                    idle.append(s)
        for s in idle:
            s.stop("idle")
        if now - self._last_evict > 60:
            self._last_evict = now
            with self.lock:
                protected = {fp for fp, s in self.sources.items() if s.running}
            for fp in self.cache.evict(protected):
                with self.lock:
                    s = self.sources.pop(fp, None)
                if s is not None:
                    s.stop("evicted")

    def shutdown(self) -> None:
        self._stop.set()
        with self.lock:
            srcs = list(self.sources.values())
        for s in srcs:
            s.stop("shutdown")
            if s.proxy_cap:
                self.proxy.unregister(s.proxy_cap)
            if s.upstream is not None:
                s.upstream.close()
        self.pool.shutdown(wait=False, cancel_futures=True)
