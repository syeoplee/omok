# LAN omok (gomoku) server - Python stdlib only (no pip install needed)
# Run:  python server.py [port]      (default port 8001)
# Colors: "b" = black (moves first), "w" = white
import json
import socket
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import os
# Cloud hosts (Render/Railway/etc.) inject the port via $PORT.
PORT = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8001))
ROOT = Path(__file__).resolve().parent

lock = threading.Lock()
game = {
    "moves": [],      # list of {"r","c"}; even index = black, odd = white
    "players": {},    # id -> "b" | "w" | "s"
    "reset": 0,
    "undo": {"b": 2, "w": 2},   # takebacks left per player, per game
    "undoReq": None,            # color that requested a takeback, or None
    "result": None,             # {"kind": "resign", "winner": "b"|"w"} or None
}


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


IP = lan_ip()


def wan_url():
    f = ROOT / "tunnel_url.txt"
    try:
        u = f.read_text(encoding="utf-8-sig").strip()   # -sig: strip BOM if present
        return u if u.startswith("http") else None
    except OSError:
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet console
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            page = (ROOT / "index.html").read_bytes()
            self._send(200, page, "text/html")
        elif url.path == "/state":
            since = int((parse_qs(url.query).get("since") or ["0"])[0])
            with lock:
                self._send(200, {
                    "moves": game["moves"][since:],
                    "total": len(game["moves"]),
                    "players": self._slots(),
                    "reset": game["reset"],
                    "undo": game["undo"],
                    "undoReq": game["undoReq"],
                    "result": game["result"],
                    "wan": wan_url(),
                })
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        data = self._body()
        if self.path == "/join":
            with lock:
                pid = data.get("id")
                want = data.get("want")
                if pid and pid in game["players"]:
                    pass  # rejoin keeps the existing color
                elif want in ("b", "w", "s"):
                    taken = set(game["players"].values())
                    if want != "s" and want in taken:
                        self._send(409, {"error": "taken", "players": self._slots()})
                        return
                    pid = uuid.uuid4().hex
                    game["players"][pid] = want
                else:
                    # unknown id and no pick yet: tell the client to choose
                    self._send(200, {
                        "id": None, "color": None,
                        "players": self._slots(),
                        "reset": game["reset"],
                        "url": f"http://{IP}:{PORT}",
                    })
                    return
                self._send(200, {
                    "id": pid,
                    "color": game["players"][pid],
                    "players": self._slots(),
                    "reset": game["reset"],
                    "url": f"http://{IP}:{PORT}",
                })
        elif self.path == "/move":
            with lock:
                pid = data.get("id")
                color = game["players"].get(pid)
                slots = self._slots()
                expected = "b" if len(game["moves"]) % 2 == 0 else "w"
                m = data.get("move") or {}
                r, c = m.get("r"), m.get("c")
                occupied = any(mv["r"] == r and mv["c"] == c for mv in game["moves"])
                if color not in ("b", "w"):
                    self._send(403, {"error": "not a player"})
                elif game["result"]:
                    self._send(409, {"error": "game is over"})
                elif not (slots["b"] and slots["w"]):
                    self._send(409, {"error": "waiting for opponent"})
                elif color != expected:
                    self._send(409, {"error": "not your turn"})
                elif data.get("n") != len(game["moves"]):
                    self._send(409, {"error": "out of sync"})
                elif not (isinstance(r, int) and isinstance(c, int)
                          and 0 <= r < 15 and 0 <= c < 15) or occupied:
                    self._send(409, {"error": "invalid point"})
                else:
                    game["moves"].append({"r": r, "c": c})
                    game["undoReq"] = None  # a new move voids any pending takeback
                    self._send(200, {"ok": True, "total": len(game["moves"])})
        elif self.path == "/undo":
            with lock:
                color = game["players"].get(data.get("id"))
                made_a_move = len(game["moves"]) >= (1 if color == "b" else 2)
                if color not in ("b", "w"):
                    self._send(403, {"error": "not a player"})
                elif game["result"]:
                    self._send(409, {"error": "game is over"})
                elif game["undoReq"]:
                    self._send(409, {"error": "request already pending"})
                elif game["undo"][color] <= 0:
                    self._send(409, {"error": "no takebacks left"})
                elif not made_a_move:
                    self._send(409, {"error": "nothing to take back"})
                else:
                    game["undoReq"] = color
                    self._send(200, {"ok": True})
        elif self.path == "/undo_reply":
            with lock:
                color = game["players"].get(data.get("id"))
                req = game["undoReq"]
                if color not in ("b", "w") or not req or color == req:
                    self._send(403, {"error": "not allowed"})
                else:
                    if data.get("accept"):
                        # pop moves until the requester's last move is removed
                        pops = 0
                        while game["moves"] and pops < 2:
                            mover = "b" if (len(game["moves"]) - 1) % 2 == 0 else "w"
                            game["moves"].pop()
                            pops += 1
                            if mover == req:
                                break
                        game["undo"][req] -= 1
                    game["undoReq"] = None
                    self._send(200, {"ok": True})
        elif self.path == "/resign":
            with lock:
                color = game["players"].get(data.get("id"))
                if color not in ("b", "w"):
                    self._send(403, {"error": "not a player"})
                elif game["result"]:
                    self._send(409, {"error": "game already over"})
                else:
                    game["result"] = {"kind": "resign",
                                      "winner": "w" if color == "b" else "b"}
                    game["undoReq"] = None
                    self._send(200, {"ok": True})
        elif self.path == "/reset":
            with lock:
                if game["players"].get(data.get("id")) in ("b", "w"):
                    game["moves"] = []
                    game["reset"] += 1
                    game["undo"] = {"b": 2, "w": 2}
                    game["undoReq"] = None
                    game["result"] = None
                    self._send(200, {"ok": True, "reset": game["reset"]})
                else:
                    self._send(403, {"error": "not a player"})
        else:
            self._send(404, {"error": "not found"})

    def _slots(self):
        vals = list(game["players"].values())
        return {"b": "b" in vals, "w": "w" in vals, "spec": vals.count("s")}


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("=" * 46)
    print("  LAN Omok server running!")
    print(f"  내 브라우저:   http://localhost:{PORT}")
    print(f"  상대방 접속:   http://{IP}:{PORT}")
    print("  (같은 와이파이/공유기에 연결되어 있어야 합니다)")
    print("  종료: Ctrl+C")
    print("=" * 46)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
