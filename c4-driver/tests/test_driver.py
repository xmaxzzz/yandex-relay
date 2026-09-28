"""Behaviour tests for driver.lua on LuaJIT (same Lua 5.1 dialect as DriverWorks jit=1).

The Control4 API is replaced by recording stubs (c4_stubs.lua). Each test boots
a fresh driver, drives it through the proxy and HTTP entry points, and checks
what it sent back. Run: python c4-driver/tests/test_driver.py
Requires: pip install lupa
"""

import json
import os
import sys
import unittest

from lupa import luajit21 as lupa_rt

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVER = os.path.join(HERE, "..", "driver.lua")
STUBS = os.path.join(HERE, "c4_stubs.lua")

ROOMS_XML = (
    "<item><id>1</id><name>Home</name><type>2</type><itemdata/><subitems>"
    "<item><id>2</id><name>Дом</name><type>3</type><itemdata/><subitems>"
    "<item><id>3</id><name>1 этаж</name><type>4</type><itemdata/><subitems>"
    "<item><id>12</id><name>Гостиная &amp; кухня</name><type>8</type><itemdata/></item>"
    "<item><id>13</id><name>Спальня</name><type>8</type><itemdata/></item>"
    "</subitems></item></subitems></item></subitems></item>"
)


class Driver:
    """One booted driver instance plus helpers to talk to it."""

    def __init__(self, project_xml=ROOMS_XML):
        self.lua = lupa_rt.LuaRuntime(unpack_returned_tuples=True)
        g = self.lua.globals()
        g.YANDEX_RELAY_TEST = self.lua.table()
        g.PROJECT_XML = project_xml
        with open(STUBS, encoding="utf-8") as f:
            self.lua.execute(f.read())
        with open(DRIVER, encoding="utf-8") as f:
            self.lua.execute(f.read())
        self.g = g
        g.OnDriverInit()
        g.OnDriverLateInit()
        self.handle = 100

    # --- stub state ---------------------------------------------------------
    def calls(self, name):
        rec = self.g.REC[name]
        return [rec[i] for i in range(1, len(rec) + 1)] if rec else []

    def clear(self):
        self.lua.execute("for k in pairs(REC) do REC[k] = {} end")

    def prop(self, name):
        return self.g.PROPS[name]

    def proxy_cmds(self, cmd):
        return [c for c in self.calls("SendToProxy") if c["cmd"] == cmd]

    def webhooks(self, event=None):
        out = [json.loads(c["body"]) for c in self.calls("urlPost")]
        return [w for w in out if event is None or w["event"] == event]

    def set_time(self, t):
        self.g.FAKE_NOW = t

    def fire_timers(self):
        self.lua.execute("FireTimers()")

    # --- entry points -------------------------------------------------------
    def proxy(self, cmd, **params):
        t = self.lua.table_from({k: str(v) for k, v in params.items()})
        return self.g.ReceivedFromProxy(5001, cmd, t)

    def http_raw(self, *chunks):
        self.handle += 1
        h = self.handle
        self.g.OnServerConnectionStatusChanged(h, 18765, "ONLINE", "10.0.0.5")
        for chunk in chunks:
            self.g.OnServerDataIn(h, chunk, "10.0.0.5", "50000")
        sent = [c["data"] for c in self.calls("ServerSend") if c["h"] == h]
        if not sent:
            return None, None
        head, _, body = sent[-1].partition("\r\n\r\n")
        return int(head.split(" ")[1]), json.loads(body)

    def http(self, method, path, body=None, key="__code__"):
        if key == "__code__":
            key = self.prop("Pairing Code")
        payload = json.dumps(body) if body is not None else ""  # ensure_ascii like aiohttp
        head = f"{method} {path} HTTP/1.1\r\nHost: c4\r\nContent-Length: {len(payload.encode())}\r\n"
        if key is not None:
            head += f"X-Relay-Key: {key}\r\n"
        return self.http_raw(head + "\r\n" + payload)

    def room(self, rid):
        return self.g.YANDEX_RELAY_TEST.rooms[rid]


def other_timers(d):
    """Active timers except the always-running alarm (15 s) and HA watch (30 s) ticks."""
    ts = d.g.TIMERS_ACTIVE()
    return [ts[i] for i in range(1, len(ts) + 1) if ts[i].ms not in (15000, 30000)]


def play_body(key="t1", url="https://cdn/t1.mp3", fallback="http://ha:8123/api/yandex_station/x.mp3"):
    return {"room_id": 12, "url": url, "fallback_url": fallback, "title": "Тест",
            "artist": "Queen", "album": "A", "image": "https://img/1.jpg",
            "duration_ms": 180000, "key": key}


class Boot(unittest.TestCase):
    def test_late_init(self):
        d = Driver()
        code = d.prop("Pairing Code")
        self.assertEqual(len(code), 8)
        self.assertEqual(d.calls("CreateServer")[0]["port"], 18765)
        self.assertIn("2: Гостиная & кухня, Спальня", d.prop("Rooms Found"))
        self.assertEqual(d.prop("Driver Version"), "0.5.0")
        self.assertEqual(d.g.PERSIST["pairing_code"], code)
        self.assertEqual(d.calls("RegisterVariableListener")[0]["var"], 1009)

    def test_code_survives_restart(self):
        d = Driver()
        code = d.prop("Pairing Code")
        d.g.OnDriverLateInit()
        self.assertEqual(d.prop("Pairing Code"), code)

    def test_no_rooms_falls_back_to_driver_room(self):
        d = Driver(project_xml="<item><id>1</id><name>Home</name><type>2</type></item>")
        self.assertIn("1: Room 77", d.prop("Rooms Found"))


class Http(unittest.TestCase):
    def setUp(self):
        self.d = Driver()

    def test_rejects_without_key(self):
        self.assertEqual(self.d.http("GET", "/info", key=None)[0], 401)
        self.assertEqual(self.d.http("GET", "/info", key="WRONG")[0], 401)

    def test_info(self):
        code, body = self.d.http("GET", "/info")
        self.assertEqual(code, 200)
        self.assertEqual(body["version"], "0.5.0")
        self.assertFalse(body["paired"])

    def test_split_packets(self):
        payload = json.dumps({"webhook_url": "http://ha:8123/api/webhook/abc"})
        head = (f"POST /pair HTTP/1.1\r\nContent-Length: {len(payload)}\r\n"
                f"X-Relay-Key: {self.d.prop('Pairing Code')}\r\n\r\n")
        code, body = self.d.http_raw(head[:10], head[10:], payload[:5], payload[5:])
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])

    def test_pair(self):
        code, _ = self.d.http("POST", "/pair", {"webhook_url": "http://ha:8123/api/webhook/abc"})
        self.assertEqual(code, 200)
        self.assertEqual(self.d.prop("Paired With"), "ha:8123")
        self.assertEqual(self.d.g.PERSIST["webhook"], "http://ha:8123/api/webhook/abc")
        self.assertEqual(self.d.webhooks("hello")[0]["driver_version"], "0.5.0")
        self.assertEqual(self.d.calls("urlPost")[0]["url"], "http://ha:8123/api/webhook/abc")

    def test_pair_rejects_bad_url(self):
        self.assertEqual(self.d.http("POST", "/pair", {"webhook_url": "ftp://x"})[0], 400)

    def test_rooms(self):
        code, body = self.d.http("GET", "/rooms")
        self.assertEqual(code, 200)
        self.assertEqual(body["rooms"], [{"id": 12, "name": "Гостиная & кухня"}, {"id": 13, "name": "Спальня"}])

    def test_errors(self):
        self.assertEqual(self.d.http("GET", "/nope")[0], 404)
        self.assertEqual(self.d.http_raw("POST /play HTTP/1.1\r\nContent-Length: 3\r\n"
                                         f"X-Relay-Key: {self.d.prop('Pairing Code')}\r\n\r\n{{{{{{")[0], 400)
        self.assertEqual(self.d.http_raw("POST /play HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n")[0], 411)
        self.assertEqual(self.d.http_raw("X" * 70000)[0], 413)
        self.assertEqual(self.d.http("POST", "/play", {"room_id": 12})[0], 400)

    def test_connection_closed_after_response(self):
        self.d.http("GET", "/info")
        self.d.fire_timers()
        self.assertEqual(len(self.d.calls("ServerCloseClient")), 1)


class Playback(unittest.TestCase):
    def setUp(self):
        self.d = Driver()
        self.d.http("POST", "/pair", {"webhook_url": "http://ha:8123/api/webhook/abc"})
        self.d.set_time(1000)
        self.d.clear()

    def start(self, key="t1", **kw):
        code, body = self.d.http("POST", "/play", play_body(key=key, **kw))
        self.assertEqual(code, 200)
        self.d.proxy("INTERNET_RADIO_SELECTED", QUEUE_ID=501, ROOM_ID=12, QUEUE_INFO=key,
                     STATION_URL=kw.get("url", "https://cdn/t1.mp3"))
        return body

    def test_play_selects_stream_with_metadata(self):
        body = self.start()
        self.assertEqual(body["state"], "starting")
        sel = self.d.proxy_cmds("SELECT_INTERNET_RADIO")[0]["params"]
        self.assertEqual(sel["ROOM_ID"], "12")
        self.assertEqual(sel["STATION_URL"], "https://cdn/t1.mp3")
        self.assertEqual(sel["QUEUE_INFO"], "t1")
        self.assertEqual(self.d.room(12)["state"], "playing")
        info = self.d.proxy_cmds("UPDATE_MEDIA_INFO")[0]["params"]
        self.assertEqual(info["LINE1"], "Тест")   # \u-escaped JSON decoded to UTF-8
        self.assertEqual(info["ROOMID"], "12")
        ev = [c["params"] for c in self.d.proxy_cmds("SEND_EVENT") if c["params"]["NAME"] == "QueueChanged"][0]
        self.assertEqual(ev["ROOMS"], "12")
        self.assertIn("<title>Тест</title>", ev["EVTARGS"])
        self.assertEqual(self.d.webhooks("state")[-1]["state"], "playing")

    def test_meta_reasserted_then_timer_stops(self):
        self.start()
        n0 = len(self.d.proxy_cmds("UPDATE_MEDIA_INFO"))
        for _ in range(10):
            self.d.fire_timers()
        self.assertEqual(len(self.d.proxy_cmds("UPDATE_MEDIA_INFO")) - n0, 8 * 3)
        self.assertEqual(len(other_timers(self.d)), 0)

    def test_fallback_on_early_failure(self):
        self.start()
        self.d.set_time(1003)
        self.d.proxy("QUEUE_STATE_CHANGED", QUEUE_ID=501, STATE="STOP", QUEUE_INFO="t1")
        sels = self.d.proxy_cmds("SELECT_INTERNET_RADIO")
        self.assertEqual(sels[-1]["params"]["STATION_URL"], "http://ha:8123/api/yandex_station/x.mp3")
        # late END of the dead direct stream must not mark the track ended
        self.d.proxy("QUEUE_STATE_CHANGED", QUEUE_ID=501, STATE="END", QUEUE_INFO="t1")
        self.assertEqual(self.d.room(12)["state"], "starting")
        self.d.proxy("INTERNET_RADIO_SELECTED", QUEUE_ID=502, ROOM_ID=12, QUEUE_INFO="t1")
        code, st = self.d.http("GET", "/state?room_id=12")
        self.assertEqual((st["state"], st["source"]), ("playing", "fallback"))

    def test_no_second_fallback(self):
        self.start()
        self.d.set_time(1002)
        self.d.proxy("QUEUE_STATE_CHANGED", QUEUE_ID=501, STATE="STOP", QUEUE_INFO="t1")
        self.d.proxy("INTERNET_RADIO_SELECTED", QUEUE_ID=502, ROOM_ID=12, QUEUE_INFO="t1")
        self.d.set_time(1004)
        self.d.proxy("QUEUE_STATE_CHANGED", QUEUE_ID=502, STATE="STOP", QUEUE_INFO="t1")
        self.assertEqual(len(self.d.proxy_cmds("SELECT_INTERNET_RADIO")), 2)
        self.assertEqual(self.d.room(12)["state"], "stopped")

    def test_natural_end_does_not_advance(self):
        self.start()
        self.d.set_time(1200)
        self.d.proxy("QUEUE_STATE_CHANGED", QUEUE_ID=501, STATE="END", QUEUE_INFO="t1")
        self.assertEqual(self.d.room(12)["state"], "ended")
        self.assertEqual(len(self.d.proxy_cmds("SELECT_INTERNET_RADIO")), 1)
        self.assertEqual(self.d.webhooks("state")[-1]["state"], "ended")

    def test_new_track_ignores_old_track_events(self):
        self.start("t1")
        self.d.set_time(1100)
        self.d.http("POST", "/play", play_body(key="t2", url="https://cdn/t2.mp3"))
        self.d.proxy("QUEUE_STATE_CHANGED", QUEUE_ID=501, STATE="END", QUEUE_INFO="t1")
        self.assertEqual(self.d.room(12)["state"], "starting")
        self.d.proxy("INTERNET_RADIO_SELECTED", QUEUE_ID=501, ROOM_ID=12, QUEUE_INFO="t2")
        self.assertEqual(self.d.room(12)["state"], "playing")
        self.assertEqual(len(self.d.proxy_cmds("SELECT_INTERNET_RADIO")), 2)   # no fallback fired

    def test_ha_pause_is_not_echoed(self):
        self.start()
        self.d.set_time(1100)
        self.d.http("POST", "/pause", {"room_id": 12})
        dev = self.d.calls("SendToDevice")[0]
        # Site 2026-09-28: digital audio PAUSE left the room sounding; room off silences it.
        self.assertEqual((dev["id"], dev["cmd"]), (12, "ROOM_OFF"))
        ret = self.d.proxy("PAUSE", ROOM_ID=12)          # if the room reports it back
        self.assertIn("<handled>false</handled>", ret)
        self.assertIsNone(self.d.proxy("OFF"))            # OFF {} after our ROOM_OFF
        self.assertEqual(self.d.webhooks("transport"), [])
        self.d.proxy("QUEUE_STATE_CHANGED", QUEUE_ID=501, STATE="STOP", QUEUE_INFO="t1")
        self.assertEqual(self.d.room(12)["state"], "paused")
        self.d.proxy("QUEUE_DELETED", QUEUE_ID=501)
        self.assertEqual(self.d.webhooks("deselected"), [])

    def test_resume_restarts_track(self):
        self.start()
        self.d.set_time(1100)
        self.d.http("POST", "/pause", {"room_id": 12})
        self.d.proxy("PAUSE", ROOM_ID=12)
        self.d.proxy("QUEUE_STATE_CHANGED", QUEUE_ID=501, STATE="STOP", QUEUE_INFO="t1")
        self.d.proxy("QUEUE_DELETED", QUEUE_ID=501)       # queue gone: nothing to resume in place
        code, st = self.d.http("POST", "/resume", {"room_id": 12})
        self.assertEqual((code, st["state"], st["resume"]), (200, "starting", "restart"))
        self.assertEqual(self.d.proxy_cmds("SELECT_INTERNET_RADIO")[-1]["params"]["STATION_URL"], "https://cdn/t1.mp3")

    def test_ha_resume_in_place(self):
        self.start()
        self.d.set_time(1100)
        self.d.http("POST", "/pause", {"room_id": 12})
        self.d.proxy("PAUSE", ROOM_ID=12)
        self.d.proxy("QUEUE_STATE_CHANGED", QUEUE_ID=501, STATE="PAUSE", QUEUE_INFO="t1")
        self.d.clear()
        code, st = self.d.http("POST", "/resume", {"room_id": 12})
        self.assertEqual((code, st["resume"]), (200, "in_place"))
        self.assertEqual([(c["id"], c["cmd"]) for c in self.d.calls("SendToDevice")], [(12, "PLAY")])
        self.assertEqual(self.d.proxy_cmds("SELECT_INTERNET_RADIO"), [])
        self.assertIn("<handled>false</handled>", self.d.proxy("PLAY", ROOM_ID=12))
        self.assertEqual(self.d.webhooks("transport"), [])   # own PLAY is not echoed
        self.d.proxy("QUEUE_STATE_CHANGED", QUEUE_ID=501, STATE="PLAY", QUEUE_INFO="t1")
        self.assertEqual(self.d.room(12)["state"], "playing")

    def test_resume_without_track(self):
        self.assertEqual(self.d.http("POST", "/resume", {"room_id": 13})[0], 409)

    def test_panel_transport_goes_to_ha(self):
        self.start()
        self.assertIn("<handled>true</handled>", self.d.proxy("SKIP_FWD", ROOM_ID=12))
        self.assertIn("<handled>true</handled>", self.d.proxy("SKIP_REV", ROOM_ID=12))
        self.assertIn("<handled>false</handled>", self.d.proxy("PAUSE", ROOM_ID=12))
        self.assertIn("<handled>true</handled>", self.d.proxy("PLAY", ROOMID=12, NAVID="nav", SEQ=9))
        acts = [(w["room_id"], w["action"]) for w in self.d.webhooks("transport")]
        self.assertEqual(acts, [(12, "next"), (12, "prev"), (12, "pause"), (12, "play")])

    def test_panel_play_restarts_last_track(self):
        # Site test 2026-09-28: navigator PLAY arrives with ROOMID/NAVID/SEQ
        self.start()
        self.d.set_time(1100)
        self.d.proxy("STOP", ROOM_ID=12)
        self.d.proxy("QUEUE_STATE_CHANGED", QUEUE_ID=501, STATE="STOP", QUEUE_INFO="t1")
        self.d.proxy("QUEUE_DELETED", QUEUE_ID=501)
        ret = self.d.proxy("PLAY", ROOMID=12, NAVID="nav", SEQ=2)
        self.assertIn("<handled>true</handled>", ret)
        sels = self.d.proxy_cmds("SELECT_INTERNET_RADIO")
        self.assertEqual(len(sels), 2)
        self.assertEqual(sels[-1]["params"]["STATION_URL"], "https://cdn/t1.mp3")
        self.assertEqual(self.d.webhooks("transport")[-1]["action"], "play")
        self.assertEqual(self.d.webhooks("transport")[-1]["resume"], "restart")
        # HA answers with /resume once the station resumes: no double start
        self.d.http("POST", "/resume", {"room_id": 12})
        self.assertEqual(len(self.d.proxy_cmds("SELECT_INTERNET_RADIO")), 2)

    def test_panel_play_after_pause_resumes_in_place(self):
        # Site 2026-09-28: Pause via the room leaves the queue in a real STATE=PAUSE.
        self.start()
        self.d.set_time(1100)
        self.d.proxy("PAUSE", ROOM_ID=12)
        self.d.proxy("QUEUE_STATE_CHANGED", QUEUE_ID=501, STATE="PAUSE", QUEUE_INFO="t1")
        self.assertEqual(self.d.room(12)["state"], "paused")
        self.d.clear()
        self.assertIn("<handled>true</handled>", self.d.proxy("PLAY", ROOMID=12, NAVID="nav", SEQ=5))
        self.assertEqual([(c["id"], c["cmd"]) for c in self.d.calls("SendToDevice")], [(12, "PLAY")])
        self.assertEqual(self.d.proxy_cmds("SELECT_INTERNET_RADIO"), [])
        self.assertEqual(self.d.webhooks("transport")[-1]["resume"], "in_place")
        self.assertIn("<handled>false</handled>", self.d.proxy("PLAY", ROOM_ID=12))  # comes back
        self.assertEqual(len(self.d.webhooks("transport")), 1)

    def test_room_play_notification_never_restarts(self):
        self.start()
        self.d.set_time(1100)
        self.d.proxy("PAUSE", ROOM_ID=12)
        self.d.proxy("QUEUE_STATE_CHANGED", QUEUE_ID=501, STATE="PAUSE", QUEUE_INFO="t1")
        self.assertIn("<handled>false</handled>", self.d.proxy("PLAY", ROOM_ID=12))
        self.assertEqual(len(self.d.proxy_cmds("SELECT_INTERNET_RADIO")), 1)
        self.assertEqual(self.d.webhooks("transport")[-1]["resume"], "in_place")

    def test_queue_info_changed_is_quiet(self):
        self.assertIsNone(self.d.proxy("QUEUE_INFO_CHANGED", QUEUE_ID=501, QUEUE_STATE="PAUSE"))

    def test_panel_play_without_track_only_notifies(self):
        ret = self.d.proxy("PLAY", ROOMID=13, NAVID="nav", SEQ=2)
        self.assertIn("<handled>true</handled>", ret)
        self.assertEqual(self.d.proxy_cmds("SELECT_INTERNET_RADIO"), [])
        self.assertEqual(self.d.webhooks("transport")[-1]["room_id"], 13)

    def test_select_source_is_quiet(self):
        self.assertIsNone(self.d.proxy("SELECT_SOURCE", MEDIA_ID=0, PATH_TYPE=2, ROOM_ID=12))

    def test_dashboard_pause_stop_are_room_buttons(self):
        # Site 2026-09-28: PROTOCOL Pause/Stop never stopped the stream; TuneIn uses ROOM.
        with open(os.path.join(HERE, "..", "driver.xml"), encoding="utf-8") as f:
            xml = f.read()
        for name, kind in (("PLAY", "PROTOCOL"), ("PAUSE", "ROOM"), ("STOP", "ROOM"),
                           ("SKIP_FWD", "PROTOCOL"), ("SKIP_REV", "PROTOCOL")):
            self.assertRegex(xml, rf"<Name>{name}</Name>\s*<Type>{kind}</Type>", name)

    def test_digital_audio_connections_autobind(self):
        # Site 2026-09-28: without autobind the connections had to be made by hand.
        with open(os.path.join(HERE, "..", "driver.xml"), encoding="utf-8") as f:
            xml = f.read()
        for cls in ("DIGITAL_AUDIO_SERVER", "DIGITAL_AUDIO_CLIENT"):
            self.assertRegex(xml, rf"<autobind>True</autobind>\s*<classname>{cls}</classname>", cls)
        self.assertNotIn("AUDIO_SELECTION", xml)

    def test_room_notification_pause_lets_digital_audio_handle(self):
        self.start()
        self.assertIn("<handled>false</handled>", self.d.proxy("PAUSE", ROOM_ID=12))
        self.assertIn("<handled>false</handled>", self.d.proxy("STOP", ROOM_ID=12))
        self.assertEqual([w["action"] for w in self.d.webhooks("transport")], ["pause"])
        self.assertEqual([(c["id"], c["cmd"]) for c in self.d.calls("SendToDevice")], [(12, "ROOM_OFF")])

    def test_panel_stop_turns_room_off(self):
        self.start()
        self.d.proxy("STOP", ROOM_ID=12)
        self.assertEqual([(c["id"], c["cmd"]) for c in self.d.calls("SendToDevice")], [(12, "ROOM_OFF")])
        self.d.proxy("QUEUE_STATE_CHANGED", QUEUE_ID=501, STATE="STOP", QUEUE_INFO="t1")
        self.assertEqual(self.d.room(12)["state"], "stopped")

    def test_user_room_off_reaches_ha(self):
        self.start()
        self.d.set_time(1100)
        self.d.proxy("OFF")                               # user switched the room off
        self.assertEqual(self.d.webhooks("transport")[-1]["action"], "off")
        self.d.proxy("QUEUE_STATE_CHANGED", QUEUE_ID=501, STATE="STOP", QUEUE_INFO="t1")
        self.assertEqual(self.d.webhooks("state")[-1]["state"], "stopped")

    def test_navigator_pause_stop_return_nothing(self):
        # Site 2026-09-28: handled=false on navigator PAUSE/STOP left the stream playing.
        self.start()
        self.assertIsNone(self.d.proxy("PAUSE", ROOMID=12, NAVID="nav", SEQ=3))
        self.assertIsNone(self.d.proxy("STOP", ROOMID=12, NAVID="nav", SEQ=4))
        acts = [w["action"] for w in self.d.webhooks("transport")]
        self.assertEqual(acts, ["pause"])                 # STOP right after is the room-off echo
        self.assertEqual([c["cmd"] for c in self.d.calls("SendToDevice")], ["ROOM_OFF"])

    def test_panel_pause_is_not_a_failure(self):
        self.start()
        self.d.set_time(1002)                              # inside the fallback window
        self.d.proxy("PAUSE", ROOM_ID=12)
        self.d.proxy("QUEUE_STATE_CHANGED", QUEUE_ID=501, STATE="STOP", QUEUE_INFO="t1")
        self.assertEqual(self.d.room(12)["state"], "paused")
        self.assertEqual(len(self.d.proxy_cmds("SELECT_INTERNET_RADIO")), 1)

    def test_room_switched_source(self):
        self.start()
        self.d.set_time(1100)
        self.d.g.OnWatchedVariableChanged(100002, 1009,
            "<audioQueueInfo><queue><id>501</id><rooms><id>13</id></rooms></queue></audioQueueInfo>")
        self.assertEqual(self.d.webhooks("deselected")[0]["room_id"], 12)

    def test_queue_deleted_while_playing(self):
        self.start()
        self.d.set_time(1100)
        self.d.proxy("QUEUE_DELETED", QUEUE_ID=501)
        self.assertEqual(self.d.webhooks("deselected")[0]["room_id"], 12)

    def test_progress_forwarded_to_queue_rooms(self):
        self.start()
        self.d.g.OnWatchedVariableChanged(100002, 1009,
            "<audioQueueInfo><queue><id>501</id><state>PLAY</state><rooms><id>12</id><id>13</id></rooms></queue></audioQueueInfo>")
        self.d.proxy("QUEUE_STREAM_STATUS_CHANGED", QUEUE_ID=501, STATUS="offset=10,length=180")
        ev = [c["params"] for c in self.d.proxy_cmds("SEND_EVENT") if c["params"]["NAME"] == "ProgressChanged"][-1]
        self.assertEqual(ev["ROOMS"], "12,13")
        self.assertIn("<offset>10</offset>", ev["EVTARGS"])

    def test_browse_answers_navigator(self):
        self.d.proxy("GetBrowseMenu", NAVID="nav1", SEQ=7)
        dr = self.d.proxy_cmds("DATA_RECEIVED")[0]["params"]
        self.assertEqual((dr["NAVID"], dr["SEQ"]), ("nav1", "7"))
        self.assertIn("Алиса", dr["DATA"])

    def test_selected_event(self):
        self.d.proxy("DEVICE_SELECTED", idRoom=12)
        self.assertEqual(self.d.webhooks("selected")[0]["room_id"], 12)


class Volume(unittest.TestCase):
    """Station volume -> room volume, ducking, per-room calibration (v0.2.0)."""

    def setUp(self):
        self.d = Driver()
        self.d.http("POST", "/pair", {"webhook_url": "http://ha:8123/api/webhook/abc"})
        self.d.set_time(1000)

    def play(self, room=12):
        body = play_body()
        body["room_id"] = room
        self.d.http("POST", "/play", body)
        self.d.proxy("INTERNET_RADIO_SELECTED", QUEUE_ID=500 + room, ROOM_ID=room, QUEUE_INFO="t1")
        self.d.clear()

    def sv(self, level, room=12, initial=False):
        return self.d.http("POST", "/station_volume", {"room_id": room, "level": level, "initial": initial})[1]

    def levels(self, room=12):
        return [int(c["params"]["LEVEL"]) for c in self.d.calls("SendToDevice")
                if c["id"] == room and c["cmd"] == "SET_VOLUME_LEVEL"]

    def test_first_value_only_remembered(self):
        self.assertEqual(self.sv(0.4)["result"], "recorded")
        self.assertEqual(self.levels(), [])

    def test_initial_flag_resets_baseline(self):
        self.sv(0.4)
        self.assertEqual(self.sv(0.9, initial=True)["result"], "recorded")
        self.assertEqual(self.levels(), [])

    def test_step_moves_from_current_room_level(self):
        self.sv(0.4)
        self.assertEqual(self.sv(0.5)["volume"], 45)        # room variable says 40, step 5
        self.assertEqual(self.sv(0.4)["volume"], 40)
        self.assertEqual(self.levels(), [45, 40])

    def test_keypad_level_is_kept(self):
        self.sv(0.4)
        self.d.g.OnWatchedVariableChanged(12, 1011, "20")  # someone turned it down on a keypad
        self.assertEqual(self.sv(0.5)["volume"], 25)

    def test_absolute_for_bigger_jumps(self):
        self.sv(0.1)
        self.assertEqual(self.sv(0.8)["result"], "absolute")
        self.assertEqual(self.levels(), [5 + round(0.8 * 65)])   # min + level * (max - min)

    def test_cap_and_floor(self):
        self.sv(0.4)
        self.d.g.OnWatchedVariableChanged(12, 1011, "68")
        self.assertEqual(self.sv(0.5)["volume"], 70)       # default max 70
        self.d.g.OnWatchedVariableChanged(12, 1011, "7")
        self.assertEqual(self.sv(0.4)["volume"], 5)        # default min 5

    def test_no_volume_variable_falls_back_to_absolute(self):
        self.d.g.ROOM_VARS[12] = self.d.lua.table_from({1000: self.d.lua.table_from({"name": "POWER_STATE", "value": "1"})})
        self.sv(0.4)
        self.assertEqual(self.sv(0.5)["result"], "absolute")

    def test_duck_lowers_and_restores(self):
        self.play()
        self.sv(0.4)
        r = self.d.http("POST", "/duck", {"room_id": 12, "active": True})[1]
        self.assertEqual((r["result"], r["ducked"]), ("ducked", True))
        self.assertEqual(self.levels(), [12])               # 30 % of 40
        self.sv(0.5)                                        # "Алиса, громче" while ducked
        self.assertEqual(self.levels(), [12])               # nothing yet
        r = self.d.http("POST", "/duck", {"room_id": 12, "active": False})[1]
        self.assertEqual(r["result"], "restored")
        self.assertEqual(self.levels(), [12, 45])           # restored with the step applied

    def test_duck_only_while_playing(self):
        r = self.d.http("POST", "/duck", {"room_id": 12, "active": True})[1]
        self.assertEqual(r["result"], "not playing")
        self.assertEqual(self.d.calls("SendToDevice"), [])

    def test_duck_released_by_timeout(self):
        self.play()
        self.d.http("POST", "/duck", {"room_id": 12, "active": True})
        self.d.fire_timers()
        self.assertEqual(self.levels(), [12, 40])
        self.assertFalse(self.d.g.YANDEX_RELAY_TEST.vol[12]["ducked"])

    def test_duck_mute_and_off_modes(self):
        self.play()
        self.select_room("Гостиная & кухня")
        self.set_prop("Volume: Duck", "Mute")
        self.d.clear()
        self.d.http("POST", "/duck", {"room_id": 12, "active": True})
        self.d.http("POST", "/duck", {"room_id": 12, "active": False})
        self.assertEqual([c["cmd"] for c in self.d.calls("SendToDevice")], ["MUTE_ON", "MUTE_OFF"])
        self.set_prop("Volume: Duck", "Off")
        self.d.clear()
        self.assertEqual(self.d.http("POST", "/duck", {"room_id": 12, "active": True})[1]["result"], "off")
        self.assertEqual(self.d.calls("SendToDevice"), [])

    # --- Composer properties -------------------------------------------------
    def select_room(self, name):
        self.d.g.PROPS["Volume: Room"] = name
        self.d.g.OnPropertyChanged("Volume: Room")

    def set_prop(self, name, value):
        self.d.g.PROPS[name] = value
        self.d.g.OnPropertyChanged(name)

    def test_room_selector_lists_project_rooms(self):
        lists = [c for c in self.d.calls("UpdatePropertyList") if c["name"] == "Volume: Room"]
        self.assertEqual(lists[-1]["list"], "-,Гостиная & кухня,Спальня")

    def test_settings_saved_per_room(self):
        self.select_room("Спальня")
        self.assertEqual(self.d.prop("Volume: Max %"), "70")        # defaults loaded
        self.assertEqual(self.d.prop("Volume: Current"), "40 %")
        self.set_prop("Volume: Max %", "50")
        self.set_prop("Volume: Step %", "10")
        self.assertEqual(self.d.g.PERSIST["volume_cfg"]["13"]["max"], 50)
        self.sv(0.4, room=13)
        self.assertEqual(self.sv(0.5, room=13)["volume"], 50)        # 40 + 10, capped 50
        self.assertEqual(self.d.g.YANDEX_RELAY_TEST.vol_cfg(12)["max"], 70)   # other room untouched
        self.select_room("Гостиная & кухня")
        self.assertEqual(self.d.prop("Volume: Max %"), "70")
        self.select_room("Спальня")
        self.assertEqual(self.d.prop("Volume: Step %"), "10")

    def test_min_kept_below_max(self):
        self.select_room("Спальня")
        self.set_prop("Volume: Min %", "80")
        self.assertEqual(self.d.prop("Volume: Min %"), "69")

    def test_field_without_room_not_saved(self):
        self.set_prop("Volume: Max %", "10")
        self.assertIsNone(self.d.g.PERSIST["volume_cfg"])

    def test_calibration_actions(self):
        self.select_room("Спальня")
        self.d.g.OnWatchedVariableChanged(13, 1011, "55")
        self.assertEqual(self.d.prop("Volume: Current"), "55 %")
        self.d.g.ExecuteCommand("LUA_ACTION", self.d.lua.table_from({"ACTION": "VolMaxFromCurrent"}))
        self.assertEqual(self.d.prop("Volume: Max %"), "55")
        self.d.g.OnWatchedVariableChanged(13, 1011, "15")
        self.d.g.ExecuteCommand("LUA_ACTION", self.d.lua.table_from({"ACTION": "VolMinFromCurrent"}))
        self.assertEqual(self.d.prop("Volume: Min %"), "15")
        self.assertEqual(dict(self.d.g.PERSIST["volume_cfg"]["13"].items())["max"], 55)

    def test_settings_survive_restart(self):
        self.select_room("Спальня")
        self.set_prop("Volume: Max %", "40")
        self.d.g.OnDriverLateInit()
        self.assertEqual(self.d.g.YANDEX_RELAY_TEST.vol_cfg(13)["max"], 40)

    # --- reporting --------------------------------------------------------
    def test_room_volume_change_reported_to_ha(self):
        self.play()
        self.d.http("GET", "/state?room_id=12")                    # starts watching the variable
        self.sv(0.4)
        self.d.g.OnWatchedVariableChanged(12, 1011, "33")
        ev = self.d.webhooks("volume")[-1]
        self.assertEqual((ev["room_id"], ev["level"]), (12, 33))
        self.assertEqual(self.d.http("GET", "/state?room_id=12")[1]["volume"], 33)

    def test_direct_volume_capped(self):
        self.assertEqual(self.d.http("POST", "/volume", {"room_id": 12, "level": 95})[1]["volume"], 70)


class V030(unittest.TestCase):
    """Volume by Alice's executed command, copyable code, pairing extras (v0.3.0)."""

    def setUp(self):
        self.d = Driver()
        self.hook = "http://ha:8123/api/webhook/abc"

    def pair(self, **extra):
        return self.d.http("POST", "/pair", {"webhook_url": self.hook, **extra})[1]

    def levels(self, room=12):
        return [int(c["params"]["LEVEL"]) for c in self.d.calls("SendToDevice")
                if c["id"] == room and c["cmd"] == "SET_VOLUME_LEVEL"]

    def test_volume_step_from_current_level(self):
        self.pair()
        self.assertEqual(self.d.http("POST", "/volume_step", {"room_id": 12, "steps": 1})[1]["volume"], 45)
        self.assertEqual(self.d.http("POST", "/volume_step", {"room_id": 12, "steps": -1})[1]["volume"], 40)
        self.assertEqual(self.levels(), [45, 40])

    def test_volume_level_maps_onto_min_max(self):
        self.pair()
        r = self.d.http("POST", "/volume_level", {"room_id": 12, "level": 0.5})[1]
        self.assertEqual((r["result"], r["volume"]), ("absolute", 38))     # 5 + 0.5 * 65

    def test_volume_step_while_ducked_changes_restore_level(self):
        self.pair()
        body = play_body()
        self.d.http("POST", "/play", body)
        self.d.proxy("INTERNET_RADIO_SELECTED", QUEUE_ID=501, ROOM_ID=12, QUEUE_INFO="t1")
        self.d.http("POST", "/duck", {"room_id": 12, "active": True})
        self.d.clear()
        self.d.http("POST", "/volume_step", {"room_id": 12, "steps": 1})
        self.assertEqual(self.levels(), [])
        self.d.http("POST", "/duck", {"room_id": 12, "active": False})
        self.assertEqual(self.levels(), [45])

    def test_volume_step_needs_room_level(self):
        self.d.g.ROOM_VARS[12] = self.d.lua.table_from({})
        self.assertEqual(self.d.http("POST", "/volume_step", {"room_id": 12, "steps": 1})[1]["result"],
                         "volume unknown")

    def test_bad_requests(self):
        self.assertEqual(self.d.http("POST", "/volume_step", {"room_id": 12})[0], 400)
        self.assertEqual(self.d.http("POST", "/volume_level", {"room_id": 12})[0], 400)

    def test_invalid_pairing_code_edits_are_undone(self):
        code = self.d.prop("Pairing Code")
        for bad in ("abc", "ПАРОЛЬ12", "AB-CD-EF-GH", ""):
            self.d.g.PROPS["Pairing Code"] = bad
            self.d.g.OnPropertyChanged("Pairing Code")
            self.assertEqual(self.d.prop("Pairing Code"), code)
        self.assertEqual(self.d.http("GET", "/info")[0], 200)

    def test_old_code_pasted_into_readded_driver(self):
        self.pair()
        old = self.d.prop("Pairing Code")
        self.d.g.PROPS["Pairing Code"] = " abcd 2345 "
        self.d.g.OnPropertyChanged("Pairing Code")
        self.assertEqual(self.d.prop("Pairing Code"), "ABCD2345")
        self.assertEqual(self.d.g.PERSIST["pairing_code"], "ABCD2345")
        self.assertEqual(self.d.http("GET", "/info", key=old)[0], 401)
        # HA with this code: heartbeat says "not paired", HA pairs again
        code, body = self.d.http("POST", "/heartbeat", {"webhook_url": self.hook}, key="ABCD2345")
        self.assertEqual((code, body["paired"]), (200, False))
        self.d.http("POST", "/pair", {"webhook_url": self.hook}, key="ABCD2345")
        self.assertTrue(self.d.http("POST", "/heartbeat", {"webhook_url": self.hook}, key="ABCD2345")[1]["paired"])

    def test_pairing_code_property_is_editable(self):
        with open(os.path.join(HERE, "..", "driver.xml"), encoding="utf-8") as f:
            xml = f.read()
        self.assertRegex(xml, r"<name>Pairing Code</name>\s*<type>STRING</type>\s*<default></default>\s*<readonly>false</readonly>")

    def test_bound_rooms_listed_first_and_persisted(self):
        self.pair(rooms=[13])
        lists = [c for c in self.d.calls("UpdatePropertyList") if c["name"] == "Volume: Room"]
        self.assertEqual(lists[-1]["list"], "-,Спальня,Гостиная & кухня")
        self.assertEqual(list(self.d.g.PERSIST["bound_rooms"].values()), [13])
        self.d.g.OnDriverLateInit()                                  # survives restart
        lists = [c for c in self.d.calls("UpdatePropertyList") if c["name"] == "Volume: Room"]
        self.assertEqual(lists[-1]["list"], "-,Спальня,Гостиная & кухня")

    def test_fresh_driver_takes_ha_copy_of_settings(self):
        r = self.pair(volume_cfg={"13": {"step": 10, "max": 42, "min": 3, "duck": "Mute", "duck_level": 20}})
        self.assertTrue(r["volume_restored"])
        self.assertEqual(self.d.g.YANDEX_RELAY_TEST.vol_cfg(13)["max"], 42)
        self.assertEqual(self.d.g.PERSIST["volume_cfg"]["13"]["step"], 10)

    def test_existing_settings_not_overwritten_by_ha(self):
        self.pair()
        self.d.g.PROPS["Volume: Room"] = "Спальня"
        self.d.g.OnPropertyChanged("Volume: Room")
        self.d.g.PROPS["Volume: Max %"] = "55"
        self.d.g.OnPropertyChanged("Volume: Max %")
        r = self.pair(volume_cfg={"13": {"max": 42}})
        self.assertFalse(r["volume_restored"])
        self.assertEqual(self.d.g.YANDEX_RELAY_TEST.vol_cfg(13)["max"], 55)
        self.assertEqual(r["volume_cfg"]["13"]["max"], 55)

    def test_settings_change_sent_to_ha_and_summary(self):
        self.pair(rooms=[12])
        self.d.g.PROPS["Volume: Room"] = "Спальня"
        self.d.g.OnPropertyChanged("Volume: Room")
        self.d.g.PROPS["Volume: Max %"] = "50"
        self.d.g.OnPropertyChanged("Volume: Max %")
        ev = self.d.webhooks("volume_cfg")[-1]
        self.assertEqual(ev["volume_cfg"]["13"]["max"], 50)
        self.assertEqual(self.d.prop("Volume: Summary"),
                         "Гостиная & кухня 5/70/5 Lower 30 wake 10/2; Спальня 5/50/5 Lower 30 wake 10/2")


class Alarms(unittest.TestCase):
    """Station alarms -> per-room events, variable, volume ramp (v0.4.0)."""

    WEEKDAYS = {"id": "a1", "time": "07:00", "days": [1, 2, 3, 4, 5], "enabled": True}

    def setUp(self):
        self.d = Driver()
        self.d.http("POST", "/pair", {"webhook_url": "http://ha:8123/api/webhook/abc", "rooms": [12]})
        self.at(2026, 9, 28, 6, 40)          # Monday
        self.d.clear()

    def at(self, y, mo, d, h, mi, s=0):
        self.d.set_time(self.d.lua.eval(f"os.time({{year={y},month={mo},day={d},hour={h},min={mi},sec={s}}})"))

    def alarms(self, *alarms, room=12):
        return self.d.http("POST", "/alarms", {"room_id": room, "alarms": list(alarms)})

    def fired(self):
        return [c["id"] for c in self.d.calls("FireEventByID")]

    def var(self, room_name="Гостиная & кухня"):
        return self.d.g.VARS[room_name + ": следующий будильник"]

    def levels(self, room=12):
        return [int(c["params"]["LEVEL"]) for c in self.d.calls("SendToDevice")
                if c["id"] == room and c["cmd"] == "SET_VOLUME_LEVEL"]

    def play(self):
        self.d.http("POST", "/play", play_body())
        self.d.proxy("INTERNET_RADIO_SELECTED", QUEUE_ID=512, ROOM_ID=12, QUEUE_INFO="t1")

    def test_bound_room_gets_events_and_variable(self):
        d = Driver()
        d.http("POST", "/pair", {"webhook_url": "http://ha:8123/api/webhook/abc", "rooms": [12]})
        events = {c["id"]: c["name"] for c in d.calls("AddEvent")}
        self.assertEqual(events, {10121: "Гостиная & кухня: подготовка к пробуждению",
                                  10122: "Гостиная & кухня: будильник"})
        self.assertEqual([c["name"] for c in d.calls("AddVariable")],
                         ["HA_ONLINE", "Гостиная & кухня: следующий будильник"])

    def test_weekday_alarm_prewake_then_ring(self):
        code, body = self.alarms(self.WEEKDAYS)
        self.assertEqual(code, 200)
        self.assertEqual(body["next"], "2026-09-28 07:00")
        self.assertEqual(self.var(), "2026-09-28 07:00")
        self.at(2026, 9, 28, 6, 49)
        self.d.fire_timers()
        self.assertEqual(self.fired(), [])
        self.at(2026, 9, 28, 6, 50)
        self.d.fire_timers()
        self.d.fire_timers()
        self.assertEqual(self.fired(), [10121])                 # once
        self.at(2026, 9, 28, 7, 0, 10)
        self.d.fire_timers()
        self.assertEqual(self.fired(), [10121, 10122])
        self.assertEqual(self.var(), "2026-09-29 07:00")
        phases = [(w["phase"], w["alarm_id"]) for w in self.d.webhooks("alarm")]
        self.assertEqual(phases, [("prewake", "a1"), ("ring", "a1")])

    def test_weekend_skipped(self):
        self.at(2026, 10, 3, 8, 0)             # Saturday
        self.assertEqual(self.alarms(self.WEEKDAYS)[1]["next"], "2026-10-05 07:00")

    def test_one_off_and_disabled(self):
        self.alarms({"id": "b", "time": "09:00", "date": "2026-09-29"},
                    {"id": "c", "time": "06:45", "days": [1, 2, 3, 4, 5, 6, 7], "enabled": False})
        self.assertEqual(self.var(), "2026-09-29 09:00")
        self.at(2026, 9, 28, 6, 45)
        self.d.fire_timers()
        self.assertEqual(self.fired(), [])
        self.at(2026, 9, 29, 9, 0)
        self.d.fire_timers()
        self.assertEqual(self.fired(), [10121, 10122])      # pre-wake overdue: both, in order
        self.assertEqual(self.var(), "")

    def test_restart_does_not_repeat(self):
        self.alarms(self.WEEKDAYS)
        self.at(2026, 9, 28, 7, 0)
        self.d.fire_timers()
        self.d.g.OnDriverLateInit()
        self.at(2026, 9, 28, 7, 2)
        self.d.fire_timers()
        self.assertEqual(self.fired(), [10121, 10122])

    def test_late_alarm_within_grace_only(self):
        self.at(2026, 9, 28, 7, 3)
        self.alarms(self.WEEKDAYS)
        self.assertEqual(self.fired(), [10121, 10122])
        d = Driver()
        d.http("POST", "/pair", {"webhook_url": "http://ha:8123/api/webhook/abc", "rooms": [12]})
        self.d = d
        self.at(2026, 9, 28, 7, 10)
        self.alarms(self.WEEKDAYS)
        self.assertEqual(self.fired(), [])
        self.assertEqual(self.var(), "2026-09-29 07:00")

    def test_prewake_setting_per_room(self):
        self.d.g.PROPS["Volume: Room"] = "Гостиная & кухня"
        self.d.g.OnPropertyChanged("Volume: Room")
        self.assertEqual(self.d.prop("Alarm: Pre-wake min"), "10")
        self.d.g.PROPS["Alarm: Pre-wake min"] = "0"
        self.d.g.OnPropertyChanged("Alarm: Pre-wake min")
        self.assertEqual(self.d.webhooks("volume_cfg")[-1]["volume_cfg"]["12"]["prewake"], 0)
        self.alarms(self.WEEKDAYS)
        self.assertEqual(self.d.prop("Alarm: Next"), "2026-09-28 07:00 (1 alarm)")
        self.at(2026, 9, 28, 7, 0)
        self.d.fire_timers()
        self.assertEqual(self.fired(), [10122])

    def test_ramp_when_room_plays_at_ring(self):
        self.play()
        self.alarms(self.WEEKDAYS)
        self.at(2026, 9, 28, 7, 0)
        self.d.clear()
        self.d.fire_timers()
        self.assertEqual(self.levels(), [5])                   # from Min
        self.at(2026, 9, 28, 7, 1)
        self.d.fire_timers()
        self.assertEqual(self.levels()[-1], 23)                 # halfway to 40
        self.at(2026, 9, 28, 7, 2)
        self.d.fire_timers()
        self.assertEqual(self.levels()[-1], 40)
        self.assertIsNone(self.d.g.YANDEX_RELAY_TEST.ramp[12])

    def test_ramp_waits_for_stream_and_voice_stops_it(self):
        self.alarms(self.WEEKDAYS)
        self.at(2026, 9, 28, 7, 0)
        self.d.fire_timers()
        self.assertEqual(self.levels(), [])                    # room silent: only armed
        self.at(2026, 9, 28, 7, 1)
        self.d.clear()
        self.play()
        self.assertEqual(self.levels(), [5])
        self.d.http("POST", "/volume_step", {"room_id": 12, "steps": 1})
        self.assertEqual(self.levels(), [5, 10])
        self.at(2026, 9, 28, 7, 3)
        self.d.fire_timers()
        self.assertEqual(self.levels(), [5, 10])

    def test_keypad_change_stops_ramp(self):
        self.play()
        self.alarms(self.WEEKDAYS)
        self.at(2026, 9, 28, 7, 0)
        self.d.fire_timers()
        self.d.g.OnWatchedVariableChanged(12, 1011, "5")       # our own level coming back
        self.assertIsNotNone(self.d.g.YANDEX_RELAY_TEST.ramp[12])
        self.d.g.OnWatchedVariableChanged(12, 1011, "30")      # someone on a keypad
        self.assertIsNone(self.d.g.YANDEX_RELAY_TEST.ramp[12])

    def test_ramp_too_late_after_ring(self):
        self.alarms(self.WEEKDAYS)
        self.at(2026, 9, 28, 7, 0)
        self.d.fire_timers()
        self.at(2026, 9, 28, 7, 10)
        self.d.clear()
        self.play()
        self.assertEqual(self.levels(), [])

    def test_test_actions(self):
        self.d.g.PROPS["Volume: Room"] = "Гостиная & кухня"
        self.d.g.OnPropertyChanged("Volume: Room")
        for action in ("AlarmTestPrewake", "AlarmTestRing"):
            self.d.g.ExecuteCommand("LUA_ACTION", self.d.lua.table_from({"ACTION": action}))
        self.assertEqual(self.fired(), [10121, 10122])

    def test_bad_and_malformed(self):
        self.assertEqual(self.d.http("POST", "/alarms", {"room_id": 12})[0], 400)
        code, body = self.alarms({"id": "x", "time": "25:00", "days": [1]}, {"id": "y", "time": "07:00"},
                                 {"id": "z", "time": "7:30", "days": [0, 3, 9]})
        self.assertEqual(code, 200)
        self.assertEqual(body["next"], "2026-09-30 07:30")      # only z, Wednesday
        self.assertEqual(self.d.http("GET", "/alarms?room_id=12")[1]["alarms"][0]["id"], "z")
        self.alarms()
        self.assertIsNone(self.d.g.YANDEX_RELAY_TEST.alarms()["12"])


class HaLink(unittest.TestCase):
    """Heartbeat, HA online/offline events (v0.5.0)."""

    def setUp(self):
        self.d = Driver()
        self.hook = "http://ha:8123/api/webhook/abc"
        self.d.set_time(1000)

    def events(self):
        return [c["name"] for c in self.d.calls("FireEvent")]

    def test_unpaired_never_goes_offline(self):
        self.assertEqual(self.d.prop("Home Assistant"), "not paired")
        self.d.set_time(5000)
        self.d.fire_timers()
        self.assertEqual(self.events(), [])

    def test_heartbeat_summary_and_offline_events(self):
        self.d.http("POST", "/pair", {"webhook_url": self.hook})
        body = self.d.http("POST", "/heartbeat", {"webhook_url": self.hook,
                                                  "summary": "c4_relay 0.5.0, AlexxIT 3.19; Офис ok"})[1]
        self.assertEqual(body, {"ok": True, "version": "0.5.0", "paired": True})
        self.assertEqual(self.d.prop("Home Assistant"), "online · c4_relay 0.5.0, AlexxIT 3.19; Офис ok")
        self.assertEqual(self.d.g.VARS["HA_ONLINE"], "1")
        self.d.set_time(1170)
        self.d.fire_timers()
        self.assertEqual(self.events(), [])
        self.d.set_time(1190)
        self.d.fire_timers()
        self.d.fire_timers()
        self.assertEqual(self.events(), ["Home Assistant: связь потеряна"])
        self.assertTrue(self.d.prop("Home Assistant").startswith("OFFLINE since "))
        self.assertEqual(self.d.g.VARS["HA_ONLINE"], "0")
        self.d.http("GET", "/state")                    # any request from HA
        self.assertEqual(self.events(), ["Home Assistant: связь потеряна", "Home Assistant: связь восстановлена"])
        self.assertEqual(self.d.g.VARS["HA_ONLINE"], "1")

    def test_ha_silent_after_controller_boot(self):
        self.d.http("POST", "/pair", {"webhook_url": self.hook})
        self.d.g.OnDriverLateInit()                     # controller restarted, HA never came
        self.d.set_time(1200)
        self.d.fire_timers()
        self.assertIn("Home Assistant: связь потеряна", self.events())

    def test_rejected_request_is_no_sign_of_life(self):
        self.d.http("POST", "/pair", {"webhook_url": self.hook})
        self.d.set_time(1100)
        self.d.http("GET", "/info", key="WRONG")
        self.d.set_time(1190)
        self.d.fire_timers()
        self.assertEqual(self.events(), ["Home Assistant: связь потеряна"])

    def test_events_declared_in_xml(self):
        with open(os.path.join(HERE, "..", "driver.xml"), encoding="utf-8") as f:
            xml = f.read()
        self.assertIn("<name>Home Assistant: связь потеряна</name>", xml)
        self.assertIn("<name>Home Assistant: связь восстановлена</name>", xml)


class Actions(unittest.TestCase):
    def test_regenerate_unpairs(self):
        d = Driver()
        d.http("POST", "/pair", {"webhook_url": "http://ha:8123/api/webhook/abc"})
        old = d.prop("Pairing Code")
        d.g.ExecuteCommand("LUA_ACTION", d.lua.table_from({"ACTION": "RegeneratePairingCode"}))
        self.assertNotEqual(d.prop("Pairing Code"), old)
        self.assertEqual(d.prop("Paired With"), "")
        self.assertEqual(d.http("GET", "/info", key=old)[0], 401)
        self.assertFalse(d.http("GET", "/info")[1]["paired"])

    def test_server_retried_until_online(self):
        # Site 2026-09-28: re-added instance stayed "starting", port still held.
        d = Driver()
        d.g.SERVER_BIND_FAILS = True
        d.g.PROPS["Bridge Port"] = "18766"
        d.g.OnPropertyChanged("Bridge Port")
        self.assertEqual(d.prop("Bridge Status"), "starting :18766")
        d.fire_timers()
        self.assertEqual(d.prop("Bridge Status"), "retry 1 :18766")
        d.g.SERVER_BIND_FAILS = False
        d.fire_timers()
        self.assertEqual(d.prop("Bridge Status"), "ONLINE :18766")
        n = len(d.calls("CreateServer"))
        d.fire_timers()
        self.assertEqual(len(d.calls("CreateServer")), n)       # online: no more retries

    def test_offline_triggers_retry_and_old_port_ignored(self):
        d = Driver()
        n = len(d.calls("CreateServer"))
        d.g.OnServerStatusChanged(12345, "OFFLINE")              # some other port
        self.assertEqual(d.prop("Bridge Status"), "ONLINE :18765")
        d.g.OnServerStatusChanged(18765, "OFFLINE")
        d.fire_timers()
        self.assertEqual(len(d.calls("CreateServer")), n + 1)
        self.assertEqual(d.prop("Bridge Status"), "ONLINE :18765")

    def test_port_change_restarts_server(self):
        d = Driver()
        d.g.PROPS["Bridge Port"] = "19000"
        d.g.OnPropertyChanged("Bridge Port")
        self.assertEqual(d.calls("DestroyServer")[-1]["port"], 18765)
        self.assertEqual(d.calls("CreateServer")[-1]["port"], 19000)


class Parsers(unittest.TestCase):
    def test_room_map(self):
        d = Driver()
        m = d.g.YANDEX_RELAY_TEST.parse_room_map(
            "<audioQueueInfo><queue><id>7</id><rooms><id>1</id><id>2</id></rooms></queue>"
            "<queue><id>8</id><rooms></rooms></queue></audioQueueInfo>")
        self.assertEqual(list(m[7].values()), [1, 2])
        self.assertEqual(len(m[8]), 0)


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=1).result
    sys.exit(0 if result.wasSuccessful() else 1)
