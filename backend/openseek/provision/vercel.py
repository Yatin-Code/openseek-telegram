"""Thin Vercel REST client for the provisioning wizard.

Base https://api.vercel.com, Bearer auth with the participant's own account token.
Only the handful of endpoints the wizard needs. Error handling is centralized:
401/403 are authorization failures, 429 quota/rate limits with wait guidance, 409 a
name conflict. Plain text never contains the token and never quotes a response body.
"""
from __future__ import annotations

import hashlib
import httpx
from urllib.parse import quote

from .errors import (AUTH, CONFLICT, QUOTA, PROVIDER, ProviderError,
                     code_for_status, transport_error)

BASE_URL = "https://api.vercel.com"

# SSO protection modes that make even production deployment URLs redirect to
# Vercel's SSO endpoint. Preview-only protection is the safe relaxed state: it
# keeps preview deployments authenticated while production URLs stay public.
_BLOCKING_SSO_MODES = ("all", "prod_deployment_urls_and_all_previews",
                       "all_except_custom_domains")
_SSO_UNEXPECTED = ("Vercel answered with an unexpected response about this "
                   "project's access mode. Check the project in your Vercel "
                   "dashboard, then run the wizard again to resume.")
_SSO_UNCONFIRMED = ("Vercel did not confirm the production access change. Check "
                    "the project in your Vercel dashboard, then run the wizard "
                    "again to resume.")


def _project_json(response: httpx.Response) -> dict:
    """Parse a project response or fail safely; the body is never quoted."""
    plain = _SSO_UNEXPECTED
    try:
        body = response.json()
    except ValueError:
        raise ProviderError(PROVIDER, 0, plain) from None
    if not isinstance(body, dict):
        raise ProviderError(PROVIDER, 0, plain)
    return body


def _sso_mode(project: dict) -> str:
    """The project's SSO deployment type ('' when unprotected)."""
    protection = project.get("ssoProtection")
    if protection is None:
        return ""
    if isinstance(protection, dict) and isinstance(protection.get("deploymentType"), str):
        return protection["deploymentType"]
    raise ProviderError(PROVIDER, 0, _SSO_UNEXPECTED)

_DEPLOYMENT_GUIDANCE = {
    "ERROR": "Vercel finished the deployment with an error. The build log in your "
             "Vercel dashboard shows the reason; fix what it names and run the wizard "
             "again (it reuses the configured project and environment).",
    "CANCELED": "Vercel reports the deployment as canceled, usually from your dashboard. "
                "Run the wizard again to deploy.",
    "BLOCKED": "Vercel blocked this deployment. Most often this is the account's daily "
               "deployment allowance (about 100 per day) or an unverified domain; wait "
               "for the allowance to reset or check your Vercel dashboard, then run the "
               "wizard again. Waiting fixes this; no paid plan is needed.",
}


class VercelClient:
    """Minimal typed wrapper over the Vercel REST surface used by `openseek wizard`."""

    def __init__(self, token: str, transport=None, timeout: float = 30.0):
        self._token = token
        self._client = httpx.Client(base_url=BASE_URL, transport=transport, timeout=timeout,
                                    headers={"authorization": f"Bearer {token}"})

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, *, json=None, content: bytes | None = None,
                 headers: dict | None = None, params=None, allow_404: bool = False) -> httpx.Response:
        try:
            response = self._client.request(method, path, json=json, content=content,
                                            headers=headers, params=params)
        except httpx.HTTPError:
            raise transport_error("Vercel") from None
        if response.status_code < 400 or (allow_404 and response.status_code == 404):
            return response
        code = code_for_status(response.status_code)
        if code == AUTH:
            plain = ("Vercel rejected this token. Create a token at "
                     "https://vercel.com/account/tokens and run the wizard again.")
        elif code == QUOTA:
            plain = ("Vercel is rate-limiting this account. Wait for the limit to reset "
                     "(deployments are capped per day) and run the wizard again; "
                     "waiting is all that is needed on the free Hobby plan.")
        elif code == CONFLICT:
            plain = ("Vercel already uses that name for another project. Pick a "
                     "different project name and run the wizard again.")
        else:
            detail = response.json().get("error", {}).get("code") if _is_json(response) else ""
            suffix = f" (Vercel code: {detail})" if detail else ""
            plain = f"Vercel refused the request{suffix}. The Vercel dashboard explains; run the wizard again to resume."
        raise ProviderError(code, response.status_code, plain) from None

    def whoami(self) -> dict:
        """The authenticated user: {id, username, email, limited}."""
        response = self._request("GET", "/v2/user")
        user = response.json().get("user", {})
        return {key: user.get(key, "" if key != "limited" else False)
                for key in ("id", "username", "email", "limited")}

    def get_project(self, name_or_id: str) -> dict | None:
        """Fetch a project by name or id, or None when it does not exist."""
        response = self._request("GET", f"/v9/projects/{quote(name_or_id, safe='')}",
                                 allow_404=True)
        if response.status_code == 404:
            return None
        body = response.json()
        return {"id": body["id"], "name": body["name"]} if isinstance(body, dict) else None

    def ensure_production_access(self, project_id_or_name: str) -> dict:
        """Make the project's production deployment URLs publicly reachable.

        Vercel SSO protection redirects public URL reads to the Vercel sign-in,
        which blocks the node's health check. A known blocking mode is repaired
        exactly once by narrowing it to preview-only protection with an official
        `PATCH /v9/projects/{idOrName}` whose body contains nothing but
        `ssoProtection`; preview authentication stays enabled, password
        protection, trusted IPs and every other project setting are left
        untouched, and no paid setting is involved. Projects without blocking
        SSO (and already-preview projects) are only read, never patched. The
        final protection is verified from the response, with one re-read before
        an unmodeled outcome fails safely.
        """
        path = f"/v9/projects/{quote(project_id_or_name, safe='')}"
        snapshot = _project_json(self._request("GET", path))
        mode = _sso_mode(snapshot)
        if not mode or mode == "preview":
            return snapshot
        if mode not in _BLOCKING_SSO_MODES:
            raise ProviderError(PROVIDER, 0, _SSO_UNEXPECTED)
        patched = _project_json(self._request(
            "PATCH", path, json={"ssoProtection": {"deploymentType": "preview"}}))
        if _sso_mode(patched) == "preview":
            return patched
        snapshot = _project_json(self._request("GET", path))
        if _sso_mode(snapshot) == "preview":
            return snapshot
        raise ProviderError(PROVIDER, 0, _SSO_UNCONFIRMED)

    def create_project(self, name: str, env_vars: dict[str, str]) -> dict:
        """Create the project with Production env variables (type encrypted)."""
        variables = [{"key": key, "value": value, "type": "encrypted",
                      "target": ["production"]} for key, value in sorted(env_vars.items())]
        response = self._request("POST", "/v11/projects",
                                 json={"name": name, "framework": "fastapi",
                                       "environmentVariables": variables})
        body = response.json()
        return {"id": body.get("id", ""), "name": body.get("name", name)}

    def upsert_env_vars(self, project_id_or_name: str, env_vars: dict[str, str]) -> None:
        """Create-or-update Production env variables (upsert=true)."""
        variables = [{"key": key, "value": value, "type": "encrypted",
                      "target": ["production"]} for key, value in sorted(env_vars.items())]
        self._request("POST", f"/v10/projects/{quote(project_id_or_name, safe='')}/env",
                      json=variables, params=[("upsert", "true")])

    def env_var_keys(self, project_id_or_name: str) -> set[str]:
        """The key names present on the project; the API never returns usable values."""
        response = self._request("GET", f"/v10/projects/{quote(project_id_or_name, safe='')}/env")
        envs = response.json().get("envs", [])
        return {entry.get("key", "") for entry in envs if isinstance(entry, dict)}

    def list_projects(self, limit: int = 100) -> list[dict]:
        """Projects on this account: [{id, name}] (one page, newest first)."""
        response = self._request("GET", "/v9/projects", params=[("limit", str(limit))])
        body = response.json()
        rows = body.get("projects", []) if isinstance(body, dict) else []
        return [{"id": row.get("id", ""), "name": row.get("name", "")}
                for row in rows if isinstance(row, dict) and row.get("name")]

    def delete_env_key(self, project_id_or_name: str, key: str) -> None:
        """Remove every Production environment entry named `key`; absent keys are fine."""
        response = self._request("GET",
                                 f"/v10/projects/{quote(project_id_or_name, safe='')}/env")
        entries = response.json().get("envs", [])
        for entry in entries:
            if isinstance(entry, dict) and entry.get("key") == key and entry.get("id"):
                self._request(
                    "DELETE",
                    f"/v9/projects/{quote(project_id_or_name, safe='')}"
                    f"/env/{quote(str(entry['id']), safe='')}")

    def create_deployment(self, name: str, files: list[tuple[str, bytes]]) -> dict:
        """Upload sources (sha1 digest each) and create a production deployment.

        Every file is uploaded to /v2/files with its digest; a file Vercel already
        holds returns its URLs instead of storing again and needs no re-upload. The
        deployment itself is created against the project `name`. The wizard never
        writes vercel.json: the repository owns it.
        """
        entries: list[dict] = []
        for path, content in files:
            digest = hashlib.sha1(content).hexdigest()
            # 200 with an empty body (stored now), or {"urls": [...]} when Vercel
            # already holds the bytes; both count as uploaded.
            self._request("POST", "/v2/files", content=content,
                          headers={"x-vercel-digest": digest,
                                   "x-now-size": str(len(content)),
                                   "content-type": "application/octet-stream"})
            entries.append({"file": path, "sha": digest, "size": len(content)})
        response = self._request("POST", "/v13/deployments", json={
            "name": name, "target": "production",
            "projectSettings": {"framework": "fastapi"}, "files": entries})
        body = response.json()
        return {"id": body.get("id", ""), "state": body.get("readyState", ""),
                "url": body.get("url", "")}

    def deployment_status(self, deployment_id: str) -> dict:
        """Normalize a deployment snapshot: {id, state, reason, url}.

        state is Vercel's readyState (QUEUED / INITIALIZING / BUILDING / READY / ERROR
        / CANCELED / BLOCKED) and reason the optional readyStateReason.
        """
        response = self._request("GET", f"/v13/deployments/{quote(deployment_id, safe='')}")
        body = response.json()
        state = str(body.get("readyState") or "QUEUED").upper()
        return {"id": body.get("id", ""), "state": state,
                "reason": str(body.get("readyStateReason") or ""),
                "url": body.get("url", "")}

    @staticmethod
    def deployment_failure(state: str, reason: str) -> ProviderError:
        """Turn a terminal readyState into plain-language guidance."""
        base = _DEPLOYMENT_GUIDANCE.get(state,
                                        f"Vercel stopped the deployment ({state}{' / ' + reason if reason else ''}). "
                                        "Run the wizard again to deploy.")
        return ProviderError("deployment", 0, base)


def _is_json(response: httpx.Response) -> bool:
    return response.headers.get("content-type", "").startswith("application/json")
