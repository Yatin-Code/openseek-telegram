"""Local, resumable setup for a participant-owned Vercel/Supabase pool node.

The provider CLIs own their browser sign-in sessions. This module never asks for a
provider access token and never writes Supabase/Vercel credentials to the local
resume file. A short-lived setup key is kept in the owner-only resume file only
while the pairing invite is created, then removed from both places.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import getpass
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlsplit

import httpx


class SetupFailure(RuntimeError):
    """A user-safe setup failure; provider output and secrets are never included."""

    def __init__(self, step: str, reason: str = "provider_error"):
        self.step = step
        self.reason = reason
        super().__init__(f"{step}: {reason}")


class SetupProvider(Protocol):
    def authenticate(self) -> None: ...
    def supabase_projects(self) -> list[dict]: ...
    def supabase_organizations(self) -> list[dict]: ...
    def create_supabase_project(self, name: str, organization: str, region: str) -> dict: ...
    def apply_schema(self, project_ref: str, state_dir: Path, schema_path: Path) -> None: ...
    def supabase_credentials(self, project: dict) -> tuple[str, str]: ...
    def configure_private_bucket(self, url: str, secret: str) -> None: ...
    def vercel_projects(self) -> list[dict]: ...
    def create_vercel_project(self, source_dir: Path) -> dict: ...
    def link_vercel_project(self, source_dir: Path, project: dict) -> None: ...
    def set_vercel_secret(self, source_dir: Path, name: str, value: str, *, secret: bool) -> None: ...
    def remove_vercel_secret(self, source_dir: Path, name: str) -> None: ...
    def create_pairing_invite(self, url: str, setup_key: str) -> dict: ...
    def deploy(self, source_dir: Path) -> str: ...
    def check_health(self, url: str, *, pending: bool = True) -> bool: ...


def _project_ref(project: dict) -> str:
    return str(project.get("id") or project.get("ref") or project.get("project_ref") or "")


def _project_name(project: dict) -> str:
    return str(project.get("name") or project.get("slug") or _project_ref(project))


def _choose(prompt: str, items: list[dict], input_fn: Callable[[str], str],
            print_fn: Callable[[str], None], *, allow_create: bool = False) -> dict | None:
    if not items:
        return None
    print_fn(prompt)
    for index, item in enumerate(items, 1):
        print_fn(f"  {index}) {_project_name(item)}")
    if allow_create:
        print_fn(f"  {len(items) + 1}) Create a new project")
    raw = input_fn("Choose a number: ").strip()
    try:
        choice = int(raw)
    except ValueError:
        return None
    if 1 <= choice <= len(items):
        return items[choice - 1]
    return None


def _atomic_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=".pool-setup-", dir=path.parent)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        if os.name == "posix":
            os.chmod(path.parent, 0o700)
            os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _atomic_secret(path: Path, value: str) -> None:
    """Keep the generated dashboard credential separate from provider resume state."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=".pool-secret-", dir=path.parent)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        if os.name == "posix":
            os.chmod(path.parent, 0o700)
            os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _load_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("version") == 1:
            return value
    except (OSError, ValueError):
        pass
    return {"version": 1, "step": "start"}


def _safe_vercel_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or
            not (parsed.hostname.endswith(".vercel.app") or parsed.hostname == "vercel.app")):
        raise SetupFailure("health check", "deployment_url_unavailable")
    return f"https://{parsed.netloc}"


class ParticipantSetup:
    """One observable wizard flow, with provider work isolated behind `SetupProvider`."""

    def __init__(self, provider: SetupProvider, *, source_dir: Path, state_path: Path,
                 input_fn: Callable[[str], str] = input,
                 print_fn: Callable[[str], None] = print,
                 secret_input_fn: Callable[[str], str] = getpass.getpass,
                 telegram_joiner=None, new_pairing: bool = False):
        self.provider = provider
        self.source_dir = source_dir.resolve()
        self.state_path = state_path.expanduser()
        self.input = input_fn
        self.print = print_fn
        self.secret_input = secret_input_fn
        self.telegram_joiner = telegram_joiner
        self.new_pairing = new_pairing
        self.state = _load_state(self.state_path)

    def run(self) -> int:
        if not (self.source_dir / "vercel.json").exists() or not (self.source_dir / "pyproject.toml").exists():
            self.print("Run this from the OpenSeek backend checkout so the wizard can deploy that source.")
            return 2
        if self.state.get("step") == "healthy":
            return self._continue_telegram()
        if self.state.get("step") in {"telegram_deployed", "telegram_ready"}:
            if self.state.get("step") == "telegram_ready":
                return self._create_pairing_invite()
            return self._finish_telegram_deploy()
        if self.state.get("step") in {"pairing_configured", "pairing_deployed"}:
            return self._create_pairing_invite()
        if self.state.get("step") == "paired":
            if self.new_pairing:
                return self._create_pairing_invite()
            self.print("This OpenSeek node is already paired. Run `openseek setup --new-pairing` to create a replacement invite.")
            token_path = self._owner_token_path()
            try:
                owner_token = token_path.read_text(encoding="utf-8").strip()
            except OSError:
                owner_token = ""
            if owner_token:
                deployment = str(self.state.get("deployment_url") or "")
                self.print(f"Dashboard access (owner secret): {deployment}/#{owner_token}")
            else:
                self.print(f"Dashboard owner secret file is missing: {token_path}. Run `openseek setup --new-pairing` to reset it.")
            return 0
        self.print("Join the shared OpenSeek pool")
        self.print("This creates resources in your own provider accounts and uses their current free-plan limits.")
        self.print("The wizard never selects or enables a paid upgrade. A free-plan limit pauses setup for you to resume later.")

        try:
            self.provider.authenticate()
            self.state["step"] = "authorized"
            self._save()

            supabase = self._supabase_project()
            self.state["supabase_ref"] = _project_ref(supabase)
            self.state["step"] = "supabase_selected"
            self._save()

            schema = Path(__file__).parent / "supabase" / "schema.sql"
            self.provider.apply_schema(_project_ref(supabase), self.state_path.parent / "supabase", schema)
            supabase_url, supabase_secret = self.provider.supabase_credentials(supabase)
            self.provider.configure_private_bucket(supabase_url, supabase_secret)
            self.state["supabase_url"] = supabase_url
            self.state["step"] = "supabase_ready"
            self._save()

            vercel = self._vercel_project()
            self.state["vercel_project"] = _project_name(vercel)
            self.state.pop("vercel_creating", None)
            self.state.pop("vercel_projects_before", None)
            self.state["step"] = "vercel_selected"
            self._save()
            self.provider.link_vercel_project(self.source_dir, vercel)
            admin_token = self._owner_token()
            for key, value, secret in (
                ("OPENSEEK_PROFILE", "vercel", False),
                ("OPENSEEK_POOL_MODE", "channel", False),
                ("OPENSEEK_SETUP_PENDING", "1", False),
                ("OPENSEEK_ADMIN_TOKEN", admin_token, True),
                ("OPENSEEK_SUPABASE_URL", supabase_url, False),
                ("OPENSEEK_SUPABASE_SERVICE_KEY", supabase_secret, True),
                ("OPENSEEK_SUPABASE_BUCKET", "openseek-staging", False),
            ):
                self.provider.set_vercel_secret(self.source_dir, key, value, secret=secret)
            self.state["step"] = "vercel_configured"
            self._save()

            deployment = self.provider.deploy(self.source_dir)
            deployment = _safe_vercel_url(deployment)
            self.state["deployment_url"] = deployment
            self.state["step"] = "deployed"
            self._save()
            if not self.provider.check_health(deployment, pending=True):
                raise SetupFailure("health check", "deployment_not_healthy_yet")
            self.state["step"] = "healthy"
            self._save()
        except SetupFailure as exc:
            if exc.reason in {"quota", "plan_limit", "payment_required"}:
                self.print(f"Free-plan limit reached during {exc.step}. Setup is saved; no paid plan was enabled. Resume after freeing quota or choosing another free project.")
            else:
                self.print(f"Setup paused during {exc.step} ({exc.reason}). Your completed steps are saved. Run `openseek setup` again to resume.")
            return 1
        except (OSError, ValueError):
            self.print("Setup could not save its progress. Check that your local setup folder is writable, then retry.")
            return 1

        self.print(f"Your OpenSeek node is healthy at {deployment}.")
        self.print("Run `openseek setup` again to connect your Telegram account and continue setup.")
        return 0

    def _continue_telegram(self) -> int:
        from .telegram_setup import TelegramAccountSetup, TelegramSetupError

        deployment = str(self.state.get("deployment_url") or "")
        if not deployment:
            return 1
        self.print(f"Your Vercel node is ready at {deployment}.")
        self.print("OpenSeek will publish as your Telegram user account. No bot token is needed.")
        self.print("Join the shared pool with the group invite. The group must allow direct joins, history, messages, and media.")
        try:
            api_id = int(self.input("Telegram API ID: ").strip())
        except ValueError:
            self.print("Enter the numeric Telegram API ID, then rerun setup.")
            return 1
        api_hash = self.secret_input("Telegram API hash (hidden): ").strip()
        invite = self.input("Shared Telegram group invite link: ").strip()
        joiner = self.telegram_joiner or TelegramAccountSetup(input_fn=self.input)
        try:
            joined = joiner.join(api_id, api_hash, invite)
        except TelegramSetupError as exc:
            messages = {
                "telegram_api_credentials_required": "Add a Telegram API ID and API hash, then retry.",
                "phone_number_required": "Enter your Telegram phone number, then retry.",
                "telegram_login_invalid": "Telegram did not accept that login code or password. Retry the login step.",
                "telegram_login_required": "Telegram authorization did not finish. Retry the login step.",
                "join_request_required": "This invite requires approval. Ask the pool owner for an invite configured for direct joining.",
                "group_access_denied": "This account cannot access the shared group. Rejoin with the current invite.",
                "group_id_required": "Enter the group's numeric Telegram ID so the wizard can verify its access.",
                "shared_pool_must_be_supergroup": "The shared pool must be a Telegram supergroup, not a broadcast channel.",
                "shared_pool_history_unavailable": "The group history is empty or hidden for new members. Ask the owner to make history visible and add a pool welcome message.",
                "shared_pool_send_media_denied": "The group does not allow this account to send messages and media. Ask the owner to enable both permissions.",
                "flood_wait": "Telegram asked this account to wait. Retry setup after the wait period.",
                "telethon_unavailable": "The OpenSeek Telegram client is missing. Reinstall the backend dependencies.",
            }
            self.print(messages.get(exc.reason, "Telegram setup failed. Check the invite and account access, then retry."))
            return 1
        except Exception:
            self.print("Telegram setup could not be completed. Your provider setup is saved; retry this step.")
            return 1

        # Never put the StringSession or API hash into the local resume file. These
        # values go straight from memory into this participant's Vercel environment.
        try:
            self.provider.authenticate()
            project = {"name": str(self.state.get("vercel_project") or "")}
            self.provider.link_vercel_project(self.source_dir, project)
            for key, value, secret in (
                ("OPENSEEK_ADMIN_TOKEN", self._owner_token(), True),
                ("OPENSEEK_TELEGRAM_API_ID", str(api_id), False),
                ("OPENSEEK_TELEGRAM_API_HASH", api_hash, True),
                ("OPENSEEK_TELEGRAM_CHANNEL_ID", joined.channel_id, False),
                ("OPENSEEK_TELEGRAM_SESSION", joined.session, True),
                # Paired Nuvio devices can publish only when the user enables
                # contribution on that device. The default client setting stays off.
                ("OPENSEEK_CONTRIBUTION", "1", False),
            ):
                self.provider.set_vercel_secret(self.source_dir, key, value, secret=secret)
            self.provider.set_vercel_secret(self.source_dir, "OPENSEEK_SETUP_PENDING", "0", secret=False)
        except SetupFailure as exc:
            self.print(f"Could not save Telegram setup in your Vercel project ({exc.reason}). Retry this step.")
            return 1
        except (OSError, ValueError):
            self.print("Could not save the local dashboard secret. Check that your setup folder is writable, then retry.")
            return 1
        self.state["telegram_channel_id"] = joined.channel_id
        self.state["step"] = "telegram_deployed"
        self._save()
        return self._finish_telegram_deploy()

    def _finish_telegram_deploy(self) -> int:
        try:
            self.provider.authenticate()
            self.provider.link_vercel_project(
                self.source_dir, {"name": str(self.state.get("vercel_project") or "")})
            deployment = self.provider.deploy(self.source_dir)
            deployment = _safe_vercel_url(deployment)
            self.state["deployment_url"] = deployment
            self.state["step"] = "telegram_deployed"
            self._save()
            if not self.provider.check_health(deployment, pending=False):
                raise SetupFailure("Telegram node health check", "deployment_not_healthy_yet")
        except SetupFailure as exc:
            self.print(f"Telegram setup is saved in Vercel but the node did not become ready ({exc.reason}). Rerun `openseek setup` to resume.")
            return 1
        self.state["step"] = "telegram_ready"
        self._save()
        return self._create_pairing_invite()

    def _create_pairing_invite(self) -> int:
        deployment = str(self.state.get("deployment_url") or "")
        project = str(self.state.get("vercel_project") or "")
        if not deployment or not project:
            self.print("The node deployment details are missing. Resume the provider setup first.")
            return 1
        try:
            self.provider.authenticate()
            self.provider.link_vercel_project(self.source_dir, {"name": project})
            self.provider.set_vercel_secret(self.source_dir, "OPENSEEK_ADMIN_TOKEN",
                                            self._owner_token(), secret=True)
            setup_key = str(self.state.get("pairing_setup_key") or secrets.token_urlsafe(32))
            self.state["pairing_setup_key"] = setup_key
            self.state["step"] = "pairing_configured"
            self._save()
            self.provider.set_vercel_secret(self.source_dir, "OPENSEEK_SETUP_KEY", setup_key, secret=True)
            deployment = _safe_vercel_url(self.provider.deploy(self.source_dir))
            self.state["deployment_url"] = deployment
            self.state["step"] = "pairing_deployed"
            self._save()
            if not self.provider.check_health(deployment, pending=False):
                raise SetupFailure("pairing setup deployment", "deployment_not_healthy_yet")
            invite = self.provider.create_pairing_invite(deployment, setup_key)
            code = str(invite.get("code") or "")
            if not code or len(code.replace("-", "")) != 16:
                raise SetupFailure("Nuvio pairing invite", "invalid_invite_response")
            # Remove the temporary management credential before presenting the
            # invite to the user. The invite itself is one-time and expires.
            self.provider.remove_vercel_secret(self.source_dir, "OPENSEEK_SETUP_KEY")
            deployment = _safe_vercel_url(self.provider.deploy(self.source_dir))
            self.state["deployment_url"] = deployment
            if not self.provider.check_health(deployment, pending=False):
                raise SetupFailure("pairing cleanup deployment", "deployment_not_healthy_yet")
            self.state.pop("pairing_setup_key", None)
            self.state["step"] = "paired"
            self._save()
        except SetupFailure as exc:
            self.print(f"Nuvio pairing paused during {exc.step} ({exc.reason}). Setup is saved; rerun `openseek setup` to resume.")
            return 1
        except (OSError, ValueError):
            self.print("Nuvio pairing setup could not be saved. Check local disk space and retry.")
            return 1

        bundle = f"{deployment}|{code}"
        self.print(f"Your OpenSeek node is ready at {deployment}.")
        self.print("On Nuvio, open Settings → OpenSeek → Pair and enter this one-time pairing bundle:")
        self.print(bundle)
        self.print(f"Dashboard access (owner secret): {deployment}/#{self._owner_token()}")
        self.print("Nuvio connects over HTTPS and stores its own revocable device credential. Contributions start off; enable them in Nuvio when you choose to share.")
        return 0

    def _owner_token_path(self) -> Path:
        return self.state_path.with_name("participant-setup.admin-token")

    def _owner_token(self) -> str:
        path = self._owner_token_path()
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if len(value) >= 32:
            if os.name == "posix":
                os.chmod(path, 0o600)
            return value
        value = secrets.token_urlsafe(32)
        _atomic_secret(path, value)
        return value

    def _supabase_project(self) -> dict:
        ref = str(self.state.get("supabase_ref") or "")
        projects = self.provider.supabase_projects()
        found = next((project for project in projects if _project_ref(project) == ref), None)
        if found:
            return found
        planned_name = str(self.state.get("supabase_create_name") or "")
        found = next((project for project in projects if planned_name and project.get("name") == planned_name), None)
        if found:
            return found
        selected = _choose("Choose your Supabase project", projects, self.input, self.print, allow_create=True)
        if selected is not None:
            return selected
        if projects and self.input("Create a new Supabase project? [y/N] ").strip().lower() not in ("y", "yes"):
            raise SetupFailure("Supabase project selection", "selection_required")
        organizations = self.provider.supabase_organizations()
        if not organizations:
            raise SetupFailure("Supabase organization", "no_accessible_organization")
        org = _choose("Choose the Supabase organization that owns this free project", organizations,
                      self.input, self.print)
        if org is None:
            raise SetupFailure("Supabase organization", "selection_required")
        name = self.input("New Supabase project name [openseek-pool-node]: ").strip() or "openseek-pool-node"
        region = self.input("Project region [ap-south-1]: ").strip() or "ap-south-1"
        if not re.fullmatch(r"[a-z0-9-]{2,64}", name) or not re.fullmatch(r"[a-z0-9-]{2,32}", region):
            raise SetupFailure("Supabase project creation", "invalid_project_name_or_region")
        self.state["supabase_create_name"] = name
        self._save()                         # a retry discovers the created resource by this name
        return self.provider.create_supabase_project(name, str(org.get("id") or org.get("slug") or ""), region)

    def _vercel_project(self) -> dict:
        wanted = str(self.state.get("vercel_project") or "")
        projects = self.provider.vercel_projects()
        found = next((project for project in projects if _project_name(project) == wanted), None)
        if found:
            return found
        if self.state.get("vercel_creating"):
            before = set(self.state.get("vercel_projects_before") or [])
            created = [project for project in projects if _project_name(project) not in before]
            if len(created) == 1:
                return created[0]
        selected = _choose("Choose your Vercel project", projects, self.input, self.print, allow_create=True)
        if selected is not None:
            return selected
        if projects and self.input("Create a new Vercel project? [y/N] ").strip().lower() not in ("y", "yes"):
            raise SetupFailure("Vercel project selection", "selection_required")
        self.state["vercel_creating"] = True
        self.state["vercel_projects_before"] = [_project_name(project) for project in projects]
        self._save()                         # an interrupted CLI create is found from the list diff
        return self.provider.create_vercel_project(self.source_dir)

    def _save(self) -> None:
        _atomic_state(self.state_path, self.state)


class CliSetupProvider:
    """Adapter for the official Vercel and Supabase CLIs plus Supabase Storage REST."""

    def __init__(self, *, runner=None, httpx_client_factory=httpx.Client,
                 vercel: str = "vercel", supabase: str = "supabase"):
        self.runner = runner or subprocess.run
        self.httpx_client_factory = httpx_client_factory
        self.vercel = vercel
        self.supabase = supabase

    def _run(self, args: list[str], *, cwd: Path | None = None, interactive: bool = False,
             input_text: str | None = None, step: str, allow_failure: bool = False):
        try:
            result = self.runner(args, cwd=str(cwd) if cwd else None, text=True,
                                 input=input_text,
                                 capture_output=not interactive, check=False)
        except FileNotFoundError:
            raise SetupFailure(step, "cli_not_installed") from None
        if result.returncode and not allow_failure:
            output = "\n".join(filter(None, (getattr(result, "stdout", ""), getattr(result, "stderr", "")))).lower()
            if any(term in output for term in ("quota", "free plan limit", "payment required", "spending limit")):
                raise SetupFailure(step, "quota") from None
            raise SetupFailure(step, "provider_error") from None
        return result

    def authenticate(self) -> None:
        if shutil.which(self.vercel) is None:
            raise SetupFailure("Vercel sign-in", "install_vercel_cli")
        if shutil.which(self.supabase) is None:
            raise SetupFailure("Supabase sign-in", "install_supabase_cli")
        if self._run([self.vercel, "whoami"], step="Vercel sign-in", allow_failure=True).returncode:
            self._run([self.vercel, "login"], interactive=True, step="Vercel sign-in")
        if self._run([self.supabase, "projects", "list", "--output-format", "json"],
                     step="Supabase sign-in", allow_failure=True).returncode:
            self._run([self.supabase, "login"], interactive=True, step="Supabase sign-in")
            self._run([self.supabase, "projects", "list", "--output-format", "json"], step="Supabase sign-in")

    @staticmethod
    def _json_list(stdout: str) -> list[dict]:
        try:
            body = json.loads(stdout)
        except ValueError:
            return []
        if isinstance(body, list):
            return [row for row in body if isinstance(row, dict)]
        for key in ("projects", "organizations", "orgs", "data"):
            if isinstance(body, dict) and isinstance(body.get(key), list):
                return [row for row in body[key] if isinstance(row, dict)]
        return []

    def supabase_projects(self) -> list[dict]:
        out = self._run([self.supabase, "projects", "list", "--output-format", "json"],
                        step="Supabase project list").stdout
        return self._json_list(out)

    def supabase_organizations(self) -> list[dict]:
        out = self._run([self.supabase, "orgs", "list", "--output-format", "json"],
                        step="Supabase organization list").stdout
        return self._json_list(out)

    def create_supabase_project(self, name: str, organization: str, region: str) -> dict:
        result = self._run([self.supabase, "projects", "create", name, "--org-id", organization,
                            "--region", region, "--yes"],
                           step="Supabase project creation")
        projects = self.supabase_projects()
        found = next((p for p in projects if p.get("name") == name), None)
        if found is None:
            raise SetupFailure("Supabase project creation", "project_id_unavailable")
        return found

    def apply_schema(self, project_ref: str, state_dir: Path, schema_path: Path) -> None:
        if not project_ref or not schema_path.is_file():
            raise SetupFailure("Supabase schema", "schema_unavailable")
        root = state_dir / "cli-project"
        migrations = root / "supabase" / "migrations"
        migrations.mkdir(parents=True, exist_ok=True, mode=0o700)
        config = root / "supabase" / "config.toml"
        if not config.exists():
            config.write_text('project_id = "openseek-pool-node"\n', encoding="utf-8")
        package_migrations = Path(__file__).parent / "supabase" / "migrations"
        migration_sources = sorted(package_migrations.glob("*.sql"))
        if migration_sources:
            for migration_source in migration_sources:
                target = migrations / migration_source.name
                if not target.exists():
                    shutil.copyfile(migration_source, target)
        else:
            migration = migrations / "20261105000000_openseek_pool.sql"
            if not migration.exists():
                shutil.copyfile(schema_path, migration)
        if os.name == "posix":
            for migration in migrations.glob("*.sql"):
                os.chmod(migration, 0o600)
        self._run([self.supabase, "link", "--project-ref", project_ref],
                  cwd=root, interactive=True, step="Supabase project link")
        self._run([self.supabase, "db", "push", "--linked", "--yes"], cwd=root,
                  interactive=True, step="Supabase schema apply")

    def supabase_credentials(self, project: dict) -> tuple[str, str]:
        ref = _project_ref(project)
        if not ref:
            raise SetupFailure("Supabase credentials", "project_ref_unavailable")
        result = self._run([self.supabase, "projects", "api-keys", "--project-ref", ref,
                            "--output-format", "json"], step="Supabase secret retrieval")
        try:
            rows = self._json_list(result.stdout)
            secret_row = next((row for row in rows if row.get("type") == "secret"), None)
            if secret_row is None:
                secret_row = next((row for row in rows if row.get("name") == "service_role"), None)
            secret = str((secret_row or {}).get("api_key") or (secret_row or {}).get("apiKey") or
                         (secret_row or {}).get("key") or "")
        except Exception:
            secret = ""
        if not secret:
            raise SetupFailure("Supabase credentials", "secret_key_unavailable")
        url = str(project.get("api_url") or project.get("url") or f"https://{ref}.supabase.co")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith("supabase.co"):
            raise SetupFailure("Supabase credentials", "project_url_invalid")
        return url.rstrip("/"), secret

    def configure_private_bucket(self, url: str, secret: str) -> None:
        payload = {"id": "openseek-staging", "name": "openseek-staging", "public": False,
                   "file_size_limit": 16 * 1024 * 1024}
        headers = {"apikey": secret, "authorization": f"Bearer {secret}", "content-type": "application/json"}
        try:
            with self.httpx_client_factory(timeout=20, follow_redirects=False, trust_env=False) as client:
                response = client.post(f"{url.rstrip('/')}/storage/v1/bucket", json=payload, headers=headers)
                if response.status_code == 409:
                    response = client.put(f"{url.rstrip('/')}/storage/v1/bucket/openseek-staging",
                                          json=payload, headers=headers)
        except Exception:
            raise SetupFailure("Supabase staging bucket", "provider_unavailable") from None
        if response.status_code >= 400:
            if response.status_code in (402, 429):
                raise SetupFailure("Supabase staging bucket", "quota")
            raise SetupFailure("Supabase staging bucket", "bucket_configuration_failed")

    def vercel_projects(self) -> list[dict]:
        out = self._run([self.vercel, "project", "ls", "--json"], step="Vercel project list").stdout
        return self._json_list(out)

    def create_vercel_project(self, source_dir: Path) -> dict:
        before = {_project_name(project) for project in self.vercel_projects()}
        self._run([self.vercel, "project", "add"], cwd=source_dir, interactive=True,
                  step="Vercel project creation")
        projects = self.vercel_projects()
        created = [project for project in projects if _project_name(project) not in before]
        project = created[0] if len(created) == 1 else None
        if project is None:
            project = next((row for row in projects if _project_name(row) == source_dir.name), None)
        if project is None:
            raise SetupFailure("Vercel project creation", "project_id_unavailable")
        return project

    def link_vercel_project(self, source_dir: Path, project: dict) -> None:
        name = _project_name(project)
        self._run([self.vercel, "link", "--yes", "--project", name], cwd=source_dir,
                  interactive=True, step="Vercel project link")

    def set_vercel_secret(self, source_dir: Path, name: str, value: str, *, secret: bool) -> None:
        # Replace in place so a resumed setup never creates duplicate variables. Values
        # travel over stdin and both CLI streams are captured and discarded.
        self._run([self.vercel, "env", "rm", name, "production", "--yes"], cwd=source_dir,
                  step=f"Vercel {name} configuration", allow_failure=True)
        args = [self.vercel, "env", "add", name, "production", "--yes"]
        if secret:
            args += ["--type", "secret"]
        self._run(args, cwd=source_dir, input_text=value + "\n",
                  step=f"Vercel {name} configuration")

    def remove_vercel_secret(self, source_dir: Path, name: str) -> None:
        self._run([self.vercel, "env", "rm", name, "production", "--yes"], cwd=source_dir,
                  step=f"Vercel {name} removal", allow_failure=True)

    def create_pairing_invite(self, url: str, setup_key: str) -> dict:
        try:
            with self.httpx_client_factory(timeout=20, follow_redirects=False, trust_env=False) as client:
                response = client.post(
                    f"{url.rstrip('/')}/v1/setup/pairing",
                    headers={"authorization": f"Bearer {setup_key}"},
                    json={"label": "Nuvio TV"})
                if response.status_code >= 400:
                    raise SetupFailure("Nuvio pairing invite", "pairing_endpoint_unavailable")
                body = response.json()
        except SetupFailure:
            raise
        except Exception:
            raise SetupFailure("Nuvio pairing invite", "provider_unavailable") from None
        if not isinstance(body, dict):
            raise SetupFailure("Nuvio pairing invite", "invalid_invite_response")
        return body

    def deploy(self, source_dir: Path) -> str:
        result = self._run([self.vercel, "deploy", "--prod", "--yes"], cwd=source_dir,
                           step="Vercel deployment")
        output = "\n".join(filter(None, (getattr(result, "stdout", ""), getattr(result, "stderr", ""))))
        matches = re.findall(r"https://[A-Za-z0-9._-]+\.vercel\.app", output)
        if not matches:
            raise SetupFailure("Vercel deployment", "deployment_url_unavailable")
        return matches[-1]

    def check_health(self, url: str, *, pending: bool = True) -> bool:
        try:
            response = httpx.get(f"{url.rstrip('/')}/v1/health", timeout=15,
                                 follow_redirects=False, trust_env=False)
            data = response.json()
        except Exception:
            return False
        return (response.status_code == 200 and isinstance(data, dict) and
                data.get("service") == "openseek" and data.get("setup_pending", False) is pending)
