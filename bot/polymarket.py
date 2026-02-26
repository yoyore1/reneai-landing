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
    moonbag_mode: bool = False     # True once gain hits 20%+, trailing stop at profit_target
    peak_gain: float = 0.0        # highest gain % ever seen


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
        self.balance_usdc: Optional[float] = None  # cached for dashboard

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
            sig_type = getattr(cfg, "poly_signature_type", 1)
            funder = getattr(cfg, "poly_funder_address", "") or None
            self._clob_client = ClobClient(
                cfg.poly_clob_host,
                key=cfg.poly_private_key,
                chain_id=cfg.chain_id,
                creds=creds,
                signature_type=sig_type,
                funder=funder,
            )
            self._api_creds_loaded = True
            log.info("Polymarket CLOB client initialised (LIVE mode)")
        except Exception as exc:
            import traceback
            log.error("Failed to init CLOB client: %s -- falling back to DRY_RUN", exc)
            log.error("Traceback: %s", traceback.format_exc())
            cfg.dry_run = True

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    async def find_active_btc_5min_markets(self) -> List[Market]:
        """
        Discover active BTC 5-minute markets using two methods:
          1. Slug-based lookup (btc-updown-5m-{epoch}) -- most reliable
          2. Keyword search fallback ("bitcoin" + "up or down")
        """
        import json as _json
        from datetime import datetime as _dt

        markets: List[Market] = []
        seen_cids: set = set()
        now = time.time()

        # ── Method 1: Slug-based (current + next 2 windows) ──
        # The bot re-discovers every 30s, so we only need to stay
        # 1-2 windows ahead. This keeps API calls low (3 per cycle).
        try:
            current_slot = (int(now) // 300) * 300
            for offset in range(0, 3):  # current, next, next+1
                epoch = current_slot + offset * 300
                slug = f"btc-updown-5m-{epoch}"
                url = f"{self.GAMMA_API}/events?slug={slug}"
                try:
                    async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        events = await resp.json()
                except Exception:
                    continue
                if not events:
                    continue
                for m in events[0].get("markets", []):
                    cid = m.get("conditionId", "")
                    if cid in seen_cids:
                        continue
                    raw_tokens = m.get("clobTokenIds", "[]")
                    tokens = _json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
                    if len(tokens) < 2:
                        continue
                    end_str = m.get("endDate", "")
                    if not end_str:
                        continue
                    end_dt = _dt.fromisoformat(end_str.replace("Z", "+00:00"))
                    end_ts = end_dt.timestamp()
                    if end_ts < now:
                        continue
                    mkt = Market(
                        condition_id=cid,
                        question=m.get("question", ""),
                        yes_token_id=tokens[0],
                        no_token_id=tokens[1],
                        active=True,
                    )
                    mkt.window_end = end_ts
                    mkt.window_start = end_ts - 300
                    mkt.reference_price = self._parse_reference_price(m.get("question", ""))
                    markets.append(mkt)
                    seen_cids.add(cid)
        except Exception as exc:
            log.warning("Slug-based discovery error: %s", exc)

        # ── Method 2: Keyword fallback ──
        try:
            params = {
                "closed": "false",
                "limit": "100",
                "order": "startDate",
                "ascending": "false",
            }
            url = f"{self.GAMMA_API}/markets"
            async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()

            for item in data:
                title = item.get("question", "").lower()
                cid = item.get("conditionId", item.get("condition_id", ""))
                if cid in seen_cids:
                    continue
                is_btc_5min = (
                    ("bitcoin" in title or "btc" in title)
                    and "up or down" in title
                    and "15" not in title  # skip 15-min markets
                )
                if not is_btc_5min:
                    continue
                raw_tokens = item.get("clobTokenIds", item.get("clob_token_ids", "[]"))
                tokens = _json.loads(raw_tokens) if isinstance(raw_tokens, str) else (raw_tokens if isinstance(raw_tokens, list) else [])
                if len(tokens) < 2:
                    continue
                end_date = item.get("endDate", "")
                if not end_date:
                    continue
                try:
                    end_dt = _dt.fromisoformat(end_date.replace("Z", "+00:00"))
                    end_ts = end_dt.timestamp()
                except Exception:
                    continue
                if end_ts < now:
                    continue
                mkt = Market(
                    condition_id=cid,
                    question=item.get("question", ""),
                    yes_token_id=tokens[0],
                    no_token_id=tokens[1],
                    active=True,
                )
                mkt.window_end = end_ts
                mkt.window_start = end_ts - 300
                mkt.reference_price = self._parse_reference_price(item.get("question", ""))
                markets.append(mkt)
                seen_cids.add(cid)
        except Exception as exc:
            log.warning("Keyword discovery error: %s", exc)

        log.info("Found %d active BTC 5-min markets", len(markets))
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

    async def refresh_balance(self) -> None:
        """Fetch USDC balance from Polymarket (for dashboard). Caches in self.balance_usdc."""
        sig_type = getattr(cfg, "poly_signature_type", 1)
        # 1. Try CLOB balance-allowance API
        if self._clob_client:
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

                params = BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=sig_type,
                )
                resp = self._clob_client.get_balance_allowance(params)
                if isinstance(resp, dict):
                    for key in ("balance", "balanceAllowance", "balance_amount", "balanceAmount"):
                        raw = resp.get(key)
                        if raw is not None and str(raw).strip():
                            val = float(str(raw).strip())
                            if val >= 1_000_000:
                                val = val / 1e6
                            self.balance_usdc = round(val, 2)
                            return
            except Exception as exc:
                log.warning("CLOB balance failed: %s", exc)
        # 2. On-chain USDC balance (accurate for funder; polygon-rpc.com often 401, use publicnode)
        funder = getattr(cfg, "poly_funder_address", "") or None
        addr = funder or (self._clob_client.get_address() if self._clob_client else None)
        if not addr and cfg.poly_private_key:
            try:
                from eth_account import Account
                key = (cfg.poly_private_key or "").strip().lstrip("0x")
                if len(key) >= 40:
                    acct = Account.from_key("0x" + key)
                    addr = acct.address
            except Exception:
                pass
        if addr:
            try:
                from web3 import Web3
                w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
                usdc = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
                addr_hex = addr.strip().lstrip("0x").lower().zfill(40)
                data = "0x70a08231" + ("0" * 24) + addr_hex
                bal_hex = w3.eth.call({"to": Web3.to_checksum_address(usdc), "data": data})
                raw = int(bal_hex.hex(), 16)
                self.balance_usdc = round(raw / 1e6, 2)
                return
            except Exception as exc:
                log.debug("On-chain balance failed: %s", exc)
        # 3. Fallback: Data API /value (can be stale)
        try:
            if addr:
                url = "https://data-api.polymarket.com/value"
                async with self._session.get(url, params={"user": addr}, timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        data = await r.json()
                        if isinstance(data, list) and data:
                            v = data[0].get("value", 0)
                            if v is not None:
                                self.balance_usdc = round(float(v), 2)
        except Exception as exc:
            log.debug("Data API value failed: %s", exc)

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
