"""
Configuration loaded from environment / .env file.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Polymarket CLOB credentials
    poly_api_key: str = os.getenv("POLY_API_KEY", "")
    poly_api_secret: str = os.getenv("POLY_API_SECRET", "")
    poly_api_passphrase: str = os.getenv("POLY_API_PASSPHRASE", "")
    poly_private_key: str = os.getenv("POLY_PRIVATE_KEY", "")

    # Spike detection: $X consistent move within Y seconds = real momentum
    # Uses midpoint check (no delay) — price must move consistently, not V-shape
    spike_move_usd: float = float(os.getenv("SPIKE_MOVE_USD", "15.0"))
    spike_window_sec: float = float(os.getenv("SPIKE_WINDOW_SEC", "2.0"))

    # If gain hits 15%+, let it ride (moonbag) with dynamic trailing stop
    moonbag_pct: float = float(os.getenv("MOONBAG_PCT", "15.0"))

    # Normal profit target: sell between 5-20%
    profit_target_pct: float = float(os.getenv("PROFIT_TARGET_PCT", "5.0"))

    # If position drops below this %, enter protection mode
    drawdown_trigger_pct: float = float(os.getenv("DRAWDOWN_TRIGGER_PCT", "-15.0"))

    # In protection mode, sell at this % (accept small loss to avoid big one)
    protection_exit_pct: float = float(os.getenv("PROTECTION_EXIT_PCT", "-10.0"))

    # Hard stop loss -- if position hits this, sell IMMEDIATELY no exceptions (S1: -50% to avoid liquidation)
    # Use -40 or -30 to cut losses sooner (set HARD_STOP_PCT in env).
    hard_stop_pct: float = float(os.getenv("HARD_STOP_PCT", "-50.0"))

    # Maximum USDC to risk per 5-minute window
    max_position_usdc: float = float(os.getenv("MAX_POSITION_USDC", "50.0"))

    # Daily loss limit (EST): if strategy daily P&L <= this, no new trades until next day. Set 0 to disable.
    daily_loss_limit_usdc: float = float(os.getenv("DAILY_LOSS_LIMIT_USDC", "-100.0"))

    # Per-trade dollar cap: exit any position once unrealized loss reaches this $ (stops one trade blowing up)
    max_loss_per_trade_usdc: float = float(os.getenv("MAX_LOSS_PER_TRADE_USDC", "12.0"))

    # After this many consecutive losses, pause new entries for N minutes (cool off, avoid revenge trading)
    consecutive_losses_to_pause: int = int(os.getenv("CONSECUTIVE_LOSSES_TO_PAUSE", "2"))
    pause_minutes_after_streak: float = float(os.getenv("PAUSE_MINUTES_AFTER_STREAK", "25.0"))

    # S1: don't buy if ask is below this (e.g. 20c — avoid junk odds)
    s1_min_buy_cents: float = float(os.getenv("S1_MIN_BUY_CENTS", "0.20"))

    # How often (seconds) to poll / check for spike + exit conditions
    poll_interval_sec: float = float(os.getenv("POLL_INTERVAL_SEC", "0.5"))

    # Paper-trading mode -- no real orders
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"

    # Create public URL for dashboard (phone from anywhere) via cloudflared. Set USE_TUNNEL=true or use --tunnel.
    use_tunnel: bool = os.getenv("USE_TUNNEL", "false").lower() == "true"

    # Binance WebSocket endpoint (public, no key needed)
    binance_ws_url: str = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    binance_rest_url: str = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"

    # Polymarket CLOB host
    poly_clob_host: str = "https://clob.polymarket.com"

    # Chain ID for Polygon mainnet
    chain_id: int = 137


cfg = Config()
