"""
Web server that streams live bot state to the React dashboard via WebSocket.

Serves:
  - WebSocket at /ws  -> pushes full JSON state every second
  - REST at /api/state -> snapshot
  - React SPA build at /
"""

import asyncio
import collections
import json
import logging
import time
from pathlib import Path
from typing import Set

from aiohttp import web

from bot.config import cfg

log = logging.getLogger("server")

BUILD_DIR = Path(__file__).resolve().parent.parent / "build"
MAX_PRICE_HISTORY = 120   # ~2 min of 1-second ticks
MAX_LOG_ENTRIES = 50


class DashboardServer:

    def __init__(self, feed, strategy, strategy2=None, strategy3=None, strategy4=None, host="0.0.0.0", port=8899):
        self._feed = feed
        self._strat = strategy
        self._strat2 = strategy2
        self._strat3 = strategy3
        self._strat4 = strategy4
        self._host = host
        self._port = port
        self._clients: Set[web.WebSocketResponse] = set()
        self._start_time = time.time()
        self._price_history = collections.deque(maxlen=MAX_PRICE_HISTORY)
        self._event_log = collections.deque(maxlen=MAX_LOG_ENTRIES)

        self._app = web.Application()
        self._app.router.add_get("/ws", self._ws_handler)
        self._app.router.add_get("/api/state", self._state_handler)
        if BUILD_DIR.exists():
            self._app.router.add_static("/static", str(BUILD_DIR / "static"))
            self._app.router.add_get("/{path:.*}", self._spa_handler)
        else:
            self._app.router.add_get("/", self._no_build_handler)

    # ── helpers ──

    def push_event(self, kind: str, msg: str):
        self._event_log.append({"ts": time.time(), "kind": kind, "msg": msg})

    def _record_price(self):
        if self._feed.current_price:
            self._price_history.append({
                "t": time.time(),
                "p": self._feed.current_price,
            })

    # ── state builder ──

    def _build_state(self) -> dict:
        feed = self._feed
        strat = self._strat
        s = strat.stats
        now = time.time()

        # BTC price history for sparkline
        prices = list(self._price_history)

        # Windows
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
                "move_pct": None,
                "time_left": None,
                "phase": "waiting",   # waiting | settling | active | closing | ended
            }
            if ws.window_open_price and feed.current_price:
                w["move_pct"] = round(
                    ((feed.current_price - ws.window_open_price) / ws.window_open_price) * 100, 4
                )
            if ws.market.window_end:
                w["time_left"] = max(0, ws.market.window_end - now)
            if ws.market.window_start:
                elapsed = now - ws.market.window_start
                remaining = (ws.market.window_end or 0) - now
                if elapsed < 0:
                    w["phase"] = "waiting"
                elif elapsed < 10:
                    w["phase"] = "settling"
                elif remaining < 20:
                    w["phase"] = "closing"
                elif remaining <= 0:
                    w["phase"] = "ended"
                else:
                    w["phase"] = "active"
            windows.append(w)

        # Positions with live P&L
        positions = []
        for pos in strat._open_positions:
            gain_pct = None
            current_val = None
            if pos.avg_entry > 0:
                gain_pct = round(pos.peak_gain, 2)
            positions.append({
                "side": pos.side,
                "entry": pos.avg_entry,
                "qty": pos.qty,
                "spent": round(pos.avg_entry * pos.qty, 2),
                "age": round(now - pos.entry_time),
                "protection_mode": pos.protection_mode,
                "moonbag_mode": pos.moonbag_mode,
                "peak_gain": round(pos.peak_gain, 2),
                "market": pos.market.question,
            })

        # Closed trades with extra detail
        closed = []
        for pos in strat._closed_positions[-30:]:
            entry_cost = pos.avg_entry * pos.qty
            closed.append({
                "side": pos.side,
                "entry": pos.avg_entry,
                "exit": pos.exit_price,
                "qty": pos.qty,
                "spent": round(entry_cost, 2),
                "pnl": round(pos.pnl, 2) if pos.pnl is not None else None,
                "pnl_pct": round(((pos.exit_price - pos.avg_entry) / pos.avg_entry) * 100, 1) if pos.exit_price and pos.avg_entry else None,
                "market": pos.market.question,
            })

        # Computed aggregates
        wins_pnl = [t["pnl"] for t in closed if t["pnl"] is not None and t["pnl"] >= 0]
        loss_pnl = [t["pnl"] for t in closed if t["pnl"] is not None and t["pnl"] < 0]
        total_trades = s.wins + s.losses
        win_rate = round((s.wins / total_trades) * 100, 1) if total_trades > 0 else 0

        return {
            "ts": now,
            "uptime": round(now - self._start_time),
            "btc_price": feed.current_price,
            "btc_live": feed.is_live,
            "price_history": prices,
            "config": {
                "spike_move_usd": cfg.spike_move_usd,
                "spike_window_sec": cfg.spike_window_sec,
                "profit_target": cfg.profit_target_pct,
                "moonbag": cfg.moonbag_pct,
                "drawdown_trigger": cfg.drawdown_trigger_pct,
                "protection_exit": cfg.protection_exit_pct,
                "hard_stop": cfg.hard_stop_pct,
                "max_position": cfg.max_position_usdc,
                "dry_run": cfg.dry_run,
            },
            "stats": {
                "signals": s.total_signals,
                "trades": s.total_trades,
                "exits": s.total_exits,
                "wins": s.wins,
                "losses": s.losses,
                "pnl": round(s.total_pnl, 2),
                "last_action": s.last_action,
                "win_rate": win_rate,
                "avg_win": round(sum(wins_pnl) / len(wins_pnl), 2) if wins_pnl else 0,
                "avg_loss": round(sum(loss_pnl) / len(loss_pnl), 2) if loss_pnl else 0,
                "best_trade": round(max(wins_pnl), 2) if wins_pnl else 0,
                "worst_trade": round(min(loss_pnl), 2) if loss_pnl else 0,
                "hourly_pnl": dict(s.hourly_pnl) if hasattr(s, 'hourly_pnl') else {},
            },
            "windows": windows,
            "positions": positions,
            "closed": closed,
            "events": list(self._event_log),
            "s2": self._build_s2_state(),
            "s3": self._build_s3_state(),
            "s4": self._build_s4_state(),
        }

    def _build_s2_state(self) -> dict:
        """Build strategy 2 (Momentum Pro) state."""
        if not self._strat2:
            return {"enabled": False}

        s2 = self._strat2
        st = s2.stats

        positions = []
        for p in s2.open_positions:
            positions.append({
                "side": p.side, "entry": p.avg_entry, "qty": p.qty,
                "age": round(time.time() - p.entry_time),
                "peak_gain": round(p.peak_gain, 2),
                "moonbag_mode": p.moonbag_mode,
                "market": p.market.question,
            })

        closed = []
        for p in s2.closed_positions[-20:]:
            closed.append({
                "side": p.side, "entry": p.avg_entry, "exit": p.exit_price,
                "qty": p.qty, "pnl": round(p.pnl, 2) if p.pnl is not None else None,
                "pnl_pct": round(((p.exit_price - p.avg_entry) / p.avg_entry) * 100, 1) if p.exit_price and p.avg_entry else None,
                "market": p.market.question,
            })

        total = st.wins + st.losses
        return {
            "enabled": True,
            "stats": {
                "signals": st.total_signals, "trades": st.total_trades,
                "exits": st.total_exits,
                "rejected_volume": getattr(st, 'rejected_volume', 0),
                "rejected_trend": getattr(st, 'rejected_trend', 0),
                "wins": st.wins, "losses": st.losses,
                "pnl": round(st.total_pnl, 2),
                "win_rate": round((st.wins / total) * 100, 1) if total > 0 else 0,
                "last_action": st.last_action,
                "hourly_pnl": dict(st.hourly_pnl),
            },
            "positions": positions,
            "closed": closed,
        }

    # ── handlers ──

    def _build_s3_state(self) -> dict:
        if not self._strat3:
            return {"enabled": False}

        s3 = self._strat3
        st = s3.stats

        positions = []
        for p in s3.open_positions:
            positions.append({
                "side": p.side, "entry": p.entry_price, "qty": p.qty,
                "spent": p.spent, "age": round(time.time() - p.entry_time),
                "market": p.market.question, "status": p.status,
            })

        closed = []
        for p in s3.closed_positions[-20:]:
            closed.append({
                "side": p.side, "entry": p.entry_price, "exit": p.exit_price,
                "qty": p.qty, "spent": p.spent,
                "pnl": round(p.pnl, 2) if p.pnl is not None else None,
                "pnl_pct": round(((p.exit_price - p.entry_price) / p.entry_price) * 100, 1) if p.exit_price and p.entry_price else None,
                "market": p.market.question, "status": p.status,
            })

        total = st.wins + st.losses
        return {
            "enabled": True,
            "stats": {
                "analyzed": st.markets_analyzed,
                "trades": st.trades,
                "skipped_choppy": st.skipped_choppy,
                "skipped_no_leader": st.skipped_no_leader,
                "wins": st.wins, "losses": st.losses,
                "pnl": round(st.total_pnl, 2),
                "win_rate": round((st.wins / total) * 100, 1) if total > 0 else 0,
                "last_action": st.last_action,
                "hourly_pnl": dict(st.hourly_pnl),
            },
            "positions": positions,
            "closed": closed,
        }

    def _build_s4_state(self) -> dict:
        if not self._strat4:
            return {"enabled": False}
        s4 = self._strat4
        st = s4.stats
        positions = []
        for p in s4.open_positions:
            positions.append({
                "side": p.side, "entry": p.avg_entry, "qty": p.qty,
                "age": round(time.time() - p.entry_time),
                "peak_gain": round(p.peak_gain, 2),
                "moonbag_mode": p.moonbag_mode,
                "market": p.market.question,
            })
        closed = []
        for p in s4.closed_positions[-20:]:
            closed.append({
                "side": p.side, "entry": p.avg_entry, "exit": p.exit_price,
                "qty": p.qty, "pnl": round(p.pnl, 2) if p.pnl is not None else None,
                "pnl_pct": round(((p.exit_price - p.avg_entry) / p.avg_entry) * 100, 1) if p.exit_price and p.avg_entry else None,
                "market": p.market.question,
            })
        total = st.wins + st.losses
        return {
            "enabled": True,
            "stats": {
                "signals": st.total_signals, "trades": st.total_trades,
                "exits": st.total_exits,
                "rejected_volume": getattr(st, 'rejected_volume', 0),
                "rejected_volatility": getattr(st, 'rejected_volatility', 0),
                "rejected_trend": getattr(st, 'rejected_trend', 0),
                "rejected_cooldown": getattr(st, 'rejected_cooldown', 0),
                "wins": st.wins, "losses": st.losses,
                "pnl": round(st.total_pnl, 2),
                "win_rate": round((st.wins / total) * 100, 1) if total > 0 else 0,
                "last_action": st.last_action,
                "hourly_pnl": dict(st.hourly_pnl),
            },
            "positions": positions, "closed": closed,
        }

    async def _spa_handler(self, request):
        req_path = request.match_info.get("path", "")
        file_path = BUILD_DIR / req_path
        if req_path and file_path.exists() and file_path.is_file():
            return web.FileResponse(file_path)
        return web.FileResponse(BUILD_DIR / "index.html")

    async def _no_build_handler(self, request):
        return web.Response(
            text="<h2>Run <code>npm run build</code> first, then restart the bot.</h2>",
            content_type="text/html",
        )

    async def _ws_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        log.info("Dashboard client connected (%d total)", len(self._clients))
        self.push_event("connect", "Dashboard client connected")
        try:
            async for msg in ws:
                pass
        finally:
            self._clients.discard(ws)
        return ws

    async def _state_handler(self, request):
        return web.json_response(self._build_state())

    async def _broadcast_loop(self):
        while True:
            self._record_price()
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
        self.push_event("start", f"Bot started (dry_run={cfg.dry_run})")
        await self._broadcast_loop()
