"""Per-station switch: stream this station to its Control4 room or not."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import RelayHub
from .media_player import hub_device


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    hub: RelayHub = entry.runtime_data
    switches = []
    for station, room_id in hub.bindings.items():
        if room_id not in hub.rooms:
            continue
        sw = RelayEnabledSwitch(hub, station, hub.rooms[room_id])
        hub.switches[station] = sw
        switches.append(sw)
    async_add_entities(switches)


class RelayEnabledSwitch(SwitchEntity, RestoreEntity):
    """Off: the station plays by itself (source "Станция"), no Control4."""

    _attr_should_poll = False
    _attr_icon = "mdi:speaker-wireless"

    def __init__(self, hub: RelayHub, station: str, room_name: str) -> None:
        self._hub = hub
        self._station = station
        self._attr_name = f"Трансляция в Control4 {room_name}"
        self._attr_unique_id = f"{hub.entry.entry_id}_{station}_enabled"
        self._attr_device_info = hub_device(hub)
        self._attr_is_on = True

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last is not None:
            self._attr_is_on = last.state != "off"

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()
        await self._hub.ensure_sources()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()
        await self._hub.ensure_sources()
