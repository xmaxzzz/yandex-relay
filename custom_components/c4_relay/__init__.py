"""Control4 Yandex Relay: Yandex Station music played through Control4 rooms.

AlexxIT YandexStation streams a station's music to a media_player chosen as the
station's source. This integration provides one such media_player per Control4
room, forwards play_media to the Yandex Relay driver on the controller, and
turns the driver's webhook events (panel buttons, room state) into station
commands. It also hands each room its stations' alarms (AlexxIT alarm
calendar), which the driver turns into Control4 events.
Design and protocol: docs/DESIGN.md in the yandex-relay repo.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from aiohttp import web

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)

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
from .helpers import (
    alarm_from_event,
    classify_dialog_volume,
    classify_volume_directives,
    extract_directives,
    room_stop_pauses_station,
    station_calls_for_transport,
)

if TYPE_CHECKING:
    from .media_player import RelayRoomPlayer
    from .switch import RelayEnabledSwitch

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.MEDIA_PLAYER, Platform.SWITCH]
SOURCE_CHECK_DELAY = 10  # s after HA start before stations are pointed at their rooms
HOOK_CHECK_INTERVAL = timedelta(seconds=60)  # re-attach after AlexxIT reconnects/reloads
EVENT_VINS = "c4_relay_vins"
EVENT_ALARM = "c4_relay_alarm"
# Level AlexxIT unmutes a streaming station to while Alice talks. Kept away
# from 0.0/0.1 (what "тише"/"громче" leave behind) so spoken commands can be
# told from other requests; also Alice's answers stay audible.
UNMUTE_LEVEL = 0.4
ALARM_SYNC_INTERVAL = timedelta(seconds=30)  # AlexxIT polls the alarms about once a minute
HEARTBEAT_INTERVAL = timedelta(seconds=60)   # the driver calls HA offline after 3 min of silence
HEALTH_ISSUE_AFTER = 180       # s a station problem lasts before a Repairs issue
UNREACHABLE_ISSUE_AFTER = 300  # s the driver is unreachable before a Repairs issue
REAUTH_AFTER = 600             # s of a rejected code before the reauth flow starts


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
        self._last_diag: dict[str, tuple] = {}
        self._hook_failed: set[str] = set()
        self._station_volume: dict[str, float] = {}   # last volume seen in Glagol state
        self._playing: dict[str, bool] = {}           # last "playing" seen in Glagol state
        self._dump_until: dict[str, float] = {}       # debug: full messages around a dialog
        self._dialog: dict[str, dict] = {}            # station -> {"unmute", "handled"} while Alice talks
        # HA's copy of the driver's per-room volume settings (restored on re-add).
        self.store: Store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.volume_cfg: dict = {}
        self._alarms_sent: dict[int, list[dict]] = {}
        self._calendar_hint: set[str] = set()
        self.webhook_url: str = ""
        self.versions: dict[str, str] = {}
        self._since: dict[str, float] = {}     # Repairs issue key -> problem seen since
        self._reauth_started = False

    # --- bindings ---------------------------------------------------------
    def station_for_room(self, room_id: int | None) -> str | None:
        """The room's station; with several, the one playing into the room now."""
        stations = [s for s, room in self.bindings.items() if room == room_id]
        if len(stations) > 1:
            for station in stations:
                if self.streaming(station) and self.station_playing(station):
                    return station
        return stations[0] if stations else None

    def station_playing(self, station: str) -> bool | None:
        """Whether the station itself plays (Glagol state first, HA state otherwise)."""
        if station in self._playing:
            return self._playing[station]
        st = self.hass.states.get(station)
        return None if st is None else st.state == "playing"

    def enabled(self, station: str) -> bool:
        sw = self.switches.get(station)
        return sw.is_on if sw is not None else True

    # --- station control ---------------------------------------------------
    async def station_call(self, station: str, service: str, data: dict | None = None) -> None:
        await self.hass.services.async_call(
            "media_player", service, {"entity_id": station, **(data or {})}, blocking=False
        )

    # --- Glagol messages (diagnostic, 0.2.1) -------------------------------
    def _station_entity(self, station: str) -> Any:
        try:
            return self.hass.data["entity_components"]["media_player"].get_entity(station)
        except (KeyError, AttributeError):
            return None

    @callback
    def hook_stations(self) -> None:
        """Watch every bound station's Glagol messages without changing them.

        AlexxIT hands each local message to glagol.update_handler (its own
        async_set_state). We wrap that callable and pass everything through
        unchanged; the wrapper only looks at the message. Re-run periodically:
        AlexxIT creates a new Glagol client when the station reconnects.
        """
        for station in self.bindings:
            ent = self._station_entity(station)
            glagol = getattr(ent, "glagol", None)
            original = getattr(glagol, "update_handler", None)
            if glagol is None or original is None:
                if station not in self._hook_failed:
                    self._hook_failed.add(station)
                    _LOGGER.warning("%s: no local Glagol connection to watch (yet)", station)
                continue
            if getattr(original, "_c4_relay_hook", False):
                continue
            hub = self

            def wrapper(data, _original=original, _station=station):
                try:
                    hub.on_glagol_message(_station, data)
                except Exception:  # never break AlexxIT because of us
                    _LOGGER.exception("c4_relay Glagol hook failed")
                return _original(data)

            wrapper._c4_relay_hook = True
            glagol.update_handler = wrapper
            self._hook_failed.discard(station)
            self.keep_unmute_level(station)
            _LOGGER.info("Watching Glagol messages of %s for volume commands", station)

    def hooked(self, station: str | None) -> bool:
        """True while our wrapper sits in the station's Glagol handler."""
        if not station:
            return False
        glagol = getattr(self._station_entity(station), "glagol", None)
        return bool(getattr(getattr(glagol, "update_handler", None), "_c4_relay_hook", False))

    def streaming(self, station: str) -> bool:
        """The station is currently pointed at its Control4 room."""
        room = self.bindings.get(station)
        player = self.players.get(room) if room is not None else None
        st = self.hass.states.get(station)
        return (player is not None and st is not None and self.enabled(station)
                and st.attributes.get("source") == player.name)

    async def apply_volume_command(self, station: str, kind: str, value: float) -> None:
        room = self.bindings.get(station)
        try:
            if kind == "step":
                resp = await self.client.volume_step(room, int(value))
            else:
                resp = await self.client.volume_level(room, value)
        except RelayError as err:
            _LOGGER.warning("volume %s %s for room %s failed: %s", kind, value, room, err)
            return
        _LOGGER.debug("volume %s %s -> room %s: %s", kind, value, room, resp)
        if room in self.players and isinstance(resp.get("volume"), (int, float)):
            self.players[room].set_room_volume(resp["volume"])

    @callback
    def on_glagol_message(self, station: str, data: Any) -> None:
        """Voice volume commands: act on what Alice executed, not on the number.

        The directive usually arrives before the station reports its new
        volume, so the level seen before this message is the "before" value.
        """
        if not isinstance(data, dict):
            return
        state = data.get("state") or {}
        key = (state.get("aliceState"), state.get("volume"), state.get("playing"))
        if self._last_diag.get(station) != key:
            self._last_diag[station] = key
            _LOGGER.debug("%s aliceState=%s volume=%s playing=%s", station, *key)
        if isinstance(state.get("playing"), bool):
            self._playing[station] = state["playing"]
        # Diagnostic (debug log only): every message from Alice's wake-up until a
        # few seconds after she is idle again, to see how a spoken command shows up
        # when no vinsResponse comes (site 2026-10-01).
        if _LOGGER.isEnabledFor(logging.DEBUG):
            now = time.monotonic()
            if state.get("aliceState") not in (None, "IDLE"):
                self._dump_until[station] = now + 6
            if now < self._dump_until.get(station, 0):
                brief = {k: v for k, v in state.items() if k not in ("playerState", "hdmi")}
                extra = {k: v for k, v in data.items() if k not in ("state", "vinsResponse")}
                _LOGGER.debug("%s dialog msg state=%s extra=%s", station,
                              json.dumps(brief, ensure_ascii=False)[:1200],
                              json.dumps(extra, ensure_ascii=False)[:800])
        alice = state.get("aliceState")
        dialog = self._dialog.get(station)
        if alice is not None and alice != "IDLE" and dialog is None:
            dialog = self._dialog[station] = {"unmute": self.unmute_level(station), "handled": False}
        before = self._station_volume.get(station)
        vins = data.get("vinsResponse")
        if vins:
            directives = extract_directives(vins)
            _LOGGER.debug("%s directives=%s", station, json.dumps(directives, ensure_ascii=False))
            self.hass.bus.async_fire(EVENT_VINS, {"entity_id": station, "directives": directives})
            command = classify_volume_directives(directives, before)
            if command and self.streaming(station):
                _LOGGER.debug("%s: volume command %s (station was %s)", station, command, before)
                self.hass.async_create_task(self.apply_volume_command(station, *command))
                if dialog is not None:
                    dialog["handled"] = True
        if alice == "IDLE" and dialog is not None:
            # Alice is done: a spoken command shows only in the station volume.
            self._dialog.pop(station, None)
            if not dialog["handled"] and self.streaming(station):
                command = classify_dialog_volume(state.get("volume"), dialog["unmute"])
                if command:
                    _LOGGER.debug("%s: spoken volume command %s (volume %s, unmute %s)", station,
                                  command, state.get("volume"), dialog["unmute"])
                    self.hass.async_create_task(self.apply_volume_command(station, *command))
        if alice == "IDLE":
            # After AlexxIT has taken the level from this same message. On every
            # IDLE message: the station repeats "IDLE 0.1" after "громче", and a
            # reset done only once was undone again (site 2026-10-01).
            self.hass.loop.call_soon(self.keep_unmute_level, station)
        if isinstance(state.get("volume"), (int, float)):
            self._station_volume[station] = float(state["volume"])

    def unmute_level(self, station: str) -> float | None:
        """The level AlexxIT restores the muted station to (its volume_level)."""
        level = getattr(self._station_entity(station), "_attr_volume_level", None)
        return float(level) if isinstance(level, (int, float)) else None

    @callback
    def keep_unmute_level(self, station: str) -> None:
        """Keep AlexxIT's restore level at UNMUTE_LEVEL while the station streams.

        AlexxIT unmutes to its last non-zero volume, which after "громче" is
        0.1: the next "громче" (also 0.1) could not be seen, and Alice answered
        barely audibly.
        """
        ent = self._station_entity(station)
        level = getattr(ent, "_attr_volume_level", None)
        if ent is not None and self.streaming(station) and level != UNMUTE_LEVEL:
            ent._attr_volume_level = UNMUTE_LEVEL

    # --- alarms (AlexxIT alarm calendar -> driver schedule) ------------------
    def _calendar_of(self, station: str) -> tuple[str | None, bool]:
        """The station's AlexxIT alarm calendar: (entity_id, disabled)."""
        ent_reg = er.async_get(self.hass)
        st = ent_reg.async_get(station)
        if st is None or st.device_id is None:
            return None, False
        for e in er.async_entries_for_device(ent_reg, st.device_id, include_disabled_entities=True):
            if e.domain == "calendar" and e.platform == YANDEX_DOMAIN:
                return e.entity_id, e.disabled_by is not None
        return None, False

    def _calendar_entity(self, entity_id: str) -> Any:
        try:
            return self.hass.data["entity_components"]["calendar"].get_entity(entity_id)
        except (KeyError, AttributeError):
            return None

    async def sync_alarms(self) -> None:
        """Send every bound room its stations' alarms when they changed.

        AlexxIT's calendar keeps all alarms of a station in memory (one event
        each); its state only shows the next one. A disabled calendar means
        "no alarms" for that station, one not loaded yet is skipped.
        """
        per_room: dict[int, list[dict]] = {}
        for station, room in self.bindings.items():
            calendar, disabled = self._calendar_of(station)
            if calendar is None:
                continue
            if disabled:
                if station not in self._calendar_hint:
                    self._calendar_hint.add(station)
                    _LOGGER.info("Alarms of %s are not passed to Control4: enable %s", station, calendar)
                per_room.setdefault(room, [])
                continue
            ent = self._calendar_entity(calendar)
            if ent is None:
                continue
            alarms = per_room.setdefault(room, [])
            for event in getattr(ent, "events", None) or []:
                alarm = alarm_from_event(event)
                if alarm is not None:
                    alarms.append(alarm)
        for room, alarms in per_room.items():
            alarms = sorted(alarms, key=lambda a: a["id"])
            if self._alarms_sent.get(room) == alarms:
                continue
            try:
                resp = await self.client.alarms(room, alarms)
            except RelayError as err:
                _LOGGER.warning("alarms for room %s not sent: %s", room, err)
                continue
            self._alarms_sent[room] = alarms
            _LOGGER.info("Room %s: %d alarm(s) sent to Control4, next %s", room, len(alarms),
                         resp.get("next") or "none")

    # --- pairing and health (heartbeat) --------------------------------------
    async def pair(self) -> None:
        """Hand the driver our webhook, bound rooms and the volume settings copy."""
        paired = await self.client.pair(self.webhook_url, sorted(set(self.bindings.values())), self.volume_cfg)
        if paired.get("volume_restored"):
            _LOGGER.info("Volume settings restored into the Control4 driver")
        if isinstance(paired.get("volume_cfg"), dict):
            await self.save_volume_cfg(paired["volume_cfg"])
        self._alarms_sent.clear()   # a (re-)paired driver may have no alarms yet

    def station_health(self, station: str) -> dict[str, Any]:
        st = self.hass.states.get(station)
        player = self.players.get(self.bindings.get(station))
        calendar, disabled = self._calendar_of(station)
        return {
            "available": st is not None and st.state != "unavailable",
            "volume": "directives" if self.hooked(station) else "fallback",
            "source": (player is not None and st is not None
                       and player.name in (st.attributes.get("source_list") or [])),
            "calendar": "none" if calendar is None else ("off" if disabled else "on"),
        }

    def health_summary(self, health: dict[str, dict]) -> str:
        """One line for the driver's "Home Assistant" property."""
        rooms = []
        for station, h in health.items():
            name = self.rooms.get(self.bindings[station], str(self.bindings[station]))
            problems = []
            if not h["available"]:
                problems.append("station unavailable")
            else:
                if h["volume"] == "fallback":
                    problems.append("voice volume fallback")
                if not h["source"] and self.enabled(station):
                    problems.append("not a stream target")
            if h["calendar"] != "on":
                problems.append("no alarm calendar" if h["calendar"] == "none" else "alarm calendar off")
            if not self.enabled(station):
                problems.append("streaming off")
            rooms.append(f"{name}: " + (", ".join(problems) if problems else "ok"))
        head = f"c4_relay {self.versions.get(DOMAIN, '?')}, AlexxIT {self.versions.get(YANDEX_DOMAIN, '?')}"
        return "; ".join([head, *rooms])

    def _issue(self, key: str, active: bool, after: float, now: float,
               placeholders: dict[str, str] | None = None) -> None:
        """Raise a Repairs issue once a problem has lasted `after` s; clear it when gone."""
        issue_id = f"{self.entry.entry_id}_{key}".replace(".", "_")
        if not active:
            if self._since.pop(key, None) is not None:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        since = self._since.setdefault(key, now)
        if now - since >= after:
            ir.async_create_issue(self.hass, DOMAIN, issue_id, is_fixable=False,
                                  severity=ir.IssueSeverity.WARNING, translation_key=key.split(":")[0],
                                  translation_placeholders=placeholders or {})

    async def heartbeat(self, now: float | None = None) -> None:
        """Tell the driver we are alive; check what AlexxIT updates may break.

        A driver that answers paired=false (re-added with the old code) is
        paired again. A rejected code raises a Repairs issue at once and starts
        the reauth flow after REAUTH_AFTER, to leave time to paste the old code.
        """
        now = time.monotonic() if now is None else now
        health = {station: self.station_health(station) for station in self.bindings}
        for station, h in health.items():
            ph = {"station": station, "alexxit": self.versions.get(YANDEX_DOMAIN, "?")}
            self._issue(f"volume_fallback:{station}", h["available"] and h["volume"] == "fallback",
                        HEALTH_ISSUE_AFTER, now, ph)
            self._issue(f"source_missing:{station}", h["available"] and not h["source"] and self.enabled(station),
                        HEALTH_ISSUE_AFTER, now, ph)
        where = {"host": self.entry.data[CONF_HOST], "port": str(self.entry.data[CONF_PORT]),
                 "code": self.entry.data[CONF_PAIRING_CODE]}
        try:
            resp = await self.client.heartbeat(self.webhook_url, self.health_summary(health))
        except RelayAuthError:
            self._issue("driver_unreachable", False, 0, now)
            self._issue("pairing_rejected", True, 0, now, where)
            if now - self._since["pairing_rejected"] >= REAUTH_AFTER and not self._reauth_started:
                self._reauth_started = True
                self.entry.async_start_reauth(self.hass)
            return
        except RelayError as err:
            _LOGGER.debug("heartbeat failed: %s", err)
            self._issue("driver_unreachable", True, UNREACHABLE_ISSUE_AFTER, now, where)
            return
        self._issue("driver_unreachable", False, 0, now)
        self._issue("pairing_rejected", False, 0, now)
        if resp.get("version"):
            self.driver_version = resp["version"]
        if resp.get("paired") is False:
            _LOGGER.info("The Control4 driver is not paired with this HA (re-added?), pairing again")
            try:
                await self.pair()
            except RelayError as err:
                _LOGGER.warning("pairing again failed: %s", err)
                return
            await self.sync_alarms()

    async def save_volume_cfg(self, cfg: dict) -> None:
        if cfg and cfg != self.volume_cfg:
            self.volume_cfg = cfg
            await self.store.async_save({"volume_cfg": cfg})

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
        elif event == "volume_cfg" and isinstance(evt.get("volume_cfg"), dict):
            await self.save_volume_cfg(evt["volume_cfg"])
            return
        elif event == "alarm":
            self.hass.bus.async_fire(EVENT_ALARM, {
                "room_id": room, "room": self.rooms.get(room), "station": self.station_for_room(room),
                "phase": evt.get("phase"), "alarm_id": evt.get("alarm_id"), "time": evt.get("time"),
            })
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

    stored = await hub.store.async_load() or {}
    hub.volume_cfg = stored.get("volume_cfg") or {}
    for domain in (DOMAIN, YANDEX_DOMAIN):
        try:
            hub.versions[domain] = str((await async_get_integration(hass, domain)).version or "?")
        except Exception:  # not installed (tests) or broken manifest: only for the summary
            hub.versions[domain] = "?"

    hub.webhook_url = entry.data[CONF_HA_URL].rstrip("/") + webhook.async_generate_path(webhook_id)
    try:
        await hub.pair()
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
        await hub.sync_alarms()
        await hub.heartbeat()

    async def _heartbeat(_now: Any) -> None:
        await hub.heartbeat()

    entry.async_on_unload(async_track_time_interval(hass, _heartbeat, HEARTBEAT_INTERVAL))

    async def _sync_alarms(_now: Any) -> None:
        await hub.sync_alarms()

    entry.async_on_unload(async_track_time_interval(hass, _sync_alarms, ALARM_SYNC_INTERVAL))

    @callback
    def _check_later(_: Any = None) -> None:
        entry.async_on_unload(async_call_later(hass, SOURCE_CHECK_DELAY, _check_sources))

    @callback
    def _hook(_now: Any = None) -> None:
        hub.hook_stations()

    entry.async_on_unload(async_track_time_interval(hass, _hook, HOOK_CHECK_INTERVAL))
    _hook()

    # async_at_started: its unsubscribe stays valid after the start event fired
    # (a bare async_listen_once logged "Unable to remove unknown job listener"
    # on unload, site 2026-10-01).
    entry.async_on_unload(async_at_started(hass, _check_later))

    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_remove()
    for (domain, issue_id) in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(entry.entry_id):
            ir.async_delete_issue(hass, DOMAIN, issue_id)
