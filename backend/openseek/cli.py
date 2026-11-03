"""Command line: `openseek serve | pair | devices | revoke <id> | dashboard | openapi <file>`.

`pair`, `devices`, `revoke` and `dashboard` talk to the running daemon's loopback admin endpoint
(http://127.0.0.1:<OPENSEEK_ADMIN_PORT>); they never touch the network.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .config import ConfigError, Settings


def _admin(settings: Settings, method: str, path: str) -> tuple[int, dict | None]:
    from .auth import admin_token
    req = urllib.request.Request(f"http://127.0.0.1:{settings.admin_port}{path}", method=method,
                                 headers={"X-OpenSeek-Admin": admin_token(settings.data_dir, settings.admin_token)})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never use a proxy
    try:
        with opener.open(req, timeout=30) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        return e.code, None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="openseek")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the daemon (HTTPS API + loopback admin)")
    sub.add_parser("doctor", help="show pool configuration and live channel permissions")
    sub.add_parser("setup", help="write a protected pool configuration file")
    sub.add_parser("pair", help="open a 10-minute pairing window and print the code")
    sub.add_parser("devices", help="list paired devices")
    sub.add_parser("dashboard", help="open the local admin dashboard (the secret travels in the URL fragment, never over the network)")
    rv = sub.add_parser("revoke", help="revoke a paired device")
    rv.add_argument("device_id")
    oa = sub.add_parser("openapi", help="write the API's OpenAPI document")
    oa.add_argument("path")
    pool = sub.add_parser("pool", help="inspect or rebuild the Telegram channel pool")
    pool_sub = pool.add_subparsers(dest="pool_action", required=True)
    pool_sub.add_parser("status", help="show the configured pool state")
    pool_sub.add_parser("scan", help="scan new Telegram messages once")
    pool_sub.add_parser("rebuild", help="clear and rebuild the channel index")
    pool_sub.add_parser("login", help="log in to a Telegram user session")
    a = ap.parse_args(argv)
    if a.cmd == "setup":
        from .setup import run_setup
        return run_setup()
    try:
        env = dict(os.environ)
        if a.cmd == "pool" and a.pool_action == "login":
            if env.get("OPENSEEK_POOL_MODE", "").strip().lower() != "channel":
                print("set OPENSEEK_POOL_MODE=channel and Telegram API credentials before pool login", file=sys.stderr)
                return 2
            if not (env.get("OPENSEEK_TELEGRAM_BOT_TOKEN") or env.get("OPENSEEK_TELEGRAM_SESSION") or
                    env.get("OPENSEEK_TELEGRAM_SESSION_FILE")):
                data_dir = env.get("OPENSEEK_DATA_DIR", "data")
                env["OPENSEEK_TELEGRAM_SESSION_FILE"] = str(Path(data_dir) / "telegram" / "openseek.session")
        settings = Settings.from_env(env)
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2

    if a.cmd == "serve":
        from .app import serve
        serve(settings)
        return 0
    if a.cmd == "doctor":
        report = {"mode": settings.effective_pool_mode}
        if settings.effective_pool_mode == "channel":
            report["telegram_identity"] = "bot" if settings.telegram_bot_token else "user_session"
            report["channel_id"] = settings.telegram_channel_id
            report["configured_writable"] = settings.pool_channel_writable
        try:
            status, body = _admin(settings, "GET", "/admin/pool/status")
        except (urllib.error.URLError, OSError):
            print(json.dumps({**report, "daemon": "unavailable"}, indent=2, sort_keys=True))
            return 1
        if status == 200 and body:
            report["daemon"] = "running"
            report["pool"] = body
            if isinstance(body, dict) and "contribution_path" in body:
                report["contribution_path"] = body["contribution_path"]
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        print(json.dumps({**report, "daemon": f"HTTP {status}"}, indent=2, sort_keys=True))
        return 1
    if a.cmd == "pool":
        if a.pool_action == "login":
            from .pool import PoolError
            from .telegram_mt import TelegramMTProtoStore

            store = TelegramMTProtoStore(settings)
            try:
                store.login()
                print("Telegram user session saved with restricted file permissions.")
                return 0
            except PoolError as exc:
                print(f"Telegram login failed ({exc.code})", file=sys.stderr)
                return 1
            finally:
                store.close()
        method, path = {
            "status": ("GET", "/admin/pool/status"),
            "scan": ("POST", "/admin/pool/scan"),
            "rebuild": ("POST", "/admin/pool/rebuild"),
        }[a.pool_action]
        try:
            status, body = _admin(settings, method, path)
        except (urllib.error.URLError, OSError):
            print("cannot reach the OpenSeek daemon on the loopback admin port", file=sys.stderr)
            return 1
        if status != 200 or body is None:
            print(f"failed (HTTP {status})", file=sys.stderr)
            return 1
        print(json.dumps(body, indent=2, sort_keys=True))
        return 0
    if a.cmd == "dashboard":
        import webbrowser

        from .auth import admin_token
        url = f"http://127.0.0.1:{settings.admin_port}/#{admin_token(settings.data_dir, settings.admin_token)}"
        print("Opening the dashboard in your browser. The link contains the admin secret: do not share it.")
        if not webbrowser.open(url):
            print(url)
        return 0
    if a.cmd == "openapi":
        import tempfile
        from pathlib import Path

        from .app import Core, api_openapi
        with tempfile.TemporaryDirectory() as d:
            core = Core(settings.with_(data_dir=Path(d)))
            try:
                doc = api_openapi(core)
            finally:
                core.stop()
        Path(a.path).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    try:
        if a.cmd == "pair":
            st, body = _admin(settings, "POST", "/admin/pair")
            if st != 200 or not body:
                print(f"failed (HTTP {st})", file=sys.stderr)
                return 1
            print(f"Pairing code: {body['code']}  (valid {body['expires_in_s'] // 60} min, single use)")
            print(f"Server pin:   {body['spki_pin']}")
            return 0
        if a.cmd == "devices":
            st, body = _admin(settings, "GET", "/admin/devices")
            if st != 200 or body is None:
                print(f"failed (HTTP {st})", file=sys.stderr)
                return 1
            for d in body["devices"]:
                print(f"{d['device_id']}  {d['name']}")
            return 0
        if a.cmd == "revoke":
            st, _ = _admin(settings, "DELETE", f"/admin/devices/{a.device_id}")
            print("revoked" if st == 204 else f"failed (HTTP {st})")
            return 0 if st == 204 else 1
    except (urllib.error.URLError, OSError):
        print("cannot reach the OpenSeek daemon on the loopback admin port", file=sys.stderr)
        return 1
    return 2
