# Plan: SeekDB — a hint-matched NVMT pool

Status: revised plan. Supersedes the previous SeekDB proposal in this file.
Implementation tracker: server GitHub issues #1–#19 in `Yatin-Code/openseek-telegram`; client tickets in
`Nuvio-native-tizen-fixes/.scratch/hints-pool/`.
Protocol: `docs/api/HINTS-v2.md` and `docs/api/CONTRACT-v1.md`.

## The decision

SeekDB is a shared **store and index of finished NVMT thumbnail packages**. It never decodes video and never
generates thumbnails; clients do that locally and contribute the result.

- `fp` is the exact blob key (size + first/last 64 KiB). It is never replaced.
- Reuse is driven by **raw hints**: IMDb, season, episode, duration, size, normalized filename. No hash of hints.
- Hints live in Telegram beside the tiles. The local SQLite index is rebuildable from the channel.
- `POOL_MODE=channel` is the primary shared-pool path. A central `host` is optional.
- Matching is approximate and server-ranked. Clients do **not** sample-verify frames at request time.
- NVMT only. VTT and the preview API are not on the pool path.
- Serverless is deferred.

## What changed from the previous plan

| Previous plan | This plan |
|---|---|
| Leave `library.py` behind as a media-only concern | The hint/reuse layer is the core; only its media-generation parts stay behind |
| `episode_key` is "already solved" | It rejected films (`episode <= 0`); D1 fixes the client, A2 indexes films |
| Telegram is cold storage; SQLite is authoritative | Hints are persisted in Telegram; the channel rebuild restores them |
| `channel` mode has no HTTP pool endpoints | A channel node serves `/v1/pool/*` reads from its local index |
| Extract a separate `seekdb` service first | Evolve this repo; extract only if hosting demands it |
| Vercel/Cloudflare in scope | Deferred until the client can do CA TLS, redirects, and presigned/multipart upload |

## Product model

| Role | What it does |
|---|---|
| **Uploader** | A client that generated NVMT locally and `PUT`s it with hints |
| **Receiver** | A client that asks for exact `fp`, then hint-ranked candidates |
| **Giver** | A node that serves its local index over `/v1/pool/*` |
| **Librarian** | A Telegram bot that accepts DM contributions, validates them, and reposts into the channel |

The pool has no generator and no video fetcher.

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

### Host node (optional)

One central node with filesystem or Telegram storage. Same API, same protocol. Useful when a group wants a
single always-on reader/writer, but not required for any feature.

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
| Upload | `PUT /v1/pool/{fp}/nvmt?profile=…&duration_ms=…&imdb=…&season=…&episode=…&size_bytes=…&filename=…` |

Hints are query parameters on both GET and PUT. The Telegram manifest is one message per `(fp, profile)`:

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

| Object | Host | Channel |
|---|---|---|
| Packages | `pool_packages` | assembled from `pool_channel_tiles` |
| Tiles | `pool_tiles` | `pool_channel_tiles` |
| Hints | `pool_hints` | `pool_hints`, rebuilt from manifests |
| Legacy episode index | `pool_episode_packages` | `pool_episode_packages` |
| Outbox | `pool_outbox` | `pool_channel_outbox` |
| Scan state | n/a | `pool_channel_state` |

Telegram is the source of truth for channel blobs and hints. SQLite is a rebuildable index. `rebuild()` clears
tiles, hints, and the legacy episode index, then scans messages. Old v1 packages rebuild as exact-`fp`-only.

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

| Profile | Storage index | Blob storage | Status |
|---|---|---|---|
| Channel | SQLite | Telegram channel via MTProto | Primary |
| Host + filesystem | SQLite | Local filesystem | Supported |
| Host + Telegram | SQLite | Telegram via bot | Supported |
| Channel read-only | SQLite | Telegram channel via MTProto | Primary for foreign channels |
| Serverless (Vercel/Cloudflare) | PostgreSQL/S3/R2 | object storage | Deferred |

v1 is environment-only: `.env` plus env vars, no setup wizard. `doctor` reports mode, entity, history access,
index counts, write capability, and librarian status. See `docs/INSTALL.md` and `docs/TELEGRAM-POOL.md`.

Serverless remains blocked on the client, not the server: the current Nuvio transport pins the SPKI, refuses
redirects, and sends up to 16 MiB in a single PUT.

## Phases and tickets

| Phase | Tickets | Outcome |
|---|---|---|
| 0 | A1 (done) | Protocol frozen: hints v2, candidate fields, manifest, read-only response |
| 1 | A2–A5 | Host-mode hint index, upload hints, ranking, legacy route delegation |
| 2 | B1–B7 | Channel nodes serve `/v1/pool/*`, publish or route contributions, rebuild hints |
| 3 | C1–C3 | Librarian intake, quotas/verified, per-device offsets/rejections |
| 4 | D0–D7 | Nuvio: film work key, hints, ranked candidate, linear mapping, read-only contribution |
| 5 | E1–E3 | This plan, operator docs, migration note |

Server tickets: GitHub issues #1–#19. Client tickets: `Nuvio-native-tizen-fixes/.scratch/hints-pool/`.

## Non-goals

- Video decode or thumbnail generation in the pool.
- VTT, `/v1/previews`, `/v1/lookup`, sprite-sheet URLs.
- Full-file hashing or hashing the hints.
- A metadata scraper or library manager.
- Serverless deployment in this phase.
- S3/R2 and PostgreSQL until a serverless profile is actually built.

## Risks

- **Telegram limits:** every reader node fetches its own copy of hot tiles; FloodWait and per-account limits
  bound scale. A central host dedupes but is optional.
- **Poisoning:** no hash means a bad contributor can create a competing package. Mitigate with ranking,
  quotas, `verified`, and per-device rejections; do not claim a proof.
- **Freshness:** the index is only as fresh as the last scan (`POOL_INDEX_REFRESH_S`, default 300 s) plus the
  live handler. A restart rebuilds from Telegram.
- **Upload size:** the client still sends up to 16 MiB in one PUT; serverless needs a different transport.
- **Client coverage:** the Nuvio client is the only NVMT consumer today; other clients need the same protocol.

## Migration

- v1 packages without manifests stay exact-`fp`-only. No automatic backfill.
- An operator may opt into `backfill-hints` with a sidecar `fp -> hints` file; the server never guesses.
- Existing paired devices, URLs, and TLS pins survive the label rename.
