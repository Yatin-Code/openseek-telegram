"""Public listener exposes only the current pool and pairing contract."""
from __future__ import annotations

import asyncio

import httpx

from openseek.app import Core, create_api_app, guest_route_allowed
from openseek.config import Settings
from openseek.pairing import new_token, token_hash
from openseek.public import classify


class NoSourceProxy:
    def stop(self):
        pass


def test_public_route_table_keeps_pool_contract_and_rejects_retired_routes():
    fp = "a" * 64
    assert classify("GET", f"/v1/pool/{fp}/nvmt", guest_route_allowed) == "pool_read"
    assert classify("PUT", f"/v1/pool/{fp}/nvmt", guest_route_allowed) == "pool_write"
    assert classify("POST", f"/v1/pool/{fp}/nvmt", guest_route_allowed) is None
    assert classify("GET", f"/v1/pool/{fp}/nvmt/", guest_route_allowed) is None
    assert classify("POST", "/v1/lookup", guest_route_allowed) is None
    assert classify("GET", "/v1/previews/" + "b" * 32 + "/thumbs.vtt", guest_route_allowed) is None
    assert classify("GET", "/admin/status", guest_route_allowed) is None


def test_public_listener_requires_pairing_for_guest_routes_and_hides_owner_routes(tmp_path):
    core = Core(Settings(data_dir=tmp_path / "data"), proxy=NoSourceProxy())
    token = new_token()
    core.auth.register("Guest", token_hash(token), role="guest")
    app = create_api_app(core, public=True)

    async def request(method: str, path: str, auth: str | None = None):
        transport = httpx.ASGITransport(app=app, client=("198.51.100.19", 41000))
        async with httpx.AsyncClient(transport=transport, base_url="https://pool.test") as client:
            headers = {"Authorization": f"Bearer {auth}"} if auth else {}
            return await client.request(method, path, headers=headers)

    try:
        anonymous_health = asyncio.run(request("GET", "/v1/health"))
        anonymous_status = asyncio.run(request("GET", "/v1/pair/status"))
        guest_status = asyncio.run(request("GET", "/v1/pair/status", token))
        old_preview = asyncio.run(request("GET", "/v1/previews/" + "b" * 32 + "/thumbs.vtt", token))
        owner_status = asyncio.run(request("GET", "/admin/status", token))
        owner_generation = asyncio.run(request("POST", "/admin/generations", token))

        assert anonymous_health.status_code == 200
        assert anonymous_status.status_code == 401
        assert guest_status.status_code == 200
        assert old_preview.status_code == 404
        assert owner_status.status_code == 404
        assert owner_generation.status_code == 404
    finally:
        core.stop()
