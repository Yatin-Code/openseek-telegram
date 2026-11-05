# Participant-owned Vercel nodes

Each participant runs their own OpenSeek Vercel project and Supabase project. There is
no project-operated OpenSeek API or shared Supabase index. Telegram is the durable
shared pool; a participant's Supabase database keeps only that node's rebuildable
index, upload status, and temporary NVMT staging objects.

## Start the setup wizard

Run the wizard from an OpenSeek source checkout, inside `backend` (or from the repo
root):

```sh
python -m openseek setup
```

Choose **Join the shared pool on my own Vercel account**. The wizard signs in through
the official Vercel and Supabase CLIs. Their browser sign-in flows keep provider
credentials in the providers' own CLI credential stores; OpenSeek never asks you to
paste those credentials into chat or writes them to the setup progress file.

The wizard lets you select an existing Supabase project or create one in an
organization you can access. For a new project it asks for a name and database region.
It applies OpenSeek's SQL schema, creates or updates a private `openseek-staging`
bucket with a 16 MiB object limit, and configures the selected Vercel project with the
Supabase connection. Vercel and Supabase resources are created under your signed-in
accounts. The wizard does not change your plan or request a paid upgrade.

The first deployment is deliberately setup-pending: only `GET /v1/health` is available,
and it reports `setup_pending: true`. This gives the wizard a stable, verified endpoint
before it asks you to authorize Telegram. Once a stage completes, the local resume file
contains only provider resource IDs, the deployment URL, and a progress marker. It is
stored under `~/.config/openseek/participant-setup.json` with owner-only permissions.
Rerun `python -m openseek setup` to resume.

After the node is healthy, rerun the same wizard to connect Telegram. It asks for the
Telegram application ID and hidden API hash, then authorizes your Telegram user account
with its phone, login code, and optional two-step password. It joins the shared invite
directly and checks that the account can read group history and send messages and media.
The group must be a supergroup with history visible to new members and an invite that
does not request owner approval. If these checks fail, the wizard explains what needs
to be fixed and lets you retry.

The account's MTProto StringSession is created in memory and sent directly to your
Vercel project's secret environment variables. It is not written to the local resume
file. The wizard redeploys with Telegram enabled and checks the node again. OpenSeek
publishes as your Telegram account; it does not create a bot or ask the pool owner to add
one. Contribution stays disabled until you opt in later in setup.

## Local requirements

- Python 3.12 or later and the OpenSeek backend dependencies.
- The Vercel CLI and Supabase CLI installed and available on `PATH`.
- Accounts with access to the projects you select. Free tiers have provider-defined
  quotas; when they are exhausted, setup pauses and can be resumed after capacity is
  available. OpenSeek never enables a paid plan.

The provider CLIs perform sign-in in your browser. Do not copy access tokens into
OpenSeek configuration. The Supabase secret key is held in process memory only long
enough for the wizard to configure the private bucket and pipe it to `vercel env add`;
the command captures provider output and never prints the key.

The wizard creates a random `OPENSEEK_ADMIN_TOKEN` for the dashboard. It stores that
first-party owner secret separately at
`~/.config/openseek/participant-setup.admin-token` with owner-only permissions and
prints a dashboard link with the secret in the URL fragment. `OPENSEEK_PUBLIC_TLS_PIN`
is optional for participant nodes; when configured, it adds an exact certificate-key
pin to Nuvio's normal HTTPS verification.

## Browser dashboard

The same dashboard serves local/VPS and Vercel nodes. It can inspect pairing and pool
status, revoke devices and invites, enable or disable guest uploads, and start explicit
NVMT generation. On Vercel, open the root deployment URL with the owner secret in the
fragment once, such as `https://your-node.vercel.app/#<OPENSEEK_ADMIN_TOKEN>`. The
dashboard keeps it in that browser session and removes it from the address bar. The
owner secret is separate from Nuvio's pool keys and paired-device token.

Generation is started only from the dashboard. Its source URL stays in the running
function's memory; only generation progress and publication status are stored in
Supabase. A Vercel generation is bounded to 270 seconds so it can finish within the
function's configured duration. If the invocation times out or is interrupted, the
dashboard reports that state and the owner can retry it. Local/VPS generation runs
outside the Vercel function deadline.

## Runtime data

- Telegram holds accepted package tiles and manifests as the canonical shared pool.
- Each participant's Supabase database holds only that node's derived index and durable
  upload/retry status.
- Supabase Storage holds an NVMT package only while it is being published to Telegram.
  The staging object is deleted after acceptance or terminal cleanup.
- Nuvio sends requests to the participant's own OpenSeek URL. Pool reads fetch validated
  tiles from Telegram, and pool writes publish as the participant's Telegram account.

Vercel's `/tmp` directory is scratch space only. Function instances do not share it.
The node must not depend on background workers or a frequent Cron schedule: reads and
contribution requests advance bounded work. If a provider quota or inactivity pause
stops the node, local thumbnail generation remains available.

The wizard authorizes one Telegram user session for this node. OpenSeek stores the
StringSession as a Vercel secret and uses a short Supabase lease to serialize requests
that access the shared group. Each invocation disconnects before releasing the lease;
a warm function instance reconnects on its next request.
