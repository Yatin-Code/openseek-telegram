# OpenSeek

<p align="center"><img src="docs/images/banner.png" alt="OpenSeek thumbnail sharing" width="100%"></p>

**OpenSeek helps Nuvio-compatible clients reuse preview thumbnails through a community-run pool.** Each client still makes thumbnails on its own device. When a user opts in, the finished thumbnails can be shared so another client can reuse them instead of decoding the same media again.

The pool is designed to have **no central OpenSeek API and no project-paid hosting**. Participants operate their own pool nodes and accounts. Telegram is the durable shared store; each node keeps its own index and upload state. To keep a node at zero spend, use the providers' free plans and disable any account-level automatic upgrade. Free tiers have limits, so a node may pause or queue work when it runs out of capacity. OpenSeek never silently upgrades a plan.

OpenSeek does not receive or store video files, playback URLs, or source headers. The shared format is NVMT: a bounded package of generated JPEG preview tiles and timing metadata.

## What a contribution contains

An opted-in contribution contains generated JPEG tiles, their timing and NVMT profile, the media fingerprint, and any available matching hints such as IMDb ID, season and episode, duration, file size, or normalized filename. Those hints let another release of the same title find an approximate package. OpenSeek never sends the source video, playback URL, or source request headers. Nuvio contribution is off until the user enables it, and can be disabled again in Settings.

## How the pool fits together

![OpenSeek pool architecture: a Nuvio client paired with a Vercel or local/VPS node; both node types read from and publish to a shared Telegram supergroup](docs/images/pool-architecture.png)

A Vercel node and a local/VPS node are peers, not separate roles. Either deployment can serve exact and hint lookups and publish accepted contributions. The main difference is where the rebuildable index lives (Supabase for Vercel, SQLite for local/VPS); Vercel also uses temporary staging for large uploads.

Nuvio generates thumbnails locally while playback continues. It can ask a pool node for an **exact match** using the media fingerprint and NVMT profile. If no exact package is available, it can try a **hint-ranked candidate** using allowed catalog and file hints. Hint matches are approximate and use a timing map.

The node checks stored tile digests before returning them. Nuvio checks each response's SHA-256, NVMT structure, tile slots, and JPEG markers before using shared thumbnails. Sparse or partial packages are useful: Nuvio keeps verified shared tiles and generates any missing frames locally. A bad package, an empty pool, or an offline node does not stop local thumbnail generation.

With sharing enabled, Nuvio submits its locally generated NVMT package to its configured node. That node validates it and publishes the tiles and hint manifest to Telegram. An upload is reported as accepted only after Telegram has accepted the publication.

## Who connects to the Telegram group?

The pool owner creates one private Telegram **supergroup**, allows members to read history and send messages and media, and shares an invite that lets people join directly. The owner does not add a bot for every participant or approve every join request.

Each participant connects their own OpenSeek node to the group with their own Telegram user account. The setup wizard signs that account in, joins through the invite, checks read and post access, and stores its session only in that participant's deployment. The node publishes as that participant. It does not need a bot token.

There are a few one-time setup actions: sign in to Vercel and Supabase, authorize Telegram with a login code and optional two-step password, accept the shared invite, and enter the one-time OpenSeek pairing bundle in Nuvio. After that, sharing is automatic when the user has enabled contribution in Nuvio. There is no manual Telegram upload or approval for each contribution.

![Nuvio generates and contributes packages, nodes publish them, and later clients retrieve verified partial packages](docs/images/thumbnail-lifecycle.png)

## Supported pool modes

| Mode | Where it runs | Telegram connection | Index and upload state |
|---|---|---|---|
| Local-only | Nuvio with no shared pool | None | None |
| Host API | An existing local/VPS OpenSeek host | The host uses its configured Telegram Bot API identity; participants use scoped read/write keys | Host's local SQLite database |
| Direct Telegram | Each local/VPS OpenSeek node | The node's configured Telegram identity (usually its own user session; a bot must be added by a group admin) | Rebuildable SQLite index on each node |
| Participant Vercel | Each participant's Vercel deployment | Each node uses its own authorized Telegram user session | That participant's Supabase Postgres project; temporary upload staging in Supabase Storage |

The host API remains available for communities that choose a trusted host. Direct Telegram and participant Vercel modes avoid requiring one shared OpenSeek API. These modes use the same Nuvio pool protocol, so clients still try exact matches first, then hints, and generate locally for missing tiles.

## Environment variables

The setup wizard writes most of these values for you. Use this table when configuring a node by hand, or when checking what a wizard-managed deployment uses. [`.env.example`](.env.example) is a local/VPS template; [docs/INSTALL.md](docs/INSTALL.md) and [docs/VERCEL.md](docs/VERCEL.md) cover the surrounding steps.

- **Local/VPS:** keep the `OPENSEEK_*` values in the file named by `OPENSEEK_ENV_FILE`, or in `<OPENSEEK_DATA_DIR>/.env` when `OPENSEEK_ENV_FILE` is unset. Restrict the file to mode `0600`. Set `OPENSEEK_DATA_DIR` and `OPENSEEK_ENV_FILE` in the service environment, not in the file. The file reader ignores keys without the `OPENSEEK_` prefix, and the process environment wins over the file.
- **Vercel:** add the values under **Project → Settings → Environment Variables → Production**, then redeploy. There is no `.env` file on Vercel.
- **Secrets:** values in angle brackets are placeholders. Never commit the file or paste its values into issues.

| Variable | Local/VPS | Vercel | Notes |
|---|---|---|---|
| `OPENSEEK_PROFILE` | `local` (default) | `vercel` (required) | `VERCEL=1` also selects the Vercel profile. |
| `OPENSEEK_DATA_DIR` | service environment; default `data` | not set; `/tmp` scratch only | Holds SQLite, the TLS key, the admin token, and the cache. Keep it outside the checkout and private. |
| `OPENSEEK_ENV_FILE` | service environment; optional | not used | Path to the `.env` file. Defaults to `<OPENSEEK_DATA_DIR>/.env`. |
| `OPENSEEK_POOL_MODE` | `channel` for a Telegram pool; omit for local-only | `channel` (required) | `host` is the host-API mode. |
| `OPENSEEK_ADMIN_TOKEN` | optional; random value in the data directory | required; at least 16 characters | Owner dashboard secret. Keep it out of source control. |
| `OPENSEEK_TELEGRAM_CHANNEL_ID` | required for `channel` | required | Shared supergroup ID, for example `-1001234567890`. |
| `OPENSEEK_TELEGRAM_API_ID` | required for `channel` | required | Numeric application ID from [my.telegram.org](https://my.telegram.org). |
| `OPENSEEK_TELEGRAM_API_HASH` | required for `channel` | required | Application hash from [my.telegram.org](https://my.telegram.org). Keep it secret. |
| `OPENSEEK_TELEGRAM_BOT_TOKEN` | one identity option | rejected | A local node can use a bot added by a group admin. Vercel requires a user session instead. |
| `OPENSEEK_TELEGRAM_SESSION_FILE` | one identity option | not usable | Local user-session file. Create it with `python -m openseek pool login` and keep it private. |
| `OPENSEEK_TELEGRAM_SESSION` | one identity option | required | Telethon StringSession. On Vercel this must be the user-session string, not a file path. |
| `OPENSEEK_CONTRIBUTION` | `0` by default; set `1` to publish | set `1` to publish | The server-side publishing switch. Nuvio still gates submissions with its own opt-in setting. |
| `OPENSEEK_POOL_CHANNEL_WRITABLE` | `auto` (default) | `auto` (default) | `1` or `0` overrides the detected Telegram post permission. |
| `OPENSEEK_SUPABASE_URL` | not used | required | `https://<project>.supabase.co`; HTTPS origin only. |
| `OPENSEEK_SUPABASE_SERVICE_KEY` | not used | required | Service-role key; never the anon key. |
| `OPENSEEK_SUPABASE_BUCKET` | not used | `openseek-staging` (default) | Private staging bucket for temporary upload bytes. |
| `OPENSEEK_POOL_WRITE_KEY` | host mode or contribution relay | optional | At least 24 characters. Required with `OPENSEEK_POOL_CONTRIB_URL`; the Vercel wizard normally uses paired-device tokens instead. |
| `OPENSEEK_POOL_CONTRIB_URL` | read-only node only | not needed by default | HTTPS origin of a writable OpenSeek node that accepts contributions. |
| `OPENSEEK_LIBRARIAN` | optional relay | optional | Bot username or HTTPS URL used as an alternative contribution path. |
| `OPENSEEK_LIBRARIAN_KEY` | with `OPENSEEK_LIBRARIAN` | optional | At least 24 characters; only when the relay requires a bearer key. |
| `OPENSEEK_LIBRARIAN_ENABLED` | relay host only | not usable | `1` on the relay host's bot. It requires a bot token, which the Vercel profile rejects. |
| `OPENSEEK_PUBLIC_TLS_PIN` | optional | optional | `sha256//<spki-pin>` for clients that pin the deployment certificate. |
| `CRON_SECRET` | not needed | optional | Bearer secret for `/v1/internal/tick`; the admin token is also accepted. Vercel's cron integration sends this value when it is configured. |
| `OPENSEEK_CRON_SECRET` | optional | optional | Prefixed equivalent for `.env` files; the file reader ignores `CRON_SECRET`. |
| `OPENSEEK_POOL_URL` | host mode only | ignored in channel mode | HTTPS origin of the upstream pool API. HTTP is allowed only for loopback tests. |
| `OPENSEEK_POOL_READ_KEY` | host mode only | ignored in channel mode | At least 24 characters. Channel-mode nodes use paired-device tokens instead. |
| `OPENSEEK_POOL_SHARE` | host mode only | ignored in channel mode | Allow this host to accept shared contributions; requires `OPENSEEK_POOL_WRITE_KEY`. |
| `OPENSEEK_POOL_ENABLED` | legacy host switch | ignored in channel mode | Still written by the host setup wizard; prefer `OPENSEEK_POOL_MODE=host` for new configurations. |

Use exactly one Telegram identity: a bot token, a local session file, or a StringSession. A Vercel node needs the user StringSession because its functions cannot keep a session file and the profile rejects bot tokens.

Local-only mode needs only `OPENSEEK_PROFILE=local`; leave `OPENSEEK_POOL_MODE` unset and supply no Telegram or Supabase values. Host API mode uses `OPENSEEK_POOL_MODE=host`, `OPENSEEK_POOL_ENABLED=1`, `OPENSEEK_TELEGRAM_BOT_TOKEN`, `OPENSEEK_TELEGRAM_CHANNEL_ID`, `OPENSEEK_POOL_READ_KEY`, and `OPENSEEK_POOL_WRITE_KEY`; the host setup wizard writes that set.

### Minimal local/VPS pool

Service environment:

```sh
OPENSEEK_DATA_DIR=/var/lib/openseek
OPENSEEK_ENV_FILE=/var/lib/openseek/.env
```

`/var/lib/openseek/.env` (mode `0600`):

```dotenv
OPENSEEK_PROFILE=local
OPENSEEK_POOL_MODE=channel
OPENSEEK_TELEGRAM_CHANNEL_ID=-1001234567890
OPENSEEK_TELEGRAM_API_ID=123456
OPENSEEK_TELEGRAM_API_HASH=<api-hash>
OPENSEEK_TELEGRAM_BOT_TOKEN=<bot-token>
# Or replace the bot token with a user session:
# OPENSEEK_TELEGRAM_SESSION_FILE=/var/lib/openseek/telegram/openseek.session
OPENSEEK_POOL_CHANNEL_WRITABLE=auto
OPENSEEK_CONTRIBUTION=1
# Optional; otherwise generated in <OPENSEEK_DATA_DIR>/admin.token
# OPENSEEK_ADMIN_TOKEN=<at-least-16-characters>
```

### Minimal Vercel node

Set these as Production environment variables:

```dotenv
OPENSEEK_PROFILE=vercel
OPENSEEK_POOL_MODE=channel
OPENSEEK_ADMIN_TOKEN=<at-least-16-characters>
OPENSEEK_TELEGRAM_CHANNEL_ID=-1001234567890
OPENSEEK_TELEGRAM_API_ID=123456
OPENSEEK_TELEGRAM_API_HASH=<api-hash>
OPENSEEK_TELEGRAM_SESSION=<telethon-string-session>
OPENSEEK_SUPABASE_URL=https://<project>.supabase.co
OPENSEEK_SUPABASE_SERVICE_KEY=<service-role-key>
OPENSEEK_SUPABASE_BUCKET=openseek-staging
OPENSEEK_CONTRIBUTION=1
# Optional:
# CRON_SECRET=<cron-secret>
# OPENSEEK_PUBLIC_TLS_PIN=sha256//<spki-pin>
```

The Vercel setup wizard creates the Telegram session, Supabase schema, bucket, and environment variables for you. Its temporary `OPENSEEK_SETUP_PENDING` and `OPENSEEK_SETUP_KEY` values are wizard-managed; do not set them by hand. The optional REST wizard also reads `OPENSEEK_VERCEL_TOKEN` and `OPENSEEK_SUPABASE_ACCESS_TOKEN` from your local environment; neither is a deployed node setting.

Runtime tuning that is not needed to start a pool node (ports, cache, worker limits, preview tolerance, public guest listener, TLS files, and logging) is documented with its defaults in `backend/openseek/config.py`.

## Participant-owned Vercel setup

The wizard runs on the participant's computer. It uses the official Vercel and Supabase CLIs for browser sign-in, creates or links the participant's own projects, applies the OpenSeek schema, configures a private staging bucket, deploys the node, and checks its health. Provider credentials are not written to the wizard's resume file.

### Requirements

- Python 3.12 or later and the OpenSeek backend dependencies.
- A Vercel account and the Vercel CLI.
- A Supabase account and the Supabase CLI.
- A Telegram account, an API ID and API hash from [my.telegram.org](https://my.telegram.org), and the current shared-group invite.
- A Nuvio build with OpenSeek pairing and contribution settings.

Install the Vercel and Supabase CLIs using their official guides: [Vercel CLI](https://vercel.com/docs/cli) and [Supabase CLI](https://supabase.com/docs/guides/cli). The OpenSeek wizard will prompt those tools to sign in through their normal browser flows.

### Run the wizard

```sh
git clone https://github.com/Yatin-Code/openseek-telegram.git
cd openseek-telegram/backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -c requirements-lock.txt
.venv/bin/python -m openseek setup
```

On Windows, use `.venv\Scripts\pip` and `.venv\Scripts\python` in place of the POSIX paths above. Choose **Join the shared pool on my own Vercel account**. The wizard guides you through:

1. Signing in to Vercel and linking or creating your OpenSeek project.
2. Linking or creating your Supabase project and applying the required schema and private storage bucket.
3. Deploying a setup-pending node and checking that it responds.
4. Signing in to Telegram, joining the shared invite, and checking group history and send permissions.
5. Deploying the Telegram-enabled node and checking it again.
6. Creating a one-time Nuvio pairing bundle. Enter it in **Nuvio → Settings → OpenSeek → Pair**, then enable contribution if you want to share.

The wizard can resume after an interruption. Its local progress file is `~/.config/openseek/participant-setup.json` with owner-only permissions. Telegram session and API hash values are sent to the participant's Vercel project and are not saved in that progress file. The generated dashboard secret is stored separately in `~/.config/openseek/participant-setup.admin-token` with owner-only permissions; setup prints a dashboard link that carries the secret only in the URL fragment. The Telegram account remains under the participant's control; revoke its session or disable contribution at any time.

The pool owner still performs the one-time group setup and shares the direct-join invite. For the group to work, new members must see its history and be allowed to send messages and media. An invite that requires owner approval is not suitable for the hands-off participant flow.

For the full wizard details and recovery steps, see [Participant-owned Vercel nodes](docs/VERCEL.md).

## Manage your node

The browser dashboard manages the OpenSeek node you own. On Vercel, open its deployment
URL with the owner secret in the URL fragment, for example
`https://your-node.vercel.app/#<OPENSEEK_ADMIN_TOKEN>`. The dashboard saves that secret
in the browser session and removes it from the address bar. On a local or VPS install,
open the dashboard URL shown by `python -m openseek dashboard`.

From the dashboard, you can check the Telegram connection and pool index, pair and revoke
Nuvio devices, enable or disable pool sharing and guest uploads, and create or revoke
invites. The owner secret protects these controls; Nuvio uses its separate device token
or pool keys. To publish a package from the dashboard, submit a media URL and start
**Generate and publish**. This is an explicit owner action with progress and status.
Playback and lookup requests never trigger media extraction; during playback Nuvio
generates its own thumbnails locally when it has no usable shared match.

## What the Vercel node stores

![Vercel and Supabase layout: participant functions, rebuildable Postgres tables, temporary staging, and Telegram storage](docs/images/participant-vercel-layout.png)

| Location | Data | Purpose |
|---|---|---|
| Telegram supergroup | Accepted NVMT tile documents and versioned hint manifests | Canonical durable pool; participants' nodes can rebuild their indexes from it |
| Supabase `pool_channel_tiles` | Tile slot, digest, duration, and Telegram message ID | Exact NVMT package index |
| Supabase `pool_hints` and `pool_episode_packages` | Validated catalog, episode, filename, duration, and ranking metadata | Hint-based candidate lookup |
| Supabase `pool_channel_state` | Last indexed Telegram message ID and scan time | Continue or rebuild channel indexing |
| Supabase `pool_uploads` and `pool_publication_queue` | Pending upload metadata, status, retry time, and error code | Durable publish lifecycle and recovery |
| Supabase `pool_devices`, `pool_invites`, and session lease | Pairing credentials, one-time invites, and concurrency lock | Manage Nuvio access and serialize use of the Telegram session |
| Private Supabase Storage bucket | Temporary staged NVMT upload object | Bypass Vercel's function request body limit; removed after publication or cleanup |

Supabase is not another permanent thumbnail store. Its tables are rebuildable or operational state; accepted thumbnail bytes remain in Telegram. A participant's Vercel deployment and Supabase project are independent of every other participant's deployment.

## Limits, failures, and recovery

| Situation | OpenSeek behavior |
|---|---|
| Large contribution on Vercel | Nuvio sends package bytes to a short-lived signed Supabase staging URL. Vercel receives only bounded metadata and status requests. |
| Large thumbnail read | The node returns at most 12 tiles per chunk, with a 4.5 MB response guard matching [Vercel's Function payload limit](https://vercel.com/docs/functions/limitations). NVMT packages are capped at 16 MiB and individual tiles at 256 KiB. |
| Partial package or missing Telegram tiles | Nuvio keeps valid shared tiles and locally fills the missing slots. |
| Invalid manifest, checksum, or JPEG tile | The response is rejected; local thumbnail generation continues. |
| Supabase outage or quota | The node returns a retryable or paused state. Quota pauses honor provider `Retry-After` when available; otherwise publication waits before retrying. No paid plan is enabled automatically. |
| Telegram FloodWait or group slow mode | The publication queue retains the contribution and schedules another attempt after Telegram's requested delay. |
| Expired or terminal upload | Temporary staged bytes are deleted immediately where possible; cleanup retries if Supabase is unavailable. |
| Empty, stale, or unavailable node index | A node refreshes or rebuilds its own index from Telegram. A newly posted item may take a refresh cycle to appear in a different node's index. |

Contribution status distinguishes work that is pending, retrying, paused, accepted, or failed. `accepted` means Telegram accepted the tiles and manifest. Vercel, Supabase, and Telegram set their own free-tier quotas and service availability; OpenSeek is best-effort and can pause or build a backlog to preserve the zero-spend rule.

For recovery and troubleshooting, see [docs/VERCEL.md](docs/VERCEL.md), [docs/TELEGRAM-POOL.md](docs/TELEGRAM-POOL.md), and [docs/INSTALL.md](docs/INSTALL.md).

## Local OpenSeek setup

The shared pool is optional. OpenSeek works in local-only mode, where Nuvio generates thumbnails on the device and no Telegram, Vercel, or Supabase setup is needed.

For a local/VPS service, see the [installation guide](docs/INSTALL.md). The shortest Linux source setup is:

```sh
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -c requirements-lock.txt
.venv/bin/python -m openseek serve
```

To create a node that joins a Telegram community directly, run `.venv/bin/python -m openseek setup` and choose **Join a community pool**. To use a host API, choose **Host a public pool** or configure it as described in [docs/TELEGRAM-POOL.md](docs/TELEGRAM-POOL.md).

## Project boundaries

- OpenSeek moves generated preview thumbnails; it does not download or redistribute source video.
- Sharing is opt-in. Nuvio generates previews locally whether the pool is enabled or not.
- Exact fingerprints remain distinct from approximate catalog hints. Hints never replace the exact content key.
- The shared pool is experimental and has no uptime, latency, or unlimited-capacity guarantee.
- You are responsible for having rights or permission to generate and share thumbnails from the media you use. Read [Responsible use](docs/RESPONSIBLE-USE.md).

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Participant-owned Vercel setup](docs/VERCEL.md)
- [Telegram pool modes](docs/TELEGRAM-POOL.md)
- [Installation](docs/INSTALL.md)
- [HTTP API contract](docs/api/CONTRACT-v1.md)
- [Security model](docs/SECURITY-MODEL.md) and [vulnerability reporting](SECURITY.md)
- [Responsible use](docs/RESPONSIBLE-USE.md)
- [Contributing](CONTRIBUTING.md), [license](LICENSE), and [changelog](CHANGELOG.md)

OpenSeek is free software under the **GNU Affero General Public License v3 or later** (AGPL-3.0-or-later). See [LICENSE](LICENSE). The project does not publish prebuilt Docker images because the PyAV wheels bundle FFmpeg libraries built with GPL components; build from source with `docker compose up -d --build`. See [docs/LICENSE-OPTIONS.md](docs/LICENSE-OPTIONS.md).
