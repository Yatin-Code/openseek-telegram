"""Durable owner, device, invitation, and sharing state for Vercel."""
from __future__ import annotations

import time


_DEVICE_COLUMNS = "device_id,name,token_hash,created,last_seen,role,expires,invite_id,contribute"
_INVITE_COLUMNS = ("pair_id,label,role,salt,iterations,k_hex,created,expires,max_uses,uses,attempts,"
                   "revoked,device_days,tag,contribute")


class RemoteAdminStore:
    """The service-role-only Supabase adapter for records SQLite owns on local installs."""

    def __init__(self, client):
        self.client = client

    # ---------------------------------------------------------------- devices
    def auth_rows(self) -> list[tuple[str, str, str, float | None]]:
        rows = self.client.select("pool_devices", [("select", "token_hash,device_id,role,expires")])
        return [(r["token_hash"], r["device_id"], r["role"], r.get("expires")) for r in rows]

    def add_device(self, device_id: str, name: str, token_hash: str, role: str = "owner",
                   expires: float | None = None, invite_id: str | None = None) -> None:
        self.client.upsert("pool_devices", [{
            "device_id": device_id, "name": name, "token_hash": token_hash,
            "created": time.time(), "last_seen": time.time(), "role": role,
            "expires": expires, "invite_id": invite_id, "contribute": False,
        }], on_conflict="device_id")

    def devices(self) -> list[dict]:
        rows = self.client.select("pool_devices", [("select", _DEVICE_COLUMNS), ("order", "created.asc")])
        return [{**row, "contribute": bool(row.get("contribute"))} for row in rows]

    def touch_device(self, device_id: str) -> None:
        self.client.patch("pool_devices", [("device_id", f"eq.{device_id}")],
                          {"last_seen": time.time()})

    def remove_device(self, device_id: str) -> bool:
        return bool(self.client.delete("pool_devices", [("device_id", f"eq.{device_id}")]))

    def set_contribute(self, device_id: str, on: bool) -> bool:
        return bool(self.client.patch(
            "pool_devices", [("device_id", f"eq.{device_id}"), ("role", "eq.guest")],
            {"contribute": bool(on)}))

    def can_contribute(self, device_id: str) -> bool:
        rows = self.client.select(
            "pool_devices", [("select", "contribute"), ("device_id", f"eq.{device_id}"),
                             ("role", "eq.guest"), ("limit", "1")])
        return bool(rows and rows[0].get("contribute"))

    # ---------------------------------------------------------------- invitations
    def add_invite(self, pair_id: str, label: str, role: str, salt: str, iterations: int,
                   k_hex: str, created: float, expires: float, max_uses: int,
                   device_days: float | None, tag: str | None = None,
                   contribute: bool = False) -> None:
        self.client.upsert("pool_invites", [{
            "pair_id": pair_id, "label": label, "role": role, "salt": salt,
            "iterations": int(iterations), "k_hex": k_hex, "created": float(created),
            "expires": float(expires), "max_uses": int(max_uses), "uses": 0,
            "attempts": 0, "revoked": None, "device_days": device_days,
            "tag": tag, "contribute": bool(contribute),
        }], on_conflict="pair_id")

    def tag_in_use(self, tag: str, now: float) -> bool:
        return bool(self.client.select(
            "pool_invites", [("select", "pair_id"), ("tag", f"eq.{tag}"),
                             ("revoked", "is.null"), ("expires", f"gt.{float(now)}"),
                             ("uses", "lt.1"), ("limit", "1")]))

    def invite_by_tag(self, tag: str, now: float) -> str | None:
        rows = self.client.select(
            "pool_invites", [("select", "pair_id"), ("tag", f"eq.{tag}"),
                             ("revoked", "is.null"), ("expires", f"gt.{float(now)}"),
                             ("uses", "lt.1"), ("attempts", "lt.5"),
                             ("order", "created.desc"), ("limit", "1")])
        return rows[0]["pair_id"] if rows else None

    def invite(self, pair_id: str) -> dict | None:
        rows = self.client.select(
            "pool_invites", [("select", _INVITE_COLUMNS), ("pair_id", f"eq.{pair_id}"), ("limit", "1")])
        return rows[0] if rows else None

    def invites(self) -> list[dict]:
        return self.client.select("pool_invites", [("select", _INVITE_COLUMNS), ("order", "created.desc")])

    def bump_invite_attempts(self, pair_id: str) -> None:
        self.client.rpc("record_pool_invite_failure", {"p_pair_id": pair_id})

    def use_invite(self, pair_id: str, now: float, max_attempts: int) -> bool:
        invite = self.invite(pair_id)
        if invite is None:
            return False
        rows = self.client.patch(
            "pool_invites",
            [("pair_id", f"eq.{pair_id}"), ("revoked", "is.null"),
             ("uses", f"eq.{int(invite['uses'])}"),
             ("uses", f"lt.{int(invite['max_uses'])}"),
             ("attempts", f"lt.{int(max_attempts)}"), ("expires", f"gt.{float(now)}")],
            {"uses": int(invite["uses"]) + 1})
        return bool(rows)

    def revoke_invite(self, pair_id: str, now: float) -> bool:
        return bool(self.client.patch(
            "pool_invites", [("pair_id", f"eq.{pair_id}"), ("revoked", "is.null")],
            {"revoked": float(now)}))

    # ---------------------------------------------------------------- owner settings
    def get_setting(self, key: str) -> str | None:
        rows = self.client.select("pool_owner_settings", [("select", "v"), ("k", f"eq.{key}"), ("limit", "1")])
        return str(rows[0]["v"]) if rows else None

    def set_setting(self, key: str, value: str) -> None:
        self.client.upsert("pool_owner_settings", [{"k": key, "v": value}], on_conflict="k")
