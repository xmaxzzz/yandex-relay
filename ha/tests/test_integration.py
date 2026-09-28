"""c4_relay on a real Home Assistant core, with the driver's HTTP API mocked.

The station is a plain state plus an entity-registry entry of platform
yandex_station (what the integration looks for); station service calls are
recorded instead of executed.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import patch

import pytest

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.c4_relay.const import (
    CONF_BINDINGS,
    CONF_HA_URL,
    CONF_PAIRING_CODE,
    CONF_WEBHOOK_ID,
    DOMAIN,
)

HOST, PORT = "192.0.2.10", 18765
BASE = f"http://{HOST}:{PORT}"
ROOMS = [{"id": 21, "name": "Офис"}, {"id": 22, "name": "Гостиная"}]
HOOK = "testhook123"


def proxy_url(direct: str) -> str:
    b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"http://192.0.2.20:8123/api/yandex_station/{b64({'alg': 'HS256'})}.{b64({'url': direct, 'exp': 9})}.sig.mp3"


def mock_driver(aioclient_mock, info_status: int = 200) -> None:
    aioclient_mock.get(f"{BASE}/info", status=info_status,
                       json={"driver": "yandex-relay", "version": "0.1.6", "paired": False, "rooms": 2})
    aioclient_mock.get(f"{BASE}/rooms", json={"rooms": ROOMS})
    aioclient_mock.post(f"{BASE}/pair", json={"ok": True, "version": "0.1.6"})
    aioclient_mock.get(f"{BASE}/state?room_id=21", json={"room_id": 21, "state": "idle"})
    aioclient_mock.post(f"{BASE}/play", json={"room_id": 21, "state": "starting", "title": "Song", "artist": "Band"})
    aioclient_mock.post(f"{BASE}/pause", json={"room_id": 21, "state": "paused"})
    aioclient_mock.post(f"{BASE}/resume", json={"room_id": 21, "state": "playing", "resume": "in_place"})
    aioclient_mock.post(f"{BASE}/stop", json={"room_id": 21, "state": "stopped"})
    aioclient_mock.post(f"{BASE}/station_volume", json={"room_id": 21, "result": "step", "volume": 45})
    aioclient_mock.post(f"{BASE}/duck", json={"room_id": 21, "result": "ducked", "ducked": True})


def add_station(hass: HomeAssistant, *, area: str | None = "Офис", state: str = "playing",
                source: str = "Станция", source_list: list[str] | None = None, **attrs) -> str:
    ent_reg = er.async_get(hass)
    entity = ent_reg.async_get_or_create(
        "media_player", "yandex_station", "station-office",
        suggested_object_id="yandex_station_office", original_name="Яндекс Станция",
    )
    if area:
        entity = ent_reg.async_update_entity(entity.entity_id, area_id=ar.async_get(hass).async_get_or_create(area).id)
    set_station(hass, entity.entity_id, state=state, source=source, source_list=source_list, **attrs)
    return entity.entity_id


def set_station(hass, entity_id, *, state="playing", source="Станция", source_list=None, **attrs) -> None:
    hass.states.async_set(entity_id, state, {
        "friendly_name": "Яндекс Станция", "source": source,
        "source_list": source_list if source_list is not None else ["Станция"], **attrs,
    })


def calls_to(aioclient_mock, path: str) -> list:
    return [c for c in aioclient_mock.mock_calls if str(c[1]).startswith(BASE + path)]


# --- config flow -------------------------------------------------------------

async def test_config_flow_binds_station_by_area(hass: HomeAssistant, aioclient_mock) -> None:
    mock_driver(aioclient_mock)
    station = add_station(hass)
    with patch("custom_components.c4_relay.async_setup_entry", return_value=True):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        assert r["type"] is FlowResultType.FORM and r["step_id"] == "user"
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {
            "host": HOST, "port": PORT, CONF_PAIRING_CODE: " abcd1234 ", CONF_HA_URL: "http://192.0.2.20:8123/",
        })
        assert r["step_id"] == "bind"
        key = next(iter(r["data_schema"].schema))
        assert str(key) == "Яндекс Станция"
        assert key.default() == "21"                    # area "Офис" -> room Офис
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {str(key): "21"})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["data"][CONF_PAIRING_CODE] == "ABCD1234"
    assert r["data"][CONF_HA_URL] == "http://192.0.2.20:8123"
    assert r["data"][CONF_WEBHOOK_ID]
    assert r["options"][CONF_BINDINGS] == {station: 21}


async def test_config_flow_bad_code(hass: HomeAssistant, aioclient_mock) -> None:
    mock_driver(aioclient_mock, info_status=401)
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {
        "host": HOST, "port": PORT, CONF_PAIRING_CODE: "WRONG", CONF_HA_URL: "http://192.0.2.20:8123",
    })
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": "invalid_auth"}


async def test_config_flow_unreachable(hass: HomeAssistant, aioclient_mock) -> None:
    import aiohttp
    aioclient_mock.get(f"{BASE}/info", exc=aiohttp.ClientConnectionError())
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {
        "host": HOST, "port": PORT, CONF_PAIRING_CODE: "X", CONF_HA_URL: "http://192.0.2.20:8123",
    })
    assert r["errors"] == {"base": "cannot_connect"}


# --- running entry -------------------------------------------------------------

@pytest.fixture
async def relay(hass: HomeAssistant, aioclient_mock):
    """Set-up entry bound to one station; yields (entry, station, recorded station calls)."""
    mock_driver(aioclient_mock)
    station = add_station(hass, media_title="Bohemian Rhapsody", media_artist="Queen",
                          media_album_name="A Night at the Opera", media_duration=354,
                          media_content_id="12345")
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=f"{HOST}:{PORT}", title="Control4",
        data={"host": HOST, "port": PORT, CONF_PAIRING_CODE: "ABCD1234",
              CONF_HA_URL: "http://192.0.2.20:8123", CONF_WEBHOOK_ID: HOOK},
        options={CONF_BINDINGS: {station: 21}},
    )
    entry.add_to_hass(hass)
    calls: list = []

    async def record(self, entity_id, service, data=None):
        calls.append((entity_id, service, data or {}))

    with patch("custom_components.c4_relay.RelayHub.station_call", record):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry, station, calls
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


def player_id(hass, entry) -> str:
    return er.async_get(hass).async_get_entity_id("media_player", DOMAIN, f"{entry.entry_id}_21")


async def test_setup_pairs_and_creates_entities(hass, aioclient_mock, relay) -> None:
    entry, station, _ = relay
    assert entry.state is ConfigEntryState.LOADED
    pair = calls_to(aioclient_mock, "/pair")[0]
    assert pair[2] == {"webhook_url": f"http://192.0.2.20:8123/api/webhook/{HOOK}"}
    assert pair[3]["X-Relay-Key"] == "ABCD1234"
    st = hass.states.get(player_id(hass, entry))
    assert st.name == "Control4 Офис" and st.state == "idle"
    sw = er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_{station}_enabled")
    assert hass.states.get(sw).state == "on"


async def test_play_media_sends_direct_and_fallback(hass, aioclient_mock, relay) -> None:
    entry, _, _ = relay
    direct = "https://s1.storage.yandex.net/get-mp3/x/y/z.mp3"
    proxy = proxy_url(direct)
    await hass.services.async_call("media_player", "play_media", {
        "entity_id": player_id(hass, entry), "media_content_type": "music", "media_content_id": proxy,
    }, blocking=True)
    body = calls_to(aioclient_mock, "/play")[0][2]
    assert body["room_id"] == 21
    assert body["url"] == direct and body["fallback_url"] == proxy
    assert (body["title"], body["artist"], body["album"]) == ("Bohemian Rhapsody", "Queen", "A Night at the Opera")
    assert body["duration_ms"] == 354000 and body["key"] == "12345"
    assert hass.states.get(player_id(hass, entry)).state == "playing"


async def test_transport_commands_to_driver(hass, aioclient_mock, relay) -> None:
    entry, station, calls = relay
    pid = player_id(hass, entry)
    await hass.services.async_call("media_player", "media_pause", {"entity_id": pid}, blocking=True)
    assert hass.states.get(pid).state == "paused"
    await hass.services.async_call("media_player", "media_play", {"entity_id": pid}, blocking=True)
    await hass.services.async_call("media_player", "media_stop", {"entity_id": pid}, blocking=True)
    assert [len(calls_to(aioclient_mock, p)) for p in ("/pause", "/resume", "/stop")] == [1, 1, 1]
    await hass.services.async_call("media_player", "media_next_track", {"entity_id": pid}, blocking=True)
    assert calls[-1] == (station, "media_next_track", {})


async def test_station_resume_after_room_off_seeks_station(hass, aioclient_mock, relay) -> None:
    entry, station, calls = relay
    aioclient_mock.clear_requests()                   # first match wins: restart before defaults
    aioclient_mock.post(f"{BASE}/resume", json={"room_id": 21, "state": "starting", "resume": "restart"})
    mock_driver(aioclient_mock)
    await hass.services.async_call("media_player", "media_play", {"entity_id": player_id(hass, entry)}, blocking=True)
    assert calls == [(station, "media_seek", {"seek_position": 0})]


async def test_webhook_panel_buttons_drive_station(hass, relay, hass_client_no_auth) -> None:
    _, station, calls = relay
    client = await hass_client_no_auth()
    for evt in ({"event": "transport", "room_id": 21, "action": "next"},
                {"event": "transport", "room_id": 21, "action": "play", "resume": "restart"}):
        resp = await client.post(f"/api/webhook/{HOOK}", json=evt)
        assert resp.status == 200
    await hass.async_block_till_done()
    assert calls == [(station, "media_next_track", {}), (station, "media_play", {}),
                     (station, "media_seek", {"seek_position": 0})]


async def test_webhook_room_stopped_pauses_playing_station(hass, relay, hass_client_no_auth) -> None:
    entry, station, calls = relay
    client = await hass_client_no_auth()
    await client.post(f"/api/webhook/{HOOK}", json={"event": "state", "room_id": 21, "state": "stopped"})
    await hass.async_block_till_done()
    assert calls == [(station, "media_pause", {})]
    assert hass.states.get(player_id(hass, entry)).state == "idle"
    # natural end of a track: the station moves on, nothing to do
    calls.clear()
    await client.post(f"/api/webhook/{HOOK}", json={"event": "state", "room_id": 21, "state": "ended"})
    await hass.async_block_till_done()
    assert calls == []


async def test_webhook_rejects_garbage(hass, relay, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    resp = await client.post(f"/api/webhook/{HOOK}", data="not json")
    assert resp.status in (200, 400)   # HA's webhook layer may swallow the handler status


async def test_ensure_sources_selects_room_player(hass, relay) -> None:
    entry, station, calls = relay
    set_station(hass, station, source="Станция", source_list=["Станция", "Control4 Офис"])
    await entry.runtime_data.ensure_sources()
    assert (station, "select_source", {"source": "Control4 Офис"}) in calls


async def test_ensure_sources_reloads_alexxit_once(hass, relay) -> None:
    entry, station, calls = relay
    set_station(hass, station, source="Станция", source_list=["Станция"])
    with patch.object(hass.config_entries, "async_reload") as reload, \
         patch.object(hass.config_entries, "async_entries", return_value=[MockConfigEntry(domain="yandex_station")]):
        await entry.runtime_data.ensure_sources()
        await entry.runtime_data.ensure_sources()
    assert reload.call_count == 1
    assert not [c for c in calls if c[1] == "select_source"]


async def test_switch_off_returns_station_to_itself(hass, relay) -> None:
    entry, station, calls = relay
    set_station(hass, station, source="Control4 Офис", source_list=["Станция", "Control4 Офис"])
    sw = er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_{station}_enabled")
    await hass.services.async_call("switch", "turn_off", {"entity_id": sw}, blocking=True)
    assert (station, "select_source", {"source": "Станция"}) in calls
    calls.clear()
    # disabled: panel presses no longer reach the station
    await entry.runtime_data.handle_event({"event": "transport", "room_id": 21, "action": "next"})
    assert calls == []


async def test_bad_code_at_setup_starts_reauth(hass, aioclient_mock) -> None:
    mock_driver(aioclient_mock, info_status=401)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=f"{HOST}:{PORT}", data={
        "host": HOST, "port": PORT, CONF_PAIRING_CODE: "OLD", CONF_HA_URL: "http://x:8123", CONF_WEBHOOK_ID: HOOK,
    }, options={CONF_BINDINGS: {}})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == config_entries.SOURCE_REAUTH for f in flows)


# --- volume (0.2.0) ----------------------------------------------------------

async def test_station_volume_goes_to_driver_first_as_baseline(hass, aioclient_mock, relay) -> None:
    entry, _, _ = relay
    pid = player_id(hass, entry)
    for level in (0.4, 0.5):
        await hass.services.async_call("media_player", "volume_set",
                                       {"entity_id": pid, "volume_level": level}, blocking=True)
    bodies = [c[2] for c in calls_to(aioclient_mock, "/station_volume")]
    assert bodies == [{"room_id": 21, "level": 0.4, "initial": True},
                      {"room_id": 21, "level": 0.5, "initial": False}]
    assert hass.states.get(pid).attributes["volume_level"] == 0.45   # room level from the driver


async def test_alice_state_ducks_room(hass, aioclient_mock, relay) -> None:
    _, station, _ = relay
    for state in ("LISTENING", "SPEAKING", "IDLE"):
        set_station(hass, station, alice_state=state)
        await hass.async_block_till_done()
    bodies = [c[2] for c in calls_to(aioclient_mock, "/duck")]
    assert bodies[-3:] == [{"room_id": 21, "active": True}, {"room_id": 21, "active": True},
                           {"room_id": 21, "active": False}]


async def test_alice_state_ignored_when_streaming_disabled(hass, aioclient_mock, relay) -> None:
    entry, station, _ = relay
    sw = er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_{station}_enabled")
    await hass.services.async_call("switch", "turn_off", {"entity_id": sw}, blocking=True)
    n = len(calls_to(aioclient_mock, "/duck"))
    set_station(hass, station, alice_state="LISTENING")
    await hass.async_block_till_done()
    assert len(calls_to(aioclient_mock, "/duck")) == n


async def test_webhook_room_volume_shown_on_player(hass, relay, hass_client_no_auth) -> None:
    entry, _, calls = relay
    client = await hass_client_no_auth()
    await client.post(f"/api/webhook/{HOOK}", json={"event": "volume", "room_id": 21, "level": 33})
    await hass.async_block_till_done()
    assert hass.states.get(player_id(hass, entry)).attributes["volume_level"] == 0.33
    assert calls == []                  # volume changes on the C4 side never touch the station


# --- Glagol diagnostic hook (0.2.1) ---------------------------------------------

async def test_glagol_hook_passes_messages_and_reports_directives(hass, relay) -> None:
    from types import SimpleNamespace
    from pytest_homeassistant_custom_component.common import async_capture_events

    entry, station, _ = relay
    seen = []
    glagol = SimpleNamespace(update_handler=lambda data: seen.append(data))
    fake = SimpleNamespace(glagol=glagol)
    hub = entry.runtime_data
    events = async_capture_events(hass, "c4_relay_vins")
    with patch.object(hub, "_station_entity", return_value=fake):
        hub.hook_stations()
        hub.hook_stations()                          # second call must not double-wrap
    msg = {"state": {"aliceState": "SPEAKING", "volume": 0.3, "playing": True},
           "vinsResponse": {"response": {"directives": [{"name": "sound_louder"}]}}}
    glagol.update_handler(msg)
    await hass.async_block_till_done()
    assert seen == [msg]                             # AlexxIT still gets every message
    assert events[0].data == {"entity_id": station,
                              "directives": [{"name": "sound_louder", "payload": None}]}
    glagol.update_handler(None)                      # disconnect notification passes through
    assert seen[-1] is None


async def test_glagol_hook_survives_missing_alexxit(hass, relay) -> None:
    entry, _, _ = relay
    with patch.object(entry.runtime_data, "_station_entity", return_value=None):
        entry.runtime_data.hook_stations()           # only a warning, no exception
