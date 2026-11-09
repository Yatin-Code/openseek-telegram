# Telegram is the canonical shared pool store

OpenSeek keeps accepted NVMT packages and their manifests in Telegram as the durable shared pool. Local/VPS SQLite and Vercel Supabase PostgreSQL hold rebuildable indexes; Supabase Storage is temporary Vercel upload staging, not a second package store. Vercel uploads are staged only to bypass the function request limit, then published to Telegram with retry/status tracking. Downloads stream from Telegram through OpenSeek to Nuvio without staging. This keeps the shared data in one user-understood pool while allowing both self-hosted and serverless deployments.

## Accepted host-mode g5 exception (G4 #48, 2026-11-08)

Host/VPS nodes retain the exact uploaded NVMT package on that node. Telegram carries the shared ten-second tile representation; when the host package uses the five-second g5 grid, publication samples the slots nearest each ten-second point. That Telegram representation cannot rebuild every exact g5 slot. This is an accepted storage trade-off: exact host-local g5 retention remains supported, while exact cross-node g5 reconstruction is not promised. This decision does not create a second shared canonical package store.
