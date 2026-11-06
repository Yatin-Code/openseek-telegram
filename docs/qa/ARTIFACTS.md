# OpenSeek QA artefacts

The reference deployment built for Epic G, and the inputs Epic H needs.

Rules:

- Public identifier: write the value. A URL, a channel id, a bot username, a project ref.
- Secret: write the location only. Never the value.
- Secrets live in `~/.config/openseek/qa.env` (mode 0600, outside this repository).
- Nothing in this file may be pasted into an issue body until it has been checked against these rules.

## Reference deployment

| Artefact | Value |
|---|---|
| Telegram bot | @openseeekbot (id 9277790259) — token in qa.env, key OPENSEEK_TELEGRAM_BOT_TOKEN |
| Shared pool | "openseek pool", supergroup id -1004391668814 |
| Pool invite | https://t.me/+mzUOB2hd1VczYzRl |
| Publisher account | session file ~/.config/openseek/telegram.session (key OPENSEEK_TELEGRAM_SESSION also stored) |
| Supabase project | qilkfmndsuwmopfoihzu, https://qilkfmndsuwmopfoihzu.supabase.co |
| Supabase org | omhprlsnyrhinrlpvibz |
| Supabase keys | qa.env: SUPABASE_ANON_KEY, SUPABASE_SERVICE_KEY, SUPABASE_DB_PASSWORD |
| Supabase bucket | openseek-staging (private, 16 MiB) |
| Vercel deployment | not created yet |
| Vercel team | yatdoescode-8909 (CLI session) |
| Client artefact | not installed yet |
| Retired | broadcast channel -1003859844064, replaced by the supergroup above |
| Extra | organisation "openseek-qa" (knmqhdqymbsmzebmebhv), created while diagnosing a 403; deletable |

## Published QA package

| Field | Value |
|---|---|
| Source | /tmp/qa-g2/source.mp4 (synthetic, barcode frames, 30 s) |
| Fingerprint | see /tmp/qa-g2/report.json |
| Package | /tmp/qa-g2/package.nvmt, 21121 bytes, 3 slots |
| Telegram messages | 3, 4, 5 (tiles) and 6 (manifest) |
| Verification | all three tiles re-downloaded and sha256-matched; manifest fingerprint matched |

## Channel-mode node (G3)

| Field | Value |
|---|---|
| Base URL | https://127.0.0.1:8787 (loopback, self-signed; pin in the node log) |
| Data directory | /tmp/qa-g3/data |
| Mode | channel, entity megagroup, history readable, writable |
| Paired device | 95b0f1b74973819c (name qa-g3) |
| Admin listener | http://127.0.0.1:8788 |
| Indexed pool | 3 tiles, 1 manifest, last message id 6 |
| Served package | fp 413bb6ee018f2dcc751ecd26ae3f3a50469dc76b486fba74f448b567ed6e3d3d |
| Served bytes | 21121, sha256 b0eb6a7dfd4d7ccf602e3ff72700774ec4fea53cb7fbe8048871a048288f33d0 (identical to the built package) |
| Removed | the librarian second contribution (messages 7-10) after it exposed the manifest defect below |
