# Plan: OpenSeek — a shared NVMT pool

Status: revised plan. Supersedes the previous SeekDB proposal in this file.
Implementation tracker: server GitHub issues #1–#23 in `Yatin-Code/openseek-telegram`; client tickets in
`Nuvio-native-tizen-fixes/.scratch/hints-pool/`.
Protocol: `docs/api/HINTS-v2.md` and `docs/api/CONTRACT-v1.md`.

## The decision

OpenSeek is a shared **store and index of NVMT packages**, with Telegram as the canonical durable pool. Nuvio
generates NVMT locally when playback has no usable pool match. OpenSeek may also generate an NVMT package when
an owner explicitly starts that action from the admin dashboard; generation never runs on a player request.

- `fp` is the exact blob key (size + first/last 64 KiB). It is never replaced.
- Reuse is driven by **raw hints**: IMDb, season, episode, duration, size, normalized filename. No hash of hints.
- Hints live in Telegram beside the tiles. Local SQLite and Vercel PostgreSQL indexes rebuild from the channel.
- Telegram is the permanent shared pool. SQLite or PostgreSQL is a rebuildable index; temporary staging is not
  a second durable package store.
- Local/VPS nodes and the Vercel profile are first-class deployments. Local/VPS nodes use SQLite and local
  temporary disk; Vercel uses Supabase for its index, temporary upload staging, and retry queue.
- The same browser dashboard is the owner interface in every profile. Local/VPS use the local admin secret;
  Vercel uses one owner-configured secret. There are no user accounts or sign-up flow in v1.
- `POOL_MODE=channel` is the primary local/VPS shared-pool path. A central `host` is optional.
- Matching is approximate and server-ranked. Clients do **not** sample-verify frames at request time.
- The shared pool speaks NVMT only. The old player preview/VTT surface is not part of the pool and is removed
  after the dashboard NVMT flow replaces the administration use cases.

## What changed from the previous plan

| Previous plan | This plan |
|---|---|
| Keep the old media preview generator as a product surface | Keep only the generation path needed for explicit dashboard NVMT creation; retire player preview/VTT generation |
| `episode_key` is "already solved" | It rejected films (`episode <= 0`); D1 fixes the client, A2 indexes films |
| Telegram is cold storage; SQLite is authoritative | Hints are persisted in Telegram; the channel rebuild restores them |
| `channel` mode has no HTTP pool endpoints | A channel node serves `/v1/pool/*` reads from its local index |
| Extract a separate `seekdb` service first | Evolve this repo; extract only if hosting demands it |
| Vercel/Cloudflare in scope | Vercel is required; Cloudflare is not part of v1 |

## Product model

| Role | What it does |
|---|---|
| **Uploader** | Nuvio or the admin dashboard submits a validated NVMT package with hints |
| **Receiver** | A client that asks for exact `fp`, then hint-ranked candidates |
| **Giver** | A node that serves its local index over `/v1/pool/*` |
| **Admin dashboard** | The browser interface for node operations and explicit NVMT generation |
| **Librarian** | A Telegram bot that accepts DM contributions, validates them, and reposts into the channel |

The player path never asks OpenSeek to decode video. Nuvio generates on a playback miss; an owner may separately
start NVMT generation from the dashboard. Both outputs enter the same validation and Telegram publication flow.

## Topologies

### Channel node (primary)

Each user runs a node with a Telegram bot token (default) or a user session (fallback). The node indexes the
shared channel over MTProto, serves its own TVs, and refreshes every `POOL_INDEX_REFRESH_S`. No central server
and no invitation from a host are required to read.

Contribution depends on what the node may do in the channel:

| Channel | Node can post? | Contribution path |
|---|---|---|
| Host's canonical channel | yes | `PUT /v1/pool/{fp}/nvmt` publishes tiles + manifest directly |
| Foreign/community channel | no | Proxy to `POOL_CONTRIB_URL`, or `403 read_only_channel` with `contribute_url`; librarian DM is the Telegram-native intake |

`POOL_CHANNEL_WRITABLE=auto|1|0` overrides detection. A read-only node still serves reads and rebuilds its index.

### Local/VPS node

The same OpenSeek node runs on a developer machine, home server, or VPS. Telegram stays the durable shared pool;
the node's database is a rebuildable index and any local package bytes are cache or temporary data. An always-on
VPS can serve as a convenient giver, but it is not required for the pool to exist.

### Vercel profile (required)

Vercel runs the same OpenSeek API and browser dashboard. Supabase PostgreSQL holds the rebuildable index and
operation status. Supabase Storage holds only short-lived upload staging, and Supabase Queues/Cron retries
publication to Telegram. Telegram remains the durable pool.

For Nuvio uploads, OpenSeek issues a short-lived signed upload target so the NVMT bytes go directly to temporary
storage instead of through the Vercel function body limit. Nuvio finalizes the upload and polls an upload ID;
OpenSeek reports completion only after Telegram accepts the package, then removes the staged copy. Downloads
stream from Telegram through OpenSeek directly to Nuvio; they are not staged in Supabase.

The dashboard offers the same owner operations in local/VPS and Vercel profiles. A Vercel owner uses one
configured admin secret; no multi-user identity provider is required. Dashboard-initiated NVMT generation is
also part of the target in both profiles and runs as an asynchronous operation.

### Portable admin surface

The browser dashboard is the only OpenSeek administration app. Local/VPS deployments serve it from the node;
Vercel serves the same dashboard against its deployment adapters. Existing pairing, pool status, scan/rebuild,
sharing, and settings operations remain available through the same owner-facing interface. Platform-specific
storage and queue details stay behind the deployment profile.

### Librarian

A bot in the channel with post rights. Contributors DM an NVMT package plus hints; the librarian validates,
splits, reposts the tiles and one manifest, and every node indexes them on refresh. Rate limits and quotas apply
per sender.

## Protocol

The client contract is `/v1/pool/*`. See `docs/api/HINTS-v2.md` for the full field list and examples.

| Operation | Endpoint |
|---|---|
| Exact package | `GET /v1/pool/{fp}/nvmt?profile=…` |
| Sparse package | `GET /v1/pool/{fp}/nvmt?profile=…&slots=1,2` |
| Manifest | `GET /v1/pool/{fp}/manifest?profile=…` |
| Exact tile (legacy) | `GET /v1/pool/{fp}/tiles/{k}.jpg?profile=…` |
| Hint candidates | `GET /v1/pool/candidates?imdb=…&season=…&episode=…&duration_ms=…&size_bytes=…&filename=…` |
| Legacy episode candidates | `GET /v1/pool/episodes/{episode_key}?duration_ms=…` |
| Local/VPS upload | `PUT /v1/pool/{fp}/nvmt` with the NVMT package and hints |
| Vercel upload | Create signed temporary upload → upload directly to staging → finalize → poll until Telegram accepts |

Hints are query parameters on candidate GETs and local/VPS direct PUTs. Vercel carries them through the signed
upload creation/finalization flow. The Telegram manifest is one message per `(fp, profile)`:

```
openseek:hints:v1:<base64url(json)>
```

Tile captions stay `openseek:v1:<fp>:<profile>:<k>:<sha256>`. A package without a manifest is exact-`fp`-only.

## Identity and ranking

- `fp` stays the exact content key. A different release is a different entry.
- Hints are claims; every candidate is `approximate: true`.
- Ranking tiers: IMDb + season/episode + duration (100), 24/25 speed (95), length-differs (90), IMDb-only film
  (85), filename exact (75), filename fuzzy (65). Below 65 is not returned.
- Hard cutoff: duration delta `> max(90_000, 15%)` is never returned.
- Tie-break: size within 1%, closest duration, newest.
- The client accepts `candidates[0]` at `score >= 60` and `relation != length_differs`; otherwise it generates
  locally.
- Timing mapping is `t_target = (t_source * a_num) / a_den + b_ms`.
- No request-time frame verification.

## Data and rebuild

| Data | Local/VPS | Vercel |
|---|---|---|
| Durable NVMT packages and manifests | Telegram | Telegram |
| Rebuildable index and pool metadata | SQLite | Supabase PostgreSQL |
| In-flight upload bytes | Local temporary disk | Supabase Storage with expiry |
| Retry/status work | Local process and SQLite | Supabase Queues/Cron and PostgreSQL |

Telegram is the source of truth for shared pool bytes and hints. SQLite or PostgreSQL is a rebuildable index.
`rebuild()` clears derived package/hint rows, then scans Telegram. Old v1 packages rebuild as exact-`fp`-only.

## Trust and abuse

- Hints are client-claimed. There is no full-file hash and no request-time verification.
- Mitigations: strict validation, per-sender and per-title quotas, first-writer-wins, a `verified` flag for
  trusted contributors as a tie-break only, and per-device rejections.
- A rejection hides that `fp` for that device and work key for N days; an offset shifts `b_ms` for that device
  only. Other devices are unaffected.
- A bad upload cannot replace an existing tile for `(fp, profile, k)`, but it can create a competing package.
  The ranker prefers exact IMDb + duration and the client can reject; this is the accepted trade-off for
  no-hash contribution.

## Deployment profiles

| Profile | Storage index/status | Durable package store | Temporary work | Status |
|---|---|---|---|---|
| Local/VPS | SQLite | Telegram channel via MTProto or bot | Local temporary disk/cache | First-class |
| Vercel | Supabase PostgreSQL | Telegram | Supabase Storage with expiry; Supabase queue for retry | Required target |

Read-only access to a foreign Telegram channel is a node permission/contribution mode, not a separate deployment
profile. Local filesystem package storage is not a durable shared-pool backend.

The admin website starts with a browser setup wizard when an installation is not configured. On local/VPS it
collects and validates Telegram pool settings, writes them to the protected per-install `.env`, checks channel
history and posting access, and starts the first scan. On Vercel it guides the owner through setting project
environment variables and validates them after deployment; a function cannot write Vercel project settings.
`doctor` remains available for later diagnostics and reports mode, entity, history access, index counts, write
capability, and librarian status. See `docs/INSTALL.md` and `docs/TELEGRAM-POOL.md`.

Vercel uploads require a client transport for short-lived signed staging and asynchronous Telegram publication.
The existing direct PUT remains the local/VPS path. Vercel downloads continue through OpenSeek as a direct
Telegram stream.

## Phases and tickets

| Phase | Tickets | Outcome |
|---|---|---|
| 0 | A1 (done) | Protocol frozen: hints v2, candidate fields, manifest, read-only response |
| 1 | A2–A5 | Host-mode hint index, upload hints, ranking, legacy route delegation |
| 2 | B1–B7 | Channel nodes serve `/v1/pool/*`, publish or route contributions, rebuild hints |
| 3 | C1–C3 | Librarian intake, quotas/verified, per-device offsets/rejections |
| 4 | D0–D8 | Nuvio: film work key, hints, ranked candidate, linear mapping, read-only and Vercel uploads |
| 5 | E1–E3 | This plan, operator docs, migration note |
| 6 | F1–F4 | Vercel profile, one shared dashboard, dashboard NVMT generation, retire desktop/legacy preview paths |

Server tickets: GitHub issues #1–#23. Client tickets: `Nuvio-native-tizen-fixes/.scratch/hints-pool/`.

## Non-goals

- Video decode or generation during a pool/player request.
- VTT, `/v1/previews`, `/v1/lookup`, and sprite-sheet URLs in the shared-pool product.
- Full-file hashing or hashing the hints.
- A metadata scraper or library manager.
- Cloudinary, Upstash Redis, QStash, or a second durable object store in v1.
- Multi-user accounts or a sign-up flow for the admin dashboard.

## Risks

- **Telegram limits:** every reader node fetches its own copy of hot tiles; FloodWait and per-account limits
  bound scale. A central host dedupes but is optional.
- **Poisoning:** no hash means a bad contributor can create a competing package. Mitigate with ranking,
  quotas, `verified`, and per-device rejections; do not claim a proof.
- **Freshness:** the index is only as fresh as the last scan (`POOL_INDEX_REFRESH_S`, default 300 s) plus the
  live handler. A restart rebuilds from Telegram.
- **Vercel upload size:** packages bypass the function body through temporary signed staging; failed publication
  must remain visible and retryable until Telegram accepts it.
- **Client coverage:** the Nuvio client is the only NVMT consumer today; other clients need the same protocol.

## Migration

- v1 packages without manifests stay exact-`fp`-only. No automatic backfill.
- An operator may opt into `backfill-hints` with a sidecar `fp -> hints` file; the server never guesses.
- Existing paired devices, URLs, and TLS pins survive the label rename.
