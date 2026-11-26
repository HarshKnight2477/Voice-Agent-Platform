"""The TelephonyProvider port.

The rest of the platform talks in calls, rooms-as-opaque-names and participants; only the
adapter (providers/telephony/livekit_provider.py) knows LiveKit's objects. Phone-number,
SIP trunk, dispatch-rule and transfer methods are added to this port with Phase 3.3/3.4.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol


@dataclass(frozen=True)
class WebJoin:
    """Everything a browser needs to join a test call."""

    server_url: str
    token: str
    room: str
    expires_at: datetime


class TelephonyProvider(Protocol):
    name: str

    def issue_web_join(
        self,
        *,
        room: str,
        participant_identity: str,
        dispatch_metadata: dict[str, Any],
        ttl: timedelta,
    ) -> WebJoin:
        """A short-lived credential to join `room`; joining starts the agent for that room."""
        ...

    async def check_reachable(self) -> None:
        """Raise if the media server can't be reached with the configured credentials."""
        ...
