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
