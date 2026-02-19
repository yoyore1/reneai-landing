# Binance-Polymarket BTC 5-Minute Arbitrage Bot

An automated trading bot that exploits the latency gap between Binance real-time BTC prices and Polymarket's 5-minute BTC prediction markets.

## How It Works

Polymarket runs rolling 5-minute binary markets like *"Will BTC be above $X at 12:35?"*. The reference price is set at the start of each window (e.g., 12:30), and the market resolves 5 minutes later.

Binance updates BTC/USDT prices in **real time** (sub-second). Polymarket odds, priced by human traders, lag behind by seconds to minutes.

**The strategy:**

1. **Monitor Binance** -- WebSocket feed gives us the live BTC price every trade.
2. **Detect spikes** -- When BTC moves more than a configurable threshold (default 0.15%) from the window-open price, the outcome is increasingly certain.
3. **Buy on Polymarket** -- Immediately buy the winning side (Up if BTC spiked up, Down if it spiked down) at whatever price is available.
4. **Exit tiers:**
   - **+10% to +20%** -- Sell immediately, take profit.
   - **+20% or higher** -- Moonbag mode. Let it ride, but set a trailing stop at +10%. If it drops from +25% back to +10%, sell there (still a win).
   - **Below +10%** -- Keep waiting for it to reach +10%.
   - **Drops past -15%** -- Protection mode. Stop hoping, start surviving. Sell when it recovers to -10% (small loss beats big loss).
5. **Hold to resolution** -- If no exit triggers before the window ends, the market resolves on-chain.

```
Binance: BTC jumps from $97,000 → $97,200 (+0.21%) in 90 seconds
Polymarket: "BTC above $97,000 at 12:35?" YES still priced at $0.55
Bot: BUY Up @ $0.55
... 60 seconds later ...
Polymarket: Up reprices to $0.85 as traders notice the move
Bot: SELL Up @ $0.61 (+10.9% gain)   ← normal profit

--- Moonbag scenario: ---
Bot: BUY Up @ $0.50 (BTC spiked hard)
Position hits +22% → MOONBAG MODE, let it ride!
Position peaks at +30%, then pulls back...
Position hits +10% → trailing stop triggers
Bot: SELL Up @ $0.55 (+10% gain, rode the wave)

--- Protection scenario: ---
Bot: BUY Up @ $0.55 (BTC spiked up)
BTC reverses... position drops to -18% → PROTECTION MODE
BTC bounces back a little... position recovers to -10%
Bot: SELL Up @ $0.495 (-10% loss, protected from worse)
```

## Project Structure

```
bot/
  __init__.py
  __main__.py         # python -m bot entry point
  main.py             # orchestrator -- wires everything together
  config.py           # all settings via environment variables
  binance_feed.py     # real-time BTC/USDT from Binance WebSocket
  polymarket.py       # Polymarket CLOB API client (discover, buy, sell)
  strategy.py         # spike detection + position management
  dashboard.py        # live Rich terminal dashboard
requirements.txt
.env.example          # copy to .env and fill in your keys
```

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your credentials
```

**Required for live trading:**
- `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE` -- get these from [Polymarket](https://polymarket.com) after connecting your wallet and generating API keys.
- `POLY_PRIVATE_KEY` -- your Polygon wallet private key (the wallet must hold USDC on Polygon).

**Optional tuning:**
| Variable | Default | Description |
|----------|---------|-------------|
| Variable | Default | Description |
|----------|---------|-------------|
| `SPIKE_MOVE_USD` | `20.0` | BTC must move this many dollars... |
| `SPIKE_WINDOW_SEC` | `3.0` | ...within this many seconds to trigger a buy |
| `MOONBAG_PCT` | `20.0` | If gain hits this %, let it ride (trailing stop at PROFIT_TARGET) |
| `PROFIT_TARGET_PCT` | `10.0` | Sell between 10-20%, or trailing stop floor for moonbag |
| `DRAWDOWN_TRIGGER_PCT` | `-15.0` | If position drops past this %, enter protection mode |
| `PROTECTION_EXIT_PCT` | `-10.0` | In protection mode, sell at this % to cut losses |
| `MAX_POSITION_USDC` | `50.0` | Max USDC to spend per trade |
| `POLL_INTERVAL_SEC` | `1.0` | How often to check for signals (seconds) |
| `DRY_RUN` | `true` | Paper trading mode -- no real money |

### 3. Run

**With live dashboard:**
```bash
python -m bot
```

**Headless (logs only):**
```bash
python -m bot --headless
```

The dashboard shows:
- Real-time BTC price from Binance
- All tracked 5-minute windows with % move from open
- Active signals and positions
- P&L tracking

### 4. Go live

When you're ready to trade real money:
```bash
DRY_RUN=false python -m bot
```

Make sure your Polygon wallet has USDC and your API keys are configured.

## Safety Notes

- **Start with DRY_RUN=true** to observe the bot's behavior before risking real money.
- **Small positions first** -- set `MAX_POSITION_USDC` low ($5-10) until you trust the setup.
- This strategy depends on Polymarket having sufficient liquidity in the 5-minute BTC markets. If the order book is thin, fills may be poor.
- Polymarket market structure and API can change. Monitor the bot.
- **This is not financial advice.** Trading involves risk of loss.

## How the Bot Finds Markets

The bot queries Polymarket's Gamma API every 30 seconds for active markets matching:
- Tags: crypto
- Keywords: "bitcoin" or "btc" AND "5 min" / "5-min"

It automatically parses the reference price from the market question (e.g., "$97,000.00") and tracks the time window from the market's end date.

## Architecture

```
                    +------------------+
                    |  Binance WS      |
                    |  (BTC/USDT)      |
                    +--------+---------+
                             |
                    real-time price updates
                             |
                    +--------v---------+
                    |                  |
                    |    Strategy      |  <-- spike detection
                    |    Engine        |  <-- position management
                    |                  |  <-- exit at 10% or resolution
                    +--------+---------+
                             |
                      buy / sell orders
                             |
                    +--------v---------+
                    |  Polymarket      |
                    |  CLOB API        |
                    +------------------+
```

## License

MIT
