"""REST-token implementation of the participant setup provider.

The CLI-free counterpart of :class:`openseek.participant_setup.CliSetupProvider`.
It drives the same `ParticipantSetup` flow - including Telegram, the owner secret and
the Nuvio pairing bundle - through the provider management APIs: a Vercel account
token and a Supabase personal access token (`sbp_`). No provider CLI is required.

Tokens are read from the constructor, then the environment
(`OPENSEEK_VERCEL_TOKEN`, `OPENSEEK_SUPABASE_ACCESS_TOKEN`), then prompted for with
the hidden input function. They are held in memory for the wizard run only.
"""
from __future__ import annotations

import getpass
import os
import re
import secrets
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx

from ..participant_setup import SetupFailure
from .errors import (AUTH, CONFLICT, NOT_FOUND, PAUSED, PROVIDER, QUOTA,
                     SETUP_REQUIRED, TIMEOUT, ProviderError)
from .staging import DEFAULT_STAGING_BUCKET, ensure_staging_bucket
from .supabase import SupabaseProvisioner
from .vercel import VercelClient
from .wizard import deployment_files

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,62}\Z")
_REF_RE = re.compile(r"[a-z0-9]{8,30}\Z")
_REASONS = {AUTH: "auth", QUOTA: "quota", PAUSED: "paused", NOT_FOUND: "not_found",
            CONFLICT: "conflict", TIMEOUT: "timeout", SETUP_REQUIRED: "setup_required"}


def _project_ref(project: dict) -> str:
    return str(project.get("id") or project.get("ref") or project.get("project_ref") or "")


def _project_name(project: dict) -> str:
    return str(project.get("name") or project.get("slug") or _project_ref(project))


def _project_slug(raw: str, fallback: str) -> str:
    text = re.sub(r"[^a-z0-9-]+", "-", raw.strip().lower()).strip("-")
    return text if _NAME_RE.fullmatch(text) else fallback


class RestSetupProvider:
    """A `SetupProvider` backed by the Vercel and Supabase management APIs."""

    def __init__(self, *, vercel_token: str = "", supabase_token: str = "",
                 input_fn: Callable[[str], str] = input,
                 secret_input_fn: Callable[[str], str] = getpass.getpass,
                 httpx_client_factory: Callable[..., httpx.Client] = httpx.Client,
                 vercel_transport=None, supabase_transport=None,
                 bucket_transport=None, poll_s: float = 5.0,
                 max_polls: int = 120):
        self._vercel_token = vercel_token.strip()
        self._supabase_token = supabase_token.strip()
        self._input_fn = input_fn
        self._secret_input_fn = secret_input_fn
        self._httpx_client_factory = httpx_client_factory
        self._vercel_transport = vercel_transport
        self._supabase_transport = supabase_transport
        self._bucket_transport = bucket_transport
        self._poll_s = poll_s
        self._max_polls = max_polls
        self._vc: VercelClient | None = None
        self._sc: SupabaseProvisioner | None = None
        self._project = ""

    # ---------------------------------------------------------------- lifecycle
    def close(self) -> None:
        for client in (self._vc, self._sc):
            if client is not None:
                try:
                    client.close()
                except Exception:             # noqa: BLE001 - shutdown only
                    pass
        self._vc = None
        self._sc = None

    def authenticate(self) -> None:
        vercel = (self._vercel_token or
                  os.environ.get("OPENSEEK_VERCEL_TOKEN", "").strip() or
                  self._secret_input_fn("Vercel access token (hidden): ").strip())
        supabase = (self._supabase_token or
                    os.environ.get("OPENSEEK_SUPABASE_ACCESS_TOKEN", "").strip() or
                    self._secret_input_fn("Supabase access token (hidden, sbp_...): ").strip())
        if not vercel:
            raise SetupFailure("Vercel sign-in", "auth")
        if not supabase.startswith("sbp_"):
            raise SetupFailure("Supabase sign-in", "auth")
        self._vercel_token, self._supabase_token = vercel, supabase
        self.close()
        try:
            self._vercel().whoami()
            organizations = self._supabase().list_organizations()
        except ProviderError as exc:
            raise SetupFailure("provider sign-in", _REASONS.get(exc.code, "provider_error")) from None
        if not organizations:
            raise SetupFailure("Supabase sign-in", "no_accessible_organization")

    # ---------------------------------------------------------------- Supabase
    def supabase_projects(self) -> list[dict]:
        try:
            return self._supabase().list_projects()
        except ProviderError as exc:
            raise SetupFailure("Supabase project list", _REASONS.get(exc.code, "provider_error")) from None

    def supabase_organizations(self) -> list[dict]:
        try:
            return self._supabase().list_organizations()
        except ProviderError as exc:
            raise SetupFailure("Supabase organization list", _REASONS.get(exc.code, "provider_error")) from None

    def create_supabase_project(self, name: str, organization: str, region: str) -> dict:
        slug = self._organization_slug(organization)
        password = secrets.token_urlsafe(24)
        try:
            created = self._supabase().create_project(
                slug, name, password,
                region_selection={"type": "specific", "code": region})
            ref = _project_ref(created)
            if not ref:
                raise SetupFailure("Supabase project creation", "project_id_unavailable")
            self._supabase().wait_until_active(ref, poll_s=self._poll_s,
                                               max_polls=self._max_polls)
            project = self._supabase().get_project(ref) or {}
        except ProviderError as exc:
            raise SetupFailure("Supabase project creation", _REASONS.get(exc.code, "provider_error")) from None
        return {"id": ref, "ref": ref, "name": project.get("name") or name,
                "status": project.get("status", "")}

    def _organization_slug(self, organization: str) -> str:
        organizations = self.supabase_organizations()
        match = next((row for row in organizations
                      if organization in (str(row.get("id") or ""), str(row.get("slug") or ""))), None)
        slug = str((match or {}).get("slug") or "")
        if not _NAME_RE.fullmatch(slug):
            raise SetupFailure("Supabase organization", "no_accessible_organization")
        return slug

    def apply_schema(self, project_ref: str, state_dir: Path, schema_path: Path) -> None:
        if not project_ref or not schema_path.is_file():
            raise SetupFailure("Supabase schema", "schema_unavailable")
        try:
            self._supabase().run_sql(project_ref, schema_path.read_text(encoding="utf-8"))
        except ProviderError as exc:
            raise SetupFailure("Supabase schema", _REASONS.get(exc.code, "provider_error")) from None

    def supabase_credentials(self, project: dict) -> tuple[str, str]:
        ref = _project_ref(project)
        if not ref:
            raise SetupFailure("Supabase credentials", "project_ref_unavailable")
        try:
            secret = self._supabase().service_key(ref)
        except ProviderError as exc:
            raise SetupFailure("Supabase credentials", _REASONS.get(exc.code, "provider_error")) from None
        return f"https://{ref}.supabase.co", secret

    def configure_private_bucket(self, url: str, secret: str) -> None:
        ref = (urlsplit(url).hostname or "").partition(".")[0]
        if not _REF_RE.fullmatch(ref):
            raise SetupFailure("Supabase staging bucket", "project_url_invalid")
        try:
            ensure_staging_bucket(self._supabase(), ref=ref, service_key=secret,
                                  bucket=DEFAULT_STAGING_BUCKET,
                                  transport=self._bucket_transport)
        except ProviderError as exc:
            raise SetupFailure("Supabase staging bucket", _REASONS.get(exc.code, "provider_error")) from None

    # ---------------------------------------------------------------- Vercel
    def vercel_projects(self) -> list[dict]:
        try:
            return self._vercel().list_projects()
        except ProviderError as exc:
            raise SetupFailure("Vercel project list", _REASONS.get(exc.code, "provider_error")) from None

    def create_vercel_project(self, source_dir: Path) -> dict:
        fallback = "openseek-node"
        default = _project_slug(f"openseek-{source_dir.name}", fallback)
        raw = self._input_fn(f"Vercel project name [{default}]: ").strip() or default
        name = _project_slug(raw, "")
        if not name:
            raise SetupFailure("Vercel project creation", "invalid_project_name")
        try:
            existing = self._vercel().get_project(name)
            if existing is not None:
                self._project = _project_name(existing)
                return existing
            created = self._vercel().create_project(name, {})
        except ProviderError as exc:
            raise SetupFailure("Vercel project creation", _REASONS.get(exc.code, "provider_error")) from None
        self._project = _project_name(created)
        return created

    def link_vercel_project(self, source_dir: Path, project: dict) -> None:
        name = _project_name(project)
        if not name:
            raise SetupFailure("Vercel project link", "project_name_unavailable")
        try:
            self._vercel().ensure_production_access(name)
        except ProviderError as exc:
            raise SetupFailure("Vercel production access",
                               _REASONS.get(exc.code, "provider_error")) from None
        self._project = name

    def set_vercel_secret(self, source_dir: Path, name: str, value: str, *, secret: bool) -> None:
        project = self._project
        if not project:
            raise SetupFailure(f"Vercel {name} configuration", "project_unavailable")
        try:
            self._vercel().upsert_env_vars(project, {name: value})
        except ProviderError as exc:
            raise SetupFailure(f"Vercel {name} configuration",
                               _REASONS.get(exc.code, "provider_error")) from None

    def remove_vercel_secret(self, source_dir: Path, name: str) -> None:
        project = self._project
        if not project:
            return
        try:
            self._vercel().delete_env_key(project, name)
        except ProviderError as exc:
            raise SetupFailure(f"Vercel {name} removal",
                               _REASONS.get(exc.code, "provider_error")) from None

    def deploy(self, source_dir: Path) -> str:
        project = self._project
        if not project:
            raise SetupFailure("Vercel deployment", "project_unavailable")
        try:
            created = self._vercel().create_deployment(project, deployment_files())
            deployment_id = str(created.get("id") or "")
            url = str(created.get("url") or "")
            if not deployment_id:
                raise SetupFailure("Vercel deployment", "deployment_id_unavailable")
            for _ in range(self._max_polls):
                snapshot = self._vercel().deployment_status(deployment_id)
                state = str(snapshot.get("state") or "").upper()
                if state == "READY":
                    url = str(snapshot.get("url") or url)
                    break
                if state in ("ERROR", "CANCELED", "BLOCKED"):
                    raise VercelClient.deployment_failure(state, str(snapshot.get("reason") or ""))
                time.sleep(self._poll_s)
            else:
                raise SetupFailure("Vercel deployment", "deployment_timeout")
        except ProviderError as exc:
            raise SetupFailure("Vercel deployment", "deployment_failed") from None
        if not url:
            raise SetupFailure("Vercel deployment", "deployment_url_unavailable")
        return url if url.startswith("http") else f"https://{url}"

    # ---------------------------------------------------------------- node
    def create_pairing_invite(self, url: str, setup_key: str, *, enable_sharing: bool = False) -> dict:
        try:
            with self._httpx_client_factory(timeout=20, follow_redirects=False,
                                            trust_env=False) as client:
                response = client.post(
                    f"{url.rstrip('/')}/v1/setup/pairing",
                    headers={"authorization": f"Bearer {setup_key}"},
                    json={"label": "Nuvio TV", "enable_sharing": enable_sharing})
        except httpx.HTTPError:
            raise SetupFailure("Nuvio pairing invite", "provider_unavailable") from None
        if response.status_code >= 400:
            raise SetupFailure("Nuvio pairing invite", "pairing_endpoint_unavailable")
        try:
            body = response.json()
        except ValueError:
            raise SetupFailure("Nuvio pairing invite", "invalid_invite_response") from None
        if not isinstance(body, dict):
            raise SetupFailure("Nuvio pairing invite", "invalid_invite_response")
        return body

    def check_health(self, url: str, *, pending: bool = True) -> bool:
        try:
            with self._httpx_client_factory(timeout=15, follow_redirects=False,
                                            trust_env=False) as client:
                response = client.get(f"{url.rstrip('/')}/v1/health")
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return False
        return (response.status_code == 200 and isinstance(data, dict)
                and data.get("service") == "openseek"
                and data.get("setup_pending", False) is pending)

    # ---------------------------------------------------------------- clients
    def _vercel(self) -> VercelClient:
        if not self._vercel_token:
            raise SetupFailure("Vercel sign-in", "auth")
        if self._vc is None:
            self._vc = VercelClient(self._vercel_token, transport=self._vercel_transport)
        return self._vc

    def _supabase(self) -> SupabaseProvisioner:
        if not self._supabase_token:
            raise SetupFailure("Supabase sign-in", "auth")
        if self._sc is None:
            self._sc = SupabaseProvisioner(self._supabase_token,
                                           transport=self._supabase_transport)
        return self._sc
