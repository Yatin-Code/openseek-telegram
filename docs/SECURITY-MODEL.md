# Security model

OpenSeek is a self-hosted service for a Telegram-backed NVMT pool. Nuvio creates NVMT locally during playback. OpenSeek accepts and serves pool packages, and an owner can start a separate generation task from the dashboard.

## Trust boundaries

- Telegram is the canonical durable package store. Keep the bot token or MTProto session on the server.
- Local/VPS SQLite and Vercel Supabase are rebuildable indexes. Vercel object staging is temporary and is used only while a queued upload is published to Telegram.
- Paired clients use revocable device tokens. OpenSeek stores a SHA-256 hash of each token.
- Pool clients may use read and write keys. Treat write keys as secrets and share them only with trusted contributors.
- The browser dashboard uses a per-install admin secret. Local/VPS admin traffic is loopback-only. Vercel checks the admin header and same-origin requests.
- The Vercel function has a configured maximum duration of 300 seconds. Dashboard generation can fail when extraction, packaging, and publication do not finish within that duration.

## Dashboard generation

Generation starts only after an owner submits a media URL through the dashboard. OpenSeek validates the destination, resolves addresses, and reads media through its bounded range proxy. The URL and request headers stay in memory; generation status does not store the source URL. The worker uses the proxy rather than connecting to the source directly.

## Pool validation

Before accepting NVMT, OpenSeek validates the fingerprint, profile, duration, slot bounds, package size, and JPEG records. It rejects conflicting immutable package data. Downloads are checked against the indexed digest. Telegram credentials are not included in client responses.

The package hint index stores only the normalized fields described in docs/api/HINTS-v2.md. Hints help rank possible matches; they are not proof that two media files are the same.

## Network controls

Outbound source access is limited to supported HTTP(S) destinations and configured ports. DNS results are checked before connections. Redirect destinations are checked again. Proxy headers do not change the source client address or authorization.

The owner API is private by default. The optional public listener uses exact route allowlists, per-address limits, and temporary blocks. Admin routes are not public.

## Data

OpenSeek may store device-token hashes, device names, invitations, pool hints, Telegram message metadata, package digests, and local extraction cache data. It does not store source URLs in the dashboard generation status. Logs redact URL queries, bearer tokens, pairing codes, and sensitive headers.

Keep the local data directory private. Back up pairing and Telegram recovery details. SQLite and Supabase indexes can be rebuilt from Telegram; Telegram is the durable pool.