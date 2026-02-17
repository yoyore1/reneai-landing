#!/usr/bin/env python3
"""
LIVE simulation against the next 3 real Polymarket BTC 5-min markets.

Uses:
  - Binance US REST for real-time BTC/USDT price
  - Polymarket CLOB for real order book data
  - Polymarket Gamma API for market discovery

All trades are paper (simulated). No keys needed.
"""

import asyncio
import collections
import json
import time
import sys
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional, List, Tuple

import aiohttp

# ───── config ─────
SPIKE_MOVE_USD       = 20.0     # BTC must move this many $ ...
SPIKE_WINDOW_SEC     = 3.0      # ... within this many seconds
MOONBAG_PCT          = 20.0     # if gain hits this, let it ride
PROFIT_TARGET_PCT    = 10.0     # normal sell target / trailing stop floor
DRAWDOWN_TRIGGER_PCT = -15.0    # enter protection mode if we drop this far
PROTECTION_EXIT_PCT  = -10.0    # in protection mode, sell here to cut losses
HARD_STOP_PCT        = -25.0    # absolute floor — sell immediately, no exceptions
MAX_POSITION_USDC    = 50.0
POLL_SEC             = 1.5

BINANCE_URL = "https://api.binance.us/api/v3/ticker/price?symbol=BTCUSDT"
GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB        = "https://clob.polymarket.com"

# ───── colors ─────
R = "\033[0m"; B = "\033[1m"; G = "\033[92m"; RD = "\033[91m"
Y = "\033[93m"; C = "\033[96m"; D = "\033[2m"

def ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def log(msg):
    print(f"{D}[{ts()}]{R} {msg}", flush=True)

# ───── data ─────
@dataclass
class Mkt:
    question: str
    cond_id: str
    up_tok: str
    down_tok: str
    start: datetime
    end: datetime

@dataclass
class Pos:
    side: str
    token: str
    entry: float
    qty: float
    spent: float
    t_entry: float
    exit_px: Optional[float] = None
    pnl: Optional[float] = None
    reason: str = ""
    protection_mode: bool = False   # True once we drop past DRAWDOWN_TRIGGER
    moonbag_mode: bool = False      # True once gain hits 20%+, trailing stop at 10%
    peak_gain: float = 0.0         # highest gain ever seen
    worst_pnl: float = 0.0         # track worst drawdown

# ───── API calls ─────
async def btc_price(s: aiohttp.ClientSession) -> Optional[float]:
    try:
        async with s.get(BINANCE_URL, timeout=aiohttp.ClientTimeout(total=4)) as r:
            d = await r.json()
            return float(d["price"])
    except Exception as e:
        log(f"{RD}Binance err: {e}{R}")
        return None

async def best_ask(s: aiohttp.ClientSession, tok: str) -> Tuple[Optional[float], str]:
    try:
        async with s.get(f"{CLOB}/book", params={"token_id": tok}, timeout=aiohttp.ClientTimeout(total=4)) as r:
            bk = await r.json()
        asks = sorted(bk.get("asks", []), key=lambda a: float(a["price"]))
        if asks:
            return float(asks[0]["price"]), f"${asks[0]['price']}x{asks[0]['size']}"
        return None, "empty"
    except Exception as e:
        return None, str(e)

async def best_bid(s: aiohttp.ClientSession, tok: str) -> Tuple[Optional[float], str]:
    try:
        async with s.get(f"{CLOB}/book", params={"token_id": tok}, timeout=aiohttp.ClientTimeout(total=4)) as r:
            bk = await r.json()
        bids = sorted(bk.get("bids", []), key=lambda b: -float(b["price"]))
        if bids:
            return float(bids[0]["price"]), f"${bids[0]['price']}x{bids[0]['size']}"
        return None, "empty"
    except Exception as e:
        return None, str(e)

async def full_book_str(s: aiohttp.ClientSession, tok: str) -> str:
    try:
        async with s.get(f"{CLOB}/book", params={"token_id": tok}, timeout=aiohttp.ClientTimeout(total=4)) as r:
            bk = await r.json()
        asks = sorted(bk.get("asks",[]), key=lambda a: float(a["price"]))[:5]
        bids = sorted(bk.get("bids",[]), key=lambda b: -float(b["price"]))[:5]
        a = "  ".join(f"${x['price']}x{x['size']}" for x in asks)
        b = "  ".join(f"${x['price']}x{x['size']}" for x in bids)
        return f"asks[{a}] bids[{b}]"
    except:
        return "err"

# ───── discover 3 markets by slug ─────
async def get_target_markets(s: aiohttp.ClientSession, epochs: List[int]) -> List[Mkt]:
    mkts = []
    for ep in epochs:
        slug = f"btc-updown-5m-{ep}"
        url = f"{GAMMA_API}/events?slug={slug}"
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            data = await r.json()
        if not data:
            continue
        ev = data[0]
        for m in ev.get("markets", []):
            raw = m.get("clobTokenIds", "[]")
            toks = json.loads(raw) if isinstance(raw, str) else raw
            if len(toks) < 2:
                continue
            end_str = m.get("endDate", "")
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            start_dt = end_dt - timedelta(minutes=5)
            mkts.append(Mkt(
                question=m["question"],
                cond_id=m.get("conditionId", ""),
                up_tok=toks[0], down_tok=toks[1],
                start=start_dt, end=end_dt,
            ))
    return mkts

# ───── simulate one window ─────
async def sim_window(s: aiohttp.ClientSession, mkt: Mkt, num: int) -> Optional[Pos]:
    print()
    print(f"{B}{C}{'='*72}")
    print(f"  WINDOW {num}/3: {mkt.question}")
    print(f"  {mkt.start.strftime('%H:%M:%S')} → {mkt.end.strftime('%H:%M:%S')} UTC")
    print(f"{'='*72}{R}")

    now = datetime.now(timezone.utc)

    # wait for window start
    wait = (mkt.start - now).total_seconds()
    if wait > 0:
        log(f"Waiting {wait:.0f}s for window to open...")
        while (mkt.start - datetime.now(timezone.utc)).total_seconds() > 0:
            left = (mkt.start - datetime.now(timezone.utc)).total_seconds()
            if left > 10:
                log(f"  {left:.0f}s to go...")
                await asyncio.sleep(min(10, left))
            else:
                await asyncio.sleep(min(1, left))

    # ── Window is open — wait 10s before tracking ──
    open_px = await btc_price(s)
    if not open_px:
        log(f"{RD}No BTC price, skipping window{R}")
        return None

    log(f"{B}WINDOW OPEN | BTC = ${open_px:,.2f}{R}")
    log(f"{Y}Waiting 10s before trading (letting market settle)...{R}")
    await asyncio.sleep(10)

    # Re-read BTC price after the 10s wait — this is our real baseline
    open_px = await btc_price(s) or open_px
    log(f"{B}READY | BTC baseline = ${open_px:,.2f}{R}")

    # snapshot Polymarket books
    for label, tok in [("Up", mkt.up_tok), ("Down", mkt.down_tok)]:
        bk = await full_book_str(s, tok)
        log(f"  {label} book: {bk}")

    pos: Optional[Pos] = None
    tick = 0
    # Rolling price buffer for spike detection: (timestamp, price)
    price_buf = collections.deque(maxlen=100)

    while datetime.now(timezone.utc) < mkt.end:
        px = await btc_price(s)
        if not px:
            await asyncio.sleep(POLL_SEC)
            continue

        now_t = time.time()
        price_buf.append((now_t, px))

        move = ((px - open_px) / open_px) * 100
        mv_c = G if move >= 0 else RD
        left = (mkt.end - datetime.now(timezone.utc)).total_seconds()
        tick += 1

        # Detect spike: find price from SPIKE_WINDOW_SEC ago in buffer
        spike_delta = None
        cutoff = now_t - SPIKE_WINDOW_SEC
        for buf_t, buf_px in price_buf:
            if buf_t >= cutoff:
                spike_delta = px - buf_px
                break
        if spike_delta is not None and abs(spike_delta) < SPIKE_MOVE_USD:
            spike_delta = None  # not big enough

        if pos is None:
            # ── Waiting for signal ──
            spike_str = f"  3s-move=${spike_delta:+.0f}" if spike_delta else ""
            log(f"BTC ${px:,.2f}  {mv_c}{move:+.4f}%{R}  left={left:.0f}s{spike_str}")

            # Also check if the Polymarket book has tightened
            if tick % 3 == 0:
                up_a, up_a_s = await best_ask(s, mkt.up_tok)
                dn_a, dn_a_s = await best_ask(s, mkt.down_tok)
                log(f"  Poly asks: Up={up_a_s}  Down={dn_a_s}")

            if spike_delta is not None and left > 20:
                side = "Up" if spike_delta > 0 else "Down"
                tok = mkt.up_tok if side == "Up" else mkt.down_tok
                col = G if side == "Up" else RD

                print()
                log(f"{B}{col}*** SPIKE: ${spike_delta:+.0f} in {SPIKE_WINDOW_SEC}s → BUY {side.upper()} ***{R}")

                # Get real ask
                ask, ask_s = await best_ask(s, tok)
                bk_s = await full_book_str(s, tok)
                log(f"  {side} full book: {bk_s}")

                if ask is None or ask > 0.90:
                    modeled = 0.52 + (abs(spike_delta) / 100) * 0.5
                    modeled = min(modeled, 0.65)
                    log(f"{Y}  Book too wide (ask={ask_s}). Using modeled fill @ ${modeled:.3f}{R}")
                    ask = modeled

                qty = MAX_POSITION_USDC / ask

                pos = Pos(
                    side=side, token=tok,
                    entry=ask, qty=qty,
                    spent=MAX_POSITION_USDC,
                    t_entry=time.time(),
                )
                log(f"{B}SIMULATED BUY: {qty:.2f} {side} shares @ ${ask:.3f} (${MAX_POSITION_USDC:.2f}){R}")
                print()

        else:
            # ── Have position, watch for exit ──
            bid_px, bid_s = await best_bid(s, pos.token)

            # Model bid if book is thin
            if bid_px is None or bid_px < 0.05:
                elapsed = time.time() - pos.t_entry
                catchup = min(1.0, elapsed / 90.0)
                true_val = 0.55 + abs(move) * 0.8
                true_val = min(true_val, 0.95)
                modeled_bid = pos.entry + (true_val - pos.entry) * catchup
                if move * (1 if pos.side == "Up" else -1) < 0:
                    modeled_bid = pos.entry * (1.0 + move * 0.02)
                bid_px = max(modeled_bid, 0.01)
                bid_s = f"${bid_px:.3f}(model)"

            gain = ((bid_px - pos.entry) / pos.entry) * 100
            gc = G if gain >= 0 else RD

            # Track peak and worst
            if gain > pos.peak_gain:
                pos.peak_gain = gain
            if gain < pos.worst_pnl:
                pos.worst_pnl = gain

            # ── Mode transitions ──

            # Moonbag: if gain ever hits 20%+, let it ride with a 10% trailing stop
            if not pos.moonbag_mode and not pos.protection_mode and gain >= MOONBAG_PCT:
                pos.moonbag_mode = True
                print()
                log(f"{B}{G}*** MOONBAG MODE: gain hit {gain:+.1f}%! Letting it ride ***{R}")
                log(f"{G}  Trailing stop at +{PROFIT_TARGET_PCT}% (will sell if it drops back to +{PROFIT_TARGET_PCT}%){R}")
                print()

            # Protection: if gain drops past -15%, switch to damage control
            if not pos.protection_mode and gain <= DRAWDOWN_TRIGGER_PCT:
                pos.protection_mode = True
                pos.moonbag_mode = False  # override moonbag if somehow both
                print()
                log(f"{B}{RD}*** PROTECTION MODE: dropped to {gain:+.1f}% (trigger={DRAWDOWN_TRIGGER_PCT}%) ***{R}")
                log(f"{Y}  Will sell when position recovers to {PROTECTION_EXIT_PCT}%{R}")
                print()

            # Status label
            mode_str = ""
            if pos.moonbag_mode:
                mode_str = f" {G}[MOONBAG peak={pos.peak_gain:+.1f}%]{R}"
            elif pos.protection_mode:
                mode_str = f" {Y}[PROTECT worst={pos.worst_pnl:+.1f}%]{R}"

            log(f"  {pos.side} pos: entry=${pos.entry:.3f} bid={bid_s} {gc}P&L={gain:+.1f}%{R}{mode_str}  BTC={mv_c}{move:+.4f}%{R}  left={left:.0f}s")

            # ── Exit decisions ──

            # HARD STOP — no exceptions
            if gain <= HARD_STOP_PCT:
                pos.exit_px = bid_px
                pos.pnl = (bid_px - pos.entry) * pos.qty
                pos.reason = f"HARD STOP {gain:+.1f}%"
                print()
                log(f"{B}{RD}*** HARD STOP SELL @ ${bid_px:.3f} | PnL: ${pos.pnl:+.2f} ({gain:+.1f}%) ***{R}")
                log(f"  Emergency exit — hit {HARD_STOP_PCT}% floor")
                print()
                return pos

            elif pos.moonbag_mode:
                # Was above 20%, now trailing stop: sell if drops to 10%
                if gain <= PROFIT_TARGET_PCT:
                    pos.exit_px = bid_px
                    pos.pnl = (bid_px - pos.entry) * pos.qty
                    pos.reason = f"MOONBAG STOP +{gain:.1f}% (peak was +{pos.peak_gain:.1f}%)"
                    print()
                    log(f"{B}{G}*** TRAILING STOP SELL @ ${bid_px:.3f} | PnL: ${pos.pnl:+.2f} ({gain:+.1f}%) ***{R}")
                    log(f"  Peak was +{pos.peak_gain:.1f}%, sold on pullback to +{gain:.1f}%")
                    print()
                    return pos

            elif pos.protection_mode:
                # Was below -15%, sell when recovers to -10%
                if gain >= PROTECTION_EXIT_PCT:
                    pos.exit_px = bid_px
                    pos.pnl = (bid_px - pos.entry) * pos.qty
                    pos.reason = f"PROTECTION EXIT {gain:+.1f}% (worst was {pos.worst_pnl:+.1f}%)"
                    print()
                    log(f"{B}{Y}*** PROTECTION SELL @ ${bid_px:.3f} | PnL: ${pos.pnl:+.2f} ({gain:+.1f}%) ***{R}")
                    log(f"  Took small loss to avoid bigger one (worst was {pos.worst_pnl:+.1f}%)")
                    print()
                    return pos

            else:
                # Normal mode: between 10% and 20% → sell
                if gain >= PROFIT_TARGET_PCT:
                    pos.exit_px = bid_px
                    pos.pnl = (bid_px - pos.entry) * pos.qty
                    pos.reason = f"PROFIT +{gain:.1f}%"
                    print()
                    log(f"{B}{G}*** SELL @ ${bid_px:.3f} | PnL: ${pos.pnl:+.2f} ({gain:+.1f}%) ***{R}")
                    print()
                    return pos

        await asyncio.sleep(POLL_SEC)

    # ── Window ended ──
    final = await btc_price(s)
    final_move = ((final - open_px) / open_px) * 100 if final else 0
    winner = "Up" if final_move >= 0 else "Down"

    if pos and pos.exit_px is None:
        if pos.side == winner:
            pos.exit_px = 1.0
            pos.pnl = (1.0 - pos.entry) * pos.qty
            pos.reason = f"WIN (resolved ${final:,.2f}, {final_move:+.4f}%)"
        else:
            pos.exit_px = 0.0
            pos.pnl = -pos.spent
            pos.reason = f"LOSS (resolved ${final:,.2f}, {final_move:+.4f}%)"
        col = G if pos.pnl >= 0 else RD
        print()
        log(f"{B}{col}*** WINDOW END: {pos.reason} | PnL: ${pos.pnl:+.2f} ***{R}")
        log(f"  Open: ${open_px:,.2f} → Close: ${final:,.2f} ({final_move:+.4f}%)")
    elif not pos:
        log(f"{Y}No signal (BTC stayed within {SPIKE_THRESHOLD_PCT}% threshold){R}")
        log(f"  Open: ${open_px:,.2f} → Close: ${final:,.2f} ({final_move:+.4f}%)")

    print()
    return pos

# ───── main ─────
async def main():
    print()
    print(f"{B}{C}╔════════════════════════════════════════════════════════════════════╗")
    print(f"║       Binance x Polymarket BTC 5-Min Arbitrage — LIVE SIM        ║")
    print(f"║                                                                  ║")
    print(f"║   Real Binance prices  |  Real Polymarket books  |  Paper trades ║")
    print(f"╚════════════════════════════════════════════════════════════════════╝{R}")
    print()

    # Auto-detect next 3 upcoming 5-min windows based on current time
    now_ts = int(datetime.now(timezone.utc).timestamp())
    # Polymarket 5-min windows align to 5-min boundaries (epoch % 300 == 0)
    current_slot = (now_ts // 300) * 300
    # Start from the next upcoming slot
    next_slot = current_slot + 300
    target_epochs = [next_slot, next_slot + 300, next_slot + 600]
    
    for i, ep in enumerate(target_epochs):
        dt = datetime.fromtimestamp(ep, tz=timezone.utc)
        et = dt - timedelta(hours=5)
        log(f"  Target {i+1}: {et.strftime('%I:%M %p')} ET (epoch {ep})")

    async with aiohttp.ClientSession() as s:
        px = await btc_price(s)
        log(f"BTC/USDT now: {B}${px:,.2f}{R}" if px else f"{RD}Cannot reach Binance{R}")

        log("Fetching target markets from Polymarket...")
        mkts = await get_target_markets(s, target_epochs)
        log(f"Got {len(mkts)} markets:")
        for i, m in enumerate(mkts, 1):
            log(f"  {i}. {m.question}  ({m.start.strftime('%H:%M')}-{m.end.strftime('%H:%M')} UTC)")

        print()
        log(f"Config: spike=${SPIKE_MOVE_USD:.0f}/{SPIKE_WINDOW_SEC:.0f}s  sell=+{PROFIT_TARGET_PCT}-{MOONBAG_PCT}%  moonbag=+{MOONBAG_PCT}%(stop@+{PROFIT_TARGET_PCT}%)  protect={DRAWDOWN_TRIGGER_PCT}%(exit@{PROTECTION_EXIT_PCT}%)  size=${MAX_POSITION_USDC}")
        print()

        results: List[Optional[Pos]] = []
        for i, m in enumerate(mkts, 1):
            p = await sim_window(s, m, i)
            results.append(p)

        # ── Summary ──
        print()
        print(f"{B}{C}{'='*72}")
        print(f"  FINAL RESULTS")
        print(f"{'='*72}{R}")
        print()

        total_pnl = 0.0
        trades = 0; wins = 0; losses = 0
        for i, p in enumerate(results):
            m = mkts[i]
            print(f"  {B}Window {i+1}: {m.question}{R}")
            if p:
                trades += 1
                pnl = p.pnl or 0
                total_pnl += pnl
                col = G if pnl >= 0 else RD
                if pnl >= 0: wins += 1
                else: losses += 1
                print(f"    Side:   {p.side}")
                print(f"    Entry:  ${p.entry:.3f}  ({p.qty:.1f} shares)")
                print(f"    Exit:   ${p.exit_px:.3f}" if p.exit_px is not None else "    Exit:   --")
                print(f"    {col}PnL:    ${pnl:+.2f}{R}")
                print(f"    Reason: {p.reason}")
            else:
                print(f"    {D}No trade{R}")
            print()

        pc = G if total_pnl >= 0 else RD
        print(f"  {B}Trades: {trades}  |  W: {wins}  L: {losses}  |  {pc}Total PnL: ${total_pnl:+.2f}{R}")
        print()

if __name__ == "__main__":
    asyncio.run(main())
