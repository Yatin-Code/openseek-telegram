"""Thin Supabase management-API client for the provisioning wizard.

Base https://api.supabase.com, Bearer auth with the participant's own personal access
token (sbp_; management requests are limited to 120 per minute). Only the endpoints
the wizard needs. Project lifecycle states surface as plain-language blocks: a paused
project always points the participant at the Studio dashboard, never at a paid plan.
"""
from __future__ import annotations

import time
import httpx
from urllib.parse import quote

from .errors import (AUTH, CONFLICT, NOT_FOUND, PAUSED, PROVIDER, QUOTA, TIMEOUT,
                     ProviderError, code_for_status, transport_error)

BASE_URL = "https://api.supabase.com"

# The Supabase Free plan allows two ACTIVE projects per participant; paused ones do
# not count against it. The allowance is per user across organizations, so callers
# must check both the selected organization and the account total (free_slot_available
# plus total_active_projects).
FREE_PROJECT_ALLOWANCE = 2

_ACTIVE = ("ACTIVE_HEALTHY",)
_INITIALIZING = ("COMING_UP", "GOING_UP_COMMON", "GOING_DOWN_COMMON", "UPGRADING",
                 "RESTORING", "UNKNOWN")
_PAUSED = ("PAUSED", "PAUSING")
_FAILED = ("INIT_FAILED", "INIT_HARD_FAILED", "RESTORE_FAILED", "UP_FAILED", "REMOVED")


def project_state(status: str) -> str:
    """Group a Supabase project status into active / initializing / paused / failed."""
    status = str(status or "").upper()
    if status in _ACTIVE:
        return "active"
    if status in _INITIALIZING:
        return "initializing"
    if status in _PAUSED:
        return "paused"
    if status in _FAILED:
        return "failed"
    return "unknown"


class SupabaseProvisioner:
    """Minimal typed wrapper over the Supabase management API used by the wizard."""

    def __init__(self, token: str, transport=None, timeout: float = 30.0):
        self._token = token
        self._client = httpx.Client(base_url=BASE_URL, transport=transport, timeout=timeout,
                                    headers={"authorization": f"Bearer {token}"})

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, *, json=None, params=None,
                 allow_404: bool = False) -> httpx.Response:
        try:
            response = self._client.request(method, path, json=json, params=params)
        except httpx.HTTPError:
            raise transport_error("Supabase") from None
        if response.status_code < 400 or (allow_404 and response.status_code == 404):
            return response
        code = code_for_status(response.status_code)
        if code == AUTH:
            plain = ("Supabase rejected this access token. Create one at "
                     "https://supabase.com/dashboard/account/tokens and run the wizard "
                     "again.")
        elif code == QUOTA:
            plain = ("Supabase limited this request (usage allowance or rate limit for "
                     "management calls). Wait a bit or resume the project in the "
                     "Supabase dashboard, then run the wizard again; no paid plan is "
                     "needed.")
        elif code == CONFLICT:
            plain = ("Supabase refuses that project name here. Pick a different name "
                     "and run the wizard again, or select an existing project.")
        else:
            plain = ("Supabase refused the request. The dashboard shows the reason; "
                     "run the wizard again to resume.")
        raise ProviderError(code, response.status_code, plain) from None

    def list_organizations(self) -> list[dict]:
        """Organizations on this account: [{id, slug, name}]."""
        response = self._request("GET", "/v1/organizations")
        rows = response.json()
        return [{"id": row.get("id", ""), "slug": row.get("slug", ""),
                 "name": row.get("name", "")}
                for row in rows if isinstance(row, dict) and row.get("slug")]

    def list_projects(self) -> list[dict]:
        """Projects on this account: [{id, ref, name, status, organization_slug}]."""
        response = self._request("GET", "/v1/projects")
        rows = response.json()
        return [{"id": row.get("id", ""), "ref": row.get("id", ""),
                 "name": row.get("name", ""), "status": row.get("status", ""),
                 "organization_slug": row.get("organization_id", "")
                 or row.get("organization_slug", "")}
                for row in rows if isinstance(row, dict)]

    def get_project(self, ref: str) -> dict | None:
        """One project snapshot ({ref, name, status, organization_slug}), or None."""
        response = self._request("GET", f"/v1/projects/{quote(ref, safe='')}",
                                 allow_404=True)
        if response.status_code == 404:
            return None
        row = response.json()
        return {"ref": row.get("id", ref), "name": row.get("name", ""),
                "status": row.get("status", ""),
                "organization_slug": row.get("organization_id", "")}

    def create_project(self, org_slug: str, name: str, db_pass: str,
                       region_selection: dict | None = None) -> dict:
        """Create a project inside the participant's own organization, Free default.

        The request deliberately never carries a `plan` field: plans are an
        organization-level attribute and hard-coding anything here could silently
        request a paid tier, which the zero-spend rule forbids. The database password
        is generated by the wizard and used only for this request.
        """
        body = {"name": name, "organization_slug": org_slug, "db_pass": db_pass}
        if region_selection:
            body["region_selection"] = region_selection
        response = self._request("POST", "/v1/projects", json=body)
        row = response.json()
        return {"ref": row.get("id", ""), "name": row.get("name", name),
                "status": row.get("status", "")}

    def wait_until_active(self, ref: str, *, poll_s: float = 5.0,
                          max_polls: int = 60) -> None:
        """Poll get_project until ACTIVE_HEALTHY; typed errors for pause/failure/timeout."""
        for _ in range(max(1, int(max_polls))):
            project = self.get_project(ref)
            if project is None:
                raise ProviderError(NOT_FOUND, 404,
                                    f"Supabase no longer has a project with ref {ref}. "
                                    "Pick an existing project or create a new one and "
                                    "run the wizard again.")
            state = project_state(project["status"])
            if state == "active":
                return
            if state == "paused":
                raise ProviderError(PAUSED, 0,
                                    f"Supabase paused project {ref}. Resume it in the "
                                    "Supabase Studio dashboard, then run the wizard "
                                    "again (pausing is free plan housekeeping, not a "
                                    "billing step).")
            if state == "failed":
                raise ProviderError(PROVIDER, 0,
                                    f"Supabase could not initialize project {ref}. "
                                    "Delete it in the Supabase dashboard if you no "
                                    "longer want it, or select an existing project; "
                                    "run the wizard again afterwards.")
            time.sleep(poll_s)
        raise ProviderError(TIMEOUT, 0,
                            f"Supabase is still initializing project {ref}. Run the "
                            "wizard again in a few minutes; it will keep going from "
                            "where it stopped.")

    def service_key(self, ref: str) -> str:
        """Reveal the project's service_role API key. Never logged or echoed."""
        response = self._request("GET", f"/v1/projects/{quote(ref, safe='')}/api-keys",
                                 params=[("reveal", "true")])
        entries = response.json()
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and entry.get("name") == "service_role" \
                    and entry.get("api_key"):
                return str(entry["api_key"])
        raise ProviderError(NOT_FOUND, 404,
                            f"Supabase did not return a service_role API key for "
                            f"{ref}. Open the project's API settings in the dashboard "
                            "to check, then run the wizard again.")

    def run_sql(self, ref: str, sql: str) -> list[dict]:
        """Send one SQL string to the management query endpoint and return its rows.

        Honest caveat: Supabase's own documentation marks /database/query as
        experimental; when the endpoint answers that it is unavailable here, the error
        tells the participant to run backend/openseek/supabase/schema.sql manually in
        the Studio SQL editor and then resume. Only the account token authorizes this
        call (the service key is not involved).
        """
        response = self._request("POST", f"/v1/projects/{quote(ref, safe='')}/database/query",
                                 json={"query": sql})
        if response.status_code == 404:
            raise ProviderError(NOT_FOUND, 404,
                                "This Supabase account cannot run management SQL "
                                "queries. Paste backend/openseek/supabase/schema.sql "
                                "into the Studio SQL editor, run it, then run the "
                                "wizard again.")
        body = response.json()
        if isinstance(body, list):
            return body
        if isinstance(body, dict) and body.get("error"):
            raise ProviderError(PROVIDER, response.status_code,
                                "Supabase reported a database error while applying "
                                "the schema; the Studio SQL editor shows the same "
                                "query output. Fix or re-run it manually there, then "
                                "run the wizard again.")
        return []

    def list_buckets(self, ref: str) -> list[dict]:
        """Storage buckets known to the project: [{name, public, ...}]."""
        response = self._request("GET",
                                 f"/v1/projects/{quote(ref, safe='')}/storage/buckets")
        rows = response.json()
        return [row for row in rows if isinstance(row, dict) and row.get("name")]

    @staticmethod
    def free_slot_available(projects: list[dict], org_slug: str) -> bool:
        """Whether the selected organization has a free active-project slot.

        The caller must ALSO check `total_active_projects`, because Supabase counts the
        Free allowance of two active projects per participant across all organizations.
        """
        organization = [row for row in projects
                        if row.get("organization_slug") == org_slug
                        and project_state(row.get("status", "")) == "active"]
        return len(organization) < FREE_PROJECT_ALLOWANCE

    @staticmethod
    def total_active_projects(projects: list[dict]) -> int:
        """Active projects on this account across every organization."""
        return sum(1 for row in projects
                   if project_state(row.get("status", "")) == "active")

    @staticmethod
    def free_slot_message(org_slug: str, org_count: int, total_count: int) -> str:
        """The plain refusal the wizard raises when the free allowance is used up."""
        where = (f"for your organizations (the one you picked holds {org_count})"
                 if total_count >= FREE_PROJECT_ALLOWANCE
                 else f"across all your organizations ({total_count} active)")
        return ("The Supabase Free plan allows 2 active projects and you already have "
                f"that many {where}. Select one of your existing projects instead, or "
                "pause one in the Supabase Studio dashboard and run the wizard again. "
                "Nothing here requires a paid plan.")
