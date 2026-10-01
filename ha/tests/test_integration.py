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
    aioclient_mock.post(f"{BASE}/volume_step", json={"room_id": 21, "result": "step", "volume": 45})
    aioclient_mock.post(f"{BASE}/volume_level", json={"room_id": 21, "result": "absolute", "volume": 38})
    aioclient_mock.post(f"{BASE}/alarms", json={"room_id": 21, "next": "2026-09-29 07:30"})
    aioclient_mock.post(f"{BASE}/heartbeat", json={"ok": True, "version": "0.5.0", "paired": True})


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
    assert pair[2] == {"webhook_url": f"http://192.0.2.20:8123/api/webhook/{HOOK}", "rooms": [21]}
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


async def test_transport_commands_to_driver(hass, aioclient_mock, relay, hass_admin_user) -> None:
    from homeassistant.core import Context
    entry, station, calls = relay
    pid = player_id(hass, entry)
    user = Context(user_id=hass_admin_user.id)            # a person: no track-change grace
    await hass.services.async_call("media_player", "media_pause", {"entity_id": pid}, blocking=True, context=user)
    assert hass.states.get(pid).state == "paused"
    await hass.services.async_call("media_player", "media_play", {"entity_id": pid}, blocking=True)
    await hass.services.async_call("media_player", "media_stop", {"entity_id": pid}, blocking=True, context=user)
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


# --- volume by Alice's directives (0.3.0) -----------------------------------------

def hook_fake_station(hub):
    from types import SimpleNamespace
    seen = []
    glagol = SimpleNamespace(update_handler=lambda data: seen.append(data))
    fake = SimpleNamespace(glagol=glagol)
    patcher = patch.object(hub, "_station_entity", return_value=fake)
    patcher.start()
    hub.hook_stations()
    return glagol, seen, patcher


def set_level_msg(level, volume=0.0):
    return {"state": {"aliceState": "IDLE", "volume": volume, "playing": True},
            "vinsResponse": {"response": {"directives": [
                {"name": "sound_set_level", "payload": {"new_level": level, "new_percent_level": level * 10}},
                {"name": "tts_play_placeholder", "payload": {"channel": "Dialog"}}]}}}


async def test_louder_on_muted_station_steps_room(hass, aioclient_mock, relay) -> None:
    entry, station, _ = relay
    set_station(hass, station, source="Control4 Офис", source_list=["Станция", "Control4 Офис"])
    glagol, seen, patcher = hook_fake_station(entry.runtime_data)
    try:
        glagol.update_handler({"state": {"aliceState": "IDLE", "volume": 0.0, "playing": True}})
        glagol.update_handler(set_level_msg(1))           # "громче" on the muted station
        glagol.update_handler(set_level_msg(0))           # "тише"
        await hass.async_block_till_done()
    finally:
        patcher.stop()
    assert [c[2] for c in calls_to(aioclient_mock, "/volume_step")] == [
        {"room_id": 21, "steps": 1}, {"room_id": 21, "steps": -1}]
    assert len(seen) == 3                                   # AlexxIT got all messages
    assert hass.states.get(player_id(hass, entry)).attributes["volume_level"] == 0.45


async def test_absolute_level_and_unmuted_station(hass, aioclient_mock, relay) -> None:
    entry, station, _ = relay
    set_station(hass, station, source="Control4 Офис", source_list=["Станция", "Control4 Офис"])
    glagol, _, patcher = hook_fake_station(entry.runtime_data)
    try:
        glagol.update_handler({"state": {"volume": 0.0}})
        glagol.update_handler(set_level_msg(5))            # "громкость 5"
        glagol.update_handler({"state": {"volume": 0.4}})  # station not muted
        glagol.update_handler(set_level_msg(5, volume=0.4))
        await hass.async_block_till_done()
    finally:
        patcher.stop()
    assert [c[2] for c in calls_to(aioclient_mock, "/volume_level")] == [{"room_id": 21, "level": 0.5}]
    assert [c[2] for c in calls_to(aioclient_mock, "/volume_step")] == [{"room_id": 21, "steps": 1}]


async def test_directives_ignored_when_not_streaming(hass, aioclient_mock, relay) -> None:
    entry, station, _ = relay
    set_station(hass, station, source="Станция", source_list=["Станция", "Control4 Офис"])
    glagol, _, patcher = hook_fake_station(entry.runtime_data)
    try:
        glagol.update_handler(set_level_msg(1))
        await hass.async_block_till_done()
    finally:
        patcher.stop()
    assert calls_to(aioclient_mock, "/volume_step") == []


async def test_alexxit_volume_sync_ignored_while_hooked(hass, aioclient_mock, relay) -> None:
    entry, _, _ = relay
    _, _, patcher = hook_fake_station(entry.runtime_data)
    try:
        await hass.services.async_call("media_player", "volume_set",
                                       {"entity_id": player_id(hass, entry), "volume_level": 0.1},
                                       blocking=True)
    finally:
        patcher.stop()
    assert calls_to(aioclient_mock, "/station_volume") == []
    assert calls_to(aioclient_mock, "/volume_level") == []


async def test_user_slider_sets_room_level(hass, aioclient_mock, relay, hass_admin_user) -> None:
    from homeassistant.core import Context
    entry, _, _ = relay
    await hass.services.async_call("media_player", "volume_set",
                                   {"entity_id": player_id(hass, entry), "volume_level": 0.5},
                                   blocking=True, context=Context(user_id=hass_admin_user.id))
    assert [c[2] for c in calls_to(aioclient_mock, "/volume_level")] == [{"room_id": 21, "level": 0.5}]


async def test_volume_settings_backed_up_and_sent_on_pairing(hass, aioclient_mock, hass_client_no_auth) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry as MCE
    mock_driver(aioclient_mock)
    station = add_station(hass)
    entry = MCE(domain=DOMAIN, unique_id=f"{HOST}:{PORT}", title="Control4",
                data={"host": HOST, "port": PORT, CONF_PAIRING_CODE: "ABCD1234",
                      CONF_HA_URL: "http://192.0.2.20:8123", CONF_WEBHOOK_ID: HOOK},
                options={CONF_BINDINGS: {station: 21}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    cfg = {"21": {"step": 10, "max": 50, "min": 5, "duck": "Lower", "duck_level": 30}}
    client = await hass_client_no_auth()
    await client.post(f"/api/webhook/{HOOK}", json={"event": "volume_cfg", "volume_cfg": cfg})
    await hass.async_block_till_done()
    assert entry.runtime_data.volume_cfg == cfg
    # re-pairing (e.g. the driver was re-added) sends the copy back
    aioclient_mock.clear_requests()
    mock_driver(aioclient_mock)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert calls_to(aioclient_mock, "/pair")[0][2]["volume_cfg"] == cfg
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# --- alarms (0.4.0) -------------------------------------------------------------

def add_station_calendar(hass, station, *, disabled=False) -> str:
    """Put the station on a device together with an AlexxIT alarm calendar."""
    from homeassistant.helpers import device_registry as dr
    yentry = MockConfigEntry(domain="yandex_station", unique_id="yandex-account")
    yentry.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(config_entry_id=yentry.entry_id,
                                                identifiers={("yandex_station", "dev-office")})
    ent_reg = er.async_get(hass)
    ent_reg.async_update_entity(station, device_id=dev.id)
    cal = ent_reg.async_get_or_create(
        "calendar", "yandex_station", "dev-office_calendar", device_id=dev.id,
        suggested_object_id="yandex_station_dev_office_calendar",
        disabled_by=er.RegistryEntryDisabler.INTEGRATION if disabled else None)
    return cal.entity_id


def fake_calendar(*events):
    from types import SimpleNamespace
    return SimpleNamespace(events=list(events))


def alarm_event(hh, mm, uid, rrule=None, summary="Будильник"):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    start = datetime(2026, 9, 29, hh, mm, tzinfo=timezone(timedelta(hours=3)))
    return SimpleNamespace(start=start, end=start + timedelta(minutes=1), summary=summary, uid=uid, rrule=rrule)


async def test_alarms_sent_once_and_on_change(hass, aioclient_mock, relay) -> None:
    entry, station, _ = relay
    add_station_calendar(hass, station)
    hub = entry.runtime_data
    cal = fake_calendar(alarm_event(7, 30, "w", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"),
                        alarm_event(9, 0, "o"))
    with patch.object(hub, "_calendar_entity", return_value=cal):
        await hub.sync_alarms()
        await hub.sync_alarms()                               # unchanged: not sent again
        assert [c[2] for c in calls_to(aioclient_mock, "/alarms")] == [{"room_id": 21, "alarms": [
            {"id": "o", "time": "09:00", "enabled": True, "date": "2026-09-29"},
            {"id": "w", "time": "07:30", "enabled": True, "days": [1, 2, 3, 4, 5]}]}]
        cal.events.pop()                                      # "Алиса, удали будильник"
        await hub.sync_alarms()
    assert calls_to(aioclient_mock, "/alarms")[-1][2] == {"room_id": 21, "alarms": [
        {"id": "w", "time": "07:30", "enabled": True, "days": [1, 2, 3, 4, 5]}]}


async def test_alarms_without_calendar(hass, aioclient_mock, relay) -> None:
    entry, station, _ = relay
    hub = entry.runtime_data
    await hub.sync_alarms()                                   # station has no calendar at all
    assert calls_to(aioclient_mock, "/alarms") == []
    add_station_calendar(hass, station, disabled=True)
    await hub.sync_alarms()                                   # disabled calendar = no alarms
    assert [c[2] for c in calls_to(aioclient_mock, "/alarms")] == [{"room_id": 21, "alarms": []}]


async def test_alarm_calendar_not_loaded_yet(hass, aioclient_mock, relay) -> None:
    entry, station, _ = relay
    add_station_calendar(hass, station)
    await entry.runtime_data.sync_alarms()
    assert calls_to(aioclient_mock, "/alarms") == []


async def test_alarm_event_from_driver(hass, aioclient_mock, relay, hass_client_no_auth) -> None:
    entry, station, _ = relay
    seen = []
    hass.bus.async_listen("c4_relay_alarm", lambda e: seen.append(e.data))
    client = await hass_client_no_auth()
    await client.post(f"/api/webhook/{HOOK}", json={"event": "alarm", "room_id": 21, "phase": "prewake",
                                                    "alarm_id": "w", "time": "2026-09-29 07:30"})
    await hass.async_block_till_done()
    assert seen == [{"room_id": 21, "room": "Офис", "station": station, "phase": "prewake",
                     "alarm_id": "w", "time": "2026-09-29 07:30"}]


# --- heartbeat, health, Repairs (0.5.0) ------------------------------------------

def issue(hass, entry, key):
    from homeassistant.helpers import issue_registry as ir
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{entry.entry_id}_{key}".replace(".", "_"))


def remock(aioclient_mock, path, **kw):
    """Replace one endpoint's answer (first registered match wins)."""
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{BASE}{path}", **kw)
    mock_driver(aioclient_mock)


async def test_heartbeat_carries_health_summary(hass, aioclient_mock, relay) -> None:
    entry, station, _ = relay
    set_station(hass, station, source="Control4 Офис", source_list=["Станция", "Control4 Офис"])
    glagol, _, patcher = hook_fake_station(entry.runtime_data)
    try:
        await entry.runtime_data.heartbeat(now=0)
    finally:
        patcher.stop()
    body = calls_to(aioclient_mock, "/heartbeat")[-1][2]
    assert body["webhook_url"] == f"http://192.0.2.20:8123/api/webhook/{HOOK}"
    assert body["summary"].endswith("; Офис: no alarm calendar")
    assert body["summary"].startswith("c4_relay ")


async def test_readded_driver_with_old_code_is_paired_again(hass, aioclient_mock, relay) -> None:
    entry, station, _ = relay
    remock(aioclient_mock, "/heartbeat", json={"ok": True, "version": "0.5.0", "paired": False})
    await entry.runtime_data.heartbeat(now=0)
    assert calls_to(aioclient_mock, "/pair")[0][2]["rooms"] == [21]


async def test_rejected_code_issue_then_reauth(hass, aioclient_mock, relay) -> None:
    entry, _, _ = relay
    remock(aioclient_mock, "/heartbeat", status=401, json={"error": "bad pairing code"})
    hub = entry.runtime_data
    await hub.heartbeat(now=0)
    found = issue(hass, entry, "pairing_rejected")
    assert found is not None and found.translation_placeholders["code"] == "ABCD1234"
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    await hub.heartbeat(now=601)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    remock(aioclient_mock, "/heartbeat", json={"ok": True, "paired": True})
    await hub.heartbeat(now=660)                          # old code pasted: issue gone
    assert issue(hass, entry, "pairing_rejected") is None


async def test_unreachable_driver_issue_after_5_min(hass, aioclient_mock, relay) -> None:
    import aiohttp
    entry, _, _ = relay
    remock(aioclient_mock, "/heartbeat", exc=aiohttp.ClientConnectionError())
    hub = entry.runtime_data
    await hub.heartbeat(now=0)
    assert issue(hass, entry, "driver_unreachable") is None
    await hub.heartbeat(now=300)
    assert issue(hass, entry, "driver_unreachable") is not None


async def test_alexxit_breakage_raises_issues(hass, aioclient_mock, relay) -> None:
    entry, station, _ = relay
    hub = entry.runtime_data
    set_station(hass, station, source="Станция", source_list=["Станция"])   # no hook, no target
    await hub.heartbeat(now=0)
    assert issue(hass, entry, f"volume_fallback:{station}") is None
    await hub.heartbeat(now=180)
    assert issue(hass, entry, f"volume_fallback:{station}") is not None
    assert issue(hass, entry, f"source_missing:{station}") is not None
    set_station(hass, station, source="Control4 Офис", source_list=["Станция", "Control4 Офис"])
    _, _, patcher = hook_fake_station(hub)
    try:
        await hub.heartbeat(now=240)
    finally:
        patcher.stop()
    assert issue(hass, entry, f"volume_fallback:{station}") is None
    assert issue(hass, entry, f"source_missing:{station}") is None


async def test_unavailable_station_raises_no_alexxit_issue(hass, aioclient_mock, relay) -> None:
    entry, station, _ = relay
    set_station(hass, station, state="unavailable")
    await entry.runtime_data.heartbeat(now=0)
    await entry.runtime_data.heartbeat(now=500)
    assert issue(hass, entry, f"volume_fallback:{station}") is None
    assert "Офис: station unavailable" in calls_to(aioclient_mock, "/heartbeat")[-1][2]["summary"]


# --- stray play_media while the station is paused (0.5.1) -----------------------

def glagol_state(playing: bool, track: str = "1675194") -> dict:
    return {"state": {"aliceState": "IDLE", "volume": 0.0, "playing": playing,
                      "playerState": {"id": track}}}


async def play_media(hass, entry, url="https://s1.storage.yandex.net/get-mp3/x/y/z.mp3"):
    await hass.services.async_call("media_player", "play_media", {
        "entity_id": player_id(hass, entry), "media_content_type": "music",
        "media_content_id": proxy_url(url)}, blocking=True)


async def test_resync_while_paused_does_not_start_room(hass, aioclient_mock, relay) -> None:
    entry, station, calls = relay
    glagol, _, patcher = hook_fake_station(entry.runtime_data)
    try:
        glagol.update_handler(glagol_state(playing=False))
        await play_media(hass, entry)                     # AlexxIT: source selected again
        assert calls_to(aioclient_mock, "/play") == []
        glagol.update_handler(glagol_state(playing=True))
        await hass.services.async_call("media_player", "media_play",
                                       {"entity_id": player_id(hass, entry)}, blocking=True)
    finally:
        patcher.stop()
    assert len(calls_to(aioclient_mock, "/play")) == 1       # the held track, not /resume
    assert calls_to(aioclient_mock, "/resume") == []
    assert calls[-1] == (station, "media_seek", {"seek_position": 0})


async def test_track_change_while_playing_goes_through(hass, aioclient_mock, relay) -> None:
    entry, _, _ = relay
    glagol, _, patcher = hook_fake_station(entry.runtime_data)
    try:
        glagol.update_handler(glagol_state(playing=True))
        await play_media(hass, entry)
    finally:
        patcher.stop()
    assert len(calls_to(aioclient_mock, "/play")) == 1


async def test_paused_station_by_ha_state_without_hook(hass, aioclient_mock, relay) -> None:
    entry, station, _ = relay
    set_station(hass, station, state="paused")
    await play_media(hass, entry)
    assert calls_to(aioclient_mock, "/play") == []


async def test_room_with_two_stations_follows_the_playing_one(hass, aioclient_mock, relay) -> None:
    entry, station, _ = relay
    hub = entry.runtime_data
    other = "media_player.yandex_station_bedroom"
    hub.bindings = {other: 21, station: 21}
    name = hub.players[21].name
    hass.states.async_set(other, "paused", {"source": name, "source_list": ["Станция", name]})
    set_station(hass, station, state="playing", source=name, source_list=["Станция", name])
    assert hub.station_for_room(21) == station


# --- track change = pause + play from AlexxIT (0.5.3) ----------------------------

async def test_track_change_keeps_room_on(hass, aioclient_mock, relay) -> None:
    entry, _, _ = relay
    glagol, _, patcher = hook_fake_station(entry.runtime_data)
    try:
        with patch("custom_components.c4_relay.media_player.PAUSE_SETTLE", 0.05):
            glagol.update_handler(glagol_state(playing=True))
            await hass.services.async_call("media_player", "media_pause",
                                           {"entity_id": player_id(hass, entry)}, blocking=False)
            await play_media(hass, entry)          # next track a moment later
            await hass.async_block_till_done()
    finally:
        patcher.stop()
    assert calls_to(aioclient_mock, "/pause") == []
    assert len(calls_to(aioclient_mock, "/play")) == 1


async def test_real_pause_still_switches_room_off(hass, aioclient_mock, relay) -> None:
    entry, _, _ = relay
    glagol, _, patcher = hook_fake_station(entry.runtime_data)
    try:
        with patch("custom_components.c4_relay.media_player.PAUSE_SETTLE", 0.05):
            glagol.update_handler(glagol_state(playing=False))   # "Алиса, пауза"
            await hass.services.async_call("media_player", "media_pause",
                                           {"entity_id": player_id(hass, entry)}, blocking=True)
    finally:
        patcher.stop()
    assert len(calls_to(aioclient_mock, "/pause")) == 1


async def test_user_pause_is_immediate(hass, aioclient_mock, relay, hass_admin_user) -> None:
    from homeassistant.core import Context
    entry, _, _ = relay
    glagol, _, patcher = hook_fake_station(entry.runtime_data)
    try:
        with patch("custom_components.c4_relay.media_player.PAUSE_SETTLE", 30):
            glagol.update_handler(glagol_state(playing=True))
            await hass.services.async_call("media_player", "media_pause",
                                           {"entity_id": player_id(hass, entry)}, blocking=True,
                                           context=Context(user_id=hass_admin_user.id))
    finally:
        patcher.stop()
    assert len(calls_to(aioclient_mock, "/pause")) == 1


# --- spoken volume commands without vinsResponse (0.6.2) -------------------------

def hook_fake_station_with_level(hub, level):
    from types import SimpleNamespace
    glagol = SimpleNamespace(update_handler=lambda data: None)
    fake = SimpleNamespace(glagol=glagol, _attr_volume_level=level)
    patcher = patch.object(hub, "_station_entity", return_value=fake)
    patcher.start()
    hub.hook_stations()
    return glagol, fake, patcher


def alice(state, volume):
    return {"state": {"aliceState": state, "volume": volume, "playing": True}}


async def test_spoken_louder_quieter_and_level(hass, aioclient_mock, relay) -> None:
    entry, station, _ = relay
    set_station(hass, station, source="Control4 Офис", source_list=["Станция", "Control4 Офис"])
    glagol, fake, patcher = hook_fake_station_with_level(entry.runtime_data, 0.1)
    try:
        await hass.async_block_till_done()
        assert fake._attr_volume_level == 0.4                 # restore level kept away from 0.1
        for seq in ([("LISTENING", 0.0), ("BUSY", 0.4), ("IDLE", 0.1)],     # "громче"
                    [("LISTENING", 0.0), ("IDLE", 0.0)],                    # "тише"
                    [("LISTENING", 0.4), ("BUSY", 0.4), ("IDLE", 0.5)],     # "громкость 5"
                    [("LISTENING", 0.4), ("BUSY", 0.4), ("NONE", 0.4), ("IDLE", 0.4)]):  # a question
            fake._attr_volume_level = 0.4
            for st, vol in seq:
                glagol.update_handler(alice(st, vol))
            glagol.update_handler(alice("IDLE", 0.0))         # AlexxIT mutes again
            await hass.async_block_till_done()
    finally:
        patcher.stop()
    assert [c[2] for c in calls_to(aioclient_mock, "/volume_step")] == [
        {"room_id": 21, "steps": 1}, {"room_id": 21, "steps": -1}]
    assert [c[2] for c in calls_to(aioclient_mock, "/volume_level")] == [{"room_id": 21, "level": 0.5}]


async def test_text_command_with_directive_not_counted_twice(hass, aioclient_mock, relay) -> None:
    entry, station, _ = relay
    set_station(hass, station, source="Control4 Офис", source_list=["Станция", "Control4 Офис"])
    glagol, fake, patcher = hook_fake_station_with_level(entry.runtime_data, 0.4)
    try:
        glagol.update_handler(alice("BUSY", 0.4))
        glagol.update_handler({"state": {"aliceState": "BUSY", "volume": 0.4, "playing": True},
                               "vinsResponse": {"response": {"directives": [{"name": "sound_louder"}]}}})
        glagol.update_handler(alice("IDLE", 0.5))
        await hass.async_block_till_done()
    finally:
        patcher.stop()
    assert [c[2] for c in calls_to(aioclient_mock, "/volume_step")] == [{"room_id": 21, "steps": 1}]
    assert calls_to(aioclient_mock, "/volume_level") == []


async def test_spoken_command_ignored_when_not_streaming(hass, aioclient_mock, relay) -> None:
    entry, station, _ = relay
    set_station(hass, station, source="Станция", source_list=["Станция", "Control4 Офис"])
    glagol, fake, patcher = hook_fake_station_with_level(entry.runtime_data, 0.6)
    try:
        for st, vol in [("LISTENING", 0.6), ("IDLE", 0.7)]:
            glagol.update_handler(alice(st, vol))
        await hass.async_block_till_done()
    finally:
        patcher.stop()
    assert calls_to(aioclient_mock, "/volume_step") == [] and calls_to(aioclient_mock, "/volume_level") == []
    assert fake._attr_volume_level == 0.6                     # not streaming: AlexxIT's level untouched


async def test_unmute_level_kept_after_repeated_idle(hass, aioclient_mock, relay) -> None:
    """Site 2026-10-01: "IDLE 0.1" came twice; AlexxIT took 0.1 again from the second."""
    from types import SimpleNamespace
    entry, station, _ = relay
    set_station(hass, station, source="Control4 Офис", source_list=["Станция", "Control4 Офис"])
    fake = SimpleNamespace(_attr_volume_level=0.4)

    def alexxit(data):                       # AlexxIT remembers every non-zero level
        vol = (data.get("state") or {}).get("volume")
        if vol:
            fake._attr_volume_level = vol

    fake.glagol = SimpleNamespace(update_handler=alexxit)
    hub = entry.runtime_data
    with patch.object(hub, "_station_entity", return_value=fake):
        hub.hook_stations()
        for _ in range(2):                   # "громче", "громче"
            for st, vol in [("LISTENING", 0.0), ("LISTENING", 0.4), ("IDLE", 0.1), ("IDLE", 0.1), ("IDLE", 0.0)]:
                fake.glagol.update_handler(alice(st, vol))
                await hass.async_block_till_done()
    assert [c[2] for c in calls_to(aioclient_mock, "/volume_step")] == [
        {"room_id": 21, "steps": 1}, {"room_id": 21, "steps": 1}]
    assert fake._attr_volume_level == 0.4
