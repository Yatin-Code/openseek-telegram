# Telegram pool

Telegram is the canonical durable store for shared packages. Each OpenSeek pool node keeps a rebuildable index. A Vercel deployment uses Supabase for that index and for temporary upload staging.

Nuvio creates NVMT locally when playback has no usable pool match. OpenSeek receives and serves the NVMT package; it does not receive the playback media URL. An owner can also start a package generation task from the browser dashboard.

## Storage flow

Nuvio or the dashboard uploads a validated NVMT package through the pool API. A local/VPS node publishes it to Telegram directly. Vercel places the upload in temporary staging and a scheduled task publishes it to Telegram. The index records hints and Telegram message metadata. If the index is lost, it can be rebuilt from Telegram.

Pool nodes serve package bytes to clients. Telegram bot tokens, MTProto sessions, and pool write keys remain on the server. Clients use paired device tokens or the configured read key.

## Local/VPS and Vercel profiles

Use `OPENSEEK_POOL_MODE=channel` for the Telegram-backed pool. Local/VPS and Vercel use the same Telegram channel and browser dashboard, with different rebuildable state:

| Profile | Index and upload state | Dashboard |
|---|---|---|
| Local/VPS (`OPENSEEK_PROFILE=local`) | SQLite in the protected `OPENSEEK_DATA_DIR`; no Supabase required | Loopback admin port 8788; use an SSH tunnel to reach a VPS dashboard |
| Vercel (`OPENSEEK_PROFILE=vercel`) | Supabase PostgreSQL; Supabase Storage is temporary upload staging | Project root URL; Vercel Cron advances scans and publication retries |

Telegram is the durable package store for both. The local/VPS first-run dashboard saves Telegram settings in the private `<data>/.env` file, verifies channel access, and starts the initial scan. Run `python -m openseek dashboard` on the host to open it. See [INSTALL.md](INSTALL.md#localvps-on-linux) for a Linux service and VPS tunnel. Vercel does not accept Telegram settings in the browser: create the Supabase schema and private staging bucket, then add the required values to the Vercel Production environment and redeploy. The full Vercel steps and environment-variable list are in [VERCEL.md](VERCEL.md).

The Vercel profile requires an owner secret in `OPENSEEK_ADMIN_TOKEN` (at least 16 characters). Open the deployed root URL with that secret in the URL fragment once: `https://your-project.vercel.app/#<OPENSEEK_ADMIN_TOKEN>`. The dashboard keeps it in that browser's session storage and removes the fragment from the address bar. This owner secret protects dashboard operations; Nuvio pool read/write keys and paired-device tokens are separate credentials. Local/VPS generates the owner secret in `<data>/admin.token` unless `OPENSEEK_ADMIN_TOKEN` is set.

## Deployment

For a Vercel deployment, follow docs/VERCEL.md. Configure Supabase as a rebuildable index and temporary staging service. Telegram remains the durable package store.

For a local or VPS node, set the pool mode and Telegram credentials in the environment or use the dashboard setup wizard. A local node stores its index in SQLite and can upload directly to Telegram when the account has channel write permission. If the channel is read-only, configure an authorized upload path.

## Telegram membership and first scan

Create a private Telegram channel or supergroup. Get the Telegram API ID and API hash from [my.telegram.org](https://my.telegram.org/apps). Choose one identity:

- **Bot:** create it with BotFather and ask a Telegram channel/group admin to add it. Grant access to channel history. Grant posting permission only if this OpenSeek instance should publish directly. A bot cannot join a private invite link by itself.
- **User session:** the account must be a member and able to read history. Direct group posting also needs Send Messages and Send Media rights. A user session can join a private supergroup with an invite link.

For a local/VPS first run, start OpenSeek in the default `local` profile with no channel settings, then open `python -m openseek dashboard`. Enter the channel ID, API ID/hash, and either the bot token or a Telegram StringSession in the setup form. It checks history access and posting rights, then runs an initial scan. The form saves credentials to `<data>/.env` with private permissions and reports whether the channel can be written. `OPENSEEK_CONTRIBUTION` starts off; enable it only after deciding which upload path the instance should use.

For Vercel, add the channel ID, API ID/hash, and bot token (or a Telegram StringSession) to the Production environment as described in [VERCEL.md](VERCEL.md). A session file cannot be used on Vercel because function instances are disposable. After deployment, opening the dashboard's pool status or waiting for the once-per-minute Vercel tick starts the bounded indexing passes. Use the dashboard's **Scan** control to request an immediate pass.

The separate CLI setup wizard is also available:

```sh
python -m openseek setup
```

Choose **Join a community pool** to log in with a Telegram user session, join a private-supergroup invite, and write `openseek.env` with mode `0600`. It does not modify `<data>/.env`; it prints the command to load its output. For an existing bot or a channel where the account is already a member, use the browser setup or environment configuration instead.

The equivalent user-session configuration is:

```sh
OPENSEEK_POOL_MODE=channel
OPENSEEK_TELEGRAM_API_ID=123456
OPENSEEK_TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
OPENSEEK_TELEGRAM_SESSION_FILE=/var/lib/openseek/telegram/openseek.session
OPENSEEK_TELEGRAM_CHANNEL_ID=-1001234567890
OPENSEEK_CONTRIBUTION=1
```

For a local/VPS user account, the session file is created under the data directory by default and is restricted to mode `0600`. To authenticate manually, make `OPENSEEK_POOL_MODE=channel`, `OPENSEEK_DATA_DIR`, and `OPENSEEK_ENV_FILE` available to the CLI, then run `python -m openseek pool login`. A channel-mode instance needs read access to the private channel even when it sends contributions through a librarian. Vercel requires a bot token or StringSession; a local session file is not durable across function invocations.

Use these commands to inspect or repair the local index:

```sh
python -m openseek pool status
python -m openseek pool scan
python -m openseek pool rebuild
```

Run `python -m openseek doctor` for a health summary. Look for `"daemon": "running"`, `history_readable: true`, a resolved channel entity, and a recent `scanned_at`; `tiles`, `hints`, and `last_message_id` show indexing progress. `channel_writable: false` means the instance can read but cannot post directly. `last_error` is empty when the most recent pool operation succeeded. `python -m openseek pool status` returns the pool status without the doctor summary. If doctor reports `daemon: unavailable`, check that OpenSeek is running and that the command uses the service's same `OPENSEEK_DATA_DIR` and `OPENSEEK_ENV_FILE`. On Vercel, use the browser dashboard's pool status and Scan/Rebuild controls; the local CLI connects only to a loopback admin port.

The local/VPS loopback admin dashboard also shows pool mode and has Scan and Rebuild controls. Channel-mode uploads keep the caption `openseek:v1:<fp>:<profile>:<k>:<sha256>`; their Telegram document filenames carry the timestamp and media duration needed for NVMT subsets.

## Package and hint contract

The pool serves NVMT packages by exact fingerprint and can return ranked candidates from the approved package hints. See docs/api/CONTRACT-v1.md and docs/api/HINTS-v2.md.

The server validates package size, profile, duration, slots, and JPEG records before accepting the upload. Duplicate writes are idempotent. Conflicting data does not replace an already accepted package.

## Vercel temporary upload staging

Local/VPS sends an NVMT package directly to OpenSeek. Vercel stages package bytes in the private Supabase bucket to avoid the function request-body limit: OpenSeek issues a signed upload URL, the uploader sends bytes to that URL, finalizes the upload, and polls its upload ID. Staging URLs and pending objects expire after 15 minutes; the scheduled tick cleans up expired objects. The bucket file limit and OpenSeek package ceiling are both 16 MiB.

An upload is accepted only after Telegram accepts the package. Supabase PostgreSQL stores its status and publication retry queue, not a durable package copy. Vercel Cron runs every minute, advances channel-index scans, publishes a bounded batch, and retries temporary failures. Downloads in both profiles stream from Telegram and do not use Storage.

## Existing packages and hint backfill

Packages published before the NVMT hint manifest are still available by their exact `fp` and profile. They do not become cross-release candidates merely because an index is rebuilt. A scan or rebuild never derives IMDb IDs, season/episode numbers, duration, file size, or title from a video, Telegram filename, or other guessed metadata.

An operator can opt an individual existing package into hint matching when they have authoritative metadata. Keep a sidecar mapping from the exact package identity to the supplied values, for example:

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

For a channel pool, retrieve that same NVMT package by its exact `fp` and profile, then have a writable channel node accept it again through `PUT /v1/pool/{fp}/nvmt` with the sidecar values as query parameters. The node validates the package and metadata, keeps the existing `fp`, and publishes the hint manifest to Telegram. The duration must match the package; `size_bytes` and `filename` must describe the original media from a trusted catalog. If that metadata is unavailable, leave the package exact-`fp`-only. There is no automatic or bulk backfill command.

## Operating rules

- Keep the Telegram channel private and grant only the required history or posting permissions.
- Keep bot tokens, MTProto sessions, admin secrets, and pool write keys out of client builds, URLs, logs, and source control.
- Restrict write access to trusted contributors. For IMDb-keyed NVMT package contributions — episodes with season and episode both above zero, or movies with season and episode both zero — the librarian accepts up to three per sender per work key per UTC day, where the work key identifies the episode (IMDb ID plus season and episode) or the movie (IMDb ID alone). Filename-only NVMT packages, packages with incomplete season/episode identity, and single-tile contributions are not subject to this quota. A submission that fails validation, download, or publication does not consume one of those three slots. Rejections reply `Rejected: <code>` (for example `quota_exceeded`, `sender_blocked`, `bad_mime_type`, `package_too_large`, `malformed_hints`, `bad_tile`, `manifest_too_large`, `repost_failed`, `index_failed`). To review contributor quotas and blocked senders, run `python -m openseek pool contributor list`. To block or unblock a sender, run `python -m openseek pool contributor block <sender_id>` or `python -m openseek pool contributor unblock <sender_id>`.
- Back up Telegram configuration and account recovery details. SQLite and Supabase indexes can be rebuilt; Telegram is the durable store.
- The dashboard generation task is limited by its host. On Vercel, the function stops after the configured 300-second maximum duration.
