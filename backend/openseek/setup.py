"""Interactive pool setup that writes a separate, mode-0600 environment file."""
from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Callable


def _ask(prompt: str, default: str, input_fn: Callable[[str], str]) -> str:
    value = input_fn(f"{prompt} [{default}]: ").strip()
    return value or default


def _env_assignment(name: str, value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@+-]*", value):
        return f"{name}={value}"
    return f"{name}='" + value.replace("'", "'\"'\"'") + "'"


def _write_env(path: Path, lines: list[str]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(lines).rstrip() + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    if os.name == "posix":
        os.chmod(path, 0o600)


def run_setup(input_fn: Callable[[str], str] = input, print_fn: Callable[[str], None] = print,
              output_path: Path = Path("openseek.env"), state_path: Path | None = None,
              new_pairing: bool = False, provider: str | None = None) -> int:
    print_fn("OpenSeek pool setup")
    print_fn("1) Host a public pool")
    print_fn("2) Join a community pool")
    print_fn("3) Skip (local-only)")
    print_fn("4) Join the shared pool on my own Vercel account")
    choice = _ask("Choose", "3", input_fn)
    if choice not in ("1", "2", "3", "4"):
        print_fn("Choose 1, 2, 3, or 4.")
        return 2

    if choice == "4":
        from .participant_setup import CliSetupProvider, ParticipantSetup

        source_dir = Path.cwd()
        if not (source_dir / "vercel.json").exists() and (source_dir / "backend" / "vercel.json").exists():
            source_dir = source_dir / "backend"
        resume_file = (state_path or Path.home() / ".config" / "openseek" / "participant-setup.json")
        # Choose the provider backend: the official CLIs (browser sign-in) or the
        # provider APIs with an account token. Tokens in the environment select the
        # REST backend without an extra flag; --provider always wins.
        selected = (provider or os.environ.get("OPENSEEK_SETUP_PROVIDER", "").strip().lower() or
                    ("rest" if os.environ.get("OPENSEEK_VERCEL_TOKEN") and
                     os.environ.get("OPENSEEK_SUPABASE_ACCESS_TOKEN") else "cli"))
        if selected == "rest":
            from .provision.provider import RestSetupProvider
            setup_provider = RestSetupProvider(input_fn=input_fn)
        elif selected == "cli":
            setup_provider = CliSetupProvider()
        else:
            print_fn("Unknown setup provider; choose cli or rest.")
            return 2
        return ParticipantSetup(setup_provider, source_dir=source_dir,
                                state_path=resume_file, input_fn=input_fn,
                                print_fn=print_fn, new_pairing=new_pairing).run()

    lines: list[str]
    if choice == "3":
        lines = ["# OpenSeek local-only pool configuration", "OPENSEEK_POOL_MODE=",
                 "OPENSEEK_POOL_ENABLED=0"]
    elif choice == "1":
        print_fn("Create a private Telegram channel and add the pool bot as an administrator with post permission.")
        token = _ask("Telegram bot token", "", input_fn)
        channel = _ask("Telegram channel ID", "", input_fn)
        if not token or not channel:
            print_fn("A bot token and channel ID are required.")
            return 2
        lines = ["# OpenSeek public pool host", "OPENSEEK_POOL_MODE=host", "OPENSEEK_POOL_ENABLED=1",
                 _env_assignment("OPENSEEK_TELEGRAM_BOT_TOKEN", token),
                 _env_assignment("OPENSEEK_TELEGRAM_CHANNEL_ID", channel),
                 _env_assignment("OPENSEEK_POOL_READ_KEY", secrets.token_urlsafe(32)),
                 _env_assignment("OPENSEEK_POOL_WRITE_KEY", secrets.token_urlsafe(32))]
        print_fn("The read and write keys are stored in the protected env file. Share the read key with readers and the write key only with trusted contributors.")
    else:
        print_fn("The pool group must be private. Member posting requires Send Messages and Send Media permissions.")
        channel = _ask("Telegram group/channel ID (leave blank when using an invite link)", "", input_fn)
        api_id = _ask("Telegram API ID from my.telegram.org", "", input_fn)
        api_hash = _ask("Telegram API hash", "", input_fn)
        if not api_id or not api_hash:
            print_fn("A Telegram API ID and API hash are required.")
            return 2
        print_fn("1) Join by a private-group invite link (user session)")
        print_fn("2) Use a librarian bot or HTTPS relay")
        path = _ask("Contribution path", "1", input_fn)
        if path not in ("1", "2"):
            print_fn("Choose 1 or 2.")
            return 2
        identity = "user"
        token = ""
        session_file = str(Path(os.environ.get("OPENSEEK_DATA_DIR", "data")) / "telegram" / "openseek.session")
        invite = ""
        librarian = ""
        librarian_key = ""
        if path == "1":
            print_fn("1) User session joins by invite link")
            print_fn("2) Bot token already added by an administrator")
            identity = _ask("Telegram identity", "1", input_fn)
            if identity not in ("1", "2"):
                print_fn("Choose 1 or 2.")
                return 2
            if identity == "1":
                identity = "user"
                invite = _ask("Private group invite link", "", input_fn)
                if not invite:
                    print_fn("An invite link is required for this path.")
                    return 2
                session_file = _ask("User session file", session_file, input_fn)
            else:
                identity = "bot"
                token = _ask("Telegram bot token", "", input_fn)
                if not token or not channel:
                    print_fn("A bot token and channel ID are required. An administrator must add the bot to the group.")
                    return 2
        else:
            print_fn("The librarian path uses a user session; Telegram does not allow a bot to DM another bot. The session still needs read access to the shared channel.")
            session_file = _ask("User session file", session_file, input_fn)
            librarian = _ask("Librarian @username or HTTPS URL", "", input_fn)
            if not librarian:
                print_fn("A librarian username or HTTPS URL is required.")
                return 2
            if librarian.startswith("https://"):
                librarian_key = _ask("Librarian bearer key (optional)", "", input_fn)
        contribute = _ask("Enable contribution? (y/n)", "y", input_fn).lower() in ("y", "yes", "1")
        if (not channel and (identity != "user" or path != "1")):
            print_fn("A channel ID is required unless a user session joins by invite link.")
            return 2
        lines = ["# OpenSeek channel pool", "OPENSEEK_POOL_MODE=channel",
                 _env_assignment("OPENSEEK_TELEGRAM_API_ID", api_id),
                 _env_assignment("OPENSEEK_TELEGRAM_API_HASH", api_hash),
                 _env_assignment("OPENSEEK_TELEGRAM_CHANNEL_ID", channel),
                 f"OPENSEEK_CONTRIBUTION={1 if contribute else 0}"]
        if identity == "user":
            lines.append(_env_assignment("OPENSEEK_TELEGRAM_SESSION_FILE", session_file))
        if token:
            lines.append(_env_assignment("OPENSEEK_TELEGRAM_BOT_TOKEN", token))
        if librarian:
            lines.append(_env_assignment("OPENSEEK_LIBRARIAN", librarian))
        if librarian_key:
            if len(librarian_key) < 24:
                print_fn("The librarian bearer key must contain at least 24 characters.")
                return 2
            lines.append(_env_assignment("OPENSEEK_LIBRARIAN_KEY", librarian_key))
        from .config import Settings
        from .telegram_mt import TelegramMTProtoStore

        setup_env = {
            "OPENSEEK_DATA_DIR": os.environ.get("OPENSEEK_DATA_DIR", "data"),
            "OPENSEEK_POOL_MODE": "channel",
            "OPENSEEK_TELEGRAM_API_ID": api_id,
            "OPENSEEK_TELEGRAM_API_HASH": api_hash,
            "OPENSEEK_TELEGRAM_CHANNEL_ID": channel or "0",
            "OPENSEEK_CONTRIBUTION": "0",
            "OPENSEEK_LIBRARIAN": librarian,
        }
        if identity == "user":
            setup_env["OPENSEEK_TELEGRAM_SESSION_FILE"] = session_file
            setup_env.pop("OPENSEEK_TELEGRAM_BOT_TOKEN", None)
        else:
            setup_env["OPENSEEK_TELEGRAM_BOT_TOKEN"] = token
        if librarian_key:
            setup_env["OPENSEEK_LIBRARIAN_KEY"] = librarian_key
        settings = Settings.from_env(setup_env)
        store = TelegramMTProtoStore(settings)
        try:
            if identity == "user":
                store.login(input_fn=input_fn)
                if invite:
                    channel = store.join_invite(invite)
                    lines = [line for line in lines if not line.startswith("OPENSEEK_TELEGRAM_CHANNEL_ID=")]
                    lines.append(f"OPENSEEK_TELEGRAM_CHANNEL_ID={channel}")
                print_fn("Telegram user session authorized and saved with restricted file permissions.")
            settings = settings.with_(telegram_channel_id=channel or settings.telegram_channel_id,
                                     contribution=contribute)
            store.settings = settings
            store.channel_id = settings.telegram_channel_id
            store.start()
        except Exception as exc:
            print_fn(f"Telegram setup failed ({getattr(exc, 'code', type(exc).__name__)}). No env file was written.")
            return 1
        finally:
            store.close()

    try:
        _write_env(output_path, lines)
    except FileExistsError:
        print_fn(f"{output_path} already exists; it was left unchanged.")
        return 1
    except OSError as exc:
        print_fn(f"Could not write {output_path} ({type(exc).__name__}).")
        return 1
    print_fn(f"Created {output_path} with mode 0600.")
    print_fn(f"Load it with: set -a; source ./{output_path}; set +a")
    if choice == "1":
        print_fn("Run `openseek pool status` after starting the server to check the host pool.")
    return 0
