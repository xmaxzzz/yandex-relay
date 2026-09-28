"""Pure helpers: no Home Assistant needed (loaded by file path)."""

import base64
import importlib.util
import json
import os

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "custom_components", "c4_relay", "helpers.py")
_spec = importlib.util.spec_from_file_location("c4_relay_helpers", _PATH)
helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helpers)


def proxy_url(payload: dict, ext: str = "mp3") -> str:
    """An AlexxIT-style proxy URL (core/stream.py get_url): JWT then .ext."""
    b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    token = f"{b64({'alg': 'HS256', 'typ': 'JWT'})}.{b64(payload)}.c2lnbmF0dXJl"
    return f"http://192.0.2.20:8123/api/yandex_station/{token}.{ext}"


def test_direct_url_extracted():
    url = "https://s123.storage.yandex.net/get-mp3/abc/def/track.mp3?x=1"
    assert helpers.direct_url_from_proxy(proxy_url({"url": url, "exp": 1})) == url


@pytest.mark.parametrize("url", [
    "https://cdn.example/track.mp3",                          # not a proxy URL
    proxy_url({"url": "/local/file.mp3"}),                    # relative: HA-served file
    proxy_url({"nourl": 1}),
    "http://ha:8123/api/yandex_station/garbage.mp3",
    "http://ha:8123/api/yandex_station/a.b.c.d.mp3",
    None,
])
def test_direct_url_falls_back(url):
    assert helpers.direct_url_from_proxy(url) is None


ROOMS = [{"id": 21, "name": "Офис"}, {"id": 22, "name": "Гостиная"},
         {"id": 23, "name": "Гостиная 2"}, {"id": 30, "name": "Детская Ёлка"}]


@pytest.mark.parametrize("cands,expected", [
    (["Офис", "Яндекс Станция"], 21),                  # area name, exact
    ([None, "Станция Гостиная"], 22),                  # contained in the name
    ([None, "Мини Гостиная 2"], 23),                   # longest match wins
    (["детская елка"], 30),                            # case and ё/е
    ([None, "Станция Кухня"], None),
    ([None, "Гостинаяx"], None),                       # whole words only
])
def test_suggest_room(cands, expected):
    assert helpers.suggest_room(cands, ROOMS) == expected


def test_transport_mapping():
    f = helpers.station_calls_for_transport
    assert f("next", None) == [("media_next_track", {})]
    assert f("prev", None) == [("media_previous_track", {})]
    assert f("play", "in_place") == [("media_play", {})]
    assert f("play", "restart") == [("media_play", {}), ("media_seek", {"seek_position": 0})]
    assert f("pause", None) == [("media_pause", {})]
    assert f("stop", None) == [("media_pause", {})]
    assert f("off", None) == []


def test_room_stop_pauses_station():
    f = helpers.room_stop_pauses_station
    assert f("deselected", None)
    assert f("state", "stopped") and f("state", "paused")
    assert not f("state", "ended")          # natural end: station moves on
    assert not f("state", "playing")
    assert not f("transport", None)


def test_extract_directives_anywhere():
    vins = {"response": {"directives": [{"name": "sound_louder", "payload": {}}],
                         "card": {"text": "Громче"}},
            "payload": {"response": {"directives": [{"name": "sound_set_level", "payload": {"new_level": 5}}]}}}
    names = [d["name"] for d in helpers.extract_directives(vins)]
    assert sorted(names) == ["sound_louder", "sound_set_level"]
    assert helpers.extract_directives({"response": {"card": {}}}) == []
    assert helpers.extract_directives(None) == []
