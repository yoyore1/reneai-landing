#!/usr/bin/env python3
"""
Binance-Polymarket 5-Minute BTC Arbitrage Bot
==============================================

Entry point.  Spins up concurrent tasks:
  1. Binance real-time price feed (WebSocket)
  2. Strategy engine (spike detection + order management)
  3. Terminal dashboard (Rich live display) OR web UI
  4. Web dashboard server (always on at http://localhost:8899)

Usage:
    python -m bot.main              # terminal dashboard + web UI
    python -m bot.main --headless   # logs only + web UI
    python -m bot.main --tunnel    # same + public URL for phone (anywhere)

Same WiFi: open http://<your-PC-IP>:8899 on your phone.
Outside: use --tunnel (requires cloudflared installed) and open the logged URL.
"""

import argparse
import asyncio
import logging
import re
import signal
import socket
import sys

from bot.config import cfg
from bot.time_util import datetime_est, write_daily_calendar
from bot.binance_feed import BinanceFeed
from bot.polymarket import PolymarketClient


class ESTFormatter(logging.Formatter):
    """Format log timestamps in EST."""

    def formatTime(self, record, datefmt=None):
        dt = datetime_est(record.created)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


def setup_logging(headless: bool):
    level = logging.INFO
    fmt = "%(asctime)s [%(name)-12s] %(levelname)-7s %(message)s"
    if headless:
        logging.basicConfig(level=level, format=fmt, stream=sys.stdout)
    else:
        logging.basicConfig(level=level, format=fmt, filename="bot.log", filemode="a")
    # All times in EST
    for h in logging.root.handlers:
        h.setFormatter(ESTFormatter(fmt))
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def _local_ip() -> str:
    """Best-effort local LAN IP for same-WiFi access."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


async def _run_tunnel(log, shutdown_event: asyncio.Event):
    """Start cloudflared quick tunnel to localhost:8899; log public URL and keep running."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "cloudflared", "tunnel", "--url", "http://127.0.0.1:8899",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        log.warning(
            "Tunnel skipped: cloudflared not found. Install it for access from outside WiFi: "
            "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
        )
        return
    url_match = re.compile(r"https://[a-zA-Z0-9.-]+\.trycloudflare\.com")
    try:
        if proc.stdout:
            while not shutdown_event.is_set():
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace")
                m = url_match.search(line_str)
                if m:
                    log.info("Dashboard from anywhere (phone, etc.): %s", m.group(0))
                    break
    except asyncio.TimeoutError:
        pass
    except Exception as e:
        log.warning("Tunnel read error: %s", e)
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=0.1)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()


async def main(headless: bool = False, tunnel: bool = False, s3_only: bool = False, live: bool = False):
    setup_logging(headless)
    log = logging.getLogger("main")
    s3_only = s3_only or getattr(cfg, "s3_only", False)
    if live:
        cfg.dry_run = False
        log.info("--live flag: forcing LIVE mode (real Polymarket trades)")

    write_daily_calendar("daily_calendar_EST.txt", days=7)
    log.info("📅 Daily calendar (EST) written to daily_calendar_EST.txt")

    log.info("=" * 60)
    if s3_only:
        log.info("LATE BOT ONLY (S3) — Strategy 3 only")
        log.info("  dry_run     = %s", cfg.dry_run)
        log.info("  >>> %s <<<", "LIVE MODE — real Polymarket orders" if not cfg.dry_run else "DRY RUN — simulated only, no real orders")
        start_h, start_m = getattr(cfg, "s3_trade_start_hour_est", 0), getattr(cfg, "s3_trade_start_minute_est", 0)
        log.info("  trade window= %d:%02d–%d:00 EST", start_h, start_m, getattr(cfg, "s3_trade_end_hour_est", 5))
        log.info("  daily target= $%.0f (stop new trades when daily PnL >= this)", getattr(cfg, "s3_daily_profit_target_usdc", 100))
        log.info("  size/trade  = $%.0f", getattr(cfg, "s3_usdc_per_trade", 30))
    else:
        log.info("Binance-Polymarket Arbitrage Bot starting")
        log.info("  dry_run        = %s", cfg.dry_run)
        log.info("  spike          = $%.0f in %.0fs", cfg.spike_move_usd, cfg.spike_window_sec)
        log.info("  profit_target  = %.1f%%", cfg.profit_target_pct)
        log.info("  max_position   = $%.2f", cfg.max_position_usdc)
        log.info("  daily_loss_limit = $%.2f (0 = off)", cfg.daily_loss_limit_usdc)
    log.info("=" * 60)

    feed = BinanceFeed()
    poly = PolymarketClient()
    await poly.start()

    strat = strat2 = strat4 = None
    strat3 = None
    from bot.strategy3 import Strategy3
    strat3 = Strategy3(poly, feed)

    if not s3_only:
        from bot.strategy import Strategy
        strat = Strategy(feed, poly)
        from bot.strategy2 import Strategy2
        strat2 = Strategy2(poly)
        from bot.strategy4 import Strategy4
        strat4 = Strategy4(poly, feed)

    shutdown_event = asyncio.Event()

    def _shutdown(*_):
        log.info("Shutdown signal received")
        feed.stop()
        if strat:
            strat.stop()
        if strat2:
            strat2.stop()
        strat3.stop()
        if strat4:
            strat4.stop()
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig_name, _shutdown)
        except NotImplementedError:
            pass

    tasks = [
        asyncio.create_task(feed.run(), name="binance-feed"),
        asyncio.create_task(strat3.run(), name="strategy-3"),
    ]
    if strat:
        tasks.append(asyncio.create_task(strat.run(), name="strategy-1"))
    if strat2:
        tasks.append(asyncio.create_task(strat2.run(), name="strategy-2"))
    if strat4:
        tasks.append(asyncio.create_task(strat4.run(), name="strategy-4"))

    from bot.server import DashboardServer
    server = DashboardServer(feed, poly, strat, strat2, strat3, strat4, on_shutdown=_shutdown)
    tasks.append(asyncio.create_task(server.run(), name="web-dashboard"))
    log.info("Web dashboard: http://localhost:8899")
    local_ip = _local_ip()
    if local_ip:
        log.info("On phone (same WiFi): http://%s:8899", local_ip)
    if tunnel or cfg.use_tunnel:
        tasks.append(asyncio.create_task(_run_tunnel(log, shutdown_event), name="tunnel"))

    if not headless and strat:
        from bot.dashboard import run_dashboard
        tasks.append(asyncio.create_task(run_dashboard(feed, strat), name="dashboard"))
    else:
        async def status_printer():
            while not shutdown_event.is_set():
                s = strat3.stats
                px = f"${feed.current_price:,.2f}" if feed.current_price else "n/a"
                print(
                    f"[S3] BTC={px}  trades={s.trades}  PnL=${s.total_pnl:+.2f}  day=${getattr(s,'daily_pnl',0):+.2f}  "
                    f"last={s.last_action or '-'}",
                    flush=True,
                )
                await asyncio.sleep(30)
        tasks.append(asyncio.create_task(status_printer(), name="status"))

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        await poly.stop()
        log.info("Bot stopped.")


def cli():
    parser = argparse.ArgumentParser(description="Binance-Polymarket BTC 5-min Arbitrage Bot")
    parser.add_argument("--headless", action="store_true", help="Run without the terminal dashboard")
    parser.add_argument("--tunnel", action="store_true", help="Create public URL for dashboard (phone from anywhere; needs cloudflared)")
    parser.add_argument("--s3-only", action="store_true", help="Run only the late bot (S3); 12am–5am EST, $30/trade, stop at daily profit target")
    parser.add_argument("--live", action="store_true", help="Force LIVE mode (real Polymarket orders); overrides DRY_RUN env")
    args = parser.parse_args()
    asyncio.run(main(headless=args.headless, tunnel=args.tunnel or cfg.use_tunnel, s3_only=args.s3_only or cfg.s3_only, live=args.live))


if __name__ == "__main__":
    cli()
