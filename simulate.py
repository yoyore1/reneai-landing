#!/usr/bin/env python3
"""
Simulation: Run the Binance-Polymarket arbitrage strategy against the
next 3 real "Bitcoin Up or Down - 5 min" markets on Polymarket.

Connects to:
  - Binance (via binance.us REST) for live BTC/USDT price
  - Polymarket Gamma API for market discovery
  - Polymarket CLOB for real-time order book data

All trades are simulated (paper). No API keys required.
"""

import asyncio
import json
import time
import sys
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional, List

import aiohttp

# ───────────────────────────── config ─────────────────────────────

SPIKE_THRESHOLD_PCT = 0.08       # BTC must move this % from window open
PROFIT_TARGET_PCT   = 10.0       # sell when position is up this %
MAX_POSITION_USDC   = 50.0       # paper spend per trade
POLL_SEC            = 2.0        # how often we tick (seconds)
BINANCE_PRICE_URL   = "https://api.binance.us/api/v3/ticker/price?symbol=BTCUSDT"
GAMMA_API           = "https://gamma-api.polymarket.com"
CLOB_HOST           = "https://clob.polymarket.com"

# ───────────────────────────── types ──────────────────────────────

@dataclass
class Market:
    question: str
    condition_id: str
    up_token: str
    down_token: str
    window_start_utc: datetime
    window_end_utc: datetime

@dataclass
class SimPosition:
    market: Market
    side: str            # "Up" or "Down"
    token_id: str
    entry_price: float
    qty: float
    entry_time: float
    usdc_spent: float
    exit_price: Optional[float] = None
    exit_time: Optional[float] = None
    pnl: Optional[float] = None
    exit_reason: str = ""

# ──────────────────────── helpers ─────────────────────────────────

C_RESET  = "\033[0m"
C_BOLD   = "\033[1m"
C_GREEN  = "\033[92m"
C_RED    = "\033[91m"
C_YELLOW = "\033[93m"
C_CYAN   = "\033[96m"
C_DIM    = "\033[2m"

def ts(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S UTC")

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def log(msg: str):
    stamp = now_utc().strftime("%H:%M:%S")
    print(f"{C_DIM}[{stamp}]{C_RESET} {msg}", flush=True)

# ──────────────────── Binance price ───────────────────────────────

async def get_btc_price(session: aiohttp.ClientSession) -> Optional[float]:
    try:
        async with session.get(BINANCE_PRICE_URL, timeout=aiohttp.ClientTimeout(total=5)) as r:
            data = await r.json()
            return float(data["price"])
    except Exception as e:
        log(f"{C_RED}Binance price error: {e}{C_RESET}")
        return None

# ──────────────── Polymarket discovery ────────────────────────────

async def discover_markets(session: aiohttp.ClientSession) -> List[Market]:
    """Find all upcoming BTC Up or Down 5-min markets."""
    url = f"{GAMMA_API}/events"
    params = {
        "limit": "50",
        "order": "startDate",
        "ascending": "false",
        "closed": "false",
        "tag": "crypto",
    }
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        events = await r.json()

    markets = []
    for ev in events:
        slug = ev.get("slug", "")
        if "btc-updown-5m" not in slug:
            continue
        for m in ev.get("markets", []):
            raw_tokens = m.get("clobTokenIds", "[]")
            tokens = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
            if len(tokens) < 2:
                continue

            end_str = m.get("endDate", "")
            if not end_str:
                continue
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            start_dt = end_dt - timedelta(minutes=5)

            markets.append(Market(
                question=m["question"],
                condition_id=m.get("conditionId", ""),
                up_token=tokens[0],
                down_token=tokens[1],
                window_start_utc=start_dt,
                window_end_utc=end_dt,
            ))

    markets.sort(key=lambda x: x.window_start_utc)
    return markets

# ──────────────── Polymarket order book ───────────────────────────

async def get_best_ask(session: aiohttp.ClientSession, token_id: str) -> Optional[float]:
    """Return the cheapest ask price for a token."""
    try:
        url = f"{CLOB_HOST}/book"
        async with session.get(url, params={"token_id": token_id}, timeout=aiohttp.ClientTimeout(total=5)) as r:
            book = await r.json()
        asks = book.get("asks", [])
        if asks:
            return min(float(a["price"]) for a in asks)
    except Exception as e:
        log(f"{C_RED}Ask fetch error: {e}{C_RESET}")
    return None

async def get_best_bid(session: aiohttp.ClientSession, token_id: str) -> Optional[float]:
    """Return the best bid price for a token."""
    try:
        url = f"{CLOB_HOST}/book"
        async with session.get(url, params={"token_id": token_id}, timeout=aiohttp.ClientTimeout(total=5)) as r:
            book = await r.json()
        bids = book.get("bids", [])
        if bids:
            return max(float(b["price"]) for b in bids)
    except Exception as e:
        log(f"{C_RED}Bid fetch error: {e}{C_RESET}")
    return None

async def get_full_book(session: aiohttp.ClientSession, token_id: str) -> dict:
    try:
        url = f"{CLOB_HOST}/book"
        async with session.get(url, params={"token_id": token_id}, timeout=aiohttp.ClientTimeout(total=5)) as r:
            return await r.json()
    except:
        return {"asks": [], "bids": []}

# ─────────────────── simulation engine ────────────────────────────

async def simulate_window(session: aiohttp.ClientSession, market: Market, window_num: int) -> Optional[SimPosition]:
    """
    Simulate one 5-minute window:
      1. Wait for window to start
      2. Record BTC open price from Binance
      3. Poll for spike + Polymarket book
      4. If spike detected -> simulated BUY
      5. Poll for 10% exit or window end
    """
    print()
    print(f"{C_BOLD}{C_CYAN}{'='*70}")
    print(f"  WINDOW {window_num}/3: {market.question}")
    print(f"  Start: {ts(market.window_start_utc)}  |  End: {ts(market.window_end_utc)}")
    print(f"{'='*70}{C_RESET}")
    print()

    # ---- Wait for window start ----
    wait_secs = (market.window_start_utc - now_utc()).total_seconds()
    if wait_secs > 0:
        log(f"Waiting {wait_secs:.0f}s for window to open...")
        # Show countdown every 30s
        while (market.window_start_utc - now_utc()).total_seconds() > 0:
            remaining = (market.window_start_utc - now_utc()).total_seconds()
            if remaining > 60:
                log(f"  ... {remaining:.0f}s until window opens")
                await asyncio.sleep(min(30, remaining))
            else:
                await asyncio.sleep(min(5, remaining))
    elif (market.window_end_utc - now_utc()).total_seconds() < 0:
        log(f"{C_RED}Window already ended, skipping{C_RESET}")
        return None

    # ---- Record open price ----
    open_price = await get_btc_price(session)
    if open_price is None:
        log(f"{C_RED}Could not get BTC price at window open!{C_RESET}")
        return None

    log(f"{C_BOLD}Window OPEN  |  BTC = ${open_price:,.2f}{C_RESET}")

    # Fetch initial Polymarket book state
    up_ask = await get_best_ask(session, market.up_token)
    down_ask = await get_best_ask(session, market.down_token)
    log(f"Polymarket book  |  Up ask: ${up_ask or 0:.3f}  |  Down ask: ${down_ask or 0:.3f}")

    # ---- Poll for spike ----
    position: Optional[SimPosition] = None
    signal_fired = False

    while now_utc() < market.window_end_utc:
        btc = await get_btc_price(session)
        if btc is None:
            await asyncio.sleep(POLL_SEC)
            continue

        move_pct = ((btc - open_price) / open_price) * 100

        # Color the move
        if move_pct > 0:
            mv_str = f"{C_GREEN}+{move_pct:.4f}%{C_RESET}"
        else:
            mv_str = f"{C_RED}{move_pct:.4f}%{C_RESET}"

        time_left = (market.window_end_utc - now_utc()).total_seconds()

        if not signal_fired:
            log(f"BTC ${btc:,.2f}  move={mv_str}  left={time_left:.0f}s")

            if abs(move_pct) >= SPIKE_THRESHOLD_PCT:
                # SIGNAL!
                side = "Up" if move_pct > 0 else "Down"
                token_id = market.up_token if side == "Up" else market.down_token
                direction = "UP" if side == "Up" else "DOWN"
                color = C_GREEN if side == "Up" else C_RED

                print()
                log(f"{C_BOLD}{color}*** SIGNAL: BTC moved {move_pct:+.4f}% → BUY {side.upper()} ***{C_RESET}")

                # Get current Polymarket ask for the winning side
                ask = await get_best_ask(session, token_id)
                if ask is None or ask <= 0 or ask >= 0.99:
                    log(f"{C_YELLOW}No good ask available (ask={ask}), using model price{C_RESET}")
                    # Model: at 50/50 start, a 0.1% BTC move should push true prob to ~60-70%
                    # But poly market lags, so the ask is still near 0.50-0.55
                    ask = 0.55 if abs(move_pct) < 0.2 else 0.60

                qty = MAX_POSITION_USDC / ask
                position = SimPosition(
                    market=market,
                    side=side,
                    token_id=token_id,
                    entry_price=ask,
                    qty=qty,
                    entry_time=time.time(),
                    usdc_spent=MAX_POSITION_USDC,
                )
                signal_fired = True

                log(f"{C_BOLD}SIMULATED BUY: {qty:.2f} {side} shares @ ${ask:.3f} (${MAX_POSITION_USDC:.2f} spent){C_RESET}")

                # Also show the full book snapshot
                book = await get_full_book(session, token_id)
                asks_str = "  ".join(f"${a['price']}x{a['size']}" for a in book.get("asks", [])[:3])
                bids_str = "  ".join(f"${b['price']}x{b['size']}" for b in book.get("bids", [])[:3])
                log(f"  Book asks: {asks_str or 'empty'}")
                log(f"  Book bids: {bids_str or 'empty'}")
                print()

        if position and position.exit_price is None:
            # Check for exit
            bid = await get_best_bid(session, position.token_id)
            if bid and bid > 0:
                gain_pct = ((bid - position.entry_price) / position.entry_price) * 100
                gain_color = C_GREEN if gain_pct >= 0 else C_RED
                log(f"  Position: {position.side} @ ${position.entry_price:.3f} | bid=${bid:.3f} | {gain_color}P&L={gain_pct:+.1f}%{C_RESET} | left={time_left:.0f}s")

                if gain_pct >= PROFIT_TARGET_PCT:
                    position.exit_price = bid
                    position.exit_time = time.time()
                    position.pnl = (bid - position.entry_price) * position.qty
                    position.exit_reason = f"TARGET HIT (+{gain_pct:.1f}%)"
                    print()
                    log(f"{C_BOLD}{C_GREEN}*** SELL: {position.qty:.2f} shares @ ${bid:.3f} | PnL: ${position.pnl:+.2f} ({gain_pct:+.1f}%) ***{C_RESET}")
                    print()
                    return position

        await asyncio.sleep(POLL_SEC)

    # ---- Window ended ----
    if position and position.exit_price is None:
        # Check final resolution
        final_btc = await get_btc_price(session)
        final_move = ((final_btc - open_price) / open_price) * 100 if final_btc else 0
        winning_side = "Up" if final_move >= 0 else "Down"

        if position.side == winning_side:
            # We won! Shares resolve to $1.00
            position.exit_price = 1.0
            position.pnl = (1.0 - position.entry_price) * position.qty
            position.exit_reason = f"RESOLVED WIN (BTC {final_move:+.4f}%)"
        else:
            # We lost. Shares resolve to $0.00
            position.exit_price = 0.0
            position.pnl = -position.usdc_spent
            position.exit_reason = f"RESOLVED LOSS (BTC {final_move:+.4f}%)"

        position.exit_time = time.time()
        color = C_GREEN if position.pnl >= 0 else C_RED
        print()
        log(f"{C_BOLD}{color}*** WINDOW END: {position.exit_reason} | PnL: ${position.pnl:+.2f} ***{C_RESET}")
        log(f"  BTC close: ${final_btc:,.2f} vs open: ${open_price:,.2f} ({final_move:+.4f}%)")
        print()

    elif not position:
        final_btc = await get_btc_price(session)
        final_move = ((final_btc - open_price) / open_price) * 100 if final_btc else 0
        log(f"{C_YELLOW}No signal this window (max move below {SPIKE_THRESHOLD_PCT}% threshold){C_RESET}")
        log(f"  BTC close: ${final_btc:,.2f} vs open: ${open_price:,.2f} ({final_move:+.4f}%)")
        print()

    return position


# ─────────────────────── main ─────────────────────────────────────

async def main():
    print()
    print(f"{C_BOLD}{C_CYAN}╔══════════════════════════════════════════════════════════════╗")
    print(f"║     Binance-Polymarket BTC 5-Min Arbitrage Simulation      ║")
    print(f"║                                                            ║")
    print(f"║  Using REAL Binance prices + REAL Polymarket order books   ║")
    print(f"║  All trades are SIMULATED (paper)                         ║")
    print(f"╚══════════════════════════════════════════════════════════════╝{C_RESET}")
    print()
    print(f"  Spike threshold: {SPIKE_THRESHOLD_PCT}%")
    print(f"  Profit target:   {PROFIT_TARGET_PCT}%")
    print(f"  Position size:   ${MAX_POSITION_USDC:.2f}")
    print(f"  Current time:    {ts(now_utc())}")
    print()

    async with aiohttp.ClientSession() as session:
        # Get current BTC price
        btc = await get_btc_price(session)
        if btc:
            log(f"Current BTC/USDT: {C_BOLD}${btc:,.2f}{C_RESET}")
        else:
            log(f"{C_RED}Cannot reach Binance -- aborting{C_RESET}")
            return

        # Discover markets
        log("Discovering BTC 5-min markets on Polymarket...")
        all_markets = await discover_markets(session)
        log(f"Found {len(all_markets)} total BTC 5-min markets")

        # Filter to upcoming windows (not yet ended)
        now = now_utc()
        upcoming = [m for m in all_markets if m.window_end_utc > now]

        if not upcoming:
            log(f"{C_YELLOW}No upcoming 5-min windows found right now.")
            log(f"Markets are available during trading hours (typically 9:30 AM - 4 PM ET).")
            log(f"Found {len(all_markets)} markets for next session.{C_RESET}")
            if all_markets:
                log(f"Next window starts: {ts(all_markets[0].window_start_utc)}")
                # Run against the first 3 anyway as a forward-looking sim
                upcoming = all_markets[:3]
                log(f"{C_YELLOW}Running forward simulation against next 3 scheduled markets...{C_RESET}")
            else:
                return

        # Pick next 3
        target_markets = upcoming[:3]
        print()
        log(f"{C_BOLD}Target markets:{C_RESET}")
        for i, m in enumerate(target_markets, 1):
            log(f"  {i}. {m.question}")
            log(f"     {ts(m.window_start_utc)} → {ts(m.window_end_utc)}")

        # Run simulation for each
        results: List[Optional[SimPosition]] = []
        for i, market in enumerate(target_markets, 1):
            pos = await simulate_window(session, market, i)
            results.append(pos)

        # ── Summary ──
        print()
        print(f"{C_BOLD}{C_CYAN}{'='*70}")
        print(f"  SIMULATION SUMMARY")
        print(f"{'='*70}{C_RESET}")
        print()

        total_pnl = 0.0
        total_trades = 0
        wins = 0
        losses = 0

        for i, pos in enumerate(results, 1):
            mkt = target_markets[i - 1]
            print(f"  {C_BOLD}Window {i}: {mkt.question}{C_RESET}")
            if pos:
                total_trades += 1
                pnl = pos.pnl or 0
                total_pnl += pnl
                color = C_GREEN if pnl >= 0 else C_RED
                if pnl >= 0:
                    wins += 1
                else:
                    losses += 1
                print(f"    Side:   {pos.side}")
                print(f"    Entry:  ${pos.entry_price:.3f}")
                print(f"    Exit:   ${pos.exit_price:.3f}" if pos.exit_price is not None else "    Exit:   pending")
                print(f"    Qty:    {pos.qty:.2f} shares")
                print(f"    Spent:  ${pos.usdc_spent:.2f}")
                print(f"    {color}PnL:    ${pnl:+.2f}{C_RESET}")
                print(f"    Reason: {pos.exit_reason}")
            else:
                print(f"    {C_DIM}No trade (threshold not reached){C_RESET}")
            print()

        pnl_color = C_GREEN if total_pnl >= 0 else C_RED
        print(f"  {C_BOLD}─── TOTALS ───{C_RESET}")
        print(f"  Trades:   {total_trades}")
        print(f"  Wins:     {wins}")
        print(f"  Losses:   {losses}")
        print(f"  {pnl_color}{C_BOLD}Total PnL: ${total_pnl:+.2f}{C_RESET}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
