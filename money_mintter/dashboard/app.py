from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from typing import Callable, Any
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    system_action: str
    investing_wallet: float
    profit_wallet: float
    broker_cash: float
    analyzed_stocks: tuple[dict[str, object], ...]
    open_positions: tuple[dict[str, object], ...]
    trade_history: tuple[dict[str, object], ...]
    health: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "system_action": self.system_action,
            "investing_wallet": self.investing_wallet,
            "profit_wallet": self.profit_wallet,
            "broker_cash": self.broker_cash,
            "analyzed_stocks": list(self.analyzed_stocks),
            "open_positions": list(self.open_positions),
            "trade_history": list(self.trade_history),
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
<title>Money Mintter Control Room & Dashboard</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem;background:#f8f9fa;color:#333}}
h1,h2{{color:#1a1a1a}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem;margin-bottom:2rem}}
.card{{background:#fff;border:1px solid #dee2e6;border-radius:10px;padding:1.2rem;box-shadow:0 2px 4px rgba(0,0,0,0.05)}}
.card b{{color:#555;font-size:0.9rem;text-transform:uppercase}}
.card .val{{font-size:1.5rem;font-weight:bold;margin-top:0.5rem;color:#007bff}}
.section{{background:#fff;border:1px solid #dee2e6;border-radius:10px;padding:1.5rem;margin-bottom:1.5rem;box-shadow:0 2px 4px rgba(0,0,0,0.05)}}
table{{width:100%;border-collapse:collapse;margin-top:0.5rem}}
th,td{{padding:0.75rem;border-bottom:1px solid #dee2e6;text-align:left;font-size:0.95rem}}
th{{background:#f1f3f5}}
</style>
</head>
<body>
<h1>Money Mintter Control Room</h1>
<p>Autonomous Intraday Trading Engine & Wallet Management System (>85% Elite Standard)</p>

<div class="grid">
<div class="card"><b>System Status</b><div class="val" id="action" style="font-size:1.2rem">{snapshot.system_action}</div></div>
<div class="card"><b>Investing Wallet</b><div class="val" id="wallet">${snapshot.investing_wallet:.2f}</div></div>
<div class="card"><b>Profit Wallet (Cashflow)</b><div class="val" id="reserve" style="color:#28a745">${snapshot.profit_wallet:.2f}</div></div>
<div class="card"><b>Broker Cash</b><div class="val" id="cash">${snapshot.broker_cash:.2f}</div></div>
</div>

<div class="section">
<h2>Currently Analyzed Stocks (Top 5 Candidates)</h2>
<table>
<thead><tr><th>Symbol</th><th>Market</th><th>Liquidity</th><th>Activity</th><th>Signal Quality</th><th>Score</th></tr></thead>
<tbody id="stocksTable"><tr><td colspan="6">Loading...</td></tr></tbody>
</table>
</div>

<div class="section">
<h2>Open Positions / Active Trades</h2>
<table>
<thead><tr><th>Symbol</th><th>Side</th><th>Quantity</th><th>Entry Price</th><th>Stop Price</th><th>Target Price</th><th>Partial Taken</th></tr></thead>
<tbody id="openTable"><tr><td colspan="7">No open positions</td></tr></tbody>
</table>
</div>

<div class="section">
<h2>Trade History</h2>
<table>
<thead><tr><th>Symbol</th><th>Side</th><th>Quantity</th><th>Entry</th><th>Exit</th><th>Realized P&L</th><th>Reason</th></tr></thead>
<tbody id="historyTable"><tr><td colspan="7">No trade history yet</td></tr></tbody>
</table>
</div>

<script>
async function refreshState() {{
  try {{
    const res = await fetch('/api/state');
    const data = await res.json();
    document.getElementById("action").textContent = data.system_action;
    document.getElementById("wallet").textContent = "$" + data.investing_wallet.toFixed(2);
    document.getElementById("reserve").textContent = "$" + data.profit_wallet.toFixed(2);
    document.getElementById("cash").textContent = "$" + data.broker_cash.toFixed(2);

    // Analyzed Stocks
    const stocksTb = document.getElementById("stocksTable");
    stocksTb.innerHTML = "";
    if (data.analyzed_stocks && data.analyzed_stocks.length > 0) {{
      data.analyzed_stocks.forEach(s => {{
        stocksTb.innerHTML += `<tr><td><b>${{s.symbol}}</b></td><td>${{s.market}}</td><td>${{s.liquidity_score.toFixed(2)}}</td><td>${{s.activity_score.toFixed(2)}}</td><td>${{s.signal_quality_score.toFixed(2)}}</td><td>${{s.score.toFixed(2)}}</td></tr>`;
      }});
    }} else {{
      stocksTb.innerHTML = `<tr><td colspan="6">No stocks currently under deep analysis</td></tr>`;
    }}

    // Open Positions
    const openTb = document.getElementById("openTable");
    openTb.innerHTML = "";
    if (data.open_positions && data.open_positions.length > 0) {{
      data.open_positions.forEach(p => {{
        openTb.innerHTML += `<tr><td><b>${{p.symbol}}</b></td><td>${{p.side}}</td><td>${{p.quantity}}</td><td>$${{p.entry_price.toFixed(2)}}</td><td>$${{p.stop_price.toFixed(2)}}</td><td>$${{p.target_price.toFixed(2)}}</td><td>${{p.partial_taken ? 'Yes' : 'No'}}</td></tr>`;
      }});
    }} else {{
      openTb.innerHTML = `<tr><td colspan="7">No open positions</td></tr>`;
    }}

    // Trade History
    const histTb = document.getElementById("historyTable");
    histTb.innerHTML = "";
    if (data.trade_history && data.trade_history.length > 0) {{
      data.trade_history.forEach(h => {{
        const pnlColor = h.realized_pnl >= 0 ? 'green' : 'red';
        histTb.innerHTML += `<tr><td><b>${{h.symbol}}</b></td><td>${{h.side}}</td><td>${{h.quantity}}</td><td>$${{h.entry_price.toFixed(2)}}</td><td>${{h.exit_price ? h.exit_price.toFixed(2) : '-'}}</td><td style="color:${{pnlColor}}">$${{h.realized_pnl ? h.realized_pnl.toFixed(2) : '0.00'}}</td><td>${{h.close_reason}}</td></tr>`;
      }});
    }} else {{
      histTb.innerHTML = `<tr><td colspan="7">No trade history yet</td></tr>`;
    }}
  }} catch (e) {{
    console.error(e);
  }}
}}

refreshState();
setInterval(refreshState, 2000);
</script>
</body>
</html>"""


class DashboardServer:
    """Local read-only HTTP dashboard server for live monitoring & control room."""

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
                self._json({"error": "dashboard is read-only monitoring control room"}, status=405)

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
