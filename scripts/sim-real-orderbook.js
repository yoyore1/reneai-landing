#!/usr/bin/env node

/**
 * Simulates ACTUAL Polymarket buys and sells on 290 real resolved
 * BTC 5-min markets using the real order book structure.
 *
 * How it works:
 *   - BUY: You hit the ask to get in. Real ask = 51c (2c spread from 50c mid).
 *   - HOLD to resolution: If you win, shares pay out $1. If you lose, $0.
 *   - SELL early: You could sell at the bid (49c) before resolution.
 *   - Polymarket fee: 2% on profit.
 *
 * This simulates $100 per trade, buying at real ask prices.
 */

const axios = require("axios");

const GAMMA = "https://gamma-api.polymarket.com";
const FEE = 0.02;
const TRADE_SIZE = 100; // $100 per trade

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
      const m = (res.data || [])[0];
      if (m) {
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
        `  Loading ${all.length} resolved...\r`
      );
      await new Promise((r) => setTimeout(r, 80));
    }
  }

  all.sort((a, b) => a.ts - b.ts);
  return all;
}

function simulate(markets, stratName, decideFn) {
  const trades = [];
  const ASK = 0.51; // Real ask price from order book
  const BID = 0.49; // Real bid price from order book

  for (let i = 0; i < markets.length; i++) {
    const decision = decideFn(markets, i);
    if (decision === null) continue; // skip

    const side = decision.side; // "UP" or "DOWN"
    const action = decision.action; // "BUY_AND_HOLD" or "BUY_AND_SELL"
    const entryPrice = decision.entry || ASK; // what we pay per share

    const shares = TRADE_SIZE / entryPrice;
    const spent = TRADE_SIZE;

    let pnl;
    let exitPrice;
    const outcome = markets[i].result;

    if (action === "BUY_AND_HOLD") {
      // Hold to resolution
      const won = side === outcome;
      if (won) {
        const payout = shares * 1.0;
        const profit = payout - spent;
        const fee = profit * FEE;
        pnl = profit - fee;
        exitPrice = 1.0;
      } else {
        pnl = -spent;
        exitPrice = 0;
      }
    } else if (action === "BUY_BOTH") {
      // Buy both sides — guaranteed resolution
      const upShares = (TRADE_SIZE / 2) / entryPrice;
      const downShares = (TRADE_SIZE / 2) / entryPrice;
      const totalSpent = TRADE_SIZE;
      // Winner pays $1 per share, loser pays $0
      const winShares = outcome === "UP" ? upShares : downShares;
      const payout = winShares * 1.0;
      const profit = payout - totalSpent;
      const fee = profit > 0 ? profit * FEE : 0;
      pnl = profit - fee;
      exitPrice = entryPrice;
    }

    trades.push({
      market: markets[i].q,
      side,
      action,
      entryPrice,
      shares: shares.toFixed(1),
      spent,
      outcome,
      won: side === outcome || action === "BUY_BOTH",
      pnl,
      exitPrice,
    });
  }

  return trades;
}

function printResults(name, trades) {
  if (trades.length === 0) {
    console.log(`  ${name}: NO TRADES\n`);
    return null;
  }

  const wins = trades.filter((t) => t.pnl > 0).length;
  const losses = trades.filter((t) => t.pnl <= 0).length;
  const totalPnL = trades.reduce((a, t) => a + t.pnl, 0);
  const totalSpent = trades.reduce((a, t) => a + t.spent, 0);
  const roi = (totalPnL / totalSpent) * 100;

  let cum = 0, peak = 0, maxDD = 0;
  for (const t of trades) {
    cum += t.pnl;
    if (cum > peak) peak = cum;
    if (peak - cum > maxDD) maxDD = peak - cum;
  }

  const g = roi > 0 ? "\x1b[32m" : "\x1b[31m";
  const r = "\x1b[0m";

  // Show first few trades as examples
  console.log(`  ${name}`);
  console.log(`  Trades: ${trades.length} | Wins: ${wins} | Losses: ${losses} | Win Rate: ${((wins / trades.length) * 100).toFixed(1)}%`);
  console.log(`  ${g}Total P&L: ${totalPnL >= 0 ? "+" : ""}$${totalPnL.toFixed(2)} | ROI: ${roi >= 0 ? "+" : ""}${roi.toFixed(1)}% | Avg/trade: ${totalPnL >= 0 ? "+" : ""}$${(totalPnL / trades.length).toFixed(2)}${r}`);
  console.log(`  Max Drawdown: $${maxDD.toFixed(2)} | Total Risked: $${totalSpent.toFixed(0)}`);
  console.log();

  // Show sample trades
  console.log("  Sample trades:");
  trades.slice(0, 3).forEach((t) => {
    const icon = t.pnl > 0 ? "✅" : "❌";
    console.log(
      `    ${icon} Bought ${t.side} @ ${(t.entryPrice * 100).toFixed(0)}c → Result: ${t.outcome} → P&L: ${t.pnl >= 0 ? "+" : ""}$${t.pnl.toFixed(2)}`
    );
    console.log(`       ${t.market}`);
  });
  if (trades.length > 3) console.log(`    ... and ${trades.length - 3} more trades`);
  console.log();

  return { name, trades: trades.length, wins, losses, winRate: (wins / trades.length) * 100, totalPnL, roi, maxDD };
}

async function main() {
  console.log("╔═══════════════════════════════════════════════════════════════╗");
  console.log("║  ORDER BOOK SIMULATION — Real Buys on 290 Resolved Markets  ║");
  console.log("║  $100 per trade | Buy @ 51c ask | Sell @ 49c bid           ║");
  console.log("║  2% Polymarket fee on profits                               ║");
  console.log("╚═══════════════════════════════════════════════════════════════╝\n");

  const markets = await fetchResolved();
  console.log(`\n  Loaded ${markets.length} resolved markets\n`);

  const ups = markets.filter((m) => m.result === "UP").length;
  console.log(`  Baseline: ${ups} UP (${((ups / markets.length) * 100).toFixed(1)}%) | ${markets.length - ups} DOWN (${(((markets.length - ups) / markets.length) * 100).toFixed(1)}%)\n`);
  console.log("══════════════════════════════════════════════════════════════\n");

  const allResults = [];

  // ── S1: Always buy UP at the ask (51c) ─────────────────
  console.log("─── STRATEGY 1: Always Buy UP @ 51c (real ask) ───────────");
  allResults.push(
    printResults(
      "Always UP @ 51c",
      simulate(markets, "up", (m, i) => ({
        side: "UP",
        action: "BUY_AND_HOLD",
        entry: 0.51,
      }))
    )
  );

  // ── S2: Always buy DOWN at the ask (51c) ───────────────
  console.log("─── STRATEGY 2: Always Buy DOWN @ 51c ────────────────────");
  allResults.push(
    printResults(
      "Always DOWN @ 51c",
      simulate(markets, "down", (m, i) => ({
        side: "DOWN",
        action: "BUY_AND_HOLD",
        entry: 0.51,
      }))
    )
  );

  // ── S3: Momentum @ 51c ────────────────────────────────
  console.log("─── STRATEGY 3: Momentum @ 51c (bet last result) ─────────");
  allResults.push(
    printResults(
      "Momentum @ 51c",
      simulate(markets, "mom", (m, i) => {
        if (i === 0) return null;
        return {
          side: m[i - 1].result,
          action: "BUY_AND_HOLD",
          entry: 0.51,
        };
      })
    )
  );

  // ── S4: Mean Reversion @ 51c ──────────────────────────
  console.log("─── STRATEGY 4: Mean Reversion @ 51c (bet opposite) ──────");
  allResults.push(
    printResults(
      "Mean Rev @ 51c",
      simulate(markets, "rev", (m, i) => {
        if (i === 0) return null;
        return {
          side: m[i - 1].result === "UP" ? "DOWN" : "UP",
          action: "BUY_AND_HOLD",
          entry: 0.51,
        };
      })
    )
  );

  // ── S5: 3-Streak Fade @ 51c ──────────────────────────
  console.log("─── STRATEGY 5: 3-Streak Fade @ 51c (reversal after 3+) ──");
  allResults.push(
    printResults(
      "3-Streak Fade @ 51c",
      simulate(markets, "fade3", (m, i) => {
        if (i < 3) return null;
        if (
          m[i - 1].result === m[i - 2].result &&
          m[i - 2].result === m[i - 3].result
        ) {
          return {
            side: m[i - 1].result === "UP" ? "DOWN" : "UP",
            action: "BUY_AND_HOLD",
            entry: 0.51,
          };
        }
        return null;
      })
    )
  );

  // ── S6: Streak Fade @ 51c (2+) ───────────────────────
  console.log("─── STRATEGY 6: 2-Streak Fade @ 51c ──────────────────────");
  allResults.push(
    printResults(
      "2-Streak Fade @ 51c",
      simulate(markets, "fade2", (m, i) => {
        if (i < 2) return null;
        if (m[i - 1].result === m[i - 2].result) {
          return {
            side: m[i - 1].result === "UP" ? "DOWN" : "UP",
            action: "BUY_AND_HOLD",
            entry: 0.51,
          };
        }
        return null;
      })
    )
  );

  // ── S7: Buy BOTH sides @ 51c (spread arb attempt) ────
  console.log("─── STRATEGY 7: Buy BOTH @ 51c (spread arb) ──────────────");
  allResults.push(
    printResults(
      "Buy Both @ 51c",
      simulate(markets, "both51", (m, i) => ({
        side: "UP",
        action: "BUY_BOTH",
        entry: 0.51,
      }))
    )
  );

  // ── S8: Momentum with limit order at 49c (bid) ───────
  console.log("─── STRATEGY 8: Momentum @ 49c limit (sit on bid) ────────");
  allResults.push(
    printResults(
      "Momentum @ 49c bid",
      simulate(markets, "mom49", (m, i) => {
        if (i === 0) return null;
        return {
          side: m[i - 1].result,
          action: "BUY_AND_HOLD",
          entry: 0.49,
        };
      })
    )
  );

  // ── S9: Always UP @ 49c limit ────────────────────────
  console.log("─── STRATEGY 9: Always UP @ 49c limit (sit on bid) ───────");
  allResults.push(
    printResults(
      "Always UP @ 49c",
      simulate(markets, "up49", (m, i) => ({
        side: "UP",
        action: "BUY_AND_HOLD",
        entry: 0.49,
      }))
    )
  );

  // ── S10: 3-Streak Fade @ 49c ─────────────────────────
  console.log("─── STRATEGY 10: 3-Streak Fade @ 49c limit ───────────────");
  allResults.push(
    printResults(
      "3-Streak Fade @ 49c",
      simulate(markets, "fade349", (m, i) => {
        if (i < 3) return null;
        if (
          m[i - 1].result === m[i - 2].result &&
          m[i - 2].result === m[i - 3].result
        ) {
          return {
            side: m[i - 1].result === "UP" ? "DOWN" : "UP",
            action: "BUY_AND_HOLD",
            entry: 0.49,
          };
        }
        return null;
      })
    )
  );

  // ── FINAL RANKINGS ────────────────────────────────────

  console.log("╔═══════════════════════════════════════════════════════════════╗");
  console.log("║  FINAL RANKINGS                                             ║");
  console.log("╚═══════════════════════════════════════════════════════════════╝\n");

  const valid = allResults.filter((r) => r !== null);
  valid.sort((a, b) => b.roi - a.roi);

  console.log(
    "  " +
      "#".padEnd(4) +
      "Strategy".padEnd(24) +
      "Trades".padEnd(8) +
      "Win%".padEnd(8) +
      "ROI".padEnd(10) +
      "P&L".padEnd(14) +
      "MaxDD".padEnd(10)
  );
  console.log("  " + "─".repeat(78));

  valid.forEach((r, i) => {
    const icon = r.roi > 1 ? " ✅" : r.roi > -1 ? " ➖" : " ❌";
    console.log(
      "  " +
        `#${i + 1}`.padEnd(4) +
        r.name.padEnd(24) +
        String(r.trades).padEnd(8) +
        (r.winRate.toFixed(1) + "%").padEnd(8) +
        ((r.roi >= 0 ? "+" : "") + r.roi.toFixed(1) + "%").padEnd(10) +
        ((r.totalPnL >= 0 ? "+" : "") + "$" + r.totalPnL.toFixed(2)).padEnd(14) +
        ("$" + r.maxDD.toFixed(2)).padEnd(10) +
        icon
    );
  });

  console.log("\n  KEY:");
  console.log("  @ 51c = market order (hit the ask, what you'd actually pay)");
  console.log("  @ 49c = limit order (sit on the bid, might not fill)");
  console.log("  All P&L includes 2% Polymarket fee on winnings");
  console.log("  $100 per trade, hold to resolution\n");

  const winner = valid[0];
  console.log(
    `  🏆 BEST STRATEGY: ${winner.name}`
  );
  console.log(
    `     ROI: ${winner.roi >= 0 ? "+" : ""}${winner.roi.toFixed(1)}% | Win Rate: ${winner.winRate.toFixed(1)}% | P&L: ${winner.totalPnL >= 0 ? "+" : ""}$${winner.totalPnL.toFixed(2)} over ${winner.trades} trades`
  );
  console.log();
}

main().catch(console.error);
