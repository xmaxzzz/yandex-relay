"""One media_player per Control4 room; AlexxIT streams a station's music to it."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RelayHub
from .api import RelayError
from .const import DOMAIN
from .helpers import RELAY_TO_HA_STATE, direct_url_from_proxy

_LOGGER = logging.getLogger(__name__)

HA_STATES = {
    "playing": MediaPlayerState.PLAYING,
    "paused": MediaPlayerState.PAUSED,
    "idle": MediaPlayerState.IDLE,
}


def hub_device(hub: RelayHub) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, hub.entry.entry_id)},
        name="Control4 Yandex Relay",
        manufacturer="Yandex Relay",
        model="Control4 driver",
        sw_version=hub.driver_version,
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    hub: RelayHub = entry.runtime_data
    players = []
    for room_id in sorted(set(hub.bindings.values())):
        if room_id in hub.rooms:
            player = RelayRoomPlayer(hub, room_id, hub.rooms[room_id])
            hub.players[room_id] = player
            players.append(player)
        else:
            _LOGGER.warning("Room %s is bound but the driver does not report it", room_id)
    async_add_entities(players)


class RelayRoomPlayer(MediaPlayerEntity):
    """A Control4 room as a streaming target for a Yandex Station."""

    _attr_should_poll = False
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
        | MediaPlayerEntityFeature.VOLUME_SET
    )

    def __init__(self, hub: RelayHub, room_id: int, room_name: str) -> None:
        self._hub = hub
        self.room_id = room_id
        # AlexxIT lists targets by entity name, so this exact string becomes the
        # station's source; kept stable and without a device-name prefix.
        self._attr_name = f"Control4 {room_name}"
        self._attr_unique_id = f"{hub.entry.entry_id}_{room_id}"
        self._attr_device_info = hub_device(hub)
        self._attr_state = MediaPlayerState.IDLE
        self._attr_volume_level = None
        self._station_volume_seen = False

    async def async_added_to_hass(self) -> None:
        try:
            self._apply(await self._hub.client.state(self.room_id))
        except RelayError as err:
            _LOGGER.debug("initial state for room %s: %s", self.room_id, err)

    # --- state from the driver ------------------------------------------
    def set_relay_state(self, relay_state: str | None) -> None:
        self._attr_state = HA_STATES[RELAY_TO_HA_STATE.get(relay_state or "idle", "idle")]
        if self.hass is not None:
            self.async_write_ha_state()

    def set_room_volume(self, level: Any) -> None:
        """The room's real Control4 volume (0..100) reported by the driver."""
        if isinstance(level, (int, float)):
            self._attr_volume_level = max(0.0, min(1.0, level / 100))
            if self.hass is not None:
                self.async_write_ha_state()

    def _apply(self, resp: dict[str, Any]) -> None:
        if isinstance(resp.get("volume"), (int, float)):
            self._attr_volume_level = max(0.0, min(1.0, resp["volume"] / 100))
        if resp.get("title") is not None:
            self._attr_media_title = resp.get("title")
            self._attr_media_artist = resp.get("artist")
        self.set_relay_state(resp.get("state"))

    # --- station metadata ------------------------------------------------
    def _station_metadata(self) -> dict[str, Any]:
        station = self._hub.station_for_room(self.room_id)
        st = self.hass.states.get(station) if station else None
        if st is None:
            return {}
        a = st.attributes
        image = None
        try:  # the station entity has the raw cover URL; state only has the HA proxy
            ent = self.hass.data["entity_components"]["media_player"].get_entity(station)
            image = getattr(ent, "media_image_url", None)
        except (KeyError, AttributeError):
            pass
        duration = a.get("media_duration")
        return {
            "title": a.get("media_title") or "",
            "artist": a.get("media_artist") or "",
            "album": a.get("media_album_name") or "",
            "image": image or "",
            "duration_ms": int(float(duration) * 1000) if duration else 0,
            "key": str(a.get("media_content_id") or ""),
        }

    # --- commands --------------------------------------------------------
    async def _call(self, coro) -> dict[str, Any]:
        try:
            resp = await coro
        except RelayError as err:
            raise HomeAssistantError(f"Control4 Yandex Relay: {err}") from err
        self._apply(resp)
        return resp

    async def async_play_media(self, media_type: str, media_id: str, **kwargs: Any) -> None:
        meta = self._station_metadata()
        payload = {
            "room_id": self.room_id,
            "url": direct_url_from_proxy(media_id) or "",
            "fallback_url": media_id,
            **meta,
        }
        if not payload.get("key"):
            payload["key"] = media_id.rsplit("/", 1)[-1][:40]
        self._attr_media_image_url = meta.get("image") or None
        self._attr_media_duration = (meta.get("duration_ms") or 0) / 1000 or None
        await self._call(self._hub.client.play(payload))

    async def async_media_play(self) -> None:
        # The station resumed. After a pause the room was switched off, so the
        # driver restarts the track; seek the station back to 0 to match.
        resp = await self._call(self._hub.client.resume(self.room_id))
        station = self._hub.station_for_room(self.room_id)
        if resp.get("resume") == "restart" and station:
            await self._hub.station_call(station, "media_seek", {"seek_position": 0})

    async def async_media_pause(self) -> None:
        await self._call(self._hub.client.pause(self.room_id))

    async def async_media_stop(self) -> None:
        await self._call(self._hub.client.stop(self.room_id))

    async def async_media_next_track(self) -> None:
        if station := self._hub.station_for_room(self.room_id):
            await self._hub.station_call(station, "media_next_track")

    async def async_media_previous_track(self) -> None:
        if station := self._hub.station_for_room(self.room_id):
            await self._hub.station_call(station, "media_previous_track")

    async def async_set_volume_level(self, volume: float) -> None:
        """Two callers: a person (slider, service call) and AlexxIT's sync.

        A person's call (context with a user) sets the room absolutely within
        its min..max. AlexxIT syncs the station volume here too; while our Glagol
        hook is in place voice commands are taken from Alice's directives
        instead, because the station volume is garbage while the station is
        muted. Without the hook the old guessing path (/station_volume) is used.
        """
        station = self._hub.station_for_room(self.room_id)
        try:
            if self._context is not None and self._context.user_id:
                resp = await self._hub.client.volume_level(self.room_id, volume)
            elif self._hub.hooked(station):
                _LOGGER.debug("room %s: station volume sync %.2f ignored (directives in use)",
                              self.room_id, volume)
                return
            else:
                initial = not self._station_volume_seen
                self._station_volume_seen = True
                resp = await self._hub.client.station_volume(self.room_id, volume, initial)
        except RelayError as err:
            raise HomeAssistantError(f"Control4 Yandex Relay: {err}") from err
        if isinstance(resp.get("volume"), (int, float)):
            self.set_room_volume(resp["volume"])
