#!/usr/bin/env python3
"""
S3 Bot — Late Momentum Strategy

Usage:
    python -m bot.main                                              # test: dry run, port 9001
    python -m bot.main --port 9002 --live                           # official: live, port 9002
    python -m bot.main --port 9002 --live --trade-start 00:20 --trade-end 07:00  # with EST time window
"""

import argparse
import asyncio
import logging
import signal
import sys

from bot.config import cfg
from bot.polymarket import PolymarketClient
from bot.strategy3 import Strategy3


def setup_logging():
    fmt = "%(asctime)s [%(name)-12s] %(levelname)-7s %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, stream=sys.stdout)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def parse_time(s: str):
    """Parse 'HH:MM' → (hour, minute)."""
    parts = s.split(":")
    return int(parts[0]), int(parts[1])


async def main(port: int, live: bool, trade_start: str, trade_end: str):
    setup_logging()
    log = logging.getLogger("main")

    if live:
        cfg.dry_run = False
    else:
        cfg.dry_run = True

    trade_hours = None
    if trade_start and trade_end:
        sh, sm = parse_time(trade_start)
        eh, em = parse_time(trade_end)
        trade_hours = (sh, sm, eh, em)

    mode_str = "LIVE" if not cfg.dry_run else "DRY RUN"
    log.info("=" * 50)
    log.info("S3 Bot starting | %s | port %d", mode_str, port)
    if trade_hours:
        log.info("  Trading hours: %s → %s EST", trade_start, trade_end)
    log.info("=" * 50)

    poly = PolymarketClient()
    await poly.start()
    strat3 = Strategy3(poly, trade_hours=trade_hours)

    shutdown_event = asyncio.Event()

    def _shutdown(*_):
        log.info("Shutdown signal received")
        strat3.stop()
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig_name, _shutdown)
        except NotImplementedError:
            pass

    from bot.server import DashboardServer
    server = DashboardServer(strat3, host="0.0.0.0", port=port)

    tasks = [
        asyncio.create_task(strat3.run(), name="strategy-3"),
        asyncio.create_task(server.run(), name="dashboard"),
    ]

    log.info("Dashboard at http://0.0.0.0:%d", port)

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        await poly.stop()
        log.info("Bot stopped.")


def cli():
    parser = argparse.ArgumentParser(description="S3 Late Momentum Bot")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--live", action="store_true", help="Enable live trading (real money)")
    parser.add_argument("--trade-start", type=str, default="", help="Start time EST (HH:MM)")
    parser.add_argument("--trade-end", type=str, default="", help="End time EST (HH:MM)")
    args = parser.parse_args()
    asyncio.run(main(port=args.port, live=args.live,
                      trade_start=args.trade_start, trade_end=args.trade_end))


if __name__ == "__main__":
    cli()
