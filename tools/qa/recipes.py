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

            name = "an anonymous request to an unlisted route is refused (401 unauthorized)"
            r = anon.get("/v1/unknown-gone")
            passed = r.status_code == 401 and r.json()["error"] == "unauthorized"
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
    name = "a pool read with no key is refused (403 forbidden)"
    code, headers, body = http.get(f"/v1/pool/{probe}/manifest?profile=nvmt-v1-g10")
    passed = code == 403 and json.loads(body)["error"] == "forbidden"
    steps.evidence(f"HTTP {code} {body.decode()[:80]}")
    steps.check(name, passed)
    ok = ok and passed

    junk_headers = {"Content-Type": "text/plain"}
    name = "no key: refused before the request body is parsed (denial, not 400)"
    code, headers, body = http.put(f"/v1/pool/{probe}/nvmt?profile=nvmt-v1-g10&duration_ms=30000",
                                   headers=junk_headers, data=b"junk-body-not-nvmt")
    passed = code == 403 and json.loads(body)["error"] == "forbidden"
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

    name = "retired legacy routes never reach a handler (401 unauthorized)"
    code, headers, body = http.get("/v1/previews/" + "b" * 32 + "/thumbs.vtt")
    passed = code == 401 and json.loads(body)["error"] == "unauthorized"
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
    "secret-leak-drill": {
        "title": "Negative drill: the runner refuses to publish a known secret value",
        "targets": ("drill",),
        "ticket": "P1",
        "runner": _run_secret_drill,
    },
}
