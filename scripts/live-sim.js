#!/usr/bin/env node

/**
 * LIVE SIMULATION — Places simulated orders on 4 real Polymarket
 * BTC 5-min markets using the actual order book, then waits for
 * resolution and calculates real P&L.
 *
 * Simulates a $100 bankroll, buying at the REAL ask price.
 */

const axios = require("axios");

const GAMMA = "https://gamma-api.polymarket.com";
const CLOB = "https://clob.polymarket.com";
const FEE = 0.02;
const BANKROLL = 100;

async function getMarketBySlug(slug) {
  try {
    const res = await axios.get(`${GAMMA}/markets`, {
      params: { slug },
      timeout: 5000,
    });
    return (res.data || [])[0] || null;
  } catch {
    return null;
  }
}

async function getBook(tokenId) {
  try {
    const res = await axios.get(`${CLOB}/book`, {
      params: { token_id: tokenId },
      timeout: 8000,
    });
    const bids = (res.data.bids || []).sort(
      (a, b) => parseFloat(b.price) - parseFloat(a.price)
    );
    const asks = (res.data.asks || []).sort(
      (a, b) => parseFloat(a.price) - parseFloat(b.price)
    );
    return { bids, asks };
  } catch {
    return { bids: [], asks: [] };
  }
}

function simulateBuy(book, dollars) {
  let spent = 0;
  let shares = 0;
  for (const ask of book.asks) {
    const price = parseFloat(ask.price);
    const available = parseFloat(ask.size);
    const canBuy = Math.min(available, (dollars - spent) / price);
    if (canBuy <= 0) break;
    shares += canBuy;
    spent += canBuy * price;
    if (spent >= dollars - 0.01) break;
  }
  return { shares, spent, avgPrice: spent > 0 ? spent / shares : 0 };
}

function simulateSell(book, sharesToSell) {
  let received = 0;
  let sold = 0;
  for (const bid of book.bids) {
    const price = parseFloat(bid.price);
    const available = parseFloat(bid.size);
    const canSell = Math.min(available, sharesToSell - sold);
    if (canSell <= 0) break;
    sold += canSell;
    received += canSell * price;
    if (sold >= sharesToSell - 0.01) break;
  }
  return { sold, received, avgPrice: sold > 0 ? received / sold : 0 };
}

async function waitForResolution(slug, maxWaitMs) {
  const start = Date.now();
  while (Date.now() - start < maxWaitMs) {
    const m = await getMarketBySlug(slug);
    if (m) {
      const prices = m.outcomePrices ? JSON.parse(m.outcomePrices) : [];
      const up = prices[0] ? parseFloat(prices[0]) : null;
      if (up !== null && (up > 0.9 || up < 0.1)) {
        return up > 0.9 ? "UP" : "DOWN";
      }
    }
    await new Promise((r) => setTimeout(r, 15000));
    process.stdout.write(".");
  }
  return null;
}

async function main() {
  console.log("╔═══════════════════════════════════════════════════════════════╗");
  console.log("║  LIVE SIMULATION — Real Polymarket Order Books              ║");
  console.log("║  Simulating actual buys/sells on 4 BTC 5-min markets       ║");
  console.log("║  $100 bankroll per trade                                     ║");
  console.log("╚═══════════════════════════════════════════════════════════════╝\n");

  // ── Step 1: Find the next 4 markets ──────────────────────

  const res = await axios.get(`${GAMMA}/markets`, {
    params: { order: "startDate", ascending: false, limit: 20 },
    timeout: 15000,
  });
  const btc5m = (res.data || [])
    .filter((m) => (m.slug || "").startsWith("btc-updown-5m"))
    .sort((a, b) => new Date(a.endDate) - new Date(b.endDate))
    .slice(0, 4);

  if (btc5m.length < 4) {
    console.log("Could not find 4 active BTC 5-min markets. Found: " + btc5m.length);
    return;
  }

  // ── Step 2: Get last 4 resolved for strategy signals ─────

  const firstTs = parseInt(btc5m[0].slug.split("-").pop());
  const history = [];
  for (let ts = firstTs - 300; ts >= firstTs - 1500; ts -= 300) {
    const slug = "btc-updown-5m-" + ts;
    const m = await getMarketBySlug(slug);
    if (m) {
      const prices = m.outcomePrices ? JSON.parse(m.outcomePrices) : [];
      const up = prices[0] ? parseFloat(prices[0]) : null;
      if (up !== null && (up > 0.9 || up < 0.1)) {
        history.push({ slug, result: up > 0.9 ? "UP" : "DOWN", q: m.question });
      }
    }
  }

  console.log("Recent resolved markets (for signals):");
  if (history.length === 0) {
    console.log("  None resolved yet in recent window.\n");
  } else {
    history.forEach((h) => console.log("  " + h.q + " => " + h.result));
    console.log();
  }

  // ── Step 3: Read order books + simulate trades ───────────

  const strategies = [
    { name: "Always UP", decide: () => "UP" },
    { name: "Always DOWN", decide: () => "DOWN" },
    {
      name: "Momentum",
      decide: (hist) => (hist.length > 0 ? hist[0].result : "UP"),
    },
    {
      name: "Mean Reversion",
      decide: (hist) =>
        hist.length > 0
          ? hist[0].result === "UP"
            ? "DOWN"
            : "UP"
          : "DOWN",
    },
    {
      name: "3-Streak Fade",
      decide: (hist) => {
        if (
          hist.length >= 3 &&
          hist[0].result === hist[1].result &&
          hist[1].result === hist[2].result
        ) {
          return hist[0].result === "UP" ? "DOWN" : "UP";
        }
        return null; // skip
      },
    },
  ];

  const trades = []; // { market, strategy, side, entry, shares, spent, result, pnl }

  for (const market of btc5m) {
    const tokens = market.clobTokenIds ? JSON.parse(market.clobTokenIds) : [];
    const outcomes = market.outcomes ? JSON.parse(market.outcomes) : ["Up", "Down"];

    if (tokens.length < 2) continue;

    // Fetch order books
    const upBook = await getBook(tokens[0]);
    const downBook = await getBook(tokens[1]);

    const bestUpAsk =
      upBook.asks.length > 0 ? parseFloat(upBook.asks[0].price) : null;
    const bestDownAsk =
      downBook.asks.length > 0 ? parseFloat(downBook.asks[0].price) : null;
    const bestUpBid =
      upBook.bids.length > 0 ? parseFloat(upBook.bids[0].price) : null;
    const bestDownBid =
      downBook.bids.length > 0 ? parseFloat(downBook.bids[0].price) : null;

    console.log("══════════════════════════════════════════════════════════");
    console.log("MARKET: " + market.question);
    console.log("Resolves: " + market.endDate);
    console.log(
      "Book: Up " +
        (bestUpBid ? (bestUpBid * 100).toFixed(0) : "?") +
        "c/" +
        (bestUpAsk ? (bestUpAsk * 100).toFixed(0) : "?") +
        "c | Down " +
        (bestDownBid ? (bestDownBid * 100).toFixed(0) : "?") +
        "c/" +
        (bestDownAsk ? (bestDownAsk * 100).toFixed(0) : "?") +
        "c"
    );
    console.log();

    for (const strat of strategies) {
      const decision = strat.decide(history);
      if (decision === null) {
        console.log("  " + strat.name + ": SKIP (no signal)");
        trades.push({
          market: market.question,
          slug: market.slug,
          strategy: strat.name,
          side: "SKIP",
          entry: 0,
          shares: 0,
          spent: 0,
        });
        continue;
      }

      const book = decision === "UP" ? upBook : downBook;
      const fill = simulateBuy(book, BANKROLL);

      console.log(
        "  " +
          strat.name +
          ": BUY " +
          decision +
          " — " +
          fill.shares.toFixed(2) +
          " shares @ " +
          (fill.avgPrice * 100).toFixed(1) +
          "c avg = $" +
          fill.spent.toFixed(2) +
          " spent"
      );

      trades.push({
        market: market.question,
        slug: market.slug,
        strategy: strat.name,
        side: decision,
        entry: fill.avgPrice,
        shares: fill.shares,
        spent: fill.spent,
      });
    }

    // Update history for next market's signals
    // (We don't know the result yet, so we can't update — strategies
    //  will use the same history for all 4 markets)

    console.log();
  }

  // ── Step 4: Wait for markets to resolve ──────────────────

  console.log("══════════════════════════════════════════════════════════");
  console.log("WAITING FOR MARKETS TO RESOLVE...");
  console.log("(Polling every 15 seconds, timeout 15 min per market)\n");

  const slugs = [...new Set(trades.map((t) => t.slug))];
  const results = {};

  for (const slug of slugs) {
    const marketName = trades.find((t) => t.slug === slug)?.market || slug;
    process.stdout.write("  " + marketName + " ");
    const result = await waitForResolution(slug, 15 * 60 * 1000);
    if (result) {
      results[slug] = result;
      console.log(" => " + result);
    } else {
      console.log(" => TIMEOUT (not resolved in 15 min)");
      results[slug] = null;
    }
  }

  // ── Step 5: Calculate P&L ────────────────────────────────

  console.log("\n══════════════════════════════════════════════════════════");
  console.log("RESULTS — ACTUAL SIMULATED P&L");
  console.log("══════════════════════════════════════════════════════════\n");

  const stratTotals = {};

  for (const trade of trades) {
    if (trade.side === "SKIP") continue;

    const outcome = results[trade.slug];
    if (outcome === null) {
      console.log(
        "  " + trade.strategy + " on " + trade.market + " — DID NOT RESOLVE"
      );
      continue;
    }

    const won = trade.side === outcome;
    let pnl;
    if (won) {
      // Shares resolve at $1 each
      const grossPayout = trade.shares * 1.0;
      const fee = (grossPayout - trade.spent) * FEE;
      pnl = grossPayout - trade.spent - fee;
    } else {
      // Shares resolve at $0
      pnl = -trade.spent;
    }

    trade.result = outcome;
    trade.won = won;
    trade.pnl = pnl;

    if (!stratTotals[trade.strategy]) {
      stratTotals[trade.strategy] = { wins: 0, losses: 0, totalPnL: 0, totalSpent: 0, trades: 0 };
    }
    stratTotals[trade.strategy].trades++;
    stratTotals[trade.strategy].totalSpent += trade.spent;
    stratTotals[trade.strategy].totalPnL += pnl;
    if (won) stratTotals[trade.strategy].wins++;
    else stratTotals[trade.strategy].losses++;

    const icon = won ? "✅" : "❌";
    const pnlStr = (pnl >= 0 ? "+" : "") + "$" + pnl.toFixed(2);
    console.log(
      "  " +
        icon +
        " " +
        trade.strategy.padEnd(18) +
        " Bought " +
        trade.side.padEnd(4) +
        " @ " +
        (trade.entry * 100).toFixed(1) +
        "c | Result: " +
        outcome +
        " | P&L: " +
        pnlStr
    );
    console.log("    " + trade.market);
  }

  // ── Strategy Totals ──────────────────────────────────────

  console.log("\n══════════════════════════════════════════════════════════");
  console.log("STRATEGY TOTALS ($100 per trade)");
  console.log("══════════════════════════════════════════════════════════\n");

  const sorted = Object.entries(stratTotals).sort(
    (a, b) => b[1].totalPnL - a[1].totalPnL
  );

  console.log(
    "  " +
      "Strategy".padEnd(20) +
      "W-L".padEnd(8) +
      "Win%".padEnd(8) +
      "P&L".padEnd(12) +
      "ROI".padEnd(10)
  );
  console.log("  " + "─".repeat(58));

  for (const [name, s] of sorted) {
    const winPct = ((s.wins / s.trades) * 100).toFixed(0);
    const roi = ((s.totalPnL / s.totalSpent) * 100).toFixed(1);
    const pnlStr = (s.totalPnL >= 0 ? "+" : "") + "$" + s.totalPnL.toFixed(2);
    const icon = s.totalPnL > 0 ? " ✅" : s.totalPnL < 0 ? " ❌" : " ➖";
    console.log(
      "  " +
        name.padEnd(20) +
        (s.wins + "-" + s.losses).padEnd(8) +
        (winPct + "%").padEnd(8) +
        pnlStr.padEnd(12) +
        (roi + "%").padEnd(10) +
        icon
    );
  }

  const bestStrat = sorted[0];
  console.log(
    "\n  🏆 BEST: " +
      bestStrat[0] +
      " — " +
      (bestStrat[1].totalPnL >= 0 ? "+" : "") +
      "$" +
      bestStrat[1].totalPnL.toFixed(2) +
      " P&L"
  );
  console.log("\n══════════════════════════════════════════════════════════\n");
}

main().catch(console.error);
