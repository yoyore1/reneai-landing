#!/usr/bin/env python3
"""
S3 Bot — Late Momentum Strategy

Usage:
    python -m bot.main                                                          # test: dry run, port 9001
    python -m bot.main --port 9002 --live --trade-start 00:20 --trade-end 07:00 # official: live, time-restricted
"""

import argparse
import asyncio
import logging
import signal
import sys

from bot.config import cfg
from bot.polymarket import PolymarketClient
from bot.strategy3 import Strategy3
from bot.pnl_store import PnLStore

def _get_strategy_class(name: str):
    if name == "vol":
        from bot.strategy3_vol import Strategy3Vol
        return Strategy3Vol
    if name in ("scalp", "scalp_predict"):
        from bot.strategy_scalp import StrategyScalp
        return StrategyScalp
    if name == "v2":
        from bot.strategy3_v2 import Strategy3V2
        return Strategy3V2
    if name == "perfected":
        from bot.strategy_perfected import StrategyPerfected
        return StrategyPerfected
    if name == "elite":
        from bot.strategy_elite import StrategyElite
        return StrategyElite
    if name == "edge":
        from bot.strategy_edge import StrategyEdge
        return StrategyEdge
    if name in ("mg", "guard"):
        from bot.strategy3_mg import Strategy3MG
        return Strategy3MG
    if name in ("mg2", "mg2r"):
        from bot.strategy3_mg import Strategy3MG
        return Strategy3MG
    return Strategy3


def setup_logging():
    fmt = "%(asctime)s [%(name)-12s] %(levelname)-7s %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, stream=sys.stdout)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def parse_time(s: str):
    parts = s.split(":")
    return int(parts[0]), int(parts[1])


async def main(port: int, live: bool, trade_start: str, trade_end: str, pnl_file: str, strategy: str = "", bot_name: str = "test", skip_no_leader: bool = True, sl_price: float = None):
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

    pnl_store = PnLStore(pnl_file)
    email_on_loss = live and bool(cfg.email_to)

    mode_str = "LIVE" if not cfg.dry_run else "DRY RUN"
    log.info("=" * 50)
    log.info("S3 Bot starting | %s | port %d", mode_str, port)
    if trade_hours:
        log.info("  Trading hours: %s -> %s EST", trade_start, trade_end)
    log.info("  PnL file: %s", pnl_file)
    if email_on_loss:
        log.info("  Loss emails -> %s", cfg.email_to)
    log.info("=" * 50)

    StratClass = _get_strategy_class(strategy)
    poly = PolymarketClient()
    await poly.start()

    kwargs = dict(trade_hours=trade_hours, pnl_store=pnl_store, email_on_loss=email_on_loss)
    if StratClass is Strategy3:
        kwargs["bot_name"] = bot_name
        kwargs["skip_no_leader"] = skip_no_leader
        if sl_price is not None:
            kwargs["sl_price"] = sl_price

    from bot.strategy_scalp import StrategyScalp
    if StratClass is StrategyScalp:
        kwargs["bot_name"] = bot_name
        if strategy == "scalp_predict":
            kwargs["flip_size"] = 20.0

    from bot.strategy_elite import StrategyElite
    from bot.strategy_perfected import StrategyPerfected
    from bot.strategy_edge import StrategyEdge
    if StratClass is StrategyPerfected:
        kwargs["bot_name"] = bot_name

    if StratClass is StrategyElite:
        kwargs["bot_name"] = bot_name

    if StratClass is StrategyEdge:
        kwargs["bot_name"] = bot_name

    from bot.strategy3_mg import Strategy3MG
    if StratClass is Strategy3MG:
        kwargs["bot_name"] = bot_name
        kwargs["skip_no_leader"] = skip_no_leader
        if sl_price is not None:
            kwargs["sl_price"] = sl_price
        if strategy == "guard":
            kwargs["guard_config"] = {
                "win_streak_threshold": 999,
                "alternation_threshold": 5,
                "choppy_rate_threshold": 0.40,
                "cooldown_markets": 1,
            }
        if strategy == "mg":
            kwargs["guard_config"] = {
                "win_streak_threshold": 999,
                "alternation_threshold": 5,
                "choppy_rate_threshold": 0.40,
                "cooldown_markets": 1,
            }
            kwargs["flip_on_guard"] = True
            kwargs["flip_size"] = 40.0
            kwargs["flip_sl"] = 0.10
            from bot.market_health import MarketHealthMonitor
            kwargs["health_monitor"] = MarketHealthMonitor(
                bot_name=bot_name, skip_threshold=-2)
        if strategy in ("mg2", "mg2r"):
            gc = {
                "win_streak_threshold": 999,
                "alternation_threshold": 5,
                "choppy_rate_threshold": 0.40,
                "cooldown_markets": 1,
            }
            if strategy == "mg2":
                gc["sister_bot"] = "mg2"
            kwargs["guard_config"] = gc
            kwargs["entry_gate"] = 0.78
            if strategy == "mg2r":
                kwargs["reversal_exit_threshold"] = 0.35
            kwargs["flip_on_guard"] = True
            kwargs["flip_size"] = 40.0
            kwargs["flip_sl"] = 0.10
    strat3 = StratClass(poly, **kwargs)

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
    server = DashboardServer(strat3, pnl_store=pnl_store, host="0.0.0.0", port=port)

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
    parser.add_argument("--live", action="store_true", help="Enable live trading")
    parser.add_argument("--trade-start", type=str, default="", help="Start time EST (HH:MM)")
    parser.add_argument("--trade-end", type=str, default="", help="End time EST (HH:MM)")
    parser.add_argument("--pnl-file", type=str, default="pnl_data.json", help="PnL data file")
    parser.add_argument("--strategy", type=str, default="", help="Strategy variant (e.g. 'vol')")
    parser.add_argument("--bot-name", type=str, default="test", help="Bot name for trade history (test/official)")
    parser.add_argument("--no-skip-noleader", action="store_true", help="Disable skip-no-leader (buy even when no side hits 70c)")
    parser.add_argument("--sl", type=float, default=None, help="Stop loss price (e.g. 0.45 for 45c)")
    args = parser.parse_args()
    asyncio.run(main(port=args.port, live=args.live,
                      trade_start=args.trade_start, trade_end=args.trade_end,
                      pnl_file=args.pnl_file, strategy=args.strategy,
                      bot_name=args.bot_name,
                      skip_no_leader=not args.no_skip_noleader,
                      sl_price=args.sl))


if __name__ == "__main__":
    cli()
