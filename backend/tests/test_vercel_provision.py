"""Deterministic payload regressions for the provisioning Vercel client.

Coverage target: the wizard's REST deployment must request the FastAPI framework
preset (repo `pyproject.toml` `[tool.vercel]` entrypoint plus `vercel.json`
function entry stay the source of truth and are never written), upload source
files unchanged, and turn a 409 project-name conflict into a typed
`ProviderError` whose plain text never echoes tokens or response bodies.

Plain `httpx.MockTransport` double; this module never opens a network connection
and never signs in to a real account.
"""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from openseek.provision.errors import ProviderError
from openseek.provision.vercel import VercelClient


def test_create_project_and_deployment_request_fastapi():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST" and request.url.path == "/v11/projects":
            return httpx.Response(201, json={"id": "prj_1",
                                             "name": json.loads(request.content)["name"]})
        if request.method == "POST" and request.url.path == "/v2/files":
            return httpx.Response(200)
        if request.method == "POST" and request.url.path == "/v13/deployments":
            return httpx.Response(201, json={"id": "dpl_1", "readyState": "READY",
                                             "url": "https://my-openseek-node.vercel.app"})
        return httpx.Response(404)

    client = VercelClient("vercel-secret-token", transport=httpx.MockTransport(handler))
    client.create_project("openseek-g7-qa", {"OPENSEEK_PROFILE": "vercel"})
    deployment = client.create_deployment(
        "openseek-g7-qa",
        [("openseek/vercel.py", b"app\n"), ("pyproject.toml", b"[project]\n")])

    create_body = json.loads(calls[0].content)
    assert create_body["framework"] == "fastapi"
    assert create_body["name"] == "openseek-g7-qa"
    assert create_body["environmentVariables"] == [
        {"key": "OPENSEEK_PROFILE", "value": "vercel", "type": "encrypted",
         "target": ["production"]}]

    deploy_body = json.loads(calls[-1].content)
    assert deploy_body["name"] == "openseek-g7-qa"
    assert deploy_body["target"] == "production"
    assert deploy_body["projectSettings"] == {"framework": "fastapi"}
    sha_provider = hashlib.sha1(b"app\n").hexdigest()
    sha_pyproject = hashlib.sha1(b"[project]\n").hexdigest()
    assert deploy_body["files"] == [
        {"file": "openseek/vercel.py", "sha": sha_provider, "size": 4},
        {"file": "pyproject.toml", "sha": sha_pyproject, "size": 10},
    ]  # source files are uploaded unchanged around the framework preset
    assert len(calls) == 4  # create project, two uploads, create deployment
    assert deployment["id"] == "dpl_1" and deployment["state"] == "READY"


def test_name_conflict_is_typed_and_prints_no_secrets():
    """A 409 must raise the typed conflict failure: never a NameError, and the
    plain text carries neither token, env secret nor quoted response body."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v11/projects":
            return httpx.Response(409, json={"error": {
                "code": "project_name_conflict",
                "message": "my-node already exists; files live under my-node.vercel.app"}})
        return httpx.Response(404)

    client = VercelClient("vercel-secret-token", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as caught:
        client.create_project("my-node", {"OPENSEEK_ADMIN_TOKEN": "first-party-secret"})

    assert caught.value.code == "conflict"
    assert caught.value.status == 409
    plain = caught.value.plain
    assert plain.startswith("Vercel already uses that name")
    assert "vercel-secret-token" not in plain
    assert "first-party-secret" not in plain
    assert "my-node already exists" not in plain      # response body never quoted
    assert "my-node.vercel.app" not in plain          # response body never quoted


def _protected_client(calls, *, sso, patch_status=200, patched_body=None):
    """Vercel double that remembers requests and answers the access endpoints."""
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path,
                      json.loads(request.content) if request.content else None))
        if request.method == "GET" and request.url.path == "/v9/projects/prj_g7":
            body = {"id": "prj_g7", "name": "openseek-g7-qa"}
            if sso is not Ellipsis:
                body["ssoProtection"] = sso
            return httpx.Response(200, json=body)
        if request.method == "PATCH" and request.url.path == "/v9/projects/prj_g7":
            return httpx.Response(patch_status, json=patched_body)
        return httpx.Response(404, json={"error": {"code": "not_found"}})
    return VercelClient("vercel-secret-token", transport=httpx.MockTransport(handler))


def test_ensure_production_access_patches_blocked_production_exactly_once():
    calls = []
    client = _protected_client(
        calls, sso={"deploymentType": "all_except_custom_domains"},
        patched_body={"id": "prj_g7", "name": "openseek-g7-qa",
                      "ssoProtection": {"deploymentType": "preview"}})
    client.ensure_production_access("prj_g7")

    assert [(method, path) for method, path, _ in calls] == [
        ("GET", "/v9/projects/prj_g7"), ("PATCH", "/v9/projects/prj_g7")]
    assert calls[1][2] == {"ssoProtection": {"deploymentType": "preview"}}  # nothing else mutated


@pytest.mark.parametrize("protection", [Ellipsis, None, {"deploymentType": "preview"}],
                         ids=["absent", "null", "preview"])
def test_ensure_production_access_never_patches_unblocked_projects(protection):
    calls = []
    client = _protected_client(calls, sso=protection,
                               patched_body={"id": "prj_g7"})
    client.ensure_production_access("prj_g7")

    assert [(method, path) for method, path, _ in calls] == [("GET", "/v9/projects/prj_g7")]


@pytest.mark.parametrize("protection", [{"deploymentType": "some_new_mode"}, "enabled"],
                         ids=["unknown-mode", "malformed"])
def test_ensure_production_access_fails_closed_on_unmodeled_protection(protection):
    calls = []
    client = _protected_client(calls, sso=protection)
    with pytest.raises(ProviderError) as caught:
        client.ensure_production_access("prj_g7")

    assert [(method, path) for method, path, _ in calls] == [("GET", "/v9/projects/prj_g7")]
    assert caught.value.plain.startswith("Vercel answered with an unexpected response")
    assert "some_new_mode" not in caught.value.plain      # raw response never quoted
    assert "enabled" not in caught.value.plain
