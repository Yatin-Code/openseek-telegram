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
| Vercel deployment | not created yet |
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
| Schema | 11 base tables + 1 view (pool_packages); RLS enabled with no policies |
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
| Base URL | https://127.0.0.1:21787 (loopback, self-signed; pin sha256//ioQfpXg4OqYr18ImdJfMWzWc/928yroGWDEg3Br+Bhs=) |
| LAN address | https://10.228.84.160:21787 (the daemon bound 0.0.0.0, as documented; see defect #71) |
| Data directory | /tmp/qa-g1/data |
| Mode | local-only (no pool, no Telegram, no Supabase) |
| Admin listener | http://127.0.0.1:21788 |
| Admin secret | /tmp/qa-g1/data/admin.token (file location only; never recorded by value) |
| Paired device | 3deacc7d2aee0ee6 (name qa-g1-living-room) |
| Install | fresh venv /tmp/qa-g1/venv from backend/requirements.txt + backend/requirements-lock.txt |
| Evidence | /tmp/qa-g1/evidence/ |
| Defects | #69 revoke HTTP 500 leaves this device paired; #70 local-only dashboard shows the Telegram setup form; #71 non-loopback bind not refused |

The device could not be revoked: `python -m openseek revoke 3deacc7d2aee0ee6` returned HTTP 500
(`AttributeError: 'JobManager' object has no attribute 'revoke_device'`, defect #69). The registry records the id
because the pairing is still live.

