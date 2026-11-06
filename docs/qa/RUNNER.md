# The QA runner

One entry point for the acceptance campaign (ticket #42, epic #41). It turns a
ticket's contract checks into a runnable recipe and produces evidence that can
be pasted into an issue, without ever printing a secret.

Rules:

- A recipe is black-box: it drives the highest seam a user drives (the pinned
  HTTPS API, the console script, the OSP1 client code) and asserts what a user
  observes — never server internals or fakes of the thing under test.
- Secrets live in `~/.config/openseek/qa.env` (mode 0600, outside this
  repository), per `docs/qa/ARTIFACTS.md`. The runner reads values from there
  and the environment as inputs and never echoes them.
- Nothing the runner prints may be pasted into an issue unless it came from a
  guarded run (the guard is the runner itself; see the secret rule below).
- CI never reads the operator's secret stores: CI runs `pytest` (see
  `.github/workflows/tests.yml`); the runner runs by hand, locally.

## Usage

From the repository root, with the project venv:

```sh
.venv/bin/python tools/qa/run.py list                     # campaign tickets from the tracker
.venv/bin/python tools/qa/run.py run contract-host        # live target: the registered G4 host node
.venv/bin/python tools/qa/run.py run contract-local       # local target: cold-start live-server harness
.venv/bin/python tools/qa/run.py run secret-leak-drill --expect-leak
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | every step passed (and, for the drill with `--expect-leak`, the guard caught the planted leak) |
| 1 | a step failed — diagnose and re-run green before sign-off |
| 2 | the secret guard redacted output (a known secret value appeared), or a usage error |

Output is a per-step PASS/FAIL table plus an `### Evidence` block ready to
paste into the ticket, in the same format used for G2/G4 sign-offs.

## Targets

- `live` — registered reference deployments from `docs/qa/ARTIFACTS.md`. The
  TLS pin is the identity: every connection verifies the SPKI pin from qa.env
  before any request is sent, and a mismatch aborts the run. Never downgrade
  to certificate-blind TLS here.
- `local` — the existing test harness (`backend/tests/helpers.py::LiveServer`):
  a real uvicorn node with self-signed TLS on ephemeral ports and an ephemeral
  data directory, started and stopped around one recipe run. No Telegram, no
  pool, no network beyond loopback.
- `drill` — negative checks on the runner itself (see below).

## The secret rule

The guard is name-aware: qa.env keys whose NAME is secret-shaped (contains
`TOKEN`, `KEY`, `HASH`, `PASSWORD`, `SESSION`, `SECRET`) are treated as secret
values — held in memory, redacted if they ever reach the output, and the run
exits 2. Public identifiers stored by value (base URLs, TLS pins, fingerprints)
are not collected, because printing them is allowed by the registry rules.

The `secret-leak-drill` recipe keeps the guard honest: it plants a real secret
value into the evidence stream.

- plain run: the guard must redact it and exit 2 — a leak in the output blocks
  the run.
- `--expect-leak`: catching the leak IS the pass (exit 0). If the guard does
  not fire, the drill exits 1: a silent guard is a defect, never a pass.

## Seeded recipes

| Recipe | Ticket | Default target | Checks |
|---|---|---|---|
| `contract-host` | G4 | `live` | The published API contract surface of the registered host node: health shape, forbidden reads/writes without a key, body validation only after auth, retired routes gone, NVMT v1 package read byte-for-byte (header validated client-side) with both keys. |
| `contract-local` | H1 | `local` | The same contract on a cold-start local node: health, `pair/start` serving the pin the client independently derived, a full OSP1 pairing round-trip (admin opens the window, the client derives K and claims, proofs verify), unlisted routes refused. |
| `secret-leak-drill` | P1 | `drill` | The guard drill (above). |

## Adding a recipe for a ticket

1. Add `_run_<name>(steps) -> bool` in `tools/qa/recipes.py`. Collect evidence
   with `steps.evidence(...)`, verdicts with `steps.check(name, passed)`; do
   not print directly. Never log a secret value, and never build evidence from
   server internals a user cannot see.
2. Register it in `RECIPES` with its ticket id and allowed targets.
3. Run it green against every listed target, then paste the table + evidence
   into the ticket and add the `qa:agent-pass` label (the maintainer adds
   `qa:user-pass` and closes, as with G2/G4).

What this file must never contain: a secret value. Reference inputs by their
qa.env key name.
