"""OPENSEEK_CONFIG_FILE: allowlisted keys only, environment wins, never crashes."""
import json
import logging

import pytest

from openseek.config import ConfigError, MiB, Settings
from openseek.tls import ensure_certificate


def load(tmp_path, content, env=None, raw=False):
    p = tmp_path / "openseek.json"
    if raw:
        p.write_bytes(content)
    else:
        p.write_text(json.dumps(content), encoding="utf-8")
    e = {"OPENSEEK_CONFIG_FILE": str(p)}
    e.update(env or {})
    return Settings.from_env(e)


def test_allowlisted_keys_applied(tmp_path):
    s = load(tmp_path, {"port": 9001, "admin_port": 9002, "cache_max_bytes": 512 * MiB, "retention_days": 7,
                        "bg_mbps": 4.5, "workers_per_source": 3, "log_level": "DEBUG",
                        "public_port": 9003, "public_host": "x.example.org:443"})
    assert (s.port, s.admin_port, s.cache_max_bytes, s.retention_days, s.bg_mbps) == (9001, 9002, 512 * MiB, 7.0, 4.5)
    assert s.workers_per_source == 3 and s.log_level == "DEBUG"
    assert s.public_port == 9003 and s.public_host == "x.example.org:443"


def test_env_overrides_file(tmp_path):
    s = load(tmp_path, {"port": 9001, "cache_max_bytes": 512 * MiB}, {"OPENSEEK_PORT": "9100"})
    assert s.port == 9100 and s.cache_max_bytes == 512 * MiB


def test_forbidden_and_unknown_keys_ignored(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="openseek.config")
    d = tmp_path / "evil"
    s = load(tmp_path, {"data_dir": str(d), "capture_source_file": str(d / "x"), "allow_public_clients": True,
                        "allow_http_sources": False, "allowed_ports": "22", "admin_token": "x", "nonsense": 1,
                        "port": 9005})
    base = Settings()
    assert s.data_dir == base.data_dir and s.capture_source_file == "" and s.allow_public_clients is False
    assert s.allow_http_sources is True and s.allowed_ports == base.allowed_ports and s.admin_token == ""
    assert s.port == 9005
    assert "'data_dir'" in caplog.text and "'nonsense'" in caplog.text


@pytest.mark.parametrize("key", ["host", "public_bind"])
@pytest.mark.parametrize("val,ok", [("0.0.0.0", True), ("127.0.0.1", True), ("::1", True), ("10.1.2.3", True),
                                    ("172.16.5.5", True), ("172.31.255.1", True), ("192.168.1.20", True),
                                    ("fd12:3456::1", True), ("8.8.8.8", False), ("172.32.0.1", False),
                                    ("203.0.113.9", False), ("example.com", False), ("localhost", False),
                                    ("", False), ("2001:db8::1", False), (5, False)])
def test_private_bind_rule(tmp_path, key, val, ok):
    s = load(tmp_path, {key: val})
    default = getattr(Settings(), key)
    assert getattr(s, key) == (val if ok else default)


def test_bind_in_env_is_restricted_too(tmp_path):
    with pytest.raises(ConfigError, match="OPENSEEK_HOST"):
        load(tmp_path, {}, {"OPENSEEK_HOST": "8.8.8.8"})


@pytest.mark.parametrize("content", [b"", b"not json", b"[1,2]", b"\xff\xfe\x00garbage", b'"str"', b"null", b"{" * 10])
def test_garbage_file_uses_defaults(tmp_path, content):
    assert load(tmp_path, content, raw=True) == Settings()


def test_missing_file_uses_defaults(tmp_path):
    assert Settings.from_env({"OPENSEEK_CONFIG_FILE": str(tmp_path / "nope.json")}) == Settings()
    assert Settings.from_env({"OPENSEEK_CONFIG_FILE": str(tmp_path)}) == Settings()    # a directory


def test_cron_secret_is_environment_only_and_optional(tmp_path):
    assert Settings().cron_secret == ""
    assert Settings.from_env({"OPENSEEK_CRON_SECRET": "c" * 40}).cron_secret == "c" * 40
    # Like admin tokens, a lesser-privileged config file must not inject this secret.
    assert load(tmp_path, {"cron_secret": "c" * 40}).cron_secret == ""


def test_bom_prefixed_file(tmp_path):
    s = load(tmp_path, b"\xef\xbb\xbf" + json.dumps({"port": 9010}).encode(), raw=True)
    assert s.port == 9010


def test_bad_values_skip_only_that_key(tmp_path):
    s = load(tmp_path, {"port": "abc", "admin_port": 70000, "cache_max_bytes": 1, "bg_mbps": [1], "workers_per_source": 99,
                        "retention_days": None, "upstream_rps": True, "max_active_sources": 4})
    b = Settings()
    assert (s.port, s.admin_port, s.cache_max_bytes, s.bg_mbps, s.workers_per_source) == \
        (b.port, b.admin_port, b.cache_max_bytes, b.bg_mbps, b.workers_per_source)
    assert s.retention_days == b.retention_days and s.upstream_rps == b.upstream_rps
    assert s.max_active_sources == 4


def test_string_values_use_env_converters(tmp_path):
    s = load(tmp_path, {"port": "9020", "bg_mbps": "2.5"})
    assert s.port == 9020 and s.bg_mbps == 2.5


def test_cross_field_conflict_drops_key_not_server(tmp_path):
    s = load(tmp_path, {"public_port": 8787})      # equals the default API port
    assert s.public_port == 0


def test_no_config_file_env_unchanged(tmp_path):
    assert Settings.from_env({}) == Settings()


def test_trusted_tls_certificate_pair(tmp_path):
    cert, key, _pin = ensure_certificate(tmp_path / "tls-source")
    loaded = Settings.from_env({"OPENSEEK_TLS_CERT_FILE": str(cert), "OPENSEEK_TLS_KEY_FILE": str(key)})
    assert loaded.tls_cert_file == cert and loaded.tls_key_file == key
    with pytest.raises(ConfigError):
        Settings.from_env({"OPENSEEK_TLS_CERT_FILE": str(cert)})


def vercel_env(**overrides):
    env = {
        "OPENSEEK_POOL_MODE": "channel",
        "OPENSEEK_TELEGRAM_CHANNEL_ID": "-100123",
        "OPENSEEK_TELEGRAM_API_ID": "123",
        "OPENSEEK_TELEGRAM_API_HASH": "a" * 32,
        "OPENSEEK_TELEGRAM_SESSION": "1abc",
        "OPENSEEK_SUPABASE_URL": "https://project.supabase.co",
        "OPENSEEK_SUPABASE_SERVICE_KEY": "service-role-key",
        "OPENSEEK_ADMIN_TOKEN": "owner-secret-is-long-enough",
        "OPENSEEK_PUBLIC_TLS_PIN": "sha256//" + "A" * 43 + "=",
    }
    env.update(overrides)
    return env


def test_profile_defaults_to_local():
    assert Settings().profile == "local"
    assert Settings.from_env({}).profile == "local"
    assert Settings.from_env({"OPENSEEK_PROFILE": "local"}).profile == "local"


def test_vercel_env_implies_vercel_profile_unless_explicit():
    assert Settings.from_env({**vercel_env(), "VERCEL": "1"}).profile == "vercel"
    assert Settings.from_env({**vercel_env(), "VERCEL": "1",
                              "OPENSEEK_PROFILE": "local"}).profile == "local"


def test_vercel_cron_secret_uses_platform_name_with_open_seek_override():
    assert Settings.from_env({**vercel_env(), "OPENSEEK_PROFILE": "vercel",
                              "CRON_SECRET": "v" * 40}).cron_secret == "v" * 40
    assert Settings.from_env({**vercel_env(), "OPENSEEK_PROFILE": "vercel",
                              "CRON_SECRET": "v" * 40,
                              "OPENSEEK_CRON_SECRET": "o" * 40}).cron_secret == "o" * 40


def test_vercel_profile_requires_channel_pool_mode():
    env = vercel_env()
    env.pop("OPENSEEK_POOL_MODE")
    with pytest.raises(ConfigError, match="vercel"):
        Settings.from_env({**env, "OPENSEEK_PROFILE": "vercel"})


@pytest.mark.parametrize("overrides", [
    {"OPENSEEK_SUPABASE_URL": ""},
    {"OPENSEEK_SUPABASE_SERVICE_KEY": ""},
    {"OPENSEEK_PUBLIC_TLS_PIN": "sha256//not-a-pin"},
    {"OPENSEEK_SUPABASE_URL": "http://project.supabase.co"},
    {"OPENSEEK_SUPABASE_BUCKET": "../bad"},
])
def test_vercel_profile_validates_supabase_settings(overrides):
    with pytest.raises(ConfigError):
        Settings.from_env({**vercel_env(**overrides), "OPENSEEK_PROFILE": "vercel"})


def test_vercel_public_tls_pin_is_optional_but_validated_when_set():
    settings = Settings.from_env({**vercel_env(OPENSEEK_PUBLIC_TLS_PIN=""),
                                  "OPENSEEK_PROFILE": "vercel"})
    assert settings.public_tls_pin == ""


def test_vercel_profile_accepts_complete_settings():
    s = Settings.from_env({**vercel_env(), "OPENSEEK_PROFILE": "vercel"})
    assert s.profile == "vercel" and s.supabase_bucket == "openseek-staging"


def test_local_profile_does_not_require_supabase():
    s = Settings.from_env({"OPENSEEK_PROFILE": "local", "OPENSEEK_SUPABASE_URL": "not-a-url"})
    assert s.profile == "local" and s.supabase_service_key == ""


def test_local_only_defaults_to_loopback(tmp_path):
    s = Settings.from_env({"OPENSEEK_DATA_DIR": str(tmp_path)})
    assert s.host == "127.0.0.1"


def test_channel_node_defaults_to_all_interfaces(tmp_path):
    s = Settings.from_env({"OPENSEEK_DATA_DIR": str(tmp_path), "OPENSEEK_POOL_MODE": "channel",
                           "OPENSEEK_TELEGRAM_API_ID": "123",
                           "OPENSEEK_TELEGRAM_API_HASH": "a" * 32,
                           "OPENSEEK_TELEGRAM_BOT_TOKEN": "123456:test",
                           "OPENSEEK_TELEGRAM_CHANNEL_ID": "-100123"})
    assert s.host == "0.0.0.0"


def test_private_lan_bind_is_allowed(tmp_path):
    s = Settings.from_env({"OPENSEEK_DATA_DIR": str(tmp_path), "OPENSEEK_HOST": "10.1.2.3"})
    assert s.host == "10.1.2.3"


def test_public_bind_address_is_refused_with_a_readable_error(tmp_path):
    with pytest.raises(ConfigError, match="OPENSEEK_HOST"):
        Settings.from_env({"OPENSEEK_DATA_DIR": str(tmp_path), "OPENSEEK_HOST": "203.0.113.10"})

