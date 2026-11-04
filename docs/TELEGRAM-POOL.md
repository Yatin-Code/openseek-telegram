# Telegram shared tile pool

## Work split

Each OpenSeek instance resolves its own source, probes it, decodes frames and creates the 320x180 JPEG tiles.
The pool server never receives a video URL. It accepts immutable tiles, stores them as Telegram documents, indexes
their Telegram `file_id` in its own SQLite database, and sends image bytes back to requesting OpenSeek instances.
Players continue to use the requesting instance's normal VTT and capability URLs; neither Telegram credentials nor
cache-channel membership are exposed to a player.

```text
player -> local OpenSeek -> shared pool API -> Telegram private channel
                         <- exact JPEG tile <----
local OpenSeek -> shared pool API -> Telegram private channel (only when sharing is enabled)
```

The pool matches only the exact OpenSeek fingerprint plus profile and tile index. It does not merge encodes by
title. OpenSeek's existing title-based library remains an explicitly approximate, separate feature.

## Host configuration

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

Set `OPENSEEK_POOL_MODE=channel` on every participating OpenSeek. Each instance uses a local SQLite index of one
private Telegram group or channel, downloads documents by message ID, and caches validated bytes in its ordinary
tile cache. There is no pool HTTP API in this mode. A fresh instance scans the full channel and rebuilds its index;
later scans continue after the last Telegram message ID. The channel is the source of truth.

Run the guided setup on the instance:

```sh
python -m openseek setup
```

Choose **Join a community pool**. For invite-link onboarding, use a private supergroup whose members can send media.
The wizard logs in with a Telegram user session, joins the invite, checks channel access, and writes `openseek.env`
with mode `0600`. It does not modify an existing `.env`. It prints the command to load the new file. A bot cannot
join with an invite link; an administrator must add a bot to the group and grant read and post permissions.

The equivalent user-session configuration is:

```sh
OPENSEEK_POOL_MODE=channel
OPENSEEK_TELEGRAM_API_ID=123456
OPENSEEK_TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
OPENSEEK_TELEGRAM_SESSION_FILE=/var/lib/openseek/telegram/openseek.session
OPENSEEK_TELEGRAM_CHANNEL_ID=-1001234567890
OPENSEEK_CONTRIBUTION=1
```

Get the API ID and hash from Telegram's API development page. The session file is created under the data directory by
default and is restricted to mode `0600`. To authenticate manually, set the session file and run
`python -m openseek pool login`. A channel-mode instance needs read access to the private channel even when it sends
contributions through a librarian.

`OPENSEEK_CONTRIBUTION=1` publishes completed tiles from a durable local outbox. Direct group posting requires the
user session to be a member with Send Messages and Send Media rights, or a bot that an administrator added. For a
Telegram relay, set `OPENSEEK_LIBRARIAN=@pool_librarian_bot`; contributors DM the bot and it validates and reposts
the document. The relay must run with `OPENSEEK_LIBRARIAN_ENABLED=1`, a bot token, MTProto API credentials, and write
permission in the shared channel. Telegram does not allow a bot to DM another bot. Keep the relay open only if the
community accepts open contributions; uploads are rate-limited and bad documents are deleted and temporarily
blocked.

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

The loopback admin dashboard also shows pool mode and has Scan and Rebuild controls. Channel-mode uploads keep the
caption `openseek:v1:<fp>:<profile>:<k>:<sha256>`; their Telegram document filenames carry the timestamp and media
duration needed for NVMT subsets. Older host-mode posts keep working for tile reads, but their captions contain no
duration metadata, so they cannot be used to build exact NVMT packages until a metadata-bearing duplicate appears.

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
