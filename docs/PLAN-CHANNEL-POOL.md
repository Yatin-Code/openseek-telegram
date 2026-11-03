# Plan: channel-only Telegram tile pool (`POOL_MODE=channel`)

> Superseded by [PLAN-SEEKDB.md](PLAN-SEEKDB.md). This document is kept for the
> Telegram channel-mode research and the current implementation details.

Status: superseded
Scope: `/home/yat/openseek-telegram`

## Goal

Keep one shared OpenSeek tile pool without a pool host or Azure. Every instance
indexes the same private Telegram group/channel locally, contributes tiles to it,
and serves its own TVs. No pool API is required.

Contribution must be smooth: join a private group by invite link, or send tiles
to a librarian bot, with no per-user approval from the pool owner. The
user-facing `POOL_MODE` exposes only `host` and `channel`; leaving it unset
means local-only.

## Deployment reality

Original OpenSeek is a long-running Python/FastAPI service: PyAV workers, local
disk cache, range proxy, and background jobs. It is not a Vercel/serverless app.
It runs as a Windows service/installer, a Python daemon, or Docker on a PC, NAS,
or VPS. `openseek-telegram` keeps the same runtime; this plan only changes pool
configuration and adds MTProto.

## What exists today

`POOL_ENABLED=1` runs a pool API on one host: `PoolService` +
`TelegramBotStore` + SQLite (`pool_tiles`, `pool_packages`,
`pool_episode_packages`, `pool_outbox`). Participants set `POOL_URL`,
`POOL_READ_KEY`, and optionally `POOL_WRITE_KEY` + `POOL_SHARE=1`.
`PoolClient` reads; `PoolSync` publishes from a durable outbox.

`jobs.py` already calls `pool_client.get_tile()` and
`pool_sync.enqueue()`, so a channel backend can plug into the same
interfaces without touching the scheduler.

## Target behaviour

`OPENSEEK_POOL_MODE` has two user-facing values. If it is unset or empty,
OpenSeek is **local-only**: no pool API, no Telegram connection, no scanning,
no contribution. There is no selectable `off` mode in setup or docs.

| Mode | Pool API | Index | Storage |
|---|---|---|---|
| `host` | yes, on this instance | local SQLite on host | Telegram bot channel |
| `channel` | none | local SQLite on every instance | shared Telegram group/channel |

`host` is the current Option A. It keeps working unchanged and remains the
default public-pool option: people paste its URL into the TV settings.

`channel` is the new Option B:

1. Each instance uploads generated tiles to the shared group/channel.
2. Each instance scans it and builds `pool_channel_tiles`.
3. Reads consult the local index, then download the tile by `message_id`.
4. Tiles are cached locally and served to the TV by the local OpenSeek.
5. The first valid message for `(fp, profile, k)` wins. Later conflicting
   uploads are ignored and may be deleted.
6. A cold instance rebuilds its index by scanning the group/channel. Telegram is
   the source of truth; SQLite is a rebuildable materialised view.

## Key decisions

- Use Telethon (MTProto) for group/channel history and downloads. The Bot API
  cannot list old posts and its `file_id` is bot-specific.
- Keep `TelegramBotStore` for `host`; add `TelegramMTProtoStore` for `channel`.
- Support both Telegram identities: a **bot token** (an admin must add the bot
  once) and a **user session** (can join by invite link and DM the librarian).
- Contribution paths:
  - **Private group with member posting**: join by invite link, post tiles
    directly. No per-user owner approval.
  - **Librarian bot**: send tiles to one bot; it validates and reposts them.
    No group join and no per-user owner approval, but the bot is a small upload
    relay.
- A librarian bot is **not** a pool host: it keeps no shared index and serves no
  pool API. It only accepts, validates, and reposts tiles.
- Session files and bot tokens are 0600 and never logged.
- Reuse the existing `PoolClient` / `PoolSync` interfaces. `jobs.py` only sees a
  backend object, not HTTP.
- Caption format stays:
  `openseek:v1:<fp>:<profile>:<tile_index>:<sha256>`.
- No new public HTTP endpoints in `channel` mode.

## Data model

Add to `backend/openseek/db.py`:

```sql
CREATE TABLE IF NOT EXISTS pool_channel_tiles (
  fp TEXT NOT NULL,
  profile TEXT NOT NULL,
  k INTEGER NOT NULL,
  pts_ms INTEGER NOT NULL,
  duration_ms INTEGER NOT NULL,
  n_tiles INTEGER NOT NULL,
  size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  message_id INTEGER NOT NULL,
  created REAL NOT NULL,
  PRIMARY KEY (fp, profile, k)
);

CREATE INDEX IF NOT EXISTS pool_channel_tiles_fp
  ON pool_channel_tiles(fp, profile, k);

CREATE TABLE IF NOT EXISTS pool_channel_state (
  channel_id TEXT PRIMARY KEY,
  last_message_id INTEGER NOT NULL DEFAULT 0,
  scanned_at REAL NOT NULL DEFAULT 0
);
```

`pool_tiles`, `pool_packages`, and `pool_episode_packages` remain for `host`
mode. In `channel` mode, package/episode lookup is built from
`pool_channel_tiles` on demand, the same way `PoolService._package_slots_from_tiles`
already works.

## Configuration

Add to `backend/openseek/config.py`:

| Variable | Default | Meaning |
|---|---|---|
| `OPENSEEK_POOL_MODE` | unset | `host` or `channel`; unset = local-only |
| `OPENSEEK_TELEGRAM_BOT_TOKEN` | empty | bot identity for `host`, `channel`, or librarian |
| `OPENSEEK_TELEGRAM_CHANNEL_ID` | empty | target group/channel ID |
| `OPENSEEK_TELEGRAM_API_ID` | empty | MTProto app id |
| `OPENSEEK_TELEGRAM_API_HASH` | empty | MTProto app hash |
| `OPENSEEK_TELEGRAM_SESSION` | empty | session string, or path to a session file |
| `OPENSEEK_TELEGRAM_SESSION_FILE` | empty | optional explicit file path |
| `OPENSEEK_POOL_CHANNEL_SCAN_LIMIT` | `2000` | max messages per scan pass |
| `OPENSEEK_POOL_INDEX_REFRESH_S` | `300` | background rescan interval |
| `OPENSEEK_CONTRIBUTION` | `0` | opt in to publishing generated tiles |
| `OPENSEEK_LIBRARIAN` | empty | `@bot` or HTTPS URL for the librarian |
| `OPENSEEK_LIBRARIAN_KEY` | empty | optional bearer key for an HTTP librarian |
| `OPENSEEK_LIBRARIAN_ENABLED` | `0` | accept contributions and repost them |

Validation:

- `host`: requires `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`,
  `POOL_READ_KEY`, `POOL_WRITE_KEY` (current rules).
- `channel`: requires `TELEGRAM_CHANNEL_ID` and one identity (bot token or
  session). It ignores `POOL_URL`, `POOL_READ_KEY`, and `POOL_WRITE_KEY`.
- `OPENSEEK_CONTRIBUTION=1` requires an identity and either
  `LIBRARIAN=@bot` or group membership with post permission.
- `OPENSEEK_LIBRARIAN_ENABLED=1` requires a bot token, a channel ID, and a
  private group/channel where the bot can read history and post.
- Unset `POOL_MODE` means **local-only**: no pool API, no Telegram, no
  scanning, no contribution. There is no selectable `off` mode.
- If `POOL_MODE` is unset and `POOL_ENABLED=1`, run `host` (backward
  compatibility). Otherwise the instance stays local.

Session file path defaults to
`$OPENSEEK_DATA_DIR/telegram/openseek.session`, mode 0600. Never log it.

## Backward compatibility and coexistence

Option A is not replaced. `POOL_MODE=host` remains the current
`PoolService` + `TelegramBotStore` + HTTP pool API. A public instance still
works exactly as it does now: participants put its URL in the TV settings and
use the read/write keys.

Rules:

- If `POOL_MODE` is unset and `POOL_ENABLED=1`, run `host`.
- If `POOL_MODE=host`, all existing variables (`POOL_URL`, `POOL_READ_KEY`,
  `POOL_WRITE_KEY`, `POOL_SHARE`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHANNEL_ID`) keep their meaning.
- If `POOL_MODE=channel`, `POOL_URL` and the pool keys are ignored.
- A host instance and channel instances may use the same Telegram channel.
  Host tile uploads already use the caption
  `openseek:v1:<fp>:<profile>:<k>:<sha256>`, which the channel index can read.
  Host NVMT package uploads use a different caption and are ignored by the
  channel index.
- `jobs.py` and the TV protocol are unchanged in all modes.

Add a compatibility test: no `POOL_MODE`, `POOL_ENABLED=1` → host backend;
`POOL_MODE=channel` → no pool API; unset `POOL_MODE` → local-only.

## Telegram MTProto layer

Add `backend/openseek/telegram_mt.py`:

- `TelegramMTProtoStore` owns one Telethon client and one dedicated event-loop
  thread. Public methods are synchronous and use
  `asyncio.run_coroutine_threadsafe`.
- `start()`: connect, sign in with the bot token or user session, resolve
  `channel_id`, verify the account can read and (for uploads) post.
- `put_tile(data, caption) -> message_id`: upload a JPEG as a document.
- `get_tile(message_id) -> bytes`: fetch the message, download the media, and
  return bytes.
- `scan(min_id, limit) -> list[ChannelMessage]`: iterate channel history in
  ascending `message_id` order. Return `message_id`, caption, and document size.
- `delete_message(message_id)`: best-effort cleanup for conflicting duplicates.
- `close()`: disconnect and stop the loop.

Rules:

- Validate `caption` with the existing regex before upload.
- Verify `sha256` after every download before returning bytes.
- Bound downloads to `MAX_TILE_BYTES`.
- Retry transient `FloodWait` and network errors with backoff; never block the
  decode scheduler.
- A missing message is a cache miss, not a fatal error.

## Channel index

Add `backend/openseek/channel_index.py`:

- `ChannelIndex(db, store)` with `refresh(force=False)`.
- Reads `last_message_id` from `pool_channel_state`; scans messages after it.
- Parses captions with:
  `^openseek:v1:([0-9a-f]{64}):([^:]+):(\d+):([0-9a-f]{64})$`.
- Validates `fp`, `profile`, `k`, `sha256`; ignores malformed captions.
- Upserts with `INSERT OR IGNORE` so the first message per tile wins.
- Updates `last_message_id` and `scanned_at` in one transaction.
- `rebuild()` clears the channel table and scans from `min_id=0`.
- `lookup(fp, profile, k)` returns `message_id`, `pts_ms`, `duration_ms`,
  `n_tiles`, `sha256` or `None`.
- `manifest(fp, profile)` returns the same shape as `PoolService.manifest`,
  built from `pool_channel_tiles`.
- `package_slots(fp, profile, slots)` reuses the existing NVMT encoder logic.

The background refresh thread starts in `Core.start()` when
`POOL_MODE=channel`, and stops in `Core.stop()`.

## Contribution without owner approval

The goal is: a user adds the group invite link or the librarian’s username in
setup, enables contribution, and starts sharing. The owner does not approve
each user.

### Path A: private group with member posting

- The owner creates a **private supergroup**, not a channel.
- “Send messages” and “Send media” are enabled for members.
- The owner shares an **invite link**. No per-user approval is required.
- Each instance joins with a **user session**; Telegram bots cannot join a group
  through an invite link. A bot must be added by an admin.
- The instance posts tiles directly to the group.
- OpenSeek scans the group history, validates captions, and indexes valid tiles.
- The owner can ban a sender or rotate the invite link.

### Path B: librarian bot

- One bot accepts tile documents by DM, validates them, and reposts them to the
  private group/channel.
- Users need only the librarian’s username; no group join, no admin rights.
- A bot cannot DM another bot. Contributors must use a **user session**, or both
  bots must be in a group, or the librarian must expose an HTTP upload endpoint.
- The librarian is a small **upload relay**, not a pool host. It has no shared
  index and no pool API.
- It validates captions, JPEG bytes, SHA-256, size, and rate limits; it can ban
  abusive senders.

### Identity matrix

| Contributor identity | Join group by invite | Post directly | DM librarian |
|---|---|---|---|
| User session | yes | yes | yes |
| Bot token | no (admin adds it) | yes, once added | no |
| Bot in a group with librarian | no (admin adds it) | yes | via group |

### Validation and moderation

Every contribution path applies the same checks:

- Caption must match
  `^openseek:v1:([0-9a-f]{64}):([^:]+):(\d+):([0-9a-f]{64})$`.
- Fingerprint, profile, tile index, and SHA-256 must be valid.
- The document must be a JPEG of at most `MAX_TILE_BYTES`.
- `sha256(bytes)` must equal the caption digest.
- The first valid message for `(fp, profile, k)` wins; later conflicting
  messages are ignored and may be deleted.
- Messages with unknown captions are ignored, not rejected with an error.

Librarian-specific rules:

- Accept documents only, one tile per message.
- Rate-limit per sender and globally.
- Optional contributor allowlist; otherwise open with a shared invite code.
- On abuse: delete the message, ban the sender, and keep a short blocklist.

### Moderation without approval

The owner does not approve each contributor. The owner keeps control by:

- removing a message or tile;
- banning a sender in Telegram;
- rotating the invite link;
- deleting a librarian contributor from its allowlist.

This keeps onboarding smooth while preserving a kill switch.

## Local client and publisher

Add to `backend/openseek/pool.py` (or `channel_pool.py`):

- `ChannelPoolClient` implements the same surface as `PoolClient`:
  - `get_tile(fp, k) -> (bytes, pts_ms) | None`
  - `manifest(fp, profile)`, `package_slots(fp, profile, slots)`,
    `episode_candidates(episode_key, duration_ms)`
  - It uses `ChannelIndex.lookup()` then `TelegramMTProtoStore.get_tile()`.
  - It caches validated bytes in a bounded in-process LRU, and writes them to
    the normal OpenSeek cache so the TV path does not hit Telegram twice.
- `ChannelPoolSync` implements `enqueue(fp, k)` and publishes to the channel.
  - Reuse `pool_outbox` for retries, but add a `channel` flag or a separate
    `pool_channel_outbox` table to avoid mixing host and channel state.
  - Before upload, check `ChannelIndex.lookup()`; skip if a tile already
    exists.
  - On 409/conflict, delete the losing message if it is ours; otherwise ignore.
  - Never block decoding; uploads run on the existing publisher thread.

## App wiring

In `backend/openseek/app.py` `Core.__init__`:

```python
mode = settings.pool_mode
if mode == "host":
    self.pool = PoolService(...)
    self.pool_client = PoolClient(...)  # unchanged
    self.pool_sync = PoolSync(...) if settings.pool_share else None
elif mode == "channel":
    store = TelegramMTProtoStore(settings)
    index = ChannelIndex(self.db, store)
    self.pool = None
    self.pool_client = ChannelPoolClient(self.cache, index, store)
    self.pool_sync = ChannelPoolSync(self.db, self.cache, store, index) \
        if settings.pool_upload else None
```

- `Core.start()`: start the store, run an initial `index.refresh()`, start the
  refresh thread, then `pool_sync.start()`.
- `Core.stop()`: stop the sync, refresh thread, and store.
- Pool HTTP routes stay registered but return 404/503 when `core.pool is None`.
  No route changes are needed for channel mode.
- `jobs.py` needs no change: it already calls `get_tile()` and `enqueue()`.

## Onboarding and setup

Keep env-first configuration, but add one guided path so users do not hand-edit
20 variables.

`python -m openseek setup` (and the dashboard “Pool” card) asks only:

1. **Host a public pool** or **Join a community pool**.
2. For host: create bot, create private channel, generate read/write keys, print
   the URL and pin.
3. For join: choose **group invite** or **librarian bot**, log in with a user
   session or bot token, send one test tile, then enable contribution.
4. **Skip** leaves the instance local-only. It is not a third mode.

The setup command writes `openseek.env` (mode 0600), prints the exact `source`
line, and offers `openseek pool status`. It should not rewrite an existing
`.env`; it writes a separate file or prints the block.

Env-first installs remain supported: no wizard is required. The wizard only
reduces mistakes.

`OPENSEEK_CONTRIBUTION=1` is the single user-facing contribution switch. It
requires an identity and a target (group membership or librarian). The older
`OPENSEEK_POOL_SHARE` remains only for `host` clients.

## `.env` examples

A single template may contain both modes. The app validates only the selected
mode and ignores the other block, with a warning if it is non-empty.

```sh
# --- common ---
OPENSEEK_DATA_DIR=/var/lib/openseek
OPENSEEK_HOST=0.0.0.0
OPENSEEK_PORT=8787
OPENSEEK_ADMIN_PORT=8788

# host | channel; unset = local-only
OPENSEEK_POOL_MODE=channel

# --- Option A: public pool host ---
# OPENSEEK_POOL_ENABLED=1
# OPENSEEK_POOL_READ_KEY=...
# OPENSEEK_POOL_WRITE_KEY=...
# OPENSEEK_POOL_SHARE=0
# OPENSEEK_TELEGRAM_BOT_TOKEN=...
# OPENSEEK_TELEGRAM_CHANNEL_ID=-100...

# --- Option B: channel mode ---
# OPENSEEK_TELEGRAM_API_ID=...
# OPENSEEK_TELEGRAM_API_HASH=...
# OPENSEEK_TELEGRAM_SESSION_FILE=/var/lib/openseek/telegram/me.session
# OPENSEEK_TELEGRAM_CHANNEL_ID=-100...
# OPENSEEK_CONTRIBUTION=1
# OPENSEEK_LIBRARIAN=@openseek_pool_bot
# OPENSEEK_POOL_INDEX_REFRESH_S=300
# OPENSEEK_POOL_CHANNEL_SCAN_LIMIT=2000
```

### Contributor, user session + librarian

```sh
OPENSEEK_DATA_DIR=/var/lib/openseek
OPENSEEK_HOST=0.0.0.0
OPENSEEK_PORT=8787

OPENSEEK_POOL_MODE=channel
OPENSEEK_TELEGRAM_API_ID=123456
OPENSEEK_TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
OPENSEEK_TELEGRAM_SESSION_FILE=/var/lib/openseek/telegram/me.session
OPENSEEK_TELEGRAM_CHANNEL_ID=-1001234567890

OPENSEEK_LIBRARIAN=@openseek_pool_bot
OPENSEEK_CONTRIBUTION=1
OPENSEEK_POOL_INDEX_REFRESH_S=300
OPENSEEK_POOL_CHANNEL_SCAN_LIMIT=2000
```

### Librarian bot host

```sh
OPENSEEK_DATA_DIR=/var/lib/openseek
OPENSEEK_HOST=0.0.0.0
OPENSEEK_PORT=8787

OPENSEEK_POOL_MODE=channel
OPENSEEK_LIBRARIAN_ENABLED=1
OPENSEEK_TELEGRAM_BOT_TOKEN=123456789:ABC...
OPENSEEK_TELEGRAM_CHANNEL_ID=-1001234567890
OPENSEEK_CONTRIBUTION=0
```

### Public pool host (Option A, unchanged)

```sh
OPENSEEK_DATA_DIR=/var/lib/openseek
OPENSEEK_HOST=0.0.0.0
OPENSEEK_PORT=8787

OPENSEEK_POOL_MODE=host
OPENSEEK_POOL_ENABLED=1
OPENSEEK_POOL_READ_KEY=read-secret-at-least-24-chars
OPENSEEK_POOL_WRITE_KEY=write-secret-at-least-24-chars
OPENSEEK_TELEGRAM_BOT_TOKEN=123456789:ABC...
OPENSEEK_TELEGRAM_CHANNEL_ID=-1001234567890
```

## Admin and CLI

Add to `backend/openseek/cli.py`:

- `python -m openseek pool scan` — one scan pass; prints new tiles and state.
- `python -m openseek pool rebuild` — clears `pool_channel_tiles` and rescans
  the whole channel.
- `python -m openseek pool status` — mode, channel, last message id, tile count,
  outbox count, last error.
- `python -m openseek pool login` — interactive Telethon login for a user
  session (optional; bot sessions use the token).

Admin app (`create_admin_app`):

- `GET /admin/pool/status` — mode-specific status.
- `POST /admin/pool/scan` — force refresh.
- `POST /admin/pool/rebuild` — full rebuild.
- No purge of Telegram messages by default; add a dry-run only.

## Security

- Session file and token are secrets: 0600, never in logs, never in API
  responses.
- Channel must be private; the bot/user must be a member or admin.
- Validate every caption and every downloaded JPEG; verify SHA-256.
- Bound upload/download sizes with `MAX_TILE_BYTES` and `MAX_NVMT_BYTES`.
- Do not accept a `channel_id` from a request; it comes from config only.
- Rate-limit refresh and upload; handle `FloodWait` without crashing.
- A malicious channel member can post bad tiles; validation and SHA-256 reject
  them. A member can also spam; the index ignores malformed captions, and the
  channel owner can remove the member.

## Tests

Add `backend/tests/test_pool_channel.py`:

- Fake Telethon transport (in-memory channel) implementing `put_tile`,
  `get_tile`, `scan`, `delete_message`.
- Two independent DBs + two `ChannelPoolClient`/`ChannelPoolSync` instances.
- Scenarios:
  1. Instance A generates a tile, publishes it; B scans, finds it, downloads
     it, verifies bytes.
  2. First-writer-wins: A and B publish different bytes for the same key; B's
     index keeps A's message; B's upload is ignored/deleted.
  3. Rebuild: wipe B's DB, `rebuild()`, B finds the tile again.
  4. Malformed caption and bad SHA-256 are ignored.
  5. Outbox retry after a simulated network failure.
  6. `POOL_MODE=host` still passes the existing `test_pool.py` suite.
  7. Unset `POOL_MODE` starts no store and no threads (local-only).
  8. No `POOL_MODE` + `POOL_ENABLED=1` resolves to `host`.
  9. Private-group contribution: a user session joins, posts a tile, another
     instance scans and downloads it.
  10. Librarian contribution: a user session DMs a valid tile; the librarian
      validates and reposts it. A bot-to-bot DM is rejected.
  11. `CONTRIBUTION=0` publishes nothing.
  12. Irrelevant mode variables are ignored, with one warning.
- Run: `cd backend && python -m pytest tests/test_pool.py tests/test_pool_channel.py`.

## Rollout

1. Ship config + schema + Telethon behind `POOL_MODE=channel`. Leave
   `POOL_MODE` unset as local-only; `host` unchanged.
2. Document both contribution paths: private group with invite link, and
   librarian bot. State clearly that bots cannot join by invite link.
3. Test with two instances on two machines, a real private group/channel, and a
   real librarian bot.
4. Add the setup command, admin commands, and docs.
5. Keep `host` as the recommended default until `channel` has field testing.

## Acceptance criteria

- Two instances with separate SQLite DBs share tiles through one Telegram
  group/channel with no pool API and no Azure.
- Killing either instance does not stop the other from reading or writing.
- A fresh instance rebuilds its index from the group/channel and serves tiles.
- `POOL_MODE=host` and unset/local-only behave exactly as before.
- A contributor joins a private group by invite link or DMs the librarian, sets
  `OPENSEEK_CONTRIBUTION=1`, and publishes without owner approval.
- A bot that was not added by an admin cannot join or post; the docs say so.
- No bot token or session appears in logs or HTTP responses.

## Work breakdown

| Phase | Work | Files |
|---|---|---|
| 0 | Add `telethon` pin; config fields and validation; schema migration | `requirements.txt`, `pyproject.toml`, `config.py`, `db.py` |
| 1 | `TelegramMTProtoStore` (login, upload, download, scan) | new `telegram_mt.py` |
| 2 | `ChannelIndex` (scan, upsert, rebuild, manifest, packages) | new `channel_index.py` |
| 3 | `ChannelPoolClient` / `ChannelPoolSync`; contribution paths | `pool.py`, `app.py`, new `contribution.py` |
| 4 | Setup command and dashboard card; admin status/scan/rebuild | `cli.py`, `setup.py`, `app.py` |
| 5 | Docs, tests, two-instance smoke test, field test | `tests/test_pool_channel.py`, `README.md`, `docs/TELEGRAM-POOL.md` |

## Files expected to change

- `backend/openseek/config.py` — mode, MTProto settings, validation.
- `backend/openseek/db.py` — channel tables.
- `backend/openseek/telegram_mt.py` — new MTProto store.
- `backend/openseek/channel_index.py` — new index/scan.
- `backend/openseek/pool.py` — channel client/sync; shared validation helpers.
- `backend/openseek/contribution.py` — group and librarian contribution paths.
- `backend/openseek/setup.py` — guided setup and `.env` generation.
- `backend/openseek/app.py` — mode wiring, startup/shutdown, admin routes.
- `backend/openseek/cli.py` — `setup`, `pool scan|rebuild|status|login`.
- `backend/requirements.txt`, `backend/pyproject.toml` — Telethon.
- `backend/tests/test_pool_channel.py` — new tests.
- `README.md`, `docs/TELEGRAM-POOL.md`, `docker-compose.pool.yml` — setup.
- No changes required in `jobs.py` or the TV protocol.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Telegram rate limits / FloodWait | bounded scan, backoff, cache, never block decoding |
| Session or bot token theft | 0600 files, never log, never return in API |
| Spam or junk in an open group | caption/JPEG/SHA-256 validation, rate limits, ban, rotate invite |
| Orphaned Telegram message after DB failure | caption carries the key and digest; rebuild/cleanup command |
| New instance misses old history | MTProto scan + `pool rebuild` |
| Bot cannot join by invite | document it; use a user session or have an admin add the bot |
| Librarian becomes a single upload point | allow per-community librarians; reads never depend on it |
| Public channel exposes tiles | keep the group/channel private; share only an invite link |
| File reference expiry | re-fetch by `message_id`; cache bytes locally |

## Open questions

1. **Group or channel:** a private group is the only path that lets users join
   by invite link without owner approval. A channel needs per-bot admin
   promotion. Default to a group for smooth contribution; keep channel support
   for curated pools.
2. **Librarian ownership:** one shared librarian bot is simplest, but it is a
   small central upload point. Per-community librarians avoid that at the cost
   of more setup.
3. **Invite distribution:** public invite link is smoothest but lets anyone
   join. A rotating link or join-request link is safer. Decide the default.
4. **Identity default:** user session for smooth join and librarian DMs; bot
   token for owners who prefer not to store a user session. The setup wizard
   must explain that bots cannot join by invite link.
5. **Naming:** keep `OPENSEEK_CONTRIBUTION` as the user-facing switch, and keep
   `OPENSEEK_POOL_SHARE` only for the existing `host` client. Do not add a
   second contribution flag.
6. **Config output:** setup should write `openseek.env` and print a `source`
   line, not edit an existing `.env`.







