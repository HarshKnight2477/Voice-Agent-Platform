"""LiveKit implementation of the TelephonyProvider port.

APIs used (checked against docs.livekit.io on 2026-11-26, livekit-api 2.0.1):
- AccessToken().with_identity().with_grants(VideoGrants(...)).with_room_config(
      RoomConfiguration(agents=[RoomAgentDispatch(agent_name=..., metadata=...)])).with_ttl()
  Dispatch through the token happens only when the room is first created, so every test
  call gets a fresh room name.
- LiveKitAPI(url, key, secret).room.list_rooms(ListRoomsRequest()) for a reachability check.
Dispatch metadata carries ids only (never config or secrets); it is signed inside the token,
so only this server can choose which agent version a room runs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from livekit import api

from voice_platform.shared.config import Settings
from voice_platform.telephony.ports import WebJoin

MAX_METADATA_BYTES = 4096  # LiveKit allows far more; we only ever send ids


def http_url(ws_url: str) -> str:
    return ws_url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)


class LiveKitProvider:
    name = "livekit"

    def __init__(self, settings: Settings) -> None:
        self._url = settings.livekit_url
        self._key = settings.livekit_api_key
        self._secret = settings.livekit_api_secret.get_secret_value()
        self._agent_name = settings.livekit_agent_name

    def issue_web_join(
        self,
        *,
        room: str,
        participant_identity: str,
        dispatch_metadata: dict[str, Any],
        ttl: timedelta,
    ) -> WebJoin:
        metadata = json.dumps(dispatch_metadata, separators=(",", ":"))
        if len(metadata.encode()) > MAX_METADATA_BYTES:
            raise ValueError("dispatch metadata too large; pass ids only")
        token = (
            api.AccessToken(self._key, self._secret)
            .with_identity(participant_identity)
            .with_ttl(ttl)
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=True,
                )
            )
            .with_room_config(
                api.RoomConfiguration(
                    agents=[api.RoomAgentDispatch(agent_name=self._agent_name, metadata=metadata)]
                )
            )
            .to_jwt()
        )
        return WebJoin(
            server_url=self._url, token=token, room=room, expires_at=datetime.now(UTC) + ttl
        )

    async def check_reachable(self) -> None:
        async with api.LiveKitAPI(http_url(self._url), self._key, self._secret) as lk:
            await lk.room.list_rooms(api.ListRoomsRequest())
