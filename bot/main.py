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
    python -m bot.main --web-only   # web UI only (no terminal dashboard)
"""

import argparse
import asyncio
import logging
import signal
import sys

from bot.config import cfg
from bot.binance_feed import BinanceFeed
from bot.polymarket import PolymarketClient
from bot.strategy import Strategy


def setup_logging(headless: bool):
    level = logging.INFO
    fmt = "%(asctime)s [%(name)-12s] %(levelname)-7s %(message)s"
    if headless:
        logging.basicConfig(level=level, format=fmt, stream=sys.stdout)
    else:
        # When the dashboard is active, log to file so it doesn't mess up the TUI
        logging.basicConfig(level=level, format=fmt, filename="bot.log", filemode="a")
    # Suppress noisy libraries
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def main(headless: bool = False):
    setup_logging(headless)
    log = logging.getLogger("main")

    log.info("=" * 60)
    log.info("Binance-Polymarket Arbitrage Bot starting")
    log.info("  dry_run        = %s", cfg.dry_run)
    log.info("  spike          = $%.0f in %.0fs", cfg.spike_move_usd, cfg.spike_window_sec)
    log.info("  profit_target  = %.1f%%", cfg.profit_target_pct)
    log.info("  max_position   = $%.2f", cfg.max_position_usdc)
    log.info("=" * 60)

    # --- Initialise components ---
    feed = BinanceFeed()
    poly = PolymarketClient()
    await poly.start()
    strat = Strategy(feed, poly)

    # Strategy 2: passive limit orders
    from bot.strategy2 import Strategy2
    strat2 = Strategy2(poly)

    # Strategy 3: late momentum — buy the leader at 1:00 remaining
    from bot.strategy3 import Strategy3
    strat3 = Strategy3(poly, feed)

    # Strategy 4: Buy both sides arb (feed used for resolution display: which side won)
    from bot.strategy4 import Strategy4
    strat4 = Strategy4(poly, feed)

    # --- Graceful shutdown ---
    shutdown_event = asyncio.Event()

    def _shutdown(*_):
        log.info("Shutdown signal received")
        feed.stop()
        strat.stop()
        strat2.stop()
        strat3.stop()
        strat4.stop()
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig_name, _shutdown)
        except NotImplementedError:
            pass  # Windows

    # --- Build task list ---
    tasks = [
        asyncio.create_task(feed.run(), name="binance-feed"),
        asyncio.create_task(strat.run(), name="strategy-1"),
        asyncio.create_task(strat2.run(), name="strategy-2"),
        asyncio.create_task(strat3.run(), name="strategy-3"),
        asyncio.create_task(strat4.run(), name="strategy-4"),
    ]

    # Web dashboard server (always runs)
    from bot.server import DashboardServer
    server = DashboardServer(feed, strat, strat2, strat3, strat4)
    tasks.append(asyncio.create_task(server.run(), name="web-dashboard"))
    log.info("Web dashboard will be at http://localhost:8899")

    if not headless:
        from bot.dashboard import run_dashboard
        tasks.append(asyncio.create_task(run_dashboard(feed, strat), name="dashboard"))
    else:
        async def status_printer():
            while not shutdown_event.is_set():
                s = strat.stats
                px = f"${feed.current_price:,.2f}" if feed.current_price else "n/a"
                print(
                    f"[STATUS] BTC={px}  signals={s.total_signals}  "
                    f"trades={s.total_trades}  PnL=${s.total_pnl:+.2f}  "
                    f"last={s.last_action or '-'}",
                    flush=True,
                )
                await asyncio.sleep(30)
        tasks.append(asyncio.create_task(status_printer(), name="status"))

    # --- Run until shutdown ---
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
    args = parser.parse_args()
    asyncio.run(main(headless=args.headless))


if __name__ == "__main__":
    cli()
