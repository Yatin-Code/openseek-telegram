# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses semantic versioning.

## [Unreleased]

### Added
* One browser dashboard across local/VPS and Vercel, with a first-run Telegram setup wizard, private per-install `.env` storage, and durable Supabase-backed device/invitation/sharing state on Vercel.
* Explicit dashboard NVMT generation with progress, bounded job execution, validated package assembly, and Telegram publication; player and pool lookup requests never start generation.
* Participant-owned Vercel setup wizard for creating or linking each participant's own Vercel and Supabase projects,
  authorizing a Telegram user session, joining the shared supergroup, and pairing Nuvio without per-participant bots.
* Token-based participant setup (`openseek setup --provider rest`) driving the same journey through the Vercel and
  Supabase management APIs, for hosts without the provider CLIs.
* Vercel channel-pool deployment profile with Supabase-backed rebuildable index and temporary upload staging; Telegram remains the only durable package store.
* Bounded NVMT staging and publication with retryable/paused upload states, Supabase quota backoff, Telegram FloodWait/slow-mode handling,
  and cleanup of expired or terminal staging objects.
* Sparse exact and hint-matched NVMT retrieval so Nuvio can keep valid shared tiles and fill missing frames locally.
* Telegram librarian support for NVMT package contributions, contributor quotas and moderation, verified hint ranking,
  and per-device candidate offsets and rejections.
* Optional Telegram-backed shared pool for exact-file OpenSeek tiles. Participating instances generate all video
  frames locally; only finished JPEGs are uploaded. The pool uses SQLite metadata, separate read/write bearer keys,
  Telegram `sendDocument` storage, a durable upload outbox, exact-content lookup, and local-cache fallback on misses.
* `docs/TELEGRAM-POOL.md`, `UPSTREAMS.md`, and `LICENSES.md` documenting deployment, provenance, and licenses.

### Removed
* The legacy `/v1/previews`, `/v1/lookup`, VTT, capability-image and sprite-sheet player surfaces; the browser dashboard is the only owner interface.
* The Windows desktop-service installer and frozen Windows server build. Windows servers can still run from source.
* On-demand sheet composition and the old title-library lookup and remapping code. Dashboard NVMT generation keeps the shared extraction worker.
* The native Windows admin application and its build, release, installer and licensing paths. The server's browser dashboard at `http://127.0.0.1:8788` is the admin interface.

### Fixed
* Dashboard generation accepts a blank optional IMDb field instead of failing with `bad_hints` after extraction (#80).
* Dashboard: a rejected admin secret renders an "Admin access denied" notice instead of a broken page, and phone and TV widths wrap pairing and invitation text instead of overflowing (#75).
* Generation: a blocked source answers `400 source_forbidden` instead of surfacing an unhandled 500 (#79).
* Participant setup (#50): resuming an interrupted `openseek setup` skips completed provisioning; the wizard requests explicit Nuvio guest-device access for each invitation, enabling sharing without changing existing contribution choices; and a serialized Telethon StringSession no longer raises ENAMETOOLONG when it is probed as a file path on Python 3.12, which had surfaced as `503 telegram_unavailable` package reads.
* Setup pairing returns `404 not_found` for a missing or invalid setup key, unavailable key, or non-Vercel profile before reading the request body.
* A failed librarian package-index write rolls back its SQLite tile and hint changes, preserves existing rows, and releases the failed submission's quota claim.
* Librarian rollback warnings log only the exception class, keeping raw exception details out of logs.
* Reviewed #78 host-generation repair: host-profile dashboard jobs publish through the host PoolService path.
* Generation packaging preserves scheduled cache slots for frames whose actual PTS is within extraction tolerance, including sparse g5 slots (#78).

## [0.2.0] - 2026-10-19

### Added
* **Windows installer** (`OpenSeekSetup-v0.2.0-Windows-x64.exe`): installs the server (frozen, no Python needed) as an automatic Windows service under a restricted virtual account, with a private data folder, a LAN-only firewall rule, Start Menu entries and an uninstaller. Source code in `installer/`.
* Desktop: automatic connection to the installed server, Start/Stop/Restart via the service, **Connect a TV** (address, one-time pairing code, confirmation), simple and advanced modes, and an advanced server-settings editor.
* Server: optional `OPENSEEK_CONFIG_FILE` (allowlisted keys only), frozen-executable worker dispatch.
* Third-party source offer for the bundled FFmpeg libraries (GPL build), notices and licence texts; the corresponding source archives are attached to the release.

### Licence
* OpenSeek is released under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later); `LICENSE` added and `pyproject.toml` updated. Source code and the Dockerfile are published; prebuilt Docker images are not, because the FFmpeg libraries inside the PyAV wheels are a GPL build.

### Security
* The loopback admin API now requires a per-install secret (`<data>/admin.token` or `OPENSEEK_ADMIN_TOKEN`) on every call except the static dashboard page; new `python -m openseek dashboard`. Previously any local process could pair itself as owner.
* Contributions can no longer overwrite the title ids of an existing cache entry.
* Upstream reads abandon slow-drip sources (wall-clock throughput floor); absurd `Content-Range` totals are rejected.
* The decoder is opened with an empty FFmpeg protocol whitelist; the inbound TLS 1.2 floor is pinned by a test.
* Start-up warnings for a Windows data directory readable by other local users and for running in a container.
* Docker base image pinned by digest; `requirements-lock.txt` pins the full runtime dependency closure.
* Fail-closed deployment rules: a container refuses to start unless `OPENSEEK_PUBLISHED_ON` is loopback or a private LAN address; owner and admin requests carrying proxy headers are refused; `OPENSEEK_ALLOW_PUBLIC_CLIENTS` needs an explicit acknowledgement; Windows refuses a data directory other users can write to.
* Supported Docker LAN setup: `tools/docker-lan.ps1` / `tools/docker-lan.sh` publish the owner API on a private LAN address only and verify the binding. The compose image is now `openseek-selfhosted:local`.
* Upgrade note: the desktop app and any client that sent `X-OpenSeek-Admin: 1` must be updated to the matching version (see `docs/INSTALL.md` "Updating"). Pairings, devices, sharing and invitations carry over unchanged.
* See `docs/security/PRE-RELEASE-REVIEW.md` for the review, the remaining open items and what could not be tested.

## [0.1.0] - Initial public release

### Added
* Seek-preview service: WebVTT plus 320x180 JPEG tiles and 3200x1800 sheets (`tiles`, `sheets` and `hybrid` layouts), generated on demand from HTTP(S) sources with bounded range reads.
* Scheduler: scrub target first (latest wins), playhead window, progressive overview; background byte cap; tolerance of 5 s.
* Persistent per-source worker subprocess (PyAV) behind a loopback range proxy; HDR to SDR tone-mapping.
* Optional experiments, off by default: multi-range prefetch, global request pacing, alternative low-resolution source negotiation, hierarchical scheduling.
* Playback guard: pauses background extraction on rebuffer, stale telemetry or source stall.
* Thumbnail cache with LRU eviction and retention; opt-in title index for cross-release reuse with approximate timeline mapping, offsets and rejections.
* Device pairing (OSP1) with certificate pinning, SHA-256 token storage, expiring capability URLs, CLI (`serve`, `pair`, `devices`, `revoke`, `openapi`).
* Admin interface on loopback with Host/Origin checks and a CSRF header.
* Optional invite-only public guest listener with deny-by-default routing, per-address limits and temporary blocks.
* Contributor Mode: allowlisted, https-only, SSRF-safe contributions with a work-conserving scheduler that yields to the owner, contributor playback telemetry, and preemption with resume.
* SSRF-hardened outbound fetching, log redaction, no telemetry.
* OpenSeek Desktop (PySide6, Windows): dashboard, poster library, live activity, logs, sharing, settings.
* Dockerfile and compose file; benchmark harness (`bench/`); probes and helper scripts (`backend/tools/`).
* Documentation: architecture, security model, installation, API contract, responsible use, licence options.

### Known limitations
See the README section "Status and limitations".
