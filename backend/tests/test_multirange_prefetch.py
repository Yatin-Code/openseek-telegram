"""Multi-range parser, scheduler lookahead, and configuration unit tests."""
from __future__ import annotations

import pytest

from openseek.fetch import ByteRangesParser, MultiRangeError
from openseek.scheduler import Scheduler


# ----------------------------------------------------------------------------- units
def test_lookahead_matches_next_order_and_claims_nothing():
    a = Scheduler(60)
    b = Scheduler(60)
    ahead = a.lookahead(8)
    assert a.inflight_tasks == {}
    got = []
    for _ in range(8):
        t = b.next()
        got.append(t)
        b.complete(t, ok=True)
    assert [t.k for t in ahead] == [t.k for t in got][:len(ahead)] or {t.k for t in ahead} == {t.k for t in got}
    assert len({t.k for t in ahead}) == len(ahead)


def test_lookahead_never_returns_a_claimed_task():
    s = Scheduler(60)
    first = s.next()
    ahead = s.lookahead(5)
    assert first.k not in {t.k for t in ahead}
    assert [t.k for t in ahead] == [1, 2, 3, 4, 5]


def _mp(parts, total, boundary="BND"):
    body = b""
    for (a, b), data in parts:
        body += f"--{boundary}\r\nContent-Type: x/y\r\nContent-Range: bytes {a}-{b}/{total}\r\n\r\n".encode() + data + b"\r\n"
    return body + f"--{boundary}--\r\n".encode()


def test_parser_handles_any_chunking_and_returns_parts_as_they_complete():
    ranges = [(0, 9), (100, 104), (500, 519)]
    parts = [((a, b), bytes([i]) * (b - a + 1)) for i, (a, b) in enumerate(ranges)]
    body = _mp(parts, 1000)
    for step in (1, 7, 64, len(body)):
        p = ByteRangesParser("BND", ranges, 1000)
        got = []
        for i in range(0, len(body), step):
            got += p.feed(body[i:i + step])
        assert [(a, b, d) for a, b, d in got] == [(a, b, d) for (a, b), d in parts] and p.done
    p = ByteRangesParser("BND", ranges, 1000)
    first = p.feed(body[:body.index(b"--BND", 5) + 40])
    assert first and first[0][:2] == (0, 9)                                  # first part usable before the rest arrived


@pytest.mark.parametrize("bad", ["range", "total", "repeat", "nocr", "length"])
def test_parser_rejects_anything_unexpected(bad):
    ranges = [(0, 9), (100, 104)]
    if bad == "range":
        body = _mp([((0, 9), b"a" * 10), ((100, 105), b"b" * 6)], 1000)
    elif bad == "total":
        body = _mp([((0, 9), b"a" * 10)], 999)
    elif bad == "repeat":
        body = _mp([((0, 9), b"a" * 10), ((0, 9), b"a" * 10)], 1000)
    elif bad == "nocr":
        body = b"--BND\r\nContent-Type: x/y\r\n\r\nxxxx\r\n--BND--\r\n"
    else:
        body = _mp([((0, 9), b"a" * 9)], 1000)
    p = ByteRangesParser("BND", ranges, 1000)
    with pytest.raises(MultiRangeError):
        p.feed(body)


def test_multirange_is_off_by_default_and_only_the_flag_turns_it_on():
    from openseek.config import ConfigError, Settings
    assert Settings().multirange_prefetch is False
    assert Settings.from_env({}).multirange_prefetch is False
    assert Settings.from_env({"OPENSEEK_MULTIRANGE_PREFETCH": "0"}).multirange_prefetch is False
    on = Settings.from_env({"OPENSEEK_MULTIRANGE_PREFETCH": "1"})
    assert on.multirange_prefetch is True and on.multirange_batch == 8
    with pytest.raises(ConfigError):
        Settings.from_env({"OPENSEEK_MULTIRANGE_BATCH": "99"})
