"""Resumable provisioning wizard state: one 0600 JSON file, atomically replaced."""
from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1
STATE_FILE = "provision-wizard.json"

# Fields that hold provider credentials while the wizard is in flight. They are
# saved between steps so a resumed run can call the providers without asking again,
# and `purge_secrets` blanks them at completion: the durable copy of a participant's
# secrets lives only in their provider accounts and the Vercel project env store,
# never in this file.
SECRETS = ("vercel_token", "supabase_token", "db_password", "service_key")


@dataclass
class StepRecord:
    """One wizard step's outcome. `detail` is plain language and secret-free."""

    name: str
    outcome: str                      # done | failed
    detail: str = ""
    created: float = field(default_factory=time.time)


@dataclass
class WizardState:
    """What the wizard knows so far. Non-secret facts plus transient credentials."""

    summary_status: str = "interrupted"   # ready | interrupted | blocked_quota | blocked_paused | failed_auth | failed_provider
    completed: dict[str, StepRecord] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    # Non-secret facts (safe to show the participant; names/slugs/urls, never creds).
    vercel_user: str = ""
    vercel_project: str = ""
    supabase_org: str = ""
    supabase_project_name: str = ""
    supabase_ref: str = ""
    supabase_url: str = ""
    staging_bucket: str = ""
    deployment_id: str = ""
    deployment_url: str = ""
    node_url: str = ""
    # Transient secrets (see the module docstring); purged at completion.
    vercel_token: str = ""
    supabase_token: str = ""
    db_password: str = ""
    service_key: str = ""

    def to_dict(self) -> dict:
        """Full serialization, including the transient secrets (save() protects the file)."""
        return {
            "version": SCHEMA_VERSION,
            "summary_status": self.summary_status,
            "completed": {name: {"name": record.name, "outcome": record.outcome,
                                 "detail": record.detail,
                                 "created": record.created}
                          for name, record in sorted(self.completed.items())},
            "created_at": self.created_at,
            "vercel_user": self.vercel_user,
            "vercel_project": self.vercel_project,
            "supabase_org": self.supabase_org,
            "supabase_project_name": self.supabase_project_name,
            "supabase_ref": self.supabase_ref,
            "supabase_url": self.supabase_url,
            "staging_bucket": self.staging_bucket,
            "deployment_id": self.deployment_id,
            "deployment_url": self.deployment_url,
            "node_url": self.node_url,
            "vercel_token": self.vercel_token,
            "supabase_token": self.supabase_token,
            "db_password": self.db_password,
            "service_key": self.service_key,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WizardState":
        """Rebuild from save()'s JSON. Unknown or malformed shapes raise ValueError."""
        if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
            raise ValueError("wizard state is from a different version")
        state = cls(summary_status=data.get("summary_status") or "interrupted",
                    created_at=float(data.get("created_at") or time.time()))
        for name, record in data.get("completed", {}).items():
            if isinstance(record, dict) and record.get("outcome") in ("done", "failed"):
                state.completed[str(name)] = StepRecord(
                    str(record.get("name") or name), str(record["outcome"]),
                    str(record.get("detail") or ""), float(record.get("created") or 0.0))
        for key in ("vercel_user", "vercel_project", "supabase_org",
                    "supabase_project_name", "supabase_ref", "supabase_url",
                    "staging_bucket", "deployment_id", "deployment_url", "node_url",
                    *SECRETS):
            value = data.get(key, "")
            setattr(state, key, value if isinstance(value, str) else "")
        return state


def state_path(data_dir: Path) -> Path:
    """Default state-file location under the install's data directory."""
    return Path(data_dir) / STATE_FILE


def load(path: Path) -> WizardState | None:
    """Load state for resuming, or None when there is nothing usable yet.

    A missing, corrupt, or future-version file means "start fresh": every resource the
    wizard can create is discovered (or re-checked with get-before-create) before being
    created again, so a fresh start never duplicates anything.
    """
    try:
        return WizardState.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def save(path: Path, state: WizardState) -> None:
    """Atomically persist state to a new 0600 file (temp + os.replace, no torn writes).

    The temp file is created with O_EXCL so a concurrent run cannot trample it, fsynced
    before the rename, and the final file is chmod 0600 because it holds secrets while
    the wizard is in flight (purged on success, see purge_secrets).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    body = json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def purge_secrets(state: WizardState) -> None:
    """Blank every credential field before the completion save.

    The wizard calls this in its finalize step, so a successful run leaves no provider
    token, database password or service key anywhere on disk; while the wizard is in
    flight the state file holds them transiently (0600, never logged).
    """
    for name in SECRETS:
        setattr(state, name, "")
