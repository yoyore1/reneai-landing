"""
Polymarket CLOB client wrapper.

Handles:
  - Discovering the currently-active BTC 5-minute price markets
  - Placing buy / sell orders for YES and NO outcome tokens
  - Querying open positions and order status
"""

import asyncio
import logging
import time
import math
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

import aiohttp

from bot.config import cfg

log = logging.getLogger("polymarket")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Market:
    """Represents one 5-minute BTC price bucket on Polymarket."""
    condition_id: str
    question: str
    # token IDs for the two outcomes
    yes_token_id: str
    no_token_id: str
    # the reference (opening) BTC price for this window
    reference_price: Optional[float] = None
    # window timestamps (UTC epoch)
    window_start: float = 0.0
    window_end: float = 0.0
    # current best ask prices (cost to buy 1 share)
    yes_ask: float = 0.0
    no_ask: float = 0.0
    # active flag
    active: bool = True


@dataclass
class Position:
    """A position we hold in a market."""
    market: Market
    side: str  # "YES" or "NO"
    token_id: str
    qty: float = 0.0
    avg_entry: float = 0.0
    order_id: str = ""
    filled: bool = False
    entry_time: float = 0.0
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    protection_mode: bool = False  # True once position drops past drawdown trigger


# ---------------------------------------------------------------------------
# Polymarket REST helpers (async, no SDK dependency at runtime)
# ---------------------------------------------------------------------------

class PolymarketClient:
    """
    Async wrapper around the Polymarket CLOB REST API.

    In DRY_RUN mode every 'trade' is simulated locally.
    """

    GAMMA_API = "https://gamma-api.polymarket.com"

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._clob_client = None  # lazy-init when not dry-run
        self._headers: Dict[str, str] = {}
        self._api_creds_loaded = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        self._session = aiohttp.ClientSession()
        if not cfg.dry_run:
            await self._init_clob_client()

    async def stop(self):
        if self._session:
            await self._session.close()

    # ------------------------------------------------------------------
    # CLOB client init (only for live trading)
    # ------------------------------------------------------------------

    async def _init_clob_client(self):
        """Initialize the py-clob-client for real order placement."""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=cfg.poly_api_key,
                api_secret=cfg.poly_api_secret,
                api_passphrase=cfg.poly_api_passphrase,
            )
            self._clob_client = ClobClient(
                cfg.poly_clob_host,
                key=cfg.poly_private_key,
                chain_id=cfg.chain_id,
                creds=creds,
            )
            self._api_creds_loaded = True
            log.info("Polymarket CLOB client initialised (LIVE mode)")
        except Exception as exc:
            log.error("Failed to init CLOB client: %s -- falling back to DRY_RUN", exc)
            cfg.dry_run = True

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    async def find_active_btc_5min_markets(self) -> List[Market]:
        """
        Query the Gamma API for currently-active BTC 5-minute markets.
        These markets have titles like:
          'Bitcoin above $XX,XXX.XX at HH:MM (5 min)'
        or similar patterns. We search and parse.
        """
        markets: List[Market] = []
        try:
            params = {
                "closed": "false",
                "limit": "50",
                "order": "startDate",
                "ascending": "false",
                "tag": "crypto",
            }
            url = f"{self.GAMMA_API}/markets"
            async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()

            now = time.time()
            for item in data:
                title = item.get("question", "").lower()
                # Match BTC / Bitcoin 5-minute price markets
                is_btc_5min = (
                    ("bitcoin" in title or "btc" in title)
                    and ("5 min" in title or "5-min" in title or "five min" in title)
                )
                if not is_btc_5min:
                    continue

                outcomes = item.get("outcomes", [])
                tokens = item.get("clobTokenIds", item.get("clob_token_ids", []))
                if len(tokens) < 2:
                    continue

                mkt = Market(
                    condition_id=item.get("conditionId", item.get("condition_id", "")),
                    question=item.get("question", ""),
                    yes_token_id=tokens[0],
                    no_token_id=tokens[1],
                    active=True,
                )

                # Try to parse reference price from the question
                mkt.reference_price = self._parse_reference_price(item.get("question", ""))

                # Try to parse time window
                end_date = item.get("endDate", "")
                if end_date:
                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        mkt.window_end = dt.timestamp()
                        mkt.window_start = mkt.window_end - 300  # 5 minutes before
                    except Exception:
                        pass

                # Skip markets that already ended
                if mkt.window_end and mkt.window_end < now:
                    continue

                markets.append(mkt)

            log.info("Found %d active BTC 5-min markets", len(markets))
        except Exception as exc:
            log.error("Market discovery failed: %s", exc, exc_info=True)

        return markets

    async def get_market_prices(self, market: Market) -> None:
        """Fetch current best-ask for YES and NO on a market."""
        try:
            url = f"{cfg.poly_clob_host}/book"
            for side, token_id, attr in [
                ("YES", market.yes_token_id, "yes_ask"),
                ("NO", market.no_token_id, "no_ask"),
            ]:
                params = {"token_id": token_id}
                async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    book = await resp.json()
                asks = book.get("asks", [])
                if asks:
                    best = min(asks, key=lambda a: float(a.get("price", "999")))
                    setattr(market, attr, float(best["price"]))
        except Exception as exc:
            log.warning("Price fetch failed for %s: %s", market.condition_id[:8], exc)

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def buy(self, market: Market, side: str, usdc_amount: float) -> Position:
        """
        Buy `side` (YES or NO) shares on `market` spending up to `usdc_amount`.
        Returns a Position.
        """
        token_id = market.yes_token_id if side == "YES" else market.no_token_id
        ask_price = market.yes_ask if side == "YES" else market.no_ask

        if ask_price <= 0 or ask_price >= 1.0:
            log.warning("Bad ask price %.4f for %s %s, skipping", ask_price, side, market.condition_id[:8])
            return Position(market=market, side=side, token_id=token_id)

        qty = usdc_amount / ask_price
        qty = math.floor(qty * 100) / 100  # round down to 2 decimals

        pos = Position(
            market=market,
            side=side,
            token_id=token_id,
            qty=qty,
            avg_entry=ask_price,
            entry_time=time.time(),
        )

        if cfg.dry_run:
            pos.filled = True
            pos.order_id = f"DRY-{int(time.time()*1000)}"
            log.info(
                "[DRY] BUY %s %.2f shares @ $%.4f ($%.2f) | %s",
                side, qty, ask_price, usdc_amount, market.question[:60],
            )
            return pos

        # --- Live order via py-clob-client ---
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            order_args = OrderArgs(
                price=ask_price,
                size=qty,
                side=BUY,
                token_id=token_id,
            )
            signed = self._clob_client.create_order(order_args)
            result = self._clob_client.post_order(signed, OrderType.GTC)
            pos.order_id = result.get("orderID", result.get("id", ""))
            pos.filled = result.get("status", "") == "matched"
            log.info(
                "[LIVE] BUY %s %.2f shares @ $%.4f | order=%s status=%s",
                side, qty, ask_price, pos.order_id, result.get("status"),
            )
        except Exception as exc:
            log.error("Buy order failed: %s", exc, exc_info=True)

        return pos

    async def sell(self, position: Position) -> bool:
        """
        Market-sell an existing position.
        Returns True if the sell was submitted / simulated.
        """
        if position.qty <= 0:
            return False

        # Fetch current bid price
        bid_price = await self._get_best_bid(position.token_id)
        if bid_price is None or bid_price <= 0:
            log.warning("No bid available for %s, cannot sell", position.token_id[:8])
            return False

        if cfg.dry_run:
            pnl = (bid_price - position.avg_entry) * position.qty
            position.exit_price = bid_price
            position.pnl = pnl
            log.info(
                "[DRY] SELL %s %.2f shares @ $%.4f | PnL: $%.2f (%.1f%%)",
                position.side, position.qty, bid_price,
                pnl, (bid_price / position.avg_entry - 1) * 100,
            )
            return True

        # --- Live sell ---
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import SELL

            order_args = OrderArgs(
                price=bid_price,
                size=position.qty,
                side=SELL,
                token_id=position.token_id,
            )
            signed = self._clob_client.create_order(order_args)
            result = self._clob_client.post_order(signed, OrderType.GTC)
            pnl = (bid_price - position.avg_entry) * position.qty
            position.exit_price = bid_price
            position.pnl = pnl
            log.info(
                "[LIVE] SELL %s %.2f shares @ $%.4f | order=%s PnL=$%.2f",
                position.side, position.qty, bid_price,
                result.get("orderID", ""), pnl,
            )
            return True
        except Exception as exc:
            log.error("Sell order failed: %s", exc, exc_info=True)
            return False

    async def _get_best_bid(self, token_id: str) -> Optional[float]:
        try:
            url = f"{cfg.poly_clob_host}/book"
            params = {"token_id": token_id}
            async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                book = await resp.json()
            bids = book.get("bids", [])
            if bids:
                best = max(bids, key=lambda b: float(b.get("price", "0")))
                return float(best["price"])
        except Exception as exc:
            log.warning("Bid fetch failed: %s", exc)
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_reference_price(question: str) -> Optional[float]:
        """
        Try to extract the dollar reference price from a market question.
        Examples:
          'Will Bitcoin be above $98,765.43 at 12:35 (5 min)?'
          'Bitcoin above $98765.43 at 12:35 (5-min)'
        """
        import re
        match = re.search(r"\$([0-9,]+\.?\d*)", question)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except ValueError:
                pass
        return None
