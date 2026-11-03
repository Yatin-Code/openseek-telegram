# Tickets: hint-based shared pool (NVMT, Telegram-first)

Status: backlog, supersedes the hint-related parts of `PLAN-SEEKDB.md`.
Branches: server `openseek-telegram@openseek-thumbnil`; client `Nuvio-native-tizen-fixes@openseek-thumbnil`.
Scope rule: `pool.py` routes stay the only client contract.

## Hard invariants

1. **NVMT always, VTT never.** The pool path speaks only `application/vnd.openseek.nvmt` and
   JPEG tiles wrapped in it. No VTT endpoint, no WebVTT parsing, no sprite-sheet URL protocol,
   no `/v1/previews` or `/v1/lookup` dependency. The existing OpenSeek preview/VTT code may
   remain for its own desktop clients, but it is never on the pool path and no pool ticket may
   introduce a VTT branch.
2. `fp` is the exact blob key. Hints never replace it.
3. No full-file hash, no hash of hints, no video URL or header ever reaches the pool.
4. No request-time frame/sample verification.

## Settled decisions these tickets encode

- `fp` remains the exact blob key (size + first/last 64 KiB). It is never replaced by a hint key.
- Reuse is driven by **raw hints**: IMDb, season, episode, duration, size, filename. No hash of hints.
- Hints are persisted in Telegram; the local SQLite index is rebuildable from the channel.
- `POOL_MODE=channel` is the primary shared-pool path. A central `host` is optional.
- No request-time frame/sample verification. The server ranks; the client accepts the best candidate and applies a timing mapping.
- Old v1 packages stay exact-`fp`-only. No forced backfill.
- Serverless is deferred until the Nuvio client can do CA TLS, redirects, and presigned/multipart upload.

## Contribution topologies

Contribution has two shapes, selected by **what the node is allowed to do in the pool channel**,
not by a global mode flag. The channel id decides which shape applies; the node detects it at startup
(the existing `_verify_entity` already calls `get_permissions(entity, "me")`).

| Channel ownership | Node can post? | Contribution path |
|---|---|---|
| **Hosted channel** — the pool operator owns/administers it | yes | Client `PUT /v1/pool/{fp}/nvmt` to its node; the node publishes tiles + manifest directly. This is the current behavior and stays the default for `host` and writable `channel` nodes. |
| **Foreign channel** — a community/shared channel owned by someone else | no | The node is read-only. It indexes the channel for its own TVs but cannot publish. Contributions go to the channel owner's intake: server-side proxy to `POOL_CONTRIB_URL` when configured, otherwise a machine-readable `contribute_url` in the 403 body so the client can retry against the host. The librarian DM path is the Telegram-native variant of the same intake. |

In practice this maps to the channel id: a node pointed at the host's canonical channel publishes
as it does today; a node pointed at any other channel is read-only unless its own bot/user has post
rights there. `POOL_CHANNEL_WRITABLE=auto|1|0` overrides detection for odd permission setups.
A read-only node still serves `/v1/pool/*` reads (B1) and still rebuilds its index (B3); only writes
change shape.

## Definition of done for the reported bug

Two different files of the same film (different `fp`, same IMDb id, near-equal duration):
the second client gets a candidate hit, downloads one NVMT package, applies it, and performs
**zero local frame extraction**. Logs show `exact miss -> candidate hit -> applied`, and the
client never enters the sample-verification loop.

---

## Epic A — Hints protocol and server index (host mode first)

### A1 — Freeze the v2 hint manifest
**Why:** the protocol is the dependency for every other ticket.
**Scope:** `docs/api/CONTRACT-v1.md`, new `docs/api/HINTS-v2.md`.
**Do:**
- Define a manifest per `(fp, profile)` carrying: `fp`, `profile`, `imdb`, `season`, `episode`,
  `duration_ms`, `size_bytes`, `filename_norm`, `n_tiles`, `grid`, `created`.
- `filename_norm` = basename, lowercase, extension stripped, separators collapsed, max 200 chars.
  Never store a path, URL, header, token, or raw release-group string beyond the normalized name.
- Wire form: one Telegram message per package, caption `openseek:hints:v1:<base64url(json)>`,
  max 900 bytes; tiles keep the existing `openseek:v1:...` caption.
- `episode_key` stays accepted as a legacy query param; new clients send raw hints.
- Define candidate response: `score`, `match` (`imdb|filename|duration`), `relation`
  (`same_length|speed_24_25|length_differs`), `approximate: true`,
  `a_num`, `a_den`, `b_ms`, `source_duration_ms`, `n_slots`, `n_tiles`.
**Acceptance:** a client can implement the protocol from the doc alone; examples for movie, series, speed-shifted release.
**Depends:** none. **Size:** S.

### A2 — Add the hint index to SQLite
**Why:** `pool_packages`/`pool_episode_packages` have no IMDb/filename columns and cannot answer hint queries.
**Scope:** `backend/openseek/db.py`, `backend/openseek/pool.py`.
**Do:**
- Add `pool_hints(fp, profile, imdb, season, episode, duration_ms, size_bytes, filename_norm,
  n_tiles, grid, created, PRIMARY KEY(fp, profile))`.
- Add indexes: `(imdb, season, episode)`, `(filename_norm)`, `(duration_ms)`.
- Backfill nothing. A package without a manifest simply has no row.
- `episode_candidates` keeps working off `pool_episode_packages` for old clients.
**Acceptance:** migration is idempotent; a fresh DB and an existing DB both open; `db.py` backup/gc paths include the new table.
**Tests:** migration test, orphan cleanup test. **Depends:** A1. **Size:** S.

### A3 — Accept hints on upload (host mode)
**Why:** the server cannot index what the client does not send.
**Scope:** `backend/openseek/pool.py` (`upload_package`, `index_episode_package`), `app.py` (`PUT /v1/pool/{fp}/nvmt`).
**Do:**
- Accept a bounded `X-OpenSeek-Hints` header: base64url JSON, <= 1024 bytes decoded, validated strictly.
- Validate: IMDb `tt\d{1,10}`, season/episode 0..9999, duration 1..12h, size >= 0,
  filename normalized server-side, no control chars. Reject unknown keys (`extra=forbid` equivalent).
- Upsert `pool_hints` in the same transaction as the package; a duplicate package may update hints.
- Keep `episode_key` compatibility: if present and hints absent, derive only the legacy index.
**Acceptance:** PUT with hints returns 201 and a following candidate query finds it; malformed hints return 400 and store nothing.
**Tests:** `test_pool.py` new cases for valid, oversized, malformed, duplicate-with-updated-hints. **Depends:** A2. **Size:** M.

### A4 — Candidate query and ranking
**Why:** this is the actual reuse decision. It must be deterministic and cheap, because the client will not sample-verify.
**Scope:** `backend/openseek/pool.py`, `app.py`, `docs/api/HINTS-v2.md`.
**Do:**
- Add `GET /v1/pool/candidates?imdb=&season=&episode=&duration_ms=&size_bytes=&filename=` (all optional except duration).
- Rank:
  1. IMDb + season + episode exact, `relation=same_length` (|Δ| <= 1000 ms)
  2. same key, `relation=speed_24_25` (ratio 25/24 or 24/25 within 0.15%)
  3. same key, `relation=length_differs` (|Δ| behind `max(90_000, 15%)`)
  4. IMDb exact only (movies with season/episode 0)
  5. `filename_norm` exact + `same_length`
  6. `filename_norm` fuzzy (token overlap) + `same_length`
- Tie-break: `size_bytes` within 1%, then closest duration, then newest.
- Hard cutoff: |Δ| > `max(90_000, 15%)` is never returned.
- Return max 8, best first, each with `a_num/a_den/b_ms` computed from durations; `b_ms=0` unless a real offset exists.
- Exclude the caller's own `fp` when supplied.
**Acceptance:** movie and series cases; a different title with the same duration is rejected; responses are stable for the same inputs.
**Tests:** table-driven ranking tests, including 24/25, wrong title, filename-only, no-IMDb.
**Depends:** A2, A3. **Size:** M.

### A5 — Legacy episode route delegates to ranking
**Why:** two candidate implementations will drift.
**Scope:** `pool.py::episode_candidates`, `app.py::pool_episode_candidates`.
**Do:** keep the route and response shape; internally call the A4 ranker with the key's hints when available, else the old duration-only query. Add `score/match/relation` fields without removing existing ones.
**Acceptance:** old client response remains parseable; new fields present.
**Tests:** existing `test_episode_variant_index_and_sparse_sample_routes` still passes. **Depends:** A4. **Size:** S.

---

## Epic B — Channel mode becomes the product

### B1 — Serve `/v1/pool/*` in channel mode
**Why:** `core.pool` is only constructed for host mode (`app.py:91-101`); in `channel` mode every pool route returns 404, so a channel node cannot serve its own TV.
**Scope:** `app.py`, new `backend/openseek/channel_pool.py` (thin adapter), `channel_index.py`.
**Do:**
- Implement the same surface as `PoolService`: `manifest`, `tile`, `package`, `package_slots`,
  `episode_candidates`/`candidates`, `upload`, `upload_package`, `index_episode_package`.
- Reads delegate to `ChannelIndex` (`pool_channel_tiles`); writes delegate to the MTProto store and upsert the local index.
- Construct it when `effective_pool_mode == "channel"` and assign to `core.pool` so routes do not branch on mode.
- Missing tiles return the same `404 not_found` shape as host mode.
**Acceptance:** a channel node with a seeded channel serves manifest, sparse NVMT, and candidates to a paired TV; no OpenSeek preview job is created.
**Tests:** new `test_pool_channel.py` route tests against a fake in-memory store. **Depends:** A5. **Size:** L.

### B2 — Channel upload path (hosted/writable channel)
**Why:** when the pool channel belongs to the host, contributors publish through the node exactly as they do now.
**Scope:** `channel_pool.py`, `pool.py` (validation/splitting), `telegram_mt.py`.
**Do:**
- Only active when the node has post rights (B7). `PUT /v1/pool/{fp}/nvmt` validates the package, splits it into tiles, checks first-writer-wins per `(fp, profile, k)` in the index, publishes missing tiles to the channel, then writes `pool_channel_tiles` + hints + manifest.
- Publish the v2 manifest after the last tile; a partial failure leaves the package unindexed and retryable.
- Return 201 on new, 200 with `duplicate: true` when every tile already exists.
- Respect write auth (`pool_write_denial`) and contributor role.
- A read-only node must never enter this path; it returns the B7 response instead.
**Acceptance:** upload from node A is readable from node B after refresh; a second upload of the same package is a no-op; a corrupt tile rejects the whole package; a read-only node stores nothing locally.
**Tests:** fake store + two `ChannelIndex` instances; partial publish retry; read-only rejection. **Depends:** B1, A3. **Size:** L.

### B3 — Rebuild hints and manifests from Telegram
**Why:** Telegram must be the source of truth; today `rebuild()` only deletes/repopulates `pool_channel_tiles` and the hint index would be lost.
**Scope:** `channel_index.py` (`_CAPTION`, `_parse_message`, `_refresh_once`, `rebuild`), `db.py`.
**Do:**
- Add `_HINTS_CAPTION` parsing; index manifest messages into `pool_hints`.
- `rebuild()` also clears `pool_hints` and `pool_episode_packages` before the scan.
- Incremental scan handles manifest messages that arrive before their tiles, and tiles before their manifest.
- Ignore unknown caption versions; never crash the scan.
- A manifest for a package with no tiles is stored but not returned as a candidate.
**Acceptance:** wipe SQLite, rebuild from the channel, candidate query returns the same results; old v1-only channels rebuild with zero hints and still serve exact `fp`.
**Tests:** extend `test_pool_channel.py` (rebuild assertion at `:115`); manifest-before-tiles ordering; malformed manifest. **Depends:** B2. **Size:** M.

### B4 — Real channel candidate query
**Why:** `ChannelIndex.episode_candidates` is a stub returning `[]` (`channel_index.py:257-260`).
**Scope:** `channel_index.py`.
**Do:** replace the stub with the A4 ranking query over `pool_channel_tiles` joined to `pool_hints`; join `pool_packages`-equivalent fields from `pool_channel_tiles` (duration, `ceil(duration/grid)` slots, tile count).
**Acceptance:** the stub test is replaced by a real candidate assertion; duration cutoff and IMDb exactness hold.
**Tests:** `test_episode_variant_index_and_sparse_sample_routes` plus movie case. **Depends:** B3, A4. **Size:** M.

### B5 — Channel node authentication and doctor checks
**Why:** mass users must join with minimal admin work; the code already supports bot tokens and user sessions.
**Scope:** `config.py`, `telegram_mt.py`, `cli.py`, `docs/INSTALL.md`.
**Do:**
- Default: `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHANNEL_ID`; the bot must be a member with history read access. `_verify_entity` already checks this.
- Fallback: `TELEGRAM_SESSION` / phone login, for groups where adding a bot is not possible.
- `seekdb doctor` (or `openseek doctor`) prints: mode, entity resolved, history readable, last message id, index row counts, librarian status.
- Clear startup error when the bot is not a member or history is disabled.
**Acceptance:** a fresh env-only setup reaches a serving node without editing code; doctor distinguishes bot, session, and permission failures.
**Tests:** config validation unit tests; a fake store doctor test. **Depends:** none. **Size:** M.

### B6 — Two-node consistency test
**Why:** channel mode is only credible if independent nodes converge.
**Scope:** `backend/tests/test_pool_channel.py`.
**Do:** two nodes, same fake channel; node A uploads; node B refreshes; B serves the same NVMT bytes and candidate list; force a B rebuild and repeat.
**Acceptance:** byte-identical NVMT, identical candidate ordering, no local video decode. **Depends:** B2, B3, B4. **Size:** S.

### B7 — Detect write capability and route read-only contributions
**Why:** a node pointed at a foreign/shared channel must not attempt to post, and its users still need a contribution path. The channel id alone is not enough; the node's permissions decide.
**Scope:** `telegram_mt.py`, `config.py`, `channel_pool.py`, `app.py`, `cli.py`.
**Do:**
- At startup determine `can_post` from `get_permissions(entity, "me")` plus entity type (`broadcast` vs `megagroup`); expose it in node status. `POOL_CHANNEL_WRITABLE=auto|1|0` overrides.
- Writable: B2 direct publish.
- Read-only with `POOL_CONTRIB_URL`: proxy `PUT /v1/pool/{fp}/nvmt` to that host (same content type, bounded body, no double buffering) and relay the response. Do not index the proxied package locally from the response; it arrives through the normal channel refresh.
- Read-only without `POOL_CONTRIB_URL`: return `403 read_only_channel` with `{"error":"read_only_channel","contribute_url":"<host or librarian URL>"}`. Do not use an HTTP redirect; the client refuses redirects today.
- `doctor` prints `channel_writable`, the detected permission, and the effective contribution path.
**Acceptance:** a bot without post rights starts read-only and still serves reads; a configured proxy publishes on the host and the read-only node sees it after refresh; an unconfigured read-only node gives a clear 403.
**Tests:** permission matrix, proxy success/failure, missing intake config. **Depends:** B2, B5. **Size:** M.

---

## Epic C — Contributions and trust

### C1 — Librarian accepts NVMT packages plus manifests
**Why:** the librarian only accepts single JPEG tiles with v1 captions (`contribution.py:35-100`), so a normal client cannot contribute a package through Telegram. This is the Telegram-native variant of the foreign-channel intake in B7: the contributor DMs the package, the librarian validates and reposts into the channel.
**Scope:** `contribution.py`, `channel_index.py`, `telegram_mt.py`.
**Do:**
- Accept a document with `application/vnd.openseek.nvmt` + a hints caption; validate the package, split into tiles, repost each tile + one manifest.
- Keep the existing single-tile path.
- Enforce sender rate limits before download, bounded download size, and per-package tile caps.
- Reject a package whose hints are missing or malformed with a clear DM/log reason.
**Acceptance:** a DM package is discoverable by every node after refresh; a bad package is rejected without partial publication.
**Tests:** fake store; valid package, wrong MIME, oversized, malformed hints, partial tile failure. **Depends:** B3. **Size:** L.

### C2 — Contribution trust and quotas
**Why:** with no hash, a bad contributor can poison a popular title. The mitigation must be cheap and non-blocking.
**Scope:** `contribution.py`, `pool.py`, `db.py`, `config.py`.
**Do:**
- Per-sender: existing 1 msg/s + 60/min; add per-title cap (e.g. 3 competing packages per `work key` per day).
- `verified` flag on `pool_hints` for moderators/trusted contributors; ranking prefers verified only as a tie-break, never hides unverified.
- Per-device rejections (C3) are the correction path.
- Admin CLI: list/revoke a contributor, mark verified.
**Acceptance:** a flood is throttled; a rejected package does not overwrite an existing one; a verified flag changes tie-break only.
**Tests:** quota unit tests, first-writer-wins under race. **Depends:** C1. **Size:** M.

### C3 — Per-device offsets and rejections
**Why:** approximate reuse needs a correction path that does not require re-encoding.
**Scope:** `pool.py`, `app.py`, `db.py`, `docs/api/HINTS-v2.md`.
**Do:**
- `PUT/DELETE /v1/previews/{id}/offset` equivalents under `/v1/pool/candidates/{fp}/offset` and `/reject`, keyed by device + work key + fp.
- Reject hides that `fp` for that device and work key for N days; offset shifts `b_ms` for that device only.
- No global effect. No re-ranking for other devices.
**Acceptance:** a rejection changes only the calling device's next candidate; an offset survives restart.
**Tests:** device isolation, expiry. **Depends:** A4. **Size:** M.

---

## Epic D — Nuvio client

### D0 — Commit the pending label rename first
**Why:** the tree has 35 modified files (OpenSeek→SeekDB labels) and the hint diff must be reviewable.
**Scope:** `Nuvio-native-tizen-fixes`.
**Do:** review and commit the label-only changes as one commit before touching `minipool.c`/`miniatura.c`.
**Acceptance:** `git status` clean; hint work starts from a known base. **Depends:** none. **Size:** S.

### D1 — Work key for movies (the actual bug)
**Why:** `minipool_episode_key` returns 0 when `episodio <= 0` (`src/minipool.c:361-389`), so films never enter the candidate path and every source is a fresh miss.
**Scope:** `src/minipool.c`, `src/minipool.h`.
**Do:** rename to `minipool_work_key`; allow `(imdb, 0, 0)` for films; keep the existing hash input format for series compatibility, add a `openseek-work-v1` variant for films. Keep the 64-hex output.
**Acceptance:** a film with a valid IMDb produces a key; a film without IMDb produces 0 and the client falls back to hints-only lookup.
**Tests:** unit cases: movie, series, id-with-suffix, no IMDb, invalid IMDb. **Depends:** D0. **Size:** S.

### D2 — Send raw hints on lookup and upload
**Why:** the server has nothing to match on today; `minipool_enviar` sends only `duration_ms` + `episode_key` (`src/minipool.c:539-575`).
**Scope:** `src/minipool.c`, `src/minipool.h`.
**Do:**
- Add URL-encoded `imdb`, `season`, `episode`, `duration_ms`, `size_bytes`, `filename` to the candidate GET and the NVMT PUT.
- Source them from `MiniMeta` (`imdb`, `temporada`, `episodio`, `durMs`) and the work item (`w->nome`, `w->bytes`/`l->total`).
- Add a bounded percent-encoder; cap filename at 200 bytes.
- Keep the old request as fallback when the server returns 404/400 for unknown params.
**Acceptance:** a captured request shows the full hint set; a server without the new route still works.
**Tests:** request-builder unit tests with spaces/UTF-8/special chars. **Depends:** D1, A1. **Size:** M.

### D3 — Accept the ranked candidate without sample verification
**Why:** the current loop fetches up to 8 candidates and decodes/compares frames (`src/miniatura.c:702+`), which is exactly the request-time cost the user rejected.
**Scope:** `src/miniatura.c`, `src/minipool.c`.
**Do:**
- Call the candidates endpoint once; take `candidates[0]` when `score >= 60` and `relation != length_differs`.
- Validate structurally only: NVMT magic/version/grid, slot bounds, tile count, package duration.
- On structural failure or HTTP error, try the next candidate (max 2), then fall back to local generation.
- Remove the frame-compare loop from the normal path; keep it behind a debug flag.
- Log one line: `hint hit fp=... score=... relation=...`.
**Acceptance:** second source of the same film downloads one package and starts previews without decoding any local frame; wrong candidate falls back safely.
**Tests:** fake server returning a ranked candidate; corrupt package; score below threshold; no candidates. **Depends:** D2, A4. **Size:** L.

### D4 — Apply linear timing mapping
**Why:** the client scales by `pts * j->dur / sourceDuration` (`src/miniatura.c:661`), which cannot express a speed ratio or an offset.
**Scope:** `src/miniatura.c`, `src/minipool.h`.
**Do:** carry `a_num/a_den/b_ms` in `MpoolCandidate`; map `pts -> (pts * a_num) / a_den + b_ms` with 64-bit math; clamp to `[0, durMs]`; skip out-of-range tiles.
**Acceptance:** 24/25 candidate maps to the right slots; same-length candidate is identity (`a=1,b=0`).
**Tests:** unit table for 24/25, 25/24, offset, clamping. **Depends:** D3. **Size:** M.

### D5 — Reject/offset UI (phase 2)
**Why:** the correction path is what makes no-verification acceptable.
**Scope:** `src/miniatura.c`, `src/ajustes.c`, `src/ajustes_ux_tela.inc`, i18n files.
**Do:** a small "wrong previews?" action on the player overlay: shift ±, reject this source, retry. Wire to C3 endpoints. Persist per-device choice.
**Acceptance:** rejection re-requests the next candidate; offset persists across restart; no crash on offline.
**Depends:** D4, C3. **Size:** M.

### D6 — Client test harness
**Why:** `minipool.c` currently has no unit harness for URLs/keys.
**Scope:** new `tests/` target or a small host-side test binary.
**Do:** extract pure helpers (key derivation, URL build, mapping) behind `#ifdef` test hooks; add a `make test-minipool` target.
**Acceptance:** `make test-minipool` runs without a TV. **Depends:** D1, D2. **Size:** M.

---

## Epic E — Docs and migration

### E1 — Rewrite `PLAN-SEEKDB.md`
**Why:** the current plan says to leave `library.py` behind, treats `episode_key` as solved, and makes Telegram cold storage. All three are now wrong.
**Scope:** `docs/PLAN-SEEKDB.md`.
**Do:** replace with the channel-first spec: hints manifest, candidate ranking, no hash, NVMT-only, central host optional, serverless deferred. Link this ticket file.
**Acceptance:** no statement contradicts A1–B7; the "leave behind" list no longer includes the hints layer.
**Depends:** A1. **Size:** M.

### E2 — Operator docs for channel mode
**Scope:** `docs/INSTALL.md`, `docs/TELEGRAM-POOL.md`, `.env.example`.
**Do:** env-only setup, bot membership, first scan, doctor output, librarian contribution rules, quota defaults.
**Acceptance:** a new operator can go from empty env to a serving node without reading code. **Depends:** B5. **Size:** M.

### E3 — Migration and backfill note
**Why:** existing channels have v1 tiles with no manifests.
**Scope:** `docs/TELEGRAM-POOL.md`, optional `cli.py` command.
**Do:** document that v1 packages remain exact-`fp`-only. Optional `backfill-hints` command that reads a sidecar file of `fp -> hints`; never guesses.
**Acceptance:** no automatic migration; an operator can opt in. **Depends:** B3. **Size:** S.

---

## Deferred (explicitly not ticketed now)

- **Serverless / Vercel**: blocked on a v2 client (CA TLS, redirects, presigned or multipart upload). The current client pins SPKI, refuses redirects, and sends up to 16 MiB in one PUT.
- **`/v1/previews` + `/v1/lookup` + VTT**: Nuvio renders NVMT only; adopting the preview API needs a VTT parser and sprite cropper. Out of scope.
- **Central host removal**: `host` stays as an optional profile.
- **Full-file hash**: rejected; `fp` is already the exact-blob key and the pool never sees the bytes.

---

## Suggested first wave

| Lane | Tickets | Unblocks |
|---|---|---|
| Protocol | A1 | everything |
| Server | A2, A3, A4, A5 | candidate reuse in host mode |
| Client | D0, D1 | movie key, reviewable diff |
| Channel read | B1, B3, B4 | channel nodes serving their own TVs |
| Channel write | B2, B7 | direct publish on a hosted channel; clean 403 or proxy on a foreign one |
| Contributions | C1, C2 | librarian intake for foreign channels |
| Client | D2, D3, D4 | end-to-end hint reuse |
| Phase 2 | C3, D5 | offsets and rejections |

Run server A2–A5 and client D0–D1 in parallel after A1. Start B1 after A2, B2 after B1, B7 after B2/B5, C1 after B3. Start D2 after A1; D3 after A4.
