"""Full-grid tile scheduling used by dashboard NVMT generation."""
from openseek.scheduler import Scheduler, Task, tile_count


def drain(s: Scheduler, limit: int) -> list[Task]:
    out = []
    for _ in range(limit):
        task = s.next()
        if task is None:
            break
        s.complete(task, ok=True)
        out.append(task)
    return out


def test_grid_count():
    assert tile_count(7_234_000) == 724
    assert tile_count(10_000) == 1
    assert tile_count(10_001) == 2
    assert tile_count(0) == 0


def test_full_grid_is_scheduled_once_in_timestamp_order():
    scheduler = Scheduler(8)
    tasks = drain(scheduler, 20)
    assert [task.k for task in tasks] == list(range(8))
    assert scheduler.complete_all


def test_cached_tiles_are_skipped_and_lost_tiles_are_requeued():
    scheduler = Scheduler(5, done={0, 1, 2})
    assert [task.k for task in drain(scheduler, 10)] == [3, 4]
    scheduler.mark_missing(1)
    assert not scheduler.complete_all
    assert scheduler.next() == Task(1)


def test_transient_failure_retries_and_permanent_failure_finishes_slot():
    scheduler = Scheduler(3)
    first = scheduler.next()
    scheduler.complete(first, ok=False)
    retry = scheduler.next()
    assert retry == first
    scheduler.complete(retry, ok=False, permanent_failure=True)
    assert [task.k for task in drain(scheduler, 5)] == [1, 2]
    assert scheduler.complete_all


def test_concurrency_is_bounded_and_lookahead_does_not_claim_tiles():
    scheduler = Scheduler(10, max_inflight=2)
    first = scheduler.next()
    lookahead = scheduler.lookahead(4)
    assert [task.k for task in lookahead] == [1, 2, 3, 4]
    assert {task.k for task in lookahead}.isdisjoint(scheduler.inflight_tasks)
    second = scheduler.next()
    assert second.k == 1
    assert scheduler.next() is None
    scheduler.complete(first, ok=True)
    assert scheduler.next().k == 2
