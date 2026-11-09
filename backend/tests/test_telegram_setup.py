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


class StrictInviteClient(FakeClient):
    """A double that models a fresh StringSession.

    A session opened this run has no entity cache and no access hashes, so the
    only referent `get_entity` can resolve is the validated invite link.
    Telethon treats numeric strings as phone numbers there, which is exactly
    the failure this class guards against.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entity_lookups = []
        self.iter_entities = []
        self.permission_lookups = []

    async def get_entity(self, invite):
        self.entity_lookups.append(invite)
        text = str(invite)
        if isinstance(invite, int) or text.lstrip("-").isdigit():
            raise AssertionError(
                f"a fresh session cannot resolve numeric referent {text!r}; "
                "resolve the validated invite link instead")
        assert text.startswith("https://t.me/+"), text
        return self.entity

    async def iter_messages(self, entity, limit):
        self.iter_entities.append(entity)
        async for message in super().iter_messages(entity, limit):
            yield message

    async def get_permissions(self, entity, user):
        self.permission_lookups.append(entity)
        return await super().get_permissions(entity, user)


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
    """The already-joined fallback resolves the pool from the original
    validated invite link: a fresh session cannot resolve a numeric peer, and
    an account that already joined is never asked for one."""
    class UserAlreadyParticipantError(Exception):
        pass

    client = StrictInviteClient(Entity(), join_error=UserAlreadyParticipantError())
    input_calls = []

    def record_input(prompt):
        input_calls.append(prompt)
        return ""  # any numeric answer would be wrong by design; keep it empty

    setup = TelegramAccountSetup(client_factory=lambda *_args, **_kwargs: client,
                                 session_factory=lambda: object(),
                                 input_fn=record_input,
                                 peer_id_fn=lambda item: -1000000000000 - item.id,
                                 invite_request_factory=lambda invite_hash: SimpleNamespace(hash=invite_hash))

    result = setup.join(1234, "api-hash", "https://t.me/+invite-secret")

    assert input_calls == [], input_calls
    assert client.entity_lookups == ["https://t.me/+invite-secret"], client.entity_lookups
    assert client.joined_hash is None  # the join clearly failed, nothing was re-imported
    assert result.channel_id == "-1000000123456"
    assert result.session == "private-string-session"
    assert client.disconnects == 1


@pytest.mark.parametrize(
    "client_kwargs, reason, screen",
    [pytest.param({"history": False}, "shared_pool_history_unavailable", "history",
                  id="hidden-history"),
     pytest.param({"permissions": SimpleNamespace(send_messages=True, send_media=False)},
                  "shared_pool_send_media_denied", "permissions", id="no-media"),
     pytest.param({"broadcast": True}, "shared_pool_must_be_supergroup", "type",
                  id="broadcast-channel")])
def test_telegram_setup_reused_member_is_still_screened(client_kwargs, reason, screen):
    """A reused member resolved from the invite goes through the same real
    access checks as a fresh join: history, send-media permissions (where the
    chain reaches them) and the supergroup type — never a bypass."""
    class UserAlreadyParticipantError(Exception):
        pass

    entity = Entity()
    if client_kwargs.pop("broadcast", False):
        entity.broadcast, entity.megagroup = True, False
    client = StrictInviteClient(entity, join_error=UserAlreadyParticipantError(),
                                **client_kwargs)

    def never_prompted(prompt):
        raise AssertionError(f"already-joined must not prompt: {prompt!r}")

    setup = TelegramAccountSetup(client_factory=lambda *_args, **_kwargs: client,
                                 session_factory=lambda: object(),
                                 input_fn=never_prompted,
                                 peer_id_fn=lambda item: item.id,
                                 invite_request_factory=lambda invite_hash: SimpleNamespace(hash=invite_hash))

    with pytest.raises(TelegramSetupError, match=reason):
        setup.join(1234, "api-hash", "https://t.me/+invite-secret")

    assert client.entity_lookups == ["https://t.me/+invite-secret"], client.entity_lookups
    assert client.disconnects == 1
    if screen == "history":
        assert client.iter_entities == [entity], client.iter_entities
        assert client.permission_lookups == [], client.permission_lookups
    elif screen == "permissions":
        assert client.iter_entities == [entity], client.iter_entities
        assert client.permission_lookups == [entity], client.permission_lookups
    else:  # the type screen short-circuits before any history read
        assert client.iter_entities == [], client.iter_entities
        assert client.permission_lookups == [], client.permission_lookups
