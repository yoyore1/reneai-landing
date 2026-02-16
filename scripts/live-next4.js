#!/usr/bin/env node

const axios = require("axios");

const GAMMA = "https://gamma-api.polymarket.com";
const CLOB = "https://clob.polymarket.com";
const FEE = 0.02;
const BET = 100;

async function getMarket(slug) {
  const res = await axios.get(`${GAMMA}/markets`, { params: { slug }, timeout: 5000 });
  return (res.data || [])[0] || null;
}

async function getBook(tokenId) {
  try {
    const res = await axios.get(`${CLOB}/book`, { params: { token_id: tokenId }, timeout: 8000 });
    const bids = (res.data.bids || []).sort((a, b) => parseFloat(b.price) - parseFloat(a.price));
    const asks = (res.data.asks || []).sort((a, b) => parseFloat(a.price) - parseFloat(b.price));
    return { bids, asks };
  } catch { return { bids: [], asks: [] }; }
}

async function pollResult(slug, timeoutMs) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const m = await getMarket(slug);
    if (m) {
      const p = m.outcomePrices ? JSON.parse(m.outcomePrices) : [];
      const up = p[0] ? parseFloat(p[0]) : null;
      if (up !== null && (up > 0.95 || up < 0.05)) {
        return up > 0.5 ? "UP" : "DOWN";
      }
    }
    await new Promise(r => setTimeout(r, 10000));
    process.stdout.write(".");
  }
  return null;
}

function log(msg) { console.log(msg); }

async function main() {
  log("╔════════════════════════════════════════════════════════════╗");
  log("║  LIVE SIM — Next 4 BTC 5-Min Markets, Real Order Book   ║");
  log("║  $100 per trade, actual bid/ask, wait for resolution     ║");
  log("╚════════════════════════════════════════════════════════════╝\n");

  // Find the next 4 unresolved markets near current time
  const now = Math.floor(Date.now() / 1000);
  const base = Math.ceil(now / 300) * 300;

  const targets = [];
  for (let ts = base - 600; ts < base + 1800 && targets.length < 4; ts += 300) {
    const slug = "btc-updown-5m-" + ts;
    const m = await getMarket(slug);
    if (!m) continue;
    const p = m.outcomePrices ? JSON.parse(m.outcomePrices) : [];
    const up = p[0] ? parseFloat(p[0]) : null;
    const resolved = up !== null && (up > 0.95 || up < 0.05);
    if (!resolved) {
      targets.push({ slug, ts, m });
    }
  }

  if (targets.length < 4) {
    log("Only found " + targets.length + " unresolved markets. Need 4.");
    return;
  }

  // Get the last resolved market for strategy signal
  let lastResult = null;
  let prevResults = [];
  for (let ts = targets[0].ts - 300; ts >= targets[0].ts - 1500; ts -= 300) {
    const m = await getMarket("btc-updown-5m-" + ts);
    if (m) {
      const p = m.outcomePrices ? JSON.parse(m.outcomePrices) : [];
      const up = p[0] ? parseFloat(p[0]) : null;
      if (up !== null && (up > 0.95 || up < 0.05)) {
        const r = up > 0.5 ? "UP" : "DOWN";
        prevResults.push(r);
        if (!lastResult) lastResult = r;
      }
    }
  }

  log("Last resolved: " + (lastResult || "UNKNOWN"));
  log("Recent sequence: " + (prevResults.length > 0 ? prevResults.join(" → ") : "none") + "\n");

  // Define strategies
  const strategies = [
    {
      name: "Always UP",
      decide: () => "UP",
    },
    {
      name: "Always DOWN",
      decide: () => "DOWN",
    },
    {
      name: "Momentum",
      decide: (last) => last || "UP",
    },
    {
      name: "Mean Reversion",
      decide: (last) => last ? (last === "UP" ? "DOWN" : "UP") : "DOWN",
    },
    {
      name: "3-Streak Fade",
      decide: (last, history) => {
        if (history.length >= 3 && history[0] === history[1] && history[1] === history[2]) {
          return history[0] === "UP" ? "DOWN" : "UP";
        }
        return null;
      },
    },
  ];

  // Process each market
  const allTrades = [];

  for (let mi = 0; mi < 4; mi++) {
    const { slug, m } = targets[mi];
    const tokens = m.clobTokenIds ? JSON.parse(m.clobTokenIds) : [];
    const outcomes = m.outcomes ? JSON.parse(m.outcomes) : ["Up", "Down"];
    const end = new Date(m.endDate);
    const minsLeft = ((end - Date.now()) / 60000).toFixed(1);

    log("═══════════════════════════════════════════════════════════");
    log("MARKET " + (mi + 1) + "/4: " + m.question);
    log("Resolves in ~" + minsLeft + " min (" + m.endDate + ")");

    // Fetch order books
    const upBook = tokens.length >= 2 ? await getBook(tokens[0]) : { bids: [], asks: [] };
    const downBook = tokens.length >= 2 ? await getBook(tokens[1]) : { bids: [], asks: [] };

    const upAsk = upBook.asks.length > 0 ? parseFloat(upBook.asks[0].price) : null;
    const upBid = upBook.bids.length > 0 ? parseFloat(upBook.bids[0].price) : null;
    const dnAsk = downBook.asks.length > 0 ? parseFloat(downBook.asks[0].price) : null;
    const dnBid = downBook.bids.length > 0 ? parseFloat(downBook.bids[0].price) : null;

    log("Order Book:");
    log("  UP:   bid " + (upBid ? (upBid * 100).toFixed(0) + "c" : "—") + " / ask " + (upAsk ? (upAsk * 100).toFixed(0) + "c" : "—") + "  depth: $" + upBook.bids.reduce((a, b) => a + parseFloat(b.size), 0).toFixed(0) + " / $" + upBook.asks.reduce((a, b) => a + parseFloat(b.size), 0).toFixed(0));
    log("  DOWN: bid " + (dnBid ? (dnBid * 100).toFixed(0) + "c" : "—") + " / ask " + (dnAsk ? (dnAsk * 100).toFixed(0) + "c" : "—") + "  depth: $" + downBook.bids.reduce((a, b) => a + parseFloat(b.size), 0).toFixed(0) + " / $" + downBook.asks.reduce((a, b) => a + parseFloat(b.size), 0).toFixed(0));
    log("");

    // Place simulated trades for each strategy
    const marketTrades = [];
    for (const strat of strategies) {
      const side = strat.decide(lastResult, prevResults);
      if (side === null) {
        log("  " + strat.name.padEnd(18) + "→ SKIP (no signal)");
        marketTrades.push({ strat: strat.name, slug, market: m.question, side: "SKIP", entry: 0, shares: 0, spent: 0 });
        continue;
      }

      const book = side === "UP" ? upBook : downBook;
      const bestAsk = book.asks.length > 0 ? parseFloat(book.asks[0].price) : 0.51;
      const shares = BET / bestAsk;

      log("  " + strat.name.padEnd(18) + "→ BUY " + side + " at " + (bestAsk * 100).toFixed(0) + "c ask  |  " + shares.toFixed(1) + " shares  |  $" + BET + " spent");
      marketTrades.push({ strat: strat.name, slug, market: m.question, side, entry: bestAsk, shares, spent: BET });
    }
    allTrades.push(...marketTrades);

    // Wait for resolution
    log("\n  Waiting for resolution...");
    const result = await pollResult(slug, 10 * 60 * 1000);

    if (result) {
      log(" RESOLVED: " + result + "\n");

      // Update history for next market's signals
      prevResults.unshift(result);
      lastResult = result;

      // Calculate P&L for each trade on this market
      for (const t of marketTrades) {
        if (t.side === "SKIP") {
          t.result = result;
          t.pnl = 0;
          t.won = null;
          continue;
        }
        t.result = result;
        t.won = t.side === result;
        if (t.won) {
          const payout = t.shares * 1.0;
          const profit = payout - t.spent;
          t.pnl = profit - profit * FEE;
        } else {
          t.pnl = -t.spent;
        }
        const icon = t.won ? "  ✅" : "  ❌";
        log(icon + " " + t.strat.padEnd(18) + "Bought " + t.side + " @ " + (t.entry * 100).toFixed(0) + "c → " + result + " → P&L: " + (t.pnl >= 0 ? "+" : "") + "$" + t.pnl.toFixed(2));
      }
    } else {
      log(" TIMEOUT — did not resolve in 10 min\n");
      for (const t of marketTrades) { t.result = null; t.pnl = 0; t.won = null; }
    }

    log("");
  }

  // ── Final Summary ─────────────────────────────────────────
  log("╔════════════════════════════════════════════════════════════╗");
  log("║  FINAL RESULTS — 4 Markets                               ║");
  log("╚════════════════════════════════════════════════════════════╝\n");

  const resolved = allTrades.filter(t => t.result !== null && t.side !== "SKIP");
  const stratMap = {};

  for (const t of resolved) {
    if (!stratMap[t.strat]) stratMap[t.strat] = { wins: 0, losses: 0, pnl: 0, trades: 0, spent: 0 };
    stratMap[t.strat].trades++;
    stratMap[t.strat].spent += t.spent;
    stratMap[t.strat].pnl += t.pnl;
    if (t.won) stratMap[t.strat].wins++;
    else stratMap[t.strat].losses++;
  }

  log("  " + "Strategy".padEnd(20) + "W-L".padEnd(8) + "Win%".padEnd(8) + "P&L".padEnd(14) + "ROI");
  log("  " + "─".repeat(58));

  const sorted = Object.entries(stratMap).sort((a, b) => b[1].pnl - a[1].pnl);
  for (const [name, s] of sorted) {
    const wr = s.trades > 0 ? ((s.wins / s.trades) * 100).toFixed(0) : "0";
    const roi = s.spent > 0 ? ((s.pnl / s.spent) * 100).toFixed(1) : "0.0";
    const icon = s.pnl > 0 ? " ✅" : s.pnl < 0 ? " ❌" : " ➖";
    log("  " + name.padEnd(20) + (s.wins + "-" + s.losses).padEnd(8) + (wr + "%").padEnd(8) + ((s.pnl >= 0 ? "+" : "") + "$" + s.pnl.toFixed(2)).padEnd(14) + roi + "%" + icon);
  }

  const best = sorted[0];
  log("\n  🏆 WINNER: " + best[0] + " — " + (best[1].pnl >= 0 ? "+" : "") + "$" + best[1].pnl.toFixed(2));
  log("");
}

main().catch(console.error);
