# OpenSeek QA artefacts

The reference deployment built for Epic G, and the inputs Epic H needs.

Rules:

- Public identifier: write the value. A URL, a channel id, a bot username, a project ref.
- Secret: write the location only. Never the value.
- Secrets live in `~/.config/openseek/qa.env` (mode 0600, outside this repository).
- Nothing in this file may be pasted into an issue body until it has been checked against these rules.

## Reference deployment

| Artefact | Value |
|---|---|
| Telegram bot | @openseeekbot (id 9277790259) — token in qa.env, key OPENSEEK_TELEGRAM_BOT_TOKEN |
| Shared pool | "openseek pool", supergroup id -1004391668814 |
| Pool invite | https://t.me/+mzUOB2hd1VczYzRl |
| Publisher account | session file ~/.config/openseek/telegram.session (key OPENSEEK_TELEGRAM_SESSION also stored) |
| Supabase project | qilkfmndsuwmopfoihzu, https://qilkfmndsuwmopfoihzu.supabase.co |
| Supabase org | omhprlsnyrhinrlpvibz |
| Supabase keys | qa.env: SUPABASE_ANON_KEY, SUPABASE_SERVICE_KEY, SUPABASE_DB_PASSWORD |
| Supabase bucket | openseek-staging (private, 16 MiB) |
| Vercel deployment | G7 cli-wizard project openseek-g7-qa — current node URL https://openseek-g7-o7prxwpsf-yatdoescode-8909.vercel.app (via supplemental `RestSetupProvider.deploy` of the existing project — REST only, not a CLI run; the earlier full `openseek.cli` wizard deployments are recorded in "G7 token-only participant node" below, superseding the earlier "not created yet"; the claimed pairing bundle below keeps its original registered URL). The [G6](https://github.com/Yatin-Code/openseek-telegram/issues/49) Deploy-Button reference remains deferred. |
| Vercel team | yatdoescode-8909 (CLI session) |
| Client artefact | not installed yet |
| Retired | broadcast channel -1003859844064, replaced by the supergroup above |
| Extra | organisation "openseek-qa" (knmqhdqymbsmzebmebhv), created while diagnosing a 403; deletable |

## Epic G mode 3 — host-mode node (G4)

Running on the operator machine since 2026-11-07 (redeployed the same day after a host reboot wiped
/tmp and destroyed the first deployment; rebuilt by hand from the runbook steps), separate env/data
dir/ports so it coexists with the channel-mode deployment (which holds 8787/8788). Left running as
the G4 daemon.

| Artefact | Value |
|---|---|
| Env file | /tmp/qa-g4/openseek.env (mode 0600, recreated by hand from documented settings; host pool mode, ports 18787/18788) |
| Data dir | /tmp/qa-g4/data (OPENSEEK_DATA_DIR, mode 0700; recreated with the redeploy) |
| Base URL (LAN) | https://10.228.84.160:18787 (dedicated ports; the channel-mode deployment keeps 8787) |
| Loopback admin | http://127.0.0.1:18788 |
| Admin token | qa.env: OPENSEEK_G4_ADMIN_KEY (recreated with the redeploy) |
| TLS pin | sha256//thP//5QeO5imSIfpTl5TUC/WRRCZFNWiimHgvyVd7xk= (self-signed cert, CN=openseek; new cert with the redeploy) |
| Read key | qa.env: OPENSEEK_G4_READ_KEY (same value as the first deployment, also inside the env file) |
| Write key | qa.env: OPENSEEK_G4_WRITE_KEY (same value as the first deployment, also inside the env file) |
| Pool channel | the shared supergroup from G2, -1004391668814; pool bot @openseeekbot |
| Published package | fp c379a695d15c7874a4709789916423960f1332d255d1df32b802a4e00270808a, nvmt-v1-g10, 26079 bytes, 3 slots, sha256 48beee159223146909bc8978e71d73c5dc3a829fbd81c459ea0ce45b97d43f02 (real ffmpeg frames; hints registered) |
| Working evidence | /tmp/qa-g4/{serve.log, src/} |
| First deployment | wiped 2026-11-07 by a host reboot (old pin sha256//LYLra4F2…, package fp 413bb6ee…, Telegram message 11); the old pin/fp rows are replaced by the values above |

## Published QA package

| Field | Value |
|---|---|
| Source | /tmp/qa-g2/source.mp4 (synthetic, barcode frames, 30 s) |
| Fingerprint | see /tmp/qa-g2/report.json |
| Package | /tmp/qa-g2/package.nvmt, 21121 bytes, 3 slots |
| Telegram messages | 3, 4, 5 (tiles) and 6 (manifest) |
| Verification | all three tiles re-downloaded and sha256-matched; manifest fingerprint matched |

## Channel-mode node (G3)

| Field | Value |
|---|---|
| Base URL | https://127.0.0.1:8787 (loopback, self-signed; pin in the node log) |
| Data directory | /tmp/qa-g3/data |
| Mode | channel, entity megagroup, history readable, writable |
| Paired device | 95b0f1b74973819c (name qa-g3) |
| Admin listener | http://127.0.0.1:8788 |
| Indexed pool | 3 tiles, 1 manifest, last message id 6 |
| Served package | fp 413bb6ee018f2dcc751ecd26ae3f3a50469dc76b486fba74f448b567ed6e3d3d |
| Served bytes | 21121, sha256 b0eb6a7dfd4d7ccf602e3ff72700774ec4fea53cb7fbe8048871a048288f33d0 (identical to the built package) |
| Removed | the librarian second contribution (messages 7-10) after it exposed the manifest defect below |

## Supabase project (G5)

| Field | Value |
|---|---|
| Project | qilkfmndsuwmopfoihzu, https://qilkfmndsuwmopfoihzu.supabase.co |
| Schema | 11 base tables + 1 view (pool_packages) — historical snapshot at G5 time, superseded by the current deployed 12-public-table catalog (see the supplemental verification inventory below); not a current claim |
| Copy path | schema.sql applied twice over Postgres, first attempt clean |
| Migration path | 5 migrations; table set identical to schema.sql (test_schema_parity.py) |
| Bucket | openseek-staging, private, file_size_limit 16777216 |
| Privacy proof | anonymous public read 400; signed URL 200; anon select returns [] under RLS |
| Oversize | a 16777217-byte upload is refused (EntityTooLarge) |
| Keys | qa.env: SUPABASE_ANON_KEY, SUPABASE_SERVICE_KEY, SUPABASE_DB_PASSWORD |


## Host-mode node (G4)

| Field | Value |
|---|---|
| Base URL | https://127.0.0.1:18787 (loopback, self-signed) |
| Admin listener | http://127.0.0.1:18788 |
| Data directory | /tmp/qa-g4/data |
| Configuration | /tmp/qa-g4/openseek.env (mode 0600; rebuilt by hand from the documented settings after the 2026-11-07 /tmp wipe) |
| Keys | in that file: OPENSEEK_POOL_READ_KEY, OPENSEEK_POOL_WRITE_KEY |
| Package | fp c379a695…, 26079 bytes, 3 slots, sha256 48beee15…f43f02 (redeploy 2026-11-07) |
| Package storage | local file under <data>/packages, one file per fp and profile (c379a695….nvmt-v1-g10.nvmt on disk) |
| Channel | tiles and hints only; the two pre-refactor package documents (messages 11, 16) were removed |
| Note | the node was started at 01:21 by an earlier session; this run verified it independently |

## Local-only node (G1)

| Field | Value |
|---|---|
| Base URL | https://127.0.0.1:21787 (loopback, self-signed; pin sha256//TMESKUQ3mq4Afr0l6F/K7xzchdHQe8RvX8Lf2Vf8g14=) |
| LAN address | Not exposed; local-only default binds 127.0.0.1. An explicit private LAN bind is allowed; a public address is refused. |
| Data directory | /tmp/qa-g1/data |
| Mode | local-only (no pool, no Telegram, no Supabase) |
| Admin listener | http://127.0.0.1:21788 |
| Admin secret | /tmp/qa-g1/data/admin.token (file location only; never recorded by value) |
| Paired device | b87d7d2633e8efaa (name qa-g1-reference; retained for follow-on acceptance) |
| Revocation proof | Device 14f0d86560ac4eb2 was removed; its token receives HTTP 403 `revoked_key`. |
| Install | fresh README install in `/tmp/qa-g1/fresh/backend`, using `requirements.txt` and `requirements-lock.txt` |
| Evidence | `/tmp/qa-g1/evidence/`: `pip-install.log`, `final-doctor.json`, `dashboard-proof-final.json`, `pair-revoke-proof-final.json`, `bind-proof-final.json`, `serve-final.log` |
| Verified | 2026-11-08 on commit 4e17fd3 plus the dashboard render fix in the working tree |
| Defects | #69, #70, and #71 are fixed; dashboard generation jobs are rendered before the dashboard markup is inserted. |

The local-only daemon remains running for follow-on acceptance tickets. Its paired reference device is `b87d7d2633e8efaa`.

Working-tree recheck (2026-11-08): `/tmp/opencode/g1-completion/current-proof.json` supplements the saved install evidence with a fresh isolated CLI start, doctor, browser rendering/secret handling, OSP1 pairing/revocation, and bind refusal on the current uncommitted code. This isolated check was stopped after verification; the reference deployment above retains its saved install. Published evidence: [G1 final agent evidence](https://github.com/Yatin-Code/openseek-telegram/issues/43#issuecomment-6713379988).

## G7 token-only participant node (#50)

Driven by the real `openseek.cli.main` entry with `setup --provider rest --state <absolute durable path>` inside the live-CLI isolation harness: the worker's HOME is isolated to /tmp/opencode/g7-completion/home and its PATH is swapped to a fresh empty directory, with the official `vercel` and `supabase` CLIs verified absent from the child path. Provider account tokens travel memory-to-memory only (`OPENSEEK_VERCEL_TOKEN` / `OPENSEEK_SUPABASE_ACCESS_TOKEN`); no secret value is recorded here or echoed by the run.

| Artefact | Value |
|---|---|
| Supabase | reused, no project created: qilkfmndsuwmopfoihzu |
| Vercel project | openseek-g7-qa (id prj_zDRHM2XRyrYmrbQNwpzdcOXCUg9K) — the only Vercel project created under the wizard policy |
| Current deployment | https://openseek-g7-o7prxwpsf-yatdoescode-8909.vercel.app — the live node after the supplemental repair redeploy passed: `RestSetupProvider.deploy` of the existing authorized project openseek-g7-qa (REST path only, not a CLI run — no new invite, no additional CLI claim). session-redeploy.json: health 200 `api` integer 1 version "0.2.0"; the same 12 production env key names with no OPENSEEK_SETUP_KEY; the original owner/session/device-token/bundle files unchanged; the participant state updated URL-only at 0600. The earlier full-wizard deployments (actual CLI `--new-pairing` runs, latest with the explicit device-access consent; among them dpl_33vEkhch9cDHj1T2x6NkavURmXx6) are superseded as deployments |
| Public health | Corrected, contract-aware check on the live node, re-passed on the completed repair deployment (session-redeploy.json): GET /v1/health HTTP 200, service "openseek", `"api"` integer 1 (canonical `"api":1` per [CONTRACT-v1](../api/CONTRACT-v1.md) line 73, not the pending-stub `"v1"` string that the first scratch verifier wrongly expected — a verifier bug since corrected; the pending-stub shape is historical), version "0.2.0". Production-SSO is preview-only; OpenSeek owner/device authentication is retained. Package reads now pass on the repaired node (see the node-proof re-check row below): the historical ENAMETOOLONG blocker is fixed and the fix is verified independently by the full suites on real Python 3.12 and on 3.14 (537 passed each) |
| Participant state | ~/.config/openseek/g7-participant/participant-setup.json (mode 0600), step `paired`; the repair redeploy updated only its recorded deployment URL (0600 preserved) |
| Owner admin secret | ~/.config/openseek/g7-participant/participant-setup.admin-token (mode 0600; location only) |
| Device token | ~/.config/openseek/g7-participant/device-token.txt (mode 0600, persisted through the no-clobber hard-link path; location only) — claimed device qa-g7-reference (7fcafd65f5c04884), role guest, `contribute` false |
| Telegram secret | ~/.config/openseek/g7-participant/telegram-session.txt (mode 0600; independent of the qa.env registered session; deployed once into the node env and never reconnected locally; location only) |
| Pairing bundle | replacement invite registered in full bundle shape `https://openseek-g7-ix719gxe9-yatdoescode-8909.vercel.app|[REDACTED]` (public URL, code redacted); the code itself is guarded only at ~/.config/openseek/g7-participant/latest-pairing.txt (mode 0600). The consented replacement invite was claimed by qa-g7-reference (see the node-proof row). The two earlier invites remain unclaimed and unrevoked: the first was never attempted at all, and the second's only documented node interaction is the pre-consent POST /v1/pair/start 409 `pairing_closed` — no actual claim was sent (with sharing off, no claim would have been accepted at that moment); the later consent flipped node sharing on, but no usability claim is made here for either |

Run history (harness JSON reports under /tmp/opencode/g7-completion/logs/):

| Phase | Evidence |
|---|---|
| Interrupt | g7-run-interrupt-20261108T133510Z.json — the controlled gate `SetupFailure('G7 controlled interruption', 'qa_interruption')` raised immediately before the first Vercel list call of the bootstrap (the gate was armed by the QA harness — intentional controlled interruption, not a wizard fault); CLI exit 1; persisted checkpoint step `supabase_ready` |
| Resume 1 | g7-run-resume-20261108T133551Z.json — skipped SQL/bucket, set 7 Production env vars, created the project, but the deployment stayed unused_function |
| Resume 2 | g7-run-resume-20261108T134227Z.json — skipped SQL/bucket/env and every further resource creation; deployed; public health 302 (Vercel SSO); state step `deployed` |
| Resume 3 (health) | g7-run-resume-20261108T135354Z.json — exit 0; deployed → healthy; no rebuild (resources and env untouched) |
| Telegram | g7-run-telegram-20261108T135936Z.json — exit 0; healthy → paired; production `TelegramAccountSetup` resolved the existing membership via the validated invite, then history-read, send-media, and supergroup checks passed with independent authorization; wrote the node env, deployed the first node, created the invitation, removed the setup env, and redeployed — all real production code; the first bundle was intentionally left unclaimed |
| Replace 1 | g7-run-replace-20261108T140257Z.json — actual CLI `--new-pairing`; exit 0; paired → paired; `code_changed=true`; the old invite was not revoked and remains intentionally unclaimed — no claim attempt was ever made against it; no leaks were flagged; state, bundle, and admin files all 0600; `pairing_setup_key` absent from the final state; the then-current deployment URL captured by that CLI run is superseded by the REST repair redeploy — the claimed bundle keeps its consent-run URL below |
| Replace 2 (consent) | g7-run-replace-20261108T144801Z.json — actual CLI `--new-pairing` after the consent repair: CLI exit 0 and harness verdict 0; paired → paired; the dialogue recorded `nuvio_guest_device_access:consented` and `enable_sharing: true` was forwarded into the real POST /v1/setup/pairing (this consent records only the node sharing switch — the node's pool-contribution setting and the Nuvio-device opt-in remain separate choices); `code_changed=true` against the pre-consent invite; the two earlier invites were left unclaimed and unrevoked; no leaks flagged; state, bundle and admin files 0600; `pairing_setup_key` absent from the final state and the live Vercel env removal succeeded; that CLI run's then-current deployment URL (whose URL the claimed bundle carries, preserved unchanged in the pairing-bundle row) is likewise now superseded by the REST repair redeploy |
| Node proof (pair/start, historical) | node-proof-pair-start-red.json + pair-start-diagnosis.json — the pre-consent red, and the second invite's only attempted step (the first invite was never attempted): POST /v1/pair/start returned 409 `pairing_closed`; no actual claim was sent and no device token was created; admin GET with the correct `X-OpenSeek-Admin` header showed enabled=false and contribution=false; the matching Supabase invitation row (type `guest`) was unused with 0 attempts, not expired, not revoked. Diagnosis: the wizard had created the guest while node sharing was off, so the claim was unavailable at that moment. This stands as the recorded reason for the consent repair; the audit itself is superseded by the consented replace and the claim/security-pass node-proof row below |
| Node basic check (historical) | node-basic-proof.json — GET /v1/health HTTP 200 (service "openseek", api integer 1 per CONTRACT-v1, version "0.2.0", pairing_open false); unauthenticated POST and GET on the setup endpoints each returned 404 `not_found`; the live Vercel production env list showed 12 key names with no OPENSEEK_SETUP_KEY and the setup-performed marker true. The first scratch verifier had expected the old "v1" string, so only its aggregate flag was false — that verifier bug is corrected, and the corrected health-contract check is recorded (already correct there) in the claim/security-pass node-proof row below; no aggregate pass is claimed for this release of the artifact |
| Node proof (claim/security pass, package red) | node-proof-claim-package-red.json — the preserved original claim-success + package-failure report, immutable at mode 0600, and this row's evidence (node-proof.json itself was rerun with the existing device token — no new invite or re-claim — and overwritten; the original claim snapshot is kept separately at node-proof-claim-package-red.json). Run against the consented deployment; scrubbed, no leaks: **device claim passed** — OSP1 invite claim: start 200, claim 200, status 200, 0 retries, client proof shape/salt/pair-id/SPKI shapes ok, server proof verified (PBKDF2, 600000 iterations), token saved 0600 at ~/.config/openseek/g7-participant/device-token.txt through the no-clobber hard-link path; device 7fcafd65f5c04884 name qa-g7-reference role guest; authenticated /v1/pair/status 200 with `contribute` false. **Security checks passed** — health 200 (`api` integer 1, version "0.2.0"); unauthenticated setup POST and GET each 404 `not_found`; live Vercel production env 12 key names with no OPENSEEK_SETUP_KEY and setup-performed true. **Package check failed** — GET /v1/pool/c379a695…/nvmt returned 503 `pool_index_unavailable` after the 2 bounded retries (~34.24 s elapsed); no bytes, sha256 or header fields returned. **Diagnosis (confirmed)** — Vercel's Python 3.12.13: `pathlib.Path.exists()` on the unchanged 353-character serialized Telethon StringSession (a bare name longer than NAME_MAX 255) raises OSError errno 36 (ENAMETOOLONG); the production path-helper probe propagated it and `_pool_error` translated it to 503 "telegram_unavailable", while the local 3.14 interpreter masks it (exists() returns False there). The parent-reviewed narrow fix is test-verified end to end — 537 passed on 3.14 and, independently, 537 on the real Python 3.12 runtime (pinned deps + pytest extras incl. psutil), plus the real-3.12 helper RED→GREEN proof — and was deployed by the supplemental `RestSetupProvider.deploy` repair (no invite created, no CLI claim). The re-check in the row below passed, so this row stands as the preserved red history only; its own `executed_checks_passed` false is historical, and no second invite-claim was made (the existing retained device token was reused) |
| Node proof (re-check, package pass) | node-proof.json — the same completion rerun with the EXISTING retained device token (`mode` `existing_token`, `reused_existing_token`, `replacement_invite_needed` false — no second invite-claim and no new invite; scrubbed, `fatal` null, no leaks, mode 0600) against the repair deployment https://openseek-g7-o7prxwpsf-yatdoescode-8909.vercel.app: /v1/pair/status 200 credential ok with `contribute` false, device 7fcafd65f5c04884 name qa-g7-reference; health 200 `api` integer 1 version "0.2.0"; unauthenticated setup POST and GET each 404 `not_found`; live Vercel production env 12 key names, no OPENSEEK_SETUP_KEY, setup-performed true. **Package passed** — GET /v1/pool/c379a695…/nvmt?profile=nvmt-v1-g10&duration_ms=30000 returned HTTP 200 with exactly 26079 bytes and sha256 `48beee159223146909bc8978e71d73c5dc3a829fbd81c459ea0ce45b97d43f02` equal to the expected value; the response header fields matched (profile nvmt-v1-g10, duration 30000, 3 tiles, header sha256 equal), and the separate decoded check verified grid 10000 ms, 3 slots, 3 tiles, JPEG tiles and pts-bounds ok via the real `openseek.pool.decode_nvmt`. Caution recorded: the package 200 came after 2 bounded retries (transient retryable states under the bounded retry policy, Retry-After honored ≤5 s) and the entire proof elapsed 87.11 s — no first-request, no-retry, or latency guarantee is claimed. Aggregate `executed_checks_passed` true; `acceptance_complete` false |
| Supplemental package identity | The served package is byte-identical to the host G4 record in "Reference deployment": same 26079 bytes, same sha256 `48beee15…f43f02`, profile nvmt-v1-g10, 3 slots, 3 tiles. This is supplemental host-package identity evidence only — explicitly NOT a G6 Deploy-Button equivalence claim (no G6 reference node exists; the artifact's `deploy_button_comparison` remains `blocked_no_reference_node`) |
| Aggregate | executed_checks_passed=true (node-proof.json re-check, package pass); acceptance_complete=false; blocked_flagged=true — G7 is still not complete: maintainer sign-off is pending, the Deploy-Button [G6](https://github.com/Yatin-Code/openseek-telegram/issues/49) comparison remains deferred with no reference node, and no overall agent pass is claimed — [issue #50](https://github.com/Yatin-Code/openseek-telegram/issues/50) stays open (qa:blocked) with the maintainer comparison left unchecked |

- The wizard persistently applied the exact current SQL at the interrupt checkpoint (unlike the earlier rollback-only double-apply verification path).
- Deployed privilege recheck passed live (2026-11-08): the parent-reviewed corrected schema verifier passed `--selftest` and then the live pass, evidence `supabase-after.json` (schema hash `c288…`, same as the current source). Catalog: 12 public tables including `pool_revoked_tokens`, RLS enabled on all with no policies; the pair view is `security_invoker=true` with anon/authenticated SELECT granted false and service_role true. PostgREST: anon view 401 (42501) vs service 200; anon base-table SELECT 200 with zero rows; service 200 count 0. The two open guest-wizard invitation rows were counted and recorded, not asserted unchanged. Probe activity: 6 SELECTs + 1 project GET + 4 REST GETs, no mutations; report written 0600, no leaks. Published as the [#77 correction](https://github.com/Yatin-Code/openseek-telegram/issues/77#issuecomment-6722552045) [historical]; [#77 CLOSED](https://github.com/Yatin-Code/openseek-telegram/issues/77#issuecomment-6730667474) after real deployed serverless revocation (see evidence bullet below).
- Serverless revocation [#77 CLOSED](https://github.com/Yatin-Code/openseek-telegram/issues/77#issuecomment-6730667474): existing G7 deployment unchanged. Disposable owner device `fe8a382866c48dd4` via production OSP1 over HTTPS (verified CA/empty pin): status 200, DELETE 204, next fresh HTTPS status 403 `revoked_key` twice; retained reference `7fcafd65f5c04884` still 200, public health/owner status 200. Own invite `87c69199b171b8d3f97be34bb1fe4449` revoked 204; protected files hashes/modes unchanged, paired state/bundle intact. Durable report `~/.local/state/openseek/qa-agent-campaign/revocation/report.json`. Prior attempt stopped before pair/start/claim due verifier nonempty-pin assumption; own invite cleaned, no device created (archived `attempt-1-cleaned-pin-assumption`). Parent production-route offline roundtrip + 11 selftest cases passed.
- Current repository full test run: 546 passed on 3.14.4 (63.83 s) and 546 passed on 3.12.13 (66.14 s), after setup-guard/atomic-librarian/generation-slot fixes — confirmed independently by the parent on both runtimes; durable `~/.local/state/openseek/qa-agent-campaign/final-backend-suites.json` (historical 537-passed statements for the old G7 deploy below are unchanged).
- The [G6](https://github.com/Yatin-Code/openseek-telegram/issues/49) Deploy-Button reference remains deferred, so no G6-vs-G7 comparison is made (left unchecked). The consented replace run plus the node proofs establish a real CLI 0, a consent-forwarded claimable invite, a verified guest device claim, and — after the historical ENAMETOOLONG package blocker was diagnosed, fixed, independently test-verified (537 passed on real Python 3.12 and 537 on 3.14), and deployed by the supplemental REST repair — a passing live package read (see the re-check row: HTTP 200, identical bytes/sha256 to the host package, after 2 bounded retries; no latency claim). Pairing-to-package completion is therefore proven end-to-end on the repaired node, while acceptance stays incomplete: maintainer sign-off is pending, [issue #50](https://github.com/Yatin-Code/openseek-telegram/issues/50) remains open (qa:blocked), and no overall agent pass is claimed. The final evidence comment is published at [issues/50#issuecomment-6723275458](https://github.com/Yatin-Code/openseek-telegram/issues/50#issuecomment-6723275458); an earlier milestone was published at [issues/50#issuecomment-6722379802](https://github.com/Yatin-Code/openseek-telegram/issues/50#issuecomment-6722379802), and the claim blocker above was discovered after it.
- [QA-MAP.md](../QA-MAP.md) is generated from GitHub after the final evidence comment ([#50 evidence](https://github.com/Yatin-Code/openseek-telegram/issues/50#issuecomment-6723275458)); see QA-MAP for the current status. The G6/maintainer comparison remains unchecked until an actual [G6](https://github.com/Yatin-Code/openseek-telegram/issues/49) reference node and acceptance exist.
- Never paste the admin-token or telegram-session file values anywhere (location only, per the rules at the top).

## Supplemental verification inventory (2026-11-08)

| Item | Current evidence / state |
|---|---|
| Released TPK | SHA-256 `679482fd7a98f7fd955d2c4f7be02275f12831349195fa6a6c0271ea51156eff`; workflow [42114362897](https://github.com/Yatin-Code/nuvio-native-legacy/actions/runs/42114362897), commit `14d43b09bae0ca076c434aa7a49230413ebae0b2` ([G8](https://github.com/Yatin-Code/openseek-telegram/issues/46)) |
| Connected client target | No connected Tizen target currently ([G8](https://github.com/Yatin-Code/openseek-telegram/issues/46)) |
| Vercel | One project exists: G7 cli-wizard openseek-g7-qa (id prj_zDRHM2XRyrYmrbQNwpzdcOXCUg9K); the current deployment URL is recorded in "G7 token-only participant node". [G6](https://github.com/Yatin-Code/openseek-telegram/issues/49) still has no Deploy-Button reference (deferred), [H8](https://github.com/Yatin-Code/openseek-telegram/issues/59) |
| Supabase deployed schema | Current deployed catalog is 12 public tables including `pool_revoked_tokens` — verified live (full details in the G7 section bullets; [#77](https://github.com/Yatin-Code/openseek-telegram/issues/77) published correction). Historical G5 snapshot kept in the H7 bullet below: 11 tables, no `pool_revoked_tokens`, package-view permissions gap tracked in [#77](https://github.com/Yatin-Code/openseek-telegram/issues/77) |
| Host-live generated package ([#78](https://github.com/Yatin-Code/openseek-telegram/issues/78#issuecomment-6730803385) CLOSED) | New generated artifact, not a replacement for the G4 reference: 12 s g10, 2 populated slots, 11762 bytes, SHA256 `3d3cd4cfc30bddf4ae07abb161a36334fd075108e816dede9cc5ba5a08b6dc26`, fp `3b08a274435b8e2bbfccd195bea312798f5795c3167fdc24a71c46bdb0527fd9`; Telegram tiles 66/67 + manifest 68, source 65 deleted/absent; durable `~/.local/state/openseek/qa-agent-campaign/host-live/runs/20261109T022617Z-84a68aadc4/` — not an H2 whole-browser/Tizen acceptance claim and not a G7 redeploy |
| Evidence | The former ephemeral folder `/tmp/opencode/remaining-gh-verification/` was wiped along with /tmp; the published issue comments retain the findings. The new scratch root is `/tmp/opencode/g7-completion/` (also ephemeral, not a retained deployment). |

### Follow-up verification (2026-11-08)

- **G7 provider preflight ([#50](https://github.com/Yatin-Code/openseek-telegram/issues/50)): historical, superseded by the real CLI success (a then-gap in account access, not a current block status):** the in-process `run_setup()` probe (CLI parser/main not invoked) reached named Supabase sign-in, returned `no_accessible_organization`, and exited 1; management `select 1` returned HTTP 403; the organization list returned HTTP 200 with zero rows; the project list did not include the registered project; no state path was created and no pairing bundle was produced. The real CLI path since succeeded (see the G7 sections above). Evidence: `g7-preflight.json`, `g7-provider-shape-parent.json`.
- **H7 storage boundary ([#58](https://github.com/Yatin-Code/openseek-telegram/issues/58)):** unique raw staging probes passed cleanup; the exact 16 MiB object uploaded and signed-read byte-for-byte, the 1-byte-over-limit upload was rejected, and the probe prefix returned to zero objects. This covers raw Storage only, not NVMT publication or global orphan cleanup. Evidence: `h7-storage-parent.json`.
- **H7 schema and deployed permissions ([#58](https://github.com/Yatin-Code/openseek-telegram/issues/58), [#77](https://github.com/Yatin-Code/openseek-telegram/issues/77)): historical, superseded by the deployed privilege recheck, which passed live (12 public tables including `pool_revoked_tokens`; see the G7 section):** the current-source schema passed a rollback-only double apply with counts unchanged, rollback complete, and anon/authenticated denied by the hardened view privileges. The deployed project was old at that time, lacked `pool_revoked_tokens`, and its view canary returned one view row to anon and authenticated while the base canary returned zero; the deployed view then still bypassed RLS. This supersedes the earlier G5 inference that an empty anonymous select proved privacy. Evidence: `h7-schema-twice-parent.json`, `h7-view-canary.json`.
- **Invitation/revocation recheck ([#75](https://github.com/Yatin-Code/openseek-telegram/issues/75)):** independent 390×844 invitation layout passed with no controls outside the viewport; invite text and synthetic long text fit. Create returned HTTP 200, revoke returned HTTP 204, revoked state appeared, and the session remained intact. Evidence: `h2-parent-invitation-final.json`.
- **H8 lifecycle read phases ([#59](https://github.com/Yatin-Code/openseek-telegram/issues/59#issuecomment-6730693699)) — remains OPEN, partial:** full 600 s idle, exactly one GET health 200 in 3794.078 ms, zero warmup/retry, no agent node requests overlapping. No documented numeric budget, so budget-dependent criterion unchecked. Logs on authorized pinned project/deployment: events 200, 20 build/deployment events (18 stdout, 2 stderr), 380 string fields, known+structural secret scan clean; no runtime-log claim. Current deployment ID `dpl_DX4vX52tDr6Enahm2BTchYnNPY8r`; OAuth CLI refresh fixed initial 403. Only logs criterion checked; env-change/redeploy/rollback unexecuted. Durable `vercel-lifecycle/report-h8-{idle,logs}.json` and `log-excerpt.json`; safe excerpt: build machine 2 cores/8 GB, upload build cache 69.88 MB.
- **#76 source partial ([#76](https://github.com/Yatin-Code/openseek-telegram/issues/76#issuecomment-6730013369)) — remains OPEN:** 37 values per ES/FR/JA (111 total) changed in client 3 tables; 6418 entries × 29 languages table/glyph check passes; compiled native runtime lookup `idioma` ok (official SDL2/Khronos headers in scratch); pairing/upload-conflict offline pass. Four remaining OpenSeek unkeyed source literals and on-screen Tizen/released artifact check pending; #76/#61 stay open. Durable `client76-current/report.json`.
- **Host-live ([#78](https://github.com/Yatin-Code/openseek-telegram/issues/78#issuecomment-6730803385) CLOSED):** frozen current-source CLI host with FFmpeg/PyAV + BotAPI: owner POST 202 → complete; public NVMT/manifest/2 tiles 200; IMDB + season/episode and filename candidates match. Package: 12 s g10, 2 populated slots, 11762 bytes, SHA256 `3d3cd4cfc30bddf4ae07abb161a36334fd075108e816dede9cc5ba5a08b6dc26`, fp `3b08a274435b8e2bbfccd195bea312798f5795c3167fdc24a71c46bdb0527fd9`. Independent read-only Telegram re-download: tiles 66/67 hashes match, canonical manifest 68 verified; source 65 deleted and absent; node stopped; generated assets retained. Secondary slot bug fixed: k1 frame PTS 5000 was wrongly rounded to slot 0 and dropped; scheduler-key slots now preserve it, final live tile 1 PTS 5000 retained; g5 remains sparse every-other slot. Durable `~/.local/state/openseek/qa-agent-campaign/host-live/runs/20261109T022617Z-84a68aadc4/{report.json,telegram-readback.json,source-snapshot.json}`. This does not imply H2 whole-browser/Tizen acceptance or a G7 redeploy.
- **Campaign map and gate:** [QA-MAP.md](../QA-MAP.md) is generated by `python tools/qa/map.py`. The affected slice must be green; campaign acceptance requires both agent and maintainer sign-off. The map records status and comment links but does not manufacture signatures.
