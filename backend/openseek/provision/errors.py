"""Typed provider failures with plain-language messages, shared by the provision clients.

`ProviderError.plain` is the only exception text the wizard ever prints. It is
written by hand in this package, kept free of tokens, response bodies and URLs, and
always says what the participant can do next. No code path asks a participant to
upgrade a paid plan: provider quota limits are surfaced as waits or alternative
free-tier actions (zero-spend rule).
"""
from __future__ import annotations

import httpx

AUTH = "auth"
QUOTA = "quota"
CONFLICT = "conflict"
NOT_FOUND = "not_found"
PAUSED = "paused"
TIMEOUT = "timeout"
SETUP_REQUIRED = "setup_required"
DEPLOYMENT = "deployment"
PROVIDER = "provider"

# The plain language per free-allowance block; other statuses get an honest generic text.
_STATUS_CODE = {401: AUTH, 402: QUOTA, 403: AUTH, 404: NOT_FOUND, 409: CONFLICT,
                429: QUOTA, 503: PROVIDER}


class ProviderError(Exception):
    """A provider request failed in a way the participant can act on.

    `code` is one of the module constants and drives the wizard's summary status
    (quota/pause/auth blocks read differently from transient pauses). `plain` is the
    only text ever shown to a participant; it never embeds a token or a response body.
    """

    def __init__(self, code: str, status: int, plain: str):
        self.code = code
        self.status = int(status)
        self.plain = plain
        super().__init__(plain)


def code_for_status(status: int) -> str:
    """Map an HTTP status to a ProviderError code (everything unmapped stays `provider`)."""
    return _STATUS_CODE.get(int(status), PROVIDER)


def transport_error(provider: str) -> ProviderError:
    """Wrap a network failure as a transient (status 0) provider error."""
    return ProviderError(PROVIDER, 0,
                         f"Could not reach {provider} just now. Check the network and "
                         "run the wizard again to resume.")
