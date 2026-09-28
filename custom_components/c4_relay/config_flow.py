"""Config flow: connect to the Relay driver, then bind stations to rooms."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .api import RelayAuthError, RelayClient, RelayError
from .const import (
    CONF_BINDINGS,
    CONF_HA_URL,
    CONF_PAIRING_CODE,
    CONF_WEBHOOK_ID,
    DEFAULT_PORT,
    DOMAIN,
    NO_ROOM,
    YANDEX_DOMAIN,
)
from .helpers import suggest_room


def yandex_stations(hass: HomeAssistant) -> list[dict[str, Any]]:
    """AlexxIT station media players with their display name and area name."""
    ent_reg, dev_reg, area_reg = er.async_get(hass), dr.async_get(hass), ar.async_get(hass)
    out = []
    for e in ent_reg.entities.values():
        if e.platform != YANDEX_DOMAIN or e.domain != "media_player" or e.disabled_by:
            continue
        area_id = e.area_id
        if not area_id and e.device_id and (dev := dev_reg.async_get(e.device_id)):
            area_id = dev.area_id
        area = area_reg.async_get_area(area_id) if area_id else None
        st = hass.states.get(e.entity_id)
        name = e.name or e.original_name or (st.name if st else None) or e.entity_id
        out.append({"entity_id": e.entity_id, "name": name, "area": area.name if area else None})
    return sorted(out, key=lambda s: s["name"])


def bind_schema(stations: list[dict], rooms: list[dict], current: Mapping[str, int]) -> tuple[vol.Schema, dict[str, str]]:
    """One room dropdown per station. Keys are station names (the form shows
    keys as labels); the returned map turns them back into entity ids."""
    options = [selector.SelectOptionDict(value=NO_ROOM, label="— не транслировать —")] + [
        selector.SelectOptionDict(value=str(r["id"]), label=r["name"]) for r in rooms
    ]
    room_select = selector.SelectSelector(
        selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.DROPDOWN)
    )
    fields: dict = {}
    key_to_entity: dict[str, str] = {}
    for s in stations:
        key = s["name"] if s["name"] not in key_to_entity else f"{s['name']} ({s['entity_id']})"
        key_to_entity[key] = s["entity_id"]
        if s["entity_id"] in current:
            default = str(current[s["entity_id"]])
        else:
            guess = suggest_room([s["area"], s["name"]], rooms)
            default = str(guess) if guess is not None else NO_ROOM
        fields[vol.Required(key, default=default)] = room_select
    return vol.Schema(fields), key_to_entity


def parse_bindings(user_input: Mapping[str, Any], key_to_entity: Mapping[str, str],
                   rooms: list[dict]) -> dict[str, int]:
    known = {int(r["id"]) for r in rooms}
    out = {}
    for key, value in user_input.items():
        if key in key_to_entity and value != NO_ROOM and int(value) in known:
            out[key_to_entity[key]] = int(value)
    return out


def default_ha_url(hass: HomeAssistant) -> str:
    try:
        return get_url(hass, allow_internal=True, allow_external=False, allow_cloud=False,
                       prefer_external=False)
    except NoURLAvailableError:
        return ""


async def _check(hass: HomeAssistant, host: str, port: int, code: str) -> tuple[dict, list, str | None]:
    client = RelayClient(async_get_clientsession(hass), host, port, code)
    try:
        info = await client.info()
        rooms = await client.rooms()
    except RelayAuthError:
        return {}, [], "invalid_auth"
    except RelayError:
        return {}, [], "cannot_connect"
    return info, rooms, None


class RelayConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._rooms: list[dict] = []
        self._keys: dict[str, str] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host, port = user_input[CONF_HOST].strip(), int(user_input[CONF_PORT])
            code = user_input[CONF_PAIRING_CODE].strip().upper()
            ha_url = user_input[CONF_HA_URL].strip().rstrip("/")
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()
            if not ha_url.startswith(("http://", "https://")):
                errors[CONF_HA_URL] = "bad_url"
            else:
                _info, rooms, err = await _check(self.hass, host, port, code)
                if err:
                    errors["base"] = err
                else:
                    self._rooms = rooms
                    self._data = {CONF_HOST: host, CONF_PORT: port, CONF_PAIRING_CODE: code,
                                  CONF_HA_URL: ha_url, CONF_WEBHOOK_ID: webhook.async_generate_id()}
                    return await self.async_step_bind()
        schema = vol.Schema({
            vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=(user_input or {}).get(CONF_PORT, DEFAULT_PORT)): int,
            vol.Required(CONF_PAIRING_CODE, default=(user_input or {}).get(CONF_PAIRING_CODE, "")): str,
            vol.Required(CONF_HA_URL, default=(user_input or {}).get(CONF_HA_URL) or default_ha_url(self.hass)): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_bind(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        stations = yandex_stations(self.hass)
        if user_input is not None:
            bindings = parse_bindings(user_input, self._keys, self._rooms)
            return self.async_create_entry(
                title=f"Control4 {self._data[CONF_HOST]}", data=self._data,
                options={CONF_BINDINGS: bindings},
            )
        if not stations:
            return self.async_abort(reason="no_stations")
        schema, self._keys = bind_schema(stations, self._rooms, {})
        return self.async_show_form(step_id="bind", data_schema=schema)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The driver was re-added: new pairing code (and maybe a new port)."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            code = user_input[CONF_PAIRING_CODE].strip().upper()
            port = int(user_input[CONF_PORT])
            _info, _rooms, err = await _check(self.hass, entry.data[CONF_HOST], port, code)
            if err:
                errors["base"] = err
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PAIRING_CODE: code, CONF_PORT: port}
                )
        schema = vol.Schema({
            vol.Required(CONF_PAIRING_CODE): str,
            vol.Required(CONF_PORT, default=entry.data[CONF_PORT]): int,
        })
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors,
                                    description_placeholders={"host": entry.data[CONF_HOST]})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RelayOptionsFlow()


class RelayOptionsFlow(OptionsFlow):
    """Re-bind stations to rooms (rooms are re-read from the driver)."""

    def __init__(self) -> None:
        self._rooms: list[dict] = []
        self._keys: dict[str, str] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_BINDINGS: parse_bindings(user_input, self._keys, self._rooms)}
            )
        _info, self._rooms, err = await _check(
            self.hass, entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data[CONF_PAIRING_CODE]
        )
        if err:
            return self.async_abort(reason=err)
        stations = yandex_stations(self.hass)
        if not stations:
            return self.async_abort(reason="no_stations")
        current = {k: int(v) for k, v in entry.options.get(CONF_BINDINGS, {}).items()}
        schema, self._keys = bind_schema(stations, self._rooms, current)
        return self.async_show_form(step_id="init", data_schema=schema)
