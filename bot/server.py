"""
Web server that streams live bot state to the React dashboard via WebSocket.

Runs alongside the strategy as an asyncio task.
Serves:
  - WebSocket at /ws  → pushes JSON state every second
  - Static React build at /  (if available)
"""

import asyncio
import json
import logging
import time
from typing import Set

from aiohttp import web

log = logging.getLogger("server")


class DashboardServer:
    """Lightweight aiohttp server that exposes bot state over WebSocket."""

    def __init__(self, feed, strategy, host="0.0.0.0", port=8899):
        self._feed = feed
        self._strategy = strategy
        self._host = host
        self._port = port
        self._clients: Set[web.WebSocketResponse] = set()
        self._app = web.Application()
        self._app.router.add_get("/ws", self._ws_handler)
        self._app.router.add_get("/api/state", self._state_handler)
        # Serve React build if it exists
        self._app.router.add_static("/", "build", show_index=True)

    def _build_state(self) -> dict:
        """Assemble full bot state as a JSON-serializable dict."""
        feed = self._feed
        strat = self._strategy
        s = strat.stats

        windows = []
        for cid, ws in list(strat._windows.items()):
            w = {
                "id": cid[:12],
                "question": ws.market.question,
                "open_price": ws.window_open_price,
                "window_start": ws.market.window_start,
                "window_end": ws.market.window_end,
                "signal_fired": ws.signal_fired,
                "signal_side": ws.signal_side,
            }
            if ws.window_open_price and feed.current_price:
                w["move_pct"] = round(
                    ((feed.current_price - ws.window_open_price) / ws.window_open_price) * 100, 4
                )
            else:
                w["move_pct"] = None

            # Time left
            if ws.market.window_end:
                w["time_left"] = max(0, ws.market.window_end - time.time())
            else:
                w["time_left"] = None

            windows.append(w)

        positions = []
        for pos in strat._open_positions:
            positions.append({
                "side": pos.side,
                "entry": pos.avg_entry,
                "qty": pos.qty,
                "age": round(time.time() - pos.entry_time),
                "protection_mode": pos.protection_mode,
                "moonbag_mode": pos.moonbag_mode,
                "peak_gain": round(pos.peak_gain, 2),
                "market": pos.market.question[:50],
            })

        closed = []
        for pos in strat._closed_positions[-20:]:
            closed.append({
                "side": pos.side,
                "entry": pos.avg_entry,
                "exit": pos.exit_price,
                "qty": pos.qty,
                "pnl": round(pos.pnl, 2) if pos.pnl is not None else None,
                "market": pos.market.question[:50],
            })

        return {
            "ts": time.time(),
            "btc_price": feed.current_price,
            "btc_live": feed.is_live,
            "stats": {
                "signals": s.total_signals,
                "trades": s.total_trades,
                "exits": s.total_exits,
                "wins": s.wins,
                "losses": s.losses,
                "pnl": round(s.total_pnl, 2),
                "last_action": s.last_action,
            },
            "windows": windows,
            "positions": positions,
            "closed": closed,
        }

    async def _ws_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        log.info("Dashboard client connected (%d total)", len(self._clients))
        try:
            async for msg in ws:
                pass  # client doesn't send us anything
        finally:
            self._clients.discard(ws)
            log.info("Dashboard client disconnected (%d total)", len(self._clients))
        return ws

    async def _state_handler(self, request):
        return web.json_response(self._build_state())

    async def _broadcast_loop(self):
        """Push state to all connected WebSocket clients every second."""
        while True:
            if self._clients:
                state = json.dumps(self._build_state())
                dead = set()
                for ws in self._clients:
                    try:
                        await ws.send_str(state)
                    except Exception:
                        dead.add(ws)
                self._clients -= dead
            await asyncio.sleep(1)

    async def run(self):
        runner = web.AppRunner(self._app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        log.info("Dashboard server running at http://%s:%d", self._host, self._port)
        await self._broadcast_loop()
