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

            client_kwargs = dict(
                key=cfg.poly_private_key,
                chain_id=cfg.chain_id,
                creds=creds,
            )
            if cfg.poly_signature_type:
                client_kwargs["signature_type"] = cfg.poly_signature_type
            if cfg.poly_funder_address:
                client_kwargs["funder"] = cfg.poly_funder_address

            self._clob_client = ClobClient(cfg.poly_clob_host, **client_kwargs)
            self._api_creds_loaded = True
            log.info(
                "Polymarket CLOB client initialised (LIVE mode) sig_type=%s funder=%s",
                cfg.poly_signature_type,
                cfg.poly_funder_address[:10] + "..." if cfg.poly_funder_address else "none",
            )
        except Exception as exc:
            log.error("Failed to init CLOB client: %s -- falling back to DRY_RUN", exc)
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

        ask_price = round(ask_price, 2)
        qty = int(usdc_amount / ask_price)
        if qty <= 0:
            log.warning("Qty rounds to 0 for price %.4f, skipping", ask_price)
            return Position(market=market, side=side, token_id=token_id)

        actual_cost = round(qty * ask_price, 2)

        pos = Position(
            market=market,
            side=side,
            token_id=token_id,
            qty=float(qty),
            avg_entry=ask_price,
            entry_time=time.time(),
        )

        if cfg.dry_run:
            pos.filled = True
            pos.order_id = f"DRY-{int(time.time()*1000)}"
            log.info(
                "[DRY] BUY %s %d shares @ $%.2f ($%.2f) | %s",
                side, qty, ask_price, actual_cost, market.question[:60],
            )
            return pos

        # --- Live order (aggressive limit to sweep asks) ---
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY
            import asyncio

            limit_price = min(round(ask_price + 0.05, 2), 0.99)
            order_args = OrderArgs(
                price=limit_price,
                size=float(qty),
                side=BUY,
                token_id=token_id,
            )
            log.info(
                "[LIVE] Submitting BUY %s %d shares | ask=$%.2f limit=$%.2f ($%.2f) | token=%s...",
                side, qty, ask_price, limit_price, actual_cost, token_id[:16],
            )
            signed = self._clob_client.create_order(order_args)
            result = self._clob_client.post_order(signed, OrderType.GTC)
            pos.order_id = result.get("orderID", result.get("id", ""))
            status = result.get("status", "").lower()
            pos.filled = status == "matched"
            log.info(
                "[LIVE] BUY posted: status=%s order=%s | full=%s",
                status, pos.order_id, result,
            )

            if not pos.filled and pos.order_id:
                for wait_round in (2, 3, 5):
                    await asyncio.sleep(wait_round)
                    try:
                        order_info = self._clob_client.get_order(pos.order_id)
                        live_status = order_info.get("status", "").lower()
                        size_matched = float(order_info.get("size_matched", "0"))
                        log.info("[LIVE] BUY check after %ds: status=%s matched=%.2f/%d",
                                 wait_round, live_status, size_matched, qty)
                        if size_matched > 0:
                            pos.filled = True
                            pos.qty = size_matched
                            pos.avg_entry = float(order_info.get("associate_trades", [{}])[0].get("price", ask_price)) if order_info.get("associate_trades") else ask_price
                            log.info("[LIVE] BUY FILLED (%.1f shares matched)", size_matched)
                            break
                        if live_status == "matched":
                            pos.filled = True
                            pos.qty = float(order_info.get("original_size", qty))
                            log.info("[LIVE] BUY FILLED (status=matched)")
                            break
                    except Exception as check_exc:
                        log.warning("[LIVE] BUY status check round %d failed: %s", wait_round, check_exc)

                if not pos.filled:
                    log.warning("[LIVE] BUY NOT FILLED after retries — cancelling (order=%s)", pos.order_id)
                    try:
                        self._clob_client.cancel(pos.order_id)
                    except Exception:
                        pass
        except Exception as exc:
            log.error("Buy order failed: %s", exc, exc_info=True)

        # After a successful buy, update CLOB's record of our conditional token allowance
        if pos.filled and not cfg.dry_run:
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams
                params = BalanceAllowanceParams(
                    asset_type="CONDITIONAL",
                    token_id=token_id,
                    signature_type=cfg.poly_signature_type,
                )
                self._clob_client.update_balance_allowance(params)
                log.info("[LIVE] Updated CONDITIONAL allowance for token %s...", token_id[:16])
            except Exception as exc:
                log.warning("Failed to update conditional allowance: %s", exc)

        return pos

    async def sell(self, position: Position, reason: str = "") -> bool:
        """
        Market-sell an existing position in two batches to avoid Polymarket
        balance tracking issues: sell (qty-2) first, then sell the remaining 2.
        """
        if position.qty <= 0:
            return False

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

        # --- Live sell (split into two batches) ---
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType, BalanceAllowanceParams
            from py_clob_client.order_builder.constants import SELL
            import asyncio

            try:
                params = BalanceAllowanceParams(
                    asset_type="CONDITIONAL",
                    token_id=position.token_id,
                    signature_type=cfg.poly_signature_type,
                )
                self._clob_client.update_balance_allowance(params)
            except Exception:
                pass

            total_qty = int(position.qty)
            if total_qty <= 0:
                total_qty = 1

            if reason == "sl":
                limit_price = 0.01
            else:
                limit_price = max(round(bid_price - 0.05, 2), 0.01)

            # Split: sell (qty - 2) first, then 1, then whatever is left
            first_qty = max(total_qty - 2, 1)
            leftover = total_qty - first_qty
            second_qty = min(1, leftover)
            third_qty = leftover - second_qty
            total_matched = 0.0

            for batch_idx, qty in enumerate([first_qty, second_qty, third_qty]):
                if qty <= 0:
                    continue
                label = f"batch{batch_idx + 1}"
                log.info(
                    "[LIVE] SELL %s %s %d shares | bid=$%.2f limit=$%.2f | token=%s...",
                    label, position.side, qty, bid_price, limit_price, position.token_id[:16],
                )
                order_args = OrderArgs(
                    price=limit_price,
                    size=float(qty),
                    side=SELL,
                    token_id=position.token_id,
                )
                try:
                    signed = self._clob_client.create_order(order_args)
                    result = self._clob_client.post_order(signed, OrderType.GTC)
                    order_id = result.get("orderID", result.get("id", ""))
                    status = result.get("status", "").lower()

                    if status == "matched":
                        total_matched += qty
                        log.info("[LIVE] SELL %s FILLED %d shares", label, qty)
                    elif order_id:
                        for wait_s in (2, 3, 5):
                            await asyncio.sleep(wait_s)
                            try:
                                info = self._clob_client.get_order(order_id)
                                matched = float(info.get("size_matched", "0"))
                                live_status = info.get("status", "").lower()
                                log.info("[LIVE] SELL %s check after %ds: status=%s matched=%.1f/%d",
                                         label, wait_s, live_status, matched, qty)
                                if matched > 0 or live_status == "matched":
                                    total_matched += max(matched, qty) if live_status == "matched" else matched
                                    log.info("[LIVE] SELL %s FILLED (%.1f matched)", label, matched)
                                    break
                            except Exception as chk_exc:
                                log.warning("[LIVE] SELL %s check failed: %s", label, chk_exc)
                        else:
                            try:
                                self._clob_client.cancel(order_id)
                            except Exception:
                                pass
                            log.warning("[LIVE] SELL %s NOT FILLED after retries — cancelled", label)
                except Exception as exc:
                    log.warning("[LIVE] SELL %s failed: %s", label, exc)

                if batch_idx < 2 and (second_qty > 0 or third_qty > 0):
                    if reason != "sl":
                        await asyncio.sleep(1)

            if total_matched > 0:
                fill_price = bid_price
                pnl = (fill_price - position.avg_entry) * total_matched
                position.exit_price = fill_price
                position.pnl = pnl
                log.info(
                    "[LIVE] SELL COMPLETE %s %.0f/%.0f shares @ ~$%.2f | PnL=$%.2f",
                    position.side, total_matched, float(total_qty), fill_price, pnl,
                )
                return True
            else:
                log.warning("[LIVE] SELL INCOMPLETE — only %.0f/%d matched", total_matched, total_qty)
                return False

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

    async def get_book_depth(self, token_id: str, depth_range: float = 0.05) -> dict:
        """
        Fetch order book and return summary: best bid/ask, bid/ask depth
        within `depth_range` of best price (default 5c).
        """
        try:
            url = f"{cfg.poly_clob_host}/book"
            params = {"token_id": token_id}
            async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                book = await resp.json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            best_bid = max(float(b["price"]) for b in bids) if bids else 0
            bid_depth = sum(
                float(b.get("size", 0))
                for b in bids
                if float(b["price"]) >= best_bid - depth_range
            ) if bids else 0
            best_ask = min(float(a["price"]) for a in asks) if asks else 0
            ask_depth = sum(
                float(a.get("size", 0))
                for a in asks
                if float(a["price"]) <= best_ask + depth_range
            ) if asks else 0
            return {
                "bid": best_bid, "depth": round(bid_depth, 1),
                "ask": best_ask, "ask_depth": round(ask_depth, 1),
            }
        except Exception:
            return {"bid": 0, "depth": 0, "ask": 0, "ask_depth": 0}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Auto-redeem: sweep resolved/leftover tokens back to USDC
    # ------------------------------------------------------------------

    async def auto_redeem(self) -> dict:
        """
        Query Polymarket data-api for redeemable positions (resolved wins
        and leftover dust) on the funder address, then sell them at $0.99
        on the CLOB to convert back to USDC.
        """
        if cfg.dry_run or not cfg.poly_funder_address or not self._clob_client:
            return {"redeemed": 0, "usdc_recovered": 0.0}

        try:
            url = "https://data-api.polymarket.com/positions"
            params = {
                "user": cfg.poly_funder_address,
                "redeemable": "true",
                "limit": "100",
            }
            async with self._session.get(
                url, params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                positions = await resp.json()

            if not positions or not isinstance(positions, list):
                return {"redeemed": 0, "usdc_recovered": 0.0}

            redeemed = 0
            usdc_recovered = 0.0

            for pos_data in positions:
                token_id = pos_data.get("asset", "")
                raw_size = float(pos_data.get("size", 0))
                if not token_id or raw_size <= 0:
                    continue

                qty = int(raw_size)
                if qty <= 0:
                    continue

                title = (pos_data.get("title") or pos_data.get("question")
                         or token_id[:16])

                try:
                    from py_clob_client.clob_types import (
                        OrderArgs, OrderType, BalanceAllowanceParams,
                    )
                    from py_clob_client.order_builder.constants import SELL

                    try:
                        ba = BalanceAllowanceParams(
                            asset_type="CONDITIONAL",
                            token_id=token_id,
                            signature_type=cfg.poly_signature_type,
                        )
                        self._clob_client.update_balance_allowance(ba)
                    except Exception:
                        pass

                    order_args = OrderArgs(
                        price=0.99,
                        size=float(qty),
                        side=SELL,
                        token_id=token_id,
                    )

                    log.info(
                        "[REDEEM] Selling %d redeemable tokens @ $0.99 | %s",
                        qty, str(title)[:50],
                    )
                    signed = self._clob_client.create_order(order_args)
                    result = self._clob_client.post_order(signed, OrderType.GTC)
                    status = result.get("status", "")
                    order_id = result.get("orderID", result.get("id", ""))

                    if status == "matched":
                        usdc = qty * 0.99
                        usdc_recovered += usdc
                        redeemed += 1
                        log.info(
                            "[REDEEM] OK — %d tokens → $%.2f USDC | %s",
                            qty, usdc, str(title)[:40],
                        )
                    elif order_id:
                        await asyncio.sleep(3)
                        try:
                            info = self._clob_client.get_order(order_id)
                            matched = float(info.get("size_matched", "0"))
                            if matched > 0:
                                usdc = matched * 0.99
                                usdc_recovered += usdc
                                redeemed += 1
                                log.info(
                                    "[REDEEM] Partial %d/%d tokens → $%.2f | %s",
                                    int(matched), qty, usdc, str(title)[:40],
                                )
                            try:
                                self._clob_client.cancel(order_id)
                            except Exception:
                                pass
                        except Exception:
                            try:
                                self._clob_client.cancel(order_id)
                            except Exception:
                                pass

                except Exception as exc:
                    log.warning("[REDEEM] Failed for %s: %s", str(title)[:30], exc)

            if redeemed > 0:
                log.info(
                    "[REDEEM] Sweep complete: %d positions → $%.2f USDC recovered",
                    redeemed, usdc_recovered,
                )
            return {"redeemed": redeemed, "usdc_recovered": round(usdc_recovered, 2)}

        except Exception as exc:
            log.warning("[REDEEM] Sweep check failed: %s", exc)
            return {"redeemed": 0, "usdc_recovered": 0.0}

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
