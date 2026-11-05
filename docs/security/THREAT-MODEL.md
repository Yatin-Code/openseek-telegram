# Threat model

## Product boundary

OpenSeek stores and serves NVMT packages through a Telegram-backed pool. Nuvio creates packages locally during playback. An owner can start package generation from the browser dashboard. The dashboard source URL is server-side input and is not sent to a pool client.

## Assets

- Telegram bot token or MTProto session
- Pool write key and dashboard admin secret
- Paired-device token hashes and invitation records
- Pool hints, package digests, and Telegram message identifiers
- Local extraction cache and dashboard generation state
- Vercel Supabase service credentials and temporary upload objects

## Trust boundaries

1. Client to API: TLS and paired-device or pool-key authentication.
2. Dashboard to generation worker: validated source destination, bounded media reads, and a loopback range proxy.
3. API to Telegram: server-held credentials and package validation before publication.
4. Vercel to Supabase: service credentials remain in the server environment; staged packages are temporary.
5. Admin: local/VPS binds to loopback; Vercel checks the admin header and same-origin requests.

## Main threats and controls

| Threat | Control |
|---|---|
| A client uploads malformed or conflicting packages | Validate fingerprint, profile, package bounds, duration, JPEG records, and digest; keep accepted immutable data |
| A client uses a package hint as proof of file identity | Exact fingerprint lookup remains authoritative; hint-ranked matches are marked approximate |
| Dashboard generation targets private network services | Destination policy, DNS checks, redirect revalidation, port limits, and bounded proxy reads |
| A source URL leaks through status or logs | Hold the URL in memory; redact URL queries, bearer tokens, pairing codes, and sensitive headers |
| A client steals pool or Telegram credentials | Never return Telegram credentials; keep write keys and server credentials off clients |
| Public listener is abused | Exact route allowlist, per-address quotas, strikes, and temporary blocks |
| A local process changes owner state | Require the per-install admin secret for dashboard operations |
| Vercel generation exceeds the function limit | Function execution stops at the configured 300-second maximum; generation may not finish |

## Out of scope

Host compromise, access to the protected data directory, vulnerabilities in unreachable dependencies, and deliberate exposure of the owner API remain outside this model.