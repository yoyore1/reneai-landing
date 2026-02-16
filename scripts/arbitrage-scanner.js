#!/usr/bin/env node

/**
 * Polymarket BTC 5-Minute Market Arbitrage Scanner
 *
 * Scans live Polymarket "Up or Down" markets across:
 *   - BTC 5m, 15m, 4h
 *   - ETH, SOL, XRP 15m
 *
 * Identifies arbitrage via:
 *   1. YES+NO spread (risk-free if < $1)
 *   2. Cross-timeframe (5m vs 15m vs 4h consistency)
 *   3. Cross-asset correlation (BTC vs ETH vs SOL vs XRP)
 *   4. Statistical edge vs historical 50/50 baseline
 *   5. Momentum autocorrelation (54% same-dir in 24h data)
 *
 * Usage: node scripts/arbitrage-scanner.js
 */

const axios = require("axios");

const GAMMA_API = "https://gamma-api.polymarket.com";
const CLOB_API = "https://clob.polymarket.com";

async function fetchUpDownMarkets() {
  const res = await axios.get(`${GAMMA_API}/markets`, {
    params: {
      active: true,
      closed: false,
      order: "startDate",
      ascending: false,
      limit: 100,
    },
    timeout: 15000,
  });

  const all = res.data || [];
  return all.filter((m) => (m.slug || "").includes("updown"));
}

function groupMarkets(markets) {
  const groups = {};
  markets.forEach((m) => {
    const slug = m.slug || "";
    let asset, tf;
    if (slug.startsWith("btc-updown-5m")) { asset = "BTC"; tf = "5m"; }
    else if (slug.startsWith("btc-updown-15m")) { asset = "BTC"; tf = "15m"; }
    else if (slug.startsWith("btc-updown-4h")) { asset = "BTC"; tf = "4h"; }
    else if (slug.startsWith("eth-updown-15m")) { asset = "ETH"; tf = "15m"; }
    else if (slug.startsWith("eth-updown-4h")) { asset = "ETH"; tf = "4h"; }
    else if (slug.startsWith("sol-updown-15m")) { asset = "SOL"; tf = "15m"; }
    else if (slug.startsWith("sol-updown-4h")) { asset = "SOL"; tf = "4h"; }
    else if (slug.startsWith("xrp-updown-15m")) { asset = "XRP"; tf = "15m"; }
    else if (slug.startsWith("xrp-updown-4h")) { asset = "XRP"; tf = "4h"; }
    else return;

    const key = `${asset}-${tf}`;
    if (!groups[key]) groups[key] = [];
    groups[key].push({
      ...m,
      _asset: asset,
      _tf: tf,
      _upPrice: m.outcomePrices ? parseFloat(JSON.parse(m.outcomePrices)[0]) : null,
      _downPrice: m.outcomePrices ? parseFloat(JSON.parse(m.outcomePrices)[1]) : null,
    });
  });

  Object.values(groups).forEach((arr) =>
    arr.sort((a, b) => new Date(a.endDate) - new Date(b.endDate))
  );

  return groups;
}

async function fetchOrderBook(tokenId) {
  try {
    const res = await axios.get(`${CLOB_API}/book`, {
      params: { token_id: tokenId },
      timeout: 10000,
    });
    return res.data;
  } catch {
    return null;
  }
}

async function getBTCStats() {
  try {
    const res = await axios.get(
      "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
      { params: { vs_currency: "usd", days: 1 }, timeout: 15000 }
    );

    const prices = res.data.prices;
    const candles = [];
    for (let i = 0; i < prices.length - 1; i++) {
      const interval = (prices[i + 1][0] - prices[i][0]) / 60000;
      if (interval > 3 && interval < 10) {
        candles.push({
          open: prices[i][1],
          close: prices[i + 1][1],
          direction: prices[i + 1][1] > prices[i][1] ? "UP" : "DOWN",
          change: ((prices[i + 1][1] - prices[i][1]) / prices[i][1]) * 100,
        });
      }
    }

    let up = 0, down = 0, sameDir = 0, diffDir = 0;
    candles.forEach((c) => (c.direction === "UP" ? up++ : down++));
    for (let i = 1; i < candles.length; i++) {
      if (candles[i].direction === candles[i - 1].direction) sameDir++;
      else diffDir++;
    }

    const changes = candles.map((c) => c.change);
    const avg = changes.reduce((a, b) => a + b, 0) / changes.length;
    const stdDev = Math.sqrt(
      changes.reduce((a, b) => a + (b - avg) ** 2, 0) / changes.length
    );

    return {
      currentPrice: prices[prices.length - 1][1],
      totalCandles: candles.length,
      upPct: ((up / (up + down)) * 100).toFixed(1),
      downPct: ((down / (up + down)) * 100).toFixed(1),
      momentumPct: ((sameDir / (sameDir + diffDir)) * 100).toFixed(1),
      reversionPct: ((diffDir / (sameDir + diffDir)) * 100).toFixed(1),
      avgChange: avg.toFixed(4),
      stdDev: stdDev.toFixed(4),
      lastDirection: candles.length > 0 ? candles[candles.length - 1].direction : null,
      last3: candles.slice(-3).map((c) => c.direction),
    };
  } catch {
    return null;
  }
}

function analyzeSpreadArbitrage(groups) {
  const opps = [];
  for (const [key, markets] of Object.entries(groups)) {
    for (const m of markets) {
      if (m._upPrice !== null && m._downPrice !== null) {
        const sum = m._upPrice + m._downPrice;
        const spread = Math.abs(1.0 - sum);
        if (spread > 0.005) {
          opps.push({
            type: sum < 1.0 ? "GUARANTEED_PROFIT" : "OVERPRICED",
            market: m.question,
            group: key,
            upPrice: m._upPrice,
            downPrice: m._downPrice,
            sum,
            spread,
            profit: sum < 1.0 ? ((1 - sum) * 100).toFixed(2) + "%" : null,
          });
        }
      }
    }
  }
  return opps.sort((a, b) => b.spread - a.spread);
}

function analyzeCrossTimeframe(groups) {
  const opps = [];
  const btc5m = groups["BTC-5m"] || [];
  const btc15m = groups["BTC-15m"] || [];
  const btc4h = groups["BTC-4h"] || [];

  for (const m15 of btc15m) {
    const endTime15 = new Date(m15.endDate).getTime();
    const startTime15 = endTime15 - 15 * 60 * 1000;

    const matching5m = btc5m.filter((m) => {
      const end = new Date(m.endDate).getTime();
      return end > startTime15 && end <= endTime15;
    });

    if (matching5m.length > 0 && m15._upPrice !== null) {
      const fiveMinPrices = matching5m.map((m) => ({
        q: m.question,
        up: m._upPrice,
        down: m._downPrice,
      }));

      const avg5mUp =
        fiveMinPrices.reduce((a, b) => a + (b.up || 0.5), 0) / fiveMinPrices.length;

      if (Math.abs(avg5mUp - m15._upPrice) > 0.03) {
        opps.push({
          type: "TIMEFRAME_MISMATCH",
          market15m: m15.question,
          price15mUp: m15._upPrice,
          avg5mUp,
          diff: Math.abs(avg5mUp - m15._upPrice),
          matching5m: fiveMinPrices,
          strategy:
            avg5mUp > m15._upPrice
              ? `5-min markets imply ${(avg5mUp * 100).toFixed(1)}% UP but 15-min only ${(m15._upPrice * 100).toFixed(1)}%. Consider buying UP on 15-min.`
              : `5-min markets imply ${(avg5mUp * 100).toFixed(1)}% UP but 15-min is ${(m15._upPrice * 100).toFixed(1)}%. Consider buying DOWN on 15-min.`,
        });
      }
    }
  }

  if (btc4h.length > 0 && btc15m.length > 0) {
    const fourH = btc4h[0];
    const avg15mUp =
      btc15m.reduce((a, b) => a + (b._upPrice || 0.5), 0) / btc15m.length;

    if (
      fourH._upPrice !== null &&
      Math.abs(avg15mUp - fourH._upPrice) > 0.03
    ) {
      opps.push({
        type: "MACRO_MISMATCH",
        market4h: fourH.question,
        price4hUp: fourH._upPrice,
        avg15mUp,
        diff: Math.abs(avg15mUp - fourH._upPrice),
        strategy:
          avg15mUp > fourH._upPrice
            ? "15-min markets are more bullish than 4h. Consider buying UP on 4h."
            : "15-min markets are more bearish than 4h. Consider buying DOWN on 4h.",
      });
    }
  }

  return opps;
}

function analyzeCrossAsset(groups) {
  const opps = [];
  const assets = ["BTC", "ETH", "SOL", "XRP"];

  const prices15m = {};
  for (const asset of assets) {
    const key = `${asset}-15m`;
    const markets = groups[key] || [];
    if (markets.length > 0) {
      prices15m[asset] = markets.map((m) => ({
        q: m.question,
        up: m._upPrice,
        endDate: m.endDate,
      }));
    }
  }

  if (Object.keys(prices15m).length >= 2) {
    const assetKeys = Object.keys(prices15m);
    for (let i = 0; i < assetKeys.length; i++) {
      for (let j = i + 1; j < assetKeys.length; j++) {
        const a1 = assetKeys[i];
        const a2 = assetKeys[j];

        for (const m1 of prices15m[a1]) {
          for (const m2 of prices15m[a2]) {
            const timeDiff = Math.abs(
              new Date(m1.endDate).getTime() - new Date(m2.endDate).getTime()
            );
            if (timeDiff < 2 * 60 * 1000 && m1.up !== null && m2.up !== null) {
              const diff = Math.abs(m1.up - m2.up);
              if (diff > 0.04) {
                opps.push({
                  type: "CROSS_ASSET_DIVERGENCE",
                  asset1: a1,
                  asset2: a2,
                  market1: m1.q,
                  market2: m2.q,
                  price1Up: m1.up,
                  price2Up: m2.up,
                  diff,
                  strategy:
                    m1.up > m2.up
                      ? `${a1} priced more bullish (${(m1.up * 100).toFixed(1)}c) than ${a2} (${(m2.up * 100).toFixed(1)}c). Assets are highly correlated — consider buying ${a2} UP or ${a1} DOWN to converge.`
                      : `${a2} priced more bullish (${(m2.up * 100).toFixed(1)}c) than ${a1} (${(m1.up * 100).toFixed(1)}c). Assets are highly correlated — consider buying ${a1} UP or ${a2} DOWN to converge.`,
                });
              }
            }
          }
        }
      }
    }
  }

  return opps;
}

function analyzeStatisticalEdge(groups, btcStats) {
  const opps = [];
  if (!btcStats) return opps;

  for (const [key, markets] of Object.entries(groups)) {
    for (const m of markets) {
      if (m._upPrice === null) continue;

      const fairUp = parseFloat(btcStats.upPct) / 100;
      const deviation = m._upPrice - fairUp;

      if (Math.abs(deviation) > 0.06) {
        opps.push({
          type: "STATISTICAL_MISPRICING",
          market: m.question,
          group: key,
          currentUpPrice: m._upPrice,
          fairValue: fairUp,
          deviation: Math.abs(deviation),
          strategy:
            deviation > 0
              ? `Market prices UP at ${(m._upPrice * 100).toFixed(1)}c but historical fair value is ${(fairUp * 100).toFixed(1)}c. BUY DOWN at ${(m._downPrice * 100).toFixed(1)}c for ~${(Math.abs(deviation) * 100).toFixed(1)}c edge.`
              : `Market prices UP at ${(m._upPrice * 100).toFixed(1)}c but historical fair value is ${(fairUp * 100).toFixed(1)}c. BUY UP at ${(m._upPrice * 100).toFixed(1)}c for ~${(Math.abs(deviation) * 100).toFixed(1)}c edge.`,
        });
      }
    }
  }

  return opps.sort((a, b) => b.deviation - a.deviation);
}

function analyzeMomentum(groups, btcStats) {
  const opps = [];
  if (!btcStats || !btcStats.lastDirection) return opps;

  const btc5m = groups["BTC-5m"] || [];
  const momentum = parseFloat(btcStats.momentumPct);

  if (momentum > 52 && btc5m.length > 0) {
    const nextMarket = btc5m[0];
    if (nextMarket._upPrice !== null) {
      const predictedDir = btcStats.lastDirection;
      const edge = (momentum - 50) / 100;
      const last3Str = btcStats.last3.join(" → ");

      opps.push({
        type: "MOMENTUM_EDGE",
        market: nextMarket.question,
        lastDirection: btcStats.lastDirection,
        last3: last3Str,
        momentumPct: momentum,
        edge: (edge * 100).toFixed(1),
        strategy:
          predictedDir === "UP"
            ? `BTC moved ${last3Str} recently. ${momentum}% autocorrelation suggests next move is UP. Buy UP at ${(nextMarket._upPrice * 100).toFixed(1)}c for ~${(edge * 100).toFixed(1)}c expected edge.`
            : `BTC moved ${last3Str} recently. ${momentum}% autocorrelation suggests next move is DOWN. Buy DOWN at ${(nextMarket._downPrice * 100).toFixed(1)}c for ~${(edge * 100).toFixed(1)}c expected edge.`,
      });
    }
  }

  return opps;
}

async function main() {
  console.log("╔══════════════════════════════════════════════════════════════╗");
  console.log("║  Polymarket BTC 5-Min Up/Down — Arbitrage Scanner          ║");
  console.log("╚══════════════════════════════════════════════════════════════╝\n");

  const [markets, btcStats] = await Promise.all([
    fetchUpDownMarkets(),
    getBTCStats(),
  ]);

  const groups = groupMarkets(markets);

  if (btcStats) {
    console.log("📊 BTC MARKET CONTEXT (last 24h, ~5-min candles)");
    console.log("─────────────────────────────────────────────────");
    console.log(`  Price:        $${btcStats.currentPrice.toLocaleString(undefined, { maximumFractionDigits: 0 })}`);
    console.log(`  Up candles:   ${btcStats.upPct}%`);
    console.log(`  Down candles: ${btcStats.downPct}%`);
    console.log(`  Momentum:     ${btcStats.momentumPct}% (same dir continues)`);
    console.log(`  Mean rev:     ${btcStats.reversionPct}% (direction flips)`);
    console.log(`  Avg change:   ${btcStats.avgChange}%`);
    console.log(`  Volatility:   ${btcStats.stdDev}% per 5-min`);
    console.log(`  Last move:    ${btcStats.lastDirection}`);
    console.log(`  Last 3:       ${btcStats.last3.join(" → ")}\n`);
  }

  console.log("📋 ACTIVE UP/DOWN MARKETS");
  console.log("─────────────────────────");
  for (const [key, mks] of Object.entries(groups)) {
    console.log(`\n  ${key} (${mks.length} markets):`);
    mks.forEach((m) => {
      const up = m._upPrice !== null ? (m._upPrice * 100).toFixed(1) : "?";
      const down = m._downPrice !== null ? (m._downPrice * 100).toFixed(1) : "?";
      const sum = m._upPrice !== null && m._downPrice !== null
        ? ((m._upPrice + m._downPrice) * 100).toFixed(1)
        : "?";
      const vol = parseFloat(m.volume || 0).toFixed(0);
      console.log(`    ${m.question}`);
      console.log(`      Up: ${up}c | Down: ${down}c | Sum: ${sum}c | Vol: $${vol}`);
    });
  }

  console.log("\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
  console.log("  ARBITRAGE ANALYSIS");
  console.log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n");

  const spreadOpps = analyzeSpreadArbitrage(groups);
  console.log(`1. SPREAD ARBITRAGE (Up + Down ≠ $1.00): ${spreadOpps.length} found`);
  spreadOpps.forEach((o, i) => {
    console.log(`   [${i + 1}] ${o.market}`);
    console.log(`       Up: ${(o.upPrice * 100).toFixed(1)}c + Down: ${(o.downPrice * 100).toFixed(1)}c = ${(o.sum * 100).toFixed(1)}c`);
    console.log(`       ${o.type === "GUARANTEED_PROFIT" ? "✅ " + o.profit + " guaranteed profit" : "⚠️  Overpriced"}`);
  });

  const crossTF = analyzeCrossTimeframe(groups);
  console.log(`\n2. CROSS-TIMEFRAME (5m vs 15m vs 4h): ${crossTF.length} found`);
  crossTF.forEach((o, i) => {
    console.log(`   [${i + 1}] ${o.type}`);
    console.log(`       ${o.strategy}`);
  });

  const crossAsset = analyzeCrossAsset(groups);
  console.log(`\n3. CROSS-ASSET DIVERGENCE: ${crossAsset.length} found`);
  crossAsset.forEach((o, i) => {
    console.log(`   [${i + 1}] ${o.asset1} vs ${o.asset2}`);
    console.log(`       ${o.strategy}`);
  });

  const statEdge = analyzeStatisticalEdge(groups, btcStats);
  console.log(`\n4. STATISTICAL MISPRICING (vs fair value): ${statEdge.length} found`);
  statEdge.forEach((o, i) => {
    console.log(`   [${i + 1}] ${o.market}`);
    console.log(`       ${o.strategy}`);
  });

  const momEdge = analyzeMomentum(groups, btcStats);
  console.log(`\n5. MOMENTUM EDGE: ${momEdge.length} found`);
  momEdge.forEach((o, i) => {
    console.log(`   [${i + 1}] ${o.market}`);
    console.log(`       ${o.strategy}`);
  });

  const totalOpps = spreadOpps.length + crossTF.length + crossAsset.length + statEdge.length + momEdge.length;

  if (totalOpps === 0) {
    console.log("\n  No live arbitrage detected at this moment.");
    console.log("  Markets are currently priced near 50/50 with tight sums.\n");
    console.log("  💡 Best times to find arbitrage:");
    console.log("     • Right when a new 5-min window opens (books are thin)");
    console.log("     • During high volatility events (Fed, CPI, whale moves)");
    console.log("     • When BTC makes a sharp move and markets lag behind");
  }

  console.log("\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
  console.log("  STRATEGY PLAYBOOK");
  console.log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n");

  console.log("  🎯 STRATEGY 1: SPREAD SNIPE (Risk-Free)");
  console.log("  ─────────────────────────────────────────");
  console.log("  If Up + Down < $1.00 → buy BOTH → guaranteed profit");
  console.log("  Monitor: new market creation (first 30 seconds)\n");

  console.log("  🎯 STRATEGY 2: CROSS-TIMEFRAME ARB");
  console.log("  ────────────────────────────────────");
  console.log("  15-min outcome = net of three 5-min windows.");
  console.log("  After 1st or 2nd 5-min resolves, the 15-min is partly decided.");
  console.log("  If BTC is already up +0.3% after 10 min, the 15-min 'Up' is");
  console.log("  very likely — but may still be priced at 50c. Buy it.\n");

  console.log("  🎯 STRATEGY 3: CROSS-ASSET CORRELATION");
  console.log("  ───────────────────────────────────────");
  console.log("  BTC, ETH, SOL, XRP are 80-95% correlated on 15-min windows.");
  console.log("  If BTC 15m 'Up' moves to 60c but ETH 15m 'Up' is still 50c,");
  console.log("  buy ETH 'Up' — it will likely follow BTC.\n");

  console.log("  🎯 STRATEGY 4: MOMENTUM AUTOCORRELATION");
  console.log("  ────────────────────────────────────────");
  if (btcStats) {
    console.log(`  24h data shows ${btcStats.momentumPct}% autocorrelation.`);
    console.log(`  Last move: ${btcStats.lastDirection}. Recent: ${btcStats.last3.join(" → ")}`);
  }
  console.log("  After an UP candle, bet UP on next. After DOWN, bet DOWN.");
  console.log("  ~4% edge per trade at 50c pricing.\n");

  console.log("  🎯 STRATEGY 5: VOLATILITY EVENT FRONT-RUN");
  console.log("  ──────────────────────────────────────────");
  console.log("  Before known events (Fed, CPI, options expiry):");
  console.log("  Place limit orders at 40-45c on both Up and Down.");
  console.log("  The sharp move will push one side to 90c+, giving you 2x.\n");

  console.log("  🎯 STRATEGY 6: LIQUIDITY SNIPE AT MARKET OPEN");
  console.log("  ──────────────────────────────────────────────");
  console.log("  New 5-min markets open with 1c/99c spreads.");
  console.log("  Place limit orders at 45-48c for your predicted direction.");
  console.log("  As books tighten to 50c, you're already in at a discount.\n");

  console.log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n");
}

main().catch(console.error);
