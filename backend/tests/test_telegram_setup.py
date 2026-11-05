from __future__ import annotations

from types import SimpleNamespace

import pytest

from openseek.telegram_setup import TelegramAccountSetup, TelegramSetupError


class Session:
    def save(self):
        return "private-string-session"


class Entity:
    id = 123456
    megagroup = True
    broadcast = False


class FakeClient:
    def __init__(self, entity, *, history=True, permissions=None, join_error=None):
        self.session = Session()
        self.entity = entity
        self.history = history
        self.permissions = permissions or SimpleNamespace(send_messages=True, send_media=True)
        self.join_error = join_error
        self.connected = False
        self.disconnects = 0
        self.joined_hash = None
        self.entity_lookup = None

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnects += 1
        self.connected = False

    async def is_user_authorized(self):
        return True

    async def __call__(self, request):
        if self.join_error:
            raise self.join_error
        self.joined_hash = request.hash
        return SimpleNamespace(chats=[self.entity])

    async def get_entity(self, _invite):
        self.entity_lookup = _invite
        return self.entity

    async def get_permissions(self, _entity, _user):
        return self.permissions

    async def iter_messages(self, _entity, limit):
        assert limit == 1
        if self.history:
            yield SimpleNamespace(id=1)


@pytest.fixture
def telethon_client(monkeypatch):
    entity = Entity()
    client = FakeClient(entity)
    setup = TelegramAccountSetup(client_factory=lambda *_args, **_kwargs: client,
                                 session_factory=lambda: object(),
                                 peer_id_fn=lambda item: -1000000000000 - item.id,
                                 invite_request_factory=lambda invite_hash: SimpleNamespace(hash=invite_hash))
    return setup, client


def test_telegram_setup_joins_invite_and_checks_read_send_media_permissions(telethon_client):
    setup, client = telethon_client

    result = setup.join(1234, "api-hash", "https://t.me/+invite-secret")

    assert result.channel_id == "-1000000123456"
    assert result.session == "private-string-session"
    assert client.joined_hash == "invite-secret"
    assert client.connected is False and client.disconnects == 1


def test_telegram_setup_rejects_hidden_or_empty_history():
    client = FakeClient(Entity(), history=False)
    setup = TelegramAccountSetup(client_factory=lambda *_args, **_kwargs: client,
                                 session_factory=lambda: object(),
                                 peer_id_fn=lambda item: item.id,
                                 invite_request_factory=lambda invite_hash: SimpleNamespace(hash=invite_hash))
    with pytest.raises(TelegramSetupError, match="shared_pool_history_unavailable"):
        setup.join(1234, "api-hash", "https://t.me/+invite-secret")
    assert client.disconnects == 1


def test_telegram_setup_rejects_group_without_send_media_permission():
    client = FakeClient(Entity(), permissions=SimpleNamespace(send_messages=True, send_media=False))
    setup = TelegramAccountSetup(client_factory=lambda *_args, **_kwargs: client,
                                 session_factory=lambda: object(),
                                 peer_id_fn=lambda item: item.id,
                                 invite_request_factory=lambda invite_hash: SimpleNamespace(hash=invite_hash))
    with pytest.raises(TelegramSetupError, match="shared_pool_send_media_denied"):
        setup.join(1234, "api-hash", "https://t.me/+invite-secret")


def test_telegram_setup_explains_join_approval_is_not_supported():
    class InviteRequestSentError(Exception):
        pass

    client = FakeClient(Entity(), join_error=InviteRequestSentError())
    setup = TelegramAccountSetup(client_factory=lambda *_args, **_kwargs: client,
                                 session_factory=lambda: object(), peer_id_fn=lambda item: item.id,
                                 invite_request_factory=lambda invite_hash: SimpleNamespace(hash=invite_hash))
    with pytest.raises(TelegramSetupError, match="join_request_required"):
        setup.join(1234, "api-hash", "https://t.me/+invite-secret")


def test_telegram_setup_recovers_when_the_account_already_joined():
    class UserAlreadyParticipantError(Exception):
        pass

    client = FakeClient(Entity(), join_error=UserAlreadyParticipantError())
    setup = TelegramAccountSetup(client_factory=lambda *_args, **_kwargs: client,
                                 session_factory=lambda: object(),
                                 input_fn=lambda _prompt: "-1000000000123",
                                 peer_id_fn=lambda item: -1000000000000 - item.id,
                                 invite_request_factory=lambda invite_hash: SimpleNamespace(hash=invite_hash))
    result = setup.join(1234, "api-hash", "https://t.me/+invite-secret")
    assert client.entity_lookup == "-1000000000123"
    assert result.channel_id == "-1000000123456"
