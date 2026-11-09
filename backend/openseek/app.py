"""HTTP layer: the LAN HTTPS API (contract v1) and the loopback admin app.

`Core` wires the components; `create_api_app` / `create_admin_app` build the ASGI apps;
`serve()` runs both under uvicorn (API with the self-signed cert, admin on 127.0.0.1 only).
"""
from __future__ import annotations

import asyncio
import ipaddress
import hmac
import json
import logging
import os
import re
import socket
import time
from dataclasses import replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import (BaseModel, ConfigDict, Field, StrictBool, StrictInt,
                      StrictStr, ValidationError, field_validator)
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import compile_path

from . import API_VERSION, __version__
from .pairing import InviteManager
from .auth import DeviceAuth, admin_token, token_shaped
from .cache import Cache, PROFILE, secure_mkdir
from .config import Settings
from .db import DB
from .envfile import write_env_file
from .fetch import RangeProxy
from .ids import is_device_id, is_fingerprint, is_invite_id, parse_index
from .jobs import JobManager, SourceForbidden
from .logutil import plain, setup_logging
from .netpolicy import DestinationPolicy, filter_forwarded_headers
from .pairing import PairingClosed, PairingFailed, PairingManager, RateLimited
from .public import PublicLimiter, classify, host_kind
from .pool import (MAX_NVMT_BYTES, ChannelPoolClient,
                   ChannelPoolSync, PoolClient, PoolError, PoolService, PoolSync,
                   TelegramBotStore, decode_nvmt)
from .generation import GenerationError, GenerationService, GenerationStateStore
from .channel_index import ChannelIndex
from .channel_pool import ChannelPoolService, PoolWritePolicy
from .remote import (RemoteChannelStore, RemotePairingStore, RemoteTelegramSessionLease,
                     RemoteStoreError, RemoteUploadQueueStore, SupabaseClient)
from .remote_admin import RemoteAdminStore
from .store import ChannelStore, UploadQueueStore
from .uploads import UploadError, UploadService
from .telegram_mt import TelegramMTProtoStore
from .contribution import TelegramLibrarian
from .tls import ensure_certificate

log = logging.getLogger("openseek.app")

MAX_BODY = 16 * 1024
MAX_MS = 12 * 3600 * 1000
MAX_VERCEL_RESPONSE = 4_500_000
PUBLIC_PATHS = {"/v1/health", "/v1/pair/start", "/v1/pair/claim",
                "/v1/setup/pairing"}  # setup authenticates its own Bearer secret
# Headers a reverse proxy, tunnel or load balancer adds. The owner and admin APIs are direct-connection only: a request that carries any of them
# was relayed by something else, so its TCP peer address says nothing about who sent it. Refused (remote access is what the guest listener is for).
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port", "forwarded", "x-real-ip",
                 "cf-connecting-ip", "true-client-ip", "x-client-ip", "via")
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\])(?::\d{1,5})?$")
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
]


# ===================================================================== core
class Core:
    def __init__(self, settings: Settings, policy: DestinationPolicy | None = None, engine_factory=None,
                 pool_storage=None, proxy=None):
        self.settings = settings
        if os.name == "posix":
            os.umask(0o077)                       # files 0600, dirs 0700 (SR-16)
        self.data_dir = Path(settings.data_dir)
        secure_mkdir(self.data_dir)
        self.db = DB(self.data_dir / "openseek.db")
        self.cache = Cache(self.data_dir, self.db, settings.cache_max_bytes, settings.retention_days)
        self.pool = None
        self.channel_store = None
        self.channel_index = None
        self.librarian = None
        self.supabase = None
        self.state_store = self.db
        self.uploads = None
        self.telegram_session_lease = None
        self.pairing_store = self.db
        mode = settings.effective_pool_mode
        self.pool_write_policy = PoolWritePolicy(mode)
        if settings.pool_host_enabled:
            storage = pool_storage or TelegramBotStore(settings.telegram_bot_token, settings.telegram_channel_id,
                                                       settings.upstream_timeout_s)
            self.pool = PoolService(self.db, storage, settings.pool_read_key, settings.pool_write_key)
        if mode == "channel":
            self._configure_channel_pool(settings, pool_storage)
        else:
            self.pool_client = (PoolClient(settings.pool_url, settings.pool_read_key, settings.pool_write_key,
                                           settings.pool_timeout_s)
                                if mode == "host" and settings.pool_url and settings.pool_read_key else None)
            self.pool_sync = (PoolSync(self.db, self.cache, self.pool_client)
                              if self.pool_client and settings.pool_share else None)
        self.cert_path, self.key_path, generated_pin = ensure_certificate(
            self.data_dir, settings.tls_cert_file, settings.tls_key_file)
        self.spki_pin = settings.public_tls_pin if settings.profile == "vercel" else generated_pin
        self.admin_token = admin_token(self.data_dir, settings.admin_token)
        self.auth = DeviceAuth(self.db, store=self.pairing_store if settings.profile == "vercel" else None)
        self.invites = InviteManager(self.pairing_store, self.spki_pin, self.auth.register)
        self.pairing = PairingManager(self.spki_pin, self.auth.register, invites=self.invites)
        self.pairing.sharing_enabled = self.sharing_enabled
        self.public_limiter = PublicLimiter()
        self.proxy = proxy or RangeProxy()
        self.policy = policy or DestinationPolicy(settings.allowed_ports, settings.allow_http_sources)
        self.jobs = JobManager(settings, self.policy, self.cache, self.proxy, engine_factory)
        self.jobs.pool_client = self.pool_client
        self.jobs.pool_sync = self.pool_sync
        self.generations = GenerationService(
            self, GenerationStateStore(self.db, self.supabase if settings.profile == "vercel" else None))
        self.started = time.time()
        self.closing = False
        self.ffmpeg = _engine_available()

    def _configure_channel_pool(self, settings: Settings, channel_store=None) -> None:
        """Wire the channel pool for startup or for the local browser setup wizard."""
        self.settings = settings
        self.channel_store = channel_store or TelegramMTProtoStore(settings)
        self.pool_write_policy = PoolWritePolicy(
            "channel", store=self.channel_store, proxy_url=settings.pool_contrib_url,
            librarian_url=_librarian_url(settings) or "")
        if settings.profile == "vercel" or (settings.supabase_url and settings.supabase_service_key):
            self.supabase = SupabaseClient(settings.supabase_url, settings.supabase_service_key,
                                           bucket=settings.supabase_bucket)
        if settings.profile == "vercel":
            index_store = RemoteChannelStore(self.supabase)
            index_db = None
            self.state_store = RemoteAdminStore(self.supabase)
            self.telegram_session_lease = RemoteTelegramSessionLease(self.supabase)
            self.pairing_store = RemotePairingStore(self.supabase)
        else:
            index_store = ChannelStore(self.db)
            index_db = self.db
            self.state_store = self.db
        self.channel_index = ChannelIndex(
            index_store, self.channel_store, db=index_db,
            channel_id=settings.telegram_channel_id,
            scan_limit=settings.pool_channel_scan_limit, refresh_s=settings.pool_index_refresh_s)
        self.pool = ChannelPoolService(self.channel_index, settings.pool_read_key, settings.pool_write_key)
        if settings.profile == "vercel" or self.supabase is not None:
            upload_store = (RemoteUploadQueueStore(self.supabase) if self.supabase is not None
                            else UploadQueueStore(self.db))
            self.uploads = UploadService(settings, self.supabase, self.pool, upload_store)
        else:
            self.uploads = None
        if settings.librarian_enabled and hasattr(self.channel_store, "set_message_handler"):
            self.librarian = TelegramLibrarian(self.channel_store, self.channel_index)
            self.librarian.attach()
        self.pool_client = ChannelPoolClient(self.cache, self.channel_index, self.channel_store)
        self.pool_sync = (ChannelPoolSync(self.db, self.cache, self.channel_store, self.channel_index)
                          if settings.contribution else None)
        if hasattr(self, "jobs"):
            self.jobs.settings = settings
            self.jobs.pool_client = self.pool_client
            self.jobs.pool_sync = self.pool_sync

    def can_contribute(self, device_id: str | None) -> bool:
        return bool(device_id and self.pool_contributions_enabled()
                    and self.auth.role_of(device_id) == "guest"
                    and self.auth.can_contribute(device_id))

    def pool_contributions_enabled(self) -> bool:
        """Owner-controlled guest NVMT writes; the setting key also preserves existing installs."""
        return (self.sharing_enabled() and
                self.state_store.get_setting("contrib_enabled") == "1")

    def sharing_enabled(self) -> bool:
        """The owner's kill switch for everything a guest can do. Off unless the owner turned it on."""
        return self.state_store.get_setting("sharing_enabled") == "1"

    def start(self) -> "Core":
        self.proxy.start()
        if self.channel_store:
            self.channel_store.start()
            if getattr(self.channel_store, "history_readable", True):
                state = self.channel_index.status()
                if not state["scanned_at"]:
                    self.channel_index.rebuild()
                else:
                    self.channel_index.refresh(force=True)
                self.channel_index.start()
            else:
                # A bot identity cannot read channel history. The librarian writes and
                # records what it posts; a channel scan can never succeed, so do not
                # start one and do not fail the boot.
                log.warning("channel history not readable (bot identity); "
                            "starting without a channel scan")
        if self.pool_sync:
            self.pool_sync.start()
        self.jobs.start()
        self.cache.evict(set())
        return self

    def stop(self) -> None:
        self.closing = True
        self.generations.close()
        self.jobs.shutdown()
        if self.pool_sync:
            self.pool_sync.stop()
        if self.channel_index:
            self.channel_index.stop()
        if self.channel_store:
            self.channel_store.close()
        if self.supabase is not None:
            self.supabase.close()
        self.proxy.stop()
        self.db.close()


def _engine_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("av") is not None and importlib.util.find_spec("PIL") is not None


# ===================================================================== helpers
def err(status: int, code: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers=headers)


def _librarian_url(settings: Settings) -> str | None:
    value = settings.librarian.strip()
    if value.startswith("@"):
        return f"https://t.me/{value[1:]}"
    return value or None


def client_ip_allowed(host: str | None, allow_public: bool) -> bool:
    if allow_public:
        return True
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


GUEST_ALLOWED = (
    ("GET", re.compile(r"/v1/health")),
    ("GET", re.compile(r"/v1/pair/status")),
    ("DELETE", re.compile(r"/v1/pair")),                                  # leave: revokes only itself
)
GUEST_WHEN_SHARING_OFF = (("GET", re.compile(r"/v1/health")), ("GET", re.compile(r"/v1/pair/status")),
                          ("DELETE", re.compile(r"/v1/pair")))


def guest_route_allowed(method: str, path: str, allowed=GUEST_ALLOWED) -> bool:
    return any(m == method and p.fullmatch(path) for m, p in allowed)


async def _send_json(send, status: int, obj: dict, extra: list | None = None) -> None:
    body = json.dumps(obj).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
               (b"cache-control", b"no-store")] + SECURITY_HEADERS + (extra or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _bearer_credential(authorization: str | None) -> tuple[str | None, bool]:
    """(credential, malformed). No header — or an empty one — is a missing credential,
    not a malformed one."""
    if not authorization or not authorization.strip():
        return None, False
    scheme, _, rest = authorization.partition(" ")
    credential = rest.strip()
    if scheme.lower() != "bearer" or not credential or any(ch.isspace() for ch in credential):
        return None, True
    return credential, False


def _setup_pairing_key_valid(profile: str, expected: str, authorization: str | None) -> bool:
    scheme, separator, supplied = (authorization or "").partition(" ")
    if profile != "vercel" or not expected or not separator or scheme.lower() != "bearer":
        return False
    try:
        return hmac.compare_digest(supplied.encode("ascii"), expected.encode("ascii"))
    except UnicodeEncodeError:
        return False


def _rate_headers(limit: int, remaining: int, reset: int) -> list[tuple[bytes, bytes]]:
    return [(b"x-ratelimit-limit", str(limit).encode()),
            (b"x-ratelimit-remaining", str(remaining).encode()),
            (b"x-ratelimit-reset", str(reset).encode())]


def _with_rate_headers(send, limit: int, remaining: int, reset: int):
    """Advertise the budget on every response that passed the limiter, including
    refusals sent directly by the guard (auth, body limit, routing)."""
    async def wrapped(message):
        if message["type"] == "http.response.start":
            headers = list(message.get("headers") or [])
            names = {k.lower() for k, _ in headers}
            for name, value in _rate_headers(limit, remaining, reset):
                if name not in names:
                    headers.append((name, value))
            message = dict(message, headers=headers)
        await send(message)
    return wrapped


class Guard:
    """Pure ASGI middleware: client-IP policy (SR-17), body limit (SR-11), bearer auth (SR-6),
    security headers (SR-19). X-Forwarded-For and friends are never consulted."""

    def __init__(self, app, core: Core, admin: bool = False, public: bool = False):
        self.app = app
        self.core = core
        self.admin = admin
        self.public = public
        # The API app's own route table: with a real credential (or none at all), an
        # undocumented path answers the documented 404 instead of leaking an auth
        # answer for a route that does not exist (H6 criterion 1).
        self._documented = []
        if not admin and not public:
            router = getattr(app, "router", None)
            routes = getattr(router, "routes", None) or getattr(app, "routes", [])
            for route in routes:
                path = getattr(route, "path", None)
                if not path or not getattr(route, "methods", None):
                    continue          # mounts and other non-endpoint routes do not define the API surface
                self._documented.append((frozenset(getattr(route, "methods", ())), compile_path(path)[0]))

    async def _deny(self, send, client, status: int, obj: dict, extra: list | None = None):
        if self.public and client[0]:
            self.core.public_limiter.strike(client[0])
        return await _send_json(send, status, obj, extra)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        client = scope.get("client") or (None, None)
        kind = None
        if self.public:
            # deny by default: exact (method, path) first, then per-client budgets; the peer address is the only identity
            lim, ip = self.core.public_limiter, client[0]
            if not ip:
                return await _send_json(send, 403, {"error": "forbidden"})
            if lim.blocked(ip):
                return await _send_json(send, 429, {"error": "rate_limited"}, [(b"retry-after", b"900")])
            kind = classify(scope["method"], scope.get("path", ""), guest_route_allowed)
            if kind is None:
                lim.strike(ip)
                return await _send_json(send, 404, {"error": "not_found"})
            allowed, limit, remaining, reset = lim.consume(ip, kind)
            if not allowed:
                return await _send_json(send, 429, {"error": "rate_limited"},
                                        _rate_headers(limit, 0, reset) +
                                        [(b"retry-after", str(reset).encode())])
            scope["openseek.ratelimit"] = (limit, remaining, reset)
            scope["openseek.public"] = True
            ph = self.core.settings.public_host
            if ph:
                scope["openseek.base"] = f"https://{ph}"
        elif self.admin:
            if self.core.settings.profile != "vercel":
                if not client_ip_allowed(client[0], False) or not ipaddress.ip_address(
                        (client[0] or "0.0.0.0").split("%")[0]).is_loopback:
                    return await _send_json(send, 403, {"error": "forbidden"})
        else:
            if self.core.settings.profile != "vercel" and not client_ip_allowed(
                    client[0], self.core.settings.allow_public_clients):
                return await _send_json(send, 403, {"error": "forbidden"})
            # Per-client budgets on the owner/LAN listener too (H6): the same classes
            # and budgets as the public listener, advertised on every response. The
            # Vercel profile is skipped: there the TCP peer is the platform proxy and
            # the platform layer fronts the deployment.
            if self.core.settings.profile != "vercel" and client[0]:
                kind = host_kind(scope["method"], scope.get("path", ""), guest_route_allowed)
                allowed, limit, remaining, reset = self.core.public_limiter.consume(client[0], kind)
                if not allowed:
                    return await _send_json(send, 429, {"error": "rate_limited"},
                                            _rate_headers(limit, 0, reset) +
                                            [(b"retry-after", str(reset).encode())])
                scope["openseek.ratelimit"] = (limit, remaining, reset)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        rl = scope.get("openseek.ratelimit")
        if rl:
            send = _with_rate_headers(send, *rl)
        # Vercel's platform proxy always adds forwarded headers; refusing them would make the
        # profile unusable. They are never consulted for auth, rate limits, or base URLs. The
        # loopback admin listener keeps the strict local-only rules (F2 owns its surface).
        forwarded_allowed = self.core.settings.profile == "vercel"
        if not self.public and not forwarded_allowed and any(h in headers for h in PROXY_HEADERS):
            return await _send_json(send, 403, {"error": "proxy_refused"})

        path = scope.get("path", "")
        if self._documented:
            route_methods = [methods for methods, rx in self._documented if rx.fullmatch(path)]
            if not route_methods:
                return await _send_json(send, 404, {"error": "not_found"})
            if not any(scope["method"] in methods for methods in route_methods):
                return await _send_json(send, 405, {"error": "method_not_allowed"})

        if path == "/v1/setup/pairing" and not _setup_pairing_key_valid(
                self.core.settings.profile, os.environ.get("OPENSEEK_SETUP_KEY", ""),
                headers.get("authorization")):
            return await _send_json(send, 404, {"error": "not_found"})

        # Authenticate from the headers BEFORE the body is read: a missing, malformed or
        # revoked credential is refused without touching the body at all (H6, SR-11).
        is_pool_route = path.startswith("/v1/pool/")
        device = None
        role = None
        if self.admin:
            bad = self._admin_check(scope, headers)
            if bad:
                return await _send_json(send, 403, {"error": bad})
        else:
            state = scope.setdefault("state", {})
            try:
                device = self.core.auth.authenticate(headers.get("authorization"))
            except Exception as exc:
                log.warning("device authentication unavailable (%s)", type(exc).__name__)
                return await _send_json(send, 503, {"error": "auth_unavailable"})
            state["device_id"] = device
            role = self.core.auth.role_of(device)
            state["role"] = role
            if await self._credential_refusal(
                    send, client, scope["method"], path, is_pool_route,
                    headers.get("authorization"), device):
                return
            if self.public and device is not None and role != "guest":
                return await self._deny(send, client, 403, {"error": "forbidden_role"})     # owner tokens: LAN only

        clen = headers.get("content-length")
        pool_upload = is_pool_route and scope["method"] == "PUT"
        package_upload = pool_upload and path.endswith("/nvmt")
        body_limit = MAX_NVMT_BYTES if package_upload else (256 * 1024 if pool_upload else MAX_BODY)
        if clen is not None and (not clen.isdigit() or int(clen) > body_limit):
            return await _send_json(send, 413, {"error": "too_large"})
        body = b""
        more = True
        if scope["method"] in ("POST", "PUT", "PATCH", "DELETE"):
            while more:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    return
                body += msg.get("body", b"")
                more = msg.get("more_body", False)
                if len(body) > body_limit:
                    return await _send_json(send, 413, {"error": "too_large"})
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        if not self.admin:
            if self.public and kind == "pair":
                try:
                    j = json.loads(body or b"{}")
                except ValueError:
                    j = None
                if not isinstance(j, dict):
                    return await _send_json(send, 400, {"error": "bad_request"})
                if path.endswith("/start"):
                    if not isinstance(j.get("invite"), str):                    # the owner's pairing window is never public
                        return await self._deny(send, client, 409, {"error": "pairing_closed"})
                elif not (isinstance(j.get("pair_id"), str) and self.core.invites.knows(j["pair_id"])):
                    return await self._deny(send, client, 403, {"error": "pairing_failed"})
            if role == "guest" and not is_pool_route:
                method = scope["method"]
                if not self.core.sharing_enabled() and not guest_route_allowed(method, path, GUEST_WHEN_SHARING_OFF):
                    return await _send_json(send, 403, {"error": "sharing_disabled"})
                if not guest_route_allowed(method, path):
                    return await _send_json(send, 403, {"error": "forbidden_role"})

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                if self.public and message.get("status") in (401, 403) and client[0]:
                    self.core.public_limiter.strike(client[0])          # wrong proof, guessed id ...
                hs = list(message.get("headers", []))
                names = {k.lower() for k, _ in hs}
                for k, v in SECURITY_HEADERS:
                    if k not in names:
                        hs.append((k, v))
                if b"cache-control" not in names:
                    hs.append((b"cache-control", b"no-store"))
                hs = [(k, v) for k, v in hs if k.lower() != b"server"]
                message = dict(message, headers=hs)
            await send(message)

        await self.app(scope, replay, send_wrapper)

    async def _credential_refusal(self, send, client, method: str, path: str, is_pool_route: bool,
                                  authorization: str | None, device: str | None) -> bool:
        """H6: missing, malformed, and revoked credentials are distinct documented refusals,
        and they arrive before the body is read. Checks a later handler owns (a guest's
        contribution right, a read key trying to write) still reach that handler."""
        if path == "/v1/internal/tick":
            secret, admin = self.core.settings.cron_secret, self.core.admin_token
            if not secret and not admin:
                await self._deny(send, client, 503, {"error": "tick_unconfigured"})
                return True
            credential, malformed = _bearer_credential(authorization)
            given = (credential or "").encode("utf-8", "replace")
            accepted = [value.encode("utf-8") for value in (secret, admin) if value]
            if malformed or credential is None or not any(
                    hmac.compare_digest(given, value) for value in accepted):
                await self._deny(send, client, 401, {"error": "unauthorized"})
                return True
            return False
        if is_pool_route:
            if self.core.pool is None:
                return False                      # no pool surface: the uniform 404 stays the handler's answer
            if self.public and method in ("GET", "HEAD"):
                return False                      # anonymous public reads are part of the public contract
            if device is not None:
                return False
            credential, malformed = _bearer_credential(authorization)
            if malformed:
                await self._deny(send, client, 400, {"error": "bad_auth"})
                return True
            if credential is None:
                await self._deny(send, client, 401, {"error": "unauthorized"})
                return True
            if self._pool_key(credential):
                return False                      # a pool key: the handler applies read/write rights
            if token_shaped(credential):
                try:
                    revoked = self.core.auth.is_revoked_token(credential)
                except Exception as exc:
                    log.warning("device revocation lookup unavailable (%s)", type(exc).__name__)
                    await self._deny(send, client, 503, {"error": "auth_unavailable"})
                    return True
                await self._deny(send, client, 403,
                                 {"error": "revoked_key" if revoked else "forbidden"})
                return True
            await self._deny(send, client, 400, {"error": "bad_auth"})
            return True
        if path in PUBLIC_PATHS or device is not None:
            return False
        credential, malformed = _bearer_credential(authorization)
        if malformed:
            await self._deny(send, client, 400, {"error": "bad_auth"})
            return True
        if credential is None:
            await self._deny(send, client, 401, {"error": "unauthorized"})
            return True
        if not token_shaped(credential):
            await self._deny(send, client, 400, {"error": "bad_auth"})
            return True
        try:
            revoked = self.core.auth.is_revoked_token(credential)
        except Exception as exc:
            log.warning("device revocation lookup unavailable (%s)", type(exc).__name__)
            await self._deny(send, client, 503, {"error": "auth_unavailable"})
            return True
        if revoked:
            await self._deny(send, client, 403, {"error": "revoked_key"})
            return True
        await self._deny(send, client, 401, {"error": "unauthorized"})
        return True

    def _pool_key(self, credential: str) -> bool:
        settings = self.core.settings
        for key in (settings.pool_read_key, settings.pool_write_key):
            if key and credential.isascii() and key.isascii() and hmac.compare_digest(credential, key):
                return True
        return False

    def _admin_check(self, scope, headers) -> str | None:
        host = headers.get("host", "")
        if self.core.settings.profile == "vercel":
            if not host or any(ch in host for ch in "\r\n/\\"):
                return "bad_host"
            origin = headers.get("origin")
            if origin:
                try:
                    parsed = urlsplit(origin)
                except ValueError:
                    return "bad_origin"
                if (parsed.scheme != "https" or parsed.netloc.casefold() != host.casefold() or
                        parsed.path or parsed.query or parsed.fragment):
                    return "bad_origin"
        else:
            port = self.core.settings.admin_port
            if host not in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
                return "bad_host"             # DNS-rebinding guard
        origin = headers.get("origin")
        if self.core.settings.profile != "vercel" and origin is not None:
            if origin not in (f"http://127.0.0.1:{self.core.settings.admin_port}",
                              f"http://localhost:{self.core.settings.admin_port}"):
                return "bad_origin"
        if scope["method"] == "GET" and scope.get("path") == "/":
            return None                       # the static dashboard shell: no data, no secret; its script gets the token from the URL fragment
        given = headers.get("x-openseek-admin", "")
        if not hmac.compare_digest(given.encode("utf-8", "replace"), self.core.admin_token.encode("utf-8")):
            return "admin_auth"               # every other admin call needs the per-install secret (also stops CSRF: a custom header)
        return None


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http(request, exc):  # noqa: ANN001
        code = {404: "not_found", 405: "method_not_allowed", 401: "unauthorized", 403: "forbidden",
                413: "too_large"}.get(exc.status_code, "bad_request" if exc.status_code < 500 else "internal")
        return err(exc.status_code, code)

    @app.exception_handler(RequestValidationError)
    async def _val(request, exc):  # noqa: ANN001 - never echo the input
        return err(400, "bad_request")

    @app.exception_handler(RemoteStoreError)
    async def _remote_store(request, exc: RemoteStoreError):
        # Provider response text can contain project details; expose a stable retryable code.
        log.warning("remote pool store unavailable (status=%d)", exc.status)
        return err(503, "pool_backend_unavailable", {"Retry-After": "5"})

    @app.exception_handler(Exception)
    async def _any(request, exc):  # noqa: ANN001
        log.error("unhandled %s", type(exc).__name__)
        return err(500, "internal")


def base_url(request: Request) -> str | None:
    fixed = request.scope.get("openseek.base")        # the public listener never echoes a client-supplied Host
    if fixed:
        return fixed
    host = request.headers.get("host", "")
    if not _HOST_RE.match(host):
        return None
    return f"{request.url.scheme}://{host}"


def request_host(request: Request) -> str | None:
    host = request.headers.get("host", "")
    return host if _HOST_RE.fullmatch(host) else None


# ===================================================================== models
class ClaimIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pair_id: StrictStr = Field(max_length=64)
    device_name: StrictStr = Field(max_length=256)
    client_proof: StrictStr = Field(max_length=128)


class UploadIn(BaseModel):
    """Vercel staging request: the same hints as the direct NVMT PUT, never bytes."""
    model_config = ConfigDict(extra="ignore")
    fp: StrictStr = Field(min_length=1, max_length=1024)
    profile: StrictStr = Field(min_length=1, max_length=1024)
    duration_ms: StrictInt
    size_bytes: StrictInt | None = None
    imdb: StrictStr | None = Field(default=None, max_length=1024)
    season: StrictInt = 0
    episode: StrictInt = 0
    filename: StrictStr | None = Field(default=None, max_length=1024)


class GenerationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_url: StrictStr = Field(min_length=1, max_length=4096)
    profile: Literal["nvmt-v1-g5", "nvmt-v1-g10"] = "nvmt-v1-g10"
    imdb: StrictStr | None = Field(default=None, max_length=16)
    season: StrictInt = Field(default=0, ge=0, le=9999)
    episode: StrictInt = Field(default=0, ge=0, le=9999)
    filename: StrictStr | None = Field(default=None, max_length=1024)

    @field_validator("imdb", mode="before")
    @classmethod
    def _blank_imdb_is_absent(cls, value: object) -> object:
        """Blank/whitespace-only IMDb is absent; nonblank values are untouched."""
        if isinstance(value, str) and not value.strip():
            return None
        return value


class PoolPurgeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    dry_run: StrictBool = True
    outbox_older_than_days: StrictInt = Field(default=30, ge=1, le=3650)
    outbox_attempts: StrictInt = Field(default=5, ge=0, le=100)
    vacuum: StrictBool = False


class StartIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    invite: StrictStr | None = Field(default=None, max_length=64)      # invitation id or its 4-letter tag (else the open window)


class InviteIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    label: StrictStr = Field(default="", max_length=64)
    ttl_s: StrictInt = Field(default=86400, ge=0, le=10 ** 7)
    device_days: StrictInt | None = Field(default=None, ge=1, le=3650)
    public: StrictBool = False        # embed the PUBLIC host:port (OPENSEEK_PUBLIC_HOST) instead of the LAN address
    read_only: StrictBool = False     # explicit Reader: by default an invited guest is a Contributor once the host enables contributions


class AdminSetupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    telegram_channel_id: StrictStr = Field(min_length=1, max_length=128)
    telegram_api_id: StrictInt = Field(gt=0)
    telegram_api_hash: StrictStr = Field(min_length=8, max_length=256)
    telegram_bot_token: StrictStr | None = Field(default=None, max_length=256)
    telegram_session: StrictStr | None = Field(default=None, max_length=8192)


class SharingIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: StrictBool


class SetupPairingIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    label: StrictStr = Field(default="Nuvio TV", max_length=64)
    enable_sharing: StrictBool = False


class PermIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    contribute: StrictBool


class PoolContributionsIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: StrictBool


# ===================================================================== API app
def create_api_app(core: Core, public: bool = False) -> FastAPI:
    app = FastAPI(title="OpenSeek", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  redirect_slashes=False)
    _install_error_handlers(app)
    jobs = core.jobs
    s = core.settings

    def device(request: Request) -> str:
        return request.state.device_id

    def pool_key(request: Request) -> str | None:
        scheme, _, credential = request.headers.get("authorization", "").partition(" ")
        return credential if scheme.lower() == "bearer" and credential else None

    def pool_allowed(request: Request, pool: PoolService, write: bool = False) -> bool:
        # Keep the host-to-host pool keys for OpenSeek synchronization. Nuvio and
        # other paired clients use their revocable device token instead; a guest
        # can publish only when both the invitation and the host's sharing switch
        # grant contribution. The public listener may serve read-only cache hits
        # without a device token, under its per-IP pool_read budget.
        if pool.authorize(pool_key(request), write=write):
            return True
        device_id = request.state.device_id
        if write:
            return core.can_contribute(device_id)
        if device_id is not None:
            return True
        return bool(request.scope.get("openseek.public"))

    def pool_write_denial(request: Request, pool: PoolService) -> JSONResponse | None:
        if pool.authorize(pool_key(request), write=True):
            return None
        device_id = request.state.device_id
        role = core.auth.role_of(device_id) if device_id is not None else None
        if core.pool_write_policy.allows_device_write(
                role=role, contribution_enabled=core.settings.contribution,
                can_contribute=core.can_contribute(device_id)):
            return None
        if device_id is not None and core.auth.role_of(device_id) == "guest":
            return err(403, "not_contribute")
        return err(403, "forbidden")

    def read_only_channel_response() -> JSONResponse:
        return JSONResponse({"error": "read_only_channel",
                             "contribute_url": _librarian_url(core.settings)}, status_code=403)

    def corrections_denial() -> JSONResponse | None:
        # Corrections live in the local SQLite index, which the Vercel profile does not
        # build. Refuse explicitly instead of accepting a write that ranking will ignore.
        if core.settings.profile == "vercel":
            return err(409, "corrections_unavailable")
        return None

    async def proxy_channel_upload(request: Request, fp: str) -> Response:
        if not is_fingerprint(fp):
            return err(400, "bad_key")
        target = f"{core.settings.pool_contrib_url.rstrip('/')}/v1/pool/{fp}/nvmt"
        if request.url.query:
            target += "?" + request.url.query
        headers = {
            "Authorization": f"Bearer {core.settings.pool_write_key}",
            "Content-Type": request.headers.get("content-type", "application/vnd.openseek.nvmt"),
        }
        try:
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False,
                                         timeout=core.settings.pool_timeout_s) as client:
                async with client.stream("PUT", target, headers=headers, content=request.stream()) as upstream:
                    if 300 <= upstream.status_code < 400:
                        return err(502, "contribute_redirect_refused")
                    chunks = []
                    body_size = 0
                    async for chunk in upstream.aiter_bytes():
                        body_size += len(chunk)
                        if body_size > 64 * 1024:
                            return err(502, "contribute_response_too_large")
                        chunks.append(chunk)
                    relay_headers = {name: upstream.headers[name] for name in
                                     ("content-type", "x-openseek-duration-ms", "retry-after")
                                     if name in upstream.headers}
                    return Response(b"".join(chunks), status_code=upstream.status_code,
                                    headers=relay_headers)
        except httpx.TimeoutException:
            return err(504, "contribute_timeout")
        except httpx.HTTPError:
            return err(502, "contribute_unavailable")

    @app.get("/v1/health")
    async def health(request: Request):
        if request.scope.get("openseek.public"):
            return {"service": "openseek", "api": API_VERSION}       # nothing about the host, version, tools or pairing state
        return {"service": "openseek", "api": API_VERSION, "version": __version__, "ffmpeg": core.ffmpeg,
                "pairing_open": core.pairing.is_open()}

    @app.post("/v1/pair/start")
    async def pair_start(body: StartIn | None = None):
        try:
            return core.pairing.start(body.invite if body is not None else None)
        except RateLimited:
            return err(429, "rate_limited", {"Retry-After": "60"})
        except PairingClosed:
            return err(409, "pairing_closed")

    @app.post("/v1/pair/claim")
    async def pair_claim(body: ClaimIn):
        try:
            return core.pairing.claim(body.pair_id, body.device_name, body.client_proof)
        except RateLimited:
            return err(429, "rate_limited", {"Retry-After": "60"})
        except PairingFailed:
            return err(403, "pairing_failed")

    @app.post("/v1/setup/pairing")
    async def setup_pairing(request: Request, body: SetupPairingIn | None = None):
        # The participant wizard exposes this short-lived key only while creating
        # the initial Nuvio invite, then removes it and redeploys.
        expected = os.environ.get("OPENSEEK_SETUP_KEY", "")
        if not _setup_pairing_key_valid(core.settings.profile, expected,
                                        request.headers.get("authorization")):
            return err(404, "not_found")
        label = body.label if body is not None else "Nuvio TV"
        enable_sharing = bool(body.enable_sharing) if body is not None else False
        if enable_sharing:
            # The participant explicitly consented to Nuvio guest-device access
            # for this invite. Only the sharing switch is recorded; the owner's
            # contribution choice and every other flag are left untouched.
            core.state_store.set_setting("sharing_enabled", "1")
        if not core.sharing_enabled():
            # An invite created while sharing stays off would be refused by the
            # pairing gate: fail named instead of completing falsely.
            return err(409, "sharing_disabled")
        try:
            invite = await asyncio.to_thread(core.invites.create, label, 86400, "guest", None, True)
        except Exception as exc:
            log.warning("Vercel pairing invite creation failed (%s)", type(exc).__name__)
            return err(503, "pairing_setup_unavailable", {"Retry-After": "5"})
        return JSONResponse(invite, headers={"Cache-Control": "no-store"})

    @app.get("/v1/pair/status")
    async def pair_status(request: Request):
        d = device(request)
        name = next((x["name"] for x in core.auth.devices() if x["device_id"] == d), None)
        core.auth.touch_device(d)
        return {"device_id": d, "device_name": name, "contribute": core.can_contribute(d)}

    @app.delete("/v1/pair", status_code=204)
    async def pair_revoke(request: Request):
        d = device(request)
        core.auth.revoke(d)
        return Response(status_code=204)

    # ------------------------------------------------------- shared tile pool
    @app.get("/v1/pool/{fp}/manifest")
    async def pool_manifest(request: Request, fp: str):
        pool = core.pool
        if pool is None:
            return err(404, "not_found")
        if not pool_allowed(request, pool):
            return err(403, "forbidden")
        profile = request.query_params.get("profile", "")
        try:
            manifest = await asyncio.to_thread(pool.manifest, fp, profile)
        except PoolError as exc:
            return err(exc.status, exc.code)
        if manifest is None:
            return err(404, "not_found")
        return JSONResponse(manifest, headers={"Cache-Control": "no-store"})

    @app.get("/v1/pool/{fp}/tiles/{name}")
    async def pool_tile(request: Request, fp: str, name: str):
        pool = core.pool
        if pool is None:
            return err(404, "not_found")
        if not pool_allowed(request, pool):
            return err(403, "forbidden")
        k = parse_index(name[:-4], 999_999) if name.endswith(".jpg") else None
        if k is None:
            return err(404, "not_found")
        profile = request.query_params.get("profile", "")
        try:
            got = await asyncio.to_thread(pool.tile, fp, profile, k)
        except PoolError as exc:
            return err(exc.status, exc.code)
        if got is None:
            return err(404, "not_found")
        data, meta = got
        return Response(data, media_type="image/jpeg", headers={
            "Cache-Control": "public, max-age=3600, immutable",
            "X-OpenSeek-Pts-Ms": str(meta["pts_ms"]),
            "X-OpenSeek-Duration-Ms": str(meta["duration_ms"]),
            "X-OpenSeek-N-Tiles": str(meta["n_tiles"]),
            "X-OpenSeek-Profile": meta["profile"],
            "X-OpenSeek-Sha256": meta["sha256"],
        })

    @app.put("/v1/pool/{fp}/tiles/{name}")
    async def pool_put_tile(request: Request, fp: str, name: str):
        pool = core.pool
        if pool is None:
            return err(404, "not_found")
        denied = pool_write_denial(request, pool)
        if denied is not None:
            return denied
        if core.pool_write_policy.path() != "direct":
            return read_only_channel_response()
        k = parse_index(name[:-4], 999_999) if name.endswith(".jpg") else None
        if k is None or request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "image/jpeg":
            return err(400, "bad_request")
        try:
            q = request.query_params
            profile = q.get("profile", "")
            pts_ms = int(q.get("pts_ms", ""))
            duration_ms = int(q.get("duration_ms", ""))
            n_tiles = int(q.get("n_tiles", ""))
            data = await request.body()
            result = await asyncio.to_thread(pool.upload, fp, profile, k, pts_ms, duration_ms, n_tiles, data)
        except ValueError:
            return err(400, "bad_request")
        except PoolError as exc:
            return err(exc.status, exc.code)
        return JSONResponse(result, status_code=200 if result["duplicate"] else 201,
                            headers={"Cache-Control": "no-store"})

    @app.get("/v1/pool/candidates")
    async def pool_candidates(request: Request):
        pool = core.pool
        if pool is None:
            return err(404, "not_found")
        if not pool_allowed(request, pool):
            return err(403, "forbidden")
        try:
            q = request.query_params
            size_text = q.get("size_bytes")
            device_id = getattr(request.state, "device_id", None)
            work_key = q.get("work_key")
            candidates = await asyncio.to_thread(
                pool.candidates,
                duration_ms=int(q.get("duration_ms", "")),
                imdb=q.get("imdb"), season=int(q.get("season", "0")),
                episode=int(q.get("episode", "0")),
                size_bytes=int(size_text) if size_text is not None else None,
                filename=q.get("filename"), exclude_fp=q.get("exclude_fp"),
                device_id=device_id, work_key=work_key)
        except ValueError:
            return err(400, "bad_request")
        except PoolError as exc:
            return err(exc.status, exc.code)
        return JSONResponse({"candidates": candidates}, headers={"Cache-Control": "no-store"})

    @app.put("/v1/pool/candidates/{fp}/offset")
    async def pool_candidate_offset(request: Request, fp: str):
        pool = core.pool
        if pool is None:
            return err(404, "not_found")
        if not pool_allowed(request, pool):
            return err(403, "forbidden")
        if not is_fingerprint(fp):
            return err(400, "bad_key")
        denied = corrections_denial()
        if denied is not None:
            return denied
        try:
            body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        except Exception:
            return err(400, "bad_request")
        work_key = body.get("work_key") or request.query_params.get("work_key")
        b_ms = body.get("b_ms")
        if not work_key or not isinstance(work_key, str) or len(work_key) > 64:
            return err(400, "bad_work_key")
        if b_ms is None or not isinstance(b_ms, int) or abs(b_ms) > 86_400_000:
            return err(400, "bad_offset")
        device_id = getattr(request.state, "device_id", None)
        if device_id is None:
            return err(401, "device_required")
        core.db.put_pool_offset(device_id, work_key, fp, b_ms)
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @app.delete("/v1/pool/candidates/{fp}/offset", status_code=204)
    async def pool_candidate_delete_offset(request: Request, fp: str):
        pool = core.pool
        if pool is None:
            return err(404, "not_found")
        if not pool_allowed(request, pool):
            return err(403, "forbidden")
        if not is_fingerprint(fp):
            return err(400, "bad_key")
        denied = corrections_denial()
        if denied is not None:
            return denied
        try:
            body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        except Exception:
            body = {}
        work_key = body.get("work_key") or request.query_params.get("work_key")
        if not work_key or not isinstance(work_key, str) or len(work_key) > 64:
            return err(400, "bad_work_key")
        device_id = getattr(request.state, "device_id", None)
        if device_id is None:
            return err(401, "device_required")
        core.db.delete_pool_offset(device_id, work_key, fp)
        return Response(status_code=204)

    @app.put("/v1/pool/candidates/{fp}/reject")
    async def pool_candidate_reject(request: Request, fp: str):
        pool = core.pool
        if pool is None:
            return err(404, "not_found")
        if not pool_allowed(request, pool):
            return err(403, "forbidden")
        if not is_fingerprint(fp):
            return err(400, "bad_key")
        denied = corrections_denial()
        if denied is not None:
            return denied
        try:
            body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        except Exception:
            return err(400, "bad_request")
        work_key = body.get("work_key") or request.query_params.get("work_key")
        days = body.get("days", 30)
        if not work_key or not isinstance(work_key, str) or len(work_key) > 64:
            return err(400, "bad_work_key")
        if not isinstance(days, int) or days < 1 or days > 365:
            return err(400, "bad_days")
        device_id = getattr(request.state, "device_id", None)
        if device_id is None:
            return err(401, "device_required")
        core.db.put_pool_rejection(device_id, work_key, fp, days)
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @app.delete("/v1/pool/candidates/{fp}/reject", status_code=204)
    async def pool_candidate_delete_reject(request: Request, fp: str):
        pool = core.pool
        if pool is None:
            return err(404, "not_found")
        if not pool_allowed(request, pool):
            return err(403, "forbidden")
        if not is_fingerprint(fp):
            return err(400, "bad_key")
        denied = corrections_denial()
        if denied is not None:
            return denied
        try:
            body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        except Exception:
            body = {}
        work_key = body.get("work_key") or request.query_params.get("work_key")
        if not work_key or not isinstance(work_key, str) or len(work_key) > 64:
            return err(400, "bad_work_key")
        device_id = getattr(request.state, "device_id", None)
        if device_id is None:
            return err(401, "device_required")
        core.db.delete_pool_rejection(device_id, work_key, fp)
        return Response(status_code=204)

    @app.get("/v1/pool/episodes/{episode_key}")
    async def pool_episode_candidates(request: Request, episode_key: str):
        pool = core.pool
        if pool is None:
            return err(404, "not_found")
        if not pool_allowed(request, pool):
            return err(403, "forbidden")
        try:
            duration_ms = int(request.query_params.get("duration_ms", ""))
            candidates = await asyncio.to_thread(pool.episode_candidates, episode_key, duration_ms)
        except ValueError:
            return err(400, "bad_request")
        except PoolError as exc:
            return err(exc.status, exc.code)
        return JSONResponse({"candidates": candidates}, headers={"Cache-Control": "no-store"})

    @app.get("/v1/pool/{fp}/nvmt/manifest")
    async def pool_nvmt_manifest(request: Request, fp: str):
        """Return exact-package metadata before the client fetches NVMT chunks."""
        pool = core.pool
        if pool is None:
            return err(404, "not_found")
        if not pool_allowed(request, pool):
            return err(403, "forbidden")
        profile = request.query_params.get("profile", "")
        if profile not in ("nvmt-v1-g5", "nvmt-v1-g10"):
            return err(400, "bad_profile")
        try:
            requested_duration = request.query_params.get("duration_ms")
            duration_ms = int(requested_duration) if requested_duration is not None else 0
            if duration_ms and not 1 <= duration_ms <= MAX_MS:
                return err(400, "bad_duration")
            manifest = await asyncio.to_thread(pool.manifest, fp, PROFILE)
            tile_slots: list[int] = []
            stored_duration = 0
            if manifest is not None:
                stored_duration = int(manifest.get("duration_ms") or 0)
                tile_slots = sorted({int(tile["k"]) for tile in manifest.get("tiles", [])
                                     if isinstance(tile, dict) and str(tile.get("k", "")).isdigit()})
            if stored_duration and duration_ms and stored_duration != duration_ms:
                return err(404, "not_found")
            duration_ms = stored_duration or duration_ms
            if not tile_slots and hasattr(pool, "package"):
                try:
                    package = await asyncio.to_thread(pool.package, fp, profile)
                except PoolError as exc:
                    return err(exc.status, exc.code)
                if package is not None:
                    package_bytes, package_meta = package
                    duration_ms = int(package_meta["duration_ms"])
                    _grid, _slots, decoded_tiles = decode_nvmt(fp, profile, duration_ms, package_bytes)
                    tile_slots = sorted(decoded_tiles)
            if not duration_ms:
                return err(404, "not_found")
            max_tiles = (duration_ms + 9_999) // 10_000
            if not tile_slots or any(slot < 0 or slot >= max_tiles for slot in tile_slots):
                return err(404, "not_found")
            episode_key = request.query_params.get("episode_key")
            if episode_key:
                try:
                    await asyncio.to_thread(pool.index_episode_package, fp, profile, episode_key)
                except PoolError as exc:
                    return err(exc.status, exc.code)
        except ValueError:
            return err(400, "bad_request")
        except PoolError as exc:
            return err(exc.status, exc.code)
        grid = 5_000 if profile == "nvmt-v1-g5" else 10_000
        return JSONResponse({"fp": fp, "profile": profile, "duration_ms": duration_ms,
                             "n_slots": (duration_ms + grid - 1) // grid,
                             "n_tiles": len(tile_slots), "tile_slots": tile_slots},
                            headers={"Cache-Control": "no-store"})

    @app.get("/v1/pool/{fp}/nvmt")
    async def pool_nvmt(request: Request, fp: str):
        pool = core.pool
        if pool is None:
            return err(404, "not_found")
        if not pool_allowed(request, pool):
            return err(403, "forbidden")
        profile = request.query_params.get("profile", "")
        try:
            slots_text = request.query_params.get("slots")
            if slots_text is None:
                if isinstance(pool, ChannelPoolService) and "duration_ms" in request.query_params:
                    duration_ms = int(request.query_params["duration_ms"])
                    got = await asyncio.to_thread(pool.package, fp, profile, duration_ms)
                else:
                    got = await asyncio.to_thread(pool.package, fp, profile)
            else:
                parts = slots_text.split(",")
                if not 1 <= len(parts) <= 12 or any(not p.isdigit() for p in parts):
                    return err(400, "bad_slots")
                got = await asyncio.to_thread(pool.package_slots, fp, profile, [int(p) for p in parts])
        except PoolError as exc:
            return err(exc.status, exc.code)
        if got is None:
            return err(404, "not_found")
        data, meta = got
        if core.settings.profile == "vercel" and len(data) > MAX_VERCEL_RESPONSE:
            return err(413, "package_chunk_too_large")
        episode_key = request.query_params.get("episode_key")
        if episode_key:
            try:
                await asyncio.to_thread(pool.index_episode_package, fp, profile, episode_key)
            except PoolError as exc:
                return err(exc.status, exc.code)
        return Response(data, media_type="application/vnd.openseek.nvmt",
                        headers={"Cache-Control": "no-store",
                                 "X-OpenSeek-Duration-Ms": str(meta["duration_ms"]),
                                 "X-OpenSeek-N-Tiles": str(meta["n_tiles"]),
                                 "X-OpenSeek-Profile": meta["profile"],
                                 "X-OpenSeek-Sha256": meta["sha256"]})

    @app.put("/v1/pool/{fp}/nvmt")
    async def pool_put_nvmt(request: Request, fp: str):
        pool = core.pool
        if pool is None:
            return err(404, "not_found")
        denied = pool_write_denial(request, pool)
        if denied is not None:
            return denied
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type not in ("application/vnd.openseek.nvmt", "application/octet-stream"):
            return err(400, "bad_request")
        write_path = core.pool_write_policy.path(package=True)
        if write_path == "proxy":
            return await proxy_channel_upload(request, fp)
        if write_path == "read_only":
            return read_only_channel_response()
        try:
            profile = request.query_params.get("profile", "")
            duration_ms = int(request.query_params.get("duration_ms", ""))
            episode_key = request.query_params.get("episode_key")
            imdb = request.query_params.get("imdb")
            season = int(request.query_params.get("season", "0"))
            episode = int(request.query_params.get("episode", "0"))
            size_text = request.query_params.get("size_bytes")
            size_bytes = int(size_text) if size_text is not None else None
            filename = request.query_params.get("filename")
            data = await request.body()
            result = await asyncio.to_thread(
                pool.upload_package, fp, profile, duration_ms, data, episode_key,
                imdb=imdb, season=season, episode=episode, size_bytes=size_bytes, filename=filename)
        except ValueError:
            return err(400, "bad_request")
        except PoolError as exc:
            headers = ({"X-OpenSeek-Duration-Ms": str(exc.duration_ms)}
                       if exc.duration_ms is not None else None)
            return err(exc.status, exc.code, headers)
        return JSONResponse(result, status_code=200 if result["duplicate"] else 201,
                            headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- Vercel staged uploads
    @app.post("/v1/pool/uploads", status_code=201)
    async def pool_upload_create(request: Request):
        pool = core.pool
        if pool is None:
            # No pool surface at all: the uniform refused answer, leaking nothing.
            return err(404, "not_found")
        denied = pool_write_denial(request, pool)
        if denied is not None:
            return denied
        if core.uploads is None:
            return err(400, "staging_unavailable")      # local/VPS: direct PUT is the only path
        try:
            payload = await request.json()
        except ValueError:
            return err(400, "bad_request")
        if not isinstance(payload, dict):
            return err(400, "bad_request")
        try:
            body = UploadIn.model_validate(payload)
        except ValidationError:
            return err(400, "bad_request")
        try:
            result = await asyncio.to_thread(
                core.uploads.create, device_id=request.state.device_id, fp=body.fp,
                profile=body.profile, duration_ms=body.duration_ms,
                size_bytes=body.size_bytes, imdb=body.imdb, season=body.season,
                episode=body.episode, filename=body.filename)
        except UploadError as exc:
            return err(exc.status, exc.code)
        except ValueError:
            return err(400, "bad_request")
        return JSONResponse(result, status_code=201, headers={"Cache-Control": "no-store"})

    @app.post("/v1/pool/uploads/{upload_id}/finalize", status_code=202)
    async def pool_upload_finalize(request: Request, upload_id: str):
        pool = core.pool
        if pool is None:
            return err(404, "not_found")
        denied = pool_write_denial(request, pool)
        if denied is not None:
            return denied
        if core.uploads is None:
            return err(404, "unknown_upload")
        try:
            result = await asyncio.to_thread(core.uploads.finalize, upload_id)
        except UploadError as exc:
            return err(exc.status, exc.code)
        return JSONResponse(result, status_code=202, headers={"Cache-Control": "no-store"})

    @app.get("/v1/pool/uploads/{upload_id}")
    async def pool_upload_status(request: Request, upload_id: str):
        pool = core.pool
        if pool is None:
            return err(404, "not_found")
        if core.uploads is None:
            return err(404, "unknown_upload")
        write_key = pool.authorize(pool_key(request), write=True)
        try:
            # Check access before allowing the status poll to trigger node-wide work.
            result = await asyncio.to_thread(
                core.uploads.status, upload_id, device_id=request.state.device_id,
                write_key=write_key)
            if result["state"] in ("pending", "retrying", "paused"):
                # Client polling is also the retry trigger on deployments without a worker.
                await asyncio.to_thread(core.uploads.drain, 1)
            result = await asyncio.to_thread(
                core.uploads.status, upload_id, device_id=request.state.device_id,
                write_key=write_key)
        except UploadError as exc:
            return err(exc.status, exc.code)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get("/v1/internal/tick")
    @app.post("/v1/internal/tick")
    async def internal_tick(request: Request):
        """Cron entry point: drain a bounded number of queued Telegram publications."""
        secret = core.settings.cron_secret
        admin = core.admin_token
        if not secret and not admin:
            return err(503, "tick_unconfigured")
        given = (pool_key(request) or "").encode("utf-8", "replace")
        accepted = [value.encode("utf-8") for value in (secret, admin) if value]
        if not any(hmac.compare_digest(given, value) for value in accepted):
            return err(401, "unauthorized")
        if core.uploads is None:
            return err(503, "tick_unconfigured")
        raw_limit = request.query_params.get("limit", "1")
        if not raw_limit.isdigit():
            return err(400, "bad_request")
        limit = max(1, min(int(raw_limit), 3))
        try:
            result = await asyncio.to_thread(core.uploads.drain, limit)
        except Exception as exc:                       # store failure, never a per-item failure
            log.error("tick failed (%s)", type(exc).__name__)
            return err(503, "tick_failed")
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return Guard(app, core, public=public)  # type: ignore[return-value]


def api_openapi(core: Core) -> dict:
    """OpenAPI document of the API app (the live app does not serve it)."""
    guard = create_api_app(core)
    app: FastAPI = guard.app
    return app.openapi()


# ===================================================================== admin app
_ADMIN_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>OpenSeek admin</title>
<style>*{box-sizing:border-box}body{font:15px system-ui,sans-serif;margin:2em auto;width:calc(100% - 2em);max-width:52em;color:#222}
code{font-size:1.1em;overflow-wrap:anywhere;word-break:break-word}table{border-collapse:collapse;width:100%;table-layout:fixed}
.pairing-details,.invite-details{overflow-wrap:anywhere;word-break:break-word}
td,th{border:1px solid #ccc;padding:.3em .6em;overflow-wrap:anywhere}.code{font-size:2.4em;letter-spacing:.15em;
font-family:monospace;overflow-wrap:anywhere}button{font:inherit;min-height:2.5rem;margin:.2em;padding:.35em .7em;white-space:normal}
input,select{font:inherit;max-width:100%;min-width:0;min-height:2.5rem}input[type=url]{width:100%}form{max-width:100%}
@media(max-width:600px){body{margin:1em auto}.code{font-size:clamp(1.5em,9vw,2.4em);letter-spacing:.1em}
.setup-form label,.generation-form label{display:block;margin:.35em 0}.setup-form input,.generation-form input,.generation-form select{display:block;width:100%}
.device-table thead{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap;border:0}
.device-table tbody,.device-table tbody tr{display:block;width:100%}.device-table tbody tr{border:1px solid #ccc;margin:.6em 0;padding:.2em .6em}
.device-table td{display:grid;grid-template-columns:7rem minmax(0,1fr);align-items:center;gap:.5rem;border:0;padding:.3em 0}
.device-table td::before{font-weight:600}.device-table td:nth-child(1)::before{content:"Device ID"}.device-table td:nth-child(2)::before{content:"Name"}
.device-table td:nth-child(3)::before{content:"NVMT uploads"}.device-table td:nth-child(4)::before{content:"Actions"}
.device-table .empty-row td{display:block}.device-table .empty-row td::before{content:none}}</style></head><body>
<h1>OpenSeek</h1><div id=s>loading…</div>
<script>
const PROFILE='__PROFILE__';
let T=(location.hash||'').slice(1)||sessionStorage.getItem('osk')||'';if(T)sessionStorage.setItem('osk',T);history.replaceState(null,'',location.pathname);
const H={'X-OpenSeek-Admin':T};
const PAIR_KEY='oskPairing';
let authFailed=false;
let loadFailureVisible=false;
function esc(t){return String(t).replace(/[&<>"']/g,c=>'&#'+c.charCodeAt(0)+';')}
function isRecord(value){return value!==null&&typeof value==='object'&&!Array.isArray(value)}
function failure(kind){const e=new Error(kind);e.kind=kind;return e}
async function adminRequest(url,options){let response;try{response=await fetch(url,options)}catch(_){throw failure('network')}
if(response.status===204)return {response,data:null};let data=null;
try{data=await response.json()}catch(_){if(response.ok)throw failure('malformed')}
if(response.status===403&&isRecord(data)&&data.error==='admin_auth')throw failure('auth');
if(response.ok&&!isRecord(data))throw failure('malformed');return {response,data}}
function showFailure(kind){const root=document.getElementById('s');if(kind==='auth'){
authFailed=true;try{sessionStorage.removeItem('osk');sessionStorage.removeItem(PAIR_KEY)}catch(_){}
window.lastPairing='';window.lastInvite='';window.setupMessage='';window.generationMessage='';T='';delete H['X-OpenSeek-Admin'];
const next=PROFILE==='vercel'?'Reopen the dashboard with the correct admin link from your deployment.':
'On the server, run python -m openseek dashboard to get a fresh link, then reopen it.';
root.innerHTML='<section role="alert" aria-live="assertive" tabindex="-1"><h2>Admin access denied</h2><p>The admin secret was not accepted. '+esc(next)+'</p></section>';
return}
if(loadFailureVisible)return;loadFailureVisible=true;
root.innerHTML='<section role="alert" aria-live="assertive" tabindex="-1"><h2>Dashboard status unavailable</h2><p>OpenSeek could not load a valid dashboard response. Check that the server is reachable, then retry.</p><button type="button" onclick="load()">Retry</button></section>';
}
async function safely(action){try{return await action()}catch(e){showFailure(e&&e.kind==='auth'?'auth':'load');return null}}
function setupErrorText(code){const messages={telegram_access_denied:'OpenSeek cannot read this channel. Add the bot or account to the channel and grant access to its message history.',telegram_login_required:'Telegram rejected these credentials. Check the bot token or StringSession and Telegram API ID/hash.',telegram_unavailable:'Telegram could not be reached. Check the network and try again.',telegram_error:'Telegram could not verify this channel. Check the channel ID and account permissions.'};return messages[code]||('Setup failed ('+code+'). Check the channel ID, Telegram credentials, and channel permissions.')}
async function act(m,u){return safely(async()=>{await adminRequest(u,{method:m,headers:H});if(m==='DELETE'&&u==='/admin/pair'){sessionStorage.removeItem(PAIR_KEY);window.lastPairing=''}await load()})}
async function openPair(){return safely(async()=>{const x=await adminRequest('/admin/pair',{method:'POST',headers:H}),r=x.response,j=x.data;
if(r.ok&&(typeof j.code!=='string'||typeof j.spki_pin!=='string'))throw failure('malformed');
window.lastPairing=r.ok?('Nuvio code: '+j.code+' | TLS pin: '+j.spki_pin+(j.join?' | Join link: '+j.join:'')):'Could not open pairing: '+((j&&j.error)||'request_failed');
if(r.ok)sessionStorage.setItem(PAIR_KEY,window.lastPairing);else sessionStorage.removeItem(PAIR_KEY);await load()})}
async function sharing(on){return safely(async()=>{await adminRequest('/admin/sharing',{method:'PUT',headers:Object.assign({'Content-Type':'application/json'},H),body:JSON.stringify({enabled:on})});await load()})}
async function poolContributions(on){return safely(async()=>{await adminRequest('/admin/pool/contributions',{method:'PUT',headers:Object.assign({'Content-Type':'application/json'},H),body:JSON.stringify({enabled:on})});await load()})}
async function deviceContribution(id,on){return safely(async()=>{await adminRequest('/admin/devices/'+encodeURIComponent(id)+'/permissions',{method:'PUT',headers:Object.assign({'Content-Type':'application/json'},H),body:JSON.stringify({contribute:on})});await load()})}
async function mkinvite(){return safely(async()=>{const x=await adminRequest('/admin/invites',{method:'POST',headers:Object.assign({'Content-Type':'application/json'},H),body:JSON.stringify({public:!!window.pubOn})}),r=x.response,j=x.data;
if(r.ok&&(typeof j.address!=='string'||typeof j.code!=='string'||typeof j.join!=='string'||!Number.isFinite(j.expires_in_s)))throw failure('malformed');
window.lastInvite=r.ok?('Server '+j.address+' - code '+j.code+' (valid '+Math.round(j.expires_in_s/3600)+' h, one use). Join string: '+j.join):'Could not create: '+((j&&j.error)||'request_failed');await load()})}
async function generate(event){event.preventDefault();return safely(async()=>{const d=Object.fromEntries(new FormData(event.currentTarget));d.season=Number(d.season||0);d.episode=Number(d.episode||0);
const x=await adminRequest('/admin/generations',{method:'POST',headers:Object.assign({'Content-Type':'application/json'},H),body:JSON.stringify(d)}),r=x.response,j=x.data;
if(r.ok&&typeof j.generation_id!=='string')throw failure('malformed');window.generationMessage=r.ok?'Generation started: '+j.generation_id:'Could not start generation: '+((j&&j.error)||'request_failed');await load()})}
window.setupScreen=false;
async function setupSubmit(event){event.preventDefault();return safely(async()=>{const f=event.currentTarget;const d=Object.fromEntries(new FormData(f));d.telegram_api_id=Number(d.telegram_api_id);
const x=await adminRequest('/admin/setup',{method:'POST',headers:Object.assign({'Content-Type':'application/json'},H),body:JSON.stringify(d)}),r=x.response,j=x.data;
if(!r.ok){document.getElementById('setupError').textContent=setupErrorText((j&&j.error)||'setup_failed');return}
if(!isRecord(j.initial_scan)||!Number.isFinite(j.initial_scan.indexed)||typeof j.channel_writable!=='boolean')throw failure('malformed');
window.setupMessage='Telegram history is readable. Initial scan indexed '+j.initial_scan.indexed+' tiles; '+(j.channel_writable?'channel posting is available.':'this channel is read-only, so uploads are disabled. Grant the bot Post Messages permission to enable uploads.');window.setupScreen=false;await load()})}
async function load(){if(authFailed)return;if(!T){document.getElementById('s').textContent=PROFILE==='vercel'?'Open this page with the admin secret in the URL fragment: https://'+location.host+'/#<OPENSEEK_ADMIN_TOKEN>':'Open this page with: python -m openseek dashboard';return}
return safely(async()=>{
const cx=await adminRequest('/admin/setup',{headers:H});if(!cx.response.ok)throw failure('load');const cfg=cx.data;
if(typeof cfg.setup_required!=='boolean')throw failure('malformed');let setupHtml='';if(cfg.setup_required){setupHtml=
'<h2>Connect the Telegram pool</h2><p>OpenSeek stores packages in Telegram. Enter the channel and Telegram API details. Provide one identity: a bot token or a StringSession. The channel must allow history access; posting permission enables uploads.</p>'+
'<form class="setup-form" onsubmit="setupSubmit(event)"><p><label>Channel ID or username<br><input name="telegram_channel_id" required maxlength="128"></label></p>'+
'<p><label>Telegram API ID<br><input name="telegram_api_id" type="number" min="1" required></label></p>'+
'<p><label>Telegram API hash<br><input name="telegram_api_hash" type="password" autocomplete="off" required></label></p>'+
'<p><label>Bot token (leave blank when using a StringSession)<br><input name="telegram_bot_token" type="password" autocomplete="off"></label></p>'+
'<p><label>StringSession (leave blank when using a bot token)<br><input name="telegram_session" type="password" autocomplete="off"></label></p>'+
'<p>Credentials are saved in the per-install private .env file. They are never shown in logs or returned by the server.</p>'+
'<button type="submit">Verify channel and start scan</button> <span id="setupError" role="alert"></span></form>';}
window.setupScreen=false;const rx=await adminRequest('/admin/status',{headers:H});if(!rx.response.ok)throw failure('load');const r=rx.data;
if(!isRecord(r.pairing)||typeof r.pairing.open!=='boolean'||!isRecord(r.server)||!Array.isArray(r.server.addresses)||
!Array.isArray(r.devices)||!r.devices.every(isRecord)||!isRecord(r.cache)||
!['entries','tiles','bytes','max_bytes'].every(k=>Number.isFinite(r.cache[k])))throw failure('malformed');
if(!r.pairing.open){sessionStorage.removeItem(PAIR_KEY);window.lastPairing=''}else if(!window.lastPairing){window.lastPairing=sessionStorage.getItem(PAIR_KEY)||''}
let h=setupHtml;
if(window.setupMessage){h+='<p>'+esc(window.setupMessage)+'</p>';window.setupMessage=''}
h+='<h2>Server</h2><p>'+(r.server.profile==='vercel'?'Dashboard host <code>'+esc(location.host)+'</code>':'API port <code>'+esc(r.server.port)+'</code>, addresses: <code>'+esc(r.server.addresses.join(', '))+'</code>')+
'<br>Pin: <code>'+esc(r.server.spki_pin)+'</code></p><h2>Pairing</h2>';
if(window.lastPairing)h+='<p class=pairing-details>'+esc(window.lastPairing)+'</p>';
if(r.pairing.open){h+=(r.pairing.code?'<p class=code>'+esc(r.pairing.code)+'</p>':'')+'<p>expires in '+esc(r.pairing.expires_in_s)+
' s, attempts left '+esc(r.pairing.attempts_left)+'</p><button onclick="act(\\'DELETE\\',\\'/admin/pair\\')">Close</button>'}
else{h+=(r.pairing.burned?'<p><b>Last window burned after too many wrong codes.</b></p>':'')+
'<button onclick="openPair()">Open pairing window</button>'}
h+='<h2>Devices</h2><table class="device-table"><thead><tr><th scope="col">id</th><th scope="col">name</th><th scope="col">NVMT uploads</th><th scope="col">Actions</th></tr></thead><tbody>';
if(!r.devices.length)h+='<tr class="empty-row"><td colspan="4">No paired devices</td></tr>';
for(const d of r.devices){h+='<tr><td><code>'+esc(d.device_id)+'</code></td><td>'+esc(d.name)+'</td><td>'+
(d.role==='guest'?'<button onclick="deviceContribution(\\''+esc(d.device_id)+'\\','+(!d.contribute)+')">'+(d.contribute?'Disable':'Enable')+'</button>': 'Owner')+
'</td><td><button onclick="act(\\'DELETE\\',\\'/admin/devices/'+esc(d.device_id)+'\\')">Revoke</button></td></tr>'}
h+='</tbody></table>';
const sx=await adminRequest('/admin/sharing',{headers:H});if(!sx.response.ok)throw failure('load');const sh=sx.data;
if(typeof sh.enabled!=='boolean'||!Array.isArray(sh.invites)||!sh.invites.every(i=>isRecord(i)&&typeof i.invite_id==='string'))throw failure('malformed');
h+='<h2>Sharing</h2><p>Sharing is <b>'+(sh.enabled?'ON':'OFF')+'</b> <button onclick="sharing('+(sh.enabled?'false':'true')+')">Turn '+(sh.enabled?'off':'on')+'</button></p>';
if(sh.enabled){h+='<p>Guest NVMT contributions are <b>'+(sh.pool_contributions_enabled?'ON':'OFF')+'</b> <button onclick="poolContributions('+(sh.pool_contributions_enabled?'false':'true')+')">Turn guest uploads '+(sh.pool_contributions_enabled?'off':'on')+'</button></p>'}
window.pubOn=!!(sh.public&&sh.public.listening&&sh.public.host);if(sh.enabled){h+='<button onclick="mkinvite()">Create invitation'+(window.pubOn?' (public: '+esc(sh.public.host)+')':' (LAN)')+'</button> <span class="invite-details" id=inv>'+esc(window.lastInvite||'')+'</span><table><tr><th>invitation</th><th>state</th><th>expires in</th><th></th></tr>';
for(const i of sh.invites){h+='<tr><td>'+esc(i.label||i.invite_id.slice(0,8))+'</td><td>'+esc(i.state)+'</td><td>'+esc(i.expires_in_s)+' s</td><td>'+
(i.state=='open'?'<button onclick="act(\\'DELETE\\',\\'/admin/invites/'+esc(i.invite_id)+'\\')">Revoke</button>':'')+'</td></tr>'}
h+='</table>'}
h+='<h2>Pool</h2>';
const gx=await adminRequest('/admin/generations',{headers:H});if(!gx.response.ok)throw failure('load');const gj=gx.data;
if(!Array.isArray(gj.jobs)||!gj.jobs.every(isRecord))throw failure('malformed');
const jobsHtml=gj.jobs.map(j=>'<p><code>'+esc(j.generation_id)+'</code> '+esc(j.state)+' · '+esc(j.tiles_done||0)+'/'+esc(j.tiles_total||'?')+' tiles'+(j.error?' · '+esc(j.error):'')+'</p>').join('');
 h+='<h2>Generate NVMT package</h2><p>This runs only when you start it here. It reads the media URL and publishes the generated package to Telegram.</p><form class="generation-form" onsubmit="generate(event)">'+
'<p><label>Media URL<br><input name="source_url" type="url" required maxlength="4096" style="width:100%"></label></p><p><label>Profile <select name="profile"><option>nvmt-v1-g10</option><option>nvmt-v1-g5</option></select></label> '+
'<label>IMDb ID <input name="imdb" placeholder="tt1234567"></label> <label>Season <input name="season" type="number" min="0" value="0"></label> <label>Episode <input name="episode" type="number" min="0" value="0"></label></p>'+
'<p><label>Filename hint <input name="filename" maxlength="1024"></label> <button type="submit">Generate and publish</button></p></form>'+
(window.generationMessage?'<p role="status">'+esc(window.generationMessage)+'</p>':'')+'<div id="generationJobs">'+jobsHtml+'</div>';
const px=await adminRequest('/admin/pool/status',{headers:H}),pr=px.response,p=isRecord(px.data)?px.data:{error:'request_failed'};
if(!pr.ok)h+='<p role="alert">Pool setup check failed: '+esc(setupErrorText(p.reason||p.error||'setup_failed'))+'</p>';
h+='<p>Mode <b>'+esc(p.mode)+'</b>';
if(p.mode==='channel'){h+='; channel <code>'+esc(p.channel_id)+'</code>; '+esc(p.tiles)+' tiles; history '+
(p.history_readable?'readable':'unavailable (bot identity: publishing only)')+'; posting '+(p.channel_writable?'available':'read-only')+(p.channel_writable?'':'; grant the bot Post Messages permission to enable uploads')+'; last message '+
esc(p.last_message_id)+'; pending uploads '+esc(p.outbox)+(p.last_error?'; last error '+esc(setupErrorText(p.last_error)):'')+' <button onclick="act(\\'POST\\',\\'/admin/pool/scan\\')">Scan</button> '+
'<button onclick="act(\\'POST\\',\\'/admin/pool/rebuild\\')">Rebuild index</button>'}
else if(p.mode==='host'){h+='; API '+(p.api_enabled?'enabled':'disabled')+(p.api_url?'; URL <code>'+esc(p.api_url)+'</code>':'')+
'; '+esc(p.tiles)+' tiles; '+
esc(p.packages)+' packages; pending uploads '+esc(p.outbox)}
else{h+='; no shared pool connections'}
h+='</p>';
h+='<h2>Cache</h2><p>'+esc(r.cache.entries)+' titles, '+esc(r.cache.tiles)+' tiles, '+
esc((r.cache.bytes/1048576).toFixed(1))+' MiB of '+esc((r.cache.max_bytes/1048576).toFixed(0))+' MiB; '+
esc(r.active_sources)+' active source(s)</p>';loadFailureVisible=false;document.getElementById('s').innerHTML=h})}
load();setInterval(load,3000);
</script></body></html>"""


def lan_addresses() -> list[str]:
    out = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))   # TEST-NET; UDP connect sends no packet, it only picks a route
            out.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    return out or ["127.0.0.1"]


def create_admin_app(core: Core) -> FastAPI:
    app = FastAPI(title="OpenSeek admin", docs_url=None, redoc_url=None, openapi_url=None,
                  redirect_slashes=False)
    _install_error_handlers(app)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(_ADMIN_HTML.replace("const PROFILE='__PROFILE__';",
                                               f"const PROFILE='{core.settings.profile}';"), headers={
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                       "connect-src 'self'; frame-ancestors 'none'"})

    @app.get("/admin/setup")
    async def setup_status():
        return {"profile": core.settings.profile,
                "setup_required": core.settings.profile == "local" and
                                 core.settings.effective_pool_mode == "local"}

    @app.post("/admin/setup")
    async def setup(body: AdminSetupIn):
        if core.settings.profile == "vercel":
            return err(409, "set_environment_variables_in_vercel")
        if core.channel_index is not None:
            return err(409, "already_configured")
        channel_id = body.telegram_channel_id.strip()
        bot_token = (body.telegram_bot_token or "").strip()
        session = (body.telegram_session or "").strip()
        if not channel_id or bool(bot_token) == bool(session):
            return err(400, "bad_telegram_settings")
        if session and (any(sep in session for sep in ("/", "\\")) or
                        session.startswith("~") or session.endswith(".session")):
            return err(400, "telegram_session_must_be_a_string_session")

        settings = replace(core.settings, profile="local", pool_mode="channel", pool_enabled=False,
                           telegram_channel_id=channel_id, telegram_api_id=body.telegram_api_id,
                           telegram_api_hash=body.telegram_api_hash.strip(), telegram_bot_token=bot_token,
                           telegram_session=session, telegram_session_file=None, contribution=False,
                           pool_channel_writable="auto")
        try:
            settings.validate()
            probe = TelegramMTProtoStore(settings)
            probe.start()                         # resolves the channel and checks readable history + posting rights
            probe.scan(0, 1)                      # verify that history can actually be read
        except (ValueError, PoolError) as exc:
            if "probe" in locals():
                probe.close()
            return err(400 if isinstance(exc, ValueError) else exc.status,
                       getattr(exc, "code", "bad_telegram_settings"))

        try:
            write_env_file(core.data_dir / ".env", {
                "OPENSEEK_PROFILE": "local", "OPENSEEK_POOL_MODE": "channel",
                "OPENSEEK_TELEGRAM_CHANNEL_ID": channel_id,
                "OPENSEEK_TELEGRAM_API_ID": str(body.telegram_api_id),
                "OPENSEEK_TELEGRAM_API_HASH": settings.telegram_api_hash,
                "OPENSEEK_TELEGRAM_BOT_TOKEN": bot_token,
                "OPENSEEK_TELEGRAM_SESSION": session,
                "OPENSEEK_POOL_CHANNEL_WRITABLE": "auto",
                "OPENSEEK_CONTRIBUTION": "0",
            })
            core._configure_channel_pool(settings, probe)
            scan = await asyncio.to_thread(core.channel_index.refresh, True)
            core.channel_index.start()
        except (OSError, PoolError) as exc:
            if core.channel_store is not probe:
                probe.close()
            log.warning("browser Telegram setup failed (%s)", type(exc).__name__)
            return err(503, getattr(exc, "code", "setup_failed"))
        return {"configured": True, "profile": "local", "history_readable": probe.history_readable,
                "channel_writable": probe.can_post, "initial_scan": scan}

    @app.get("/admin/status")
    async def status():
        pairing = core.pairing.admin_status()
        if core.settings.profile == "vercel":
            active = [invite for invite in core.invites.listing()
                      if invite["role"] == "owner" and invite["state"] == "open"]
            pairing = {"open": bool(active), "expires_in_s": max(
                (invite["expires_in_s"] for invite in active), default=0),
                "attempts_left": min((invite["attempts_left"] for invite in active), default=0)}
        return {"server": {"profile": core.settings.profile, "port": core.settings.port,
                            "addresses": lan_addresses(), "spki_pin": core.spki_pin},
                "pairing": pairing, "devices": core.auth.devices(),
                "cache": core.cache.stats(), "active_sources": len(core.jobs.running_sources())}

    @app.get("/admin/sharing")
    async def sharing_status():
        return {"enabled": core.sharing_enabled(),
                "pool_contributions_enabled": core.pool_contributions_enabled(),
                "public": {"listening": bool(core.settings.public_port), "port": core.settings.public_port,
                           "host": core.settings.public_host, **core.public_limiter.snapshot()},
                "invites": core.invites.listing(),
                "guests": [d for d in core.auth.devices() if d["role"] == "guest"]}

    @app.delete("/admin/public/blocks", status_code=204)
    async def public_unblock():
        core.public_limiter.clear()             # lift every per-address block / strike / budget on the public listener
        return Response(status_code=204)

    @app.put("/admin/sharing")
    async def sharing_set(body: SharingIn):
        core.state_store.set_setting("sharing_enabled", "1" if body.enabled else "0")
        return {"enabled": core.sharing_enabled()}

    @app.put("/admin/pool/contributions")
    async def pool_contributions_set(body: PoolContributionsIn):
        core.state_store.set_setting("contrib_enabled", "1" if body.enabled else "0")
        return {"enabled": core.pool_contributions_enabled()}

    @app.post("/admin/invites")
    async def invite_create(request: Request, body: InviteIn | None = None):
        if not core.sharing_enabled():
            return err(409, "sharing_disabled")
        b = body or InviteIn()
        if b.public and not (core.settings.public_port and core.settings.public_host):
            return err(409, "public_endpoint_not_configured")
        inv = await asyncio.to_thread(core.invites.create, b.label, b.ttl_s or 86400, "guest", b.device_days, not b.read_only)
        if core.settings.profile == "vercel":
            host = request_host(request)
            if host is None:
                await asyncio.to_thread(core.invites.revoke, inv["invite_id"])
                return err(400, "bad_host")
        else:
            addrs = lan_addresses()
            host = core.settings.public_host if b.public else f"{addrs[0]}:{core.settings.port}"
        from urllib.parse import quote
        inv["address"] = host
        inv["join"] = (f"openseek://join/1?h={quote(host, safe='')}&p={quote(core.spki_pin, safe='')}"
                       f"&i={inv['invite_id']}&c={inv['secret']}")
        return inv

    @app.get("/admin/invites")
    async def invite_list():
        return {"invites": core.invites.listing()}

    @app.delete("/admin/invites/{invite_id}", status_code=204)
    async def invite_revoke(invite_id: str):
        if not is_invite_id(invite_id) or not core.invites.revoke(invite_id):
            return err(404, "not_found")
        return Response(status_code=204)

    @app.put("/admin/devices/{device_id}/permissions")
    async def device_permissions(device_id: str, body: PermIn):
        if not is_device_id(device_id) or not core.auth.set_contribute(device_id, body.contribute):
            return err(404, "not_found")
        return {"device_id": device_id, "contribute": body.contribute}

    @app.post("/admin/pool/purge")
    async def pool_purge(body: PoolPurgeIn):
        if core.pool is None or core.settings.effective_pool_mode != "host":
            return err(404, "not_found")
        try:
            return await asyncio.to_thread(
                core.pool.purge, dry_run=body.dry_run,
                outbox_older_than_days=body.outbox_older_than_days,
                outbox_attempts=body.outbox_attempts, vacuum=body.vacuum)
        except PoolError as exc:
            return err(exc.status, exc.code)

    @app.get("/admin/pool/status")
    async def pool_status():
        mode = core.settings.effective_pool_mode
        if mode == "channel" and core.channel_index is not None:
            result = {"mode": mode, **core.channel_index.status(),
                      "librarian_status": ("attached" if core.librarian is not None and
                                            getattr(core.channel_store, "_handler_attached", True) else
                                            "disabled" if not core.settings.librarian_enabled else "unavailable"),
                      "contribution_path": core.pool_write_policy.contribution_path()}
        elif mode == "host" and core.pool is not None:
            result = {"mode": mode, "api_enabled": True,
                      "api_url": f"https://{core.settings.public_host}" if core.settings.public_host else None,
                      "spki_pin": core.spki_pin,
                      "channel_id": core.settings.telegram_channel_id,
                      "tiles": core.db.q("SELECT COUNT(*) FROM pool_tiles")[0][0],
                      "packages": core.db.q("SELECT COUNT(*) FROM pool_packages")[0][0],
                      "outbox": core.db.q("SELECT COUNT(*) FROM pool_outbox")[0][0],
                      "last_error": core.pool_sync.last_error if core.pool_sync else ""}
        else:
            result = {"mode": mode, "api_enabled": False}
        if mode == "channel":
            if core.uploads is not None:
                result["outbox"] = await asyncio.to_thread(core.uploads.store.pending_count)
            else:
                pending = core.db.q("SELECT COUNT(*) FROM pool_channel_outbox")
                result["outbox"] = int(pending[0][0]) if pending else 0
            result["last_error"] = (core.pool_sync.last_error if core.pool_sync else
                                    core.channel_index.last_error if core.channel_index else "")
        return result

    @app.post("/admin/generations", status_code=202)
    async def generation_start(background_tasks: BackgroundTasks, body: GenerationIn):
        # Issue #78 (F3 local/VPS): in host mode the PoolService publishes packages,
        # shared tiles and hints itself — no MTProto channel store exists there, so it
        # is the generation capability. Channel installs still keep their store.
        if core.pool is None or (core.channel_store is None and
                                 not hasattr(core.pool, "upload_package")):
            return err(409, "pool_unavailable")
        try:
            generation_id, job = await asyncio.to_thread(
                core.generations.start, body.source_url, body.profile, body.imdb,
                body.season, body.episode, body.filename)
        except GenerationError as exc:
            return err(429 if exc.code in ("generation_busy", "busy") else 400, exc.code)
        except SourceForbidden:
            return err(400, "source_forbidden")
        background_tasks.add_task(core.generations.run, generation_id, job)
        return JSONResponse({"generation_id": generation_id, "state": "generating"},
                            status_code=202, headers={"Cache-Control": "no-store"})

    @app.get("/admin/generations")
    async def generation_list():
        return {"jobs": await asyncio.to_thread(core.generations.recent)}

    @app.get("/admin/generations/{generation_id}")
    async def generation_status(generation_id: str):
        status = await asyncio.to_thread(core.generations.status, generation_id)
        return status if status is not None else err(404, "not_found")

    @app.delete("/admin/generations/{generation_id}", status_code=204)
    async def generation_cancel(generation_id: str):
        if not await asyncio.to_thread(core.generations.cancel, generation_id):
            return err(409, "not_running")
        return Response(status_code=204)

    @app.post("/admin/pool/scan")
    async def pool_scan():
        if core.channel_index is None:
            return err(404, "not_found")
        try:
            return await asyncio.to_thread(core.channel_index.refresh, True)
        except PoolError as exc:
            return err(exc.status, exc.code)

    @app.post("/admin/pool/rebuild")
    async def pool_rebuild():
        if core.channel_index is None:
            return err(404, "not_found")
        try:
            return await asyncio.to_thread(core.channel_index.rebuild)
        except PoolError as exc:
            return err(exc.status, exc.code)

    @app.post("/admin/pair")
    async def open_pair(request: Request):
        if core.settings.profile == "vercel":
            host = request_host(request)
            if host is None:
                return err(400, "bad_host")
            if any(invite["role"] == "owner" and invite["state"] == "open"
                   for invite in core.invites.listing()):
                return err(409, "pairing_already_open")
            invite = await asyncio.to_thread(
                core.invites.create, "Nuvio device", 600, "owner", None, False)
            from urllib.parse import quote
            join = (f"openseek://join/1?h={quote(host, safe='')}&p={quote(core.spki_pin, safe='')}"
                    f"&i={invite['invite_id']}&c={quote(invite['secret'], safe='')}")
            return {"code": invite["code"], "join": join, "invite_id": invite["invite_id"],
                    "expires_in_s": invite["expires_in_s"], "spki_pin": core.spki_pin}
        code, ttl = await asyncio.to_thread(core.pairing.open_window)
        return {"code": code, "expires_in_s": int(ttl), "spki_pin": core.spki_pin}

    @app.delete("/admin/pair", status_code=204)
    async def close_pair():
        if core.settings.profile == "vercel":
            for invite in core.invites.listing():
                if invite["role"] == "owner" and invite["state"] == "open":
                    await asyncio.to_thread(core.invites.revoke, invite["invite_id"])
        else:
            core.pairing.close_window()
        return Response(status_code=204)

    @app.get("/admin/devices")
    async def devices():
        return {"devices": core.auth.devices()}

    @app.delete("/admin/devices/{device_id}", status_code=204)
    async def revoke(device_id: str):
        if not is_device_id(device_id):
            return err(404, "not_found")
        if not core.auth.revoke(device_id):
            return err(404, "not_found")
        return Response(status_code=204)

    return Guard(app, core, admin=True)  # type: ignore[return-value]


# ===================================================================== serve
def uvicorn_configs(core: Core, api, admin):
    import uvicorn

    s = core.settings
    api_cfg = uvicorn.Config(api, host=s.host, port=s.port, ssl_certfile=str(core.cert_path),
                             ssl_keyfile=str(core.key_path), ssl_ciphers="ECDHE+AESGCM:ECDHE+CHACHA20",
                             log_config=None, proxy_headers=False,
                             server_header=False, date_header=False, access_log=True, lifespan="off",
                             timeout_keep_alive=30, limit_concurrency=256)
    admin_cfg = uvicorn.Config(admin, host="127.0.0.1", port=s.admin_port, log_config=None, proxy_headers=False,
                               server_header=False, date_header=False, access_log=True, lifespan="off")
    # note: access_log=False on either config would silence the shared "uvicorn.access" logger
    return api_cfg, admin_cfg


def public_config(core: Core, app):
    """The internet-facing guest listener, or None when OPENSEEK_PUBLIC_PORT is not set. Same certificate and pin."""
    import uvicorn

    s = core.settings
    if not s.public_port:
        return None
    return uvicorn.Config(app, host=s.public_bind, port=s.public_port, ssl_certfile=str(core.cert_path),
                          ssl_keyfile=str(core.key_path), ssl_ciphers="ECDHE+AESGCM:ECDHE+CHACHA20",
                          log_config=None, proxy_headers=False, forwarded_allow_ips="",
                          server_header=False, date_header=False, access_log=True, lifespan="off",
                          timeout_keep_alive=10, limit_concurrency=64, limit_max_requests=None)


def log_banner(core: Core) -> None:
    s = core.settings
    addrs = lan_addresses() if s.host in ("0.0.0.0", "::") else [s.host]
    lines = [f"OpenSeek {__version__} started",
             f"API (HTTPS, LAN): listening on {s.host}:{s.port}; reachable at "
             + ", ".join(f"https://{a}:{s.port}" for a in addrs),
             f"TLS certificate pin: {core.spki_pin}",
             f"Admin (loopback only): http://127.0.0.1:{s.admin_port}  -  pair a TV with `openseek pair`"]
    for line in lines:
        log.info("%s", line, extra=plain())


def serve(settings: Settings) -> None:
    import uvicorn

    setup_logging(settings.log_level)
    from .hardening import published_on_error, refuse_shared_data_dir, running_in_container
    refuse_shared_data_dir(settings.data_dir)
    if running_in_container():
        why = published_on_error(os.environ.get("OPENSEEK_PUBLISHED_ON"))
        if why:
            raise SystemExit("refusing to start in a container: " + why)
    core = Core(settings).start()
    api_cfg, admin_cfg = uvicorn_configs(core, create_api_app(core), create_admin_app(core))
    servers = [uvicorn.Server(api_cfg), uvicorn.Server(admin_cfg)]
    pub_cfg = public_config(core, create_api_app(core, public=True))
    if pub_cfg is not None:
        servers.append(uvicorn.Server(pub_cfg))
        log.info("PUBLIC guest listener: %s:%d (advertised as %s)", settings.public_bind, settings.public_port,
                 settings.public_host or "(no public host set)", extra=plain())
    from .hardening import CONTAINER_NOTE, check_data_dir
    check_data_dir(core.data_dir, log.warning)
    if running_in_container():
        log.info(CONTAINER_NOTE, os.environ.get("OPENSEEK_PUBLISHED_ON", "?"))
    log_banner(core)

    async def main():
        await asyncio.gather(*(srv.serve() for srv in servers))

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        core.stop()
