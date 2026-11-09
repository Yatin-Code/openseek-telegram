# OpenSeek dashboard

The shared browser dashboard carries the original OpenSeek desktop appearance into the current NVMT pool product. It reuses the existing admin routes and backend services. The native desktop runtime remains retired.

Open the URL printed by `python -m openseek dashboard` on local/VPS installs, or `https://<node>/#<OPENSEEK_ADMIN_TOKEN>` on Vercel. The secret is read from the fragment once, retained in session storage, and sent in the admin request header. Library preview images are fetched with that header and displayed as blob URLs.

| Destination | Integrated features |
| --- | --- |
| Dashboard | Pool/cache totals, active work, sharing and channel state, recent generation outcomes |
| Library | Published hint catalogue, preview frames, search within the current page, pagination and refresh |
| Live activity | Existing generation form, optional media hints, job progress, errors, details and cancellation |
| Sharing | Pairing and code copying, devices and revocation, guest contribution permission, global sharing, invitation options and revocation |
| Settings | Telegram first-run setup, connection details, scan/rebuild, host purge preview and confirmation, public-block clearing, contributor moderation |
| Diagnostics | Actual channel, queue, cache and public-listener status |

Library and contributor routes are thin adapters in `admin_catalog.py`; the current SQLite/Supabase stores, package retrieval, and CLI moderation operations remain their source of truth. No schema or public protocol changes are needed. Vercel library access uses the existing Telegram session lease. Contributor moderation requires a durable local pool database and is unavailable on Vercel's scratch SQLite store. Host purge only applies to host mode; scan/rebuild only applies to channel mode.

Future owner features belong in these same screens. Reuse the design documented in [DESIGN.md](../DESIGN.md), wire the current backend operation, and exercise its loading, empty, error, busy, success, and unavailable states. Polling must retain drafts, current navigation, focus, expanded details and scroll position. A completed backend ticket is not complete for dashboard users until its owner control is integrated here.

The browser dashboard is self-contained in a Python module so the existing source/VPS/Vercel packaging paths include the same UI without a separate asset build or CDN.
