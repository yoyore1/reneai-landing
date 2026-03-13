# Bot Context — Decision History & Current State

## What This Bot Does
Polymarket BTC 5-minute prediction bot. Buys YES/NO outcome tokens when one side shows strong momentum (bid >= 70c), takes profit at 94c, stops loss at 28c. Runs 24/7 on AWS EC2 (Ireland, 34.242.67.217).

## Current Configuration (as of March 13, 2026)
- **Trade size:** $60 per trade (~84 shares at 71c entry)
- **Strategy:** `mg2` (S3 + Tuned Guard V2) for official bot
- **Entry gate:** 78c (manip guard can't override trades above this price)
- **Skip no-leader:** enabled on official bot (don't trade when no side hits 70c)
- **SL price:** 28c (default), tight_sl bot tests 45c

## Guard System — 5 Signals (2+ triggers a pause)
1. **Alternation** — sides flip 5+ times in last 6 markets
2. **Hot streak** — 7+ consecutive wins (trap detection)
3. **Choppy rate** — 40%+ of last 10 markets were choppy/no-leader
4. **Reversal rate** — 30%+ of last 5 traded markets had other side hit 60c+
5. **Extreme alternation** — 6+ flips in last 6 (fires alone, no 2nd signal needed)

Guard thresholds (mg2): `win_streak=7, alternation=5, choppy_rate=40%, cooldown=1 market`

## Key Decisions & Why
- **Guard persistence** (March 13): Added because restarts wiped guard memory. On March 12th evening, a $194 loss cluster at 7-10 PM would have been caught after 1-2 losses if the guard hadn't been reset by a restart. State saves to `history/guard_{bot_name}.json` after every market.
- **SL sleep removal** (March 13): Sell logic had 2s + 1s delays between batches during stop losses. With larger trade sizes (~84 shares), the 6-second delay window allowed markets to resolve at 0c before sells filled. Removed sleeps for SL sells only; TP sells keep delays (no urgency).
- **Reversal guard** (March 12): Added after observing two consecutive reversal losses — bot buys one side at 70c+, then the other side spikes to 60c+. Tracks this as a distinct signal.
- **Skip no-leader** (March 12): Analysis showed no-leader phantom trades had lower win rate. Enabled for official bot only.
- **Volatility guard was REJECTED**: Analysis showed high BTC swing actually correlates with HIGHER win rate (95%+ at $60-150 swing vs 79% at $0-30). The bot's momentum strategy thrives on volatility. Don't filter by BTC swing.
- **Entry price 71-76c is profitable**: Despite lower win rate (77% vs 87% at 77c+), low entries generate +$269 total because avg win is bigger ($8.59 vs $3.47). Don't filter by entry price.

## Data Collection (started March 11-13)
All data in `history/` directory on EC2:
- **analysis CSV**: Pre-buy ticks with bids, asks, spread, depth (bid+ask side), BTC swing per market
- **ticks CSV**: Position ticks while holding — same fields as analysis
- **trades CSV**: Every trade with execution quality (ask_at_buy, bid_at_sell_trigger, btc_at_entry/exit, other_side_high, reversal_detected)
- **resolutions CSV**: Market outcome (Up/Down), BTC open/close/swing
- **post_exit CSV**: What happened after TP/SL — held_pnl, max/min after exit, recovery flag
- **skipped CSV**: Choppy/no-leader/manip_guard skips with highs and BTC price
- **guard JSON**: Persistent guard state per bot

## Running Bots (ports)
| Port | Name | Mode | Strategy |
|------|------|------|----------|
| 9000 | Launcher | Web UI | — |
| 9001 | Test | Dry run | S3 base |
| 9002 | Official | LIVE | mg2 |
| 9003 | Research | Dry run | mg (original guard) |
| 9004 | MG2 Tuned V2 | Dry run | mg2r |
| 9005 | Tight SL | Dry run | S3 base, SL=45c |

## Performance Summary (March 9-13)
- Overall win rate: ~80%+ across 400+ trades
- Best hours: 2-6 AM, 2-6 PM EDT (100% WR in some hours)
- Worst hour: 7 PM EDT (0% WR on March 12, -$103)
- Average book depth: ~2,700 shares (bot's 84 shares is ~3%, no market impact)
- Average BTC swing per 5-min market: $45 median, $58 mean

## SSH Access
```
ssh -i polymarket.pem ec2-user@34.242.67.217
cd ~/reneai-landing
```

## Key Files
- `bot/strategy3_mg.py` — Main strategy + position management + post-exit tracking
- `bot/manip_guard.py` — Guard system with persistence
- `bot/polymarket.py` — Polymarket API (buy/sell/order book)
- `bot/data_logger.py` — All data collection
- `bot/trade_history.py` — Trade CSV logging
- `bot/launcher.py` — Web UI to start/stop bots
- `bot/main.py` — Entry point, strategy/guard config
- `bot/server.py` — Dashboard per bot
