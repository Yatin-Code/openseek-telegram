"""G7 #50 regression: interrupted `openseek setup` resumes without repeating
completed provisioning steps.

Scenario under test: schema and private bucket provisioning succeeded, then a
Vercel-side step failed. Resuming on the same resume state must skip the finished
provider work (schema apply, bucket configure, project selection/creation) and
only perform what the interruption left undone.

This is a deterministic regression loop against `ParticipantSetup` with a
recording fake provider. It never signs in to Vercel/Supabase and never touches
the network.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from openseek.participant_setup import ParticipantSetup, SetupFailure


class RecordingProvider:
    """SetupProvider double that logs every call and fails on demand."""

    def __init__(self, *, supabase=None, vercel=None):
        self.calls = []
        self.schema_refs = []
        self.bucket_urls = []
        self.env = {}
        self.health = []
        self.fail_deploy = False
        self.fail_secret = None  # env key whose set_vercel_secret call fails
        self.fail_create_supabase = False
        self.fail_create_vercel = False
        self.fail_bucket = False
        self.fail_health = False
        self.fail_auth = False
        self.supabase = list(supabase if supabase is not None
                             else [{"id": "sb-ref-1", "name": "my-supabase"}])
        self.vercel = list(vercel if vercel is not None
                           else [{"id": "prj-1", "name": "my-openseek-node"}])

    def authenticate(self):
        self.calls.append("authenticate")
        if self.fail_auth:
            raise SetupFailure("Vercel sign-in", "provider_error")

    def supabase_projects(self):
        self.calls.append("supabase_projects")
        return [dict(project) for project in self.supabase]

    def supabase_organizations(self):
        self.calls.append("supabase_organizations")
        return [{"id": "org-1", "name": "My account"}]

    def create_supabase_project(self, name, organization, region):
        self.calls.append("create_supabase_project")
        project = {"id": "created-sb", "name": name}
        self.supabase.append(project)  # the resource exists even if the CLI was interrupted
        if self.fail_create_supabase:
            raise SetupFailure("Supabase project creation", "quota")
        return dict(project)

    def apply_schema(self, project_ref, state_dir, schema_path):
        assert schema_path.is_file()
        self.schema_refs.append(project_ref)
        self.calls.append("apply_schema")

    def supabase_credentials(self, project):
        self.calls.append("supabase_credentials")
        return "https://pool.supabase.co", "sb_secret_DO_NOT_PRINT"

    def configure_private_bucket(self, url, secret):
        self.bucket_urls.append(url)
        self.calls.append("configure_private_bucket")
        if self.fail_bucket:
            raise SetupFailure("Supabase staging bucket", "provider_error")

    def vercel_projects(self):
        self.calls.append("vercel_projects")
        return [dict(project) for project in self.vercel]

    def create_vercel_project(self, source_dir):
        self.calls.append("create_vercel_project")
        project = {"id": "prj-created", "name": "created-node"}
        self.vercel.append(project)  # the resource exists even if the CLI was interrupted
        if self.fail_create_vercel:
            raise SetupFailure("Vercel project creation", "quota")
        return dict(project)

    def link_vercel_project(self, source_dir, project):
        self.calls.append("link_vercel_project")

    def set_vercel_secret(self, source_dir, name, value, *, secret):
        self.calls.append(f"set_vercel_secret({name})")
        if self.fail_secret == name:
            raise SetupFailure(f"Vercel {name} configuration", "provider_error")
        self.env[name] = (value, secret)

    def remove_vercel_secret(self, source_dir, name):
        self.calls.append(f"remove_vercel_secret({name})")
        self.env.pop(name, None)

    def create_pairing_invite(self, url, setup_key, *, enable_sharing=False):
        self.calls.append("create_pairing_invite")
        return {"code": "ABCD-EFGH-IJKL-MNOP", "spki_pin": ""}

    def deploy(self, source_dir):
        self.calls.append("deploy")
        if self.fail_deploy:
            raise SetupFailure("Vercel deployment", "provider_error")
        return "https://my-openseek-node.vercel.app"

    def check_health(self, url, *, pending=True):
        self.calls.append(f"check_health(pending={pending})")
        self.health.append(pending)
        return not self.fail_health


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "backend"
    source.mkdir(exist_ok=True)
    (source / "vercel.json").write_text("{}\n", encoding="utf-8")
    (source / "pyproject.toml").write_text("[project]\nname='openseek'\n", encoding="utf-8")
    return source


def _state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))


def _no_input(prompt: str) -> str:
    raise AssertionError(f"resume must use saved state, not prompt for input: {prompt!r}")


def _local_secret_leak(tmp_path: Path) -> str:
    """Return the first local file mentioning the provider secret, or ''."""
    for path in sorted(tmp_path.rglob("*")):
        if path.is_file():
            try:
                if "sb_secret_DO_NOT_PRINT" in path.read_text(encoding="utf-8", errors="ignore"):
                    return str(path)
            except OSError:
                pass
    return ""


def test_resume_after_vercel_deploy_failure_skips_completed_provisioning(tmp_path):
    """G7 #50: a deploy failure after Supabase schema/bucket succeeded, when
    resumed, must not repeat apply_schema, the private bucket setup, or the
    credential fetch, and must not prompt for already-saved selections."""
    provider = RecordingProvider()
    provider.fail_deploy = True
    state_path = tmp_path / "state.json"
    output = []
    answers = iter(["1", "1"])
    first = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                             input_fn=lambda _prompt: next(answers), print_fn=output.append)
    assert first.run() == 1
    assert _state(tmp_path)["step"] == "vercel_configured"
    assert provider.schema_refs == ["sb-ref-1"]
    assert provider.bucket_urls == ["https://pool.supabase.co"]
    token_path = tmp_path / "participant-setup.admin-token"
    admin_token = token_path.read_text(encoding="utf-8").strip()

    provider.fail_deploy = False
    output.clear()
    resumed = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                               input_fn=_no_input, print_fn=output.append)
    assert resumed.run() == 0

    calls = provider.calls
    assert calls.count("apply_schema") == 1, calls
    assert calls.count("configure_private_bucket") == 1, calls
    assert calls.count("supabase_credentials") == 1, calls
    assert calls.count("create_supabase_project") == 0, calls
    assert calls.count("create_vercel_project") == 0, calls
    assert calls.count("deploy") == 2, calls  # failed first attempt + resumed success
    assert provider.env["OPENSEEK_ADMIN_TOKEN"] == (admin_token, True)

    state = _state(tmp_path)
    assert state["step"] == "healthy"
    state_blob = json.dumps(state)
    combined_output = "\n".join(output)
    assert "sb_secret_DO_NOT_PRINT" not in state_blob
    assert "sb_secret_DO_NOT_PRINT" not in combined_output
    assert _local_secret_leak(tmp_path) == ""
    assert "OPENSEEK_ADMIN_TOKEN" not in state_blob
    assert "OPENSEEK_SETUP_KEY" not in state_blob
    if os.name == "posix":
        assert state_path.stat().st_mode & 0o777 == 0o600
        assert token_path.stat().st_mode & 0o777 == 0o600


def test_resume_after_vercel_secret_configuration_failure_skips_completed_provisioning(tmp_path):
    """G7 #50: failure after Supabase schema/bucket succeeded (state
    `vercel_selected`), when resumed, must not repeat schema or bucket work.
    The service key may only be re-read, never stored locally or re-provisioned."""
    provider = RecordingProvider()
    provider.fail_secret = "OPENSEEK_ADMIN_TOKEN"
    state_path = tmp_path / "state.json"
    answers = iter(["1", "1"])
    first = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                             input_fn=lambda _prompt: next(answers), print_fn=lambda _line: None)
    assert first.run() == 1
    assert _state(tmp_path)["step"] == "vercel_selected"
    assert provider.schema_refs == ["sb-ref-1"]
    assert provider.calls.count("deploy") == 0, provider.calls

    provider.fail_secret = None
    resumed = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                               input_fn=_no_input, print_fn=lambda _line: None)
    assert resumed.run() == 0

    calls = provider.calls
    assert calls.count("apply_schema") == 1, calls
    assert calls.count("configure_private_bucket") == 1, calls
    assert calls.count("supabase_credentials") == 2, calls  # re-read only: never saved locally
    assert calls.count("deploy") == 1, calls
    assert provider.env["OPENSEEK_ADMIN_TOKEN"][1] is True
    assert provider.env["OPENSEEK_SUPABASE_SERVICE_KEY"] == ("sb_secret_DO_NOT_PRINT", True)

    state = _state(tmp_path)
    assert state["step"] == "healthy"
    assert "sb_secret_DO_NOT_PRINT" not in json.dumps(state)
    if os.name == "posix":
        assert state_path.stat().st_mode & 0o777 == 0o600


def test_interrupted_supabase_project_creation_resumes_without_recreating(tmp_path):
    """A Supabase creation that was interrupted after the resource came into
    being is discovered by the recorded name on resume: no re-prompt of the
    creation flow, no second create call."""
    provider = RecordingProvider(supabase=[])
    provider.fail_create_supabase = True
    state_path = tmp_path / "state.json"
    first_answers = iter(["1", "pool-node", "ap-south-1"])
    first = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                             input_fn=lambda _prompt: next(first_answers),
                             print_fn=lambda _line: None)
    assert first.run() == 1
    state = _state(tmp_path)
    assert state["supabase_create_name"] == "pool-node"
    assert provider.calls.count("create_supabase_project") == 1, provider.calls

    provider.fail_create_supabase = False
    prompts = []
    lines = []
    resumed_answers = iter(["1"])  # only the Vercel project chooser remains
    resumed = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                               input_fn=lambda prompt: (prompts.append(prompt),
                                                        next(resumed_answers))[1],
                               print_fn=lines.append)
    assert resumed.run() == 0

    assert provider.calls.count("create_supabase_project") == 1, provider.calls
    assert prompts == ["Choose a number: "], prompts
    assert not any("Supabase" in line for line in lines), lines
    state = _state(tmp_path)
    assert state["step"] == "healthy"
    assert state["supabase_ref"] == "created-sb"


def test_interrupted_vercel_project_creation_resumes_from_project_list_diff(tmp_path):
    """A Vercel creation interrupted before selection completes is found by the
    recorded before-list diff on resume: no prompt, no second create call."""
    provider = RecordingProvider(vercel=[])
    provider.fail_create_vercel = True
    state_path = tmp_path / "state.json"
    first_answers = iter(["1"])  # Supabase chooser; Vercel creation is promptless on an empty list
    first = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                             input_fn=lambda _prompt: next(first_answers),
                             print_fn=lambda _line: None)
    assert first.run() == 1
    state = _state(tmp_path)
    assert state["vercel_creating"] is True
    assert state["vercel_projects_before"] == []

    provider.fail_create_vercel = False
    resumed = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                               input_fn=_no_input, print_fn=lambda _line: None)
    assert resumed.run() == 0

    assert provider.calls.count("create_vercel_project") == 1, provider.calls
    state = _state(tmp_path)
    assert state["step"] == "healthy"
    assert state["vercel_project"] == "created-node"
    assert "vercel_creating" not in state
    assert "vercel_projects_before" not in state
    if os.name == "posix":
        assert state_path.stat().st_mode & 0o777 == 0o600


def test_resume_after_bucket_failure_applies_schema_once(tmp_path):
    """A staging-bucket failure right after the SQL apply lands a `schema_applied`
    checkpoint; resuming must not re-apply the schema."""
    provider = RecordingProvider()
    provider.fail_bucket = True
    state_path = tmp_path / "state.json"
    answers = iter(["1", "1"])
    first = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                             input_fn=lambda _prompt: next(answers), print_fn=lambda _line: None)
    assert first.run() == 1
    assert _state(tmp_path)["step"] == "schema_applied"
    assert provider.schema_refs == ["sb-ref-1"]

    provider.fail_bucket = False
    lines = []
    resumed_answers = iter(["1"])  # only the Vercel project chooser remains
    resumed = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                               input_fn=lambda prompt: (lines.append("input:" + prompt),
                                                        next(resumed_answers))[1],
                               print_fn=lines.append)
    assert resumed.run() == 0, "\n".join(lines)

    calls = provider.calls
    assert calls.count("apply_schema") == 1, calls
    assert calls.count("configure_private_bucket") == 2, calls  # failed attempt + one resume retry
    assert calls.count("supabase_credentials") == 2, calls      # one fetch per invocation
    assert calls.count("deploy") == 1, calls
    assert not any("Supabase" in line for line in lines), lines
    state = _state(tmp_path)
    assert state["step"] == "healthy"
    assert "sb_secret_DO_NOT_PRINT" not in json.dumps(state)


def test_resume_after_health_failure_does_not_redeploy_or_reconfigure(tmp_path):
    """A failed health check lands the `deployed` checkpoint; resuming must only
    re-check health: no redeploy, no relink, no env upserts, no credential fetch."""
    provider = RecordingProvider()
    provider.fail_health = True
    state_path = tmp_path / "state.json"
    answers = iter(["1", "1"])
    first = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                             input_fn=lambda _prompt: next(answers), print_fn=lambda _line: None)
    assert first.run() == 1
    state = _state(tmp_path)
    assert state["step"] == "deployed"
    assert state["deployment_url"] == "https://my-openseek-node.vercel.app"

    provider.fail_health = False
    resumed = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                               input_fn=_no_input, print_fn=lambda _line: None)
    assert resumed.run() == 0

    calls = provider.calls
    assert calls.count("deploy") == 1, calls
    assert calls.count("link_vercel_project") == 2, calls  # bootstrap link once per invocation
    assert calls.count("supabase_credentials") == 1, calls
    assert calls.count("apply_schema") == 1, calls
    assert calls.count("configure_private_bucket") == 1, calls
    assert calls.count("set_vercel_secret(OPENSEEK_PROFILE)") == 1, calls
    assert provider.health == [True, True]
    assert _state(tmp_path)["step"] == "healthy"


def test_auth_failure_during_resume_preserves_saved_checkpoint(tmp_path):
    """Re-authentication happens every invocation but a failed one must leave the
    saved step untouched, and a later run resumes from that exact checkpoint."""
    provider = RecordingProvider()
    provider.fail_deploy = True
    state_path = tmp_path / "state.json"
    answers = iter(["1", "1"])
    first = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                             input_fn=lambda _prompt: next(answers), print_fn=lambda _line: None)
    assert first.run() == 1
    assert _state(tmp_path)["step"] == "vercel_configured"

    provider.fail_deploy = False
    provider.fail_auth = True
    resumed = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                               input_fn=_no_input, print_fn=lambda _line: None)
    assert resumed.run() == 1
    assert _state(tmp_path)["step"] == "vercel_configured"

    provider.fail_auth = False
    recovered = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                                 input_fn=_no_input, print_fn=lambda _line: None)
    assert recovered.run() == 0
    assert _state(tmp_path)["step"] == "healthy"


def test_partial_vercel_configuration_resume_skips_completed_env_keys(tmp_path):
    """A failure between env upserts records the finished key names only and
    resuming must not re-upsert those completed keys."""
    provider = RecordingProvider()
    provider.fail_secret = "OPENSEEK_ADMIN_TOKEN"
    state_path = tmp_path / "state.json"
    answers = iter(["1", "1"])
    first = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                             input_fn=lambda _prompt: next(answers), print_fn=lambda _line: None)
    assert first.run() == 1
    state = _state(tmp_path)
    assert state["step"] == "vercel_selected"
    assert state["bootstrap_env_done"] == ["OPENSEEK_POOL_MODE", "OPENSEEK_PROFILE",
                                           "OPENSEEK_SETUP_PENDING"]
    assert "sb_secret_DO_NOT_PRINT" not in json.dumps(state)  # no secret in partial progress

    provider.fail_secret = None
    resumed = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                               input_fn=_no_input, print_fn=lambda _line: None)
    assert resumed.run() == 0

    calls = provider.calls
    for key in ("OPENSEEK_PROFILE", "OPENSEEK_POOL_MODE", "OPENSEEK_SETUP_PENDING",
                "OPENSEEK_SUPABASE_URL", "OPENSEEK_SUPABASE_SERVICE_KEY",
                "OPENSEEK_SUPABASE_BUCKET"):
        assert calls.count(f"set_vercel_secret({key})") == 1, (key, calls)
    assert calls.count("set_vercel_secret(OPENSEEK_ADMIN_TOKEN)") == 2, calls  # failed try + resumed upsert
    assert calls.count("supabase_credentials") == 2, calls  # one fetch per invocation
    state = _state(tmp_path)
    assert state["step"] == "healthy"
    assert "bootstrap_env_done" not in state  # cleared with the vercel_configured save
    assert "sb_secret_DO_NOT_PRINT" not in json.dumps(state)


def test_missing_saved_resource_in_progressed_state_fails_closed(tmp_path):
    """A progressed state whose saved ref/name is gone must fail with a named
    message and no side effects: never a silent re-choice or recreation."""
    from openseek.participant_setup import _atomic_state

    provider = RecordingProvider()
    state_path = tmp_path / "state.json"
    _atomic_state(state_path, {"version": 1, "step": "vercel_selected",
                               "vercel_project": "my-openseek-node"})  # no supabase_ref
    prompts, lines = [], []
    setup = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                             input_fn=lambda prompt: (prompts.append(prompt), "1")[1],
                             print_fn=lines.append)
    assert setup.run() == 1
    assert _state(tmp_path)["step"] == "vercel_selected"  # checkpoint preserved
    assert prompts == [], prompts
    assert provider.calls.count("create_supabase_project") == 0, provider.calls
    assert provider.env == {}
    assert any("Supabase project selection" in line for line in lines)

    provider2 = RecordingProvider()
    state2 = tmp_path / "state2.json"
    _atomic_state(state2, {"version": 1, "step": "vercel_selected",
                           "supabase_ref": "sb-ref-1"})  # no vercel_project
    prompts2 = []
    setup2 = ParticipantSetup(provider2, source_dir=_source(tmp_path), state_path=state2,
                              input_fn=lambda prompt: (prompts2.append(prompt), "1")[1],
                              print_fn=lambda _line: None)
    assert setup2.run() == 1
    assert json.loads(state2.read_text(encoding="utf-8"))["step"] == "vercel_selected"
    assert prompts2 == [], prompts2
    assert provider2.calls.count("create_vercel_project") == 0, provider2.calls
    assert provider2.env == {}


def test_missing_saved_supabase_ref_at_vercel_configured_fails_closed(tmp_path):
    """At or past `vercel_configured` no key fetch happens, yet the saved ref is
    still required: a progressed state without one must exit safely without a
    deploy, env upsert, prompt or recreation."""
    from openseek.participant_setup import _atomic_state

    provider = RecordingProvider()
    state_path = tmp_path / "state.json"
    _atomic_state(state_path, {"version": 1, "step": "vercel_configured",
                               "vercel_project": "my-openseek-node"})  # no supabase_ref
    prompts, lines = [], []
    setup = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                             input_fn=lambda prompt: (prompts.append(prompt), "1")[1],
                             print_fn=lines.append)
    assert setup.run() == 1
    assert _state(tmp_path)["step"] == "vercel_configured"  # checkpoint preserved
    assert prompts == [], prompts
    calls = provider.calls
    assert calls.count("deploy") == 0, calls
    assert calls.count("create_supabase_project") == 0, calls
    assert calls.count("supabase_projects") == 0, calls  # no relist to find a substitute
    assert calls.count("set_vercel_secret(OPENSEEK_PROFILE)") == 0, calls
    assert any("Supabase project selection" in line for line in lines), lines


def test_deployed_checkpoint_with_non_vercel_url_fails_before_health(tmp_path):
    """A saved deployment URL is validated before any resumed health check: a
    non-HTTPS/non-Vercel value fails named, with no network health call and no
    redeploy, and the checkpoint is preserved."""
    from openseek.participant_setup import _atomic_state

    for url, tag in (("http://attacker.example.com/health", "state-http"),
                     ("https://attacker.example.com", "state-https")):
        provider = RecordingProvider()
        state_path = tmp_path / tag
        _atomic_state(state_path, {"version": 1, "step": "deployed", "deployment_url": url,
                                   "supabase_ref": "sb-ref-1",
                                   "vercel_project": "my-openseek-node"})
        prompts, lines = [], []
        setup = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                                 input_fn=lambda prompt: (prompts.append(prompt), "1")[1],
                                 print_fn=lines.append)
        assert setup.run() == 1, url
        assert json.loads(state_path.read_text(encoding="utf-8"))["step"] == "deployed"
        assert provider.health == [], (url, provider.calls)  # no network call from state
        calls = provider.calls
        assert calls.count("deploy") == 0, calls
        assert calls.count("link_vercel_project") == 0, calls
        assert calls.count("check_health") == 0, calls
        assert provider.env == {}
        assert prompts == [], prompts
        assert any("health check" in line for line in lines), lines


def _sso_rest_provider(vercel_calls, supabase_calls, *, patch_status=200):
    """Real RestSetupProvider over canned doubles: SSO-protected project, node health."""
    import httpx

    from openseek.provision.provider import RestSetupProvider

    def vercel_handler(request: httpx.Request) -> httpx.Response:
        vercel_calls.append((request.method, request.url.path, request.content))
        if request.method == "GET" and request.url.path == "/v2/user":
            return httpx.Response(200, json={"user": {"id": "u1", "username": "tester"}})
        if request.method == "GET" and request.url.path == "/v9/projects/openseek-g7-qa":
            return httpx.Response(200, json={
                "id": "prj_example_g7", "name": "openseek-g7-qa",
                "ssoProtection": {"deploymentType": "all_except_custom_domains"}})
        if request.method == "PATCH" and request.url.path == "/v9/projects/openseek-g7-qa":
            if patch_status == 200:
                return httpx.Response(200, json={
                    "id": "prj_example_g7", "name": "openseek-g7-qa",
                    "ssoProtection": {"deploymentType": "preview"}})
            return httpx.Response(patch_status, json={"error": {"code": "forbidden"}})
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    def supabase_handler(request: httpx.Request) -> httpx.Response:
        supabase_calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/v1/organizations":
            return httpx.Response(200, json=[{"id": "org_uuid", "slug": "my-org"}])
        return httpx.Response(404, json={"message": "not found"})

    node_calls = []

    def node_handler(request: httpx.Request) -> httpx.Response:
        node_calls.append(request.url.path)
        if request.url.path == "/v1/health":
            return httpx.Response(200, json={"service": "openseek", "api": 1,
                                             "setup_pending": True})
        return httpx.Response(404)

    def node_factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(node_handler), **kwargs)

    provider = RestSetupProvider(
        vercel_token="vercel-token", supabase_token="sbp_test_token",
        vercel_transport=httpx.MockTransport(vercel_handler),
        supabase_transport=httpx.MockTransport(supabase_handler),
        httpx_client_factory=node_factory)
    provider._node_calls = node_calls
    return provider


def test_deployed_resume_restores_production_access_before_health(tmp_path):
    """A resume from the `deployed` checkpoint re-binds the recorded project
    (the once-needed production access repair) before checking health, with no
    env, schema, bucket, creation or deployment work repeated."""
    from openseek.participant_setup import _atomic_state

    vercel_calls, supabase_calls = [], []
    provider = _sso_rest_provider(vercel_calls, supabase_calls)
    state_path = tmp_path / "state.json"
    _atomic_state(state_path, {
        "version": 1, "step": "deployed",
        "deployment_url": "https://participant-example.vercel.app",
        "supabase_ref": "sb-ref-1", "supabase_url": "https://sb-ref-1.supabase.co",
        "vercel_project": "openseek-g7-qa"})
    output = []
    setup = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                             input_fn=_no_input, print_fn=output.append)
    try:
        assert setup.run() == 0, "\n".join(output)

        patches = [(method, path, body) for method, path, body
                   in vercel_calls if method == "PATCH"]
        assert len(patches) == 1, vercel_calls
        assert patches[0][1] == "/v9/projects/openseek-g7-qa"
        assert json.loads(patches[0][2]) == {"ssoProtection": {"deploymentType": "preview"}}
        mutating = [(method, path) for method, path, _ in vercel_calls if method == "POST"]
        assert mutating == [], vercel_calls          # no deploy, no create, no env write
        assert supabase_calls == [("GET", "/v1/organizations")], supabase_calls  # auth only
        assert set(provider._node_calls) == {"/v1/health"}, provider._node_calls
        assert json.loads(state_path.read_text(encoding="utf-8"))["step"] == "healthy"
    finally:
        provider.close()


def test_production_access_refusal_preserves_deployed_checkpoint(tmp_path):
    """A refused access change fails named, keeps the `deployed` checkpoint and
    never reaches the node's health endpoint."""
    from openseek.participant_setup import _atomic_state

    vercel_calls, supabase_calls = [], []
    provider = _sso_rest_provider(vercel_calls, supabase_calls, patch_status=403)
    state_path = tmp_path / "state.json"
    _atomic_state(state_path, {
        "version": 1, "step": "deployed",
        "deployment_url": "https://participant-example.vercel.app",
        "supabase_ref": "sb-ref-1", "supabase_url": "https://sb-ref-1.supabase.co",
        "vercel_project": "openseek-g7-qa"})
    lines = []
    setup = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                             input_fn=_no_input, print_fn=lines.append)
    try:
        assert setup.run() == 1, "\n".join(lines)
        assert json.loads(state_path.read_text(encoding="utf-8"))["step"] == "deployed"
        assert provider._node_calls == []
        assert any("Vercel production access" in line for line in lines), lines
    finally:
        provider.close()
