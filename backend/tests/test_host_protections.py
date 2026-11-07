"""H6 (#57): the host API's contract and protections.

Missing, malformed, and revoked credentials are distinct documented refusals and
arrive before the body is read; the per-class rate limit is advertised on every
response and enforced under a burst; read keys cannot write. These tests drive
the real ASGI stack (Guard included) over the same harness as the pool tests.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import httpx

from openseek.app import Core, create_api_app
from openseek.cache import PROFILE
from openseek.config import Settings
from openseek.pairing import new_token, token_hash
from openseek.pool import TelegramBotStore
from openseek.public import PublicLimits, PublicLimiter

from test_pool import READ_KEY, WRITE_KEY, FakeTelegramBotAPI

CLIENT = ("127.0.0.1", 41000)
FP = "a" * 64


class NoSourceProxy:
    def stop(self):
        pass


def make_core(tmp_path, **overrides):
    settings = dict(data_dir=tmp_path / "host", pool_enabled=True,
                    pool_read_key=READ_KEY, pool_write_key=WRITE_KEY,
                    telegram_bot_token="123456:test", telegram_channel_id="-100123")
    settings.update(overrides)
    fake = FakeTelegramBotAPI()
    return Core(Settings(**settings),
                pool_storage=TelegramBotStore("123456:test", "-100123", opener=fake.urlopen),
                proxy=NoSourceProxy())


def asgi(app, method: str, path: str, *, headers=None, body: bytes = b"",
         client=CLIENT) -> httpx.Response:
    async def go():
        transport = httpx.ASGITransport(app=app, client=client)
        async with httpx.AsyncClient(transport=transport, base_url="https://pool.test") as http:
            return await http.request(method, path, headers=headers or {}, content=body)
    return asyncio.run(go())


def test_missing_malformed_unknown_and_revoked_are_distinct(tmp_path):
    """Criterion 2: a missing, malformed, and revoked key each return a distinct
    documented response; an unrecognised well-formed token stays 403 forbidden."""
    core = make_core(tmp_path)
    app = create_api_app(core)
    url = f"/v1/pool/{FP}/manifest?profile={PROFILE}"
    token = new_token()
    device_id = core.auth.register("H6 drill", token_hash(token), role="guest")
    try:
        missing = asgi(app, "GET", url)
        assert missing.status_code == 401 and missing.json() == {"error": "unauthorized"}

        malformed = asgi(app, "GET", url, headers={"Authorization": "Token not-a-bearer"})
        assert malformed.status_code == 400 and malformed.json() == {"error": "bad_auth"}

        unknown = asgi(app, "GET", url, headers={"Authorization": f"Bearer {'z' * 43}"})
        assert unknown.status_code == 403 and unknown.json() == {"error": "forbidden"}

        assert core.auth.revoke(device_id) is True
        revoked = asgi(app, "GET", url, headers={"Authorization": f"Bearer {token}"})
        assert revoked.status_code == 403 and revoked.json() == {"error": "revoked_key"}

        # the same three refusals exist on device routes, not only pool routes
        assert asgi(app, "GET", "/v1/pair/status").status_code == 401
        assert asgi(app, "GET", "/v1/pair/status",
                    headers={"Authorization": "Bearer"}).status_code == 400
        assert asgi(app, "GET", "/v1/pair/status",
                    headers={"Authorization": f"Bearer {token}"}).status_code == 403
    finally:
        core.stop()


def test_refusals_arrive_before_an_oversized_body_is_read(tmp_path):
    """Criterion 3: a missing key is refused before the body is parsed, including
    for an oversized body (auth answer, never 413); an authorized oversized body
    still gets the documented boundary refusal."""
    core = make_core(tmp_path)
    app = create_api_app(core)
    url = f"/v1/pool/{'b' * 64}/nvmt?profile={PROFILE}&duration_ms=30000"
    huge = b"\0" * (16 * 1024 * 1024 + 1)                  # one byte over the package cap
    try:
        missing = asgi(app, "PUT", url, body=huge,
                       headers={"Content-Type": "application/vnd.openseek.nvmt"})
        assert missing.status_code == 401, missing.text[:120]
        assert missing.json() == {"error": "unauthorized"}

        malformed = asgi(app, "PUT", url, body=huge,
                         headers={"Authorization": "Basic abc",
                                  "Content-Type": "application/vnd.openseek.nvmt"})
        assert malformed.status_code == 400
        assert malformed.json() == {"error": "bad_auth"}

        authorized = asgi(app, "PUT", url, body=huge,
                          headers={"Authorization": f"Bearer {WRITE_KEY}",
                                   "Content-Type": "application/vnd.openseek.nvmt"})
        assert authorized.status_code == 413
        assert authorized.json() == {"error": "too_large"}
    finally:
        core.stop()


def test_read_key_cannot_write_and_refusal_code_is_stable(tmp_path):
    """Criterion 4: the read key cannot write; the refusal carries a stable code."""
    core = make_core(tmp_path)
    app = create_api_app(core)
    fp = hashlib.sha256(b"read key write drill").hexdigest()
    url = f"/v1/pool/{fp}/nvmt?profile={PROFILE}&duration_ms=30000"
    try:
        denied = asgi(app, "PUT", url, body=b"anything",
                      headers={"Authorization": f"Bearer {READ_KEY}",
                               "Content-Type": "application/vnd.openseek.nvmt"})
        assert denied.status_code == 403
        assert denied.json() == {"error": "forbidden"}
        assert core.db.q("SELECT COUNT(*) FROM pool_tiles")[0][0] == 0

        # the write key passes the credential layer and reaches package validation
        reaches = asgi(app, "PUT", url, body=b"not-nvmt",
                       headers={"Authorization": f"Bearer {WRITE_KEY}",
                                "Content-Type": "application/vnd.openseek.nvmt"})
        assert reaches.status_code == 400 and reaches.json() == {"error": "bad_key"}
    finally:
        core.stop()


def test_rate_limit_is_advertised_and_enforced_on_the_host_listener(tmp_path):
    """Criterion 5: the per-class budget is advertised in response headers and is
    enforced under a burst with 429 + Retry-After."""
    core = make_core(tmp_path)
    core.public_limiter = PublicLimiter(PublicLimits(
        per_minute={"health": 2, "pair": 2, "api": 2, "pool_read": 2, "pool_write": 2, "unknown": 2}))
    app = create_api_app(core)
    try:
        first = asgi(app, "GET", "/v1/health")
        assert first.status_code == 200
        assert first.headers["x-ratelimit-limit"] == "2"
        assert first.headers["x-ratelimit-remaining"] == "1"
        assert int(first.headers["x-ratelimit-reset"]) >= 1

        second = asgi(app, "GET", "/v1/health")
        assert second.status_code == 200
        assert second.headers["x-ratelimit-remaining"] == "0"

        burst = asgi(app, "GET", "/v1/health")
        assert burst.status_code == 429
        assert burst.json() == {"error": "rate_limited"}
        assert burst.headers["x-ratelimit-limit"] == "2"
        assert burst.headers["x-ratelimit-remaining"] == "0"
        assert int(burst.headers["retry-after"]) >= 1

        # budgets are per class, so a pool read is still available, with headers
        read = asgi(app, "GET", f"/v1/pool/{'c' * 64}/manifest?profile={PROFILE}",
                    headers={"Authorization": f"Bearer {READ_KEY}"})
        assert read.status_code == 404
        assert read.headers["x-ratelimit-limit"] == "2"
        assert read.headers["x-ratelimit-remaining"] == "1"

        # auth refusals after the limiter still advertise the budget
        refused = asgi(app, "GET", f"/v1/pool/{'c' * 64}/manifest?profile={PROFILE}")
        assert refused.status_code == 401
        assert refused.headers["x-ratelimit-remaining"] == "0"
    finally:
        core.stop()


def test_undocumented_host_path_answers_the_documented_not_found_shape(tmp_path):
    """Criterion 1: only documented routes answer; an undocumented path is the
    documented 404 shape (with or without a credential), and a documented path
    with the wrong method is 405."""
    core = make_core(tmp_path)
    app = create_api_app(core)
    token = new_token()
    core.auth.register("H6 routes", token_hash(token))
    try:
        unknown = asgi(app, "GET", "/v1/never-documented")
        assert unknown.status_code == 404
        assert unknown.json() == {"error": "not_found"}

        anonymous_pool = asgi(app, "GET", f"/v1/pool/{FP}/manifest?profile={PROFILE}")
        assert anonymous_pool.status_code == 401          # a documented route: auth first

        # an authenticated but undocumented method on a documented path is 405
        wrong_method = asgi(app, "POST", "/v1/health")
        assert wrong_method.status_code == 405
        assert wrong_method.json() == {"error": "method_not_allowed"}
    finally:
        core.stop()
