"""One-time Telegram user-session authorization used by the participant wizard."""
from __future__ import annotations

import asyncio
import getpass
from dataclasses import dataclass
from typing import Callable

from .telegram_mt import _invite_hash


class TelegramSetupError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class TelegramSetupResult:
    session: str
    channel_id: str


class TelegramAccountSetup:
    """Authenticate an account in an in-memory StringSession and check its pool access."""

    def __init__(self, *, client_factory=None, session_factory=None,
                 input_fn: Callable[[str], str] = input,
                 password_fn: Callable[[str], str] = getpass.getpass,
                 peer_id_fn=None, invite_request_factory=None):
        self.client_factory = client_factory
        self.session_factory = session_factory
        self.input = input_fn
        self.password = password_fn
        self.peer_id_fn = peer_id_fn
        self.invite_request_factory = invite_request_factory

    def join(self, api_id: int, api_hash: str, invite: str) -> TelegramSetupResult:
        try:
            return asyncio.run(self._join(api_id, api_hash, invite))
        except TelegramSetupError:
            raise
        except Exception as exc:
            name = type(exc).__name__
            if name == "FloodWaitError":
                raise TelegramSetupError("flood_wait") from None
            if name in ("InviteRequestSentError", "InviteRequestAlreadySentError"):
                raise TelegramSetupError("join_request_required") from None
            if name in ("ChannelPrivateError", "UserNotParticipantError", "ChatAdminRequiredError"):
                raise TelegramSetupError("group_access_denied") from None
            if name in ("PhoneCodeInvalidError", "PhoneNumberInvalidError", "SessionPasswordNeededError"):
                raise TelegramSetupError("telegram_login_invalid") from None
            raise TelegramSetupError("telegram_setup_failed") from None

    async def _join(self, api_id: int, api_hash: str, invite: str) -> TelegramSetupResult:
        if not isinstance(api_id, int) or api_id <= 0 or not api_hash:
            raise TelegramSetupError("telegram_api_credentials_required")
        if (self.client_factory is None or self.session_factory is None or
                self.invite_request_factory is None or (self.peer_id_fn is None)):
            try:
                from telethon import TelegramClient, utils
                from telethon.sessions import StringSession
                from telethon.tl.functions.messages import ImportChatInviteRequest
            except ImportError:
                raise TelegramSetupError("telethon_unavailable") from None
        else:
            TelegramClient = StringSession = ImportChatInviteRequest = utils = None

        session = self.session_factory() if self.session_factory else StringSession()
        factory = self.client_factory or TelegramClient
        client = factory(session, api_id, api_hash, flood_sleep_threshold=0)
        connected = False
        try:
            await client.connect()
            connected = True
            if not await client.is_user_authorized():
                phone = self.input("Telegram phone number: ").strip()
                if not phone:
                    raise TelegramSetupError("phone_number_required")
                sent = await client.send_code_request(phone)
                code = self.input("Telegram login code: ").strip()
                try:
                    await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
                except Exception as exc:
                    if type(exc).__name__ != "SessionPasswordNeededError":
                        raise
                    await client.sign_in(password=self.password("Telegram 2-step password: "))

            invite_hash = _invite_hash(invite)
            try:
                request_factory = self.invite_request_factory or ImportChatInviteRequest
                updates = await client(request_factory(invite_hash))
                entity = (getattr(updates, "chats", None) or [None])[0]
            except Exception as exc:
                if type(exc).__name__ != "UserAlreadyParticipantError":
                    raise
                group_id = self.input("You already joined this pool. Enter its numeric Telegram group ID: ").strip()
                if not group_id:
                    raise TelegramSetupError("group_id_required")
                entity = await client.get_entity(group_id)
            if entity is None:
                raise TelegramSetupError("group_access_denied")
            if entity is None or bool(getattr(entity, "broadcast", False)) or not bool(getattr(entity, "megagroup", False)):
                raise TelegramSetupError("shared_pool_must_be_supergroup")

            # Iterating one message verifies that the account can read the joined
            # supergroup's history. The message itself is never copied or logged.
            got_history = False
            async for _message in client.iter_messages(entity, limit=1):
                got_history = True
            if not got_history:
                raise TelegramSetupError("shared_pool_history_unavailable")
            permissions = await client.get_permissions(entity, "me")
            if (getattr(permissions, "send_messages", None) is False or
                    getattr(permissions, "send_media", None) is False):
                raise TelegramSetupError("shared_pool_send_media_denied")

            saved = client.session.save()
            if not saved:
                raise TelegramSetupError("telegram_session_unavailable")
            peer_id = self.peer_id_fn or utils.get_peer_id
            return TelegramSetupResult(session=saved, channel_id=str(peer_id(entity)))
        finally:
            if connected:
                await client.disconnect()
