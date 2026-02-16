"""
Live terminal dashboard using Rich.

Shows real-time BTC price, active signals, open positions,
and cumulative P&L in a clean, auto-refreshing table.
"""

import asyncio
import time
from datetime import datetime, timezone

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from bot.binance_feed import BinanceFeed
from bot.strategy import Strategy
from bot.config import cfg


def _ts(epoch: float) -> str:
    if epoch <= 0:
        return "--"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%H:%M:%S")


def build_dashboard(feed: BinanceFeed, strat: Strategy) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=5),
    )
    layout["body"].split_row(
        Layout(name="left", ratio=1),
        Layout(name="right", ratio=1),
    )

    # --- Header ---
    mode = "[bold red]LIVE TRADING[/]" if not cfg.dry_run else "[bold yellow]DRY RUN[/]"
    price_str = f"${feed.current_price:,.2f}" if feed.current_price else "waiting..."
    price_color = "green" if feed.is_live else "red"
    header_text = (
        f"  BTC/USDT: [{price_color}]{price_str}[/]  |  "
        f"Mode: {mode}  |  "
        f"Spike threshold: {cfg.spike_threshold_pct}%  |  "
        f"Profit target: {cfg.profit_target_pct}%  |  "
        f"Max position: ${cfg.max_position_usdc}"
    )
    layout["header"].update(Panel(header_text, title="[bold cyan]Binance-Polymarket Arbitrage Bot[/]", border_style="cyan"))

    # --- Left panel: tracked windows ---
    win_table = Table(title="Active 5-Min Windows", box=box.SIMPLE_HEAVY, expand=True)
    win_table.add_column("Market", style="white", max_width=45)
    win_table.add_column("Open $", justify="right", style="yellow")
    win_table.add_column("Move %", justify="right")
    win_table.add_column("Signal", justify="center")
    win_table.add_column("Ends", justify="right", style="dim")

    for cid, ws in list(strat._windows.items()):
        open_px = f"${ws.window_open_price:,.2f}" if ws.window_open_price else "--"
        if ws.window_open_price and feed.current_price:
            mv = ((feed.current_price - ws.window_open_price) / ws.window_open_price) * 100
            mv_str = f"[{'green' if mv >= 0 else 'red'}]{mv:+.3f}%[/]"
        else:
            mv_str = "--"
        sig = ""
        if ws.signal_fired:
            color = "green" if ws.signal_side == "YES" else "red"
            sig = f"[bold {color}]{ws.signal_side}[/]"
        ends = _ts(ws.market.window_end)
        win_table.add_row(ws.market.question[:44], open_px, mv_str, sig, ends)

    layout["left"].update(Panel(win_table, border_style="blue"))

    # --- Right panel: positions ---
    pos_table = Table(title="Open Positions", box=box.SIMPLE_HEAVY, expand=True)
    pos_table.add_column("Side", style="bold")
    pos_table.add_column("Qty", justify="right")
    pos_table.add_column("Entry", justify="right", style="yellow")
    pos_table.add_column("Curr Bid", justify="right")
    pos_table.add_column("P&L %", justify="right")
    pos_table.add_column("Age", justify="right", style="dim")

    for pos in strat._open_positions:
        age_s = int(time.time() - pos.entry_time)
        age_str = f"{age_s}s"
        pos_table.add_row(
            pos.side,
            f"{pos.qty:.2f}",
            f"${pos.avg_entry:.4f}",
            "--",
            "--",
            age_str,
        )

    # Recent closed
    closed_table = Table(title="Recent Exits", box=box.SIMPLE_HEAVY, expand=True)
    closed_table.add_column("Side")
    closed_table.add_column("Entry", justify="right")
    closed_table.add_column("Exit", justify="right")
    closed_table.add_column("PnL", justify="right")
    for pos in strat._closed_positions[-5:]:
        pnl_str = f"${pos.pnl:+.2f}" if pos.pnl is not None else "--"
        pnl_style = "green" if (pos.pnl or 0) >= 0 else "red"
        closed_table.add_row(
            pos.side,
            f"${pos.avg_entry:.4f}",
            f"${pos.exit_price:.4f}" if pos.exit_price else "--",
            f"[{pnl_style}]{pnl_str}[/]",
        )

    right_layout = Layout()
    right_layout.split_column(
        Layout(pos_table, ratio=1),
        Layout(closed_table, ratio=1),
    )
    layout["right"].update(Panel(right_layout, border_style="magenta"))

    # --- Footer ---
    s = strat.stats
    footer_text = (
        f"  Signals: {s.total_signals}  |  "
        f"Trades: {s.total_trades}  |  "
        f"Exits: {s.total_exits}  |  "
        f"Wins: {s.wins}  Losses: {s.losses}  |  "
        f"Total PnL: [{'green' if s.total_pnl >= 0 else 'red'}]${s.total_pnl:+.2f}[/]  |  "
        f"Last: {s.last_action or 'waiting...'}"
    )
    layout["footer"].update(Panel(footer_text, title="Stats", border_style="green"))

    return layout


async def run_dashboard(feed: BinanceFeed, strat: Strategy):
    """Refresh the dashboard every second."""
    console = Console()
    with Live(build_dashboard(feed, strat), console=console, refresh_per_second=2, screen=True) as live:
        while True:
            live.update(build_dashboard(feed, strat))
            await asyncio.sleep(0.5)
