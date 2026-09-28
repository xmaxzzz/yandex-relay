"""Pure helpers without Home Assistant imports (unit-tested on their own)."""

from __future__ import annotations

import base64
import json
import re
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


def room_stop_pauses_station(event: str, state: str | None) -> bool:
    """Whether a driver event means the room stopped playing for the user.

    Room switched off, another source picked, stream stopped: the station has to
    stop too, or it keeps playing silently. A natural end of a track does not
    count, the station moves on to the next one by itself.
    """
    if event == "deselected":
        return True
    return event == "state" and state in ("paused", "stopped")
