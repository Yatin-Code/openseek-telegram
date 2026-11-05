"""Interactive, resumable provisioning wizard for a participant-owned OpenSeek node.

The wizard drives the participant through: authorizing their own Vercel and Supabase
accounts, selecting or creating a Supabase project inside the Free allowance, applying
the OpenSeek schema and the private staging bucket, writing the dependent Production
environment variables, deploying their own Vercel node, and confirming `/v1/health`.

Guarantees:

* Resumable — every step is checkpointed to one 0600 JSON state file after it
  completes; a re-run skips completed steps and re-enters the one that stopped.
  Resource creation is guarded (get-before-create, env upsert, idempotent schema and
  bucket, recorded deployment ids), so an interruption never duplicates a resource.
* Zero spend — the provider calls never ask for a plan and the plain-language
  output never recommends one; free-allowance limits surface as explicit blocks
  with the exact recovery action (pause a project in Supabase Studio, wait for
  Vercel's daily deployment allowance, or pick an existing project).
* Secret discipline — provider tokens, the generated database password and the
  service-role key are read from `input_fn`, verified against the provider, kept in
  the transient state file between steps, and never printed, logged or written to an
  env file. `finalize` purges them from the durable state file at completion.

The wizard never writes vercel.json (the repository owns that file and its cron
choice is a separate ticket) and never writes a local env file: the node's settings
go to the Vercel project's own encrypted env store through the API.
"""
from __future__ import annotations

import re
import secrets
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx

from . import state as state_mod
from .errors import (AUTH, DEPLOYMENT, NOT_FOUND, PAUSED, PROVIDER, QUOTA,
                     SETUP_REQUIRED, TIMEOUT, ProviderError, code_for_status,
                     transport_error)
from .staging import DEFAULT_STAGING_BUCKET, ensure_staging_bucket
from .supabase import FREE_PROJECT_ALLOWANCE, SupabaseProvisioner, project_state
from .vercel import VercelClient
from .state import StepRecord, WizardState, purge_secrets

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "supabase" / "schema.sql"
REF_RE = re.compile(r"[a-z0-9]{8,30}\Z")
PROJECT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,62}\Z")

STEPS = ("vercel_connect", "supabase_connect", "supabase_project", "apply_schema",
         "configure_staging", "configure_node_env", "deploy_node", "wait_deploy",
         "verify_health", "finalize")

STEP_TITLES = {
    "vercel_connect": "Connect Vercel account",
    "supabase_connect": "Connect Supabase account",
    "supabase_project": "Prepare Supabase project",
    "apply_schema": "Apply OpenSeek schema",
    "configure_staging": "Prepare staging bucket",
    "configure_node_env": "Node environment settings",
    "deploy_node": "Deploy OpenSeek function",
    "wait_deploy": "Wait for the deployment",
    "verify_health": "Verify node health",
    "finalize": "Finish securely",
}

# readyState values where waiting more cannot help.
_DEPLOYMENT_TERMINAL = ("ERROR", "CANCELED", "BLOCKED")


def node_env(state: WizardState) -> dict[str, str]:
    """The Production environment variables the wizard can establish now.

    Exactly the key set in NODE_ENV_KEYS: the profile and pool mode, the Supabase
    origin derived from the project ref (no credentials, query or fragment), the
    service-role key, and the staging bucket name. The node's remaining Telegram and
    owner settings arrive in a later setup step. The dict carries the service-key
    secret and is only ever handed to the provider's encrypted-env endpoints.
    """
    return {
        "OPENSEEK_PROFILE": "vercel",
        "OPENSEEK_POOL_MODE": "channel",
        "OPENSEEK_SETUP_PENDING": "1",
        "OPENSEEK_SUPABASE_URL": state.supabase_url,
        "OPENSEEK_SUPABASE_SERVICE_KEY": state.service_key,
        "OPENSEEK_SUPABASE_BUCKET": state.staging_bucket or DEFAULT_STAGING_BUCKET,
    }


NODE_ENV_KEYS = frozenset(node_env(WizardState()))


def default_health_prober(node_url: str) -> dict:
    """Fetch `<node>/v1/health` and return its JSON body (no credentials involved).

    The wizard compares the payload to the real public-listener shape served by
    `openseek.app`: `{"service": "openseek", "api": <API_VERSION int>}`. A fresh
    deployment whose Production variables are still incomplete answers 503
    `setup_required` from the pre-configuration page instead; that surfaces as the
    typed SETUP_REQUIRED error, i.e. a resumable "deployment live, configuration
    pending" state rather than a hard failure.
    """
    try:
        response = httpx.get(node_url.rstrip("/") + "/v1/health",
                             trust_env=False, timeout=30.0)
    except httpx.HTTPError:
        raise transport_error("the new OpenSeek node") from None
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code == 503 and isinstance(payload, dict) \
            and payload.get("error") == "setup_required":
        raise ProviderError(
            SETUP_REQUIRED, 504,
            "The node is live, but its OpenSeek configuration is still incomplete: the "
            "Telegram and owner-secret variables are a later setup step. Finish that "
            "configuration, then check the health endpoint again.")
    if response.status_code != 200:
        raise ProviderError(
            PROVIDER, response.status_code,
            "The health check for the new node came back as an error. The deployment "
            "log in your Vercel dashboard explains why; check it, then run the wizard "
            "again.")
    if not isinstance(payload, dict):
        raise ProviderError(
            PROVIDER, response.status_code,
            "The node's /v1/health did not return the OpenSeek health document.")
    return payload


def _https_url(host: str) -> str:
    return host if "://" in host else f"https://{host}"


def _parse_project_ref(raw: str) -> str:
    """A Supabase ref from a bare ref or a project origin like https://xyz.supabase.co.

    Only the host is read: whatever the participant pastes is never stored as an URL
    with credentials, and the ref alone is what the data-plane origin is derived from.
    """
    text = raw.strip()
    if ".supabase.co" in text:
        text = (urlsplit(text if "://" in text else f"https://{text}")
                .hostname or "").partition(".")[0]
    return text if REF_RE.fullmatch(text) else ""


def _normalize_project_name(raw: str) -> str:
    """A Vercel-legal project name: lowercase letters, digits and hyphens."""
    text = re.sub(r"[^a-z0-9-]+", "-", raw.strip().lower()).strip("-")
    return text if PROJECT_NAME_RE.fullmatch(text) else ""


def schema_sql() -> str:
    """The idempotent OpenSeek Supabase schema (backend/openseek/supabase/schema.sql)."""
    return SCHEMA_PATH.read_text(encoding="utf-8")


def deployment_files() -> list[tuple[str, bytes]]:
    """Collect the sources Vercel needs to build the OpenSeek node.

    Package Python sources travel relative to the function entrypoint, plus the
    repository's build manifests when a source checkout provides them
    (requirements.txt is how Vercel installs dependencies; vercel.json, if present,
    is uploaded unchanged — the wizard never writes or edits that file).
    """
    package = Path(__file__).resolve().parent.parent
    backend = package.parent
    files: list[tuple[str, bytes]] = []
    for source in sorted(package.rglob("*.py")):
        if "__pycache__" in source.parts:
            continue
        files.append(("openseek/" + source.relative_to(package).as_posix(),
                      source.read_bytes()))
    for name in ("pyproject.toml", "requirements.txt", "vercel.json"):
        candidate = backend / name
        if candidate.is_file():
            files.append((name, candidate.read_bytes()))
    return files


class ProvisionWizard:
    """Drive the participant through provisioning, one resumable step at a time.

    `vercel_factory(token)` and `supabase_factory(token)` build the provider clients
    (the real CLI passes the thin wrappers; tests pass fakes over MockTransport, so
    the interactive token stays the only unknown until a step needs it).
    """

    def __init__(self, vercel_factory: Callable[[str], object],
                 supabase_factory: Callable[[str], object], *,
                 health_prober: Callable[[str], dict] = default_health_prober,
                 state_path: Path,
                 print_fn: Callable[[str], None] = print,
                 input_fn: Callable[[str], str] = input,
                 poll_s: float = 5.0, max_polls: int = 60,
                 bucket_transport=None):
        self.vercel_factory = vercel_factory
        self.supabase_factory = supabase_factory
        self._health_prober = health_prober
        self._state_path = Path(state_path)
        self._print_fn = print_fn
        self._input_fn = input_fn
        self._bucket_transport = bucket_transport   # test seam for the data-plane client
        self._poll_s = poll_s
        self._max_polls = max_polls
        self._vercel_client: VercelClient | None = None
        self._vercel_token = ""
        self._supabase_client: SupabaseProvisioner | None = None
        self._supabase_token = ""
        self._state = WizardState()

    # ---------------------------------------------------------------- public API
    def run(self) -> str:
        """Run (or resume) every step and return the summary status.

        A step failure never raises out of run(): it becomes a `failed` record in the
        state file plus a printed summary. A resumed invocation (a fresh wizard over
        the same state file) skips completed steps and re-enters the failed one.
        """
        loaded = state_mod.load(self._state_path)
        self._state = loaded if loaded is not None else WizardState()
        self._print_fn("OpenSeek node setup wizard (Vercel + Supabase)")
        self._print_fn("This provisions YOUR OWN node on your own provider accounts.")
        if loaded is None:
            self._print_fn("No previous attempt was found; starting from the beginning.")
        elif self._state.completed:
            self._print_fn(f"Resuming: {len(self._state.completed)} recorded step(s) so far.")
        status = "interrupted"
        try:
            for name in STEPS:
                record = self._state.completed.get(name)
                if record is not None and record.outcome == "done":
                    continue
                try:
                    detail = getattr(self, "_" + name)()
                    if not detail:
                        raise ProviderError(PROVIDER, 0,
                                            "That step finished without a summary text. "
                                            "Run the wizard again.")
                    self._state.completed[name] = StepRecord(name, "done", detail)
                except Exception as exc:                  # noqa: BLE001 - recorded below
                    self._record_failure(name, exc)
                    self._state.summary_status = self._failure_status(name)
                    self._save()
                    self._print_summary()
                    return self._state.summary_status
                self._save()
        finally:
            self._close_clients()
        status = "ready"
        self._state.summary_status = status
        self._save()
        self._print_summary()
        return status

    # ---------------------------------------------------------------- step methods
    def _vercel_connect(self) -> str:
        self._print_fn("Create a token for your own Vercel account at "
                       "https://vercel.com/account/tokens and paste it here "
                       "(it is not echoed back).")
        token = self._read_secret("Vercel token", self._state.vercel_token)
        self._state.vercel_token = token
        who = self._vercel().whoami()
        self._state.vercel_user = who.get("username") or who.get("id") or ""
        if not self._state.vercel_user:
            raise ProviderError(PROVIDER, 0,
                                "Vercel did not identify this account. Check the token "
                                "scopes on your account page and run the wizard again.")
        return f"Connected to Vercel as {self._state.vercel_user}."

    def _supabase_connect(self) -> str:
        self._print_fn("Create an access token for your own Supabase account at "
                       "https://supabase.com/dashboard/account/tokens (it starts with "
                       "sbp_) and paste it here (it is not echoed back).")
        token = self._read_secret("Supabase access token", self._state.supabase_token)
        if not token.startswith("sbp_"):
            raise ProviderError(AUTH, 0,
                                "A Supabase access token starts with sbp_. Create one "
                                "at https://supabase.com/dashboard/account/tokens and "
                                "run the wizard again.")
        self._state.supabase_token = token
        organizations = self._supabase().list_organizations()
        if not organizations:
            raise ProviderError(NOT_FOUND, 0,
                                "This Supabase token has no organizations. Create one "
                                "in the Supabase dashboard, then run the wizard again.")
        chosen = organizations[0]
        if len(organizations) > 1:
            slugs = ", ".join(row["slug"] for row in organizations)
            self._print_fn(f"This account has several organizations: {slugs}.")
            while True:
                slug = self._ask("Organization slug", chosen["slug"])
                match = next((row for row in organizations if row["slug"] == slug), None)
                if match is not None:
                    chosen = match
                    break
                self._print_fn("That slug is not on this account; choose one of the printed slugs.")
        self._state.supabase_org = chosen["slug"]
        return f"Connected to Supabase organization {self._state.supabase_org}."

    def _supabase_project(self) -> str:
        supa = self._supabase()
        state = self._state
        if not state.supabase_ref:
            self._print_fn("Which Supabase project should this node use?")
            self._print_fn("1) Create a new project in your organization")
            self._print_fn("2) Use an existing project")
            if self._menu_choice(("1", "2")) == "1":
                self._guard_free_slots(supa)
                state.db_password = state.db_password or secrets.token_urlsafe(24)
                name = re.sub(r"[^a-z0-9_]+", "_",
                              self._ask("New project name", "openseek").strip().lower())
                created = supa.create_project(state.supabase_org, name, state.db_password)
                state.supabase_project_name = created["name"]
                state.supabase_ref = created["ref"]
                if not created["ref"]:
                    raise ProviderError(PROVIDER, 0,
                                        "Supabase did not return a project ref, so the "
                                        "wizard cannot verify the new project. Run the "
                                        "wizard again to pick an existing project.")
                # Save BEFORE the (possibly long) initialization wait so a resume
                # never creates a second project.
                self._save()
            else:
                self._print_fn("Use the project's ref or origin, for example "
                               "https://xyz.supabase.co.")
                while True:
                    ref = _parse_project_ref(self._ask("Existing project ref", ""))
                    if ref:
                        break
                    self._print_fn("That is not a Supabase project ref or origin "
                                   "(8..30 lowercase characters).")
                project = supa.get_project(ref)
                if project is None:
                    raise ProviderError(NOT_FOUND, 404,
                                        f"Supabase has no project with ref {ref} on "
                                        "this account. Check the ref and run the "
                                        "wizard again.")
                state.supabase_project_name = project["name"]
                state.supabase_ref = project["ref"] or ref
        supa.wait_until_active(state.supabase_ref, poll_s=self._poll_s,
                               max_polls=self._max_polls)
        if not state.service_key:
            state.service_key = supa.service_key(state.supabase_ref)
        state.supabase_url = f"https://{state.supabase_ref}.supabase.co"
        return (f"Supabase project {state.supabase_project_name} "
                f"({state.supabase_ref}) is active.")

    def _apply_schema(self) -> str:
        self._supabase().run_sql(self._state.supabase_ref, schema_sql())
        return "Applied the OpenSeek schema (idempotent; service-role-only tables)."

    def _configure_staging(self) -> str:
        state = self._state
        detail = ensure_staging_bucket(self._supabase(), ref=state.supabase_ref,
                                       service_key=state.service_key,
                                       transport=self._bucket_transport)
        state.staging_bucket = DEFAULT_STAGING_BUCKET
        return detail

    def _configure_node_env(self) -> str:
        env = node_env(self._state)
        missing = [key for key in NODE_ENV_KEYS if not env.get(key)]
        if missing:
            raise ProviderError(PROVIDER, 0,
                                "The node's Supabase settings are incomplete in the "
                                "wizard state. Run the wizard again and finish the "
                                "Supabase project step first.")
        return ("Prepared " + ", ".join(sorted(env)) + " for the node's Production "
                "environment (the node's Telegram and owner settings are a later "
                "setup step).")

    def _deploy_node(self) -> str:
        state = self._state
        vercel = self._vercel()
        name = state.vercel_project
        if not name:
            self._print_fn("The node deploys as its own Vercel project on your account.")
            while True:
                name = _normalize_project_name(self._ask("Vercel project name", "openseek"))
                if name:
                    break
                self._print_fn("Use lowercase letters, digits and hyphens (2..63 characters).")
        env = node_env(state)
        project = vercel.get_project(name)
        state.vercel_project = name
        if project is None:
            vercel.create_project(name, env)
        else:
            missing = {key: value for key, value in env.items()
                       if key not in vercel.env_var_keys(name)}
            if missing:
                vercel.upsert_env_vars(name, missing)
        deployment = vercel.create_deployment(name, deployment_files())
        if not deployment.get("id"):
            raise ProviderError(PROVIDER, 0,
                                "Vercel did not return a deployment id, so the wizard "
                                "cannot watch the build. Run the wizard again to "
                                "retry; the project and its settings were kept.")
        state.deployment_id = str(deployment["id"])
        state.deployment_url = str(deployment.get("url") or "")
        return f"Created deployment {state.deployment_id} for project {name}."

    def _wait_deploy(self) -> str:
        state = self._state
        if not state.deployment_id:
            raise ProviderError(PROVIDER, 0,
                                "There is no deployment recorded to wait for. Run the "
                                "wizard again; it will deploy and then watch.")
        for _ in range(self._max_polls):
            snapshot = self._vercel().deployment_status(state.deployment_id)
            ready = str(snapshot.get("state") or "").upper()
            if ready == "READY":
                state.deployment_url = str(snapshot.get("url") or state.deployment_url)
                state.node_url = _https_url(state.deployment_url or state.deployment_id)
                return f"The deployment reports READY; the node is at {state.node_url}."
            if ready in _DEPLOYMENT_TERMINAL:
                raise VercelClient.deployment_failure(
                    ready, str(snapshot.get("reason") or ""))
            time.sleep(self._poll_s)          # QUEUED / INITIALIZING / BUILDING
        raise ProviderError(TIMEOUT, 0,
                            "Vercel is still building the deployment. Run the wizard "
                            "again to keep watching; nothing else has to change.")

    def _verify_health(self) -> str:
        state = self._state
        url = state.node_url or _https_url(state.deployment_url)
        body = self._health_prober(url)
        api = body.get("api")
        if not (body.get("service") == "openseek" and isinstance(api, int)
                and not isinstance(api, bool) and api >= 1):
            raise ProviderError(
                PROVIDER, 0,
                "The node answered, but /v1/health does not match the OpenSeek health "
                "document (service/api). Check the deployment and run the wizard "
                "again.")
        return f"Health check passed for {url}."

    def _finalize(self) -> str:
        purge_secrets(self._state)
        return "The state file no longer holds any provider token or key."

    # ---------------------------------------------------------------- plumbing
    def _vercel(self) -> VercelClient:
        token = self._state.vercel_token
        if self._vercel_client is None or token != self._vercel_token:
            self._close_clients()
            self._vercel_client = self.vercel_factory(token)
            self._vercel_token = token
        return self._vercel_client

    def _supabase(self) -> SupabaseProvisioner:
        token = self._state.supabase_token
        if self._supabase_client is None or token != self._supabase_token:
            self._close_clients()
            self._supabase_client = self.supabase_factory(token)   # type: ignore[name-defined]
        self._supabase_token = token
        return self._supabase_client

    def _close_clients(self) -> None:
        # A token change means the previous client now holds a stale credential.
        if self._vercel_client is None and self._supabase_client is None:
            return
        for client in (self._vercel_client, self._supabase_client):
            if client is not None:
                try:
                    client.close()
                except Exception:                 # noqa: BLE001 - shutdown only
                    pass
                if client is self._vercel_client:
                    self._vercel_client = None
                else:
                    self._supabase_client = None

    def _read_secret(self, prompt: str, remembered: str) -> str:
        """Read one credential without ever printing it; blank reuses the saved one."""
        if remembered:
            value = self._input_fn(f"{prompt} (press Enter to reuse the one from the "
                                   "previous attempt): ").strip()
            return value or remembered
        while True:
            value = self._input_fn(f"{prompt}: ").strip()
            if value:
                return value
            self._print_fn("A value is required.")

    def _ask(self, prompt: str, default: str) -> str:
        value = self._input_fn(f"{prompt} [{default}]: ").strip()
        return value or default

    def _menu_choice(self, allowed: tuple[str, ...]) -> str:
        while True:
            choice = input_text(self._input_fn, f"Choose [{'/'.join(allowed)}]", allowed[0])
            if choice in allowed:
                return choice
            self._print_fn("Choose " + ", ".join(allowed) + ".")

    def _guard_free_slots(self, supa: SupabaseProvisioner) -> None:
        """Refuse (non-fatally) to create a project beyond the Supabase Free allowance.

        The refusal is plain language, names the exact recovery action and never
        suggests a plan change. Supabase counts active Free projects per participant
        across organizations, so both the per-organization and the account total are
        respected.
        """
        projects = supa.list_projects()
        total = supa.total_active_projects(projects)
        organization = [row for row in projects
                        if row.get("organization_slug") == self._state.supabase_org
                        and project_state(row.get("status", "")) == "active"]
        if organization and (total >= FREE_PROJECT_ALLOWANCE
                             or len(organization) >= FREE_PROJECT_ALLOWANCE):
            raise ProviderError(QUOTA, 0,
                                "The Supabase Free plan allows 2 active projects and "
                                f"this account already has that many ({total} active). "
                                "Select one of your existing projects instead, or "
                                "pause one in the Supabase Studio dashboard and run "
                                "the wizard again. Waiting or pausing is all that is "
                                "needed; no plan change is required.")

    def _record_failure(self, name: str, exc: Exception) -> None:
        """Record a failed step; completed steps stay completed.

        Only ProviderError text is shown; any other exception is described without
        quoting it, so a stray exception carrying a credential can never leak into the
        transcript. A broken deployment releases its slot so a later run redeploys.
        """
        if isinstance(exc, ProviderError):
            code, status_, text = exc.code, exc.status, exc.plain
        else:
            code, status_, text = PROVIDER, 0, (
                "That step failed with an unexpected error. Run the wizard again "
                "to resume; completed steps stay completed.")
        self._state.completed[name] = StepRecord(name, "failed", text,
                                                 error=exc if isinstance(exc, ProviderError) else None)
        if name == "wait_deploy" and code in (DEPLOYMENT, NOT_FOUND, PROVIDER):
            self._state.completed.pop("deploy_node", None)
            self._state.deployment_id = ""
            self._state.deployment_url = ""
            self._state.node_url = ""
        if code == DEPLOYMENT and status_ == 0:
            pass                     # stays a provider failure (terminal build states)
        self._print_fn(f"{STEP_TITLES[name]} did not finish: {text}")
        self._print_fn("This attempt can be resumed by running the wizard again.")

    def _failure_status(self, name: str) -> str:
        return _status_for(self._state.completed.get(name))

    def _save(self) -> None:
        state_mod.save(self._state_path, self._state)

    def _print_summary(self) -> None:
        summary = self._state
        self._print_fn(f"Setup status: {summary.summary_status}")
        for name, record in sorted(summary.completed.items()):
            self._print_fn(f"  {STEP_TITLES.get(name, name)} ({record.outcome}): {record.detail}")
        if summary.summary_status == "ready":
            self._print_fn(f"Your OpenSeek node is live at {summary.node_url}")
            self._print_fn("Keep the URL: the node's Telegram and owner configuration "
                           "steps arrive in the next setup tickets "
                           "(see docs/PROVISION-WIZARD.md).")


def _status_for(record: StepRecord | None) -> str:
    """Map a failed step's ProviderError into the wizard's summary status machine."""
    if record is None or record.outcome != "failed":
        return "interrupted"
    error = getattr(record, "error", None)
    if error is None:            # plain failures carry their meaning in `detail`
        return "failed_provider"
    if error.code == AUTH:
        return "failed_auth"
    if error.code == QUOTA:
        return "blocked_quota"
    if error.code == PAUSED:
        return "blocked_paused"
    if error.code in (TIMEOUT, SETUP_REQUIRED) or error.status == 0 and error.code == PROVIDER:
        return "interrupted"
    return "failed_provider"


def input_text(input_fn: Callable[[str], str], prompt: str, default: str) -> str:
    """Alternative to `_ask` used by menu loops (kept beside `_ask` for clarity)."""
    value = input_fn(f"{prompt} [{default}]: ").strip()
    return value or default
