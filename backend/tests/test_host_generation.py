"""Host-dashboard NVMT generation through the normal publication path (issue #78, F3).

A host-profile Core holds a PoolService that publishes validated packages, shared
tiles and hints itself, so generation must not require the MTProto channel store.
The same dashboard flow works on local/VPS (host) and Vercel; these tests drive the
owner HTTP surface with a real Core, GenerationService, JobManager and PoolService,
and only fake the third-party Bot API and the extraction engine.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from urllib.parse import urlencode

import httpx
import pytest

from helpers import make_policy, wait_for
from openseek.app import Core, create_admin_app, create_api_app
from openseek.config import Settings
from openseek.ids import is_fingerprint
from openseek.pool import (PROFILE as SHARED_TILE_PROFILE, TelegramBotStore,
                           decode_nvmt)
from openseek.worker import GrabResult
from test_jobs_fake import FakeEngine, HOST
from test_pool import FakeResponse, FakeTelegramBotAPI

READ_KEY = "pool-read-key-0000-openseek-test"
WRITE_KEY = "pool-write-key-1111-openseek-test"
ADMIN_TOKEN = "synthetic-host-admin-token"
PROFILE = "nvmt-v1-g10"
HINTS = {"imdb": "tt1234567", "season": 1, "episode": 2, "filename": "Pillar.Show.S01E02.mkv"}


class ToleratedOffsetEngine(FakeEngine):
    """Return one valid keyframe at the five-second extraction tolerance edge."""

    def grab(self, t_ms, accurate):
        result = super().grab(t_ms, accurate)
        if t_ms == 10_000 and not accurate:
            return GrabResult(5_000, result.jpeg)
        return result


def host_settings(data_dir) -> Settings:
    return Settings(data_dir=data_dir, port=8787, admin_port=8788, admin_token=ADMIN_TOKEN,
                    pool_mode="host", pool_read_key=READ_KEY, pool_write_key=WRITE_KEY,
                    telegram_bot_token="123456:test", telegram_channel_id="-100123")


async def start_generation(core, source_url: str):
    transport = httpx.ASGITransport(app=create_admin_app(core), client=("127.0.0.1", 45000))
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://127.0.0.1:8788") as admin:
        return await admin.post(
            "/admin/generations",
            headers={"X-OpenSeek-Admin": core.admin_token},
            json={"source_url": source_url, "profile": "nvmt-v1-g10"},
        )


def test_host_start_reports_source_policy_not_pool_unavailable(tmp_path):
    """Owner POST with a loopback https:443 source answers the policy refusal 400
    source_forbidden, never the pool gate 409: the host PoolService is a valid
    publication path. Port 443 is allowed by default, so the loopback address is
    what is refused; nothing is published and no job survives."""
    core = Core(host_settings(tmp_path / "host-gate"), pool_storage=object())
    try:
        response = asyncio.run(start_generation(core, "https://127.0.0.1:443/"))
        assert response.status_code == 400
        assert response.json() == {"error": "source_forbidden"}
        assert core.generations.active == {}
        assert core.generations.store.active_count() == 0
        rows = core.generations.recent()
        assert len(rows) == 1
        assert rows[0]["state"] == "failed" and rows[0]["error"] == "source_forbidden"
        assert core.jobs.jobs == {} and core.jobs.sources == {}
        assert core.pool.db.q("SELECT COUNT(*) FROM pool_packages")[0][0] == 0
        assert core.pool.db.q("SELECT COUNT(*) FROM pool_hints")[0][0] == 0
    finally:
        core.stop()


async def admin_call(core, method: str, path: str, **kw):
    transport = httpx.ASGITransport(app=create_admin_app(core), client=("127.0.0.1", 45000))
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://127.0.0.1:8788") as admin:
        return await admin.request(method, path, headers={"X-OpenSeek-Admin": core.admin_token}, **kw)


async def api_call(core, method: str, path: str, **kw):
    transport = httpx.ASGITransport(app=create_api_app(core), client=("127.0.0.1", 45001))
    async with httpx.AsyncClient(transport=transport,
                                 base_url="https://127.0.0.1:8787") as client:
        return await client.request(method, path,
                                    headers={"Authorization": f"Bearer {READ_KEY}"}, **kw)


def test_host_generation_publishes_end_to_end_and_public_surfaces_match(tmp_path, origin):
    """The full owner flow on a host install: dashboard start -> JobManager extracts ->
    PoolService publishes -> exact package, tiles and hints are publicly served and the
    Bot API fake received those files with the same digests. Only the third-party Bot
    API and the extraction engine are fakes; no mock of start/run/_publish."""
    tg = FakeTelegramBotAPI()
    core = Core(host_settings(tmp_path / "host-e2e"),
                policy=make_policy([origin], {HOST: ["127.0.0.1"]}),
                engine_factory=lambda _src: ToleratedOffsetEngine(
                    duration_ms=12_000, gop_ms=2_000, delay=0.0),
                pool_storage=TelegramBotStore("123456:test", "-100123", opener=tg.urlopen))
    try:
        started = asyncio.run(admin_call(
            core, "POST", "/admin/generations",
            json={"source_url": origin.url("/f/pillar.mkv", host=HOST), "profile": PROFILE,
                  "imdb": HINTS["imdb"], "season": HINTS["season"], "episode": HINTS["episode"],
                  "filename": HINTS["filename"]}))
        assert started.status_code == 202
        body = started.json()
        assert body["state"] == "generating"
        generation_id, fp = body["generation_id"], None
        assert len(generation_id) == 32 and int(generation_id, 16)

        def settled():
            row = asyncio.run(admin_call(core, "GET", f"/admin/generations/{generation_id}")).json()
            return row if row and row["state"] in ("complete", "failed") else None
        row = wait_for(settled)
        assert row["state"] == "complete", row
        assert row["error"] is None
        fp = row["fp"]
        assert is_fingerprint(fp)
        assert row["profile"] == PROFILE
        assert row["package_sha256"]
        assert row["tiles_total"] == 2 and row["tiles_done"] == 2
        duration_ms = row["duration_ms"]
        assert duration_ms == 12_000

        # Post-cleanup guarantees (core jobs act as usual, nothing URL-shaped remains).
        assert core.generations.active == {}
        assert core.jobs.jobs == {}
        row_json = json.dumps(row)
        assert "source_url" not in row_json and ".mkv" not in row_json

        # Public exact package: served bytes are byte-exact and hash-consistent.
        package = asyncio.run(api_call(core, "GET", f"/v1/pool/{fp}/nvmt?profile={PROFILE}"))
        assert package.status_code == 200
        assert hashlib.sha256(package.content).hexdigest() == row["package_sha256"]
        assert package.headers["X-OpenSeek-Sha256"] == row["package_sha256"]
        _grid, slots, tiles = decode_nvmt(fp, PROFILE, duration_ms, package.content)
        assert slots == 2 and sorted(tiles) == [0, 1]
        assert tiles[1][0] == 5_000
        package_bytes = package.content

        # Public exact tiles + manifest from the shared-pool index (the shared-tile
        # profile is the pool's own constant, not the NVMT package profile).
        manifest = asyncio.run(api_call(
            core, "GET", f"/v1/pool/{fp}/manifest?profile={SHARED_TILE_PROFILE}"))
        assert manifest.status_code == 200
        manifest_body = manifest.json()
        assert manifest_body["duration_ms"] == duration_ms and manifest_body["n_tiles"] == 2
        tile_rows = {t["k"]: t for t in manifest_body["tiles"]}
        assert set(tile_rows) == {0, 1}
        for k, row_k in tile_rows.items():
            tile = asyncio.run(api_call(
                core, "GET", f"/v1/pool/{fp}/tiles/{k}.jpg?profile={SHARED_TILE_PROFILE}"))
            assert tile.status_code == 200, k
            assert hashlib.sha256(tile.content).hexdigest() == row_k["sha256"]
            assert tile.content == tiles[k][1]

        # The Bot API fake received those files and digests, one tile document each.
        assert len(tg.files) == 2
        sent_digests = sorted(hashlib.sha256(v).hexdigest() for v in tg.files.values())
        assert sent_digests == sorted(t["sha256"] for t in manifest_body["tiles"])
        tile_captions = [c for c in tg.captions if c.startswith("openseek:v1:")]
        assert sorted(tile_captions) == sorted(
            f"openseek:v1:{fp}:{SHARED_TILE_PROFILE}:{k}:{tile_rows[k]['sha256']}" for k in (0, 1))

        # Hints: the canonical manifest broadcast and public candidates query.
        hint_captions = [c for c in tg.captions if c.startswith("openseek:hints:v1:")]
        assert len(hint_captions) == 1
        encoded = hint_captions[0].split(":", 3)[3]
        hint_manifest = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        assert hint_manifest["fp"] == fp and hint_manifest["profile"] == PROFILE
        assert hint_manifest["imdb"] == HINTS["imdb"]
        assert hint_manifest["season"] == 1 and hint_manifest["episode"] == 2
        assert hint_manifest["duration_ms"] == duration_ms and hint_manifest["grid"] == 10_000
        assert hint_manifest["filename"] and isinstance(hint_manifest["size_bytes"], int)
        assert hint_manifest["size_bytes"] > 0
        hint_rows = core.pool.db.q("SELECT imdb,season,episode,filename_norm FROM pool_hints")
        assert hint_rows[0][:3] == (HINTS["imdb"], 1, 2)
        assert hint_rows[0][3] == hint_manifest["filename"]
        candidates = asyncio.run(api_call(
            core, "GET", "/v1/pool/candidates?" + urlencode({**HINTS, "duration_ms": duration_ms})))
        assert candidates.status_code == 200
        candidate_rows = candidates.json()["candidates"]
        assert candidate_rows and candidate_rows[0]["fp"] == fp
        # The host kept its byte-exact package locally (never a second Telegram copy).
        assert (core.pool.package_dir / f"{fp}.{PROFILE}.nvmt").read_bytes() == package_bytes
    finally:
        core.stop()


def test_local_install_still_refuses_with_pool_unavailable(tmp_path):
    """A deployment without any pool surface keeps the documented 409 refusal and
    never creates a generation job or row."""
    core = Core(Settings(data_dir=tmp_path / "solo", admin_token=ADMIN_TOKEN,
                         port=8787, admin_port=8788))
    try:
        response = asyncio.run(start_generation(core, "https://media.example.com/f/show.mkv"))
        assert response.status_code == 409
        assert response.json() == {"error": "pool_unavailable"}
        assert core.pool is None and core.channel_store is None
        assert core.generations.active == {}
        assert core.generations.recent() == []
        assert core.jobs.jobs == {} and core.jobs.sources == {}
    finally:
        core.stop()


class BrokenBotAPI(FakeTelegramBotAPI):
    """Bot API that accepts the connection but answers ok:false on sendDocument."""

    def urlopen(self, req, timeout=8.0):
        from urllib.parse import urlparse
        if urlparse(req.full_url).path.endswith("/sendDocument"):
            return FakeResponse(b'{"ok": false, "error_code": 500}')
        return super().urlopen(req, timeout=timeout)


def test_host_publication_failure_reports_failed_status(tmp_path, origin):
    """When Telegram rejects the publication documents the owner watches the job turn
    failed with the Bot API error code, nothing is indexed, and no package file lands."""
    tg = BrokenBotAPI()
    core = Core(host_settings(tmp_path / "host-fail"),
                policy=make_policy([origin], {HOST: ["127.0.0.1"]}),
                engine_factory=lambda _src: FakeEngine(duration_ms=12_000, gop_ms=2_000, delay=0.0),
                pool_storage=TelegramBotStore("123456:test", "-100123", opener=tg.urlopen))
    try:
        started = asyncio.run(admin_call(
            core, "POST", "/admin/generations",
            json={"source_url": origin.url("/f/pillar.mkv", host=HOST), "profile": PROFILE}))
        assert started.status_code == 202
        generation_id = started.json()["generation_id"]

        def settled():
            row = asyncio.run(admin_call(core, "GET", f"/admin/generations/{generation_id}")).json()
            return row if row and row["state"] in ("complete", "failed") else None
        row = wait_for(settled)
        assert row["state"] == "failed", row
        assert row["error"] == "telegram_error"
        assert core.pool.db.q("SELECT COUNT(*) FROM pool_packages")[0][0] == 0
        assert core.pool.db.q("SELECT COUNT(*) FROM pool_hints")[0][0] == 0
        assert list(core.pool.package_dir.iterdir()) == []
        assert core.generations.active == {} and core.jobs.jobs == {}
    finally:
        core.stop()


_OMITTED = object()


@pytest.mark.parametrize("imdb", [_OMITTED, "", "   "])
def test_absent_or_blank_imdb_generation_completes(tmp_path, origin, imdb):
    """Absent or blank IMDb behaves as absent: the job completes and publishes
    without an IMDb hint. All other inputs are identical."""
    tg = FakeTelegramBotAPI()
    core = Core(host_settings(tmp_path / "host-blank-imdb"),
                policy=make_policy([origin], {HOST: ["127.0.0.1"]}),
                engine_factory=lambda _src: FakeEngine(duration_ms=12_000, gop_ms=2_000, delay=0.0),
                pool_storage=TelegramBotStore("123456:test", "-100123", opener=tg.urlopen))
    payload = {"source_url": origin.url("/f/pillar.mkv", host=HOST), "profile": PROFILE,
               "season": 0, "episode": 0, "filename": "qa-h2-20261110.mp4"}
    if imdb is not _OMITTED:
        payload["imdb"] = imdb
    try:
        started = asyncio.run(admin_call(core, "POST", "/admin/generations", json=payload))
        assert started.status_code == 202, started.text
        generation_id = started.json()["generation_id"]

        def settled():
            row = asyncio.run(admin_call(core, "GET", f"/admin/generations/{generation_id}")).json()
            return row if row and row["state"] in ("complete", "failed") else None
        row = wait_for(settled)
        assert row["state"] == "complete", row
        assert row["error"] is None

        hint_captions = [c for c in tg.captions if c.startswith("openseek:hints:v1:")]
        assert len(hint_captions) == 1 and len(tg.files) == 2
        encoded = hint_captions[0].split(":", 3)[3]
        hint_manifest = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        assert hint_manifest["imdb"] is None
        assert hint_manifest["filename"] == "qa.h2.20261110"
    finally:
        core.stop()



def test_nonblank_invalid_imdb_is_still_rejected(tmp_path, origin):
    """Explicit invalid nonblank IMDb keeps its documented bad_hints refusal.

    Blank is absent, but a supplied value that is not a tt-digit IMDb is
    still rejected at publication rather than silently dropped.
    """
    tg = FakeTelegramBotAPI()
    core = Core(host_settings(tmp_path / "host-bad-imdb"),
                policy=make_policy([origin], {HOST: ["127.0.0.1"]}),
                engine_factory=lambda _src: FakeEngine(duration_ms=12_000, gop_ms=2_000, delay=0.0),
                pool_storage=TelegramBotStore("123456:test", "-100123", opener=tg.urlopen))
    try:
        started = asyncio.run(admin_call(
            core, "POST", "/admin/generations",
            json={"source_url": origin.url("/f/pillar.mkv", host=HOST), "profile": PROFILE,
                  "imdb": "bogus", "season": 0, "episode": 0, "filename": "qa-h2-20261110.mp4"}))
        assert started.status_code == 202, started.text
        generation_id = started.json()["generation_id"]

        def settled():
            row = asyncio.run(admin_call(core, "GET", f"/admin/generations/{generation_id}")).json()
            return row if row and row["state"] in ("complete", "failed") else None
        row = wait_for(settled)
        assert row["state"] == "failed", row
        assert row["error"] == "bad_hints"
        assert tg.files == {} and tg.messages == 0
    finally:
        core.stop()
