# Binance → Polymarket Arbitrage Bot

Detects BTC price spikes on Binance and instantly buys on Polymarket's "BTC Up or Down - 5 min" markets before the order book reprices.

## Setup

### 1. Install dependencies

```bash
cd /workspace
npm install
```

### 2. Get your Polymarket API credentials

1. Go to [polymarket.com](https://polymarket.com)
2. Connect your wallet
3. Go to Settings → API Keys
4. Create a new API key — you'll get:
   - API Key
   - API Secret
   - Passphrase

### 3. Get your wallet private key

Export from MetaMask: Account Details → Export Private Key

**NEVER share your private key with anyone.**

### 4. Configure the bot

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```
PRIVATE_KEY=0xYOUR_PRIVATE_KEY
POLY_API_KEY=your-api-key
POLY_API_SECRET=your-api-secret
POLY_API_PASSPHRASE=your-passphrase
CHAIN_ID=137
SPIKE_THRESHOLD=0.10
TRADE_SIZE=100
MAX_OPEN_TRADES=3
HOLD_TO_RESOLUTION=true
```

### 5. Run the bot

```bash
# From your Denmark VPN:
node bot/index.js
```

## How It Works

```
Binance WebSocket (real-time BTC price)
         │
         ▼
   Spike detected? (>0.10% in 2 seconds)
         │
    ┌────┴────┐
    │  YES    │  NO → keep watching
    ▼         │
Find active   │
Polymarket    │
5-min market  │
    │         │
    ▼         │
Buy UP/DOWN   │
at 51c ask    │
    │         │
    ▼         │
Hold to       │
resolution    │
($1 or $0)    │
```

## Modes

### Dry Run (default, no private key)
Shows what trades would be placed without executing. Good for testing.

### Live Trading (with private key)
Places real orders on Polymarket. Make sure you have USDC in your wallet on Polygon.

## Strategy Config

| Setting | Default | Description |
|---------|---------|-------------|
| `SPIKE_THRESHOLD` | 0.10 | Min BTC move in % to trigger (0.10% = ~$68) |
| `TRADE_SIZE` | 100 | USDC per trade |
| `MAX_OPEN_TRADES` | 3 | Max simultaneous positions |
| `HOLD_TO_RESOLUTION` | true | Hold until market resolves at $1/$0 |
| `TAKE_PROFIT_PCT` | 10 | Sell target if not holding (%) |
| `STOP_LOSS_PCT` | 20 | Cut losses if not holding (%) |

## Backtest Results

On the last 100 resolved Polymarket markets:

| Spike Threshold | Trades | Win Rate | Hold P&L | Hold ROI |
|----------------|--------|----------|----------|---------|
| >0.10% | 9 | **100%** | +$847 | **+94.2%** |
| >0.08% | 12 | 75% | +$547 | +45.6% |
| >0.05% | 26 | 57.7% | +$312 | +12.0% |
| >0.03% | 52 | 63.5% | +$1,207 | +23.2% |

## Safety

- Bot checks ask price before buying — skips if already above 60c (book already repriced)
- Won't trade if market resolves in <30 seconds
- Max open trades limit prevents overexposure
- Dry run mode by default (no real orders without private key)

## Important

- **Not financial advice.** You can lose money.
- Run from a **Denmark VPN** for full Binance access.
- Make sure you have **USDC on Polygon** in your wallet.
- The 100% win rate on 9 trades is a small sample — real performance will vary.
- Polymarket books may reprice faster during high-volume periods.
