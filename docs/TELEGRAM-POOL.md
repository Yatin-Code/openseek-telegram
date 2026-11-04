# Telegram shared tile pool

## Work split

Each OpenSeek instance resolves its own source, probes it, decodes frames and creates the 320x180 JPEG tiles.
The pool never receives a video URL. It accepts immutable tiles as Telegram documents. Bot API host mode indexes
Telegram file IDs in local SQLite; channel mode indexes Telegram message IDs in local SQLite or Vercel's Supabase
PostgreSQL. Players continue to use the requesting instance's normal VTT and capability URLs; neither Telegram
credentials nor channel membership are exposed to a player. In Bot API host mode, the request path is:

```text
player -> local OpenSeek -> shared pool API -> Telegram private channel
                         <- exact JPEG tile <----
local OpenSeek -> shared pool API -> Telegram private channel (only when sharing is enabled)
```

In channel mode there is no separate pool API host: each OpenSeek instance reads and, when authorized, writes to the
Telegram channel directly. Its SQLite or Supabase index can be rebuilt from channel history.

The pool matches only the exact OpenSeek fingerprint plus profile and tile index. It does not merge encodes by
title. OpenSeek's existing title-based library remains an explicitly approximate, separate feature.

## Local/VPS and Vercel channel profiles

Use `OPENSEEK_POOL_MODE=channel` for the Telegram-backed pool. Local/VPS and Vercel use the same Telegram channel and
browser dashboard, with different rebuildable state:

| Profile | Index and upload state | Dashboard |
|---|---|---|
| Local/VPS (`OPENSEEK_PROFILE=local`) | SQLite in the protected `OPENSEEK_DATA_DIR`; no Supabase required | Loopback admin port 8788; use an SSH tunnel to reach a VPS dashboard |
| Vercel (`OPENSEEK_PROFILE=vercel`) | Supabase PostgreSQL; Supabase Storage is temporary upload staging | Project root URL; Vercel Cron advances scans and publication retries |

Telegram is the durable package store for both. The local/VPS first-run dashboard saves Telegram settings in the private
`<data>/.env` file, verifies channel access, and starts the initial scan. Run `python -m openseek dashboard` on the host
to open it. See [INSTALL.md](INSTALL.md#localvps-on-linux) for a Linux service and VPS tunnel. Vercel does not accept
Telegram settings in the browser: create the Supabase schema and private staging bucket, then add the required values
to the Vercel Production environment and redeploy. The full Vercel steps and environment-variable list are in
[VERCEL.md](VERCEL.md). Channel setup, membership, and first-scan checks are in
[Telegram membership and first scan](#telegram-membership-and-first-scan).

The Vercel profile requires an owner secret in `OPENSEEK_ADMIN_TOKEN` (at least 16 characters). Open the deployed root
URL with that secret in the URL fragment once: `https://your-project.vercel.app/#<OPENSEEK_ADMIN_TOKEN>`. The dashboard
keeps it in that browser's session storage and removes the fragment from the address bar. This owner secret protects
dashboard operations; Nuvio pool read/write keys and paired-device tokens are separate credentials. Local/VPS generates
the owner secret in `<data>/admin.token` unless `OPENSEEK_ADMIN_TOKEN` is set.

## Host configuration (Bot API host mode)

The shared pool can run on an existing OpenSeek host. Set these variables on that host before starting OpenSeek:

```sh
export OPENSEEK_POOL_ENABLED=1
export OPENSEEK_POOL_MODE=host
export OPENSEEK_POOL_READ_KEY='replace-with-at-least-24-random-characters'
export OPENSEEK_POOL_WRITE_KEY='use-a-different-24-plus-character-secret'
export OPENSEEK_TELEGRAM_BOT_TOKEN='keep-this-only-on-the-pool-host'
export OPENSEEK_TELEGRAM_CHANNEL_ID='-1001234567890'
export OPENSEEK_PUBLIC_PORT=8789
export OPENSEEK_PUBLIC_HOST='pool.example.org:443'
export OPENSEEK_TLS_CERT_FILE='/etc/letsencrypt/live/pool.example.org/fullchain.pem'
export OPENSEEK_TLS_KEY_FILE='/etc/letsencrypt/live/pool.example.org/privkey.pem'
```

Create a Telegram bot with BotFather, create a **private** channel, and add the bot as an administrator allowed to
post. Keep the bot token on the pool host. The host needs a trusted HTTPS endpoint reachable by participants; expose
the guest listener through a reverse proxy or configure the matching trusted TLS certificate/key pair above. Do not
expose the loopback admin listener.

This repository includes `docker-compose.pool.yml` as a reference for a dedicated Linux pool host. It binds the owner
API to `127.0.0.1:8787`, publishes only the guest listener on HTTPS port 443, limits the container to 512 MiB, and
keeps its data in a named volume. It expects a mode-0600 `.pool-secrets/host.env` containing the host settings above
and trusted certificate files at `/opt/openseek/certs/{fullchain,privkey}.pem`, readable by the container's service
group. Run it with `docker compose -f docker-compose.pool.yml up -d --build`. Configure certificate renewal to copy
the renewed files into that mounted directory and restart the service.

Keep read and write keys separate. Share the read key with instances allowed to use the pool; share the write key
only with instances whose owners have opted in to contribute. Both are bearer secrets. Rotate them if a participant
is removed. This first version uses shared pool keys rather than per-instance accounts, so only share the write key
with trusted operators.

## Participant configuration

Each participating host continues to run its own OpenSeek instance and performs all video work locally. Configure:

```sh
export OPENSEEK_POOL_URL='https://pool.example.org'
export OPENSEEK_POOL_MODE=host
export OPENSEEK_POOL_READ_KEY='the-pool-read-key'
```

To opt into sharing generated tiles, also configure:

```sh
export OPENSEEK_POOL_WRITE_KEY='the-pool-write-key'
export OPENSEEK_POOL_SHARE=1
```

Pool reads are opportunistic: before locally decoding a scheduled frame, OpenSeek requests the exact tile from the
pool. A miss or pool outage falls back to local generation. Writes use a durable SQLite outbox and retry with
backoff; they do not block video decoding. A 409 conflict means another contributor already published different
bytes for that exact tile key; the pool keeps the first tile.

## Storage and edge cases

* Tiles are uploaded with Bot API `sendDocument` to preserve JPEG bytes without photo resizing. SQLite maps
  `(fingerprint, profile, tile index)` to Telegram file/message IDs and the expected digest.
* The first accepted tile fixes the media duration and tile count for that fingerprint/profile. Conflicting
  variants and conflicting duplicate tiles are rejected. Different encodes remain separate fingerprints.
* Each upload validates fingerprint, profile, duration, expected grid count, timestamp, JPEG decoding, dimensions,
  and size. Downloads verify the stored SHA-256 before returning bytes.
* Telegram is not the lookup index. If a Telegram upload succeeds but the following SQLite commit fails, an
  unindexed Telegram message may remain. Its caption includes the pool key and digest for later cleanup.
* The pool is optional. If `OPENSEEK_POOL_MODE` is unset, OpenSeek is local-only unless the legacy
  `OPENSEEK_POOL_ENABLED=1` switch selects host mode. A `POOL_URL` by itself no longer enables pool traffic.
* A source that is a different release, resolution, or duration will normally have a different fingerprint. Any
  cross-release reuse stays in OpenSeek's approximate metadata path and must not be presented as exact.

## Local end-to-end testing

The tests use a local Bot API-compatible fake transport and two independent OpenSeek databases. The end-to-end
case runs the real `Source` local-generation/outbox path, OpenSeek's ASGI middleware and pool routes, the Telegram
document upload/download adapter, and a second instance's HTTP tile lookup/cache path, using a deterministic fake
engine. If `/home/yat/sample.mkv` exists, an additional smoke test runs OpenSeek's real PyAV extractor on that
file and transfers the resulting JPEG through the same pool route path. Telegram storage is simulated by a local
Bot API-compatible fake; these tests need no real Telegram token, channel, public VPS, invite, or network socket.

```sh
cd backend
python -m pytest tests/test_pool.py
```

Real Telegram smoke testing is a separate deployment check and requires a bot token and channel ID configured in
the pool host's environment. Never put a bot token in a player URL, VTT, client configuration, Git, or issue report.

## Channel mode (no pool API host)

Set `OPENSEEK_POOL_MODE=channel` on every participating OpenSeek. Each instance indexes one private Telegram group or
channel, downloads documents by message ID, and caches validated bytes in its ordinary tile cache. There is no
separate pool API host in this mode. The channel is the source of truth; the local/VPS index is rebuildable SQLite,
while Vercel keeps its rebuildable index in Supabase PostgreSQL. Scans read bounded pages and continue after the last
indexed Telegram message ID, so a channel with a long history may take several passes.

### Telegram membership and first scan

Create a private Telegram channel or supergroup. Get the Telegram API ID and API hash from
[my.telegram.org](https://my.telegram.org/apps). Choose one identity:

- **Bot:** create it with BotFather and ask a Telegram channel/group admin to add it. Grant access to channel history.
  Grant posting permission only if this OpenSeek instance should publish directly. A bot cannot join a private invite
  link by itself.
- **User session:** the account must be a member and able to read history. Direct group posting also needs Send Messages
  and Send Media rights. A user session can join a private supergroup with an invite link.

For a local/VPS first run, start OpenSeek in the default `local` profile with no channel settings, then open
`python -m openseek dashboard`. Enter the channel ID, API ID/hash, and either the bot token or a Telegram StringSession
in the setup form. It checks history access and posting rights, then runs an initial scan. The form saves credentials to
`<data>/.env` with private permissions and reports whether the channel can be written. `OPENSEEK_CONTRIBUTION` starts
off; enable it only after deciding which upload path the instance should use.

For Vercel, add the channel ID, API ID/hash, and bot token (or a Telegram StringSession) to the Production environment
as described in [VERCEL.md](VERCEL.md). A session file cannot be used on Vercel because function instances are
disposable. After deployment, opening the dashboard's pool status or waiting for the once-per-minute Vercel tick starts
the bounded indexing passes. Use the dashboard's **Scan** control to request an immediate pass.

The separate CLI setup wizard is also available:

```sh
python -m openseek setup
```

Choose **Join a community pool** to log in with a Telegram user session, join a private-supergroup invite, and write
`openseek.env` with mode `0600`. It does not modify `<data>/.env`; it prints the command to load its output. For an
existing bot or a channel where the account is already a member, use the browser setup or environment configuration
instead.

The equivalent user-session configuration is:

```sh
OPENSEEK_POOL_MODE=channel
OPENSEEK_TELEGRAM_API_ID=123456
OPENSEEK_TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
OPENSEEK_TELEGRAM_SESSION_FILE=/var/lib/openseek/telegram/openseek.session
OPENSEEK_TELEGRAM_CHANNEL_ID=-1001234567890
OPENSEEK_CONTRIBUTION=1
```

Get the API ID and hash from Telegram's API development page. For a local/VPS user account, the session file is created
under the data directory by default and is restricted to mode `0600`. To authenticate manually, make
`OPENSEEK_POOL_MODE=channel`, `OPENSEEK_DATA_DIR`, and `OPENSEEK_ENV_FILE` available to the CLI, then run
`python -m openseek pool login`. A channel-mode instance needs read access to the private channel even when it sends
contributions through a librarian. Vercel requires a bot token or StringSession; a local session file is not durable
across function invocations.

`OPENSEEK_CONTRIBUTION=1` opts into channel-mode contribution. Direct group posting requires a user session with Send
Messages and Send Media rights, or a bot that an administrator added with posting permission. For an always-on
self-hosted Telegram relay, contributor instances set `OPENSEEK_LIBRARIAN=@pool_librarian_bot`; human contributors
DM the bot, which validates and reposts the document. The relay must run with `OPENSEEK_LIBRARIAN_ENABLED=1`, a bot
token, MTProto API credentials, and write permission in the shared channel. Telegram does not allow a bot to DM
another bot. A relay accepts only
OpenSeek tile/package documents with valid metadata and digests; it deletes malformed submissions and temporarily
blocks senders after invalid or excessive traffic. Keep it open only if the pool's operators accept open contributions.

Librarian defaults are 1 submission per sender per second, 60 submissions per minute across the relay, and at most 3
packages per sender/work key/UTC day when hints identify an IMDb work or episode. Rate-limited senders and senders
whose submitted metadata or digests fail validation are blocked for 60 seconds; a daily-quota rejection does not block
the sender. NVMT packages have a default cap of 1500 tiles; set
`OPENSEEK_POOL_LIBRARIAN_MAX_PACKAGE_TILES` in the process environment to change it. Use
`python -m openseek pool contributor list` to inspect recorded package quotas and blocked senders;
`python -m openseek pool contributor block <sender_id>` and `unblock` manage a sender block.

An HTTPS librarian URL receives a `PUT /v1/tiles/{fp}/{k}.jpg` with `image/jpeg`, the tile caption in
`X-OpenSeek-Caption`, and `pts_ms`, `duration_ms`, and `n_tiles` query values. It must return JSON containing the
reposted channel `message_id`; an optional `OPENSEEK_LIBRARIAN_KEY` is sent as a bearer token. The endpoint is an
external relay and is not exposed by channel-mode OpenSeek.

Use these commands to inspect or repair the local index:

```sh
python -m openseek pool status
python -m openseek pool scan
python -m openseek pool rebuild
```

Run `python -m openseek doctor` for a health summary. Look for `"daemon": "running"`, `history_readable: true`, a
resolved channel entity, and a recent `scanned_at`; `tiles`, `hints`, and `last_message_id` show indexing progress.
`channel_writable: false` means the instance can read but cannot post directly. `last_error` is empty when the most
recent pool operation succeeded. `python -m openseek pool status` returns the pool status without the doctor summary.
If doctor reports `daemon: unavailable`, check that OpenSeek is running and that the command uses the service's same
`OPENSEEK_DATA_DIR` and `OPENSEEK_ENV_FILE`. On Vercel, use the browser dashboard's pool status and Scan/Rebuild
controls; the local CLI connects only to a loopback admin port.

The local/VPS loopback admin dashboard also shows pool mode and has Scan and Rebuild controls. Channel-mode uploads
keep the caption `openseek:v1:<fp>:<profile>:<k>:<sha256>`; their Telegram document filenames carry the timestamp and
media duration needed for NVMT subsets. Older host-mode posts keep working for tile reads, but their captions contain no
duration metadata, so they cannot be used to build exact NVMT packages until a metadata-bearing duplicate appears.

### Vercel temporary upload staging

Local/VPS sends an NVMT package directly to OpenSeek. Vercel stages package bytes in the private Supabase bucket to
avoid the function request-body limit: OpenSeek issues a signed upload URL, the uploader sends bytes to that URL,
finalizes the upload, and polls its upload ID. Staging URLs and pending objects expire after 15 minutes; the scheduled
tick cleans up expired objects. The bucket file limit and OpenSeek package ceiling are both 16 MiB.

An upload is accepted only after Telegram accepts the package. Supabase PostgreSQL stores its status and publication
retry queue, not a durable package copy. Vercel Cron runs every minute, advances channel-index scans, publishes a
bounded batch, and retries temporary failures. Downloads in both profiles stream from Telegram and do not use Storage.

Channel mode ignores legacy host URL/key settings. With mode unset, host settings are ignored unless
`OPENSEEK_POOL_ENABLED=1`; `host` keeps the HTTP pool API and Bot API store. `python -m openseek pool status` reports
the selected mode without returning tokens or session data.

## Existing packages and hint backfill

Packages published before the NVMT hint manifest are still available by their exact `fp` and profile. They do not
become cross-release candidates merely because an index is rebuilt. A scan or rebuild never derives IMDb IDs,
season/episode numbers, duration, file size, or title from a video, Telegram filename, or other guessed metadata.

An operator can opt an individual existing package into hint matching when they have authoritative metadata. Keep a
sidecar mapping from the exact package identity to the supplied values, for example:

```json
{
  "fp": "64-lowercase-hex-characters",
  "profile": "nvmt-v1-g5",
  "imdb": "tt1234567",
  "season": 2,
  "episode": 3,
  "duration_ms": 2670000,
  "size_bytes": 1234567890,
  "filename": "Show.S02E03.mkv"
}
```

For a channel pool, retrieve that same NVMT package by its exact `fp` and profile, then have a writable channel node
accept it again through `PUT /v1/pool/{fp}/nvmt` with the sidecar values as query parameters. The node validates
the package and metadata, keeps the existing `fp`, and publishes the hint manifest to Telegram. The duration must
match the package; `size_bytes` and `filename` must describe the original media from a trusted catalog. If that
metadata is unavailable, leave the package exact-`fp`-only. There is no automatic or bulk backfill command.
