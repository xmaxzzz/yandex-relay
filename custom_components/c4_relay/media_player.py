"""One media_player per Control4 room; AlexxIT streams a station's music to it."""

from __future__ import annotations

import asyncio
import logging
import time
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

# AlexxIT reports a track change as pause + play_media about a second apart
# (the station says playing=false for a moment). Switching the room off for
# that reset its volume to the Control4 turn-on level on every track (site
# 2026-10-01: 25 % after "громче", 20 % on the next track). So a pause that is
# not a person's is applied only if no new track came and the station still
# does not play after this long.
PAUSE_SETTLE = 2.5  # s

# A room's direct output gets the track this long after Control4 started the
# room: Control4 switches the receiver to the input of its own audio path, and
# the network input has to win afterwards (an AV receiver turns to its network
# input when that starts playing).
OUTPUT_DELAY = 1.5  # s

# Control4 may switch the receiver back to its own input later than that: a
# room added to the session by programming re-selects the path (site
# 2026-11-26: Беседка switched on, Denon took HEOS at +5 s, adding Кухня put it
# back on DVD at +7 s and HEOS stopped). So the output is checked at these
# times after the track was sent and gets the track again if it is not playing.
OUTPUT_CHECKS = (10.0, 20.0, 30.0)  # s after the track was sent
OUTPUT_RETRIES = 2
OUTPUT_OK_STATES = {"playing", "buffering"}

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
        self._held_play: dict[str, Any] | None = None   # play_media ignored while paused
        self._last_play = 0.0                           # monotonic time of the last /play
        self._output_url: str | None = None             # last track sent to the direct output
        self._output_watch: asyncio.Task | None = None  # re-sends the track if the output stops

    async def async_added_to_hass(self) -> None:
        try:
            self._apply(await self._hub.client.state(self.room_id))
        except RelayError as err:
            _LOGGER.debug("initial state for room %s: %s", self.room_id, err)

    async def async_will_remove_from_hass(self) -> None:
        self._stop_output_watch()

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
        # AlexxIT re-sends the current track whenever the station's source is
        # selected again (its reload, our self-healing), even while the station
        # is paused: that switched the room on by itself (site 2026-09-29). Keep
        # it for a later "play" instead.
        station = self._hub.station_for_room(self.room_id)
        if station and self._hub.station_playing(station) is False:
            _LOGGER.info("Room %s: %s is paused, its track is not started in Control4", self.room_id, station)
            self._held_play = payload
            return
        self._held_play = None
        self._last_play = time.monotonic()
        self._attr_media_image_url = meta.get("image") or None
        self._attr_media_duration = (meta.get("duration_ms") or 0) / 1000 or None
        try:
            await self._call(self._hub.client.play(payload))
        finally:
            await self._output_play(payload)

    async def async_media_play(self) -> None:
        # The station resumed. After a pause the room was switched off, so the
        # driver restarts the track; seek the station back to 0 to match.
        station = self._hub.station_for_room(self.room_id)
        if self._held_play is not None:           # the track AlexxIT sent while paused
            payload, self._held_play = self._held_play, None
            try:
                await self._call(self._hub.client.play(payload))
            finally:
                await self._output_play(payload)
            if station:
                await self._hub.station_call(station, "media_seek", {"seek_position": 0})
            return
        resp = await self._call(self._hub.client.resume(self.room_id))
        if resp.get("resume") == "restart":
            if self._output_url:
                await self._output_play({"url": self._output_url})
            if station:
                await self._hub.station_call(station, "media_seek", {"seek_position": 0})
        else:
            await self._output_call("media_play")

    async def _track_change(self) -> bool:
        """True when a pause from AlexxIT was only the gap between two tracks."""
        if self._context is not None and self._context.user_id:
            return False                        # a person pressed pause/stop
        asked = time.monotonic()
        await asyncio.sleep(PAUSE_SETTLE)
        station = self._hub.station_for_room(self.room_id)
        if self._last_play >= asked or (station and self._hub.station_playing(station)):
            _LOGGER.debug("Room %s: pause was a track change, room kept on", self.room_id)
            return True
        return False

    async def async_media_pause(self) -> None:
        if await self._track_change():
            return
        await self._output_call("media_pause")
        await self._call(self._hub.client.pause(self.room_id))

    async def async_media_stop(self) -> None:
        if await self._track_change():
            return
        await self._output_call("media_stop")
        await self._call(self._hub.client.stop(self.room_id))

    # --- direct output -----------------------------------------------------
    # A room can have a HA media_player as its direct output (options): the same
    # track goes there too, so the room sounds even when its Control4 audio path
    # does not reach the speakers (e.g. an AV receiver played over its network
    # input). The Control4 session (panel, now playing, volume) stays as is.
    async def _output_call(self, service: str, data: dict[str, Any] | None = None) -> None:
        output = self._hub.outputs.get(self.room_id)
        if not output:
            return
        if service != "play_media":
            self._stop_output_watch()               # pause/stop: the output may stop now
        try:
            await self.hass.services.async_call(
                "media_player", service, {"entity_id": output, **(data or {})}, blocking=False,
            )
        except Exception as err:  # noqa: BLE001 - the Control4 room goes on regardless
            _LOGGER.warning("Room %s: %s on %s failed: %s", self.room_id, service, output, err)

    async def _output_play(self, payload: dict[str, Any]) -> None:
        url = payload.get("url") or payload.get("fallback_url")
        if not url or not self._hub.outputs.get(self.room_id):
            return
        self._output_url = url
        self._stop_output_watch()
        await asyncio.sleep(OUTPUT_DELAY)
        await self._output_call("play_media", {"media_content_id": url, "media_content_type": "music"})
        self._output_watch = self.hass.async_create_background_task(
            self._watch_output(url), f"c4_relay output watch room {self.room_id}")

    def _stop_output_watch(self) -> None:
        if self._output_watch is not None and not self._output_watch.done():
            if self._output_watch is not asyncio.current_task():
                self._output_watch.cancel()
        self._output_watch = None

    async def _watch_output(self, url: str) -> None:
        """Send the track again while the output is not playing it (see OUTPUT_CHECKS)."""
        output = self._hub.outputs.get(self.room_id)
        retries = 0
        waited = 0.0
        for at in OUTPUT_CHECKS:
            await asyncio.sleep(at - waited)
            waited = at
            if self._output_url != url or self._attr_state != MediaPlayerState.PLAYING or not output:
                return                              # another track, or the room stopped
            st = self.hass.states.get(output)
            if st is None or st.state in OUTPUT_OK_STATES:
                continue
            if retries >= OUTPUT_RETRIES:
                _LOGGER.warning("Room %s: %s still %s, the track is not sent again", self.room_id, output, st.state)
                return
            retries += 1
            _LOGGER.info("Room %s: %s is %s %.0f s after the track, sending it again", self.room_id,
                         output, st.state, at)
            await self._output_call("play_media", {"media_content_id": url, "media_content_type": "music"})

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
