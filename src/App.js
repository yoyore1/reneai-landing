import React, { useEffect, useState, useRef } from "react";
import "./App.css";

const WS_URL = `ws://${window.location.hostname}:8899/ws`;

/* ─── Formatters ─── */
function fmtPrice(p) {
  if (p == null) return "—";
  return "$" + Number(p).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtPct(p) {
  if (p == null) return "—";
  return (p >= 0 ? "+" : "") + p.toFixed(3) + "%";
}
function fmtPnl(p) {
  if (p == null) return "—";
  return (p >= 0 ? "+" : "") + "$" + Math.abs(p).toFixed(2);
}
function fmtTime(s) {
  if (s == null) return "—";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, "0")}`;
}

/* ─── Icons (inline SVG) ─── */
const IconDash = () => (
  <svg width="22" height="22" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
    <rect x="3" y="3" width="7" height="7" rx="1.5" />
    <rect x="14" y="3" width="7" height="7" rx="1.5" />
    <rect x="3" y="14" width="7" height="7" rx="1.5" />
    <rect x="14" y="14" width="7" height="7" rx="1.5" />
  </svg>
);
const IconChart = () => (
  <svg width="22" height="22" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
    <polyline points="22,12 18,12 15,21 9,3 6,12 2,12" />
  </svg>
);
const IconList = () => (
  <svg width="22" height="22" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
    <line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" />
    <circle cx="4" cy="6" r="1" fill="currentColor" /><circle cx="4" cy="12" r="1" fill="currentColor" /><circle cx="4" cy="18" r="1" fill="currentColor" />
  </svg>
);

/* ─── Main App ─── */
function App() {
  const [state, setState] = useState(null);
  const [connected, setConnected] = useState(false);
  const [tab, setTab] = useState("dash");
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
        try { setState(JSON.parse(e.data)); } catch {}
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
          <p className="sub">Run: <code>python -m bot --headless</code></p>
        </div>
      </div>
    );
  }

  const { btc_price, btc_live, stats, windows, positions, closed } = state;

  return (
    <div className="app">
      {/* ── Top Bar ── */}
      <header className="topbar">
        <div className="topbar-price">
          <span className={`dot ${btc_live ? "dot-live" : "dot-dead"}`} />
          <span className="price-val">{fmtPrice(btc_price)}</span>
        </div>
        <div className={`topbar-pnl ${stats.pnl >= 0 ? "green" : "red"}`}>
          {fmtPnl(stats.pnl)}
        </div>
        <span className={`conn-pill ${connected ? "conn-on" : "conn-off"}`}>
          {connected ? "LIVE" : "OFF"}
        </span>
      </header>

      {/* ── Content ── */}
      <main className="content">
        {tab === "dash" && <DashTab stats={stats} windows={windows} positions={positions} />}
        {tab === "windows" && <WindowsTab windows={windows} />}
        {tab === "history" && <HistoryTab closed={closed} />}
      </main>

      {/* ── Bottom Nav ── */}
      <nav className="bottomnav">
        <button className={`nav-btn ${tab === "dash" ? "nav-active" : ""}`} onClick={() => setTab("dash")}>
          <IconDash /><span>Dashboard</span>
        </button>
        <button className={`nav-btn ${tab === "windows" ? "nav-active" : ""}`} onClick={() => setTab("windows")}>
          <IconChart /><span>Windows</span>
        </button>
        <button className={`nav-btn ${tab === "history" ? "nav-active" : ""}`} onClick={() => setTab("history")}>
          <IconList /><span>History</span>
        </button>
      </nav>
    </div>
  );
}

/* ━━━━━━━━━━━━━━━━━━━ Dashboard Tab ━━━━━━━━━━━━━━━━━━━ */
function DashTab({ stats, windows, positions }) {
  return (
    <div className="tab-content">
      {/* Stats Grid */}
      <div className="stats-grid">
        <StatCard label="Signals" value={stats.signals} />
        <StatCard label="Trades" value={stats.trades} />
        <StatCard label="Wins" value={stats.wins} cls="green" />
        <StatCard label="Losses" value={stats.losses} cls="red" />
      </div>

      {/* P&L Hero */}
      <div className="pnl-hero">
        <span className="pnl-label">Total P&L</span>
        <span className={`pnl-value ${stats.pnl >= 0 ? "green" : "red"}`}>
          {fmtPnl(stats.pnl)}
        </span>
        {stats.last_action && (
          <span className="pnl-action">{stats.last_action}</span>
        )}
      </div>

      {/* Open Positions */}
      <h3 className="section-title">Open Positions</h3>
      {positions.length === 0 ? (
        <div className="empty-card">No open positions</div>
      ) : (
        positions.map((p, i) => (
          <div key={i} className="pos-card">
            <div className="pos-top">
              <span className={`pos-side ${p.side === "Up" || p.side === "YES" ? "side-up" : "side-down"}`}>
                {p.side === "Up" || p.side === "YES" ? "▲" : "▼"} {p.side}
              </span>
              <div className="pos-badges">
                {p.moonbag_mode && <span className="badge badge-moon">MOONBAG</span>}
                {p.protection_mode && <span className="badge badge-prot">PROTECT</span>}
              </div>
              <span className="pos-age">{p.age}s</span>
            </div>
            <div className="pos-nums">
              <div className="pos-num"><span className="pn-label">Entry</span><span>${p.entry?.toFixed(3)}</span></div>
              <div className="pos-num"><span className="pn-label">Qty</span><span>{p.qty?.toFixed(1)}</span></div>
              <div className="pos-num"><span className="pn-label">Peak</span><span className="green">+{p.peak_gain?.toFixed(1)}%</span></div>
            </div>
            <div className="pos-mkt">{p.market}</div>
          </div>
        ))
      )}

      {/* Active Windows (compact) */}
      <h3 className="section-title">Active Windows</h3>
      {windows.length === 0 ? (
        <div className="empty-card">Waiting for markets...</div>
      ) : (
        windows.slice(0, 3).map((w) => (
          <MiniWindow key={w.id} w={w} />
        ))
      )}
    </div>
  );
}

/* ━━━━━━━━━━━━━━━━━━━ Windows Tab ━━━━━━━━━━━━━━━━━━━ */
function WindowsTab({ windows }) {
  return (
    <div className="tab-content">
      <h3 className="section-title">All Windows</h3>
      {windows.length === 0 ? (
        <div className="empty-card">No active windows — waiting for markets...</div>
      ) : (
        windows.map((w) => {
          const pct = w.time_left != null ? Math.max(0, (w.time_left / 300) * 100) : 100;
          return (
            <div key={w.id} className="win-card">
              <div className="win-top">
                <span className="win-q">{w.question}</span>
                <span className="win-timer">{fmtTime(w.time_left)}</span>
              </div>
              <div className="win-bar-bg">
                <div className="win-bar" style={{ width: `${pct}%` }} />
              </div>
              <div className="win-meta">
                <span>Open: {fmtPrice(w.open_price)}</span>
                <span className={w.move_pct != null ? (w.move_pct >= 0 ? "green" : "red") : ""}>
                  {fmtPct(w.move_pct)}
                </span>
                {w.signal_fired && (
                  <span className={`sig-badge ${w.signal_side === "YES" || w.signal_side === "Up" ? "sig-up" : "sig-dn"}`}>
                    {w.signal_side}
                  </span>
                )}
              </div>
            </div>
          );
        })
      )}
    </div>
  );
}

/* ━━━━━━━━━━━━━━━━━━━ History Tab ━━━━━━━━━━━━━━━━━━━ */
function HistoryTab({ closed }) {
  const sorted = [...closed].reverse();
  return (
    <div className="tab-content">
      <h3 className="section-title">Trade History</h3>
      {sorted.length === 0 ? (
        <div className="empty-card">No trades yet</div>
      ) : (
        sorted.map((t, i) => {
          const isWin = t.pnl >= 0;
          return (
            <div key={i} className={`hist-card ${isWin ? "hist-win" : "hist-loss"}`}>
              <div className="hist-top">
                <span className={`hist-side ${t.side === "Up" || t.side === "YES" ? "green" : "red"}`}>
                  {t.side === "Up" || t.side === "YES" ? "▲" : "▼"} {t.side}
                </span>
                <span className={`hist-pnl ${isWin ? "green" : "red"}`}>{fmtPnl(t.pnl)}</span>
              </div>
              <div className="hist-nums">
                <span>Entry: ${t.entry?.toFixed(3)}</span>
                <span>Exit: ${t.exit?.toFixed(3)}</span>
                <span>Qty: {t.qty?.toFixed(1)}</span>
              </div>
              <div className="hist-mkt">{t.market}</div>
            </div>
          );
        })
      )}
    </div>
  );
}

/* ━━━━━━━━━━━━━━━━━━━ Shared Components ━━━━━━━━━━━━━━━━━━━ */
function StatCard({ label, value, cls }) {
  return (
    <div className="stat-card">
      <span className="sc-label">{label}</span>
      <span className={`sc-value ${cls || ""}`}>{value}</span>
    </div>
  );
}

function MiniWindow({ w }) {
  const pct = w.time_left != null ? Math.max(0, (w.time_left / 300) * 100) : 100;
  return (
    <div className="mini-win">
      <div className="mw-top">
        <span className="mw-q">{w.question?.replace("Bitcoin Up or Down - ", "")}</span>
        <span className="mw-time">{fmtTime(w.time_left)}</span>
      </div>
      <div className="mw-bar-bg"><div className="mw-bar" style={{ width: `${pct}%` }} /></div>
      <div className="mw-meta">
        <span className={w.move_pct != null ? (w.move_pct >= 0 ? "green" : "red") : ""}>
          {fmtPct(w.move_pct)}
        </span>
        {w.signal_fired && (
          <span className={`sig-badge ${w.signal_side === "YES" || w.signal_side === "Up" ? "sig-up" : "sig-dn"}`}>
            {w.signal_side}
          </span>
        )}
      </div>
    </div>
  );
}

export default App;
