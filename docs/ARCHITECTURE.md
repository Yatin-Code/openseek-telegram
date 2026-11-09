# Architecture

OpenSeek serves the shared NVMT pool and an owner dashboard. Telegram is the canonical durable store. Local and VPS installs use SQLite as a rebuildable index. Vercel uses Supabase for the index, upload queue, and temporary staging.

Nuvio creates NVMT locally when playback has no usable pool match. OpenSeek does not extract video during playback. An owner can start NVMT generation from the dashboard. That task extracts frames, packages them as NVMT, validates the package, and publishes it to Telegram. Vercel runs this work in a function with a 300-second maximum duration.

## Request paths

Pool reads:
1. A client requests an exact fingerprint or a hint-ranked candidate.
2. OpenSeek reads package metadata from the local index.
3. OpenSeek returns the NVMT package from Telegram or the configured pool.
4. The client validates and applies the package locally.

Pool writes:
1. Nuvio or an owner dashboard submits NVMT.
2. OpenSeek validates the package and its hints.
3. Local/VPS nodes publish to Telegram directly. Vercel stages the upload and drains the queue through its scheduled tick.
4. Telegram remains the durable copy. The index can be rebuilt.

Dashboard generation:
1. The owner enters a media URL and starts generation.
2. OpenSeek validates the destination and reads the media through its bounded range proxy.
3. A worker decodes frames and creates the NVMT package.
4. OpenSeek validates and publishes the package.
5. The source URL is held in memory. Generation status does not store it.

## Deployments

| Profile | Dashboard | Index and state | Package storage |
|---|---|---|---|
| Local or VPS | Browser dashboard on loopback | SQLite | Telegram |
| Vercel | Browser dashboard at the deployment URL | Supabase | Telegram, with temporary staging during upload |

The dashboard is the only admin interface. The project does not ship a desktop app or Windows installer. Local and VPS servers can still run on Windows from source.

## Boundaries

The shared-pool API serves NVMT packages and pool metadata. It has no player preview, lookup, VTT, or sprite-sheet routes. It does not receive playback URLs from Nuvio. The extraction worker is used only by explicit dashboard generation.

Outbound media access uses the destination policy, DNS checks, bounded HTTP range reads, and the loopback proxy. Telegram credentials stay on the server. Clients authenticate with their paired device token or the configured pool key.

## Main modules

- app.py: API, pairing, admin routes and deployment wiring.
- dashboard.py: shared browser UI using the original OpenSeek visual system.
- admin_catalog.py: thin owner catalogue, preview and moderation adapters over existing stores.
- generation.py: dashboard generation state, package assembly, and publication.
- fetch.py and netpolicy.py: validated, bounded media access.
- worker.py and engine.py: media probing and frame extraction.
- pool.py and channel_pool.py: NVMT validation and shared-pool operations.
- channel_index.py and telegram_mt.py: Telegram package index and storage.
- uploads.py and remote.py: temporary Vercel staging and Supabase state.
- cache.py and db.py: local extraction cache and rebuildable SQLite state.