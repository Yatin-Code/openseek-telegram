# OpenSeek API contract v1

The HTTP API for pairing and the shared NVMT pool. See `docs/SECURITY-MODEL.md` for security properties.

## Transport
- One HTTPS listener (default `0.0.0.0:8787`, LAN). Self-signed ECDSA P-256 cert generated on first start;
  pin = `sha256//` + base64(SHA-256(SPKI DER)). No plain-HTTP API.
- Local/VPS admin UI/CLI: `http://127.0.0.1:8788` (loopback only) shows the pairing code, server address and pin
  fingerprint, paired devices (revoke), cache stats. Vercel serves the same dashboard over HTTPS; `/admin/*`
  operations require `OPENSEEK_ADMIN_TOKEN` in `X-OpenSeek-Admin`, and clients use the public edge pin from
  `OPENSEEK_PUBLIC_TLS_PIN`. CLI: `openseek pair`, `openseek devices`, `openseek revoke <id>`.
- Owner-only dashboard NVMT generation: `POST /admin/generations` accepts a media URL and optional catalog hints,
  returns a generation ID, and publishes a validated NVMT package to Telegram. Poll `GET /admin/generations/{id}`
  for progress; list recent jobs at `GET /admin/generations`. URLs are held in memory only. Generation starts only
  from this dashboard action, never from Nuvio playback or other pool requests.
- JSON bodies ≤ 16 KiB, `Content-Type: application/json`; shared NVMT packages are binary and capped at 16 MiB.
  Errors: `{"error": "<code>"}` — stable snake_case
  codes, never echoing input.
- Auth: `Authorization: Bearer <credential>`. A paired-device token is 43 base64url characters; a shared-pool
  read/write key is deployment-defined (at least 24 characters). Missing, malformed, and revoked credentials are
  distinct refusals; see “Authentication and refusals” below.
- Optional shared-pool PUTs use raw `image/jpeg` bodies up to 256 KiB, not JSON.
- Rate limits are advertised on every response and enforced per client address; see “Rate limits” below.

## Authentication and refusals

The credential is checked before any request body is read. An empty header is a missing credential; a non-Bearer
scheme, an empty credential, embedded whitespace, or a credential that matches neither a configured pool key nor the
device-token format is malformed.

| Case | Device routes (`/v1/pair/...`) | Pool routes (`/v1/pool/...`) |
|---|---|---|
| Missing credential | `401 {"error":"unauthorized"}` | `401 {"error":"unauthorized"}` |
| Malformed credential | `400 {"error":"bad_auth"}` | `400 {"error":"bad_auth"}` |
| Well-formed, never issued | `401 {"error":"unauthorized"}` | `403 {"error":"forbidden"}` |
| Revoked device token | `403 {"error":"revoked_key"}` | `403 {"error":"revoked_key"}` |
| Read key on a write | — | `403 {"error":"forbidden"}` |

- Auth runs before body parsing: a missing, malformed, or revoked credential on an oversized body receives the auth
  refusal, never `413 {"error":"too_large"}`. An authorized oversized body still receives the documented boundary
  refusal.
- On the public listener, `/v1/health` and the pairing routes are anonymous, and anonymous pool reads are allowed
  under the `pool_read` budget; owner tokens are refused `403 {"error":"forbidden_role"}`.
- Only documented routes answer. An unknown path returns `404 {"error":"not_found"}`; a documented path with an
  unsupported method returns `405 {"error":"method_not_allowed"}`.
- `403 {"error":"read_only_channel"}` remains a write-permission refusal on a channel node after a valid credential.

## Rate limits

Budgets are per client address (the TCP peer; forwarding headers are never consulted) over a sliding one-minute
window, split by route class:

| Class | Routes | Requests/minute |
|---|---|---:|
| `health` | `/v1/health` | 30 |
| `pair` | `/v1/pair/start`, `/v1/pair/claim` | 12 |
| `api` | all other documented routes | 300 |
| `pool_read` | pool GET routes | 900 |
| `pool_write` | pool PUT/POST/DELETE routes | 120 |

Every response that passes the limiter carries `X-RateLimit-Limit`, `X-RateLimit-Remaining` (requests left in the
current window), and `X-RateLimit-Reset` (seconds until at least one request leaves the window). Over budget:

`429 {"error":"rate_limited"}` with `Retry-After` and the three `X-RateLimit-*` headers.

The host/LAN listener uses these budgets with the same advertised headers and no strikes or blocks. The `vercel`
profile does not apply the limiter: there the TCP peer is the platform proxy and the platform layer fronts the
deployment. The optional public listener uses the same budgets plus per-address strikes and temporary blocks; a
blocked address receives `429 {"error":"rate_limited"}` with `Retry-After: 900`.

## Endpoints
### `GET /v1/health` (no auth)
`200 {"service":"openseek","api":1,"version":"x.y.z","ffmpeg":true|false,"pairing_open":bool}`

### Pairing (OSP1, SR-1/SR-2; vectors in `backend/tests/vectors/osp1.json`)
`POST /v1/pair/start` (no auth; TLS not yet pinned) body `{}` →
`200 {"pair_id":"<32 hex>","salt":"<32 hex>","spki_pin":"sha256//…","iterations":600000}` |
`409 {"error":"pairing_closed"}` | `429 {"error":"rate_limited"}`.

`POST /v1/pair/claim` (no auth; client MUST pin `spki_pin`) body
`{"pair_id":"…","device_name":"Living room TV","client_proof":"<64 hex>"}` →
`200 {"device_id":"<16 hex>","token":"…","server_proof":"<64 hex>"}` | `403 {"error":"pairing_failed"}`
(also on expiry/wrong code/burned window; no distinction).

Derivation (all strings UTF-8, `‖` = concatenation, hex = lowercase):
```
code_norm = uppercase(code) with spaces/dashes removed
K            = PBKDF2-HMAC-SHA256(code_norm, "OSP1" ‖ pair_id ‖ salt ‖ spki_pin, iterations, 32 bytes)
client_proof = hex(HMAC-SHA256(K, "client" ‖ pair_id ‖ device_name))
server_proof = hex(HMAC-SHA256(K, "server" ‖ pair_id ‖ hex(SHA-256(token))))
```
Client refuses `iterations < 200000`. Code: 8 symbols from `23456789ABCDEFGHJKMNPQRSTUVWXYZ` shown as `XXXX-XXXX`.

`GET /v1/pair/status` (Bearer) → `200 {"device_id":"…","device_name":"…"}`. `DELETE /v1/pair` (Bearer) → 204 (self-revoke).

### Shared pool API

Pool endpoints are available only on a pool host configured with `OPENSEEK_POOL_MODE=host` and Telegram host
credentials, or with the legacy `OPENSEEK_POOL_ENABLED=1` setting. An unset mode is local-only. A `channel` node
serves the read endpoints from its local Telegram index and needs no central host; writes are accepted only when
the node has post rights. OpenSeek-to-OpenSeek sync may use the separate read
and write bearer keys (`OPENSEEK_POOL_READ_KEY`, `OPENSEEK_POOL_WRITE_KEY`). Paired app clients use
their revocable device token for reads; a guest can write only when its invitation grants contribution and the
host's contribution switch is on. The public listener may serve anonymous reads under its per-IP `pool_read`
budget. Never put Telegram credentials or the shared pool write key in an app build.

`GET /v1/pool/{fp}/manifest?profile=t320x180q75g10v1` (read key) returns only exact-file tile metadata:

```json
{"fp":"<64 lowercase hex>","profile":"t320x180q75g10v1","duration_ms":7234000,
 "n_tiles":724,"tiles":[{"k":0,"pts_ms":0,"size":4921,"sha256":"<64 hex>"}]}
```

`GET /v1/pool/{fp}/tiles/{k}.jpg?profile=t320x180q75g10v1` (read key) returns an exact JPEG and headers
`X-OpenSeek-Pts-Ms`, `X-OpenSeek-Duration-Ms`, `X-OpenSeek-N-Tiles`, `X-OpenSeek-Profile`, and
`X-OpenSeek-Sha256`. A miss is 404. OpenSeek clients copy a verified hit to their own cache and continue using
their own local playback path.

`PUT /v1/pool/{fp}/tiles/{k}.jpg?profile=…&pts_ms=…&duration_ms=…&n_tiles=…` (write key) accepts a raw JPEG
body. The server checks the fingerprint, profile, tile bounds, grid timestamp, duration/count consistency, JPEG
decode/dimensions and digest, then posts it to Telegram with `sendDocument`. New tile: 201; identical retry: 200;
conflicting bytes for an immutable tile key: 409; invalid tile: 400. A conflicting tile never replaces the first.
The pool key is `(fingerprint, profile, k)`; different encodes remain separate.

### TV-generated NVMT packages

Nuvio TV captures thumbnails locally and packs them into its versioned `NVMT` binary format. These endpoints
share the same fingerprint, pairing, contribution permission, Telegram storage, and anonymous-read limits as
the tile API.

`GET /v1/pool/{fp}/nvmt?profile=nvmt-v1-g5|nvmt-v1-g10` returns the raw NVMT v1 package; a miss is 404. An
optional `episode_key` records the hash-to-package association after an exact hit, so older packages can join the
cross-encode index when a client reads them. The
response includes `X-OpenSeek-Duration-Ms`, `X-OpenSeek-N-Tiles`, `X-OpenSeek-Profile`, and `X-OpenSeek-Sha256`.
Clients should validate the header and fingerprint before applying the package locally.

The optional `slots=1,2,...` query returns those NVMT records, or the nearest available record within one slot,
in a valid partial package (up to 12 requested slots). This lets a client fetch nearby images before downloading
the full package.

`PUT /v1/pool/{fp}/nvmt?profile=nvmt-v1-g5|nvmt-v1-g10&duration_ms=…&imdb=…&season=…&episode=…&size_bytes=…&filename=…&episode_key=…` accepts raw NVMT v1 bytes with
`Content-Type: application/vnd.openseek.nvmt` (or `application/octet-stream`), up to 16 MiB. The server checks
the profile, grid, duration, slot count, duplicate/out-of-range slots, and decodes each JPEG to verify 320×180
dimensions. Channel-mode nodes store the merged package as one Telegram document. Host/VPS nodes retain the exact
merged package locally and publish the shared ten-second tile representation to Telegram. For a host-mode g5
package, that representation samples g5 slots nearest each ten-second point and cannot rebuild every exact g5
slot; this is the accepted trade-off in [ADR-0001](../adr/0001-telegram-is-the-canonical-pool-store.md). The first
accepted JPEG for each slot stays; later uploads add only missing slots. New coverage: 201; unchanged retry: 200;
duration conflict for the same fingerprint/profile: 409; malformed data: 400.

On a duplicate PUT, non-absent hint values update their fields independently. `NULL` for `imdb`, `size_bytes`,
or normalized `filename`, and `0` for `season` or `episode`, mean “not supplied” and preserve the stored value.
This follows the package body's preserve-on-retry rule for tile slots. A first upload stores omitted hints as
absent (`NULL`/`0`). Omission never clears stored hints; clearing requires a separate explicit operation, and
this PUT has no clear flag. A correction supplies a replacement value.

On a channel node without post rights the write is refused with
`403 {"error":"read_only_channel","contribute_url":"https://…"}`; `contribute_url` is optional. Hints, ranking,
and the Telegram manifest are specified in `docs/api/HINTS-v2.md`.

For episode sharing, clients may include a 64-character SHA-256 `episode_key` derived from the catalog
ID, season, and episode. The server stores only this pseudonymous hash, not the catalog ID or title; because
catalog IDs are guessable, this hash is not encryption or a privacy guarantee. `GET
/v1/pool/episodes/{episode_key}?duration_ms=…` returns up to eight nearby package candidates for older clients.
New clients use `GET /v1/pool/candidates` with raw hints: `imdb`, `season`, `episode`, `duration_ms`,
`size_bytes`, and `filename`. The server ranks by those hints and the client accepts the best candidate without
decoding sample frames. Either index is optional; exact fingerprint lookup continues to work without it.

`GET /v1/pool/{fp}/nvmt/manifest?profile=nvmt-v1-g5|nvmt-v1-g10` (read key) answers
`200 {"fp":"…","profile":"…","duration_ms":…,"n_slots":…,"n_tiles":…,"tile_slots":[0,1,…]}` — the slots the
stored package covers — so a client can plan a partial `slots=` fetch before downloading the package. An
optional `episode_key` records the hash-to-package association exactly like the package GET. Unknown profile
`400 {"error":"bad_profile"}`; a mismatched `duration_ms` or a miss `404 {"error":"not_found"}`;
`403 {"error":"forbidden"}` without read access.

### Staged uploads and setup routes (every node; only a Vercel deployment serves them)

Large uploads on Vercel go through staged uploads in the private Supabase bucket rather than a direct wire
body. The routes exist on every node so clients keep one code path:

- `POST /v1/pool/uploads` (write key) creates the staged upload: `201` with the upload id and presigned
  parameters. Fingerprint, profile, duration, and contribution rules are the same as a direct PUT.
- `POST /v1/pool/uploads/{id}/finalize` (write key) hands the staged object to the pool: `202` while the node
  drains it.
- `GET /v1/pool/uploads/{id}` (write key) polls the upload state; polling also triggers a retry of paused
  work on deployments without a separate worker.

On any non-staged deployment (local, VPS, or a host node without staging) these answer
`400 {"error":"staging_unavailable"}` on `POST /v1/pool/uploads` and `404 {"error":"unknown_upload"}` on the
per-upload routes, and a client falls back to the direct `PUT /v1/pool/{fp}/nvmt` body path. On a node
without any pool surface at all they answer the uniform `404 {"error":"not_found"}`.

`GET` and `POST /v1/internal/tick` drive the staged-upload worker on serverless schedules. They accept
`Authorization: Bearer <OPENSEEK_CRON_SECRET>` or the owner admin token. Missing, malformed, or incorrect
credentials answer `401 {"error":"unauthorized"}` when a credential is configured; if neither credential is
configured, or staged uploads are unsupported, the answer is `503 {"error":"tick_unconfigured"}`. The guest-only
public listener hides this route with `404`. Tick is operational plumbing, not part of the client surface.

`POST /v1/setup/pairing` is the participant wizard's invite-minting route. It is available only on the `vercel`
profile with a valid `Authorization: Bearer $OPENSEEK_SETUP_KEY`; a missing or invalid key, or another profile,
answers `404 {"error":"not_found"}` so the route's existence is not observable elsewhere. The key is short-lived
and the wizard removes it after setup. `enable_sharing` is a strict boolean and defaults to `false`. Explicit `true`
records durable sharing consent before invite creation and preserves existing contribution choices. If sharing is
already enabled, omitted or `false` consent still allows the invite. Otherwise, omitted or `false` consent answers
`409 {"error":"sharing_disabled"}` without creating an invite. Invite creation failure answers
`503 {"error":"pairing_setup_unavailable"}`; explicit sharing consent may already have been recorded.

Profiles map `nvmt-v1-g5` to a 5-second grid and `nvmt-v1-g10` to a 10-second grid. Different file versions
have different fingerprints and separate pool entries.

## Changelog
- v1: initial version.
- 2026-11-07: documented the NVMT package manifest route, the staged-upload routes with their non-Vercel
  refusals, `/v1/internal/tick`, and `/v1/setup/pairing` (G4 surface audit, #68); duplicate-PUT hint
  semantics are now explicit — omitted hints preserve the stored record.
- 2026-11-08: documented the host API's protections (H6, #57): distinct missing/malformed/revoked refusals,
  auth-before-body including oversized bodies, the documented 404/405 shapes, and the advertised per-class
  rate limit with `X-RateLimit-*` headers and `429`/`Retry-After`. Missing pool credentials are now
  `401 unauthorized` (previously `403 forbidden`); malformed credentials are `400 bad_auth`.
- 2026-11-09: reconciled tick authentication and setup-pairing consent, refusal, and invite-failure behavior with
  the current handlers.
