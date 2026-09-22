"""Provider-independent telephony contracts for regional PSTN routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DialRequest:
    """Normalized request passed to a telephony provider."""

    to_number: str
    from_number: str
    country: str
    call_id: str
    instructions: str = ""
    sip_trunk_id: str | None = None


@dataclass(frozen=True)
class DialResult:
    """Provider response after a dial request is accepted."""

    provider_call_id: str
    status: str


class TelephonyProvider(Protocol):
    """Minimal provider contract used by call-use call-control layers."""

    async def dial(self, request: DialRequest) -> DialResult:
        """Originate a PSTN call using a provider-owned/verified caller ID."""
        ...

    async def hangup(self, provider_call_id: str) -> None:
        """Terminate an active provider call."""
        ...

    async def get_status(self, provider_call_id: str) -> str:
        """Return the provider current call status."""
        ...
