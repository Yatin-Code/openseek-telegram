from __future__ import annotations

import json
from pathlib import Path

from openseek.participant_setup import ParticipantSetup
from openseek.telegram_setup import TelegramSetupResult


class FakeProvider:
    def __init__(self, *, fail_deploy=False, empty=False):
        self.fail_deploy = fail_deploy
        self.auth_calls = 0
        self.schema_calls = []
        self.bucket_calls = []
        self.env = {}
        self.deploy_calls = 0
        self.health_calls = []
        self.health_pending = []
        self.pairing_calls = []
        self.create_supabase_calls = 0
        self.create_vercel_calls = 0
        self.projects = [] if empty else [{"id": "sb-ref-1", "name": "my-supabase"}]
        self.vercel = [] if empty else [{"name": "my-openseek-node", "id": "prj-1"}]

    def authenticate(self):
        self.auth_calls += 1

    def supabase_projects(self):
        return list(self.projects)

    def supabase_organizations(self):
        return [{"id": "org-1", "name": "My account"}]

    def create_supabase_project(self, name, organization, region):
        self.create_supabase_calls += 1
        project = {"id": "created-ref", "name": name}
        self.projects.append(project)
        return project

    def apply_schema(self, project_ref, state_dir, schema_path):
        assert project_ref
        assert schema_path.is_file()
        self.schema_calls.append((project_ref, state_dir))

    def supabase_credentials(self, project):
        return "https://pool.supabase.co", "sb_secret_DO_NOT_PRINT"

    def configure_private_bucket(self, url, secret):
        self.bucket_calls.append((url, secret))

    def vercel_projects(self):
        return list(self.vercel)

    def create_vercel_project(self, source_dir):
        self.create_vercel_calls += 1
        project = {"id": "created-vercel", "name": "created-node"}
        self.vercel.append(project)
        return project

    def link_vercel_project(self, source_dir, project):
        assert any(row.get("name") == project.get("name") for row in self.vercel)

    def set_vercel_secret(self, source_dir, name, value, *, secret):
        self.env[name] = (value, secret)

    def remove_vercel_secret(self, source_dir, name):
        self.env.pop(name, None)

    def create_pairing_invite(self, url, setup_key, *, enable_sharing=False):
        self.pairing_calls.append((url, setup_key, enable_sharing))
        return {"code": "ABCD-EFGH-IJKL-MNOP", "spki_pin": ""}

    def deploy(self, source_dir):
        self.deploy_calls += 1
        if self.fail_deploy:
            from openseek.participant_setup import SetupFailure
            raise SetupFailure("Vercel deployment", "provider_error")
        return "https://my-openseek-node.vercel.app"

    def check_health(self, url, *, pending=True):
        self.health_calls.append(url)
        self.health_pending.append(pending)
        return True


class FakeTelegramJoiner:
    def __init__(self):
        self.calls = []

    def join(self, api_id, api_hash, invite):
        self.calls.append((api_id, api_hash, invite))
        return TelegramSetupResult("telegram_session_secret", "-100123456")


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "backend"
    source.mkdir(exist_ok=True)
    (source / "vercel.json").write_text("{}\n", encoding="utf-8")
    (source / "pyproject.toml").write_text("[project]\nname='openseek'\n", encoding="utf-8")
    return source


def test_participant_wizard_provisions_and_health_checks_own_node(tmp_path):
    provider = FakeProvider()
    output = []
    answers = iter(["1", "1"])
    setup = ParticipantSetup(provider, source_dir=_source(tmp_path),
                             state_path=tmp_path / "state.json", print_fn=output.append,
                             input_fn=lambda _prompt: next(answers))

    assert setup.run() == 0

    assert provider.auth_calls == 1
    assert provider.schema_calls[0][0] == "sb-ref-1"
    assert provider.bucket_calls == [("https://pool.supabase.co", "sb_secret_DO_NOT_PRINT")]
    assert provider.env["OPENSEEK_SETUP_PENDING"] == ("1", False)
    admin_token, admin_secret = provider.env["OPENSEEK_ADMIN_TOKEN"]
    assert admin_secret is True and len(admin_token) >= 32
    assert provider.env["OPENSEEK_SUPABASE_SERVICE_KEY"] == ("sb_secret_DO_NOT_PRINT", True)
    assert provider.deploy_calls == 1
    assert provider.health_calls == ["https://my-openseek-node.vercel.app"]
    assert "sb_secret_DO_NOT_PRINT" not in "\n".join(output)
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["step"] == "healthy"
    assert "sb_secret_DO_NOT_PRINT" not in json.dumps(state)
    assert "OPENSEEK_ADMIN_TOKEN" not in json.dumps(state)
    token_path = tmp_path / "participant-setup.admin-token"
    assert token_path.read_text().strip() == admin_token
    if __import__("os").name == "posix":
        assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600
        assert token_path.stat().st_mode & 0o777 == 0o600


def test_participant_wizard_resumes_after_deployment_failure_without_recreating_resources(tmp_path):
    provider = FakeProvider(fail_deploy=True)
    state_path = tmp_path / "state.json"
    answers = iter(["1", "1"])
    first = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                             input_fn=lambda _prompt: next(answers), print_fn=lambda _line: None)
    assert first.run() == 1
    assert json.loads(state_path.read_text())["step"] == "vercel_configured"

    provider.fail_deploy = False
    second = ParticipantSetup(provider, source_dir=_source(tmp_path), state_path=state_path,
                              print_fn=lambda _line: None)
    assert second.run() == 0
    assert provider.create_supabase_calls == 0
    assert provider.create_vercel_calls == 0
    assert provider.deploy_calls == 2


def test_participant_wizard_can_create_free_plan_resources(tmp_path):
    provider = FakeProvider(empty=True)
    answers = iter(["1", "pool-node", "ap-south-1"])
    setup = ParticipantSetup(provider, source_dir=_source(tmp_path),
                             state_path=tmp_path / "state.json", input_fn=lambda _prompt: next(answers),
                             print_fn=lambda _line: None)
    assert setup.run() == 0
    assert provider.create_supabase_calls == 1
    assert provider.create_vercel_calls == 1


def test_telegram_session_goes_only_to_the_participant_vercel_project(tmp_path):
    provider = FakeProvider()
    state_path = tmp_path / "state.json"
    output = []
    from openseek.participant_setup import _atomic_state
    _atomic_state(state_path, {"version": 1, "step": "healthy", "deployment_url": "https://my-openseek-node.vercel.app",
                               "vercel_project": "my-openseek-node", "supabase_ref": "sb-ref-1"})
    setup = ParticipantSetup(
        provider, source_dir=_source(tmp_path), state_path=state_path,
        input_fn=lambda prompt: {"Telegram API ID: ": "1234",
                                 "Shared Telegram group invite link: ": "https://t.me/+invite",
                                 "Enable Nuvio guest-device access? [y/N] ": "y"}[prompt],
        secret_input_fn=lambda _prompt: "api_hash_secret",
        telegram_joiner=FakeTelegramJoiner(), print_fn=output.append)

    assert setup.run() == 0

    assert provider.env["OPENSEEK_TELEGRAM_SESSION"] == ("telegram_session_secret", True)
    assert provider.env["OPENSEEK_TELEGRAM_API_HASH"] == ("api_hash_secret", True)
    assert provider.env["OPENSEEK_ADMIN_TOKEN"][0] == (tmp_path / "participant-setup.admin-token").read_text().strip()
    assert provider.env["OPENSEEK_TELEGRAM_CHANNEL_ID"] == ("-100123456", False)
    assert provider.env["OPENSEEK_SETUP_PENDING"] == ("0", False)
    assert provider.health_pending == [False, False, False]
    assert "OPENSEEK_SETUP_KEY" not in provider.env
    assert provider.pairing_calls[0][0] == "https://my-openseek-node.vercel.app"
    assert provider.pairing_calls[0][2] is True  # the wizard's explicit consent
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["step"] == "paired"
    assert "pairing_setup_key" not in state
    assert "telegram_session_secret" not in json.dumps(state)
    assert "api_hash_secret" not in json.dumps(state)
    assert "telegram_session_secret" not in "\n".join(output)
    assert "api_hash_secret" not in "\n".join(output)
    assert "https://my-openseek-node.vercel.app|ABCD-EFGH-IJKL-MNOP" in "\n".join(output)
    assert f"https://my-openseek-node.vercel.app/#{provider.env['OPENSEEK_ADMIN_TOKEN'][0]}" in "\n".join(output)


def _telegram_ready(tmp_path: Path) -> None:
    from openseek.participant_setup import _atomic_state
    _atomic_state(tmp_path / "state.json", {
        "version": 1, "step": "telegram_ready",
        "deployment_url": "https://my-openseek-node.vercel.app",
        "vercel_project": "my-openseek-node", "supabase_ref": "sb-ref-1"})


def test_new_invite_never_touches_the_node_without_guest_consent(tmp_path):
    """`--new-pairing` must stop before any provider write when the participant
    declines guest-device access: no env, no deploy, no invite, and the saved
    checkpoint stays where it was."""
    provider = FakeProvider()
    _telegram_ready(tmp_path)
    state_path = tmp_path / "state.json"
    output = []
    asked = []

    def answer(prompt):
        asked.append(prompt)
        return {"Enable Nuvio guest-device access? [y/N] ": "n"}[prompt]

    setup = ParticipantSetup(
        provider, source_dir=_source(tmp_path), state_path=state_path,
        input_fn=answer, print_fn=output.append)

    assert setup.run() == 1
    assert asked == ["Enable Nuvio guest-device access? [y/N] "], asked
    assert any("no invite was created" in line.lower() for line in output), output
    assert provider.auth_calls == 0            # the consent question comes first
    assert provider.env == {}                  # no OPENSEEK_ADMIN_TOKEN / SETUP_KEY writes
    assert provider.deploy_calls == 0 and provider.pairing_calls == []
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["step"] == "telegram_ready" and "pairing_setup_key" not in state


def test_new_invite_carries_the_explicit_consent_into_the_request(tmp_path):
    """Consenting is asked again on every new invite and is passed through as
    the explicit `enable_sharing: True` request field, never implied."""
    provider = FakeProvider()
    _telegram_ready(tmp_path)
    setup = ParticipantSetup(
        provider, source_dir=_source(tmp_path), state_path=tmp_path / "state.json",
        input_fn=lambda prompt: {"Enable Nuvio guest-device access? [y/N] ": "y"}[prompt],
        print_fn=lambda _line: None)

    assert setup.run() == 0
    url, setup_key, enable_sharing = provider.pairing_calls[0]
    assert enable_sharing is True
    assert url == "https://my-openseek-node.vercel.app" and setup_key
    assert "OPENSEEK_SETUP_KEY" not in provider.env   # the temporary key is removed again
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["step"] == "paired" and "pairing_setup_key" not in state
