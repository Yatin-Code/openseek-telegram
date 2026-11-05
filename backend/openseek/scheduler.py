"""Bounded, resumable scheduling for dashboard NVMT extraction."""
from __future__ import annotations

from dataclasses import dataclass

GRID_MS = 10_000


@dataclass(frozen=True)
class Task:
    k: int

    @property
    def t_ms(self) -> int:
        return self.k * GRID_MS


def tile_count(duration_ms: int) -> int:
    """Number of ten-second tiles whose timestamp is before the media duration."""
    if duration_ms <= 0:
        return 0
    return (duration_ms - 1) // GRID_MS + 1


class Scheduler:
    """Schedule each missing tile once, retry transient failures, and resume from cached tiles."""

    def __init__(self, n_tiles: int, done: set[int] | None = None, max_inflight: int = 1):
        self.n = max(0, int(n_tiles))
        self.max_inflight = max(1, int(max_inflight))
        self.done = {k for k in (done or ()) if 0 <= k < self.n}
        self.failed: set[int] = set()
        self.inflight_tasks: dict[int, Task] = {}

    @property
    def inflight(self) -> Task | None:
        return next(iter(self.inflight_tasks.values()), None)

    @property
    def complete_all(self) -> bool:
        return self.n > 0 and len(self.done | self.failed) >= self.n

    def mark_missing(self, k: int) -> None:
        """Make a tile eligible again after its cached bytes are lost."""
        self.done.discard(k)
        self.failed.discard(k)

    def _pending(self, k: int) -> bool:
        return k not in self.done and k not in self.failed and k not in self.inflight_tasks

    def _next(self) -> Task | None:
        for k in range(self.n):
            if self._pending(k):
                return Task(k)
        return None

    def next(self) -> Task | None:
        if len(self.inflight_tasks) >= self.max_inflight:
            return None
        task = self._next()
        if task is not None:
            self.inflight_tasks[task.k] = task
        return task

    def lookahead(self, n: int) -> list[Task]:
        """Return upcoming unclaimed tiles for multi-range byte prefetch."""
        picked: list[Task] = []
        try:
            for _ in range(max(0, int(n))):
                task = self._next()
                if task is None:
                    break
                picked.append(task)
                self.inflight_tasks[task.k] = task
        finally:
            for task in picked:
                self.inflight_tasks.pop(task.k, None)
        return picked

    def complete(self, task: Task, ok: bool, permanent_failure: bool = False) -> None:
        self.inflight_tasks.pop(task.k, None)
        if ok:
            self.done.add(task.k)
            self.failed.discard(task.k)
        elif permanent_failure:
            self.failed.add(task.k)
