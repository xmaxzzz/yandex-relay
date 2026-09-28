"""Control4 Yandex Relay: Yandex Station music played through Control4 rooms.

AlexxIT YandexStation streams a station's music to a media_player chosen as the
station's source. This integration provides one such media_player per Control4
room, forwards play_media to the Yandex Relay driver on the controller, and
turns the driver's webhook events (panel buttons, room state) into station
commands. Design and protocol: docs/DESIGN.md in the yandex-relay repo.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .api import RelayAuthError, RelayClient, RelayError
from .const import (
    CONF_BINDINGS,
    CONF_HA_URL,
    CONF_PAIRING_CODE,
    CONF_WEBHOOK_ID,
    DOMAIN,
    SOURCE_STATION,
    YANDEX_DOMAIN,
)
from .helpers import room_stop_pauses_station, station_calls_for_transport

if TYPE_CHECKING:
    from .media_player import RelayRoomPlayer
    from .switch import RelayEnabledSwitch

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.MEDIA_PLAYER, Platform.SWITCH]
SOURCE_CHECK_DELAY = 10  # s after HA start before stations are pointed at their rooms


class RelayHub:
    """Everything one config entry (one Relay driver) needs at runtime."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: RelayClient,
                 info: dict, rooms: list[dict]) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.driver_version: str = info.get("version", "")
        self.rooms: dict[int, str] = {int(r["id"]): r["name"] for r in rooms}
        self.bindings: dict[str, int] = {
            station: int(room) for station, room in entry.options.get(CONF_BINDINGS, {}).items()
        }
        self.players: dict[int, RelayRoomPlayer] = {}
        self.switches: dict[str, RelayEnabledSwitch] = {}
        self._yandex_reloaded = False

    # --- bindings ---------------------------------------------------------
    def station_for_room(self, room_id: int | None) -> str | None:
        for station, room in self.bindings.items():
            if room == room_id:
                return station
        return None

    def enabled(self, station: str) -> bool:
        sw = self.switches.get(station)
        return sw.is_on if sw is not None else True

    # --- station control ---------------------------------------------------
    async def station_call(self, station: str, service: str, data: dict | None = None) -> None:
        await self.hass.services.async_call(
            "media_player", service, {"entity_id": station, **(data or {})}, blocking=False
        )

    async def ensure_sources(self) -> None:
        """Point every enabled, bound station at its room player.

        AlexxIT forgets the selected source on restart, and it collects the
        list of possible targets only once per station connection; if our
        players are missing from that list, reload AlexxIT once so it sees them.
        """
        missing = False
        for station, room in self.bindings.items():
            player = self.players.get(room)
            st = self.hass.states.get(station)
            if player is None or st is None or st.state == "unavailable":
                continue
            target = player.name if self.enabled(station) else SOURCE_STATION
            if target != SOURCE_STATION and target not in (st.attributes.get("source_list") or []):
                missing = True
                continue
            if st.attributes.get("source") != target:
                _LOGGER.debug("%s: source %s -> %s", station, st.attributes.get("source"), target)
                await self.station_call(station, "select_source", {"source": target})
        if missing and not self._yandex_reloaded:
            self._yandex_reloaded = True
            _LOGGER.info("Stations do not list the Control4 players yet, reloading %s", YANDEX_DOMAIN)
            for yentry in self.hass.config_entries.async_entries(YANDEX_DOMAIN):
                await self.hass.config_entries.async_reload(yentry.entry_id)

    async def alice_listening(self, station: str, active: bool) -> None:
        """Duck the station's room while Alice listens/answers (driver decides how)."""
        room = self.bindings.get(station)
        if room is None or not self.enabled(station):
            return
        try:
            resp = await self.client.duck(room, active)
            _LOGGER.debug("duck room %s active=%s: %s", room, active, resp.get("result"))
        except RelayError as err:
            _LOGGER.warning("duck room %s failed: %s", room, err)

    # --- driver events -----------------------------------------------------
    async def handle_event(self, evt: dict[str, Any]) -> None:
        event = evt.get("event")
        room = evt.get("room_id")
        room = int(room) if isinstance(room, (int, float)) else None
        _LOGGER.debug("driver event %s", evt)

        if event == "state" and room in self.players:
            self.players[room].set_relay_state(evt.get("state"))
        elif event == "volume" and room in self.players:
            self.players[room].set_room_volume(evt.get("level"))
            return

        station = self.station_for_room(room)
        if station is None or not self.enabled(station):
            return

        if event == "transport":
            for service, data in station_calls_for_transport(evt.get("action", ""), evt.get("resume")):
                await self.station_call(station, service, data)
        elif room_stop_pauses_station(event, evt.get("state")):
            st = self.hass.states.get(station)
            if st is not None and st.state == "playing":
                await self.station_call(station, "media_pause")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = RelayClient(async_get_clientsession(hass), entry.data[CONF_HOST],
                         entry.data[CONF_PORT], entry.data[CONF_PAIRING_CODE])
    try:
        info = await client.info()
        rooms = await client.rooms()
    except RelayAuthError as err:
        raise ConfigEntryAuthFailed("the driver rejected the pairing code") from err
    except RelayError as err:
        raise ConfigEntryNotReady(str(err)) from err

    hub = RelayHub(hass, entry, client, info, rooms)
    entry.runtime_data = hub

    webhook_id = entry.data[CONF_WEBHOOK_ID]

    async def _webhook(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        try:
            evt = await request.json()
        except ValueError:
            return web.Response(status=400)
        if isinstance(evt, dict):
            await hub.handle_event(evt)
        return web.Response(status=200)

    webhook.async_register(hass, DOMAIN, "Control4 Yandex Relay", webhook_id, _webhook,
                           local_only=True, allowed_methods=["POST"])
    entry.async_on_unload(lambda: webhook.async_unregister(hass, webhook_id))

    webhook_url = entry.data[CONF_HA_URL].rstrip("/") + webhook.async_generate_path(webhook_id)
    try:
        await client.pair(webhook_url)
    except RelayError as err:
        raise ConfigEntryNotReady(f"pairing failed: {err}") from err

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    @callback
    def _station_changed(event: Event) -> None:
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if new is None:
            return
        # Alice listening/answering: AlexxIT unmutes the station for that
        # time on the same attribute, so the room ducks at the same moment.
        was = old.attributes.get("alice_state") if old else None
        now = new.attributes.get("alice_state")
        if now != was and now is not None:
            hass.async_create_task(hub.alice_listening(new.entity_id, now != "IDLE"))
        keys = ("source", "source_list")
        if old is None or old.state != new.state or any(
            old.attributes.get(k) != new.attributes.get(k) for k in keys
        ):
            hass.async_create_task(hub.ensure_sources())

    if hub.bindings:
        entry.async_on_unload(
            async_track_state_change_event(hass, list(hub.bindings), _station_changed)
        )

    async def _check_sources(_now: Any) -> None:
        await hub.ensure_sources()

    @callback
    def _check_later(_: Any = None) -> None:
        entry.async_on_unload(async_call_later(hass, SOURCE_CHECK_DELAY, _check_sources))

    if hass.state is CoreState.running:
        _check_later()
    else:
        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _check_later))

    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
