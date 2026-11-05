"""Supabase/PostgREST channel-index adapter.

PostgREST stores the rebuildable index; Supabase Storage is only temporary upload
staging for the serverless profile. Telegram remains the canonical package store.
No new SDK or database driver is used: plain `httpx` over the REST endpoints.
"""
from __future__ import annotations

from urllib.parse import quote

import httpx

_TILE_COLUMNS = "fp,profile,k,pts_ms,duration_ms,n_tiles,size,sha256,message_id,created"
_HINT_COLUMNS = ("fp,profile,imdb,season,episode,duration_ms,size_bytes,filename_norm,"
                 "n_tiles,grid,created")


class RemoteStoreError(RuntimeError):
    """A PostgREST/Storage failure. The message never contains the service key."""

    def __init__(self, status: int, code: str, message: str):
        self.status = int(status)
        self.code = code
        super().__init__(f"supabase {self.status} {code}: {message}")


class SupabaseClient:
    """Minimal PostgREST + Storage REST client authenticated as the service role."""

    def __init__(self, url: str, service_key: str, *, transport=None,
                 bucket: str = "openseek-staging", timeout: float = 15.0):
        self.base_url = url.rstrip("/")
        self.bucket = bucket
        self._service_key = service_key
        self._client = httpx.Client(
            base_url=self.base_url, transport=transport, timeout=timeout,
            headers={"apikey": service_key, "authorization": f"Bearer {service_key}"})

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, *, params=None, json=None,
                prefer: str | None = None) -> httpx.Response:
        headers = {"prefer": prefer} if prefer else None
        try:
            response = self._client.request(method, path, params=params, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise RemoteStoreError(0, type(exc).__name__, str(exc)) from None
        if response.status_code >= 400:
            payload: dict = {}
            try:
                parsed = response.json()
                payload = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                pass
            code = str(payload.get("code") or f"HTTP_{response.status_code}")
            message = str(payload.get("message") or response.text[:300] or "request failed")
            raise RemoteStoreError(response.status_code, code, message)
        return response

    # ---------------------------------------------------------------- PostgREST helpers
    def select(self, table: str, params) -> list[dict]:
        response = self.request("GET", f"/rest/v1/{table}", params=list(params))
        body = response.json()
        return body if isinstance(body, list) else []

    def upsert(self, table: str, rows: list[dict], *, on_conflict: str,
               ignore_duplicates: bool = False, want_rows: bool = False) -> list[dict]:
        resolution = "ignore-duplicates" if ignore_duplicates else "merge-duplicates"
        prefer = f"resolution={resolution},return={'representation' if want_rows else 'minimal'}"
        response = self.request("POST", f"/rest/v1/{table}",
                                params=[("on_conflict", on_conflict)], json=rows, prefer=prefer)
        if not want_rows:
            return []
        body = response.json()
        return body if isinstance(body, list) else []

    def patch(self, table: str, params, values: dict) -> list[dict]:
        response = self.request("PATCH", f"/rest/v1/{table}", params=list(params), json=values,
                                prefer="return=representation")
        body = response.json()
        return body if isinstance(body, list) else []

    def delete(self, table: str, params) -> list[dict]:
        response = self.request("DELETE", f"/rest/v1/{table}", params=list(params),
                                prefer="return=representation")
        body = response.json()
        return body if isinstance(body, list) else []

    def count(self, table: str, column: str = "fp", filters=()) -> int:
        response = self.request("GET", f"/rest/v1/{table}",
                                params=[("select", column), *filters, ("limit", "1")],
                                prefer="count=exact")
        content_range = response.headers.get("content-range", "")
        total = content_range.rsplit("/", 1)[-1]
        return int(total) if total.isdigit() else 0

    def rpc(self, function: str, arguments: dict):
        """Call a narrowly scoped PostgREST database function."""
        response = self.request("POST", f"/rest/v1/rpc/{quote(function, safe='')}",
                                json=arguments)
        return response.json()

    # ---------------------------------------------------------------- Storage REST (F1b staging only)
    def create_bucket(self, name: str, *, public: bool = False,
                      size_limit: int | None = None) -> bool:
        """Create a Storage bucket and return whether it was created now.

        Idempotent: Storage answers 409 (and on some versions 400) with a
        duplicate-name message when the bucket already exists; both count as
        success. Other failures raise `RemoteStoreError`.

        Used to prepare the temporary upload-staging bucket. Storage is never a
        second durable package store: Telegram is canonical (docs/adr/0001).
        """
        body: dict = {"name": name, "public": bool(public)}
        if size_limit is not None:
            body["file_size_limit"] = int(size_limit)
        try:
            response = self._client.post("/storage/v1/bucket", json=body)
        except httpx.HTTPError as exc:
            raise RemoteStoreError(0, type(exc).__name__, str(exc)) from None
        if response.status_code < 400:
            return True
        duplicate = response.text[:300].lower()
        if response.status_code in (400, 409) and ("duplicate" in duplicate or "exists" in duplicate):
            return False
        raise RemoteStoreError(response.status_code, "bucket_create_failed",
                               response.text[:300] or "bucket creation failed")

    def put_object(self, path: str, data: bytes, content_type: str) -> None:
        try:
            response = self._client.post(
                f"/storage/v1/object/{quote(self.bucket, safe='')}/{quote(path, safe='/')}",
                content=data, headers={"content-type": content_type, "x-upsert": "true"})
        except httpx.HTTPError as exc:
            raise RemoteStoreError(0, type(exc).__name__, str(exc)) from None
        if response.status_code >= 400:
            raise RemoteStoreError(response.status_code, "storage_upload_failed",
                                   response.text[:300] or "upload failed")

    def delete_object(self, path: str) -> None:
        self.request("DELETE",
                     f"/storage/v1/object/{quote(self.bucket, safe='')}/{quote(path, safe='/')}")

    def signed_upload_url(self, path: str, expires_in: int = 900) -> str:
        response = self.request(
            "POST", f"/storage/v1/object/upload/sign/{quote(self.bucket, safe='')}/"
                    f"{quote(path, safe='/')}", json={"expiresIn": int(expires_in)})
        body = response.json()
        if not isinstance(body, dict) or not body.get("url"):
            raise RemoteStoreError(response.status_code, "storage_sign_failed",
                                   "signed upload URL missing")
        return str(body["url"])

    def _object_url(self, path: str) -> str:
        return (f"/storage/v1/object/{quote(self.bucket, safe='')}/"
                f"{quote(path, safe='/')}")

    def get_object(self, path: str, *, max_bytes: int | None = None) -> bytes:
        """Download a staged object, optionally enforcing a limit while streaming it."""
        try:
            with self._client.stream("GET", self._object_url(path)) as response:
                if response.status_code >= 400:
                    payload = {}
                    try:
                        parsed = response.json()
                        payload = parsed if isinstance(parsed, dict) else {}
                    except ValueError:
                        pass
                    code = str(payload.get("code") or f"HTTP_{response.status_code}")
                    message = str(payload.get("message") or response.text[:300] or "request failed")
                    raise RemoteStoreError(response.status_code, code, message)

                declared = response.headers.get("content-length", "")
                if max_bytes is not None and declared.isdigit() and int(declared) > max_bytes:
                    raise RemoteStoreError(413, "object_too_large", "staged object exceeds its limit")
                chunks = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise RemoteStoreError(413, "object_too_large", "staged object exceeds its limit")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as exc:
            raise RemoteStoreError(0, type(exc).__name__, str(exc)) from None

    def object_exists(self, path: str) -> bool:
        """HEAD the staged object. A missing object is a normal answer, not an exception."""
        try:
            response = self._client.head(self._object_url(path))
        except httpx.HTTPError as exc:
            raise RemoteStoreError(0, type(exc).__name__, str(exc)) from None
        if response.status_code == 404:
            return False
        if response.status_code >= 400:
            raise RemoteStoreError(response.status_code, "storage_head_failed",
                                   response.text[:300] or "head failed")
        return True


_UPLOAD_COLUMNS = ("upload_id,device_id,fp,profile,duration_ms,size_bytes,imdb,season,episode,"
                   "filename_norm,object_path,state,created,updated,expires,error")
# Mirrors store._PUBLISH_LEASE_S: a crashed publisher is reclaimable, not stuck.
_PUBLISH_LEASE_S = 300


class RemoteUploadQueueStore:
    """`UploadQueueStore` over PostgREST, with the same optimistic single-row claims.

    PostgREST cannot start a multi-statement transaction, so `claim` relies on a
    conditional PATCH (`state=eq.queued`): the only worker whose PATCH returns a row
    owns the upload. A `publishing` row older than the lease is reclaimed on a later
    tick, so a crash after Telegram acceptance retries the idempotent publication.
    """

    def __init__(self, client: SupabaseClient):
        self.client = client

    @staticmethod
    def _upload(row: dict) -> dict:
        out = dict(row)
        for key in ("duration_ms", "size_bytes", "season", "episode"):
            if out.get(key) is not None:
                out[key] = int(out[key])
        for key in ("created", "updated", "expires"):
            out[key] = float(out[key])
        return out

    def create_upload(self, row: dict) -> None:
        self.client.upsert("pool_uploads", [row], on_conflict="upload_id",
                           ignore_duplicates=True)

    def get_upload(self, upload_id: str) -> dict | None:
        rows = self.client.select(
            "pool_uploads", [("select", _UPLOAD_COLUMNS), ("upload_id", f"eq.{upload_id}"),
                             ("limit", "1")])
        return self._upload(rows[0]) if rows else None

    def pending_count(self) -> int:
        return self.client.count(
            "pool_uploads", "upload_id", [("state", "in.(pending,queued,publishing)")])

    def pending_expired(self, now: float) -> list[dict]:
        rows = self.client.select(
            "pool_uploads",
            [("select", _UPLOAD_COLUMNS), ("state", "eq.pending"),
             ("expires", f"lte.{float(now)}"), ("order", "expires.asc"),
             ("limit", "100")])
        return [self._upload(row) for row in rows]

    def staging_cleanup_candidates(self, limit: int = 100) -> list[dict]:
        rows = []
        for state in ("accepted", "expired", "failed"):
            remaining = max(0, int(limit) - len(rows))
            if remaining == 0:
                break
            candidates = self.client.select(
                "pool_uploads",
                [("select", _UPLOAD_COLUMNS), ("state", f"eq.{state}"),
                 ("object_path", "neq."), ("order", "updated.asc"),
                 ("limit", str(remaining))])
            rows.extend(self._upload(row) for row in candidates)
        return rows

    def mark_staging_deleted(self, upload_id: str, state: str, now: float) -> bool:
        if state not in ("accepted", "expired", "failed"):
            return False
        rows = self.client.patch(
            "pool_uploads",
            [("upload_id", f"eq.{upload_id}"), ("state", f"eq.{state}"),
             ("object_path", "neq.")],
            {"object_path": "", "updated": float(now)})
        return bool(rows)

    def expire(self, upload_id: str, now: float) -> bool:
        rows = self.client.patch(
            "pool_uploads",
            [("upload_id", f"eq.{upload_id}"), ("state", "eq.pending"),
             ("expires", f"lte.{float(now)}")],
            {"state": "expired", "updated": float(now), "error": "expired"})
        return bool(rows)

    def enqueue(self, upload_id: str, now: float) -> bool:
        return self.client.rpc("enqueue_pool_publication", {
            "p_upload_id": upload_id, "p_now": float(now)}) is True

    def claim(self, now: float) -> dict | None:
        due = self.client.select(
            "pool_publication_queue",
            [("select", "upload_id,attempts,state"), ("state", "eq.queued"),
             ("next_attempt", f"lte.{float(now)}"), ("order", "next_attempt.asc"),
             ("limit", "8")])
        if not due:
            stale_before = float(now) - _PUBLISH_LEASE_S
            due = self.client.select(
                "pool_publication_queue",
                [("select", "upload_id,attempts,state"), ("state", "eq.publishing"),
                 ("updated", f"lte.{stale_before}"), ("order", "updated.asc"),
                 ("limit", "8")])
        else:
            stale_before = None
        for candidate in due:
            state = candidate.get("state", "queued")
            filters = [("upload_id", f"eq.{candidate['upload_id']}"), ("state", f"eq.{state}")]
            if state == "publishing":
                stale_before = float(now) - _PUBLISH_LEASE_S
                filters.append(("updated", f"lte.{stale_before}"))
            claimed = self.client.patch(
                "pool_publication_queue", filters,
                {"state": "publishing", "updated": float(now)})
            if not claimed:
                continue
            upload_id = candidate["upload_id"]
            self.client.patch(
                "pool_uploads",
                [("upload_id", f"eq.{upload_id}"), ("state", "eq.queued")],
                {"state": "publishing", "updated": float(now)})
            row = self.get_upload(upload_id)
            if row is None:
                self.client.patch(
                    "pool_publication_queue",
                    [("upload_id", f"eq.{upload_id}"), ("state", "eq.publishing")],
                    {"state": "failed", "updated": float(now), "error": "unknown_upload"})
                continue
            row["attempts"] = int(candidate.get("attempts", 0))
            return row
        return None

    def retry(self, upload_id: str, *, attempts: int, next_attempt: float, error: str,
              terminal: bool, now: float) -> None:
        state = "failed" if terminal else "queued"
        self.client.patch(
            "pool_publication_queue", [("upload_id", f"eq.{upload_id}")],
            {"attempts": int(attempts), "next_attempt": float(next_attempt), "state": state,
             "updated": float(now), "error": error})
        self.client.patch(
            "pool_uploads", [("upload_id", f"eq.{upload_id}")],
            {"state": state, "updated": float(now), "error": error})

    def complete(self, upload_id: str, now: float) -> None:
        self.client.patch(
            "pool_uploads", [("upload_id", f"eq.{upload_id}")],
            {"state": "accepted", "updated": float(now), "error": None})
        self.client.patch(
            "pool_publication_queue", [("upload_id", f"eq.{upload_id}")],
            {"state": "done", "updated": float(now), "error": None})


class RemoteChannelStore:
    """`ChannelStore` surface implemented over PostgREST tables and the package view."""

    def __init__(self, client: SupabaseClient):
        self.client = client

    # ---------------------------------------------------------------- scan state
    def state(self, channel_id: str) -> tuple[int, float] | None:
        rows = self.client.select(
            "pool_channel_state",
            [("select", "last_message_id,scanned_at"),
             ("channel_id", f"eq.{channel_id}"), ("limit", "1")])
        if not rows:
            return None
        return int(rows[0]["last_message_id"]), float(rows[0]["scanned_at"])

    def set_state(self, channel_id: str, last_message_id: int, scanned_at: float) -> None:
        self.client.upsert("pool_channel_state", [{
            "channel_id": channel_id, "last_message_id": int(last_message_id),
            "scanned_at": float(scanned_at)}], on_conflict="channel_id")

    # ---------------------------------------------------------------- tiles
    def get_tile(self, fp: str, profile: str, k: int) -> dict | None:
        rows = self.client.select(
            "pool_channel_tiles",
            [("select", _TILE_COLUMNS), ("fp", f"eq.{fp}"), ("profile", f"eq.{profile}"),
             ("k", f"eq.{int(k)}"), ("limit", "1")])
        return _tile(rows[0]) if rows else None

    def _insert_tile(self, tile: dict) -> bool:
        rows = self.client.upsert("pool_channel_tiles", [self._row(tile)],
                                  on_conflict="fp,profile,k", ignore_duplicates=True,
                                  want_rows=True)
        return bool(rows)

    def update_tile_metadata(self, tile: dict) -> bool:
        rows = self.client.patch(
            "pool_channel_tiles",
            [("fp", f"eq.{tile['fp']}"), ("profile", f"eq.{tile['profile']}"),
             ("k", f"eq.{int(tile['k'])}"), ("sha256", f"eq.{tile['sha256']}"),
             ("duration_ms", "eq.0")],
            {"pts_ms": int(tile["pts_ms"]), "duration_ms": int(tile["duration_ms"]),
             "n_tiles": int(tile["n_tiles"]), "size": int(tile["size"])})
        return bool(rows)

    def record_tile(self, tile: dict) -> bool:
        existing = self.get_tile(tile["fp"], tile["profile"], int(tile["k"]))
        if existing is None:
            inserted = self._insert_tile(tile)
            if inserted:
                return True
            existing = self.get_tile(tile["fp"], tile["profile"], int(tile["k"]))
            if existing is None:                 # RLS/permission misconfiguration
                return False
        if existing["sha256"] != tile["sha256"]:
            return False
        if int(existing["duration_ms"]) == 0 and int(tile["duration_ms"]) > 0:
            self.update_tile_metadata(tile)
        return True

    def record_tiles(self, tiles: list[dict], *, channel_id: str, last_message_id: int,
                     scanned_at: float) -> int:
        """Apply a scan batch. PostgREST has no client-side transaction; the checkpoint
        is written last so an interrupted scan is retried from the previous position."""
        indexed = 0
        for tile in tiles:
            existing = self.get_tile(tile["fp"], tile["profile"], int(tile["k"]))
            if existing is None:
                if self._insert_tile(tile):
                    indexed += 1
                    continue
                existing = self.get_tile(tile["fp"], tile["profile"], int(tile["k"]))
                if existing is None:
                    continue
            if (existing["sha256"] == tile["sha256"] and
                    int(existing["duration_ms"]) == 0 and int(tile["duration_ms"]) > 0):
                self.update_tile_metadata(tile)
        self.set_state(channel_id, last_message_id, scanned_at)
        return indexed

    def tiles_for_package(self, fp: str, profile: str) -> list[dict]:
        rows = self.client.select(
            "pool_channel_tiles",
            [("select", _TILE_COLUMNS), ("fp", f"eq.{fp}"), ("profile", f"eq.{profile}"),
             ("order", "k.asc")])
        return [_tile(row) for row in rows]

    def forget_tile(self, fp: str, profile: str, k: int, message_id: int | None = None) -> bool:
        params = [("fp", f"eq.{fp}"), ("profile", f"eq.{profile}"), ("k", f"eq.{int(k)}")]
        if message_id is not None:
            params.append(("message_id", f"eq.{int(message_id)}"))
        return bool(self.client.delete("pool_channel_tiles", params))

    # ---------------------------------------------------------------- hints
    def put_hints(self, hints: dict) -> bool:
        self.client.upsert("pool_hints", [self._hint_row(hints)], on_conflict="fp,profile")
        return True

    def get_hints(self, fp: str, profile: str) -> tuple | None:
        rows = self.client.select(
            "pool_hints",
            [("select", "imdb,season,episode,duration_ms,size_bytes,filename_norm,n_tiles,grid"),
             ("fp", f"eq.{fp}"), ("profile", f"eq.{profile}"), ("limit", "1")])
        if not rows:
            return None
        row = rows[0]
        return (row.get("imdb"), int(row["season"]), int(row["episode"]),
                int(row["duration_ms"]), row.get("size_bytes"), row.get("filename_norm"),
                int(row["n_tiles"]), int(row["grid"]))

    def hint_rows_for_candidates(self, duration_ms: int, tolerance: int,
                                 exclude_fp: str | None) -> list[tuple]:
        params = [
            ("select", f"{_HINT_COLUMNS},verified"),
            ("duration_ms", f"gte.{max(0, int(duration_ms) - int(tolerance))}"),
            ("duration_ms", f"lte.{int(duration_ms) + int(tolerance)}"),
        ]
        if exclude_fp is not None:
            params.append(("fp", f"neq.{exclude_fp}"))
        hint_rows = self.client.select("pool_hints", params)
        if not hint_rows:
            return []
        fps = sorted({row["fp"] for row in hint_rows})
        package_rows = self.client.select(
            "pool_packages",
            [("select", "fp,profile,channel_tiles"), ("fp", f"in.({','.join(fps)})")])
        channels = {(row["fp"], row["profile"]): int(row["channel_tiles"])
                    for row in package_rows}
        out = []
        for row in hint_rows:
            key = (row["fp"], row["profile"])
            if key not in channels:
                continue
            out.append((row["fp"], row["profile"], row.get("imdb"), int(row["season"]),
                        int(row["episode"]), int(row["duration_ms"]), row.get("size_bytes"),
                        row.get("filename_norm"), int(row["n_tiles"]), int(row["grid"]),
                        float(row["created"]), channels[key], int(row.get("verified", 0))))
        return out

    # ---------------------------------------------------------------- legacy episode index
    def put_episode_package(self, episode_key: str, fp: str, profile: str,
                            duration_ms: int, updated: float) -> None:
        self.client.upsert("pool_episode_packages", [{
            "episode_key": episode_key, "fp": fp, "profile": profile,
            "duration_ms": int(duration_ms), "updated": float(updated)}],
            on_conflict="episode_key,fp,profile")

    def put_package_index(self, hints: dict, episode_key: str | None = None) -> None:
        """PostgREST has no multi-statement transaction; hints are written first and the
        rebuildable episode association follows. A failed retry is idempotent."""
        self.put_hints(hints)
        if episode_key:
            self.put_episode_package(episode_key, hints["fp"], hints["profile"],
                                     int(hints["duration_ms"]), float(hints["created"]))

    def episode_hint_key(self, episode_key: str, duration_ms: int) -> tuple | None:
        episodes = self.client.select(
            "pool_episode_packages",
            [("select", "fp,profile,duration_ms"), ("episode_key", f"eq.{episode_key}"),
             ("order", "updated.desc"), ("limit", "1000")])
        if not episodes:
            return None
        episodes.sort(key=lambda row: abs(int(row["duration_ms"]) - int(duration_ms)))
        fps = sorted({row["fp"] for row in episodes})
        hints = self.client.select(
            "pool_hints",
            [("select", "fp,profile,imdb,season,episode"), ("imdb", "not.is.null"),
             ("fp", f"in.({','.join(fps)})")])
        hint_by_key = {(row["fp"], row["profile"]): row for row in hints}
        packages = self.client.select(
            "pool_packages",
            [("select", "fp,profile"), ("fp", f"in.({','.join(fps)})")])
        with_tiles = {(row["fp"], row["profile"]) for row in packages}
        for row in episodes:
            key = (row["fp"], row["profile"])
            hint = hint_by_key.get(key)
            if hint is not None and key in with_tiles:
                return hint["imdb"], int(hint["season"]), int(hint["episode"])
        return None

    def episode_packages_for_key(self, episode_key: str, duration_ms: int, tolerance: int,
                                 limit: int) -> list[tuple]:
        episodes = self.client.select(
            "pool_episode_packages",
            [("select", "fp,profile,duration_ms,updated"), ("episode_key", f"eq.{episode_key}"),
             ("duration_ms", f"gte.{max(0, int(duration_ms) - int(tolerance))}"),
             ("duration_ms", f"lte.{int(duration_ms) + int(tolerance)}"),
             ("order", "updated.desc"), ("limit", "1000")])
        if not episodes:
            return []
        fps = sorted({row["fp"] for row in episodes})
        packages = self.client.select(
            "pool_packages",
            [("select", "fp,profile,channel_tiles"), ("fp", f"in.({','.join(fps)})")])
        channels = {(row["fp"], row["profile"]): int(row["channel_tiles"])
                    for row in packages}
        episodes.sort(key=lambda row: (abs(int(row["duration_ms"]) - int(duration_ms)),
                                       -float(row["updated"])))
        out = []
        for row in episodes[:max(1, min(int(limit), 8))]:
            count = channels.get((row["fp"], row["profile"]))
            if count is None:
                continue
            out.append((row["fp"], row["profile"], int(row["duration_ms"]),
                        float(row["updated"]), count))
        return out

    # ---------------------------------------------------------------- rebuild / counts
    def clear_channel(self, channel_id: str) -> None:
        """Clear derived channel tables. PostgREST cannot wrap these in one transaction;
        rebuild starts from message zero, so a partial clear is safe to retry."""
        self.client.delete("pool_channel_tiles", [("fp", "not.is.null")])
        self.client.delete("pool_hints", [("fp", "not.is.null")])
        self.client.delete("pool_episode_packages", [("episode_key", "not.is.null")])
        self.client.delete("pool_channel_state", [("channel_id", f"eq.{channel_id}")])

    def counts(self) -> dict[str, int]:
        return {"tiles": self.client.count("pool_channel_tiles"),
                "hints": self.client.count("pool_hints"),
                "episode_packages": self.client.count("pool_episode_packages")}

    # ---------------------------------------------------------------- row mapping
    @staticmethod
    def _row(tile: dict) -> dict:
        return {"fp": tile["fp"], "profile": tile["profile"], "k": int(tile["k"]),
                "pts_ms": int(tile["pts_ms"]), "duration_ms": int(tile["duration_ms"]),
                "n_tiles": int(tile["n_tiles"]), "size": int(tile["size"]),
                "sha256": tile["sha256"], "message_id": int(tile["message_id"]),
                "created": float(tile.get("created", 0.0))}

    @staticmethod
    def _hint_row(hints: dict) -> dict:
        return {"fp": hints["fp"], "profile": hints["profile"], "imdb": hints.get("imdb"),
                "season": int(hints["season"]), "episode": int(hints["episode"]),
                "duration_ms": int(hints["duration_ms"]),
                "size_bytes": hints.get("size_bytes"),
                "filename_norm": hints.get("filename_norm"),
                "n_tiles": int(hints["n_tiles"]), "grid": int(hints["grid"]),
                "created": float(hints["created"])}


def _tile(row: dict) -> dict:
    return {"fp": row["fp"], "profile": row["profile"], "k": int(row["k"]),
            "pts_ms": int(row["pts_ms"]), "duration_ms": int(row["duration_ms"]),
            "n_tiles": int(row["n_tiles"]), "size": int(row["size"]),
            "sha256": row["sha256"], "message_id": int(row["message_id"]),
            "created": float(row["created"])}
