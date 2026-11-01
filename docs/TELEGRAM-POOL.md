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
* The pool API is optional. With `POOL_ENABLED=0` and no `POOL_URL`, OpenSeek behaves as the original local-only
  service. A local pool does not make missing tiles appear without a participant that has generated them.
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
