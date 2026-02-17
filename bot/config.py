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

    # Spike detection: $X move within Y seconds = real momentum
    spike_move_usd: float = float(os.getenv("SPIKE_MOVE_USD", "20.0"))
    spike_window_sec: float = float(os.getenv("SPIKE_WINDOW_SEC", "3.0"))

    # Confirmation: wait this many seconds after spike, then check BTC
    # still moved in the same direction. Filters out fake-outs.
    spike_confirm_sec: float = float(os.getenv("SPIKE_CONFIRM_SEC", "1.5"))

    # If gain hits 20%+, let it ride (moonbag) with a trailing stop at 10%
    moonbag_pct: float = float(os.getenv("MOONBAG_PCT", "20.0"))

    # Normal profit target: sell between 10-20%
    profit_target_pct: float = float(os.getenv("PROFIT_TARGET_PCT", "10.0"))

    # If position drops below this %, enter protection mode
    drawdown_trigger_pct: float = float(os.getenv("DRAWDOWN_TRIGGER_PCT", "-15.0"))

    # In protection mode, sell at this % (accept small loss to avoid big one)
    protection_exit_pct: float = float(os.getenv("PROTECTION_EXIT_PCT", "-10.0"))

    # Hard stop loss -- if position hits this, sell IMMEDIATELY no exceptions
    hard_stop_pct: float = float(os.getenv("HARD_STOP_PCT", "-25.0"))

    # Maximum USDC to risk per 5-minute window
    max_position_usdc: float = float(os.getenv("MAX_POSITION_USDC", "50.0"))

    # How often (seconds) to poll / check for spike + exit conditions
    poll_interval_sec: float = float(os.getenv("POLL_INTERVAL_SEC", "0.5"))

    # Paper-trading mode -- no real orders
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"

    # Binance WebSocket endpoint (public, no key needed)
    binance_ws_url: str = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    binance_rest_url: str = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"

    # Polymarket CLOB host
    poly_clob_host: str = "https://clob.polymarket.com"

    # Chain ID for Polygon mainnet
    chain_id: int = 137


cfg = Config()
