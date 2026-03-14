"""
Wallet Tracker — Logs all trades per market from Polymarket Data API.

After each market resolves, fetches every trade that happened in that market
and logs wallet addresses, sizes, timing, and side. Over time this builds
a database to identify wallets that consistently manipulate markets
(e.g. large opposing-side buys in the last 60 seconds of reversal markets).
"""

import csv
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import aiohttp

log = logging.getLogger("wallet_tracker")

EDT = timezone(timedelta(hours=-4))
DATA_API = "https://data-api.polymarket.com"

TRADES_HEADER = [
    "fetch_time", "edt_time", "market", "condition_id",
    "wallet", "side", "asset", "size", "price",
    "trade_timestamp", "outcome",
    "market_resolution", "bot_side",
    "is_reversal", "is_winner",
]

WALLET_SUMMARY_HEADER = [
    "wallet", "total_trades", "total_size",
    "win_sells", "win_sell_size",
    "markets_seen", "reversal_markets",
    "reversal_wins", "reversal_win_size",
    "suspicion_score",
]


class WalletTracker:

    def __init__(self, bot_name: str):
        self._bot_name = bot_name
        self._dir = "history"
        os.makedirs(self._dir, exist_ok=True)

        self._trades_file = os.path.join(self._dir, f"{bot_name}_wallet_trades.csv")
        self._summary_file = os.path.join(self._dir, f"{bot_name}_wallet_summary.csv")
        self._init_csv(self._trades_file, TRADES_HEADER)

        self._http_session: aiohttp.ClientSession | None = None
        self._fetched_markets: set = set()

    def _init_csv(self, path: str, header: list):
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(header)

    async def _ensure_session(self):
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()

    async def fetch_and_log_trades(
        self,
        condition_id: str,
        market_name: str,
        window_end: float,
        resolution: str,
        bot_side: str,
    ):
        """Fetch all trades for a resolved market and log wallet activity."""
        if condition_id in self._fetched_markets:
            return
        self._fetched_markets.add(condition_id)

        try:
            await self._ensure_session()
            url = f"{DATA_API}/trades"
            params = {
                "market": condition_id,
                "takerOnly": "false",
                "limit": 10000,
            }

            async with self._http_session.get(
                url, params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    log.warning("Wallet API returned %d for %s", resp.status, condition_id[:16])
                    return
                trades = await resp.json()

            if not trades:
                log.debug("No trades returned for %s", condition_id[:16])
                return

            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            edt_str = datetime.now(EDT).strftime("%I:%M:%S %p")
            clean_name = market_name[:60].replace(",", ";")

            is_reversal = resolution != bot_side if (resolution and bot_side) else False

            rows = []
            for t in trades:
                wallet = t.get("proxyWallet", "")
                side = t.get("side", "")
                asset = t.get("asset", "")
                size = t.get("size", 0)
                price = t.get("price", 0)
                outcome = t.get("outcome", "")
                trade_ts = t.get("timestamp", 0)

                if isinstance(trade_ts, str):
                    try:
                        trade_ts = int(trade_ts)
                    except ValueError:
                        trade_ts = 0

                if trade_ts > 1e12:
                    trade_ts = trade_ts / 1000

                # Post-resolution SELLs at high price = winning side holders
                is_winner = side == "SELL" and float(price) > 0.90

                rows.append([
                    now_str, edt_str, clean_name, condition_id,
                    wallet, side, asset, size, price,
                    trade_ts, outcome,
                    resolution, bot_side,
                    "yes" if is_reversal else "no",
                    "yes" if is_winner else "no",
                ])

            with open(self._trades_file, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerows(rows)

            log.info(
                "WALLET TRACKER: Logged %d trades for %s (%s) | wallets=%d",
                len(rows), clean_name[:35], resolution,
                len(set(r[4] for r in rows)),
            )

        except Exception as exc:
            log.warning("Wallet tracker fetch failed: %s", exc)

    def analyze_wallets(self):
        """Analyze wallet data — find who consistently profits from reversals."""
        if not os.path.exists(self._trades_file):
            return {}

        wallet_data = defaultdict(lambda: {
            "total_trades": 0,
            "total_size": 0,
            "win_sells": 0,
            "win_sell_size": 0,
            "markets": set(),
            "reversal_markets": set(),
            "reversal_wins": 0,
            "reversal_win_size": 0,
            "normal_wins": 0,
        })

        with open(self._trades_file) as f:
            reader = csv.DictReader(f)
            for row in reader:
                wallet = row.get("wallet", "")
                if not wallet:
                    continue

                w = wallet_data[wallet]
                w["total_trades"] += 1
                size = float(row.get("size", 0) or 0)
                w["total_size"] += size

                cid = row.get("condition_id", "")
                w["markets"].add(cid)

                is_reversal = row.get("is_reversal") == "yes"
                is_winner = row.get("is_winner") == "yes"

                if is_winner:
                    w["win_sells"] += 1
                    w["win_sell_size"] += size

                if is_reversal:
                    w["reversal_markets"].add(cid)
                    if is_winner:
                        w["reversal_wins"] += 1
                        w["reversal_win_size"] += size
                elif is_winner:
                    w["normal_wins"] += 1

        results = {}
        for wallet, w in wallet_data.items():
            rev_count = len(w["reversal_markets"])
            market_count = len(w["markets"])

            score = 0
            if w["reversal_wins"] >= 2:
                score += 30
            if w["reversal_win_size"] > 100:
                score += 20
            if rev_count >= 2 and w["reversal_wins"] / max(rev_count, 1) > 0.5:
                score += 25
            if market_count >= 5 and rev_count / max(market_count, 1) > 0.3:
                score += 15
            if w["win_sell_size"] > 500:
                score += 10

            results[wallet] = {
                "total_trades": w["total_trades"],
                "total_size": w["total_size"],
                "win_sells": w["win_sells"],
                "win_sell_size": w["win_sell_size"],
                "markets_seen": market_count,
                "reversal_markets": rev_count,
                "reversal_wins": w["reversal_wins"],
                "reversal_win_size": w["reversal_win_size"],
                "normal_wins": w["normal_wins"],
                "suspicion_score": score,
            }

        sorted_wallets = sorted(results.items(), key=lambda x: -x[1]["suspicion_score"])
        with open(self._summary_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(WALLET_SUMMARY_HEADER)
            for wallet, data in sorted_wallets[:200]:
                writer.writerow([
                    wallet,
                    data["total_trades"], f"{data['total_size']:.1f}",
                    data["win_sells"], f"{data['win_sell_size']:.1f}",
                    data["markets_seen"], data["reversal_markets"],
                    data["reversal_wins"], f"{data['reversal_win_size']:.1f}",
                    data["suspicion_score"],
                ])

        return results

    async def close(self):
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
