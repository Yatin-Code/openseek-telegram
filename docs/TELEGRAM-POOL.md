# Telegram pool

Telegram is the canonical durable store for shared packages. Each OpenSeek pool node keeps a rebuildable index. A Vercel deployment uses Supabase for that index and for temporary upload staging.

Nuvio creates NVMT locally when playback has no usable pool match. OpenSeek receives and serves the NVMT package; it does not receive the playback media URL. An owner can also start a package generation task from the browser dashboard.

## Storage flow

Nuvio or the dashboard uploads a validated NVMT package through the pool API. A local/VPS node publishes it to Telegram directly. Vercel places the upload in temporary staging and a scheduled task publishes it to Telegram. The index records hints and Telegram message metadata. If the index is lost, it can be rebuilt from Telegram.

Pool nodes serve package bytes to clients. Telegram bot tokens, MTProto sessions, and pool write keys remain on the server. Clients use paired device tokens or the configured read key.

## Deployment

For a Vercel deployment, follow docs/VERCEL.md. Configure Supabase as a rebuildable index and temporary staging service. Telegram remains the durable package store.

For a local or VPS node, set the pool mode and Telegram credentials in the environment or use the dashboard setup wizard. A local node stores its index in SQLite and can upload directly to Telegram when the account has channel write permission. If the channel is read-only, configure an authorized upload path.

## Package and hint contract

The pool serves NVMT packages by exact fingerprint and can return ranked candidates from the approved package hints. See docs/api/CONTRACT-v1.md and docs/api/HINTS-v2.md.

The server validates package size, profile, duration, slots, and JPEG records before accepting the upload. Duplicate writes are idempotent. Conflicting data does not replace an already accepted package.

## Operating rules

- Keep the Telegram channel private and grant only the required history or posting permissions.
- Keep bot tokens, MTProto sessions, admin secrets, and pool write keys out of client builds, URLs, logs, and source control.
- Restrict write access to trusted contributors.
- Back up Telegram configuration and account recovery details. SQLite and Supabase indexes can be rebuilt; Telegram is the durable store.
- The dashboard generation task is limited by its host. On Vercel, the function stops after the configured 300-second maximum duration.