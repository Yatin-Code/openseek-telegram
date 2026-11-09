"""The REST setup provider: unit coverage plus a full wizard journey."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

from openseek.participant_setup import ParticipantSetup, SetupFailure
from openseek.provision.provider import RestSetupProvider
from test_participant_setup import FakeTelegramJoiner, _source


class FakeVercel:
    def __init__(self, node: dict):
        self.node = node
        self.project = ""
        self.env: dict[str, str] = {}
        self.deployments = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            body = {}
        if path == "/v2/user":
            return httpx.Response(200, json={"user": {"id": "u1", "username": "tester"}})
        if path == "/v9/projects":
            rows = [{"id": "prj_1", "name": self.project}] if self.project else []
            return httpx.Response(200, json={"projects": rows})
        if path == "/v11/projects":
            self.project = body["name"]
            return httpx.Response(201, json={"id": "prj_1", "name": self.project})
        if path.startswith("/v9/projects/") and path.count("/") == 3:
            name = unquote(path.rsplit("/", 1)[-1])
            if self.project == name:
                return httpx.Response(200, json={"id": "prj_1", "name": name})
            return httpx.Response(404, json={"error": {"code": "not_found"}})
        if path.startswith("/v10/projects/") and path.endswith("/env"):
            if method == "GET":
                return httpx.Response(200, json={"envs": [
                    {"id": f"env_{key}", "key": key} for key in self.env]})
            for entry in body:
                self.env[entry["key"]] = entry["value"]
                if entry["key"] == "OPENSEEK_SETUP_PENDING" and entry["value"] == "0":
                    self.node["pending"] = False
            return httpx.Response(201, json={})
        if path.startswith("/v9/projects/") and "/env/" in path and method == "DELETE":
            env_id = path.rsplit("/", 1)[-1]
            self.env.pop(env_id.removeprefix("env_"), None)
            return httpx.Response(200, json={})
        if path == "/v2/files":
            return httpx.Response(200)
        if path == "/v13/deployments":
            self.deployments += 1
            return httpx.Response(201, json={
                "id": f"dpl_{self.deployments}",
                "url": f"openseek-{self.deployments}.vercel.app",
                "readyState": "QUEUED"})
        if path.startswith("/v13/deployments/"):
            return httpx.Response(200, json={
                "id": path.rsplit("/", 1)[-1],
                "readyState": "READY",
                "url": f"openseek-{self.deployments}.vercel.app"})
        return httpx.Response(404, json={"error": {"code": "not_found"}})


class FakeSupabase:
    def __init__(self):
        self.created = 0
        self.sql = ""
        self.region = ""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/v1/organizations":
            return httpx.Response(200, json=[{"id": "org_uuid", "slug": "my-org", "name": "Mine"}])
        if path == "/v1/projects" and method == "GET":
            return httpx.Response(200, json=[])
        if path == "/v1/projects" and method == "POST":
            body = json.loads(request.content)
            self.created += 1
            self.region = str((body.get("region_selection") or {}).get("code") or "")
            return httpx.Response(201, json={"id": "abcdefghij", "name": body["name"],
                                             "status": "COMING_UP"})
        if path == "/v1/projects/abcdefghij":
            return httpx.Response(200, json={"id": "abcdefghij", "name": "openseek-pool-node",
                                             "status": "ACTIVE_HEALTHY",
                                             "organization_id": "org_uuid"})
        if path == "/v1/projects/abcdefghij/api-keys":
            return httpx.Response(200, json=[{"name": "service_role", "api_key": "service-key"}])
        if path == "/v1/projects/abcdefghij/database/query":
            self.sql = json.loads(request.content)["query"]
            return httpx.Response(200, json=[])
        if path == "/v1/projects/abcdefghij/storage/buckets":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "not found"})


def _bucket(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/storage/v1/bucket"
    return httpx.Response(200, json={"name": "openseek-staging"})


def _node_factory(node: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/health":
            return httpx.Response(200, json={"service": "openseek", "api": 1,
                                             "setup_pending": node["pending"]})
        if request.url.path == "/v1/setup/pairing":
            assert request.headers["authorization"].startswith("Bearer ")
            node.setdefault("pairing_bodies", []).append(json.loads(request.content or b"{}"))
            return httpx.Response(200, json={"code": "ABCD-EFGH-IJKL-MNOP"})
        return httpx.Response(404)

    def factory(**kwargs) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)
    return factory


def _provider(vercel, supabase, node, input_fn=input):
    return RestSetupProvider(
        vercel_token="vercel-token", supabase_token="sbp_test_token",
        input_fn=input_fn,
        vercel_transport=httpx.MockTransport(vercel),
        supabase_transport=httpx.MockTransport(supabase),
        bucket_transport=httpx.MockTransport(_bucket),
        httpx_client_factory=_node_factory(node), poll_s=0, max_polls=3)


def test_provider_lists_and_provisions(tmp_path):
    node = {"pending": True}
    vercel, supabase = FakeVercel(node), FakeSupabase()
    provider = _provider(vercel, supabase, node)
    try:
        provider.authenticate()
        assert provider.supabase_projects() == []
        orgs = provider.supabase_organizations()
        project = provider.create_supabase_project("openseek-pool-node", orgs[0]["id"], "ap-south-1")
        assert project["ref"] == "abcdefghij" and supabase.region == "ap-south-1"
        schema = Path(__file__).resolve().parents[1] / "openseek" / "supabase" / "schema.sql"
        provider.apply_schema("abcdefghij", tmp_path, schema)
        assert "create table" in supabase.sql.lower()
        url, secret = provider.supabase_credentials(project)
        assert (url, secret) == ("https://abcdefghij.supabase.co", "service-key")
        provider.configure_private_bucket(url, secret)
        vercel.project = "openseek-node"  # the double must know the project being linked
        provider.link_vercel_project(tmp_path, {"name": "openseek-node"})
        provider.set_vercel_secret(tmp_path, "OPENSEEK_PROFILE", "vercel", secret=False)
        assert vercel.env["OPENSEEK_PROFILE"] == "vercel"
        assert provider.deploy(tmp_path) == "https://openseek-1.vercel.app"
        assert provider.check_health("https://openseek-1.vercel.app", pending=True)
        invite = provider.create_pairing_invite("https://openseek-1.vercel.app", "setup-key")
        assert invite["code"] == "ABCD-EFGH-IJKL-MNOP"
        assert node["pairing_bodies"][-1] == {"label": "Nuvio TV", "enable_sharing": False}
        assert provider.create_pairing_invite("https://openseek-1.vercel.app", "setup-key",
                                              enable_sharing=True)["code"] == "ABCD-EFGH-IJKL-MNOP"
        assert node["pairing_bodies"][-1]["enable_sharing"] is True  # a consented body stays explicit
        provider.remove_vercel_secret(tmp_path, "OPENSEEK_PROFILE")
        assert "OPENSEEK_PROFILE" not in vercel.env
    finally:
        provider.close()


def test_provider_rejects_bad_tokens():
    provider = RestSetupProvider(vercel_token="vercel-token", supabase_token="not-a-pat",
                                 secret_input_fn=lambda _prompt: "unused")
    with pytest.raises(SetupFailure) as caught:
        provider.authenticate()
    assert caught.value.reason == "auth"


def test_full_participant_journey_with_rest_provider(tmp_path):
    node = {"pending": True}
    vercel, supabase = FakeVercel(node), FakeSupabase()
    output: list[str] = []

    def answers(prompt: str) -> str:
        for key, value in {
            "Choose a number": "1",
            "New Supabase project name": "",
            "Project region": "",
            "Vercel project name": "",
            "Telegram API ID": "123",
            "Shared Telegram group invite link": "https://t.me/+shared",
            "Enable Nuvio guest-device access": "y",
        }.items():
            if key in prompt:
                return value
        raise AssertionError(f"unexpected prompt: {prompt}")

    provider = _provider(vercel, supabase, node, input_fn=answers)

    setup = ParticipantSetup(
        provider, source_dir=_source(tmp_path), state_path=tmp_path / "state.json",
        input_fn=answers, print_fn=output.append,
        secret_input_fn=lambda _prompt: "api-hash",
        telegram_joiner=FakeTelegramJoiner())
    assert setup.run() == 0, "\n".join(output)    # infrastructure -> healthy
    assert node["pending"] is True
    assert setup.run() == 0                       # Telegram + pairing
    assert setup.state["step"] == "paired"
    assert node["pending"] is False
    assert any("ABCD-EFGH-IJKL-MNOP" in line for line in output)
    assert supabase.sql and vercel.deployments == 4
    provider.close()
