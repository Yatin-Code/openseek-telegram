"""Browser first-run setup: credentials are verified, stored privately, and applied live."""
from __future__ import annotations

import os
import stat

import openseek.app as app_module
from openseek.config import Settings


class TelegramProbe:
    def __init__(self, settings):
        self.settings = settings
        self.can_post = None
        self.detected_can_post = None
        self.entity_type = "unknown"
        self.entity_resolved = False
        self.history_readable = False
        self.post_permission_error = ""

    def start(self):
        self.can_post = self.detected_can_post = True
        self.entity_type = "broadcast"
        self.entity_resolved = self.history_readable = True

    def scan(self, _min_id=0, _limit=1):
        assert self.history_readable
        return []

    def close(self):
        pass


def test_browser_setup_verifies_and_saves_telegram_settings(live_factory, monkeypatch):
    monkeypatch.setattr(app_module, "TelegramMTProtoStore", TelegramProbe)
    server = live_factory(None)
    with server.admin_client() as admin:
        assert admin.get("/admin/setup").json() == {"profile": "local", "setup_required": True}
        response = admin.post("/admin/setup", json={
            "telegram_channel_id": "-1001234567890",
            "telegram_api_id": 12345,
            "telegram_api_hash": "example-api-hash",
            "telegram_bot_token": "12345:example-bot-token",
        })

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["configured"] is True
    assert result["history_readable"] is True
    assert result["channel_writable"] is True
    assert server.core.channel_index.status()["scanned_at"] > 0

    env_file = server.core.data_dir / ".env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    restored = Settings.from_env({"OPENSEEK_ENV_FILE": str(env_file)})
    assert restored.effective_pool_mode == "channel"
    assert restored.telegram_channel_id == "-1001234567890"
    assert restored.telegram_bot_token == "12345:example-bot-token"
    assert "12345:example-bot-token" not in response.text

    with server.admin_client() as admin:
        assert admin.get("/admin/setup").json()["setup_required"] is False


def test_browser_setup_rejects_session_file_and_does_not_write_credentials(live_factory):
    server = live_factory(None)
    with server.admin_client() as admin:
        response = admin.post("/admin/setup", json={
            "telegram_channel_id": "-1001234567890",
            "telegram_api_id": 12345,
            "telegram_api_hash": "example-api-hash",
            "telegram_session": "/tmp/user.session",
        })
    assert response.status_code == 400
    assert response.json() == {"error": "telegram_session_must_be_a_string_session"}
    assert not (server.core.data_dir / ".env").exists()
