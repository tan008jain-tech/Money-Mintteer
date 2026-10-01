from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from typing import Callable
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    system_action: str
    wallet: float
    profit_reserve: float
    broker_cash: float
    open_positions: tuple[dict[str, object], ...]
    health: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "system_action": self.system_action,
            "wallet": self.wallet,
            "profit_reserve": self.profit_reserve,
            "broker_cash": self.broker_cash,
            "open_positions": list(self.open_positions),
            "health": list(self.health),
        }


def render_dashboard_html(snapshot: DashboardSnapshot) -> str:
    payload = json.dumps(snapshot.as_dict(), sort_keys=True)
    escaped = (
        payload.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Money Mintter Control Room</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1rem}}
.card{{border:1px solid #ddd;border-radius:10px;padding:1rem}}
pre{{white-space:pre-wrap;overflow:auto}}
</style>
</head>
<body>
<h1>Money Mintter Control Room</h1>
<div class="grid">
<div class="card"><b>System</b><div id="action"></div></div>
<div class="card"><b>Trading Wallet</b><div id="wallet"></div></div>
<div class="card"><b>Profit Reserve</b><div id="reserve"></div></div>
<div class="card"><b>Broker Cash</b><div id="cash"></div></div>
</div>
<h2>Runtime state</h2>
<pre id="payload">{escaped}</pre>
<script>
const state={payload};
document.getElementById("action").textContent=state.system_action;
document.getElementById("wallet").textContent=state.wallet;
document.getElementById("reserve").textContent=state.profit_reserve;
document.getElementById("cash").textContent=state.broker_cash;
</script>
</body>
</html>"""


class DashboardServer:
    """Local read-only HTTP dashboard. No trading commands are exposed."""

    def __init__(
        self,
        snapshot_provider: Callable[[], DashboardSnapshot],
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self.snapshot_provider = snapshot_provider
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None

    def _build_handler(self):
        provider = self.snapshot_provider

        class Handler(BaseHTTPRequestHandler):
            def _json(self, payload: object, status: int = 200) -> None:
                encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/api/state":
                    self._json(provider().as_dict())
                    return
                if path == "/api/health":
                    self._json({"health": list(provider().as_dict()["health"])})
                    return
                if path == "/":
                    encoded = render_dashboard_html(provider()).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                    return
                self._json({"error": "not found"}, status=404)

            def do_POST(self) -> None:
                self._json({"error": "dashboard is read-only"}, status=405)

            def log_message(self, *_args) -> None:
                return

        return Handler

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = ThreadingHTTPServer((self.host, self.port), self._build_handler())
        Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
