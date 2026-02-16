#!/usr/bin/env node

/**
 * Backtest all 6 arbitrage strategies against real BTC price data.
 *
 * Data sources:
 *   - Kraken: 5-min OHLC candles (multiple days)
 *   - CoinGecko: BTC + ETH + SOL + XRP price data (24h, ~5-min intervals)
 *
 * Each strategy is simulated with $1 per trade, tracking:
 *   - Total trades, wins, losses
 *   - Win rate, P&L, ROI
 *   - Max drawdown, Sharpe-like ratio
 */

const axios = require("axios");

// ── Data fetching ───────────────────────────────────────────

async function fetchKrakenCandles() {
  const since = Math.floor(Date.now() / 1000) - 7 * 86400;
  const res = await axios.get("https://api.kraken.com/0/public/OHLC", {
    params: { pair: "XBTUSD", interval: 5, since },
    timeout: 15000,
  });
  const raw = Object.values(res.data.result).find((v) => Array.isArray(v)) || [];
  return raw.map((c) => ({
    time: c[0] * 1000,
    open: parseFloat(c[1]),
    high: parseFloat(c[2]),
    low: parseFloat(c[3]),
    close: parseFloat(c[4]),
    volume: parseFloat(c[6]),
    direction: parseFloat(c[4]) > parseFloat(c[1]) ? "UP" : "DOWN",
    change: ((parseFloat(c[4]) - parseFloat(c[1])) / parseFloat(c[1])) * 100,
  }));
}

async function fetchCoinGeckoMultiAsset() {
  const coins = ["bitcoin", "ethereum", "solana", "ripple"];
  const data = {};
  for (const coin of coins) {
    try {
      const res = await axios.get(
        `https://api.coingecko.com/api/v3/coins/${coin}/market_chart`,
        { params: { vs_currency: "usd", days: 1 }, timeout: 10000 }
      );
      data[coin] = res.data.prices;
    } catch {
      data[coin] = [];
    }
  }
  return data;
}

// ── Helpers ─────────────────────────────────────────────────

function buildCandles(prices) {
  const candles = [];
  for (let i = 0; i < prices.length - 1; i++) {
    const interval = (prices[i + 1][0] - prices[i][0]) / 60000;
    if (interval > 3 && interval < 10) {
      candles.push({
        time: prices[i][0],
        open: prices[i][1],
        close: prices[i + 1][1],
        direction: prices[i + 1][1] > prices[i][1] ? "UP" : "DOWN",
        change:
          ((prices[i + 1][1] - prices[i][1]) / prices[i][1]) * 100,
      });
    }
  }
  return candles;
}

function stats(trades) {
  if (trades.length === 0)
    return {
      trades: 0,
      wins: 0,
      losses: 0,
      winRate: "0.0",
      totalPnL: "0.00",
      avgPnL: "0.0000",
      roi: "0.0",
      maxDrawdown: "0.00",
      sharpe: "0.00",
      bestTrade: "0.00",
      worstTrade: "0.00",
    };

  const wins = trades.filter((t) => t.pnl > 0).length;
  const losses = trades.filter((t) => t.pnl <= 0).length;
  const pnls = trades.map((t) => t.pnl);
  const totalPnL = pnls.reduce((a, b) => a + b, 0);
  const avgPnL = totalPnL / trades.length;
  const totalRisked = trades.reduce((a, t) => a + t.cost, 0);

  let cumPnL = 0;
  let peak = 0;
  let maxDD = 0;
  for (const p of pnls) {
    cumPnL += p;
    if (cumPnL > peak) peak = cumPnL;
    const dd = peak - cumPnL;
    if (dd > maxDD) maxDD = dd;
  }

  const mean = avgPnL;
  const variance =
    pnls.reduce((a, b) => a + (b - mean) ** 2, 0) / pnls.length;
  const stdDev = Math.sqrt(variance);
  const sharpe = stdDev > 0 ? mean / stdDev : 0;

  return {
    trades: trades.length,
    wins,
    losses,
    winRate: ((wins / trades.length) * 100).toFixed(1),
    totalPnL: totalPnL.toFixed(4),
    avgPnL: avgPnL.toFixed(4),
    roi: ((totalPnL / totalRisked) * 100).toFixed(2),
    maxDrawdown: maxDD.toFixed(4),
    sharpe: sharpe.toFixed(3),
    bestTrade: Math.max(...pnls).toFixed(4),
    worstTrade: Math.min(...pnls).toFixed(4),
  };
}

// ── Strategy Backtests ──────────────────────────────────────

/**
 * STRATEGY 1: Spread Snipe
 * Simulate: random spread mispricings where Up+Down < $1.
 * In reality this is rare but risk-free. We model it as:
 * - At market open, there's a chance the spread is wide
 * - We use real BTC vol to estimate how often the book is
 *   mispriced (wider spread = more likely during high vol)
 */
function backtestSpreadSnipe(candles) {
  const trades = [];

  for (let i = 0; i < candles.length; i++) {
    const c = candles[i];
    const absChange = Math.abs(c.change);

    // Model: during high-vol candles, there's a chance of spread mispricing
    // Higher vol = market makers pull quotes = wider spread = opportunity
    // We simulate 5% of candles having a mispricing proportional to vol
    const mispricingProb = Math.min(absChange / 0.5, 0.15);
    const roll = Math.random();

    if (roll < mispricingProb) {
      // Spread is mispriced: Up + Down < $1.00
      // Typical spread discount: 1-4 cents
      const discount = 0.01 + Math.random() * 0.03;
      const cost = 1.0 - discount;
      const pnl = discount; // guaranteed $1 payout minus cost
      trades.push({ cost, pnl, candle: i });
    }
  }

  return trades;
}

/**
 * STRATEGY 2: Cross-Timeframe (5m informs 15m)
 * After first 5-min candle in a 15-min block, bet on 15-min direction
 * based on the 5-min result. If first 5-min was UP by a lot, bet UP on 15m.
 */
function backtestCrossTimeframe(candles) {
  const trades = [];

  // Group into 15-min blocks (3 candles each)
  for (let i = 0; i + 2 < candles.length; i += 3) {
    const c1 = candles[i];
    const c2 = candles[i + 1];
    const c3 = candles[i + 2];

    // 15-min result: net of all 3 candles
    const net15m = c1.change + c2.change + c3.change;
    const dir15m = net15m > 0 ? "UP" : "DOWN";

    // After seeing first candle, decide:
    // If first candle moved significantly (>0.05%), bet same direction on 15m
    if (Math.abs(c1.change) > 0.05) {
      const betDir = c1.direction;
      // Price to enter: assume 15m market is at 50c (hasn't repriced yet)
      const entryPrice = 0.50;
      const cost = entryPrice;
      const win = betDir === dir15m;
      const pnl = win ? 1.0 - cost : -cost;
      trades.push({ cost, pnl, betDir, dir15m, signal: c1.change.toFixed(3), candle: i });
    }
  }

  return trades;
}

/**
 * STRATEGY 3: Cross-Asset Correlation
 * When BTC 5-min goes UP, bet UP on ETH/SOL/XRP in the same time window.
 * Uses real multi-asset data from CoinGecko.
 */
function backtestCrossAsset(btcCandles, altCandles) {
  const trades = [];

  // Align BTC and alt candles by timestamp (within 3-min tolerance)
  for (let bi = 0; bi < btcCandles.length; bi++) {
    const btc = btcCandles[bi];
    if (Math.abs(btc.change) < 0.05) continue; // Only trade on significant BTC moves

    for (let ai = 0; ai < altCandles.length; ai++) {
      const alt = altCandles[ai];
      const timeDiff = Math.abs(btc.time - alt.time);
      if (timeDiff > 3 * 60 * 1000) continue; // Must be same time window

      // Bet that alt follows BTC direction
      const betDir = btc.direction;
      const altDir = alt.direction;

      // Assume alt market hasn't repriced yet (still 50c)
      const entryPrice = 0.50;
      const win = betDir === altDir;
      const pnl = win ? 1.0 - entryPrice : -entryPrice;
      trades.push({ cost: entryPrice, pnl, btcDir: btc.direction, altDir, candle: bi });
      break; // one trade per BTC candle
    }
  }

  return trades;
}

/**
 * STRATEGY 4: Momentum Autocorrelation
 * After each candle, bet that the next candle goes the same direction.
 * Entry at 50c.
 */
function backtestMomentum(candles) {
  const trades = [];

  for (let i = 1; i < candles.length; i++) {
    const prev = candles[i - 1];
    const curr = candles[i];

    // Bet same direction as previous candle
    const betDir = prev.direction;
    const entryPrice = 0.50;
    const win = betDir === curr.direction;
    const pnl = win ? 1.0 - entryPrice : -entryPrice;
    trades.push({ cost: entryPrice, pnl, betDir, actualDir: curr.direction, candle: i });
  }

  return trades;
}

/**
 * STRATEGY 4b: Enhanced Momentum (only bet after strong moves)
 * Same as momentum but only trade when the previous candle moved >0.1%.
 */
function backtestEnhancedMomentum(candles) {
  const trades = [];

  for (let i = 1; i < candles.length; i++) {
    const prev = candles[i - 1];
    const curr = candles[i];

    // Only trade after significant moves
    if (Math.abs(prev.change) < 0.10) continue;

    const betDir = prev.direction;
    const entryPrice = 0.50;
    const win = betDir === curr.direction;
    const pnl = win ? 1.0 - entryPrice : -entryPrice;
    trades.push({ cost: entryPrice, pnl, betDir, actualDir: curr.direction, signal: prev.change.toFixed(3), candle: i });
  }

  return trades;
}

/**
 * STRATEGY 4c: Streak Momentum
 * Only bet after 2+ consecutive same-direction candles.
 */
function backtestStreakMomentum(candles) {
  const trades = [];

  for (let i = 2; i < candles.length; i++) {
    const prev2 = candles[i - 2];
    const prev1 = candles[i - 1];
    const curr = candles[i];

    // Only bet if last 2 candles were same direction
    if (prev2.direction !== prev1.direction) continue;

    const betDir = prev1.direction;
    const entryPrice = 0.50;
    const win = betDir === curr.direction;
    const pnl = win ? 1.0 - entryPrice : -entryPrice;
    trades.push({ cost: entryPrice, pnl, betDir, actualDir: curr.direction, candle: i });
  }

  return trades;
}

/**
 * STRATEGY 4d: Mean Reversion
 * After each candle, bet the OPPOSITE direction.
 */
function backtestMeanReversion(candles) {
  const trades = [];

  for (let i = 1; i < candles.length; i++) {
    const prev = candles[i - 1];
    const curr = candles[i];

    const betDir = prev.direction === "UP" ? "DOWN" : "UP";
    const entryPrice = 0.50;
    const win = betDir === curr.direction;
    const pnl = win ? 1.0 - entryPrice : -entryPrice;
    trades.push({ cost: entryPrice, pnl, betDir, actualDir: curr.direction, candle: i });
  }

  return trades;
}

/**
 * STRATEGY 5: Volatility Straddle
 * Buy BOTH Up and Down at 45c each (total 90c) during volatile periods.
 * Win if the move is big enough that one side resolves near $1.
 * In Polymarket, payout is always $1 for the winning side, $0 for losing.
 * So cost = 45c + 45c = 90c. Payout = $1.00. Profit = 10c.
 * BUT this only works if you can actually buy at 45c — which requires
 * the market to be wide enough. We simulate using vol thresholds.
 */
function backtestVolatilityStraddle(candles) {
  const trades = [];

  // Look at rolling 12-candle (1-hour) volatility
  for (let i = 12; i < candles.length; i++) {
    const window = candles.slice(i - 12, i);
    const changes = window.map((c) => c.change);
    const vol = Math.sqrt(
      changes.reduce((a, b) => a + b * b, 0) / changes.length
    );

    // Only straddle during high-vol periods (vol > 0.15%)
    if (vol < 0.15) continue;

    // In high vol, assume we can buy Up at 45c and Down at 45c
    // (market is wide because MMs pulled back)
    const upCost = 0.45;
    const downCost = 0.45;
    const totalCost = upCost + downCost;

    const curr = candles[i];
    // Winner pays $1, loser pays $0
    const payout = 1.0;
    const pnl = payout - totalCost;
    trades.push({ cost: totalCost, pnl, vol: vol.toFixed(3), candle: i });
  }

  return trades;
}

/**
 * STRATEGY 5b: Selective Straddle (only during extreme vol)
 * Same but with higher vol threshold and better pricing assumption.
 */
function backtestSelectiveStraddle(candles) {
  const trades = [];

  for (let i = 12; i < candles.length; i++) {
    const window = candles.slice(i - 12, i);
    const changes = window.map((c) => c.change);
    const vol = Math.sqrt(
      changes.reduce((a, b) => a + b * b, 0) / changes.length
    );

    // Only during very high vol
    if (vol < 0.25) continue;

    // Better pricing in extreme vol: 40c each side
    const totalCost = 0.40 + 0.40;
    const pnl = 1.0 - totalCost;
    trades.push({ cost: totalCost, pnl, vol: vol.toFixed(3), candle: i });
  }

  return trades;
}

/**
 * STRATEGY 6: Liquidity Snipe at Market Open
 * Place limit order at 47c for predicted direction on new market.
 * If filled, you're in at 47c — if it resolves your way, $1 payout.
 * Model: assume 60% fill rate (books often tighten before your order hits).
 * Direction prediction: use last candle direction (momentum).
 */
function backtestLiquiditySnipe(candles) {
  const trades = [];

  for (let i = 1; i < candles.length; i++) {
    const prev = candles[i - 1];
    const curr = candles[i];

    // 60% chance our limit order gets filled at 47c
    if (Math.random() > 0.60) continue;

    const betDir = prev.direction; // momentum-based prediction
    const entryPrice = 0.47;
    const win = betDir === curr.direction;
    const pnl = win ? 1.0 - entryPrice : -entryPrice;
    trades.push({ cost: entryPrice, pnl, betDir, actualDir: curr.direction, candle: i });
  }

  return trades;
}

/**
 * STRATEGY 6b: Liquidity Snipe with volume filter
 * Only snipe on new markets following high-volume candles.
 */
function backtestLiquiditySnipeFiltered(candles) {
  const trades = [];

  for (let i = 1; i < candles.length; i++) {
    const prev = candles[i - 1];
    const curr = candles[i];

    // Only trade after high-vol candle (wider spreads expected)
    if (Math.abs(prev.change) < 0.10) continue;

    // 50% fill rate on the wider spreads
    if (Math.random() > 0.50) continue;

    const betDir = prev.direction;
    const entryPrice = 0.45; // better price on wider books
    const win = betDir === curr.direction;
    const pnl = win ? 1.0 - entryPrice : -entryPrice;
    trades.push({ cost: entryPrice, pnl, betDir, actualDir: curr.direction, candle: i });
  }

  return trades;
}

// ── Main ────────────────────────────────────────────────────

async function main() {
  console.log("╔══════════════════════════════════════════════════════════════╗");
  console.log("║  BTC 5-Min Arbitrage Strategy Backtester                   ║");
  console.log("╚══════════════════════════════════════════════════════════════╝\n");

  // Fetch data
  console.log("Fetching data...");
  const [krakenCandles, cgData] = await Promise.all([
    fetchKrakenCandles(),
    fetchCoinGeckoMultiAsset(),
  ]);

  console.log(`  Kraken: ${krakenCandles.length} BTC 5-min candles`);
  console.log(`  Period: ${new Date(krakenCandles[0].time).toISOString().slice(0,16)} → ${new Date(krakenCandles[krakenCandles.length-1].time).toISOString().slice(0,16)}`);

  const btcCG = buildCandles(cgData.bitcoin || []);
  const ethCG = buildCandles(cgData.ethereum || []);
  const solCG = buildCandles(cgData.solana || []);
  const xrpCG = buildCandles(cgData.ripple || []);

  console.log(`  CoinGecko 24h: BTC=${btcCG.length} ETH=${ethCG.length} SOL=${solCG.length} XRP=${xrpCG.length} candles\n`);

  // Quick data overview
  const upCount = krakenCandles.filter((c) => c.direction === "UP").length;
  const downCount = krakenCandles.filter((c) => c.direction === "DOWN").length;
  console.log("─── BTC 5-Min Baseline Stats ───────────────────────────────");
  console.log(`  Up candles:   ${upCount} (${((upCount / krakenCandles.length) * 100).toFixed(1)}%)`);
  console.log(`  Down candles: ${downCount} (${((downCount / krakenCandles.length) * 100).toFixed(1)}%)`);

  let sameDirCount = 0;
  for (let i = 1; i < krakenCandles.length; i++) {
    if (krakenCandles[i].direction === krakenCandles[i - 1].direction) sameDirCount++;
  }
  const autoCorr = ((sameDirCount / (krakenCandles.length - 1)) * 100).toFixed(1);
  console.log(`  Autocorrelation: ${autoCorr}% (same dir continues)`);

  const changes = krakenCandles.map((c) => c.change);
  const avgAbs = (changes.reduce((a, b) => a + Math.abs(b), 0) / changes.length).toFixed(4);
  const stdDev = Math.sqrt(changes.reduce((a, b) => a + b * b, 0) / changes.length).toFixed(4);
  console.log(`  Avg |move|: ${avgAbs}%  |  StdDev: ${stdDev}%\n`);

  // Run all backtests
  console.log("═══════════════════════════════════════════════════════════════");
  console.log("  RUNNING BACKTESTS ($1 per trade)");
  console.log("═══════════════════════════════════════════════════════════════\n");

  const strategies = [
    {
      name: "1. Spread Snipe (risk-free)",
      fn: () => backtestSpreadSnipe(krakenCandles),
      note: "Simulated — real spread arb depends on market creation timing",
    },
    {
      name: "2. Cross-Timeframe (5m→15m)",
      fn: () => backtestCrossTimeframe(krakenCandles),
      note: "Bet on 15m direction after seeing 1st 5m candle",
    },
    {
      name: "3. Cross-Asset Correlation",
      fn: () => backtestCrossAsset(btcCG, ethCG),
      note: "BTC→ETH correlation over 24h (CoinGecko data)",
    },
    {
      name: "3b. Cross-Asset (BTC→SOL)",
      fn: () => backtestCrossAsset(btcCG, solCG),
      note: "BTC→SOL correlation over 24h",
    },
    {
      name: "3c. Cross-Asset (BTC→XRP)",
      fn: () => backtestCrossAsset(btcCG, xrpCG),
      note: "BTC→XRP correlation over 24h",
    },
    {
      name: "4. Momentum (all candles)",
      fn: () => backtestMomentum(krakenCandles),
      note: "Bet same direction as last candle, every candle",
    },
    {
      name: "4b. Enhanced Momentum (|move|>0.1%)",
      fn: () => backtestEnhancedMomentum(krakenCandles),
      note: "Only bet after big moves",
    },
    {
      name: "4c. Streak Momentum (2+ same dir)",
      fn: () => backtestStreakMomentum(krakenCandles),
      note: "Only bet after 2 consecutive same-direction candles",
    },
    {
      name: "4d. Mean Reversion (bet opposite)",
      fn: () => backtestMeanReversion(krakenCandles),
      note: "Bet opposite direction of last candle",
    },
    {
      name: "5. Volatility Straddle (45c+45c)",
      fn: () => backtestVolatilityStraddle(krakenCandles),
      note: "Buy both sides at 45c during high-vol periods",
    },
    {
      name: "5b. Selective Straddle (40c+40c)",
      fn: () => backtestSelectiveStraddle(krakenCandles),
      note: "Buy both sides at 40c during extreme vol",
    },
    {
      name: "6. Liquidity Snipe (47c entry)",
      fn: () => backtestLiquiditySnipe(krakenCandles),
      note: "Limit order at 47c with momentum direction",
    },
    {
      name: "6b. Liq Snipe Filtered (45c, vol>0.1%)",
      fn: () => backtestLiquiditySnipeFiltered(krakenCandles),
      note: "Limit at 45c, only after big moves",
    },
  ];

  const results = [];

  for (const strat of strategies) {
    // Run 10 times for stochastic strategies and average
    let allStats = [];
    const hasRandomness = ["1.", "6."].some((p) => strat.name.startsWith(p));
    const runs = hasRandomness ? 50 : 1;

    for (let r = 0; r < runs; r++) {
      const trades = strat.fn();
      allStats.push(stats(trades));
    }

    // Average the stats
    const avgStats = {};
    const keys = Object.keys(allStats[0]);
    for (const k of keys) {
      if (k === "trades" || k === "wins" || k === "losses") {
        avgStats[k] = Math.round(
          allStats.reduce((a, s) => a + s[k], 0) / allStats.length
        );
      } else {
        avgStats[k] = (
          allStats.reduce((a, s) => a + parseFloat(s[k]), 0) / allStats.length
        ).toFixed(4);
      }
    }

    results.push({ name: strat.name, note: strat.note, stats: avgStats });

    console.log(`  ${strat.name}`);
    console.log(`  ${strat.note}`);
    console.log(`  ┌─────────────────────────────────────────────────┐`);
    console.log(`  │ Trades: ${String(avgStats.trades).padEnd(6)} │ Win Rate: ${String(avgStats.winRate + "%").padEnd(8)} │ ROI: ${String(avgStats.roi + "%").padEnd(10)} │`);
    console.log(`  │ P&L:    $${String(avgStats.totalPnL).padEnd(10)} │ Avg P&L: $${String(avgStats.avgPnL).padEnd(10)} │`);
    console.log(`  │ Max DD: $${String(avgStats.maxDrawdown).padEnd(10)} │ Sharpe:  ${String(avgStats.sharpe).padEnd(10)} │`);
    console.log(`  │ Best:   $${String(avgStats.bestTrade).padEnd(10)} │ Worst:  $${String(avgStats.worstTrade).padEnd(10)} │`);
    console.log(`  └─────────────────────────────────────────────────┘\n`);
  }

  // ── Final Rankings ────────────────────────────────────────
  console.log("\n═══════════════════════════════════════════════════════════════");
  console.log("  FINAL RANKINGS (sorted by ROI)");
  console.log("═══════════════════════════════════════════════════════════════\n");

  results.sort((a, b) => parseFloat(b.stats.roi) - parseFloat(a.stats.roi));

  console.log(
    "  " +
      "Rank".padEnd(5) +
      "Strategy".padEnd(42) +
      "Trades".padEnd(8) +
      "Win%".padEnd(8) +
      "ROI".padEnd(10) +
      "Total P&L".padEnd(13) +
      "Sharpe".padEnd(8)
  );
  console.log("  " + "─".repeat(89));

  results.forEach((r, i) => {
    const roi = parseFloat(r.stats.roi);
    const marker = roi > 0 ? " ✅" : roi === 0 ? " ➖" : " ❌";
    console.log(
      "  " +
        `#${i + 1}`.padEnd(5) +
        r.name.padEnd(42) +
        String(r.stats.trades).padEnd(8) +
        (r.stats.winRate + "%").padEnd(8) +
        (r.stats.roi + "%").padEnd(10) +
        ("$" + r.stats.totalPnL).padEnd(13) +
        r.stats.sharpe.padEnd(8) +
        marker
    );
  });

  // Highlight winner
  const winner = results[0];
  console.log("\n═══════════════════════════════════════════════════════════════");
  console.log(`  🏆 BEST STRATEGY: ${winner.name}`);
  console.log("═══════════════════════════════════════════════════════════════");
  console.log(`  Win Rate: ${winner.stats.winRate}%`);
  console.log(`  ROI: ${winner.stats.roi}%`);
  console.log(`  Total P&L: $${winner.stats.totalPnL} over ${winner.stats.trades} trades`);
  console.log(`  Sharpe Ratio: ${winner.stats.sharpe}`);
  console.log(`  ${winner.note}\n`);

  // Runner-up analysis
  if (results.length > 1) {
    const profitable = results.filter((r) => parseFloat(r.stats.roi) > 0);
    console.log(`  📊 Profitable strategies: ${profitable.length}/${results.length}`);
    
    if (profitable.length > 1) {
      console.log("\n  Top 3 by ROI:");
      profitable.slice(0, 3).forEach((r, i) => {
        console.log(`    ${i + 1}. ${r.name} — ${r.stats.roi}% ROI, ${r.stats.winRate}% win rate`);
      });
    }

    const bestSharpe = [...results].sort((a, b) => parseFloat(b.stats.sharpe) - parseFloat(a.stats.sharpe))[0];
    console.log(`\n  📈 Best risk-adjusted (Sharpe): ${bestSharpe.name} (${bestSharpe.stats.sharpe})`);

    const mostTrades = [...results].sort((a, b) => b.stats.trades - a.stats.trades)[0];
    console.log(`  📊 Most trade opportunities: ${mostTrades.name} (${mostTrades.stats.trades} trades)`);
  }

  console.log("\n═══════════════════════════════════════════════════════════════\n");
}

main().catch(console.error);
