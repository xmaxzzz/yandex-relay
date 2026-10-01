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



@pytest.mark.parametrize("directives,before,expected", [
    ([{"name": "sound_set_level", "payload": {"new_level": 1}}], 0.0, ("step", 1)),     # "громче", muted
    ([{"name": "sound_set_level", "payload": {"new_level": 0}}], 0.0, ("step", -1)),    # "тише", muted
    ([{"name": "sound_set_level", "payload": {"new_level": 5}}], 0.0, ("absolute", 0.5)),
    ([{"name": "sound_set_level", "payload": {"new_level": 1}}], None, ("step", 1)),
    ([{"name": "sound_set_level", "payload": {"new_level": 5}}], 0.4, ("step", 1)),     # unmuted 4 -> 5
    ([{"name": "sound_set_level", "payload": {"new_level": 3}}], 0.4, ("step", -1)),
    ([{"name": "sound_set_level", "payload": {"new_level": 8}}], 0.4, ("absolute", 0.8)),
    ([{"name": "sound_set_level", "payload": {"new_percent_level": 70}}], 0.0, ("absolute", 0.7)),
    ([{"name": "sound_louder"}], 0.5, ("step", 1)),
    ([{"name": "sound_quiter"}], 0.5, ("step", -1)),
    ([{"name": "tts_play_placeholder", "payload": {}}, {"name": "audio_play"}], 0.0, None),
    ([], 0.0, None),
])
def test_classify_volume_directives(directives, before, expected):
    assert helpers.classify_volume_directives(directives, before) == expected


class _Ev:
    def __init__(self, start, summary="Будильник", uid="a1", rrule=None):
        self.start, self.summary, self.uid, self.rrule = start, summary, uid, rrule


def test_alarm_from_event():
    from datetime import date, datetime, timedelta, timezone
    tz = timezone(timedelta(hours=3))
    start = datetime(2026, 9, 29, 7, 30, tzinfo=tz)
    assert helpers.alarm_from_event(_Ev(start, rrule="FREQ=WEEKLY;BYDAY=FR,MO,TU,WE,TH")) == {
        "id": "a1", "time": "07:30", "enabled": True, "days": [1, 2, 3, 4, 5]}
    assert helpers.alarm_from_event(_Ev(start, summary="Выключен", uid="b")) == {
        "id": "b", "time": "07:30", "enabled": False, "date": "2026-09-29"}
    assert helpers.alarm_from_event(_Ev(start, rrule="FREQ=WEEKLY")) is None
    assert helpers.alarm_from_event(_Ev(date(2026, 9, 29))) is None      # all-day: not an alarm


@pytest.mark.parametrize("final,unmute,expected", [
    (0.1, 0.4, ("step", 1)),        # "громче" from the muted 0
    (0.0, 0.4, ("step", -1)),       # "тише"
    (0.5, 0.4, ("absolute", 0.5)),  # "громкость 5"
    (0.4, 0.4, None),               # "сколько времени": the unmute level stays
    (0.1, 0.1, None),               # ambiguous when unmute is 0.1 (kept at 0.4)
    (None, 0.4, None),
    (0.3, None, None),
])
def test_classify_dialog_volume(final, unmute, expected):
    assert helpers.classify_dialog_volume(final, unmute) == expected
