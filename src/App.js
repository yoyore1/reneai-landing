import React, { useEffect, useState, useRef } from "react";
import "./App.css";

const WS_URL = `ws://${window.location.hostname}:8899/ws`;

function formatPrice(p) {
  if (p == null) return "—";
  return "$" + Number(p).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatPct(p) {
  if (p == null) return "—";
  const sign = p >= 0 ? "+" : "";
  return sign + p.toFixed(3) + "%";
}

function formatPnl(p) {
  if (p == null) return "—";
  const sign = p >= 0 ? "+" : "";
  return sign + "$" + p.toFixed(2);
}

function formatTime(s) {
  if (s == null) return "—";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, "0")}`;
}

function StatusDot({ live }) {
  return <span className={`dot ${live ? "dot-live" : "dot-dead"}`} />;
}

function App() {
  const [state, setState] = useState(null);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef(null);
  const reconnectRef = useRef(null);

  useEffect(() => {
    function connect() {
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        reconnectRef.current = setTimeout(connect, 2000);
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (e) => {
        try {
          setState(JSON.parse(e.data));
        } catch {}
      };
    }

    connect();
    return () => {
      if (wsRef.current) wsRef.current.close();
      if (reconnectRef.current) clearTimeout(reconnectRef.current);
    };
  }, []);

  if (!state) {
    return (
      <div className="app">
        <div className="connecting">
          <div className="spinner" />
          <p>Connecting to bot...</p>
          <p className="sub">Make sure the bot is running: <code>python -m bot --headless</code></p>
        </div>
      </div>
    );
  }

  const { btc_price, btc_live, stats, windows, positions, closed } = state;

  return (
    <div className="app">
      {/* Header */}
      <header className="header">
        <div className="header-left">
          <h1>BTC Arbitrage Bot</h1>
          <span className="header-sub">Binance × Polymarket</span>
        </div>
        <div className="header-center">
          <StatusDot live={btc_live} />
          <span className="btc-price">{formatPrice(btc_price)}</span>
          <span className="btc-label">BTC/USDT</span>
        </div>
        <div className="header-right">
          <span className={`conn-badge ${connected ? "conn-on" : "conn-off"}`}>
            {connected ? "LIVE" : "DISCONNECTED"}
          </span>
        </div>
      </header>

      {/* Stats bar */}
      <div className="stats-bar">
        <div className="stat">
          <span className="stat-label">Signals</span>
          <span className="stat-value">{stats.signals}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Trades</span>
          <span className="stat-value">{stats.trades}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Wins</span>
          <span className="stat-value win">{stats.wins}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Losses</span>
          <span className="stat-value loss">{stats.losses}</span>
        </div>
        <div className="stat stat-pnl">
          <span className="stat-label">Total P&L</span>
          <span className={`stat-value ${stats.pnl >= 0 ? "win" : "loss"}`}>
            {formatPnl(stats.pnl)}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Last Action</span>
          <span className="stat-value action">{stats.last_action || "—"}</span>
        </div>
      </div>

      {/* Main content */}
      <div className="main">
        {/* Left: Windows */}
        <div className="panel">
          <h2>Active Windows</h2>
          {windows.length === 0 ? (
            <div className="empty">No active windows — waiting for markets...</div>
          ) : (
            <div className="window-list">
              {windows.map((w) => {
                const pct = w.time_left != null ? Math.max(0, (w.time_left / 300) * 100) : 100;
                return (
                  <div key={w.id} className="window-card">
                    <div className="window-header">
                      <span className="window-q">{w.question}</span>
                      <span className="window-time">{formatTime(w.time_left)}</span>
                    </div>
                    <div className="window-progress-bg">
                      <div
                        className="window-progress"
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                    <div className="window-row">
                      <span>Open: {formatPrice(w.open_price)}</span>
                      <span className={w.move_pct != null ? (w.move_pct >= 0 ? "green" : "red") : ""}>
                        Move: {formatPct(w.move_pct)}
                      </span>
                      {w.signal_fired && (
                        <span className={`signal-badge ${w.signal_side === "YES" || w.signal_side === "Up" ? "signal-up" : "signal-down"}`}>
                          {w.signal_side}
                        </span>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Right: Positions */}
        <div className="panel">
          <h2>Open Positions</h2>
          {positions.length === 0 ? (
            <div className="empty">No open positions</div>
          ) : (
            <div className="pos-list">
              {positions.map((p, i) => (
                <div key={i} className="pos-card">
                  <div className="pos-header">
                    <span className={`pos-side ${p.side === "Up" || p.side === "YES" ? "side-up" : "side-down"}`}>
                      {p.side}
                    </span>
                    {p.moonbag_mode && <span className="mode-badge moonbag">MOONBAG</span>}
                    {p.protection_mode && <span className="mode-badge protect">PROTECT</span>}
                    <span className="pos-age">{p.age}s</span>
                  </div>
                  <div className="pos-details">
                    <span>Entry: ${p.entry?.toFixed(3)}</span>
                    <span>Qty: {p.qty?.toFixed(1)}</span>
                    <span>Peak: +{p.peak_gain?.toFixed(1)}%</span>
                  </div>
                  <div className="pos-market">{p.market}</div>
                </div>
              ))}
            </div>
          )}

          <h2 style={{ marginTop: "1.5rem" }}>Trade History</h2>
          {closed.length === 0 ? (
            <div className="empty">No closed trades yet</div>
          ) : (
            <table className="trade-table">
              <thead>
                <tr>
                  <th>Side</th>
                  <th>Entry</th>
                  <th>Exit</th>
                  <th>P&L</th>
                  <th>Market</th>
                </tr>
              </thead>
              <tbody>
                {[...closed].reverse().map((t, i) => (
                  <tr key={i}>
                    <td className={t.side === "Up" || t.side === "YES" ? "green" : "red"}>{t.side}</td>
                    <td>${t.entry?.toFixed(3)}</td>
                    <td>${t.exit?.toFixed(3)}</td>
                    <td className={t.pnl >= 0 ? "green" : "red"}>{formatPnl(t.pnl)}</td>
                    <td className="market-cell">{t.market}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

export default App;
