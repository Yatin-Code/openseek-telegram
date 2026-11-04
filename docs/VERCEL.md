# Deploy OpenSeek on Vercel

OpenSeek uses Vercel for the API runtime. Telegram remains the durable shared pool.
Supabase PostgreSQL stores a rebuildable channel index and upload status/queue rows.
Supabase Storage holds an NVMT upload only until Telegram accepts it. OpenSeek does not
store permanent package copies in Supabase.

The same browser dashboard serves local/VPS and Vercel. Set one owner secret with
`OPENSEEK_ADMIN_TOKEN`; it protects dashboard operations and is separate from the
Nuvio pool read/write keys and device tokens. If required deployment settings are
missing, the root page shows the variables to configure and the error to fix. Return
after redeploying to open the dashboard.

The Vercel root page is a public static shell and contains no pool/device data. Every
dashboard operation requires the owner secret; opening `/admin/*` without it is
rejected. This hosted profile has one owner secret and no user-account or multi-user
sign-in system. Local/VPS dashboard access remains loopback-only.

## 1. Prepare Supabase

1. Create a Supabase project.
2. In the SQL Editor, run [`backend/openseek/supabase/schema.sql`](../backend/openseek/supabase/schema.sql).
3. Create a **private** Storage bucket named `openseek-staging`. Set its file limit to
   16 MiB. Keep access private; OpenSeek signs a short-lived upload URL for each package.
4. Copy the project URL and the server-side service-role key. Never use the anon key for
   `OPENSEEK_SUPABASE_SERVICE_KEY`.

The service-role key is used only by the OpenSeek function. Telegram is still the only
durable package store. Accepted, expired, and failed uploads have their temporary object
removed by the publication tick; failed cleanup is retried.

## 2. Prepare the Telegram channel

Add the Telegram bot to the channel and grant it access to channel history. Grant post
permission if this node should publish contributions. Create an API ID and API hash at
Telegram's developer portal. Vercel needs a bot token or a Telegram `StringSession`; a
session file cannot survive function restarts.

## 3. Create the Vercel project

Import this repository as a Vercel project and set **Root Directory** to `backend`.
The Python entrypoint is configured in `backend/pyproject.toml`. The deployment uses the
Vercel Python runtime and Python 3.12.

Add these environment variables to the Production environment:

| Variable | Value |
|---|---|
| `OPENSEEK_PROFILE` | `vercel` |
| `OPENSEEK_POOL_MODE` | `channel` |
| `OPENSEEK_ADMIN_TOKEN` | Owner secret, at least 16 random characters |
| `OPENSEEK_PUBLIC_TLS_PIN` | The deployed domain's certificate SPKI pin (`sha256//...`) |
| `OPENSEEK_TELEGRAM_CHANNEL_ID` | The channel ID, such as `-1001234567890` |
| `OPENSEEK_TELEGRAM_API_ID` | Telegram API ID |
| `OPENSEEK_TELEGRAM_API_HASH` | Telegram API hash |
| `OPENSEEK_TELEGRAM_BOT_TOKEN` | Bot token, or set `OPENSEEK_TELEGRAM_SESSION` to a StringSession |
| `OPENSEEK_SUPABASE_URL` | Supabase project URL |
| `OPENSEEK_SUPABASE_SERVICE_KEY` | Supabase service-role key |
| `OPENSEEK_SUPABASE_BUCKET` | `openseek-staging` |
| `OPENSEEK_POOL_READ_KEY` | A long random key for Nuvio pool reads |
| `OPENSEEK_POOL_WRITE_KEY` | A separate long random key for Nuvio pool uploads |
| `CRON_SECRET` | A long random secret for Vercel's scheduled tick |

Set the same values in Preview only if you want preview deployments to access the real
Telegram channel and Supabase project. The cron runs only on Production deployments.

Vercel terminates HTTPS before the Python function runs, so the function's generated
local certificate cannot be used by paired Nuvio clients. Get the public domain's
current certificate pin and set it as `OPENSEEK_PUBLIC_TLS_PIN`:

```sh
openssl s_client -servername your-project.vercel.app -connect your-project.vercel.app:443 </dev/null 2>/dev/null \
  | openssl x509 -pubkey -noout \
  | openssl pkey -pubin -outform DER \
  | openssl dgst -sha256 -binary \
  | openssl base64 -A
```

Prefix the output with `sha256//`. Nuvio's server TLS pin must match this value. If
Vercel rotates the domain certificate's public key, update this variable and redeploy;
re-pair clients if the pin changes.

The Vercel function uses `/tmp/openseek` for scratch state when `OPENSEEK_DATA_DIR` is
unset. This directory is disposable and is not shared between function instances. The
channel index and upload queue are stored in Supabase instead.

Explicit dashboard NVMT generation is capped below the configured 300-second function
duration. A generation that does not finish in that invocation is marked interrupted;
retry it from the dashboard. Vercel Hobby's function duration ceiling is 300 seconds,
so longer work needs a self-hosted local/VPS deployment.

## 4. Deploy and check the pool

Deploy to Production. Vercel calls `GET /v1/internal/tick` once per minute with
`Authorization: Bearer $CRON_SECRET`. The tick advances channel indexing in bounded
pages, removes expired staging objects, and publishes queued uploads to Telegram. It
retries temporary publication failures. The route also accepts an authenticated `POST`
for a manual tick.

After deployment, open the root URL with the owner secret in the fragment once, for
example `https://your-project.vercel.app/#<OPENSEEK_ADMIN_TOKEN>`. The dashboard stores
it in that browser's session storage and removes it from the address bar. Use this owner
secret only for dashboard calls; Nuvio continues to use the separate pool keys or its
device token. Check `GET /v1/health`, then use the configured read key with the
`/v1/pool/*` API. For a contribution, OpenSeek returns a short-lived Supabase upload URL;
Nuvio uploads bytes directly there, finalizes the upload with OpenSeek, and polls its
upload ID until Telegram accepts the package.

Reads do not use Supabase Storage. OpenSeek fetches and validates the channel tiles from
Telegram, assembles the bounded NVMT response, and returns it to Nuvio. Local/VPS
deployments continue to use SQLite and direct upload; they do not need Supabase.

## Limits and operations

- The NVMT package ceiling is 16 MiB. The signed staging path bypasses the function
  request-body limit, but it does not raise OpenSeek's package-size limit.
- The scheduled tick handles a small bounded batch per run. A large backlog drains over
  multiple runs.
- Channel indexing scans bounded pages as pool requests and ticks run. A channel with a
  large history may take multiple ticks to index fully.
- Telegram outages leave uploads queued for retry. Nuvio reports success only after the
  upload status becomes `accepted`.
- The Vercel function does not start the local/VPS background job workers. Device
  records, invitations, and sharing settings use Supabase PostgreSQL so they survive
  instance recycling.

Vercel's [Python runtime](https://vercel.com/docs/functions/runtimes/python) supports
ASGI apps and streaming responses. Vercel Cron sends an HTTP `GET` to the configured
path and passes `CRON_SECRET` as a Bearer token when that environment variable is set;
see [Vercel Cron Jobs](https://vercel.com/docs/cron-jobs).
