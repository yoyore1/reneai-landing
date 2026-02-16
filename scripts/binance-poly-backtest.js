#!/usr/bin/env node

/**
 * BINANCE → POLYMARKET Latency Arb — Proper Backtest
 *
 * Matches Binance 1-min candle data to the exact time windows
 * of resolved Polymarket BTC 5-min markets.
 */

const axios = require("axios");

const GAMMA = "https://gamma-api.polymarket.com";
const BINANCE = "https://data-api.binance.vision/api/v3/klines";
const FEE = 0.02;
const TRADE_SIZE = 100;

async function getMarket(slug) {
  try {
    const res = await axios.get(`${GAMMA}/markets`, { params: { slug }, timeout: 4000 });
    return (res.data || [])[0] || null;
  } catch { return null; }
}

async function getBinanceCandles(startMs, endMs) {
  try {
    const res = await axios.get(BINANCE, {
      params: {
        symbol: "BTCUSDT",
        interval: "1m",
        startTime: startMs,
        endTime: endMs,
        limit: 1000,
      },
      timeout: 10000,
    });
    return res.data.map((c) => ({
      time: c[0],
      open: parseFloat(c[1]),
      high: parseFloat(c[2]),
      low: parseFloat(c[3]),
      close: parseFloat(c[4]),
      volume: parseFloat(c[5]),
    }));
  } catch (e) {
    return [];
  }
}

async function main() {
  console.log("╔═══════════════════════════════════════════════════════════════╗");
  console.log("║  BINANCE → POLYMARKET Latency Arb Backtest                 ║");
  console.log("║  Match Binance 1-min spikes to Polymarket 5-min outcomes   ║");
  console.log("╚═══════════════════════════════════════════════════════════════╝\n");

  // Step 1: Load resolved Polymarket markets
  console.log("Loading resolved Polymarket markets...");
  const refTs = 1771224300; // known recent resolved timestamp
  const markets = [];

  for (let ts = refTs; ts > refTs - 30 * 3600 && markets.length < 100; ts -= 300) {
    const m = await getMarket("btc-updown-5m-" + ts);
    if (m) {
      const p = m.outcomePrices ? JSON.parse(m.outcomePrices) : [];
      const up = p[0] ? parseFloat(p[0]) : null;
      if (up !== null && (up > 0.9 || up < 0.1)) {
        markets.push({
          ts,
          result: up > 0.9 ? "UP" : "DOWN",
          vol: parseFloat(m.volume || 0),
          q: m.question,
        });
      }
    }
    if (markets.length % 25 === 0 && markets.length > 0) {
      process.stdout.write("  " + markets.length + " markets...\r");
    }
  }

  markets.sort((a, b) => a.ts - b.ts);
  console.log("  Loaded " + markets.length + " resolved markets\n");

  if (markets.length === 0) {
    console.log("No resolved markets found.");
    return;
  }

  // Step 2: For each market, fetch matching Binance 1-min candles
  console.log("Fetching Binance price data for each market window...\n");

  // Batch fetch Binance data for the full time range
  const firstTs = markets[0].ts;
  const lastTs = markets[markets.length - 1].ts;
  const allCandles = await getBinanceCandles(
    (firstTs - 120) * 1000,
    (lastTs + 600) * 1000
  );
  console.log("  Got " + allCandles.length + " Binance 1-min candles\n");

  if (allCandles.length === 0) {
    console.log("  No Binance data available for this period.");
    return;
  }

  // Step 3: For each Polymarket market, detect Binance spike in first 1-2 min
  console.log("═══════════════════════════════════════════════════════════════");
  console.log("  SPIKE DETECTION — Binance moves during each 5-min window");
  console.log("═══════════════════════════════════════════════════════════════\n");

  const thresholds = [0.01, 0.02, 0.03, 0.05, 0.08, 0.10];

  for (const thresh of thresholds) {
    const trades = [];

    for (const pm of markets) {
      const windowStartMs = pm.ts * 1000;
      const windowEndMs = (pm.ts + 300) * 1000;

      // Get Binance candles in the FIRST 1 MINUTE of the 5-min window
      // This is our "early signal" — Binance moves first
      const earlyCandles = allCandles.filter(
        (c) => c.time >= windowStartMs && c.time < windowStartMs + 60000
      );

      if (earlyCandles.length === 0) continue;

      // Price change in first minute
      const firstOpen = earlyCandles[0].open;
      const firstClose = earlyCandles[earlyCandles.length - 1].close;
      const changePct = ((firstClose - firstOpen) / firstOpen) * 100;

      if (Math.abs(changePct) < thresh) continue;

      // Spike detected! We'd buy on Polymarket immediately
      const direction = changePct > 0 ? "UP" : "DOWN";

      // Entry: buy at the ask (51c) — book hasn't repriced yet
      const entry = 0.51;
      const shares = TRADE_SIZE / entry;
      const won = direction === pm.result;

      // If won: sell at +10% (56.1c) or hold to resolution ($1)
      // Strategy A: Take profit at +10%
      // Strategy B: Hold to resolution
      let pnlTakeProfit, pnlHoldToRes;

      if (won) {
        // Take profit at +10%
        const sellPrice = entry * 1.10;
        const rev = shares * sellPrice;
        const profit = rev - TRADE_SIZE;
        pnlTakeProfit = profit - profit * FEE;

        // Hold to resolution
        const payoutRes = shares * 1.0;
        const profitRes = payoutRes - TRADE_SIZE;
        pnlHoldToRes = profitRes - profitRes * FEE;
      } else {
        // Take profit: assume we sell at -20% stop loss or hold to $0
        pnlTakeProfit = -TRADE_SIZE * 0.20; // -20% stop
        pnlHoldToRes = -TRADE_SIZE; // hold to $0
      }

      trades.push({
        q: pm.q,
        result: pm.result,
        direction,
        spike: changePct,
        won,
        pnlTP: pnlTakeProfit,
        pnlHold: pnlHoldToRes,
      });
    }

    if (trades.length === 0) continue;

    const wins = trades.filter((t) => t.won).length;
    const tpTotal = trades.reduce((a, t) => a + t.pnlTP, 0);
    const holdTotal = trades.reduce((a, t) => a + t.pnlHold, 0);
    const wr = ((wins / trades.length) * 100).toFixed(1);
    const tpRoi = ((tpTotal / (trades.length * TRADE_SIZE)) * 100).toFixed(1);
    const holdRoi = ((holdTotal / (trades.length * TRADE_SIZE)) * 100).toFixed(1);

    console.log(
      "  Spike > " + (thresh * 100).toFixed(0) + " bps (" + thresh.toFixed(2) + "%)"
    );
    console.log("  Trades: " + trades.length + " | Wins: " + wins + " | Win Rate: " + wr + "%");
    console.log(
      "  Take Profit (+10% / -20% stop): P&L " +
        (tpTotal >= 0 ? "+" : "") + "$" + tpTotal.toFixed(2) +
        " | ROI " + tpRoi + "%"
    );
    console.log(
      "  Hold to Resolution:             P&L " +
        (holdTotal >= 0 ? "+" : "") + "$" + holdTotal.toFixed(2) +
        " | ROI " + holdRoi + "%"
    );

    // Show individual trades for best threshold
    if (trades.length <= 20) {
      trades.forEach((t) => {
        const icon = t.won ? "  ✅" : "  ❌";
        console.log(
          icon +
            " Spike " +
            (t.spike >= 0 ? "+" : "") + t.spike.toFixed(3) +
            "% → " + t.direction +
            " | Result: " + t.result +
            " | TP P&L: " + (t.pnlTP >= 0 ? "+" : "") + "$" + t.pnlTP.toFixed(2) +
            " | Hold P&L: " + (t.pnlHold >= 0 ? "+" : "") + "$" + t.pnlHold.toFixed(2)
        );
      });
    }
    console.log();
  }

  // Step 4: Summary table
  console.log("═══════════════════════════════════════════════════════════════");
  console.log("  SUMMARY — All Thresholds Compared");
  console.log("═══════════════════════════════════════════════════════════════\n");

  console.log("  " +
    "Spike Thresh".padEnd(14) +
    "Trades".padEnd(8) +
    "Win%".padEnd(8) +
    "TP P&L".padEnd(14) +
    "TP ROI".padEnd(10) +
    "Hold P&L".padEnd(14) +
    "Hold ROI"
  );
  console.log("  " + "─".repeat(76));

  for (const thresh of [0.005, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.15, 0.20]) {
    let w = 0, l = 0, tpPnl = 0, holdPnl = 0;

    for (const pm of markets) {
      const wStart = pm.ts * 1000;
      const early = allCandles.filter(
        (c) => c.time >= wStart && c.time < wStart + 60000
      );
      if (early.length === 0) continue;

      const chg = ((early[early.length - 1].close - early[0].open) / early[0].open) * 100;
      if (Math.abs(chg) < thresh) continue;

      const dir = chg > 0 ? "UP" : "DOWN";
      const won = dir === pm.result;
      const shares = TRADE_SIZE / 0.51;

      if (won) {
        w++;
        tpPnl += (shares * 0.51 * 1.10 - TRADE_SIZE) * (1 - FEE);
        holdPnl += (shares * 1.0 - TRADE_SIZE) * (1 - FEE);
      } else {
        l++;
        tpPnl -= TRADE_SIZE * 0.20;
        holdPnl -= TRADE_SIZE;
      }
    }

    const total = w + l;
    if (total === 0) continue;

    const wr = ((w / total) * 100).toFixed(1);
    const tpRoi = ((tpPnl / (total * TRADE_SIZE)) * 100).toFixed(1);
    const hRoi = ((holdPnl / (total * TRADE_SIZE)) * 100).toFixed(1);
    const tpIcon = parseFloat(tpRoi) > 0 ? " ✅" : " ❌";
    const hIcon = parseFloat(hRoi) > 0 ? " ✅" : " ❌";

    console.log("  " +
      (">"+thresh.toFixed(3)+"%").padEnd(14) +
      String(total).padEnd(8) +
      (wr + "%").padEnd(8) +
      ((tpPnl >= 0 ? "+" : "") + "$" + tpPnl.toFixed(0)).padEnd(14) +
      (tpRoi + "%").padEnd(10) +
      ((holdPnl >= 0 ? "+" : "") + "$" + holdPnl.toFixed(0)).padEnd(14) +
      hRoi + "%" + hIcon
    );
  }

  console.log("\n  TP = Take Profit at +10% gain, stop loss at -20%");
  console.log("  Hold = Hold to market resolution ($1 win / $0 loss)");
  console.log("  Entry at 51c (real ask price from order book)\n");
}

main().catch(console.error);
