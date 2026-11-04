# OpenSeek API contract v1

The HTTP API implemented by the OpenSeek backend (`backend/openapi.json` is the machine-readable export).
See `docs/SECURITY-MODEL.md` for the security properties. Clients can be written against this document.

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
  from this dashboard action, never from Nuvio playback or pool lookup requests.
- JSON bodies ≤ 16 KiB, `Content-Type: application/json`; shared NVMT packages are binary and capped at 16 MiB.
  Errors: `{"error": "<code>"}` — stable snake_case
  codes, never echoing input.
- Auth: `Authorization: Bearer <token>` (43-char base64url of 32 random bytes).
- Optional shared-pool PUTs use raw `image/jpeg` bodies up to 256 KiB, not JSON.

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

### `POST /v1/previews` (Bearer) — create or attach
```json
{"source": {"url": "https://…", "headers": {"User-Agent": "…", "Referer": "…"}, "duration_ms": 7234000},
 "hints": {"imdb_id": "tt…", "season": 0, "episode": 0, "video_size": 0, "video_hash": "", "filename": ""},
 "at_ms": null, "playhead_ms": 0, "layout": "hybrid"}
```
Only `source.url` required. Forwarded headers: allowlist `User-Agent`, `Referer`, `Origin`; others dropped.
`layout` ∈ `tiles|sheets|hybrid` (default `hybrid`).
Response `202` (always, also for cache hits — clients treat 200/202 alike):
```json
{"preview_id":"<32 hex>","state":"preparing|ready|complete|failed","error":null,
 "vtt_url":"https://<host>:8787/v1/c/<cap>/<preview_id>/thumbs.vtt?exp=<unix>&layout=hybrid",
 "vtt_version":0,"source_duration_ms":null,"tile_interval_ms":10000,"tolerance_ms":5000,
 "progress":{"tiles_done":0,"tiles_total":null},"refresh_after_ms":1000}
```
- `preview_id` = random per (device, content fingerprint); same device + same content → same id.
- `state`: `preparing` (preflight/probe running, VTT may be empty), `ready` (≥1 tile), `complete` (all
  grid tiles done), `failed` (`error` ∈ `range_unsupported`, `no_index`, `not_video`, `source_forbidden`,
  `source_unreachable`, `source_expired`, `budget_exceeded`, `unsupported_codec`, `internal`).
- Errors before a job exists: `400 bad_request`, `400 source_forbidden` (SSRF policy), `401 unauthorized`,
  `413 too_large`, `429 busy` (+`Retry-After`), `503 unavailable`.

### `GET /v1/previews/{id}` (Bearer) — status; same body as above.
Query `wait_version=N&wait_ms≤2000`: long-poll, returns when `vtt_version > N` or after `wait_ms`.

### `POST /v1/previews/{id}/priority` (Bearer)
Body `{"at_ms": 4800000|null, "playhead_ms": 1234000}` → `200 {"vtt_version": N, "state": "…"}`.
Latest-wins; `at_ms=null` = scrubbing ended. Client sends ≤ 8/s. Never awaited by the draw thread.

### `DELETE /v1/previews/{id}` (Bearer) → 204. Stops work; finished tiles stay cached.

### Optional shared tile pool

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
their own normal VTT/tile capability URLs.

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
dimensions. It stores the merged package as one Telegram document. The first accepted JPEG for each slot stays;
later uploads add only missing slots. New coverage: 201; unchanged retry: 200; duration conflict for the same
fingerprint/profile: 409; malformed data: 400.

On a channel node without post rights the write is refused with
`403 {"error":"read_only_channel","contribute_url":"https://…"}`; `contribute_url` is optional. Hints, ranking,
and the Telegram manifest are specified in `docs/api/HINTS-v2.md`.

For legacy TV episode sharing, clients may include a 64-character SHA-256 `episode_key` derived from the catalog
ID, season, and episode. The server stores only this pseudonymous hash, not the catalog ID or title; because
catalog IDs are guessable, this hash is not encryption or a privacy guarantee. `GET
/v1/pool/episodes/{episode_key}?duration_ms=…` returns up to eight nearby package candidates for older clients.
New clients use `GET /v1/pool/candidates` with raw hints: `imdb`, `season`, `episode`, `duration_ms`,
`size_bytes`, and `filename`. The server ranks by those hints and the client accepts the best candidate without
decoding sample frames. Either index is optional; exact fingerprint lookup continues to work without it.

Profiles map `nvmt-v1-g5` to a 5-second grid and `nvmt-v1-g10` to a 10-second grid. Different file versions
have different fingerprints and separate pool entries.

### Data plane (capability URLs; no headers needed; Bearer also accepted)
`cap` = base64url(HMAC-SHA256(server_secret, device_id ‖ preview_id ‖ exp))[0:32]; `exp` ≤ now + 6 h.
Invalid/expired → `403 {"error":"forbidden"}`.
- `GET /v1/c/{cap}/{preview_id}/thumbs.vtt?exp=…&layout=…` → `text/vtt`, `Cache-Control: no-store`.
- `GET /v1/c/{cap}/{preview_id}/tiles/{k}.jpg?exp=…` → 320×180 JPEG, `Cache-Control: private, max-age=…`.
  Immutable once published (k = 10 s grid index). Unpublished → 404.
- `GET /v1/c/{cap}/{preview_id}/sheets/{n}.jpg?exp=…&v=…` → 3200×1800 JPEG (10×10 tiles k = 100n…100n+99),
  only referenced when complete (`hybrid`) or with unfilled tiles black and unreferenced (`sheets`).

## VTT semantics
```
WEBVTT

00:79:57.000 --> 00:80:03.500
https://…/tiles/480.jpg?exp=…#xywh=0,0,320,180
```
- Only published tiles appear. Each tile with real pts `p` gets cue `[p−5 s, p+5 s)`, clipped at the midpoint
  between neighbouring published tiles' pts and at [0, duration]. Positions not inside any cue have no preview.
- Cues sorted, non-overlapping. Absolute URLs, same scheme/host/port the client used.
- `vtt_version` increments whenever the published set changes.
- The display label is the tile's real pts, which is NOT the cue start. Every tile/sheet URL carries the pts
  as `&t=<pts_ms>` before the fragment (e.g. `…/tiles/480.jpg?exp=…&t=4797960#xywh=…`). For sheets, each
  cue's URL carries its own tile's `t`, so the same sheet appears with different `t` values; clients MUST
  key the sheet by the URL with `t` stripped. Clients label the preview with `t`.

## Changelog
- v1: initial version.
