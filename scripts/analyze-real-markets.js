#!/usr/bin/env node

/**
 * Fetches REAL Polymarket "BTC Up or Down - 5 min" market data
 * by querying every 5-minute slug over the past 48 hours,
 * then analyzes resolved outcomes, volumes, pricing, and spreads.
 */

const axios = require("axios");

const GAMMA = "https://gamma-api.polymarket.com";

async function fetchMarketBySlug(slug) {
  try {
    const res = await axios.get(`${GAMMA}/markets`, {
      params: { slug },
      timeout: 4000,
    });
    return (res.data || [])[0] || null;
  } catch {
    return null;
  }
}

async function main() {
  console.log("==========================================================");
  console.log("  REAL Polymarket BTC 5-Min Market Analysis");
  console.log("  Scanning every 5-min slug over the past 48 hours...");
  console.log("==========================================================\n");

  // We know active slugs around ts=1771305900 (Feb 17 05:25 UTC)
  // Go backwards 48 hours
  const refTs = 1771308300; // latest known
  const goBack = 48 * 3600;
  const step = 300; // 5 min

  const all = [];
  let batchCount = 0;

  for (let ts = refTs; ts > refTs - goBack; ts -= step) {
    const slug = "btc-updown-5m-" + ts;
    const m = await fetchMarketBySlug(slug);

    if (m) {
      const prices = m.outcomePrices ? JSON.parse(m.outcomePrices) : [];
      const up = prices[0] ? parseFloat(prices[0]) : null;
      const down = prices[1] ? parseFloat(prices[1]) : null;

      all.push({
        ts,
        time: new Date(ts * 1000).toISOString(),
        q: m.question,
        slug: m.slug,
        up,
        down,
        sum: up !== null && down !== null ? up + down : null,
        vol: parseFloat(m.volume || 0),
        liq: parseFloat(m.liquidity || 0),
        active: m.active,
        closed: m.closed,
        resolved: up !== null && (up > 0.9 || up < 0.1),
        result: up !== null ? (up > 0.9 ? "UP" : up < 0.1 ? "DOWN" : null) : null,
      });
    }

    batchCount++;
    if (batchCount % 30 === 0) {
      const hrsBack = ((refTs - ts) / 3600).toFixed(1);
      process.stdout.write(
        `  Scanned ${hrsBack}h back — ${all.length} markets found (${all.filter((x) => x.resolved).length} resolved)\r`
      );
      await new Promise((r) => setTimeout(r, 100));
    }
  }

  console.log(
    `\n  Done. Total: ${all.length} markets found.\n`
  );

  // ── Categorize ──────────────────────────────────────────

  const resolved = all.filter((m) => m.resolved);
  const unresolved = all.filter((m) => !m.resolved);
  const withVol = all.filter((m) => m.vol > 0);

  console.log("──────────────────────────────────────────────────────────");
  console.log("  OVERVIEW");
  console.log("──────────────────────────────────────────────────────────");
  console.log(`  Total markets found:   ${all.length}`);
  console.log(`  Resolved:              ${resolved.length}`);
  console.log(`  Unresolved (open):     ${unresolved.length}`);
  console.log(`  With volume > $0:      ${withVol.length}`);
  console.log();

  // ── Resolved markets ───────────────────────────────────

  if (resolved.length > 0) {
    const upWins = resolved.filter((m) => m.result === "UP");
    const downWins = resolved.filter((m) => m.result === "DOWN");

    console.log("──────────────────────────────────────────────────────────");
    console.log("  RESOLVED MARKETS");
    console.log("──────────────────────────────────────────────────────────");
    console.log(`  UP wins:   ${upWins.length} (${((upWins.length / resolved.length) * 100).toFixed(1)}%)`);
    console.log(`  DOWN wins: ${downWins.length} (${((downWins.length / resolved.length) * 100).toFixed(1)}%)`);
    console.log();

    resolved.forEach((m, i) => {
      console.log(
        `  ${String(i + 1).padStart(3)}. ${m.q}`
      );
      console.log(
        `       => ${m.result}  |  vol: $${m.vol.toFixed(0)}  |  liq: $${m.liq.toFixed(0)}`
      );
    });

    // Autocorrelation on resolved
    if (resolved.length > 1) {
      let sameDir = 0;
      for (let i = 1; i < resolved.length; i++) {
        if (resolved[i].result === resolved[i - 1].result) sameDir++;
      }
      const acPct = ((sameDir / (resolved.length - 1)) * 100).toFixed(1);
      console.log(`\n  Autocorrelation: ${acPct}% (same direction continues)`);
    }

    // Streak analysis
    let streak = 1;
    let maxStreak = 1;
    let maxStreakDir = resolved[0]?.result;
    for (let i = 1; i < resolved.length; i++) {
      if (resolved[i].result === resolved[i - 1].result) {
        streak++;
        if (streak > maxStreak) {
          maxStreak = streak;
          maxStreakDir = resolved[i].result;
        }
      } else {
        streak = 1;
      }
    }
    console.log(`  Max streak: ${maxStreak} consecutive ${maxStreakDir}\n`);
  }

  // ── Volume analysis ────────────────────────────────────

  if (withVol.length > 0) {
    console.log("──────────────────────────────────────────────────────────");
    console.log("  MARKETS WITH TRADING VOLUME");
    console.log("──────────────────────────────────────────────────────────");

    withVol.sort((a, b) => b.vol - a.vol);
    const totalVol = withVol.reduce((a, m) => a + m.vol, 0);
    const avgVol = totalVol / withVol.length;

    console.log(`  Markets with volume:  ${withVol.length}`);
    console.log(`  Total volume:         $${totalVol.toFixed(2)}`);
    console.log(`  Avg volume/market:    $${avgVol.toFixed(2)}`);
    console.log(`  Max volume:           $${withVol[0].vol.toFixed(2)}`);
    console.log();

    withVol.forEach((m, i) => {
      const priceStr = m.resolved
        ? m.result
        : `Up:${(m.up * 100).toFixed(1)}c`;
      console.log(
        `  ${String(i + 1).padStart(3)}. ${m.q}`
      );
      console.log(
        `       ${priceStr.padEnd(12)}  vol: $${m.vol.toFixed(0).padEnd(6)}  liq: $${m.liq.toFixed(0)}`
      );
    });
    console.log();
  }

  // ── Spread/pricing analysis ────────────────────────────

  console.log("──────────────────────────────────────────────────────────");
  console.log("  PRICING & SPREAD ANALYSIS");
  console.log("──────────────────────────────────────────────────────────");

  const priced = all.filter((m) => m.up !== null && !m.resolved);
  const mispriced = priced.filter(
    (m) => m.sum !== null && Math.abs(1.0 - m.sum) > 0.003
  );

  console.log(`  Total priced (unresolved): ${priced.length}`);
  console.log(
    `  Mispriced (Up+Down != $1.00, >0.3c): ${mispriced.length}`
  );

  if (mispriced.length > 0) {
    mispriced.sort((a, b) => Math.abs(1 - a.sum) - Math.abs(1 - b.sum)).reverse();
    console.log();
    mispriced.slice(0, 20).forEach((m) => {
      const sumC = (m.sum * 100).toFixed(1);
      const spreadC = (Math.abs(1 - m.sum) * 100).toFixed(1);
      const type = m.sum < 1 ? "PROFIT OPP" : "OVERPRICED";
      console.log(
        `  ${type}: ${m.q}`
      );
      console.log(
        `    Up: ${(m.up * 100).toFixed(1)}c  Down: ${(m.down * 100).toFixed(1)}c  Sum: ${sumC}c  Spread: ${spreadC}c`
      );
    });
  }

  // Up price distribution
  const upPrices = priced.map((m) => m.up);
  if (upPrices.length > 0) {
    const avg = upPrices.reduce((a, b) => a + b, 0) / upPrices.length;
    const deviated = upPrices.filter((p) => Math.abs(p - 0.5) > 0.02);
    const buckets = { "< 47c": 0, "47-49c": 0, "49-51c": 0, "51-53c": 0, "> 53c": 0 };
    upPrices.forEach((p) => {
      const c = p * 100;
      if (c < 47) buckets["< 47c"]++;
      else if (c < 49) buckets["47-49c"]++;
      else if (c <= 51) buckets["49-51c"]++;
      else if (c <= 53) buckets["51-53c"]++;
      else buckets["> 53c"]++;
    });

    console.log(`\n  Up Price Distribution (${priced.length} markets):`);
    console.log(`    Average:           ${(avg * 100).toFixed(2)}c`);
    console.log(`    Off 50c by >2c:    ${deviated.length} markets`);
    for (const [bucket, count] of Object.entries(buckets)) {
      const bar = "#".repeat(Math.round((count / priced.length) * 50));
      console.log(`    ${bucket.padEnd(8)} ${String(count).padStart(4)} ${bar}`);
    }
  }

  // ── Cross-timeframe check ──────────────────────────────

  console.log("\n──────────────────────────────────────────────────────────");
  console.log("  CROSS-TIMEFRAME: 5m vs 15m vs 4h");
  console.log("──────────────────────────────────────────────────────────");

  // Fetch some 15m and 4h markets for comparison
  const tfSlugs = [];
  for (let ts = refTs; ts > refTs - 6 * 3600; ts -= 900) {
    tfSlugs.push("btc-updown-15m-" + ts);
  }
  for (let ts = refTs; ts > refTs - 24 * 3600; ts -= 14400) {
    tfSlugs.push("btc-updown-4h-" + ts);
  }

  const tfMarkets = [];
  for (const slug of tfSlugs) {
    const m = await fetchMarketBySlug(slug);
    if (m) {
      const prices = m.outcomePrices ? JSON.parse(m.outcomePrices) : [];
      const up = prices[0] ? parseFloat(prices[0]) : null;
      tfMarkets.push({
        slug: m.slug,
        q: m.question,
        up,
        vol: parseFloat(m.volume || 0),
        resolved: up !== null && (up > 0.9 || up < 0.1),
        result: up !== null ? (up > 0.9 ? "UP" : up < 0.1 ? "DOWN" : null) : null,
      });
    }
  }

  const m15 = tfMarkets.filter((m) => m.slug.includes("15m"));
  const m4h = tfMarkets.filter((m) => m.slug.includes("4h"));

  console.log(`  15-min markets found: ${m15.length}`);
  m15.forEach((m) => {
    const s = m.resolved ? m.result : `Up:${(m.up * 100).toFixed(1)}c`;
    console.log(`    ${m.q} => ${s} vol:$${m.vol.toFixed(0)}`);
  });

  console.log(`\n  4-hour markets found: ${m4h.length}`);
  m4h.forEach((m) => {
    const s = m.resolved ? m.result : `Up:${(m.up * 100).toFixed(1)}c`;
    console.log(`    ${m.q} => ${s} vol:$${m.vol.toFixed(0)}`);
  });

  // ── Cross-asset check ──────────────────────────────────

  console.log("\n──────────────────────────────────────────────────────────");
  console.log("  CROSS-ASSET: BTC vs ETH vs SOL vs XRP (15m)");
  console.log("──────────────────────────────────────────────────────────");

  const assets = ["btc", "eth", "sol", "xrp"];
  const assetData = {};
  for (const asset of assets) {
    assetData[asset] = [];
    for (let ts = refTs; ts > refTs - 3 * 3600; ts -= 900) {
      const slug = `${asset}-updown-15m-${ts}`;
      const m = await fetchMarketBySlug(slug);
      if (m) {
        const prices = m.outcomePrices ? JSON.parse(m.outcomePrices) : [];
        const up = prices[0] ? parseFloat(prices[0]) : null;
        assetData[asset].push({
          ts,
          q: m.question,
          up,
          vol: parseFloat(m.volume || 0),
        });
      }
    }
  }

  // Compare same-window prices
  for (const ts of [...new Set(Object.values(assetData).flat().map((m) => m.ts))].sort()) {
    const row = {};
    for (const asset of assets) {
      const m = assetData[asset].find((x) => x.ts === ts);
      if (m) row[asset] = m;
    }
    if (Object.keys(row).length >= 2) {
      const prices = Object.entries(row)
        .map(([a, m]) => `${a.toUpperCase()}:${m.up !== null ? (m.up * 100).toFixed(1) + "c" : "?"}`)
        .join("  ");
      const maxUp = Math.max(...Object.values(row).map((m) => m.up || 0));
      const minUp = Math.min(...Object.values(row).filter((m) => m.up !== null).map((m) => m.up));
      const divergence = ((maxUp - minUp) * 100).toFixed(1);
      const flag = maxUp - minUp > 0.03 ? " << DIVERGENCE" : "";
      console.log(`  ${new Date(ts * 1000).toISOString().slice(11, 16)}  ${prices}  spread:${divergence}c${flag}`);
    }
  }

  // ── Summary ────────────────────────────────────────────
  console.log("\n==========================================================");
  console.log("  SUMMARY & ARBITRAGE OPPORTUNITIES");
  console.log("==========================================================\n");

  if (resolved.length > 0) {
    const upPct = ((resolved.filter((m) => m.result === "UP").length / resolved.length) * 100).toFixed(1);
    console.log(`  Resolved data: ${resolved.length} markets — ${upPct}% went UP`);
  } else {
    console.log("  No resolved markets found in the API (they may be purged).");
    console.log("  Using live market pricing data instead.\n");
  }

  console.log(`  Live pricing: ${priced.length} open markets analyzed`);
  console.log(`  Mispriced (spread > 0.3c): ${mispriced.length}`);
  console.log(`  Markets with volume: ${withVol.length} (total: $${withVol.reduce((a, m) => a + m.vol, 0).toFixed(0)})`);

  if (mispriced.length > 0) {
    console.log("\n  ACTIONABLE ARBITRAGE:");
    mispriced.slice(0, 5).forEach((m) => {
      if (m.sum < 1) {
        const profit = ((1 - m.sum) * 100).toFixed(1);
        console.log(`    BUY BOTH on: ${m.q}`);
        console.log(`      Up @ ${(m.up * 100).toFixed(1)}c + Down @ ${(m.down * 100).toFixed(1)}c = ${(m.sum * 100).toFixed(1)}c => ${profit}c profit`);
      }
    });
  }

  console.log("\n==========================================================\n");
}

main().catch(console.error);
