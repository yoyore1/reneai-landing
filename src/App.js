import React, { useState, useEffect, useCallback } from "react";
import "./App.css";

const GAMMA_API = "https://gamma-api.polymarket.com";

function App() {
  const [markets, setMarkets] = useState([]);
  const [groups, setGroups] = useState({});
  const [btcStats, setBtcStats] = useState(null);
  const [arbitrageResults, setArbitrageResults] = useState(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdate, setLastUpdate] = useState(null);
  const [error, setError] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      const [mktsRes, statsRes] = await Promise.all([
        fetch(
          `${GAMMA_API}/markets?active=true&closed=false&order=startDate&ascending=false&limit=100`
        ).then((r) => r.json()),
        fetch(
          "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=1"
        )
          .then((r) => r.json())
          .catch(() => null),
      ]);

      const updownMarkets = (mktsRes || []).filter((m) =>
        (m.slug || "").includes("updown")
      );
      setMarkets(updownMarkets);

      const grouped = groupMarkets(updownMarkets);
      setGroups(grouped);

      let stats = null;
      if (statsRes && statsRes.prices) {
        stats = computeBTCStats(statsRes.prices);
      }
      setBtcStats(stats);

      const arb = runArbitrageAnalysis(grouped, stats);
      setArbitrageResults(arb);

      setLastUpdate(new Date());
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 30000);
    return () => clearInterval(interval);
  }, [fetchData]);

  if (loading) {
    return (
      <div className="loading-screen">
        <div className="loading-pulse" />
        <p>Scanning Polymarket for arbitrage...</p>
      </div>
    );
  }

  return (
    <div className="app">
      <header className="header">
        <div className="header-left">
          <h1>
            <span className="accent">BTC</span> 5-Min Arbitrage Scanner
          </h1>
          <span className="subtitle">Polymarket Up/Down Markets</span>
        </div>
        <div className="header-right">
          {btcStats && (
            <div className="btc-price">
              <span className="label">BTC</span>
              <span className="price">
                ${btcStats.currentPrice.toLocaleString(undefined, { maximumFractionDigits: 0 })}
              </span>
            </div>
          )}
          {lastUpdate && (
            <span className="last-update">
              Updated {lastUpdate.toLocaleTimeString()}
            </span>
          )}
          <button className="refresh-btn" onClick={fetchData}>
            Refresh
          </button>
        </div>
      </header>

      {error && <div className="error-banner">Error: {error}</div>}

      {btcStats && <BTCStatsPanel stats={btcStats} />}

      <div className="main-grid">
        <div className="col-markets">
          <h2>Active Markets</h2>
          <MarketsPanel groups={groups} />
        </div>
        <div className="col-arbitrage">
          <h2>Arbitrage Opportunities</h2>
          {arbitrageResults && <ArbitragePanel results={arbitrageResults} />}
        </div>
      </div>

      <StrategyPlaybook btcStats={btcStats} />
    </div>
  );
}

function BTCStatsPanel({ stats }) {
  return (
    <div className="stats-panel">
      <div className="stat-card">
        <span className="stat-label">Up Candles (24h)</span>
        <span className="stat-value green">{stats.upPct}%</span>
      </div>
      <div className="stat-card">
        <span className="stat-label">Down Candles (24h)</span>
        <span className="stat-value red">{stats.downPct}%</span>
      </div>
      <div className="stat-card">
        <span className="stat-label">Momentum</span>
        <span className="stat-value">{stats.momentumPct}%</span>
        <span className="stat-sub">same direction continues</span>
      </div>
      <div className="stat-card">
        <span className="stat-label">Avg |Move|</span>
        <span className="stat-value">{stats.avgAbsChange}%</span>
        <span className="stat-sub">per 5-min candle</span>
      </div>
      <div className="stat-card">
        <span className="stat-label">Last 3 Moves</span>
        <span className="stat-value">
          {stats.last3.map((d, i) => (
            <span key={i} className={d === "UP" ? "green" : "red"}>
              {d === "UP" ? "▲" : "▼"}
            </span>
          ))}
        </span>
      </div>
      <div className="stat-card">
        <span className="stat-label">Volatility</span>
        <span className="stat-value">{stats.stdDev}%</span>
        <span className="stat-sub">5-min std dev</span>
      </div>
    </div>
  );
}

function MarketsPanel({ groups }) {
  const groupOrder = [
    "BTC-5m",
    "BTC-15m",
    "BTC-4h",
    "ETH-15m",
    "SOL-15m",
    "XRP-15m",
  ];

  return (
    <div className="markets-list">
      {groupOrder.map((key) => {
        const mks = groups[key];
        if (!mks || mks.length === 0) return null;
        return (
          <div key={key} className="market-group">
            <h3 className="group-title">{key}</h3>
            {mks.map((m, i) => {
              const upPct = m._upPrice !== null ? (m._upPrice * 100).toFixed(1) : "?";
              const downPct = m._downPrice !== null ? (m._downPrice * 100).toFixed(1) : "?";
              const sum =
                m._upPrice !== null && m._downPrice !== null
                  ? ((m._upPrice + m._downPrice) * 100).toFixed(1)
                  : "?";
              const isMispriced = sum !== "?" && Math.abs(parseFloat(sum) - 100) > 0.5;

              return (
                <div
                  key={i}
                  className={`market-card ${isMispriced ? "mispriced" : ""}`}
                >
                  <div className="market-question">
                    {extractTimeWindow(m.question)}
                  </div>
                  <div className="market-prices">
                    <span className="up-price">
                      ▲ {upPct}c
                    </span>
                    <span className="down-price">
                      ▼ {downPct}c
                    </span>
                    <span className={`sum-price ${isMispriced ? "alert" : ""}`}>
                      Σ {sum}c
                    </span>
                  </div>
                  {parseFloat(m.volume || 0) > 0 && (
                    <div className="market-vol">
                      Vol: ${parseFloat(m.volume).toFixed(0)}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}

function ArbitragePanel({ results }) {
  const total =
    results.spread.length +
    results.crossTimeframe.length +
    results.crossAsset.length +
    results.statistical.length +
    results.momentum.length;

  if (total === 0) {
    return (
      <div className="no-arb">
        <div className="no-arb-icon">⚖️</div>
        <h3>Markets Currently Efficient</h3>
        <p>All Up + Down pairs sum to ~$1.00 and pricing is near 50/50.</p>
        <div className="best-times">
          <h4>Best times to find arbitrage:</h4>
          <ul>
            <li>Right when a new 5-min window opens (thin books)</li>
            <li>During BTC volatility spikes (news, whale moves)</li>
            <li>When BTC makes a sharp 5-min move and other markets lag</li>
            <li>Cross-asset: when BTC moves but ETH/SOL/XRP markets haven't repriced</li>
          </ul>
        </div>
      </div>
    );
  }

  return (
    <div className="arb-results">
      {results.spread.length > 0 && (
        <ArbSection
          title="Spread Arbitrage (Risk-Free)"
          icon="💰"
          items={results.spread}
          renderItem={(o) => (
            <>
              <div className="arb-market">{o.market}</div>
              <div className="arb-detail">
                Up: {(o.upPrice * 100).toFixed(1)}c + Down:{" "}
                {(o.downPrice * 100).toFixed(1)}c = {(o.sum * 100).toFixed(1)}c
              </div>
              {o.type === "GUARANTEED_PROFIT" && (
                <div className="arb-profit">Guaranteed {o.profit} profit</div>
              )}
            </>
          )}
        />
      )}

      {results.crossTimeframe.length > 0 && (
        <ArbSection
          title="Cross-Timeframe Mismatch"
          icon="⏱️"
          items={results.crossTimeframe}
          renderItem={(o) => (
            <>
              <div className="arb-type">{o.type}</div>
              <div className="arb-strategy">{o.strategy}</div>
            </>
          )}
        />
      )}

      {results.crossAsset.length > 0 && (
        <ArbSection
          title="Cross-Asset Divergence"
          icon="🔀"
          items={results.crossAsset}
          renderItem={(o) => (
            <>
              <div className="arb-market">
                {o.asset1} vs {o.asset2}
              </div>
              <div className="arb-strategy">{o.strategy}</div>
            </>
          )}
        />
      )}

      {results.statistical.length > 0 && (
        <ArbSection
          title="Statistical Mispricing"
          icon="📊"
          items={results.statistical}
          renderItem={(o) => (
            <>
              <div className="arb-market">{o.market}</div>
              <div className="arb-strategy">{o.strategy}</div>
            </>
          )}
        />
      )}

      {results.momentum.length > 0 && (
        <ArbSection
          title="Momentum Edge"
          icon="📈"
          items={results.momentum}
          renderItem={(o) => (
            <>
              <div className="arb-market">{o.market}</div>
              <div className="arb-detail">
                Last 3: {o.last3} | Autocorrelation: {o.momentumPct}%
              </div>
              <div className="arb-strategy">{o.strategy}</div>
            </>
          )}
        />
      )}
    </div>
  );
}

function ArbSection({ title, icon, items, renderItem }) {
  return (
    <div className="arb-section">
      <h3>
        {icon} {title}{" "}
        <span className="count">{items.length}</span>
      </h3>
      {items.map((item, i) => (
        <div key={i} className="arb-card">
          {renderItem(item)}
        </div>
      ))}
    </div>
  );
}

function StrategyPlaybook({ btcStats }) {
  const [expanded, setExpanded] = useState(false);

  const strategies = [
    {
      id: 1,
      title: "Spread Snipe (Risk-Free)",
      icon: "💰",
      risk: "None",
      edge: "Variable (1-5%)",
      description:
        "If Up + Down < $1.00, buy both sides. One always resolves to $1.00, guaranteeing profit.",
      howTo: [
        "Monitor newly created 5-min markets (first 30 seconds after creation)",
        "Check if Up price + Down price < $1.00",
        "Buy equal amounts of both Up and Down",
        "Wait for resolution — guaranteed profit of $1.00 minus your total cost",
      ],
    },
    {
      id: 2,
      title: "Cross-Timeframe Arbitrage",
      icon: "⏱️",
      risk: "Low",
      edge: "5-15%",
      description:
        "The 15-min market covers three consecutive 5-min windows. After the first 5-min window resolves, the 15-min outcome becomes partially decided but may not reprice fast enough.",
      howTo: [
        "Watch the first 5-min window within a 15-min block",
        "If BTC goes UP +0.3% in first 5 min, the 15-min 'Up' probability should be >60%",
        "If the 15-min market is still priced near 50c, buy 'Up' immediately",
        "Same logic applies to 4-hour blocks — after several 15-min windows resolve, the 4h outcome is clearer",
      ],
    },
    {
      id: 3,
      title: "Cross-Asset Correlation",
      icon: "🔀",
      risk: "Low-Medium",
      edge: "3-8%",
      description:
        "BTC, ETH, SOL, and XRP are 80-95% correlated on 15-minute timeframes. If one asset's market reprices but others lag, that's an arbitrage.",
      howTo: [
        "Monitor all four assets' 15-min markets simultaneously",
        "When BTC 15-min 'Up' jumps from 50c to 58c after a BTC pump...",
        "Check if ETH/SOL/XRP 15-min 'Up' are still near 50c",
        "Buy 'Up' on the lagging asset — it will likely follow BTC within minutes",
      ],
    },
    {
      id: 4,
      title: "Momentum Autocorrelation",
      icon: "📈",
      risk: "Medium",
      edge: "~4%",
      description: `BTC 5-min candles show ${btcStats ? btcStats.momentumPct : "~54"}% autocorrelation — the next candle tends to go the same direction as the previous one. At 50c pricing, this gives a statistical edge.`,
      howTo: [
        "Check the last resolved 5-min candle direction",
        "If it was UP, buy 'Up' on the next 5-min market at 50c or lower",
        "If it was DOWN, buy 'Down' on the next 5-min market at 50c or lower",
        "Expected edge: ~4 cents per dollar risked (long-term positive EV)",
      ],
    },
    {
      id: 5,
      title: "Volatility Event Front-Run",
      icon: "⚡",
      risk: "Medium-High",
      edge: "10-50%+",
      description:
        "Before known volatility events (Fed announcements, CPI data, large options expiries), place limit orders at extreme prices on both sides. The sharp move will push one side to near $1.",
      howTo: [
        "Check economic calendar for upcoming events (Fed, CPI, jobs report)",
        "5-10 minutes before the event, place limit orders:",
        "  - Buy 'Up' at 40-45c AND Buy 'Down' at 40-45c",
        "The event will sharply move BTC — one side resolves near $1.00",
        "Total cost ~85-90c, payout $1.00 = 10-17% profit if vol is high enough",
      ],
    },
    {
      id: 6,
      title: "Market Open Liquidity Snipe",
      icon: "🎯",
      risk: "Low-Medium",
      edge: "5-10%",
      description:
        "New 5-min markets are created with very wide bid/ask spreads (1c/99c). Place limit orders near 45-48c before makers tighten the books to 50c.",
      howTo: [
        "Watch for new market creation every 5 minutes",
        "Immediately place limit order at 45-48c for your predicted direction",
        "As market makers post tighter quotes, the book will move toward 50c",
        "You're already in at a discount — even a 50/50 outcome gives you 2-5c edge",
      ],
    },
  ];

  return (
    <div className="playbook">
      <div className="playbook-header" onClick={() => setExpanded(!expanded)}>
        <h2>Strategy Playbook</h2>
        <span className="expand-icon">{expanded ? "▼" : "▶"}</span>
      </div>
      {expanded && (
        <div className="strategies-grid">
          {strategies.map((s) => (
            <div key={s.id} className="strategy-card">
              <div className="strategy-header">
                <span className="strategy-icon">{s.icon}</span>
                <h3>{s.title}</h3>
              </div>
              <div className="strategy-meta">
                <span className="risk">Risk: {s.risk}</span>
                <span className="edge">Edge: {s.edge}</span>
              </div>
              <p className="strategy-desc">{s.description}</p>
              <div className="strategy-steps">
                <h4>How to execute:</h4>
                <ol>
                  {s.howTo.map((step, i) => (
                    <li key={i}>{step}</li>
                  ))}
                </ol>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Helpers ───────────────────────────────────────────────

function extractTimeWindow(question) {
  const match = question.match(/- (.+)$/);
  return match ? match[1] : question;
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

    const outcomePrices = m.outcomePrices ? JSON.parse(m.outcomePrices) : [];
    groups[key].push({
      ...m,
      _asset: asset,
      _tf: tf,
      _upPrice: outcomePrices.length >= 2 ? parseFloat(outcomePrices[0]) : null,
      _downPrice: outcomePrices.length >= 2 ? parseFloat(outcomePrices[1]) : null,
    });
  });

  Object.values(groups).forEach((arr) =>
    arr.sort((a, b) => new Date(a.endDate) - new Date(b.endDate))
  );

  return groups;
}

function computeBTCStats(prices) {
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
  const absAvg = changes.reduce((a, b) => a + Math.abs(b), 0) / changes.length;
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
    avgAbsChange: absAvg.toFixed(4),
    stdDev: stdDev.toFixed(4),
    lastDirection: candles.length > 0 ? candles[candles.length - 1].direction : null,
    last3: candles.slice(-3).map((c) => c.direction),
  };
}

function runArbitrageAnalysis(groups, btcStats) {
  return {
    spread: findSpreadArb(groups),
    crossTimeframe: findCrossTimeframeArb(groups),
    crossAsset: findCrossAssetArb(groups),
    statistical: findStatisticalEdge(groups, btcStats),
    momentum: findMomentumEdge(groups, btcStats),
  };
}

function findSpreadArb(groups) {
  const opps = [];
  for (const markets of Object.values(groups)) {
    for (const m of markets) {
      if (m._upPrice !== null && m._downPrice !== null) {
        const sum = m._upPrice + m._downPrice;
        if (Math.abs(1.0 - sum) > 0.005) {
          opps.push({
            type: sum < 1.0 ? "GUARANTEED_PROFIT" : "OVERPRICED",
            market: m.question,
            upPrice: m._upPrice,
            downPrice: m._downPrice,
            sum,
            profit: sum < 1.0 ? ((1 - sum) * 100).toFixed(2) + "%" : null,
          });
        }
      }
    }
  }
  return opps.sort((a, b) => Math.abs(1 - a.sum) - Math.abs(1 - b.sum));
}

function findCrossTimeframeArb(groups) {
  const opps = [];
  const btc5m = groups["BTC-5m"] || [];
  const btc15m = groups["BTC-15m"] || [];

  for (const m15 of btc15m) {
    const end15 = new Date(m15.endDate).getTime();
    const start15 = end15 - 15 * 60 * 1000;

    const matching = btc5m.filter((m) => {
      const end = new Date(m.endDate).getTime();
      return end > start15 && end <= end15;
    });

    if (matching.length > 0 && m15._upPrice !== null) {
      const avg5mUp =
        matching.reduce((a, b) => a + (b._upPrice || 0.5), 0) / matching.length;
      if (Math.abs(avg5mUp - m15._upPrice) > 0.03) {
        opps.push({
          type: "TIMEFRAME_MISMATCH",
          market15m: m15.question,
          price15mUp: m15._upPrice,
          avg5mUp,
          strategy:
            avg5mUp > m15._upPrice
              ? `5-min markets average ${(avg5mUp * 100).toFixed(1)}% Up but 15-min only ${(m15._upPrice * 100).toFixed(1)}%. Buy Up on 15-min.`
              : `5-min markets average ${(avg5mUp * 100).toFixed(1)}% Up but 15-min is ${(m15._upPrice * 100).toFixed(1)}%. Buy Down on 15-min.`,
        });
      }
    }
  }
  return opps;
}

function findCrossAssetArb(groups) {
  const opps = [];
  const assets = ["BTC", "ETH", "SOL", "XRP"];
  const prices15m = {};

  for (const asset of assets) {
    const mks = groups[`${asset}-15m`] || [];
    if (mks.length > 0) {
      prices15m[asset] = mks.map((m) => ({
        q: m.question,
        up: m._upPrice,
        endDate: m.endDate,
      }));
    }
  }

  const keys = Object.keys(prices15m);
  for (let i = 0; i < keys.length; i++) {
    for (let j = i + 1; j < keys.length; j++) {
      const a1 = keys[i], a2 = keys[j];
      for (const m1 of prices15m[a1]) {
        for (const m2 of prices15m[a2]) {
          const td = Math.abs(
            new Date(m1.endDate).getTime() - new Date(m2.endDate).getTime()
          );
          if (td < 2 * 60000 && m1.up !== null && m2.up !== null) {
            const diff = Math.abs(m1.up - m2.up);
            if (diff > 0.04) {
              opps.push({
                asset1: a1,
                asset2: a2,
                market1: m1.q,
                market2: m2.q,
                strategy:
                  m1.up > m2.up
                    ? `${a1} Up at ${(m1.up * 100).toFixed(1)}c but ${a2} Up only ${(m2.up * 100).toFixed(1)}c. Buy ${a2} Up.`
                    : `${a2} Up at ${(m2.up * 100).toFixed(1)}c but ${a1} Up only ${(m1.up * 100).toFixed(1)}c. Buy ${a1} Up.`,
              });
            }
          }
        }
      }
    }
  }
  return opps;
}

function findStatisticalEdge(groups, stats) {
  if (!stats) return [];
  const opps = [];
  const fairUp = parseFloat(stats.upPct) / 100;

  for (const markets of Object.values(groups)) {
    for (const m of markets) {
      if (m._upPrice === null) continue;
      const dev = Math.abs(m._upPrice - fairUp);
      if (dev > 0.06) {
        opps.push({
          market: m.question,
          currentUpPrice: m._upPrice,
          fairValue: fairUp,
          deviation: dev,
          strategy:
            m._upPrice > fairUp
              ? `Up priced at ${(m._upPrice * 100).toFixed(1)}c, fair value ~${(fairUp * 100).toFixed(1)}c. Buy Down for ${(dev * 100).toFixed(1)}c edge.`
              : `Up priced at ${(m._upPrice * 100).toFixed(1)}c, fair value ~${(fairUp * 100).toFixed(1)}c. Buy Up for ${(dev * 100).toFixed(1)}c edge.`,
        });
      }
    }
  }
  return opps.sort((a, b) => b.deviation - a.deviation);
}

function findMomentumEdge(groups, stats) {
  if (!stats || !stats.lastDirection) return [];
  const opps = [];
  const momentum = parseFloat(stats.momentumPct);

  if (momentum > 52) {
    const btc5m = groups["BTC-5m"] || [];
    if (btc5m.length > 0) {
      const next = btc5m[0];
      if (next._upPrice !== null) {
        const edge = ((momentum - 50) / 100).toFixed(4);
        opps.push({
          market: next.question,
          lastDirection: stats.lastDirection,
          last3: stats.last3.join(" -> "),
          momentumPct: momentum.toFixed(1),
          edge,
          strategy:
            stats.lastDirection === "UP"
              ? `Last move was UP (${stats.last3.join(" -> ")}). ${momentum.toFixed(1)}% autocorrelation. Buy Up at ${(next._upPrice * 100).toFixed(1)}c.`
              : `Last move was DOWN (${stats.last3.join(" -> ")}). ${momentum.toFixed(1)}% autocorrelation. Buy Down at ${(next._downPrice * 100).toFixed(1)}c.`,
        });
      }
    }
  }
  return opps;
}

export default App;
