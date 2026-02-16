# BTC 5-Minute Market Arbitrage Scanner

Real-time arbitrage scanner for Polymarket's **"Bitcoin Up or Down"** 5-minute prediction markets. Scans across multiple assets (BTC, ETH, SOL, XRP) and timeframes (5m, 15m, 4h) to find pricing inefficiencies.

## What Are These Markets?

Polymarket runs rolling 5-minute binary markets on BTC price direction:

- Every 5 minutes, a new market opens (e.g., "Bitcoin Up or Down - 12:30AM-12:35AM ET")
- You buy **Up** if you think BTC price will be higher at 12:35 than at 12:30
- You buy **Down** if you think it will be lower
- Markets also exist for **15-minute** and **4-hour** windows
- **ETH, SOL, XRP** also have 15-minute markets
- Each side (Up/Down) is priced between 0c and 100c, and should sum to ~$1.00

## Quick Start

```bash
# Install dependencies
npm install

# Run the CLI arbitrage scanner
node scripts/arbitrage-scanner.js

# Run the React dashboard
npm start
```

## Arbitrage Strategies

### 1. Spread Arbitrage (Risk-Free)

If **Up + Down < $1.00**, buy both sides — one always resolves to $1.00, guaranteeing profit.

**When it happens:** New market creation (first 30 seconds), thin liquidity periods, market maker quote pulls.

**Example:** Up @ 48c + Down @ 49c = 97c total cost → 3c guaranteed profit per share.

### 2. Cross-Timeframe Arbitrage (Low Risk)

The 15-minute market covers three consecutive 5-minute windows. After 1-2 windows resolve, the 15-min outcome is partially decided but may not reprice.

**How to execute:**
1. Watch the first 5-min window within a 15-min block
2. If BTC goes UP +0.3% in first 5 min, the 15-min "Up" probability is >60%
3. If the 15-min market is still priced near 50c, buy "Up"
4. Same logic extends to 4h blocks after several 15-min windows

### 3. Cross-Asset Correlation (Low-Medium Risk)

BTC, ETH, SOL, and XRP are 80-95% correlated on 15-minute timeframes.

**How to execute:**
1. Monitor all four assets' 15-min markets simultaneously
2. When BTC 15-min "Up" jumps to 58c after a pump...
3. Check if ETH/SOL/XRP 15-min "Up" are still near 50c
4. Buy "Up" on the lagging asset

### 4. Momentum Autocorrelation (~4% Edge)

BTC 5-minute candles show ~54% autocorrelation — the next candle tends to go the same direction as the previous one.

**How to execute:**
1. Check the last resolved 5-min candle direction
2. If it was UP, buy "Up" on the next window at 50c
3. If it was DOWN, buy "Down" on the next window at 50c
4. Long-term positive EV of ~4 cents per dollar risked

### 5. Volatility Event Front-Run (Medium-High Risk, High Reward)

Before known volatility events (Fed, CPI, jobs data), place limit orders at extreme prices.

**How to execute:**
1. Check economic calendar for upcoming events
2. 5-10 minutes before: place limit orders at 40-45c on BOTH Up and Down
3. The event moves BTC sharply — one side resolves near $1.00
4. Total cost ~85-90c, payout $1.00 = 10-17% profit

### 6. Market Open Liquidity Snipe (Low-Medium Risk)

New 5-min markets open with very wide bid/ask spreads (1c/99c). Get in before makers tighten to 50c.

**How to execute:**
1. Watch for new market creation every 5 minutes
2. Place limit order at 45-48c for your predicted direction
3. As market makers post tighter quotes, book moves toward 50c
4. You're in at a discount — even a 50/50 gives you 2-5c edge

## Live Data

The scanner pulls data from:
- **Polymarket Gamma API** — market prices, volumes, outcomes
- **Polymarket CLOB API** — order book depth, bid/ask spreads
- **CoinGecko API** — BTC price history for statistical baselines

Key slug patterns for these markets:
- `btc-updown-5m-{timestamp}` — BTC 5-minute
- `btc-updown-15m-{timestamp}` — BTC 15-minute
- `btc-updown-4h-{timestamp}` — BTC 4-hour
- `eth-updown-15m-{timestamp}` — ETH 15-minute
- `sol-updown-15m-{timestamp}` — SOL 15-minute
- `xrp-updown-15m-{timestamp}` — XRP 15-minute

## Architecture

```
scripts/
  arbitrage-scanner.js   # CLI scanner — run directly with Node.js
src/
  App.js                 # React dashboard — auto-refreshes every 30s
  App.css                # Dark theme styling
  index.js               # Entry point
  index.css              # Global styles
```

## Dashboard Features

- Real-time BTC price and 24h statistics (up/down ratio, momentum, volatility)
- Active market listing across all assets and timeframes
- Automatic arbitrage detection with actionable trade suggestions
- Strategy playbook with step-by-step execution guides
- Auto-refresh every 30 seconds

## Important Notes

- **Not financial advice.** Prediction markets carry risk. Past performance does not guarantee future results.
- These markets have relatively thin liquidity ($3-15k per side), so large orders will move the price.
- Polymarket charges fees on trades — factor this into arbitrage calculations.
- The 54% momentum autocorrelation is based on short-term data and may not persist.
- Cross-timeframe and cross-asset arbs are most profitable during active trading hours (US market hours).
