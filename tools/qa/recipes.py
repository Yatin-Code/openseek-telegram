"""QA runner recipes for the epic campaign (P1 frame, issue #42; per-ticket
recipe content lands with each campaign ticket as it is contracted).

Two targets, one runner (per the Epic H spec):
- `local`: the existing live-server harness (real uvicorn + TLS in-process,
  ephemeral data directory, no pool) with the OSP1 client implementation.
- `live`: HTTPS over pinned TLS against a registered reference deployment from
  docs/qa/ARTIFACTS.md. Public identifiers by value; secret values are inputs
  from ~/.config/openseek/qa.env and are never echoed (the runner's guard runs
  on all evidence before printing).

Recipes are black-box: they drive the highest seam a user drives and assert
what the user observes.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent
_BACKEND = _TOOLS.parents[1] / "backend"
for _p in (_TOOLS, _BACKEND, _BACKEND / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import openseek                              # noqa: E402  for the version check
from client import HTTP                      # noqa: E402
from storage import key_of, public_of, qa_env_values  # noqa: E402


def _run_contract_local(steps) -> bool:
    """Documented health/pairing behaviour, black-box, from a cold start."""
    from helpers import LiveServer, ADMIN_TOKEN, peer_pin          # backend/tests/helpers
    from openseek.netpolicy import DestinationPolicy, TestOnlyAllowance
    from openseek.pairing import client_proof, derive_key, server_proof

    data_dir = Path(tempfile.mkdtemp(prefix="openseek-qa-local-"))
    srv = LiveServer(data_dir, DestinationPolicy(allowance=TestOnlyAllowance(frozenset())),
                     admin_token=ADMIN_TOKEN)
    import httpx
    ok = True
    try:
        # the pin is derived from the wire certificate, independently of the server's own banner
        pin = peer_pin("127.0.0.1", srv.port)
        steps.evidence(f"base={srv.base} admin={srv.admin} pin={pin}")
        with httpx.Client(base_url=srv.base, verify=False, timeout=30.0) as anon:

            name = "health exposes the documented shape with no auth"
            r = anon.get("/v1/health")
            body = r.json()
            passed = (r.status_code == 200 and body["service"] == "openseek" and
                      body["api"] == 1 and body["version"] == openseek.__version__ and
                      isinstance(body["pairing_open"], bool))
            steps.evidence(f"HTTP {r.status_code} {r.text[:160]}")
            steps.check(name, passed)
            ok = ok and passed

            name = "pair/start serves the pin the client independently derived"
            with httpx.Client(base_url=srv.admin, headers={"X-OpenSeek-Admin": ADMIN_TOKEN},
                              timeout=30.0) as admin:
                window = admin.post("/admin/pair")
                code_window = window.json()
            pin_code = code_window.get("code", "")
            r = anon.post("/v1/pair/start", json={})
            st = r.json()
            passed = (r.status_code == 200 and st.get("spki_pin") == pin and
                      st.get("iterations", 0) >= 200000)
            steps.evidence(f"HTTP {r.status_code}, pair_id {st.get('pair_id', 'n/a')[:8]}..., pin matches")
            steps.check(name, passed)
            ok = ok and passed

            # full OSP1 pairing with the real client-side implementation: the admin
            # opens the window over the loopback admin API, the client derives K
            # from the code and the pinned pin, and the claim must verify.
            name = "OSP1 pairing round-trips (proofs verified, device registered)"
            with httpx.Client(base_url=srv.admin, headers={"X-OpenSeek-Admin": ADMIN_TOKEN},
                              timeout=30.0) as admin:
                code_window = admin.post("/admin/pair").json()
            pin_code = code_window.get("code", "")
            r = anon.post("/v1/pair/start", json={})
            st = r.json()
            k = derive_key(pin_code, st["pair_id"], st["salt"], st["spki_pin"], st["iterations"])
            r = anon.post("/v1/pair/claim", json={"pair_id": st["pair_id"],
                                                  "device_name": "QA runner",
                                                  "client_proof": client_proof(k, st["pair_id"], "QA runner")})
            passed = False
            device_id = ""
            if r.status_code == 200:
                claim = r.json()
                device_id = claim["device_id"]
                passed = claim["server_proof"] == server_proof(k, st["pair_id"], claim["token"])
            steps.evidence(f"claim HTTP {r.status_code}, device {device_id[:8]}...")
            steps.check(name, passed)
            ok = ok and passed

            name = "an anonymous request to an unlisted route is the documented 404 not_found"
            r = anon.get("/v1/unknown-gone")
            passed = r.status_code == 404 and r.json()["error"] == "not_found"
            steps.evidence(f"HTTP {r.status_code} {r.text[:80]}")
            steps.check(name, passed)
            ok = ok and passed
    finally:
        srv.stop()
        shutil.rmtree(data_dir, ignore_errors=True)
    return ok


def _run_contract_host(steps) -> bool:
    """The contract surface of the registered host-mode node, black-box over TLS.
    Every connection verifies the SPKI pin from the operator's qa.env; a wrong
    pin aborts the run before any request is sent."""
    env = qa_env_values()
    base = public_of("OPENSEEK_G4_BASE_URL", env)
    pin = public_of("OPENSEEK_G4_TLS_PIN", env)
    read_key = key_of("OPENSEEK_G4_READ_KEY", env)
    write_key = key_of("OPENSEEK_G4_WRITE_KEY", env)
    package_fp = public_of("OPENSEEK_G4_PACKAGE_FP", env)
    http = HTTP(base, pin)
    steps.evidence(f"target={base} pin={pin}")
    ok = True

    name = "health exposes the documented shape with no auth"
    code, headers, body = http.get("/v1/health")
    st = json.loads(body)
    passed = code == 200 and st["service"] == "openseek" and st["api"] == 1
    steps.evidence(f"HTTP {code} {body.decode()[:160]}")
    steps.check(name, passed)
    ok = ok and passed

    probe = "d" * 64
    name = "a pool read with no key is refused (401 unauthorized)"
    code, headers, body = http.get(f"/v1/pool/{probe}/manifest?profile=nvmt-v1-g10")
    passed = code == 401 and json.loads(body)["error"] == "unauthorized"
    steps.evidence(f"HTTP {code} {body.decode()[:80]}")
    steps.check(name, passed)
    ok = ok and passed

    junk_headers = {"Content-Type": "text/plain"}
    name = "no key: refused before the request body is parsed (401 unauthorized, not 400)"
    code, headers, body = http.put(f"/v1/pool/{probe}/nvmt?profile=nvmt-v1-g10&duration_ms=30000",
                                   headers=junk_headers, data=b"junk-body-not-nvmt")
    passed = code == 401 and json.loads(body)["error"] == "unauthorized"
    steps.evidence(f"PUT (no key), invalid content-type + non-NVMT bytes: HTTP {code}")
    steps.check(name, passed)
    ok = ok and passed

    name = "write key, same garbage: reaches body validation (400 bad_request)"
    code, headers, body = http.put(f"/v1/pool/{probe}/nvmt?profile=nvmt-v1-g10&duration_ms=30000",
                                   headers={**junk_headers, "Authorization": f"Bearer {write_key}"},
                                   data=b"junk-body-not-nvmt")
    passed = code == 400 and json.loads(body)["error"] == "bad_request"
    steps.evidence(f"PUT (write key), same bytes: HTTP {code}")
    steps.check(name, passed)
    ok = ok and passed

    name = "retired legacy routes never reach a handler (404 not_found)"
    code, headers, body = http.get("/v1/previews/" + "b" * 32 + "/thumbs.vtt")
    passed = code == 404 and json.loads(body)["error"] == "not_found"
    steps.evidence(f"HTTP {code}")
    steps.check(name, passed)
    ok = ok and passed

    name = "read key returns the exact registered package (NVMT v1 header validated client-side)"
    code, headers, body = http.get(f"/v1/pool/{package_fp}/nvmt?profile=nvmt-v1-g10",
                                   headers={"Authorization": f"Bearer {read_key}"})
    read_sha = hashlib.sha256(body).hexdigest() if code == 200 else ""
    le = lambda b: int.from_bytes(b, "little")       # NVMT v1 stores every uint32 little-endian
    passed = (code == 200 and body[:4] == b"NVMT" and le(body[4:8]) == 1 and
              le(body[8:12]) == 10_000 and le(body[12:16]) == 3 and
              headers.get("x-openseek-sha256") == read_sha)
    steps.evidence(f"HTTP {code}, {len(body)} bytes, sha256 {read_sha}")
    steps.check(name, passed)
    ok = ok and passed

    name = "the write key also reads the same package byte-for-byte (facet of G4 criterion 2)"
    code, headers, body = http.get(f"/v1/pool/{package_fp}/nvmt?profile=nvmt-v1-g10",
                                   headers={"Authorization": f"Bearer {write_key}"})
    write_sha = hashlib.sha256(body).hexdigest() if code == 200 else ""
    passed = code == 200 and read_sha != "" and read_sha == write_sha
    steps.evidence(f"HTTP {code}, sha256 equal={read_sha == write_sha and read_sha != ''}")
    steps.check(name, passed)
    ok = ok and passed
    return ok


def _rate_line(headers: dict[str, str]) -> str:
    names = ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset")
    return " ".join(f"{name}={headers[name]}" for name in names if name in headers)


def _run_host_protections(steps) -> bool:
    """H6 (#57): the host API's documented contract and protections, live.

    Drives the registered G4 host node black-box over pinned TLS: the auth
    refusal matrix, auth-before-body precedence, read/write separation, the
    revoked-key lifecycle (real pairing + admin revoke), the documented
    404/405 shapes, and the advertised/enforced per-class rate limit.
    """
    import httpx

    from openseek.pairing import client_proof, derive_key

    env = qa_env_values()
    base = public_of("OPENSEEK_G4_BASE_URL", env)
    pin = public_of("OPENSEEK_G4_TLS_PIN", env)
    read_key = key_of("OPENSEEK_G4_READ_KEY", env)
    write_key = key_of("OPENSEEK_G4_WRITE_KEY", env)
    package_fp = public_of("OPENSEEK_G4_PACKAGE_FP", env)
    admin_key = key_of("OPENSEEK_G4_ADMIN_KEY", env)
    admin_base = env.get("OPENSEEK_G4_ADMIN_URL") or "http://127.0.0.1:18788"
    http = HTTP(base, pin)
    probe = "d" * 64
    steps.evidence(f"target={base} admin={admin_base} pin={pin} (pinned TLS)")
    ok = True

    def matrix(name: str, method: str, path: str, *, headers: dict | None = None,
               data: bytes = b"", status: int, error: str | None = None):
        nonlocal ok
        code, hdrs, body = http.request(method, path, headers=headers, data=data)
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        passed = code == status
        if error is not None:
            passed = passed and isinstance(parsed, dict) and parsed.get("error") == error
        line = f"{method} {path} -> {code} {body[:120].decode('utf-8', 'replace')}"
        if _rate_line(hdrs):
            line += f" | {_rate_line(hdrs)}"
        steps.evidence(line)
        steps.check(name, passed)
        ok = ok and passed
        return code, hdrs, body

    code, headers, body = http.get("/v1/health")
    parsed = json.loads(body) if code == 200 else {}
    passed = code == 200 and parsed.get("service") == "openseek" and \
        headers.get("x-ratelimit-limit") == "30"
    steps.evidence(f"GET /v1/health (no auth) -> {code} {body[:90].decode('utf-8', 'replace')} | "
                   f"{_rate_line(headers)}")
    steps.check("health advertises the documented shape and the health budget", passed)
    ok = ok and passed

    matrix("missing key on a pool route: 401 unauthorized", "GET",
           f"/v1/pool/{probe}/manifest?profile=nvmt-v1-g10",
           status=401, error="unauthorized")
    matrix("malformed key: 400 bad_auth", "GET",
           f"/v1/pool/{probe}/manifest?profile=nvmt-v1-g10",
           headers={"Authorization": "Token not-a-bearer"}, status=400, error="bad_auth")
    matrix("well-formed key that was never issued: 403 forbidden", "GET",
           f"/v1/pool/{probe}/manifest?profile=nvmt-v1-g10",
           headers={"Authorization": f"Bearer {'z' * 43}"}, status=403, error="forbidden")

    oversized = b"\0" * (16 * 1024 * 1024 + 1)
    matrix("missing key on an oversized body: 401 before the body is read", "PUT",
           f"/v1/pool/{probe}/nvmt?profile=nvmt-v1-g10&duration_ms=30000",
           headers={"Content-Type": "application/vnd.openseek.nvmt"}, data=oversized,
           status=401, error="unauthorized")

    matrix("read key cannot write: 403 forbidden", "PUT",
           f"/v1/pool/{probe}/nvmt?profile=nvmt-v1-g10&duration_ms=30000",
           headers={"Authorization": f"Bearer {read_key}",
                    "Content-Type": "application/vnd.openseek.nvmt"},
           data=b"not-an-nvmt-package", status=403, error="forbidden")

    name = "write key can write: duplicate package retry answers 200 duplicate"
    code, headers, body = http.get(f"/v1/pool/{package_fp}/nvmt?profile=nvmt-v1-g10",
                                   headers={"Authorization": f"Bearer {read_key}"})
    package = body if code == 200 else b""
    duration = headers.get("x-openseek-duration-ms") or "30000"
    code, hdrs, body = http.put(
        f"/v1/pool/{package_fp}/nvmt?profile=nvmt-v1-g10&duration_ms={duration}",
        headers={"Authorization": f"Bearer {write_key}",
                 "Content-Type": "application/vnd.openseek.nvmt"}, data=package)
    try:
        duplicate = json.loads(body).get("duplicate") is True
    except (ValueError, AttributeError):
        duplicate = False
    passed = code == 200 and duplicate and bool(package)
    steps.evidence(f"read {len(package)} bytes, PUT same package with write key -> {code} "
                   f"{body[:100].decode('utf-8', 'replace')}")
    steps.check(name, passed)
    ok = ok and passed

    name = "revoked key: 403 revoked_key (real pair, then admin revoke)"
    revoked_code = 0
    revoked_error = None
    with httpx.Client(base_url=admin_base, headers={"X-OpenSeek-Admin": admin_key},
                      timeout=30.0) as admin:
        opened = admin.post("/admin/pair")
        window = opened.json() if opened.status_code == 200 else {}
        start_code, _h, start_body = http.post(
            "/v1/pair/start", headers={"Content-Type": "application/json"}, data=b"{}")
        started = json.loads(start_body) if start_code == 200 else {}
        device_id = token = ""
        if opened.status_code == 200 and started:
            k = derive_key(window["code"], started["pair_id"], started["salt"],
                           started["spki_pin"], started["iterations"])
            claim_code, _h, claim_body = http.post(
                "/v1/pair/claim",
                headers={"Content-Type": "application/json"},
                data=json.dumps({"pair_id": started["pair_id"], "device_name": "QA H6 drill",
                                 "client_proof": client_proof(k, started["pair_id"],
                                                              "QA H6 drill")}).encode())
            claimed = json.loads(claim_body) if claim_code == 200 else {}
            device_id, token = claimed.get("device_id", ""), claimed.get("token", "")
            if device_id:
                revoked = admin.delete(f"/admin/devices/{device_id}")
                if token and revoked.status_code == 204:
                    revoked_code, _h, revoked_body = http.get(
                        f"/v1/pool/{probe}/manifest?profile=nvmt-v1-g10",
                        headers={"Authorization": f"Bearer {token}"})
                    try:
                        revoked_error = json.loads(revoked_body).get("error")
                    except (ValueError, AttributeError):
                        revoked_error = None
    passed = revoked_code == 403 and revoked_error == "revoked_key"
    steps.evidence(f"pair window HTTP {opened.status_code}, start HTTP {start_code}, "
                   f"revoke HTTP {revoked.status_code if device_id else 0}, "
                   f"pool read with revoked token -> {revoked_code}")
    steps.check(name, passed)
    ok = ok and passed

    matrix("undocumented path: documented 404 not_found", "GET", "/v1/never-documented",
           status=404, error="not_found")
    matrix("documented path, unsupported method: 405 method_not_allowed", "POST", "/v1/health",
           status=405, error="method_not_allowed")

    name = "rate limit is enforced under a burst: 429 + Retry-After + advertised budget"
    statuses: list[int] = []
    burst_headers: dict[str, str] = {}
    for _ in range(40):
        code, hdrs, body = http.get("/v1/health")
        statuses.append(code)
        if code == 429:
            burst_headers = hdrs
            break
    passed = (bool(statuses) and 200 in statuses and statuses[-1] == 429 and
              burst_headers.get("retry-after", "").isdigit() and
              burst_headers.get("x-ratelimit-limit") == "30" and
              burst_headers.get("x-ratelimit-remaining") == "0")
    steps.evidence(f"{len(statuses)} rapid GET /v1/health: {statuses[0]}..{statuses[-1]}; "
                   f"429 headers {_rate_line(burst_headers)} retry-after="
                   f"{burst_headers.get('retry-after')}")
    steps.check(name, passed)
    ok = ok and passed
    return ok


def _run_secret_drill(steps, expect_leak: bool) -> bool:
    """Negative drill: a step deliberately plants a known secret value into the
    evidence stream. The runner must redact it and fail the run (exit 2) unless
    --expect-leak was given, in which case catching the leak IS the pass."""
    from secrecy import collect_known_secrets
    known = collect_known_secrets()
    if not known:
        steps.evidence("no secret stores found under ~/.config/openseek/*.env")
        steps.check("secret leak guard had a store to guard", False)
        return False
    steps.evidence(f"drill plants value from qa.env beginning {known[0][:8]}...")
    steps.evidence(known[0])
    steps.check("drill evidence carries a known secret value", True)
    return True          # the runner's guard decides the outcome from here


RECIPES = {
    "contract-host": {
        "title": "API contract surface of the live host node (registered Epic G artefact)",
        "targets": ("live",),
        "ticket": "G4",
        "runner": _run_contract_host,
    },
    "contract-local": {
        "title": "API contract surface on a cold-start local node (live-server harness)",
        "targets": ("local",),
        "ticket": "H1",
        "runner": _run_contract_local,
    },
    "host-protections": {
        "title": "Host API protections: auth matrix, body precedence, read/write, rate limit",
        "targets": ("live",),
        "ticket": "H6",
        "runner": _run_host_protections,
    },
    "secret-leak-drill": {
        "title": "Negative drill: the runner refuses to publish a known secret value",
        "targets": ("drill",),
        "ticket": "P1",
        "runner": _run_secret_drill,
    },
}
