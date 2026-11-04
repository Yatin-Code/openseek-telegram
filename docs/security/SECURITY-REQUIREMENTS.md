# Security requirements

These requirements describe the current NVMT pool and dashboard generation product.

- Dashboard and pairing operations require owner or paired-device authentication. Public routes are allowlisted and rate-limited.
- Store only hashes of device tokens. Keep admin, pool-write, Telegram, and Supabase service credentials on the server.
- Validate NVMT fingerprint, profile, duration, record bounds, JPEG data, and package size before publication. Verify package digests when reading from storage.
- Telegram is the durable package store. Local SQLite and Vercel Supabase indexes can be rebuilt. Vercel staging is temporary.
- Dashboard generation must validate source destinations, revalidate redirects, use bounded reads, and keep the source URL out of persistent status and logs.
- The worker must read through the vetted range proxy and must not receive direct network access to arbitrary URLs.
- Admin routes must not be reachable through the public guest listener. Local/VPS admin binds to loopback. Vercel admin calls require the secret header and same-origin checks.
- Do not trust proxy forwarding headers to identify a client. Reject proxied owner/admin traffic.
- Log redaction must remove source URLs, query strings, bearer tokens, pairing codes, and sensitive headers.
- Do not ship a desktop admin app or player preview, lookup, VTT, capability-image, or sprite-sheet API. Keep the extraction path only for explicit dashboard NVMT generation.
- On Vercel, dashboard generation runs under the configured 300-second function duration. Do not imply that the function can run longer.