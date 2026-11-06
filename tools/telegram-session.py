#!/usr/bin/env python3
"""Create or check the OpenSeek QA Telegram StringSession.

Run it with the QA virtualenv:

    ~/.config/openseek/venv/bin/python tools/telegram-session.py

It reads OPENSEEK_TELEGRAM_API_ID and OPENSEEK_TELEGRAM_API_HASH from
~/.config/openseek/qa.env, asks for the phone number, the login code, and the
2FA password if the account has one. It then writes OPENSEEK_TELEGRAM_SESSION
back into that file with mode 0600.

The session string is never printed. Use --check to prove a saved session
still works without asking for anything.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

ENV = pathlib.Path.home() / ".config/openseek" / "qa.env"


def load_env(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def save_env(path: pathlib.Path, updates: dict[str, str]) -> None:
    lines = path.read_text().splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")
    os.chmod(path, 0o600)


async def login(api_id: int, api_hash: str) -> int:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()
    me = await client.get_me()
    session = client.session.save()
    save_env(ENV, {"OPENSEEK_TELEGRAM_SESSION": session})
    print(f"Logged in as @{me.username or me.id} (id {me.id}).")
    print(f"Session saved to {ENV}: length {len(session)}, mode 0600.")
    print("The string was not printed. Re-run with --check to verify it.")
    await client.disconnect()
    return 0


async def check(api_id: int, api_hash: str, session: str) -> int:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        print("Saved session is not authorized. Run the script without --check.", file=sys.stderr)
        await client.disconnect()
        return 1
    me = await client.get_me()
    dialogs = await client.get_dialogs(limit=5)
    print(f"Session works for @{me.username or me.id} (id {me.id}).")
    print("First dialogs: " + ", ".join(d.name for d in dialogs))
    await client.disconnect()
    return 0


def main() -> int:
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0
    env = load_env(ENV)
    api_id = env.get("OPENSEEK_TELEGRAM_API_ID", "")
    api_hash = env.get("OPENSEEK_TELEGRAM_API_HASH", "")
    if not api_id or not api_hash:
        print(f"Missing OPENSEEK_TELEGRAM_API_ID / OPENSEEK_TELEGRAM_API_HASH in {ENV}", file=sys.stderr)
        return 2

    if "--check" in sys.argv:
        session = env.get("OPENSEEK_TELEGRAM_SESSION", "")
        if not session:
            print(f"No OPENSEEK_TELEGRAM_SESSION in {ENV}. Run without --check first.", file=sys.stderr)
            return 2
        return asyncio.run(check(int(api_id), api_hash, session))

    return asyncio.run(login(int(api_id), api_hash))


if __name__ == "__main__":
    raise SystemExit(main())
