#!/usr/bin/env node

/**
 * BINANCE → POLYMARKET ARBITRAGE BOT
 *
 * Watches Binance BTC price via WebSocket (real-time, ~100ms latency).
 * When a spike is detected, instantly places a market order on Polymarket.
 *
 * Flow:
 *   1. Binance WS → detect spike (>0.10% in 1-2 seconds)
 *   2. Find active Polymarket BTC 5-min market
 *   3. Place buy order on the correct side (Up/Down) via CLOB API
 *   4. Hold to resolution (shares pay $1 if correct, $0 if wrong)
 *
 * Run: node bot/index.js
 * Config: .env file (copy from .env.example)
 */

require("dotenv").config();
const WebSocket = require("ws");
const { ethers } = require("ethers");
const axios = require("axios");
const { ClobClient } = require("@polymarket/clob-client");

// ── Config ───────────────────────────────────────────────────

const CONFIG = {
  privateKey: process.env.PRIVATE_KEY || "",
  polyApiKey: process.env.POLY_API_KEY || "",
  polyApiSecret: process.env.POLY_API_SECRET || "",
  polyApiPassphrase: process.env.POLY_API_PASSPHRASE || "",
  chainId: parseInt(process.env.CHAIN_ID || "137"),
  spikeThreshold: parseFloat(process.env.SPIKE_THRESHOLD || "0.10"),
  tradeSize: parseFloat(process.env.TRADE_SIZE || "100"),
  maxOpenTrades: parseInt(process.env.MAX_OPEN_TRADES || "3"),
  holdToResolution: process.env.HOLD_TO_RESOLUTION !== "false",
  takeProfitPct: parseFloat(process.env.TAKE_PROFIT_PCT || "10"),
  stopLossPct: parseFloat(process.env.STOP_LOSS_PCT || "20"),
};

const GAMMA = "https://gamma-api.polymarket.com";
const CLOB_URL = "https://clob.polymarket.com";

// Binance WebSocket — use standard endpoint, fallback to stream
const BINANCE_WS_URLS = [
  "wss://stream.binance.com:9443/ws/btcusdt@trade",
  "wss://data-stream.binance.vision/ws/btcusdt@trade",
];

// ── State ────────────────────────────────────────────────────

let btcPrices = [];           // rolling window of recent prices
let lastSpikeTime = 0;        // prevent double-triggers
let currentMarket = null;      // cached active Polymarket market
let marketCacheTime = 0;
let openTrades = [];
let tradeHistory = [];
let clobClient = null;

// ── Polymarket Client Setup ──────────────────────────────────

function setupClobClient() {
  if (!CONFIG.privateKey) {
    log("WARNING", "No PRIVATE_KEY set — running in DRY RUN mode (no real trades)");
    return null;
  }

  try {
    const wallet = new ethers.Wallet(CONFIG.privateKey);
    const client = new ClobClient(
      CLOB_URL,
      CONFIG.chainId,
      wallet,
      undefined,
      undefined,
      {
        key: CONFIG.polyApiKey,
        secret: CONFIG.polyApiSecret,
        passphrase: CONFIG.polyApiPassphrase,
      }
    );
    log("OK", "CLOB client initialized for wallet " + wallet.address.slice(0, 8) + "...");
    return client;
  } catch (e) {
    log("ERROR", "Failed to init CLOB client: " + e.message);
    return null;
  }
}

// ── Logging ──────────────────────────────────────────────────

function log(level, msg) {
  const time = new Date().toISOString().slice(11, 23);
  const colors = {
    OK: "\x1b[32m",
    WARN: "\x1b[33m",
    WARNING: "\x1b[33m",
    ERROR: "\x1b[31m",
    TRADE: "\x1b[36m",
    SPIKE: "\x1b[35m",
    INFO: "\x1b[37m",
  };
  const c = colors[level] || "\x1b[37m";
  const r = "\x1b[0m";
  console.log(`${c}[${time}] [${level}]${r} ${msg}`);
}

// ── Market Discovery ─────────────────────────────────────────

async function findActiveMarket() {
  // Cache for 30 seconds
  if (currentMarket && Date.now() - marketCacheTime < 30000) {
    return currentMarket;
  }

  const now = Math.floor(Date.now() / 1000);
  const base = Math.floor(now / 300) * 300;

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
      const tokens = m.clobTokenIds ? JSON.parse(m.clobTokenIds) : [];
      const end = new Date(m.endDate);
      const secsLeft = (end - Date.now()) / 1000;

      // Must have >30 sec left, not resolved, have tokens
      if (up !== null && up > 0.05 && up < 0.95 && secsLeft > 30 && tokens.length >= 2) {
        currentMarket = {
          slug,
          question: m.question,
          endDate: m.endDate,
          secsLeft,
          upToken: tokens[0],
          downToken: tokens[1],
          conditionId: m.conditionId,
        };
        marketCacheTime = Date.now();
        return currentMarket;
      }
    } catch {}
  }
  return null;
}

// ── Order Execution ──────────────────────────────────────────

async function placeBuyOrder(side, market) {
  const tokenId = side === "UP" ? market.upToken : market.downToken;

  // Get the best ask from the order book
  let bestAsk = 0.51; // default
  try {
    const book = await axios.get(`${CLOB_URL}/book`, {
      params: { token_id: tokenId },
      timeout: 2000,
    });
    const asks = (book.data.asks || []).sort(
      (a, b) => parseFloat(a.price) - parseFloat(b.price)
    );
    if (asks.length > 0) {
      bestAsk = parseFloat(asks[0].price);
    }
  } catch {}

  // Don't buy if price is already too high (book already repriced)
  if (bestAsk > 0.60) {
    log("WARN", `Book already repriced — ${side} ask is ${(bestAsk * 100).toFixed(0)}c, skipping`);
    return null;
  }

  const shares = Math.floor(CONFIG.tradeSize / bestAsk);

  if (!clobClient) {
    // DRY RUN — simulate the trade
    log("TRADE", `[DRY RUN] BUY ${side} — ${shares} shares @ ${(bestAsk * 100).toFixed(1)}c = $${(shares * bestAsk).toFixed(2)}`);
    return {
      orderId: "dry-run-" + Date.now(),
      side,
      tokenId,
      entry: bestAsk,
      shares,
      spent: shares * bestAsk,
      market: market.question,
      slug: market.slug,
      time: new Date().toISOString(),
      dryRun: true,
    };
  }

  // REAL ORDER via Polymarket CLOB API
  try {
    log("TRADE", `Placing REAL order: BUY ${side} — ${shares} shares @ ${(bestAsk * 100).toFixed(1)}c`);

    const order = await clobClient.createAndPlaceOrder({
      tokenID: tokenId,
      price: bestAsk,
      side: "BUY",
      size: shares,
    });

    log("OK", `Order placed! ID: ${order.orderID || order.id || "unknown"}`);

    return {
      orderId: order.orderID || order.id,
      side,
      tokenId,
      entry: bestAsk,
      shares,
      spent: shares * bestAsk,
      market: market.question,
      slug: market.slug,
      time: new Date().toISOString(),
      dryRun: false,
    };
  } catch (e) {
    log("ERROR", `Order failed: ${e.message}`);
    return null;
  }
}

async function placeSellOrder(trade) {
  if (!clobClient || trade.dryRun) {
    log("TRADE", `[DRY RUN] SELL ${trade.side} — ${trade.shares} shares`);
    return true;
  }

  try {
    const order = await clobClient.createAndPlaceOrder({
      tokenID: trade.tokenId,
      price: trade.entry * (1 + CONFIG.takeProfitPct / 100),
      side: "SELL",
      size: trade.shares,
    });
    log("OK", `Sell order placed! ID: ${order.orderID || order.id || "unknown"}`);
    return true;
  } catch (e) {
    log("ERROR", `Sell order failed: ${e.message}`);
    return false;
  }
}

// ── Spike Detection ──────────────────────────────────────────

function detectSpike(price) {
  btcPrices.push({ price, time: Date.now() });

  // Keep 10-second rolling window
  const cutoff = Date.now() - 10000;
  btcPrices = btcPrices.filter((p) => p.time > cutoff);

  if (btcPrices.length < 3) return null;

  // Compare current price to price 1-2 seconds ago
  const recent = btcPrices.filter((p) => p.time > Date.now() - 2000);
  const older = btcPrices.filter(
    (p) => p.time > Date.now() - 4000 && p.time < Date.now() - 1500
  );

  if (recent.length === 0 || older.length === 0) return null;

  const currentPrice = recent[recent.length - 1].price;
  const oldPrice = older[0].price;
  const changePct = ((currentPrice - oldPrice) / oldPrice) * 100;

  if (Math.abs(changePct) >= CONFIG.spikeThreshold) {
    // Debounce — don't trigger again within 10 seconds
    if (Date.now() - lastSpikeTime < 10000) return null;
    lastSpikeTime = Date.now();

    return {
      direction: changePct > 0 ? "UP" : "DOWN",
      changePct,
      price: currentPrice,
      oldPrice,
    };
  }

  return null;
}

// ── Trade Monitor ────────────────────────────────────────────

async function checkOpenTrades() {
  for (let i = openTrades.length - 1; i >= 0; i--) {
    const trade = openTrades[i];

    // Check if market has resolved
    try {
      const m = await axios.get(`${GAMMA}/markets`, {
        params: { slug: trade.slug },
        timeout: 3000,
      });
      const mkt = (m.data || [])[0];
      if (!mkt) continue;

      const p = mkt.outcomePrices ? JSON.parse(mkt.outcomePrices) : [];
      const up = p[0] ? parseFloat(p[0]) : null;

      if (up !== null && (up > 0.95 || up < 0.05)) {
        const result = up > 0.5 ? "UP" : "DOWN";
        const won = trade.side === result;
        const pnl = won
          ? trade.shares * 1.0 - trade.spent - (trade.shares * 1.0 - trade.spent) * 0.02
          : -trade.spent;

        const icon = won ? "✅" : "❌";
        log(
          "TRADE",
          `${icon} RESOLVED: ${trade.market} → ${result} | ` +
            `Bet: ${trade.side} @ ${(trade.entry * 100).toFixed(0)}c | ` +
            `P&L: ${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)}`
        );

        tradeHistory.push({ ...trade, result, won, pnl });
        openTrades.splice(i, 1);

        // Print running total
        const totalPnL = tradeHistory.reduce((a, t) => a + t.pnl, 0);
        const totalWins = tradeHistory.filter((t) => t.won).length;
        log(
          "INFO",
          `Running: ${totalWins}/${tradeHistory.length} wins | Total P&L: ${totalPnL >= 0 ? "+" : ""}$${totalPnL.toFixed(2)}`
        );
      }
    } catch {}
  }
}

// ── Binance WebSocket ────────────────────────────────────────

function connectBinance() {
  let wsIndex = 0;

  function connect() {
    const url = BINANCE_WS_URLS[wsIndex % BINANCE_WS_URLS.length];
    log("INFO", "Connecting to Binance WS: " + url);

    const ws = new WebSocket(url);
    let lastStatusTime = 0;

    ws.on("open", () => {
      log("OK", "Binance WebSocket connected");
    });

    ws.on("message", async (data) => {
      try {
        const msg = JSON.parse(data);
        const price = parseFloat(msg.p);
        if (!price) return;

        // Status update every 10 seconds
        if (Date.now() - lastStatusTime > 10000) {
          const mkt = await findActiveMarket();
          const mktStr = mkt
            ? mkt.question.split(" - ")[1] + " (" + Math.round(mkt.secsLeft) + "s left)"
            : "searching...";
          process.stdout.write(
            `\r  BTC: $${price.toFixed(0)} | Market: ${mktStr} | Open: ${openTrades.length} | History: ${tradeHistory.length}    `
          );
          lastStatusTime = Date.now();
        }

        // Detect spike
        const spike = detectSpike(price);
        if (!spike) return;

        log(
          "SPIKE",
          `${spike.changePct >= 0 ? "+" : ""}${spike.changePct.toFixed(3)}% ($${spike.oldPrice.toFixed(0)} → $${spike.price.toFixed(0)})`
        );

        // Check if we can trade
        if (openTrades.length >= CONFIG.maxOpenTrades) {
          log("WARN", "Max open trades reached, skipping");
          return;
        }

        // Find active market
        const market = await findActiveMarket();
        if (!market) {
          log("WARN", "No active Polymarket market found, skipping");
          return;
        }

        if (market.secsLeft < 30) {
          log("WARN", "Market resolves in <30s, too risky, skipping");
          return;
        }

        // EXECUTE
        log("TRADE", `Spike ${spike.direction} detected → buying on ${market.question}`);
        const trade = await placeBuyOrder(spike.direction, market);

        if (trade) {
          openTrades.push(trade);

          if (!CONFIG.holdToResolution) {
            // Place take-profit sell order
            await placeSellOrder(trade);
          }
        }
      } catch (e) {
        // Silently handle parse errors from WS
      }
    });

    ws.on("close", () => {
      log("WARN", "Binance WS disconnected, reconnecting in 2s...");
      setTimeout(connect, 2000);
    });

    ws.on("error", (e) => {
      log("ERROR", "Binance WS error: " + e.message);
      wsIndex++;
      ws.close();
    });
  }

  connect();
}

// ── Main ─────────────────────────────────────────────────────

async function main() {
  console.log("╔═══════════════════════════════════════════════════════════════╗");
  console.log("║  BINANCE → POLYMARKET ARBITRAGE BOT                        ║");
  console.log("╠═══════════════════════════════════════════════════════════════╣");
  console.log("║  Spike threshold: " + (CONFIG.spikeThreshold + "%").padEnd(10) + "Trade size: $" + CONFIG.tradeSize.toString().padEnd(15) + "║");
  console.log("║  Max open trades: " + String(CONFIG.maxOpenTrades).padEnd(10) + "Mode: " + (CONFIG.holdToResolution ? "Hold to Resolution" : "Take Profit " + CONFIG.takeProfitPct + "%").padEnd(19) + "║");
  console.log("║  Dry run: " + (CONFIG.privateKey ? "NO (live trading)" : "YES (no real orders)").padEnd(40) + "║");
  console.log("╚═══════════════════════════════════════════════════════════════╝\n");

  // Setup CLOB client
  clobClient = setupClobClient();

  // Start Binance WebSocket
  connectBinance();

  // Poll for trade resolution every 15 seconds
  setInterval(async () => {
    if (openTrades.length > 0) {
      await checkOpenTrades();
    }
  }, 15000);

  log("INFO", "Bot is running. Watching for BTC spikes...");
  log("INFO", "Press Ctrl+C to stop.\n");
}

main().catch((e) => {
  log("ERROR", e.message);
  process.exit(1);
});
