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
