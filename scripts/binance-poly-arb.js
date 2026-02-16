#!/usr/bin/env node

/**
 * BINANCE → POLYMARKET LATENCY ARBITRAGE
 *
 * Strategy:
 *   1. Poll Binance BTC price every ~1 second
 *   2. Detect a spike (sharp move in price)
 *   3. Immediately buy the corresponding direction on Polymarket
 *      (price UP → buy "Up" shares, price DOWN → buy "Down" shares)
 *   4. Sell on Polymarket once the book reprices +10% from our entry
 *      (other traders who are slower catch up and push the price)
 *
 * The edge: Binance price moves BEFORE Polymarket's order book updates.
 * We front-run the Polymarket repricing.
 *
 * Usage: node scripts/binance-poly-arb.js
 *   (Run from Denmark VPN for full api.binance.com access,
 *    or it falls back to data-api.binance.vision)
 */

const axios = require("axios");

const GAMMA = "https://gamma-api.polymarket.com";
const CLOB = "https://clob.polymarket.com";

// Binance endpoints — tries main first, falls back to vision API
const BINANCE_ENDPOINTS = [
  "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
  "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT",
];

const CONFIG = {
  pollIntervalMs: 1000,        // Check Binance every 1 second
  spikeThreshold: 0.03,        // 0.03% move in 1 second = spike (about $20 on $68k)
  bigSpikeThreshold: 0.06,     // 0.06% = big spike ($40+)
  takeProfitPct: 10,           // Sell when Polymarket position is +10%
  stopLossPct: -50,            // Cut losses at -50%
  tradeSize: 100,              // $100 per trade
  maxOpenTrades: 4,            // Max simultaneous positions
  fee: 0.02,                   // 2% Polymarket fee
};

let binanceEndpoint = null;

async function getBTCPrice() {
  const endpoints = binanceEndpoint ? [binanceEndpoint] : BINANCE_ENDPOINTS;
  for (const url of endpoints) {
    try {
      const res = await axios.get(url, { timeout: 2000 });
      binanceEndpoint = url; // cache working endpoint
      return parseFloat(res.data.price);
    } catch {}
  }
  return null;
}

async function getCurrentMarket() {
  // Find the currently active BTC 5-min market (soonest to resolve)
  const now = Math.floor(Date.now() / 1000);
  const base = Math.floor(now / 300) * 300;

  // Try the current and next few time windows
  for (let ts = base - 300; ts <= base + 600; ts += 300) {
    const slug = "btc-updown-5m-" + ts;
    try {
      const res = await axios.get(`${GAMMA}/markets`, {
        params: { slug },
        timeout: 3000,
      });
      const m = (res.data || [])[0];
      if (!m) continue;

      const p = m.outcomePrices ? JSON.parse(m.outcomePrices) : [];
      const up = p[0] ? parseFloat(p[0]) : null;
      const resolved = up !== null && (up > 0.95 || up < 0.05);

      if (!resolved) {
        const tokens = m.clobTokenIds ? JSON.parse(m.clobTokenIds) : [];
        const end = new Date(m.endDate);
        const secsLeft = (end - Date.now()) / 1000;

        if (secsLeft > 30 && tokens.length >= 2) {
          return {
            slug,
            question: m.question,
            endDate: m.endDate,
            secsLeft,
            upToken: tokens[0],
            downToken: tokens[1],
            upPrice: up,
          };
        }
      }
    } catch {}
  }
  return null;
}

async function getBook(tokenId) {
  try {
    const res = await axios.get(`${CLOB}/book`, {
      params: { token_id: tokenId },
      timeout: 3000,
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

function bestAsk(book) {
  return book.asks.length > 0 ? parseFloat(book.asks[0].price) : null;
}

function bestBid(book) {
  return book.bids.length > 0 ? parseFloat(book.bids[0].price) : null;
}

async function runBacktest() {
  console.log("╔═══════════════════════════════════════════════════════════════╗");
  console.log("║  BINANCE → POLYMARKET Latency Arb — Backtest               ║");
  console.log("║  Detect BTC spikes on Binance, buy on Polymarket, sell +10% ║");
  console.log("╚═══════════════════════════════════════════════════════════════╝\n");

  // Step 1: Get recent BTC price history from Binance (1-second-level data)
  console.log("Fetching BTC price data...");

  let prices = [];
  try {
    // Get 1-minute klines for last 3 hours (180 candles)
    const res = await axios.get(
      "https://data-api.binance.vision/api/v3/klines",
      {
        params: { symbol: "BTCUSDT", interval: "1m", limit: 180 },
        timeout: 10000,
      }
    );
    prices = res.data.map((c) => ({
      time: c[0],
      open: parseFloat(c[1]),
      high: parseFloat(c[2]),
      low: parseFloat(c[3]),
      close: parseFloat(c[4]),
      volume: parseFloat(c[5]),
    }));
    console.log("  Got " + prices.length + " 1-min candles from Binance\n");
  } catch (e) {
    console.log("  Binance klines error: " + e.message);
    return;
  }

  // Step 2: Get resolved Polymarket markets for the same period
  console.log("Fetching matching Polymarket results...");

  const refTs = Math.floor(Date.now() / 1000);
  const polyMarkets = [];

  for (let ts = refTs; ts > refTs - 4 * 3600; ts -= 300) {
    const slug = "btc-updown-5m-" + ts;
    try {
      const res = await axios.get(`${GAMMA}/markets`, {
        params: { slug },
        timeout: 4000,
      });
      const m = (res.data || [])[0];
      if (m) {
        const p = m.outcomePrices ? JSON.parse(m.outcomePrices) : [];
        const up = p[0] ? parseFloat(p[0]) : null;
        polyMarkets.push({
          ts,
          slug,
          question: m.question,
          result: up > 0.9 ? "UP" : up < 0.1 ? "DOWN" : null,
          upPrice: up,
          volume: parseFloat(m.volume || 0),
        });
      }
    } catch {}
  }

  const resolved = polyMarkets.filter((m) => m.result !== null);
  console.log("  Found " + polyMarkets.length + " markets, " + resolved.length + " resolved\n");

  // Step 3: For each resolved Polymarket market, check what Binance
  // was doing in the 5 minutes before that market's resolution window
  console.log("═══════════════════════════════════════════════════════════════");
  console.log("  SPIKE DETECTION + TRADE SIMULATION");
  console.log("═══════════════════════════════════════════════════════════════\n");

  const trades = [];

  for (const pm of resolved) {
    // The market window is [ts, ts+300] in unix seconds
    const windowStart = pm.ts * 1000;
    const windowEnd = (pm.ts + 300) * 1000;

    // Find Binance candles during this window
    const windowCandles = prices.filter(
      (p) => p.time >= windowStart - 60000 && p.time <= windowEnd
    );

    if (windowCandles.length < 2) continue;

    // Detect spike: look at the first 1-2 candles of the window
    const first = windowCandles[0];
    const second = windowCandles.length > 1 ? windowCandles[1] : null;

    // Price change in first minute of the 5-min window
    const earlyChange = ((first.close - first.open) / first.open) * 100;

    // Combined first 2 minutes
    const twoMinChange = second
      ? ((second.close - first.open) / first.open) * 100
      : earlyChange;

    // Max spike within window (using high/low)
    const maxPrice = Math.max(...windowCandles.map((c) => c.high));
    const minPrice = Math.min(...windowCandles.map((c) => c.low));
    const maxUpSpike = ((maxPrice - first.open) / first.open) * 100;
    const maxDownSpike = ((first.open - minPrice) / first.open) * 100;

    const isSpikeUp = earlyChange > CONFIG.spikeThreshold;
    const isSpikeDown = earlyChange < -CONFIG.spikeThreshold;
    const isBigSpikeUp = earlyChange > CONFIG.bigSpikeThreshold;
    const isBigSpikeDown = earlyChange < -CONFIG.bigSpikeThreshold;

    if (!isSpikeUp && !isSpikeDown) continue;

    // We detected a spike! Simulate the trade.
    const direction = isSpikeUp ? "UP" : "DOWN";

    // Entry: We buy at 51c (ask) right when we see the spike
    // This is the critical assumption — we're faster than the Polymarket book
    const entry = 0.51;
    const shares = CONFIG.tradeSize / entry;
    const won = direction === pm.result;

    // Sell scenario 1: The book reprices and we sell at +10%
    // If we bought at 51c, +10% = sell at 56.1c
    // Sell scenario 2: Hold to resolution
    const sellPrice = entry * (1 + CONFIG.takeProfitPct / 100);

    let pnl;
    let exitType;

    if (won) {
      // Market goes our way — book reprices, we can sell at higher price
      // or hold to resolution ($1 payout)
      // Conservative: assume we sell at 10% profit
      const sellRevenue = shares * sellPrice;
      const profit = sellRevenue - CONFIG.tradeSize;
      const fee = profit * CONFIG.fee;
      pnl = profit - fee;
      exitType = "SELL +10%";
    } else {
      // Market goes against us — we lose
      // Could sell at a loss or hold to $0
      pnl = -CONFIG.tradeSize;
      exitType = "LOSS (held to $0)";
    }

    trades.push({
      market: pm.question,
      result: pm.result,
      direction,
      earlyChange: earlyChange.toFixed(3),
      entry,
      sellPrice: won ? sellPrice.toFixed(3) : "0",
      pnl,
      exitType,
      won,
      volume: pm.volume,
    });
  }

  // Print trades
  console.log("  Spike trades detected: " + trades.length + "\n");

  trades.forEach((t, i) => {
    const icon = t.won ? "✅" : "❌";
    console.log(
      `  ${icon} #${i + 1} ${t.market}`
    );
    console.log(
      `     Binance spike: ${t.earlyChange}% → Bought ${t.direction} @ ${(t.entry * 100).toFixed(0)}c`
    );
    console.log(
      `     Result: ${t.result} | Exit: ${t.exitType} | P&L: ${t.pnl >= 0 ? "+" : ""}$${t.pnl.toFixed(2)}`
    );
    console.log();
  });

  // Summary
  if (trades.length > 0) {
    const wins = trades.filter((t) => t.won).length;
    const totalPnL = trades.reduce((a, t) => a + t.pnl, 0);
    const totalSpent = trades.length * CONFIG.tradeSize;

    console.log("═══════════════════════════════════════════════════════════════");
    console.log("  RESULTS — Spike Detection Strategy");
    console.log("═══════════════════════════════════════════════════════════════\n");
    console.log("  Trades:    " + trades.length);
    console.log("  Wins:      " + wins + " (" + ((wins / trades.length) * 100).toFixed(1) + "%)");
    console.log("  Losses:    " + (trades.length - wins));
    console.log("  Total P&L: " + (totalPnL >= 0 ? "+" : "") + "$" + totalPnL.toFixed(2));
    console.log("  ROI:       " + (totalPnL >= 0 ? "+" : "") + ((totalPnL / totalSpent) * 100).toFixed(1) + "%");
    console.log("  Avg P&L:   " + (totalPnL >= 0 ? "+" : "") + "$" + (totalPnL / trades.length).toFixed(2) + " per trade");
  } else {
    console.log("  No spike trades detected in this window.");
    console.log("  Spikes are more common during US market hours (9am-4pm ET).");
  }

  // Now also test: what if we're MORE aggressive with spike threshold?
  console.log("\n═══════════════════════════════════════════════════════════════");
  console.log("  SENSITIVITY ANALYSIS — Different Spike Thresholds");
  console.log("═══════════════════════════════════════════════════════════════\n");

  const thresholds = [0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15];

  console.log(
    "  " +
      "Threshold".padEnd(12) +
      "Trades".padEnd(8) +
      "Win%".padEnd(8) +
      "P&L".padEnd(14) +
      "ROI".padEnd(10) +
      "Avg P&L"
  );
  console.log("  " + "─".repeat(62));

  for (const thresh of thresholds) {
    let w = 0, l = 0, pnl = 0;

    for (const pm of resolved) {
      const windowStart = pm.ts * 1000;
      const windowEnd = (pm.ts + 300) * 1000;
      const wc = prices.filter(
        (p) => p.time >= windowStart - 60000 && p.time <= windowEnd
      );
      if (wc.length < 1) continue;

      const chg = ((wc[0].close - wc[0].open) / wc[0].open) * 100;
      if (Math.abs(chg) < thresh) continue;

      const dir = chg > 0 ? "UP" : "DOWN";
      const won = dir === pm.result;

      if (won) {
        const shares = CONFIG.tradeSize / 0.51;
        const sellP = 0.51 * 1.10;
        const rev = shares * sellP;
        const profit = rev - CONFIG.tradeSize;
        pnl += profit - profit * CONFIG.fee;
        w++;
      } else {
        pnl -= CONFIG.tradeSize;
        l++;
      }
    }

    const total = w + l;
    if (total === 0) {
      console.log("  " + (thresh + "%").padEnd(12) + "0".padEnd(8) + "—");
      continue;
    }

    const wr = ((w / total) * 100).toFixed(1);
    const roi = ((pnl / (total * CONFIG.tradeSize)) * 100).toFixed(1);
    const avg = (pnl / total).toFixed(2);
    const icon = pnl > 0 ? " ✅" : " ❌";

    console.log(
      "  " +
        (thresh + "%").padEnd(12) +
        String(total).padEnd(8) +
        (wr + "%").padEnd(8) +
        ((pnl >= 0 ? "+" : "") + "$" + pnl.toFixed(2)).padEnd(14) +
        ((pnl >= 0 ? "+" : "") + roi + "%").padEnd(10) +
        ((pnl >= 0 ? "+" : "") + "$" + avg) +
        icon
    );
  }

  // Live monitor preview
  console.log("\n═══════════════════════════════════════════════════════════════");
  console.log("  LIVE MONITOR (showing current state)");
  console.log("═══════════════════════════════════════════════════════════════\n");

  const btcPrice = await getBTCPrice();
  const market = await getCurrentMarket();

  if (btcPrice) console.log("  BTC Price (Binance): $" + btcPrice.toFixed(2));
  if (market) {
    console.log("  Active market: " + market.question);
    console.log("  Resolves in: " + market.secsLeft.toFixed(0) + "s");
    console.log("  Up price: " + (market.upPrice ? (market.upPrice * 100).toFixed(1) + "c" : "?"));

    const upBook = await getBook(market.upToken);
    const downBook = await getBook(market.downToken);
    const ua = bestAsk(upBook);
    const ub = bestBid(upBook);
    const da = bestAsk(downBook);
    const db = bestBid(downBook);

    console.log(
      "  Book: UP " + (ub ? (ub*100).toFixed(0) : "—") + "/" + (ua ? (ua*100).toFixed(0) : "—") +
      "c  DOWN " + (db ? (db*100).toFixed(0) : "—") + "/" + (da ? (da*100).toFixed(0) : "—") + "c"
    );
  }

  console.log("\n  To run live monitoring, use: node scripts/binance-poly-arb.js --live");
  console.log("  (Best run from your Denmark VPN for full Binance access)\n");
}

async function runLiveMonitor() {
  console.log("╔═══════════════════════════════════════════════════════════════╗");
  console.log("║  LIVE MONITOR — Watching Binance for BTC Spikes            ║");
  console.log("║  Will signal when to buy on Polymarket                      ║");
  console.log("║  Press Ctrl+C to stop                                       ║");
  console.log("╚═══════════════════════════════════════════════════════════════╝\n");

  let lastPrice = null;
  let lastMarketSlug = null;
  let openTrades = [];
  let tradeLog = [];
  let tick = 0;

  const poll = async () => {
    tick++;
    const price = await getBTCPrice();
    if (!price) return;

    const market = await getCurrentMarket();

    if (lastPrice !== null) {
      const changePct = ((price - lastPrice) / lastPrice) * 100;
      const isSpike = Math.abs(changePct) >= CONFIG.spikeThreshold;
      const direction = changePct > 0 ? "UP" : "DOWN";

      // Status line every 5 ticks
      if (tick % 5 === 0) {
        const mktStr = market ? market.question.split(" - ")[1] + " (" + Math.round(market.secsLeft) + "s)" : "none";
        process.stdout.write(
          `  $${price.toFixed(0)} | ${changePct >= 0 ? "+" : ""}${changePct.toFixed(3)}% | Market: ${mktStr} | Open: ${openTrades.length}    \r`
        );
      }

      if (isSpike && market && openTrades.length < CONFIG.maxOpenTrades) {
        // SPIKE DETECTED — TRADE SIGNAL
        const tokenId = direction === "UP" ? market.upToken : market.downToken;
        const book = await getBook(tokenId);
        const ask = bestAsk(book);

        if (ask && ask < 0.60) {
          const shares = CONFIG.tradeSize / ask;
          const trade = {
            time: new Date().toISOString(),
            slug: market.slug,
            market: market.question,
            direction,
            entry: ask,
            shares,
            spent: CONFIG.tradeSize,
            targetSell: ask * (1 + CONFIG.takeProfitPct / 100),
            tokenId,
            spike: changePct.toFixed(3),
            btcPrice: price,
          };

          openTrades.push(trade);
          console.log(
            "\n  🚨 SPIKE " +
              changePct.toFixed(3) +
              "% → BUY " +
              direction +
              " @ " +
              (ask * 100).toFixed(1) +
              "c | " +
              shares.toFixed(1) +
              " shares | " +
              market.question
          );
          console.log(
            "     Target sell: " +
              (trade.targetSell * 100).toFixed(1) +
              "c (+10%)"
          );
        }
      }

      // Check open trades for exit
      for (let i = openTrades.length - 1; i >= 0; i--) {
        const t = openTrades[i];
        const book = await getBook(t.tokenId);
        const bid = bestBid(book);

        if (bid) {
          const currentValue = t.shares * bid;
          const pnlPct = ((currentValue - t.spent) / t.spent) * 100;

          if (pnlPct >= CONFIG.takeProfitPct) {
            const profit = currentValue - t.spent;
            const fee = profit * CONFIG.fee;
            const netPnl = profit - fee;

            console.log(
              "\n  💰 SELL " +
                t.direction +
                " @ " +
                (bid * 100).toFixed(1) +
                "c | P&L: +$" +
                netPnl.toFixed(2) +
                " (+" +
                pnlPct.toFixed(1) +
                "%) | " +
                t.market
            );

            tradeLog.push({ ...t, exit: bid, pnl: netPnl, exitType: "TAKE_PROFIT" });
            openTrades.splice(i, 1);
          } else if (pnlPct <= CONFIG.stopLossPct) {
            const netPnl = currentValue - t.spent;
            console.log(
              "\n  🛑 STOP LOSS " +
                t.direction +
                " @ " +
                (bid * 100).toFixed(1) +
                "c | P&L: $" +
                netPnl.toFixed(2) +
                " (" +
                pnlPct.toFixed(1) +
                "%) | " +
                t.market
            );
            tradeLog.push({ ...t, exit: bid, pnl: netPnl, exitType: "STOP_LOSS" });
            openTrades.splice(i, 1);
          }
        }
      }
    }

    lastPrice = price;
  };

  // Run poll loop
  while (true) {
    try {
      await poll();
    } catch {}
    await new Promise((r) => setTimeout(r, CONFIG.pollIntervalMs));
  }
}

// Main
const args = process.argv.slice(2);
if (args.includes("--live")) {
  runLiveMonitor().catch(console.error);
} else {
  runBacktest().catch(console.error);
}
