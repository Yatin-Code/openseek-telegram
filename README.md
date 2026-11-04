# OpenSeek

OpenSeek is the shared NVMT package pool. Telegram is the permanent storage pool. Local SQLite or the Vercel Supabase index helps find packages and can be rebuilt from Telegram. Vercel may use temporary staging while it uploads a package.

Nuvio generates NVMT on the device when playback has no usable pool match. OpenSeek does not generate thumbnails during playback. An owner can start NVMT generation from the browser dashboard. Vercel limits that function to 300 seconds; longer generation work can fail on Vercel. Local and VPS installs run outside that function limit.

The browser dashboard is the only admin interface. There is no desktop app.

## Run OpenSeek

Install Python 3.12 or newer. From the repository:

    cd backend
    python -m venv .venv
    .venv/bin/pip install -r requirements.txt -c requirements-lock.txt
    .venv/bin/python -m openseek serve

On Windows, use .venv\Scripts\pip and .venv\Scripts\python instead. Docker setup is in docs/INSTALL.md. For the Vercel deployment profile, see docs/VERCEL.md.

Open the browser dashboard at http://127.0.0.1:8788. It provides Telegram setup, pairing, pool controls and explicit NVMT generation. On Vercel, use the deployed URL and admin secret.

## Pool model

- Telegram is the canonical, durable package store.
- SQLite on local/VPS installs and Supabase on Vercel are indexes that can be rebuilt from Telegram.
- Vercel staging is temporary. It queues uploads; it does not replace Telegram as package storage.
- A pool node reads and gives NVMT packages. Nuvio and the dashboard can upload packages.
- OpenSeek does not receive video URLs from Nuvio playback requests.
- Pool reads and writes use the API documented in docs/api/CONTRACT-v1.md.

## Security

OpenSeek validates outbound media URLs for dashboard generation. It keeps the source URL in memory and does not write it to the generation status store. Telegram credentials stay on the server. Nuvio receives pool package data, not Telegram credentials.

## Documentation

- docs/INSTALL.md — local and VPS installation
- docs/VERCEL.md — Vercel setup and limits
- docs/TELEGRAM-POOL.md — Telegram storage and pool operation
- docs/ARCHITECTURE.md — service design
- docs/api/CONTRACT-v1.md — pairing and shared pool API
- docs/api/HINTS-v2.md — package hint format and ranking

License: AGPL-3.0-or-later.