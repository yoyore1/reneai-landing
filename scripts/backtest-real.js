#!/usr/bin/env node

/**
 * Backtest strategies against REAL resolved Polymarket BTC 5-min markets.
 *
 * Data: 289 resolved "Bitcoin Up or Down" 5-min markets from Polymarket
 *       with real outcomes and ~$80-100k volume per market.
 */

const axios = require("axios");

const GAMMA = "https://gamma-api.polymarket.com";

async function fetchResolved() {
  const refTs = 1771308300;
  const all = [];

  for (let ts = refTs; ts > refTs - 48 * 3600; ts -= 300) {
    const slug = "btc-updown-5m-" + ts;
    try {
      const res = await axios.get(`${GAMMA}/markets`, {
        params: { slug },
        timeout: 4000,
      });
      const data = res.data || [];
      if (data.length > 0) {
        const m = data[0];
        const prices = m.outcomePrices ? JSON.parse(m.outcomePrices) : [];
        const up = prices[0] ? parseFloat(prices[0]) : null;
        if (up !== null && (up > 0.9 || up < 0.1)) {
          all.push({
            ts,
            q: m.question,
            result: up > 0.9 ? "UP" : "DOWN",
            volume: parseFloat(m.volume || 0),
          });
        }
      }
    } catch {}

    if ((refTs - ts) % 9000 === 0 && ts !== refTs) {
      process.stdout.write(
        `  Loading... ${((refTs - ts) / 3600).toFixed(1)}h back, ${all.length} resolved\r`
      );
      await new Promise((r) => setTimeout(r, 80));
    }
  }

  // Sort chronologically (oldest first)
  all.sort((a, b) => a.ts - b.ts);
  return all;
}

function fmt(n) {
  return (n >= 0 ? "+" : "") + "$" + n.toFixed(2);
}

function pct(n) {
  return (n >= 0 ? "+" : "") + n.toFixed(1) + "%";
}

function runStats(trades, label) {
  if (trades.length === 0) return null;
  const wins = trades.filter((t) => t.pnl > 0).length;
  const total = trades.length;
  const pnls = trades.map((t) => t.pnl);
  const totalPnL = pnls.reduce((a, b) => a + b, 0);
  const totalCost = trades.reduce((a, t) => a + t.cost, 0);
  const avgPnL = totalPnL / total;

  let cumPnL = 0, peak = 0, maxDD = 0;
  for (const p of pnls) {
    cumPnL += p;
    if (cumPnL > peak) peak = cumPnL;
    if (peak - cumPnL > maxDD) maxDD = peak - cumPnL;
  }

  const variance = pnls.reduce((a, b) => a + (b - avgPnL) ** 2, 0) / total;
  const stdDev = Math.sqrt(variance);
  const sharpe = stdDev > 0.001 ? avgPnL / stdDev : avgPnL > 0 ? 99 : 0;

  return {
    label,
    trades: total,
    wins,
    losses: total - wins,
    winRate: (wins / total) * 100,
    totalPnL,
    roi: (totalPnL / totalCost) * 100,
    avgPnL,
    maxDD,
    sharpe,
  };
}

function printStats(s) {
  if (!s) { console.log("    No trades.\n"); return; }
  const winCol = s.roi > 0 ? "\x1b[32m" : s.roi < 0 ? "\x1b[31m" : "\x1b[33m";
  const r = "\x1b[0m";
  console.log(`    Trades: ${s.trades}  |  Wins: ${s.wins}  |  Losses: ${s.losses}  |  Win Rate: ${s.winRate.toFixed(1)}%`);
  console.log(`    ${winCol}P&L: ${fmt(s.totalPnL)}  |  ROI: ${pct(s.roi)}  |  Avg/trade: ${fmt(s.avgPnL)}${r}`);
  console.log(`    Max Drawdown: $${s.maxDD.toFixed(2)}  |  Sharpe: ${s.sharpe.toFixed(2)}\n`);
}

async function main() {
  console.log("╔═══════════════════════════════════════════════════════════════╗");
  console.log("║  REAL Polymarket BTC 5-Min — Strategy Backtest              ║");
  console.log("║  Using actual resolved market outcomes                       ║");
  console.log("╚═══════════════════════════════════════════════════════════════╝\n");

  const markets = await fetchResolved();
  console.log(`\n\n  Loaded ${markets.length} resolved markets\n`);

  if (markets.length === 0) {
    console.log("  No resolved markets found. Exiting.");
    return;
  }

  // Baseline
  const ups = markets.filter((m) => m.result === "UP").length;
  const downs = markets.filter((m) => m.result === "DOWN").length;
  let sameDir = 0, streakLen = 1, maxStreak = 1, maxStreakDir = markets[0].result;
  for (let i = 1; i < markets.length; i++) {
    if (markets[i].result === markets[i - 1].result) {
      sameDir++;
      streakLen++;
      if (streakLen > maxStreak) {
        maxStreak = streakLen;
        maxStreakDir = markets[i].result;
      }
    } else {
      streakLen = 1;
    }
  }

  const totalVol = markets.reduce((a, m) => a + m.volume, 0);
  const avgVol = totalVol / markets.length;

  console.log("──────────────────────────────────────────────────────────");
  console.log("  BASELINE (real Polymarket data)");
  console.log("──────────────────────────────────────────────────────────");
  console.log(`  Markets:          ${markets.length}`);
  console.log(`  UP outcomes:      ${ups} (${((ups / markets.length) * 100).toFixed(1)}%)`);
  console.log(`  DOWN outcomes:    ${downs} (${((downs / markets.length) * 100).toFixed(1)}%)`);
  console.log(`  Autocorrelation:  ${((sameDir / (markets.length - 1)) * 100).toFixed(1)}% same dir`);
  console.log(`  Max streak:       ${maxStreak} consecutive ${maxStreakDir}`);
  console.log(`  Total volume:     $${(totalVol / 1e6).toFixed(1)}M`);
  console.log(`  Avg vol/market:   $${(avgVol / 1000).toFixed(0)}k`);
  console.log();

  // ── STRATEGY TESTS ──────────────────────────────────────

  const FEE = 0.02;
  const allResults = [];

  // S1: Always bet UP (simple baseline)
  console.log("  TEST 1: Always Bet UP @ 50c");
  {
    const trades = markets.map((m) => ({
      cost: 0.5,
      pnl: m.result === "UP" ? 0.5 - 0.5 * FEE : -0.5,
    }));
    const s = runStats(trades, "Always Bet UP");
    printStats(s);
    allResults.push(s);
  }

  // S2: Always bet DOWN
  console.log("  TEST 2: Always Bet DOWN @ 50c");
  {
    const trades = markets.map((m) => ({
      cost: 0.5,
      pnl: m.result === "DOWN" ? 0.5 - 0.5 * FEE : -0.5,
    }));
    const s = runStats(trades, "Always Bet DOWN");
    printStats(s);
    allResults.push(s);
  }

  // S3: Momentum — bet same direction as previous market
  console.log("  TEST 3: Momentum (bet same dir as last outcome)");
  {
    const trades = [];
    for (let i = 1; i < markets.length; i++) {
      const betDir = markets[i - 1].result;
      const win = betDir === markets[i].result;
      trades.push({
        cost: 0.5,
        pnl: win ? 0.5 - 0.5 * FEE : -0.5,
      });
    }
    const s = runStats(trades, "Momentum");
    printStats(s);
    allResults.push(s);
  }

  // S4: Mean Reversion — bet opposite of previous market
  console.log("  TEST 4: Mean Reversion (bet opposite of last outcome)");
  {
    const trades = [];
    for (let i = 1; i < markets.length; i++) {
      const betDir = markets[i - 1].result === "UP" ? "DOWN" : "UP";
      const win = betDir === markets[i].result;
      trades.push({
        cost: 0.5,
        pnl: win ? 0.5 - 0.5 * FEE : -0.5,
      });
    }
    const s = runStats(trades, "Mean Reversion");
    printStats(s);
    allResults.push(s);
  }

  // S5: Streak Momentum — only bet after 2+ same dir in a row
  console.log("  TEST 5: Streak Momentum (after 2+ same dir, bet continuation)");
  {
    const trades = [];
    for (let i = 2; i < markets.length; i++) {
      if (markets[i - 1].result !== markets[i - 2].result) continue;
      const betDir = markets[i - 1].result;
      const win = betDir === markets[i].result;
      trades.push({
        cost: 0.5,
        pnl: win ? 0.5 - 0.5 * FEE : -0.5,
      });
    }
    const s = runStats(trades, "Streak Momentum");
    printStats(s);
    allResults.push(s);
  }

  // S6: Streak Fade — after 2+ same dir, bet reversal
  console.log("  TEST 6: Streak Fade (after 2+ same dir, bet reversal)");
  {
    const trades = [];
    for (let i = 2; i < markets.length; i++) {
      if (markets[i - 1].result !== markets[i - 2].result) continue;
      const betDir = markets[i - 1].result === "UP" ? "DOWN" : "UP";
      const win = betDir === markets[i].result;
      trades.push({
        cost: 0.5,
        pnl: win ? 0.5 - 0.5 * FEE : -0.5,
      });
    }
    const s = runStats(trades, "Streak Fade");
    printStats(s);
    allResults.push(s);
  }

  // S7: 3+ Streak Fade
  console.log("  TEST 7: Long Streak Fade (after 3+ same dir, bet reversal)");
  {
    const trades = [];
    for (let i = 3; i < markets.length; i++) {
      if (
        markets[i - 1].result !== markets[i - 2].result ||
        markets[i - 2].result !== markets[i - 3].result
      )
        continue;
      const betDir = markets[i - 1].result === "UP" ? "DOWN" : "UP";
      const win = betDir === markets[i].result;
      trades.push({
        cost: 0.5,
        pnl: win ? 0.5 - 0.5 * FEE : -0.5,
      });
    }
    const s = runStats(trades, "3-Streak Fade");
    printStats(s);
    allResults.push(s);
  }

  // S8: Volume-weighted momentum (bet dir of higher-vol market)
  console.log("  TEST 8: Volume Momentum (bet dir of higher-vol prev market)");
  {
    const avgV = totalVol / markets.length;
    const trades = [];
    for (let i = 1; i < markets.length; i++) {
      if (markets[i - 1].volume < avgV) continue; // only after high-vol candles
      const betDir = markets[i - 1].result;
      const win = betDir === markets[i].result;
      trades.push({
        cost: 0.5,
        pnl: win ? 0.5 - 0.5 * FEE : -0.5,
      });
    }
    const s = runStats(trades, "Volume Momentum");
    printStats(s);
    allResults.push(s);
  }

  // S9: Volume Fade (bet opposite after high-vol candle)
  console.log("  TEST 9: Volume Fade (bet opposite after high-vol market)");
  {
    const avgV = totalVol / markets.length;
    const trades = [];
    for (let i = 1; i < markets.length; i++) {
      if (markets[i - 1].volume < avgV) continue;
      const betDir = markets[i - 1].result === "UP" ? "DOWN" : "UP";
      const win = betDir === markets[i].result;
      trades.push({
        cost: 0.5,
        pnl: win ? 0.5 - 0.5 * FEE : -0.5,
      });
    }
    const s = runStats(trades, "Volume Fade");
    printStats(s);
    allResults.push(s);
  }

  // S10: Alternation detector — bet opposite if last 2 alternated
  console.log("  TEST 10: Alternation (if last 2 flipped, bet flip again)");
  {
    const trades = [];
    for (let i = 2; i < markets.length; i++) {
      if (markets[i - 1].result === markets[i - 2].result) continue;
      // Last 2 alternated — bet opposite of last
      const betDir = markets[i - 1].result === "UP" ? "DOWN" : "UP";
      const win = betDir === markets[i].result;
      trades.push({
        cost: 0.5,
        pnl: win ? 0.5 - 0.5 * FEE : -0.5,
      });
    }
    const s = runStats(trades, "Alternation");
    printStats(s);
    allResults.push(s);
  }

  // S11: Discount entry — same as momentum but at 47c (liquidity snipe)
  console.log("  TEST 11: Momentum @ 47c entry (liquidity snipe)");
  {
    const trades = [];
    for (let i = 1; i < markets.length; i++) {
      const betDir = markets[i - 1].result;
      const win = betDir === markets[i].result;
      const entry = 0.47;
      trades.push({
        cost: entry,
        pnl: win ? 1.0 - entry - (1.0 - entry) * FEE : -entry,
      });
    }
    const s = runStats(trades, "Momentum @ 47c");
    printStats(s);
    allResults.push(s);
  }

  // S12: Always UP @ 47c
  console.log("  TEST 12: Always UP @ 47c entry");
  {
    const trades = markets.map((m) => ({
      cost: 0.47,
      pnl: m.result === "UP" ? 1.0 - 0.47 - 0.53 * FEE : -0.47,
    }));
    const s = runStats(trades, "Always UP @ 47c");
    printStats(s);
    allResults.push(s);
  }

  // S13: Majority vote — look at last 5 markets, bet majority direction
  console.log("  TEST 13: Majority Vote (last 5 outcomes)");
  {
    const trades = [];
    for (let i = 5; i < markets.length; i++) {
      const last5 = markets.slice(i - 5, i);
      const upCount = last5.filter((m) => m.result === "UP").length;
      const betDir = upCount >= 3 ? "UP" : "DOWN";
      const win = betDir === markets[i].result;
      trades.push({
        cost: 0.5,
        pnl: win ? 0.5 - 0.5 * FEE : -0.5,
      });
    }
    const s = runStats(trades, "Majority Vote (5)");
    printStats(s);
    allResults.push(s);
  }

  // ── RANKINGS ────────────────────────────────────────────
  console.log("═══════════════════════════════════════════════════════════════");
  console.log("  FINAL RANKINGS — Real Polymarket Data");
  console.log("═══════════════════════════════════════════════════════════════\n");

  const valid = allResults.filter((s) => s !== null);
  valid.sort((a, b) => b.roi - a.roi);

  console.log(
    "  " +
      "Rank".padEnd(5) +
      "Strategy".padEnd(30) +
      "Trades".padEnd(8) +
      "Win%".padEnd(8) +
      "ROI".padEnd(10) +
      "P&L".padEnd(12) +
      "Sharpe".padEnd(8)
  );
  console.log("  " + "─".repeat(81));

  valid.forEach((s, i) => {
    const icon = s.roi > 1 ? " ✅" : s.roi > -1 ? " ➖" : " ❌";
    console.log(
      "  " +
        `#${i + 1}`.padEnd(5) +
        s.label.padEnd(30) +
        String(s.trades).padEnd(8) +
        (s.winRate.toFixed(1) + "%").padEnd(8) +
        pct(s.roi).padEnd(10) +
        fmt(s.totalPnL).padEnd(12) +
        s.sharpe.toFixed(2).padEnd(8) +
        icon
    );
  });

  const winner = valid[0];
  console.log(
    `\n  🏆 BEST: ${winner.label} — ${pct(winner.roi)} ROI, ${winner.winRate.toFixed(1)}% win rate, ${fmt(winner.totalPnL)} P&L`
  );

  const profitable = valid.filter((s) => s.roi > 0);
  console.log(`\n  ${profitable.length}/${valid.length} strategies profitable\n`);

  console.log("  KEY INSIGHT:");
  console.log("  ─────────────");
  console.log(`  BTC went UP ${((ups/markets.length)*100).toFixed(1)}% of the time in these ${markets.length} markets.`);
  console.log(`  Autocorrelation: ${((sameDir/(markets.length-1))*100).toFixed(1)}% — ${sameDir > (markets.length-1)/2 ? "slight momentum" : "slight mean reversion"}.`);
  console.log(`  Average volume per market: $${(avgVol/1000).toFixed(0)}k — significant liquidity.`);
  console.log(`  The 2% Polymarket fee means you need >51% win rate at 50c to profit.\n`);
}

main().catch(console.error);
