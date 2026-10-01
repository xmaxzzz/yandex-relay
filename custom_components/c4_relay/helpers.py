"""Pure helpers without Home Assistant imports (unit-tested on their own)."""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse


def direct_url_from_proxy(url: str) -> str | None:
    """Return the CDN URL hidden in an AlexxIT stream proxy URL.

    AlexxIT hands players http://<ha>/api/yandex_station/<jwt>.<ext>; the JWT
    payload (signed, not encrypted) holds {"url": <direct link>, "exp": ...}.
    Anything else, or a payload without an absolute http(s) URL, gives None and
    the caller falls back to the proxy URL.
    """
    if not isinstance(url, str):
        return None
    try:
        path = urlparse(url).path
    except ValueError:
        return None
    if "/api/yandex_station/" not in path:
        return None
    parts = path.rsplit("/", 1)[-1].split(".")
    if len(parts) != 4:  # header.payload.signature.ext
        return None
    payload = parts[1]
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        direct = json.loads(raw).get("url")
    except (ValueError, AttributeError):
        return None
    if isinstance(direct, str) and direct.startswith(("http://", "https://")):
        return direct
    return None


def normalize(name: str | None) -> str:
    """Lower-case, ё→е, letters and digits only (for matching names)."""
    s = (name or "").lower().replace("ё", "е")
    return re.sub(r"[^\w]+", " ", s).strip()


def suggest_room(candidates: list[str | None], rooms: list[dict]) -> int | None:
    """Pick the C4 room for a station.

    candidates: the station's area name first, then its names. An exact
    normalized match wins; otherwise the longest room name contained in a
    candidate (so "Гостиная" matches "Станция Гостиная", not "Гостиная 2").
    """
    norm_rooms = [(normalize(r["name"]), int(r["id"])) for r in rooms if r.get("name")]
    norm_cands = [normalize(c) for c in candidates if c]
    for cand in norm_cands:
        for name, rid in norm_rooms:
            if name and name == cand:
                return rid
    best: tuple[int, int] | None = None
    for cand in norm_cands:
        padded = f" {cand} "
        for name, rid in norm_rooms:
            if name and f" {name} " in padded and (best is None or len(name) > best[0]):
                best = (len(name), rid)
    return best[1] if best else None


# Relay room states -> Home Assistant media player states.
RELAY_TO_HA_STATE = {
    "starting": "playing",
    "playing": "playing",
    "paused": "paused",
    "stopped": "idle",
    "ended": "idle",
    "idle": "idle",
}


def station_calls_for_transport(action: str, resume: str | None) -> list[tuple[str, dict]]:
    """Station service calls for a panel press reported by the driver.

    The station is the queue master: panel buttons are forwarded to it and the
    resulting track/state comes back through AlexxIT. After a restart of the
    track in C4 the station is seeked to 0 so both play the same part.
    """
    if action == "next":
        return [("media_next_track", {})]
    if action == "prev":
        return [("media_previous_track", {})]
    if action == "play":
        calls = [("media_play", {})]
        if resume == "restart":
            calls.append(("media_seek", {"seek_position": 0}))
        return calls
    if action in ("pause", "stop"):
        return [("media_pause", {})]
    return []


def extract_directives(vins: object) -> list[dict]:
    """All VINS directives in a Glagol vinsResponse, wherever they are nested.

    The layout differs between devices (Yandex Module wraps it in "payload"),
    so every list called "directives" is collected: [{"name": .., "payload": ..}].
    """
    found: list[dict] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "directives" and isinstance(value, list):
                    for d in value:
                        if isinstance(d, dict) and d.get("name"):
                            found.append({"name": d.get("name"), "payload": d.get("payload")})
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(vins)
    return found


def classify_volume_directives(directives: list[dict], before: float | None) -> tuple[str, float] | None:
    """What a voice volume command meant, from the VINS directive Alice sent.

    Alice turns "громче"/"тише" into an absolute sound_set_level computed from
    the station's own volume. While AlexxIT streams, the station is muted (0),
    so "громче" arrives as level 1 and "тише" as 0 (site log 2026-09-28):
      * before is 0 (or unknown): level 1 -> step up, 0 -> step down, else absolute;
      * station not muted: +-1 from its level -> step, anything else absolute.
    Explicit "громкость 1"/"громкость 0" on a muted station read as a step.
    Returns ("step", +1|-1) or ("absolute", 0..1), None if not a volume command.
    """
    for d in directives:
        name, payload = d.get("name"), d.get("payload") or {}
        if name == "sound_louder":
            return ("step", 1)
        if name == "sound_quiter":
            return ("step", -1)
        if name != "sound_set_level":
            continue
        new = payload.get("new_level")
        if new is None and payload.get("new_percent_level") is not None:
            new = round(float(payload["new_percent_level"]) / 10)
        if new is None:
            return None
        new = int(round(float(new)))
        prev = None if before is None else int(round(float(before) * 10))
        if not prev:
            if new == 1:
                return ("step", 1)
            if new == 0:
                return ("step", -1)
        elif new - prev in (1, -1):
            return ("step", new - prev)
        return ("absolute", max(0.0, min(1.0, new / 10)))
    return None


RRULE_DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
ALARM_OFF_SUMMARY = "Выключен"   # AlexxIT calendar: summary of a disabled alarm


def alarm_from_event(event: Any) -> dict | None:
    """One AlexxIT alarm-calendar event (one station alarm) as a driver alarm.

    AlexxIT keeps one event per alarm (calendar.py alarm_to_event): start is the
    next ring (or the last one of a disabled repeating alarm), rrule
    "FREQ=WEEKLY;BYDAY=MO,TU,..." for repeating alarms, summary "Выключен" when
    the alarm is off, uid = alarm_id. The time is the station's local time.
    Returns {id, time "HH:MM", enabled, days [1..7, Mon = 1] | date "YYYY-MM-DD"}.
    """
    start = getattr(event, "start", None)
    if not isinstance(start, datetime):
        return None
    alarm: dict[str, Any] = {
        "id": str(getattr(event, "uid", None) or start.isoformat()),
        "time": start.strftime("%H:%M"),
        "enabled": getattr(event, "summary", "") != ALARM_OFF_SUMMARY,
    }
    rrule = getattr(event, "rrule", None)
    if rrule:
        parts = dict(p.split("=", 1) for p in str(rrule).split(";") if "=" in p)
        days = sorted({RRULE_DAYS.index(d) + 1 for d in parts.get("BYDAY", "").split(",") if d in RRULE_DAYS})
        if not days:
            return None
        alarm["days"] = days
    else:
        alarm["date"] = start.strftime("%Y-%m-%d")
    return alarm


def classify_dialog_volume(final: float | None, unmute: float | None) -> tuple[str, float] | None:
    """A spoken volume command, read from the station volume when Alice is done.

    Since 2026-10 spoken "громче/тише/громкость N" bring no vinsResponse (site
    2026-10-01; text commands sent from HA still do). The station still runs
    them, computed from its muted 0: "громче" leaves 0.1, "тише" 0.0,
    "громкость N" N/10. Any other request leaves the level AlexxIT unmuted it
    to for the dialog (`unmute`). So: the first IDLE volume differs from
    `unmute` -> a volume command; 1 -> step up, 0 -> step down, else absolute.
    `unmute` must not be 0.0/0.1 (c4_relay keeps it at UNMUTE_LEVEL).
    """
    if not isinstance(final, (int, float)) or not isinstance(unmute, (int, float)):
        return None
    if abs(final - unmute) < 0.05:
        return None
    level = int(round(final * 10))
    if level == 1:
        return ("step", 1)
    if level == 0:
        return ("step", -1)
    return ("absolute", max(0.0, min(1.0, level / 10)))


def room_stop_pauses_station(event: str, state: str | None) -> bool:
    """Whether a driver event means the room stopped playing for the user.

    Room switched off, another source picked, stream stopped: the station has to
    stop too, or it keeps playing silently. A natural end of a track does not
    count, the station moves on to the next one by itself.
    """
    if event == "deselected":
        return True
    return event == "state" and state in ("paused", "stopped")
