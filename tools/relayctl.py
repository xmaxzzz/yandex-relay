"""Talk to a Yandex Relay driver directly, without Home Assistant.

Used on site to check the driver before c4_relay is installed. Standard library
only. The host and pairing code come from --host/--code or the RELAY_HOST /
RELAY_CODE environment variables.

  python tools/relayctl.py info
  python tools/relayctl.py rooms
  python tools/relayctl.py listen                  # pair with this PC, print webhook events
  python tools/relayctl.py play --room 12 --url https://example/track.mp3 --title Test
  python tools/relayctl.py pause --room 12         # also: resume, stop
  python tools/relayctl.py state [--room 12]

`listen` re-pairs the driver with this PC. Pair it with Home Assistant again
afterwards (re-add the c4_relay integration or run its re-pair).
"""

import argparse
import http.client
import http.server
import json
import os
import socket
import sys
from datetime import datetime


def request(args, method, path, body=None):
    conn = http.client.HTTPConnection(args.host, args.port, timeout=10)
    payload = json.dumps(body) if body is not None else None
    headers = {"X-Relay-Key": args.code}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8") or "{}"
    try:
        parsed = json.loads(data)
    except ValueError:
        parsed = {"raw": data}
    if resp.status != 200:
        sys.exit(f"HTTP {resp.status}: {json.dumps(parsed, ensure_ascii=False)}")
    return parsed


def show(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def local_ip_towards(host):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 9))
        return s.getsockname()[0]


def cmd_listen(args):
    class Hook(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.end_headers()
            try:
                evt = json.loads(body)
            except ValueError:
                evt = {"raw": body.decode("utf-8", "replace")}
            print(datetime.now().strftime("%H:%M:%S"), json.dumps(evt, ensure_ascii=False), flush=True)

        def log_message(self, *a):
            pass

    ip = local_ip_towards(args.host)
    server = http.server.HTTPServer(("0.0.0.0", args.listen_port), Hook)
    url = f"http://{ip}:{args.listen_port}/relay-hook"
    show(request(args, "POST", "/pair", {"webhook_url": url}))
    print(f"paired, webhook {url}; Ctrl+C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default=os.environ.get("RELAY_HOST"))
    p.add_argument("--port", type=int, default=int(os.environ.get("RELAY_PORT", "18765")))
    p.add_argument("--code", default=os.environ.get("RELAY_CODE"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    sub.add_parser("rooms")
    ls = sub.add_parser("listen")
    ls.add_argument("--listen-port", type=int, default=8099)
    pl = sub.add_parser("play")
    pl.add_argument("--room", type=int, required=True)
    pl.add_argument("--url", default="")
    pl.add_argument("--fallback", default="")
    pl.add_argument("--title", default="Yandex Relay test")
    pl.add_argument("--artist", default="")
    pl.add_argument("--image", default="")
    for name in ("pause", "resume", "stop"):
        sub.add_parser(name).add_argument("--room", type=int, required=True)
    st = sub.add_parser("state")
    st.add_argument("--room", type=int)
    args = p.parse_args()
    if not args.host or not args.code:
        p.error("--host and --code (or RELAY_HOST / RELAY_CODE) are required")

    if args.cmd == "info":
        show(request(args, "GET", "/info"))
    elif args.cmd == "rooms":
        show(request(args, "GET", "/rooms"))
    elif args.cmd == "listen":
        cmd_listen(args)
    elif args.cmd == "play":
        show(request(args, "POST", "/play", {
            "room_id": args.room, "url": args.url, "fallback_url": args.fallback,
            "title": args.title, "artist": args.artist, "image": args.image,
            "key": datetime.now().strftime("test-%H%M%S"),
        }))
    elif args.cmd in ("pause", "resume", "stop"):
        show(request(args, "POST", "/" + args.cmd, {"room_id": args.room}))
    elif args.cmd == "state":
        show(request(args, "GET", f"/state?room_id={args.room}" if args.room else "/state"))


if __name__ == "__main__":
    main()
