"""Channel-index storage seam.

`ChannelStore` owns every SQL statement for the rebuildable channel tables. Local/VPS
uses it directly over SQLite; `RemoteChannelStore` implements the same surface over
Supabase/PostgREST. Telegram remains the only durable package store.
"""
from __future__ import annotations

from .cache import PROFILE
from .db import DB

_TILE_COLUMNS = "fp,profile,k,pts_ms,duration_ms,n_tiles,size,sha256,message_id,created"
_HINT_COLUMNS = ("fp,profile,imdb,season,episode,duration_ms,size_bytes,filename_norm,"
                 "n_tiles,grid,created")


def _tile_dict(row) -> dict:
    keys = ("fp", "profile", "k", "pts_ms", "duration_ms", "n_tiles", "size",
            "sha256", "message_id", "created")
    return dict(zip(keys, row))


class ChannelStore:
    """SQLite implementation of the channel index. `DB` keeps connection ownership."""

    def __init__(self, db: DB):
        self.db = db

    # ---------------------------------------------------------------- scan state
    def state(self, channel_id: str) -> tuple[int, float] | None:
        """Read the last indexed message id and scan time. Locked read via DB.q."""
        rows = self.db.q("SELECT last_message_id,scanned_at FROM pool_channel_state WHERE channel_id=?",
                         (channel_id,))
        return (int(rows[0][0]), float(rows[0][1])) if rows else None

    def set_state(self, channel_id: str, last_message_id: int, scanned_at: float) -> None:
        """Upsert one scan checkpoint. Single locked statement."""
        self.db.x(
            "INSERT INTO pool_channel_state(channel_id,last_message_id,scanned_at) VALUES(?,?,?) "
            "ON CONFLICT(channel_id) DO UPDATE SET last_message_id=excluded.last_message_id, "
            "scanned_at=excluded.scanned_at",
            (channel_id, int(last_message_id), float(scanned_at)))

    # ---------------------------------------------------------------- tiles
    def get_tile(self, fp: str, profile: str, k: int) -> dict | None:
        """One validated tile reference. Locked read via DB.q."""
        rows = self.db.q(f"SELECT {_TILE_COLUMNS} FROM pool_channel_tiles "
                         "WHERE fp=? AND profile=? AND k=?", (fp, profile, int(k)))
        return _tile_dict(rows[0]) if rows else None

    def update_tile_metadata(self, tile: dict) -> bool:
        """Fill filename-derived metadata on an already accepted tile. Single locked statement."""
        return self.db.x(
            "UPDATE pool_channel_tiles SET pts_ms=?,duration_ms=?,n_tiles=?,size=? "
            "WHERE fp=? AND profile=? AND k=? AND sha256=? AND duration_ms=0",
            (int(tile["pts_ms"]), int(tile["duration_ms"]), int(tile["n_tiles"]),
             int(tile["size"]), tile["fp"], tile["profile"], int(tile["k"]),
             tile["sha256"])) > 0

    def record_tile(self, tile: dict) -> bool:
        """Insert a tile if absent; accept a matching digest and optionally fill metadata.

        Runs in one BEGIN IMMEDIATE transaction so the first writer wins.
        Returns whether this tile key is accepted (inserted or digest-compatible).
        """
        with self.db._lock:
            self.db.conn.execute("BEGIN IMMEDIATE")
            try:
                accepted, _inserted = self._record_tile_locked(tile)
                self.db.conn.execute("COMMIT")
                return accepted
            except BaseException:
                self.db.conn.execute("ROLLBACK")
                raise

    def record_tiles(self, tiles: list[dict], *, channel_id: str, last_message_id: int,
                     scanned_at: float) -> int:
        """Apply one scan batch and its checkpoint atomically. Returns inserted tile count."""
        with self.db._lock:
            self.db.conn.execute("BEGIN IMMEDIATE")
            try:
                indexed = 0
                for tile in tiles:
                    _accepted, inserted = self._record_tile_locked(tile)
                    indexed += int(inserted)
                self.db.conn.execute(
                    "INSERT INTO pool_channel_state(channel_id,last_message_id,scanned_at) VALUES(?,?,?) "
                    "ON CONFLICT(channel_id) DO UPDATE SET last_message_id=excluded.last_message_id, "
                    "scanned_at=excluded.scanned_at",
                    (channel_id, int(last_message_id), float(scanned_at)))
                self.db.conn.execute("COMMIT")
                return indexed
            except BaseException:
                self.db.conn.execute("ROLLBACK")
                raise

    def _record_tile_locked(self, tile: dict) -> tuple[bool, bool]:
        existing = self.db.conn.execute(
            "SELECT sha256,duration_ms FROM pool_channel_tiles WHERE fp=? AND profile=? AND k=?",
            (tile["fp"], tile["profile"], int(tile["k"]))).fetchone()
        if existing is None:
            self.db.conn.execute(
                "INSERT INTO pool_channel_tiles "
                "(fp,profile,k,pts_ms,duration_ms,n_tiles,size,sha256,message_id,created) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (tile["fp"], tile["profile"], int(tile["k"]), int(tile["pts_ms"]),
                 int(tile["duration_ms"]), int(tile["n_tiles"]), int(tile["size"]),
                 tile["sha256"], int(tile["message_id"]), float(tile.get("created", 0.0))))
            return True, True
        accepted = existing[0] == tile["sha256"]
        if accepted and int(existing[1]) == 0 and int(tile["duration_ms"]) > 0:
            self.db.conn.execute(
                "UPDATE pool_channel_tiles SET pts_ms=?,duration_ms=?,n_tiles=?,size=? "
                "WHERE fp=? AND profile=? AND k=? AND sha256=? AND duration_ms=0",
                (int(tile["pts_ms"]), int(tile["duration_ms"]), int(tile["n_tiles"]),
                 int(tile["size"]), tile["fp"], tile["profile"], int(tile["k"]),
                 tile["sha256"]))
        return accepted, False

    def tiles_for_package(self, fp: str, profile: str) -> list[dict]:
        """All tile rows for a package, ordered by k. Locked read via DB.q."""
        rows = self.db.q(f"SELECT {_TILE_COLUMNS} FROM pool_channel_tiles "
                         "WHERE fp=? AND profile=? ORDER BY k", (fp, profile))
        return [_tile_dict(row) for row in rows]

    def forget_tile(self, fp: str, profile: str, k: int, message_id: int | None = None) -> bool:
        """Drop a stale Telegram reference, optionally only for one message id. Locked write."""
        if message_id is None:
            return self.db.x("DELETE FROM pool_channel_tiles WHERE fp=? AND profile=? AND k=?",
                             (fp, profile, int(k))) > 0
        return self.db.x("DELETE FROM pool_channel_tiles WHERE fp=? AND profile=? AND k=? AND message_id=?",
                         (fp, profile, int(k), int(message_id))) > 0

    # ---------------------------------------------------------------- hints
    def put_hints(self, hints: dict) -> bool:
        """Upsert normalized package hints. Single locked statement."""
        return self.db.x(
            f"INSERT INTO pool_hints({_HINT_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(fp,profile) DO UPDATE SET "
            "imdb=excluded.imdb,season=excluded.season,episode=excluded.episode,"
            "duration_ms=excluded.duration_ms,size_bytes=excluded.size_bytes,"
            "filename_norm=excluded.filename_norm,n_tiles=excluded.n_tiles,"
            "grid=excluded.grid,created=excluded.created",
            (hints["fp"], hints["profile"], hints.get("imdb"), int(hints["season"]),
             int(hints["episode"]), int(hints["duration_ms"]), hints.get("size_bytes"),
             hints.get("filename_norm"), int(hints["n_tiles"]), int(hints["grid"]),
             float(hints["created"]))) > 0

    def get_hints(self, fp: str, profile: str) -> tuple | None:
        """(imdb,season,episode,duration_ms,size_bytes,filename_norm,n_tiles,grid). Locked read."""
        rows = self.db.q(
            "SELECT imdb,season,episode,duration_ms,size_bytes,filename_norm,n_tiles,grid "
            "FROM pool_hints WHERE fp=? AND profile=?", (fp, profile))
        return tuple(rows[0]) if rows else None

    def hint_rows_for_candidates(self, duration_ms: int, tolerance: int,
                                 exclude_fp: str | None) -> list[tuple]:
        """Normalized hint rows joined to their channel tile count. Locked read via DB.q.

        Row order: (fp, profile, imdb, season, episode, duration_ms, size_bytes,
        filename_norm, n_tiles, grid, created, channel_tiles, verified).
        """
        rows = self.db.q(
            "SELECT h.fp,h.profile,h.imdb,h.season,h.episode,h.duration_ms,h.size_bytes,"
            "h.filename_norm,h.n_tiles,h.grid,h.created,COUNT(c.k),h.verified "
            "FROM pool_hints h JOIN pool_channel_tiles c ON c.fp=h.fp AND c.profile=? "
            "AND c.duration_ms=h.duration_ms "
            "WHERE ABS(h.duration_ms-?)<=? AND (? IS NULL OR h.fp<>?) "
            "GROUP BY h.fp,h.profile,h.imdb,h.season,h.episode,h.duration_ms,h.size_bytes,"
            "h.filename_norm,h.n_tiles,h.grid,h.created,h.verified",
            (PROFILE, duration_ms, tolerance, exclude_fp, exclude_fp))
        return [tuple(row) for row in rows]

    # ---------------------------------------------------------------- legacy episode index
    def put_episode_package(self, episode_key: str, fp: str, profile: str,
                            duration_ms: int, updated: float) -> None:
        """Upsert the legacy episode association. Single locked statement."""
        self.db.x(
            "INSERT INTO pool_episode_packages(episode_key,fp,profile,duration_ms,updated) "
            "VALUES(?,?,?,?,?) ON CONFLICT(episode_key,fp,profile) DO UPDATE SET "
            "duration_ms=excluded.duration_ms,updated=excluded.updated",
            (episode_key, fp, profile, int(duration_ms), float(updated)))

    def put_package_index(self, hints: dict, episode_key: str | None = None) -> None:
        """Write a package's hints and legacy episode row in one transaction."""
        with self.db._lock:
            self.db.conn.execute("BEGIN IMMEDIATE")
            try:
                self.db.conn.execute(
                    f"INSERT INTO pool_hints({_HINT_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(fp,profile) DO UPDATE SET "
                    "imdb=excluded.imdb,season=excluded.season,episode=excluded.episode,"
                    "duration_ms=excluded.duration_ms,size_bytes=excluded.size_bytes,"
                    "filename_norm=excluded.filename_norm,n_tiles=excluded.n_tiles,"
                    "grid=excluded.grid,created=excluded.created",
                    (hints["fp"], hints["profile"], hints.get("imdb"), int(hints["season"]),
                     int(hints["episode"]), int(hints["duration_ms"]), hints.get("size_bytes"),
                     hints.get("filename_norm"), int(hints["n_tiles"]), int(hints["grid"]),
                     float(hints["created"])))
                if episode_key:
                    self.db.conn.execute(
                        "INSERT INTO pool_episode_packages(episode_key,fp,profile,duration_ms,updated) "
                        "VALUES(?,?,?,?,?) ON CONFLICT(episode_key,fp,profile) DO UPDATE SET "
                        "duration_ms=excluded.duration_ms,updated=excluded.updated",
                        (episode_key, hints["fp"], hints["profile"], int(hints["duration_ms"]),
                         float(hints["created"])))
                self.db.conn.execute("COMMIT")
            except BaseException:
                self.db.conn.execute("ROLLBACK")
                raise

    def episode_hint_key(self, episode_key: str, duration_ms: int) -> tuple | None:
        """Best hint key for a legacy episode association. Locked read via DB.q."""
        rows = self.db.q(
            "SELECT h.imdb,h.season,h.episode FROM pool_episode_packages e "
            "JOIN pool_hints h ON h.fp=e.fp AND h.profile=e.profile "
            "WHERE e.episode_key=? AND h.imdb IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM pool_channel_tiles c WHERE c.fp=e.fp AND c.profile=?) "
            "ORDER BY ABS(e.duration_ms-?) ASC,e.updated DESC LIMIT 1",
            (episode_key, PROFILE, duration_ms))
        if not rows:
            return None
        imdb, season, episode = rows[0]
        return (imdb, int(season), int(episode))

    def episode_packages_for_key(self, episode_key: str, duration_ms: int, tolerance: int,
                                 limit: int) -> list[tuple]:
        """Legacy duration-only rows with their channel tile count. Locked read via DB.q."""
        rows = self.db.q(
            "SELECT e.fp,e.profile,e.duration_ms,e.updated,COUNT(c.k) "
            "FROM pool_episode_packages e JOIN pool_channel_tiles c ON c.fp=e.fp AND c.profile=? "
            "WHERE e.episode_key=? AND e.duration_ms>0 AND ABS(e.duration_ms-?)<=? "
            "GROUP BY e.fp,e.profile,e.duration_ms,e.updated "
            "ORDER BY ABS(e.duration_ms-?) ASC,e.updated DESC LIMIT ?",
            (PROFILE, episode_key, duration_ms, tolerance, duration_ms,
             max(1, min(int(limit), 8))))
        return [tuple(row) for row in rows]

    # ---------------------------------------------------------------- rebuild / counts
    def clear_channel(self, channel_id: str) -> None:
        """Clear every derived channel-index table for a rebuild, atomically."""
        with self.db._lock:
            self.db.conn.execute("BEGIN IMMEDIATE")
            try:
                self.db.conn.execute("DELETE FROM pool_channel_tiles")
                self.db.conn.execute("DELETE FROM pool_channel_state WHERE channel_id=?", (channel_id,))
                self.db.conn.execute("DELETE FROM pool_hints")
                self.db.conn.execute("DELETE FROM pool_episode_packages")
                self.db.conn.execute("COMMIT")
            except BaseException:
                self.db.conn.execute("ROLLBACK")
                raise

    def counts(self) -> dict[str, int]:
        """Row counts for doctor/status. Three locked reads via DB.q."""
        out = {}
        for key, table in (("tiles", "pool_channel_tiles"), ("hints", "pool_hints"),
                           ("episode_packages", "pool_episode_packages")):
            rows = self.db.q(f"SELECT COUNT(*) FROM {table}")
            out[key] = int(rows[0][0]) if rows else 0
        return out
