# Deployment operations: backup, restore, upgrade, teardown

Exact commands for moving, upgrading, and destroying an OpenSeek deployment.
Written and executed for ticket #51 (G9) on 2026-11-07 against the reference
channel-mode node (data `/tmp/qa-g9/data`, ports 24787/24788, shared pool
`-1004391668814`). Every command below is the command that was run; the
observed results are from that run.

Rules:

- Secret values never appear here. Secret **names** do (for example
  `OPENSEEK_POOL_WRITE_KEY`, `OPENSEEK_TELEGRAM_BOT_TOKEN`); the values live in
  `~/.config/openseek/qa.env` (mode 0600) or the deployment's own env file.
- On the campaign host, every command that reads or writes the shared Telegram
  channel is serialised with
  `flock -w 900 /tmp/openseek-qa-channel.lock -c '<command>'`. A real
  single-tenant deployment does not need that lock; this run did because four
  agents shared the pool.
- The daemon must not inherit the lock file descriptor. Start it with
  `3>&-` inside the locked shell, otherwise the running server holds the lock
  forever (observed in the first start of this run).
- Data directories contain `admin.token`, the TLS private key and the SQLite
  index. Treat a backup or a restored copy as a secret.

Placeholders used below: `REPO=/home/yat/openseek-telegram`,
`DATA=/tmp/qa-g9/data`, `ENV=/tmp/qa-g9/openseek-a.env`,
`PIDFILE=/tmp/qa-g9/serve-a2.pid`, `PORT=24787`, `ADMIN_PORT=24788`.

## 1. Backup

Stop the node, then copy the whole data directory. The SQLite database runs in
WAL mode and most rows can live in `openseek.db-wal`; copying only
`openseek.db` loses the index. In this run the stopped data directory had a
4096-byte `openseek.db` and a 618032-byte `openseek.db-wal`.

```sh
# 1. stop the live node
kill -TERM "$(cat $PIDFILE)"
for i in $(seq 1 30); do kill -0 "$(cat $PIDFILE)" 2>/dev/null || break; sleep 1; done
ss -ltn | grep -E ':(24787|24788)\b' || echo 'ports closed'

# 2. take the backup (live/ directory -> backup directory)
mkdir -p /tmp/qa-g9/backup
cp -a /tmp/qa-g9/data /tmp/qa-g9/backup/data

# 3. prove the copy is complete
diff -r /tmp/qa-g9/data /tmp/qa-g9/backup/data && echo 'backup identical'
sha256sum /tmp/qa-g9/backup/data/openseek.db /tmp/qa-g9/backup/data/openseek.db-wal
```

Observed: `node A alive after TERM: no`, `ports 24787/24788 closed`,
`backup and restore directories identical`, and the main database sha256
`2fb9e76bc8b8bb820a77e21342abe7b079f687749503315e13c0cf95106675e0`
(main file and WAL both copied; `admin.token` and `tls/` included).

## 2. Restore into an empty data directory

```sh
# 1. the target must be empty (the restore is a copy, not a merge)
test -z "$(ls -A /tmp/qa-g9/restore/data)" && echo 'empty'
cp -a /tmp/qa-g9/backup/data/. /tmp/qa-g9/restore/data/
diff -r /tmp/qa-g9/backup/data /tmp/qa-g9/restore/data && echo 'restored identical'

# 2. a separate env file that only changes OPENSEEK_DATA_DIR
sed 's#^OPENSEEK_DATA_DIR=.*#OPENSEEK_DATA_DIR=/tmp/qa-g9/restore/data#' \
    /tmp/qa-g9/openseek-a.env > /tmp/qa-g9/openseek-restore.env
chmod 600 /tmp/qa-g9/openseek-restore.env

# 3. start it. The whole startup channel scan runs under the shared lock,
#    and 3>&- keeps the daemon from inheriting the lock descriptor.
cd /home/yat/openseek-telegram
set -a; . /tmp/qa-g9/openseek-restore.env; set +a
export PYTHONPATH=/home/yat/openseek-telegram/backend
flock -w 900 /tmp/openseek-qa-channel.lock -c '
  cd /home/yat/openseek-telegram
  nohup .venv/bin/python -m openseek serve > /tmp/qa-g9/serve-b.log 2>&1 3>&- &
  echo $! > /tmp/qa-g9/serve-b.pid
  for i in $(seq 1 60); do
    curl -sk --max-time 1 https://127.0.0.1:24787/v1/health >/dev/null 2>&1 && exit 0
    sleep 1
  done
  exit 1
'

# 4. the catalogue is the user-visible proof
PYTHONPATH=backend .venv/bin/python -m openseek pool status
```

Observed: the restored node served the same catalogue as the node it was
restored from - `channel_id -1004391668814`, `tiles 9`, `hints 3`,
`episode_packages 0`, `last_message_id 20`, `last_error ""`. It also served
the package byte-for-byte: `GET /v1/pool/413bb6ee.../nvmt?profile=nvmt-v1-g10&duration_ms=30000`
returned 21121 bytes, sha256 `b0eb6a7dfd4d7ccf602e3ff72700774ec4fea53cb7fbe8048871a048288f33d0`
- identical to the original node's bytes (both captured in
`/tmp/qa-g9/fetch-a.json` and `/tmp/qa-g9/fetch-b.json`).

## 3. Upgrade, run twice

The supported upgrade is: stop, update the code, reinstall dependencies, start
again on the same data directory. Database changes in
`backend/openseek/db.py::_migrate` are additive `ALTER TABLE ... ADD COLUMN`
statements; running the same start twice must be a no-op the second time.

```sh
kill -TERM "$(cat $PIDFILE)"
cd /home/yat/openseek-telegram
git pull
.venv/bin/pip install -r backend/requirements.txt
# start with the same OPENSEEK_DATA_DIR as before, then stop and start again
# (the shared-lock start command is the one in section 2)
```

To exercise the migration itself, this run started a release-era fixture (the
additive columns removed) twice and compared the catalogue and row counts:

```sh
rm -rf /tmp/qa-g9/upgrade/data; mkdir -p /tmp/qa-g9/upgrade/data
cp -a /tmp/qa-g9/backup/data/. /tmp/qa-g9/upgrade/data/
sqlite3 /tmp/qa-g9/upgrade/data/openseek.db <<'SQL'
ALTER TABLE devices DROP COLUMN role;
ALTER TABLE devices DROP COLUMN expires;
ALTER TABLE devices DROP COLUMN invite_id;
ALTER TABLE devices DROP COLUMN contribute;
ALTER TABLE invites DROP COLUMN tag;
ALTER TABLE invites DROP COLUMN contribute;
ALTER TABLE pool_channel_outbox DROP COLUMN message_id;
ALTER TABLE pool_hints DROP COLUMN verified;
SQL

# start #1 (same locked start as section 2, with OPENSEEK_DATA_DIR=/tmp/qa-g9/upgrade/data)
# then stop, start #2, then compare:
PYTHONPATH=backend .venv/bin/python -m openseek pool status
.venv/bin/python - <<'PY'
import sqlite3
con = sqlite3.connect('/tmp/qa-g9/upgrade/data/openseek.db')
print(con.execute('PRAGMA integrity_check').fetchone()[0])
for t in ('devices', 'pool_channel_tiles', 'pool_hints', 'pool_episode_packages'):
    print(t, con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0])
print('tiles rows/keys', con.execute('SELECT COUNT(*), COUNT(DISTINCT fp||"/"||profile||"/"||k) FROM pool_channel_tiles').fetchone())
print('hints rows/keys', con.execute('SELECT COUNT(*), COUNT(DISTINCT fp||"/"||profile) FROM pool_hints').fetchone())
PY
```

Observed after start #1: all eight columns were added
(`devices.role/expires/invite_id/contribute`, `invites.tag/contribute`,
`pool_channel_outbox.message_id`, `pool_hints.verified`); the two pre-existing
devices became `role=owner`; `hints 3` and `tiles 9` were unchanged.
After start #2: the catalogue was identical (`tiles 9`, `hints 3`,
`last_message_id 20`), every count was identical, every row count equalled its
distinct key count (`9/9` tiles, `3/3` hints), `PRAGMA integrity_check` was
`ok`, and neither start logged an error.

## 4. Broken migration drill (fail closed)

A migration that cannot run must stop the process and leave no listener.
This run made the legacy schema incompatible on purpose (a previous partial
migration that had added `Role` instead of `role`), then started the node:

```sh
rm -rf /tmp/qa-g9/broken/case; mkdir -p /tmp/qa-g9/broken/case
cp -a /tmp/qa-g9/backup/data/. /tmp/qa-g9/broken/case/
sqlite3 /tmp/qa-g9/broken/case/openseek.db <<'SQL'
ALTER TABLE devices DROP COLUMN role;
ALTER TABLE devices DROP COLUMN expires;
ALTER TABLE devices DROP COLUMN invite_id;
ALTER TABLE devices DROP COLUMN contribute;
ALTER TABLE invites DROP COLUMN tag;
ALTER TABLE invites DROP COLUMN contribute;
ALTER TABLE pool_channel_outbox DROP COLUMN message_id;
ALTER TABLE pool_hints DROP COLUMN verified;
ALTER TABLE devices ADD COLUMN Role TEXT NOT NULL DEFAULT 'owner';
SQL
sed 's#^OPENSEEK_DATA_DIR=.*#OPENSEEK_DATA_DIR=/tmp/qa-g9/broken/case#' \
    /tmp/qa-g9/openseek-a.env > /tmp/qa-g9/openseek-broken.env
chmod 600 /tmp/qa-g9/openseek-broken.env
cd /home/yat/openseek-telegram
set -a; . /tmp/qa-g9/openseek-broken.env; set +a
export PYTHONPATH=/home/yat/openseek-telegram/backend
timeout 30 .venv/bin/python -m openseek serve; echo "exit=$?"
```

Observed: exit code `1`; the traceback ends with
`sqlite3.OperationalError: duplicate column name: role` (raised by
`db.py::_migrate` called from `DB.__init__`); no listener was opened
(`ss -ltn` showed nothing on 24787/24788, and `curl` exited 7 - connection
refused). The node never reached `Core.start`, so it served no request.

## 5. Teardown

Scope note for the campaign: the shared supergroup, the Supabase project and
the shared bot token belong to the running campaign and are **not** destroyed
here. The exact destructive commands are recorded so the campaign-end teardown
is mechanical, and the disposable G9 node was torn down for real.

### 5.1 Rotate the pool API keys (executed)

```sh
# keep the old key only long enough to prove it stops working
OLD_WRITE_KEY=$(grep '^OPENSEEK_POOL_WRITE_KEY=' /tmp/qa-g9/openseek-restore.env | cut -d= -f2-)
NEW_READ=$(openssl rand -hex 32)
NEW_WRITE=$(openssl rand -hex 32)
python - "$NEW_READ" "$NEW_WRITE" <<'PY'
import sys, pathlib
p = pathlib.Path('/tmp/qa-g9/openseek-restore.env')
lines = []
for line in p.read_text().splitlines():
    if line.startswith('OPENSEEK_POOL_READ_KEY='):
        line = 'OPENSEEK_POOL_READ_KEY=' + sys.argv[1]
    elif line.startswith('OPENSEEK_POOL_WRITE_KEY='):
        line = 'OPENSEEK_POOL_WRITE_KEY=' + sys.argv[2]
    lines.append(line)
p.write_text('\n'.join(lines) + '\n')
PY
chmod 600 /tmp/qa-g9/openseek-restore.env
# restart, then prove the old key is dead and the new key lives
FP=413bb6ee018f2dcc751ecd26ae3f3a50469dc76b486fba74f448b567ed6e3d3d
URL="https://127.0.0.1:24787/v1/pool/$FP/nvmt/manifest?profile=nvmt-v1-g10&duration_ms=30000"
curl -sk -o /dev/null -w 'old %{http_code}\n' -H "Authorization: Bearer $OLD_WRITE_KEY" "$URL"
curl -sk -o /dev/null -w 'new %{http_code}\n' -H "Authorization: Bearer $NEW_WRITE_KEY" "$URL"
curl -sk -o /dev/null -w 'none %{http_code}\n' "$URL"
```

Observed: old write key `403`, new write key `200`, no key `403` - the
rotation is effective. (Channel mode ignores `OPENSEEK_POOL_READ_KEY` at
startup and logs `channel pool mode ignores legacy host settings:
POOL_READ_KEY`; the write key is the one that authenticates pool reads and
writes.)

### 5.2 Revoke paired devices (blocked)

```sh
PYTHONPATH=backend .venv/bin/python -m openseek devices
PYTHONPATH=backend .venv/bin/python -m openseek revoke <device_id>
```

Expected: `204`, the device disappears, its token is refused.
Observed on a restored channel node: `HTTP 500`, the device still listed, and
`AttributeError: 'JobManager' object has no attribute 'revoke_device'` in the
node log. This is the defect tracked as #69; it must be fixed before the
device-token part of teardown can run.

**2026-11-08 follow-up:** #69 was subsequently fixed. Fresh current-code
verification in [#52](https://github.com/Yatin-Code/openseek-telegram/issues/52)
observed revoke `204` followed by `403` on the next request. The historical G9
device records have not been retroactively revoked; the HTTP 500 above remains
the result from that original run.

### 5.3 Telegram bot token (human step, not executed)

BotFather: `/mybots` -> `@openseeekbot` -> **API Token** -> **Revoke current
token**. Then replace `OPENSEEK_TELEGRAM_BOT_TOKEN` in the deployment env and
in `~/.config/openseek/qa.env`, restart the node, and update the registry.
Revoking does not touch the supergroup or its history.

### 5.4 Publisher Telegram session (G9 copy executed; shared session not destroyed)

Telegram app -> **Settings** -> **Devices** -> terminate the publisher
session. Then remove every local copy:
`rm -f /tmp/qa-g9/telegram.session` (done in this run) and delete or rotate
`OPENSEEK_TELEGRAM_SESSION` / `OPENSEEK_TELEGRAM_SESSION_FILE` in
`~/.config/openseek/qa.env`. The original operator session at
`~/.config/openseek/telegram.session` is a campaign asset and was left
untouched.

### 5.5 Supabase project (not executed - campaign asset)

```sh
supabase projects delete qilkfmndsuwmopfoihzu --yes
```

This is irreversible and deletes the index, the storage bucket and both API
keys. Before deleting, export anything the campaign still needs. The project is
kept alive while the acceptance campaign runs.

### 5.6 Vercel project (not created / not executed)

```sh
vercel project delete <project-name> --yes --scope <team>
```

No Vercel project was created for G9; delete the participant node only after
its tickets are closed, and remove its environment variables too.

### 5.7 Shared supergroup (not executed - campaign asset)

There is no Bot API call that deletes a supergroup. The owner deletes it from
the Telegram UI (**group info -> Delete group**) only when the campaign no
longer needs the shared pool. Deleting it destroys the durable pool; the
SQLite indexes can be rebuilt only while the channel exists.

### 5.8 Registry and secrets (G9 artefacts done; shared artefacts pending)

- `docs/qa/ARTIFACTS.md`: add a status to every row and mark the G9 node,
  its keys and its session copy **dead**. The G9 ledger is section 6 of this
  file; the shared registry rows stay live until their own teardown runs at
  campaign end (the file was being edited by another agent during G9, so this
  ticket did not append to it).
- `~/.config/openseek/qa.env`: delete the secret after the artefact it belongs
  to is dead; never commit the file.
- Remove local copies of the data directory, backup, env files and session:
  `rm -rf /tmp/qa-g9/data /tmp/qa-g9/backup /tmp/qa-g9/restore/data
  /tmp/qa-g9/upgrade/data /tmp/qa-g9/broken /tmp/qa-g9/*.env
  /tmp/qa-g9/telegram.session` (executed for G9 after the evidence was
  captured; the logs and JSON transcripts under `/tmp/qa-g9` contain no
  secret values).

## 6. G9 artefact ledger

| Artefact | State at end of G9 | Evidence |
|---|---|---|
| Channel-mode node, ports 24787/24788, data `/tmp/qa-g9/data` | dead - stopped, data dir removed | serve logs, `/tmp/qa-g9/status-*.json` |
| G9 pool key pair (read/write) | dead - rotated; old write key 403, new 200 | `/tmp/qa-g9/key-rotation.txt` |
| G9 paired devices (qa-g9-a, qa-g9-a, qa-g9-b on the restored copy) | not revoked - blocked by #69 | `/tmp/qa-g9/devices-before.txt`, `revoke.log` |
| G9 Telegram session copy `/tmp/qa-g9/telegram.session` | dead - local copy deleted | this file |
| Backup `/tmp/qa-g9/backup` | dead - removed after hashes captured | `/tmp/qa-g9/backup-manifest.txt` |
| Shared supergroup `-1004391668814` | live - campaign asset | `docs/qa/ARTIFACTS.md` |
| Supabase project `qilkfmndsuwmopfoihzu` | live - campaign asset | `docs/qa/ARTIFACTS.md` |
| Bot `@openseeekbot` token | live - campaign asset | `docs/qa/ARTIFACTS.md` |
| Publisher session `~/.config/openseek/telegram.session` | live - campaign asset | `docs/qa/ARTIFACTS.md` |
