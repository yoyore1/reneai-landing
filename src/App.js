import React, { useEffect, useState, useRef, useMemo } from "react";
import "./App.css";

const WS_URL = `ws://${window.location.hostname}:8899/ws`;

/* ═══ Formatters ═══ */
const fP = (p) => p == null ? "—" : "$" + Number(p).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fPct = (p) => p == null ? "—" : (p >= 0 ? "+" : "") + p.toFixed(3) + "%";
const fPnl = (p) => p == null ? "—" : (p >= 0 ? "+$" : "-$") + Math.abs(p).toFixed(2);
const fTime = (s) => {
  if (s == null) return "—";
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, "0")}`;
};
const fUptime = (s) => {
  if (!s) return "0s";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${sec}s`;
  return `${sec}s`;
};

/* ═══ SVG Icons ═══ */
const IC = {
  dash: <svg width="22" height="22" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.8"><rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/></svg>,
  chart: <svg width="22" height="22" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.8"><polyline points="22,12 18,12 15,21 9,3 6,12 2,12"/></svg>,
  list: <svg width="22" height="22" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.8"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><circle cx="4" cy="6" r="1.5" fill="currentColor"/><circle cx="4" cy="12" r="1.5" fill="currentColor"/><circle cx="4" cy="18" r="1.5" fill="currentColor"/></svg>,
  gear: <svg width="22" height="22" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.8"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/></svg>,
};

/* ═══ Sparkline (SVG mini chart) ═══ */
function Sparkline({ data, width = 200, height = 40 }) {
  if (!data || data.length < 2) return <div className="sparkline-empty" style={{ width, height }} />;
  const prices = data.map(d => d.p);
  const min = Math.min(...prices), max = Math.max(...prices);
  const range = max - min || 1;
  const points = prices.map((p, i) => {
    const x = (i / (prices.length - 1)) * width;
    const y = height - ((p - min) / range) * (height - 4) - 2;
    return `${x},${y}`;
  }).join(" ");
  const last = prices[prices.length - 1], first = prices[0];
  const color = last >= first ? "var(--green)" : "var(--red)";
  return (
    <svg width={width} height={height} className="sparkline">
      <polyline points={points} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

/* ═══ Win Rate Ring ═══ */
function WinRateRing({ rate, size = 70 }) {
  const r = (size - 8) / 2, circ = 2 * Math.PI * r;
  const offset = circ - (rate / 100) * circ;
  const color = rate >= 60 ? "var(--green)" : rate >= 40 ? "var(--yellow)" : "var(--red)";
  return (
    <div className="ring-wrap" style={{ width: size, height: size }}>
      <svg width={size} height={size}>
        <circle cx={size/2} cy={size/2} r={r} fill="none" stroke="var(--border)" strokeWidth="5" />
        <circle cx={size/2} cy={size/2} r={r} fill="none" stroke={color} strokeWidth="5"
          strokeDasharray={circ} strokeDashoffset={offset} strokeLinecap="round"
          transform={`rotate(-90 ${size/2} ${size/2})`} style={{ transition: "stroke-dashoffset 0.5s" }} />
      </svg>
      <span className="ring-text" style={{ color }}>{rate}%</span>
    </div>
  );
}

/* ═══ P&L Gauge Bar ═══ */
function PnlGauge({ current, low = -15, high = 20 }) {
  const range = high - low;
  const pct = Math.max(0, Math.min(100, ((current - low) / range) * 100));
  const zeroPos = ((0 - low) / range) * 100;
  const color = current >= 10 ? "var(--green)" : current >= 0 ? "var(--blue)" : current >= -10 ? "var(--yellow)" : "var(--red)";
  return (
    <div className="gauge">
      <div className="gauge-bg">
        <div className="gauge-zero" style={{ left: `${zeroPos}%` }} />
        <div className="gauge-fill" style={{ left: `${Math.min(pct, zeroPos)}%`, width: `${Math.abs(pct - zeroPos)}%`, background: color }} />
        <div className="gauge-dot" style={{ left: `${pct}%`, borderColor: color }} />
      </div>
      <div className="gauge-labels">
        <span>{low}%</span><span>0%</span><span>+{high}%</span>
      </div>
    </div>
  );
}

/* ═══ Phase Badge ═══ */
function PhaseBadge({ phase }) {
  const map = {
    waiting: ["WAITING", "phase-wait"],
    settling: ["10s SETTLE", "phase-settle"],
    active: ["ACTIVE", "phase-active"],
    closing: ["NO ENTRY", "phase-close"],
    ended: ["ENDED", "phase-ended"],
  };
  const [label, cls] = map[phase] || ["—", ""];
  return <span className={`phase-badge ${cls}`}>{label}</span>;
}

/* ═══════════════════ MAIN APP ═══════════════════ */
function App() {
  const [state, setState] = useState(null);
  const [connected, setConnected] = useState(false);
  const [tab, setTab] = useState("dash");
  const [strat, setStrat] = useState("s1"); // "s1", "s2", or "s3"
  const wsRef = useRef(null);

  useEffect(() => {
    let timer;
    function connect() {
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;
      ws.onopen = () => setConnected(true);
      ws.onclose = () => { setConnected(false); timer = setTimeout(connect, 2000); };
      ws.onerror = () => ws.close();
      ws.onmessage = (e) => { try { setState(JSON.parse(e.data)); } catch {} };
    }
    connect();
    return () => { if (wsRef.current) wsRef.current.close(); clearTimeout(timer); };
  }, []);

  if (!state) {
    return (
      <div className="app"><div className="connecting">
        <div className="spinner" /><p>Connecting to bot...</p>
        <p className="sub">Run: <code>python -m bot --headless</code></p>
      </div></div>
    );
  }

  const { btc_price, btc_live, stats, windows, positions, closed, config, uptime, price_history, events, s2, s3, s4, calendar } = state;
  const activePnl = strat === "s1" ? stats.pnl : strat === "s2" ? (s2?.stats?.pnl || 0) : (s3?.stats?.pnl || 0);

  return (
    <div className="app">
      {/* ── Top Bar ── */}
      <header className="topbar">
        <div className="topbar-left">
          <span className={`dot ${btc_live ? "dot-live" : "dot-dead"}`} />
          <span className="price-val">{fP(btc_price)}</span>
        </div>
        <div className={`topbar-pnl ${activePnl >= 0 ? "green" : "red"}`}>{fPnl(activePnl)}</div>
        <div className="topbar-right">
          <span className="uptime-pill">{fUptime(uptime)}</span>
          <span className={`conn-pill ${connected ? "conn-on" : "conn-off"}`}>{connected ? "LIVE" : "OFF"}</span>
        </div>
      </header>

      {/* ── Strategy Toggle ── */}
      <div className="strat-toggle">
        <button className={`strat-btn ${strat === "s1" ? "strat-active strat-s1" : ""}`} onClick={() => { setStrat("s1"); setTab("dash"); }}>
          <span className="strat-name">S1: Momentum</span>
          <span className={`strat-pnl ${stats.pnl >= 0 ? "green" : "red"}`}>{fPnl(stats.pnl)}</span>
        </button>
        <button className={`strat-btn ${strat === "s2" ? "strat-active strat-s2" : ""}`} onClick={() => { setStrat("s2"); setTab("dash"); }}>
          <span className="strat-name">S2: Passive</span>
          <span className={`strat-pnl ${(s2?.stats?.pnl || 0) >= 0 ? "green" : "red"}`}>{fPnl(s2?.stats?.pnl || 0)}</span>
        </button>
        <button className={`strat-btn ${strat === "s3" ? "strat-active strat-s3" : ""}`} onClick={() => { setStrat("s3"); setTab("dash"); }}>
          <span className="strat-name">S3: Late</span>
          <span className={`strat-pnl ${(s3?.stats?.pnl || 0) >= 0 ? "green" : "red"}`}>{fPnl(s3?.stats?.pnl || 0)}</span>
        </button>
      </div>

      {/* ── Content ── */}
      <main className="content">
        {tab === "calendar" && <CalendarTab calendar={calendar} />}
        {tab !== "calendar" && strat === "s1" && <>
          {tab === "dash" && <DashTab stats={stats} windows={windows} positions={positions} priceHistory={price_history} config={config} />}
          {tab === "windows" && <WindowsTab windows={windows} />}
          {tab === "history" && <HistoryTab closed={closed} stats={stats} />}
          {tab === "settings" && <SettingsTab config={config} events={events} uptime={uptime} stats={stats} />}
        </>}
        {tab !== "calendar" && strat === "s2" && <>
          {tab === "dash" && <S2DashTab s2={s2} />}
          {tab === "history" && <S2HistoryTab s2={s2} />}
          {tab === "settings" && <SettingsTab config={config} events={events} uptime={uptime} stats={stats} />}
        </>}
        {tab !== "calendar" && strat === "s3" && <>
          {tab === "dash" && <S3DashTab s3={s3} />}
          {tab === "history" && <S3HistoryTab s3={s3} />}
          {tab === "settings" && <SettingsTab config={config} events={events} uptime={uptime} stats={stats} />}
        </>}
      </main>

      {/* ── Bottom Nav ── */}
      <nav className="bottomnav">
        {(strat === "s1"
          ? [["dash", IC.dash, "Dashboard"], ["windows", IC.chart, "Windows"], ["history", IC.list, "History"], ["calendar", "📅", "Calendar"], ["settings", IC.gear, "Settings"]]
          : [["dash", IC.dash, "Dashboard"], ["history", IC.list, "History"], ["calendar", "📅", "Calendar"], ["settings", IC.gear, "Settings"]]
        ).map(([id, icon, label]) => (
          <button key={id} className={`nav-btn ${tab === id ? "nav-active" : ""}`} onClick={() => setTab(id)}>
            {typeof icon === "string" ? <span className="nav-emoji">{icon}</span> : icon}<span>{label}</span>
          </button>
        ))}
      </nav>
    </div>
  );
}

/* ━━━━━━━━━━━━━━━━━━━ CALENDAR TAB ━━━━━━━━━━━━━━━━━━━ */
function CalendarTab({ calendar }) {
  if (!calendar || !calendar.length) {
    return (
      <div className="tab-content">
        <h3 className="section-title">📅 Daily calendar (EST)</h3>
        <div className="empty-card">Calendar loading…</div>
      </div>
    );
  }
  return (
    <div className="tab-content">
      <h3 className="section-title">📅 Daily calendar (EST) — days with hours</h3>
      <p className="calendar-sub">Each day with all 24 hours in Eastern time.</p>
      <div className="calendar-list">
        {calendar.map((day, i) => (
          <div key={day.date} className="calendar-day">
            <div className="calendar-date">{day.date}{i === 0 ? " (today)" : ""}</div>
            <div className="calendar-hours">
              {(day.hours || []).map((h) => (
                <span key={h} className="calendar-hour">{h}</span>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ━━━━━━━━━━━━━━━━━━━ DASHBOARD TAB ━━━━━━━━━━━━━━━━━━━ */
function DashTab({ stats, windows, positions, priceHistory, config }) {
  return (
    <div className="tab-content">
      {/* BTC Price + Sparkline */}
      <div className="price-card">
        <div className="price-card-top">
          <div>
            <span className="pc-label">BTC/USDT</span>
            <span className="pc-price">{fP(priceHistory?.length > 0 ? priceHistory[priceHistory.length - 1].p : null)}</span>
          </div>
          <Sparkline data={priceHistory} width={160} height={45} />
        </div>
      </div>

      {/* Stats Row */}
      <div className="stats-row">
        <div className="sr-card">
          <WinRateRing rate={stats.win_rate} size={64} />
          <span className="sr-label">Win Rate</span>
        </div>
        <div className="sr-card">
          <span className="sr-big green">{stats.wins}</span>
          <span className="sr-label">Wins</span>
        </div>
        <div className="sr-card">
          <span className="sr-big red">{stats.losses}</span>
          <span className="sr-label">Losses</span>
        </div>
        <div className="sr-card">
          <span className="sr-big">{stats.signals}</span>
          <span className="sr-label">Signals</span>
        </div>
      </div>

      {/* P&L Hero */}
      <div className="pnl-hero">
        <div className="pnl-main">
          <span className="pnl-label">Total P&L</span>
          <span className={`pnl-value ${stats.pnl >= 0 ? "green" : "red"}`}>{fPnl(stats.pnl)}</span>
        </div>
        <div className="pnl-details">
          <div className="pnl-detail"><span className="pd-label">Avg Win</span><span className="green">{fPnl(stats.avg_win)}</span></div>
          <div className="pnl-detail"><span className="pd-label">Avg Loss</span><span className="red">{fPnl(stats.avg_loss)}</span></div>
          <div className="pnl-detail"><span className="pd-label">Best</span><span className="green">{fPnl(stats.best_trade)}</span></div>
          <div className="pnl-detail"><span className="pd-label">Worst</span><span className="red">{fPnl(stats.worst_trade)}</span></div>
        </div>
        {stats.last_action && <div className="pnl-action">{stats.last_action}</div>}
      </div>

      {/* Hourly P&L */}
      {stats.hourly_pnl && Object.keys(stats.hourly_pnl).length > 0 && (
        <>
          <h3 className="section-title">📅 Hourly P&L (EST, resets daily)</h3>
          <div className="hourly-grid">
            {Object.entries(stats.hourly_pnl).sort(([a],[b]) => a.localeCompare(b)).map(([hour, pnl]) => (
              <div key={hour} className={`hourly-card ${pnl > 0 ? "hourly-win" : pnl < 0 ? "hourly-loss" : "hourly-flat"}`}>
                <span className="hourly-time">{hour}</span>
                <span className={`hourly-val ${pnl > 0 ? "green" : pnl < 0 ? "red" : ""}`}>{fPnl(pnl)}</span>
              </div>
            ))}
          </div>
        </>
      )}

      {/* Open Positions */}
      <h3 className="section-title">Open Positions <span className="title-count">{positions.length}</span></h3>
      {positions.length === 0 ? (
        <div className="empty-card">No open positions — watching for spikes...</div>
      ) : (
        positions.map((p, i) => <PositionCard key={i} p={p} />)
      )}

      {/* Active Windows (compact) */}
      <h3 className="section-title">Active Windows <span className="title-count">{windows.length}</span></h3>
      {windows.length === 0 ? (
        <div className="empty-card">Waiting for markets...</div>
      ) : (
        windows.slice(0, 4).map((w) => <MiniWindow key={w.id} w={w} />)
      )}
    </div>
  );
}

/* ━━━━━━━━━━━━━━━━━━━ WINDOWS TAB ━━━━━━━━━━━━━━━━━━━ */
function WindowsTab({ windows }) {
  return (
    <div className="tab-content">
      <h3 className="section-title">All 5-Minute Windows <span className="title-count">{windows.length}</span></h3>
      {windows.length === 0 ? (
        <div className="empty-card">No active windows — waiting for markets...</div>
      ) : windows.map((w) => {
        const pct = w.time_left != null ? Math.max(0, (w.time_left / 300) * 100) : 100;
        return (
          <div key={w.id} className="win-card">
            <div className="win-top">
              <span className="win-q">{w.question?.replace("Bitcoin Up or Down - ", "")}</span>
              <PhaseBadge phase={w.phase} />
            </div>
            <div className="win-timer-row">
              <div className="win-bar-bg"><div className="win-bar" style={{ width: `${pct}%` }} /></div>
              <span className="win-timer">{fTime(w.time_left)}</span>
            </div>
            <div className="win-details">
              <div className="wd"><span className="wd-label">Open</span><span>{fP(w.open_price)}</span></div>
              <div className="wd"><span className="wd-label">Move</span><span className={w.move_pct != null ? (w.move_pct >= 0 ? "green" : "red") : ""}>{fPct(w.move_pct)}</span></div>
              <div className="wd">
                {w.signal_fired ? (
                  <span className={`sig-badge ${w.signal_side === "YES" || w.signal_side === "Up" ? "sig-up" : "sig-dn"}`}>
                    {w.signal_side === "Up" || w.signal_side === "YES" ? "▲" : "▼"} {w.signal_side}
                  </span>
                ) : <span className="wd-nosig">No signal</span>}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ━━━━━━━━━━━━━━━━━━━ HISTORY TAB ━━━━━━━━━━━━━━━━━━━ */
function HistoryTab({ closed, stats }) {
  const sorted = useMemo(() => [...closed].reverse(), [closed]);
  const totalTrades = stats.wins + stats.losses;
  return (
    <div className="tab-content">
      {/* Summary Banner */}
      <div className="hist-summary">
        <div className="hs-item"><span className="hs-val">{totalTrades}</span><span className="hs-label">Trades</span></div>
        <div className="hs-item"><span className="hs-val green">{stats.win_rate}%</span><span className="hs-label">Win Rate</span></div>
        <div className="hs-item"><span className={`hs-val ${stats.pnl >= 0 ? "green" : "red"}`}>{fPnl(stats.pnl)}</span><span className="hs-label">Total P&L</span></div>
        <div className="hs-item"><span className="hs-val green">{fPnl(stats.avg_win)}</span><span className="hs-label">Avg Win</span></div>
        <div className="hs-item"><span className="hs-val red">{fPnl(stats.avg_loss)}</span><span className="hs-label">Avg Loss</span></div>
      </div>

      <h3 className="section-title">All Trades <span className="title-count">{sorted.length}</span></h3>
      {sorted.length === 0 ? (
        <div className="empty-card">No trades yet</div>
      ) : sorted.map((t, i) => {
        const isWin = t.pnl >= 0;
        return (
          <div key={i} className={`hist-card ${isWin ? "hist-win" : "hist-loss"}`}>
            <div className="hist-top">
              <span className={`hist-side ${t.side === "Up" || t.side === "YES" ? "green" : "red"}`}>
                {t.side === "Up" || t.side === "YES" ? "▲" : "▼"} {t.side}
              </span>
              <div className="hist-right">
                {t.pnl_pct != null && <span className={`hist-pct ${isWin ? "green" : "red"}`}>{t.pnl_pct >= 0 ? "+" : ""}{t.pnl_pct}%</span>}
                <span className={`hist-pnl ${isWin ? "green" : "red"}`}>{fPnl(t.pnl)}</span>
              </div>
            </div>
            <div className="hist-nums">
              <span>Entry ${t.entry?.toFixed(3)}</span>
              <span>Exit ${t.exit?.toFixed(3)}</span>
              <span>{t.qty?.toFixed(1)} shares</span>
              <span>${t.spent?.toFixed(2)} risked</span>
            </div>
            <div className="hist-mkt">{t.market}</div>
          </div>
        );
      })}
    </div>
  );
}

/* ━━━━━━━━━━━━━━━━━━━ SETTINGS TAB ━━━━━━━━━━━━━━━━━━━ */
function SettingsTab({ config, events, uptime, stats }) {
  return (
    <div className="tab-content">
      {/* Strategy Config */}
      <h3 className="section-title">Strategy Config</h3>
      <div className="config-card">
        <ConfigRow label="Mode" value={config.dry_run ? "DRY RUN (Paper)" : "LIVE TRADING"} cls={config.dry_run ? "yellow" : "red"} />
        <ConfigRow label="Spike Trigger" value={`$${config.spike_move_usd} in ${config.spike_window_sec}s`} sub="Instant momentum — no delay, midpoint verified" />
        <ConfigRow label="Poll Speed" value={`${config.poll_interval || 0.5}s`} sub="How fast we check for spikes" />
        <ConfigRow label="Profit Target" value={`+${config.profit_target}%`} cls="green" />
        <ConfigRow label="Moonbag Trigger" value={`+${config.moonbag}%`} sub={`Trail stop at +${config.profit_target}%`} cls="green" />
        <ConfigRow label="Drawdown Trigger" value={`${config.drawdown_trigger}%`} cls="red" />
        <ConfigRow label="Protection Exit" value={`${config.protection_exit}%`} sub="Sell here to cut losses" cls="yellow" />
        <ConfigRow label="Hard Stop" value={`${config.hard_stop}%`} sub="Emergency sell — no exceptions" cls="red" />
        <ConfigRow label="Max Position" value={`$${config.max_position}`} />
      </div>

      {/* Rules Visual */}
      <h3 className="section-title">Exit Rules</h3>
      <div className="rules-card">
        <div className="rule"><span className="rule-zone rule-moon">+20%+</span><span className="rule-desc">MOONBAG — let it ride, trailing stop at +10%</span></div>
        <div className="rule"><span className="rule-zone rule-profit">+10-20%</span><span className="rule-desc">SELL — take profit immediately</span></div>
        <div className="rule"><span className="rule-zone rule-wait">0-10%</span><span className="rule-desc">HOLD — wait for +10%</span></div>
        <div className="rule"><span className="rule-zone rule-danger">-15%</span><span className="rule-desc">PROTECT — sell at -10% to limit damage</span></div>
        <div className="rule"><span className="rule-zone rule-danger">-25%</span><span className="rule-desc">HARD STOP — sell immediately, no exceptions</span></div>
      </div>

      {/* Session Info */}
      <h3 className="section-title">Session</h3>
      <div className="config-card">
        <ConfigRow label="Uptime" value={fUptime(uptime)} />
        <ConfigRow label="Trades Executed" value={stats.trades} />
        <ConfigRow label="Signals Detected" value={stats.signals} />
      </div>

      {/* Event Log */}
      <h3 className="section-title">Event Log <span className="title-count">{events?.length || 0}</span></h3>
      <div className="log-card">
        {(!events || events.length === 0) ? (
          <div className="log-empty">No events yet</div>
        ) : [...events].reverse().map((e, i) => {
          const d = new Date(e.ts * 1000);
          return (
            <div key={i} className="log-row">
              <span className="log-time">{d.toLocaleTimeString()}</span>
              <span className={`log-kind log-${e.kind}`}>{e.kind}</span>
              <span className="log-msg">{e.msg}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* ━━━━━━━━━━━━━━━━━━━ SHARED COMPONENTS ━━━━━━━━━━━━━━━━━━━ */
function PositionCard({ p }) {
  const isUp = p.side === "Up" || p.side === "YES";
  return (
    <div className="pos-card">
      <div className="pos-top">
        <span className={`pos-side ${isUp ? "side-up" : "side-down"}`}>{isUp ? "▲" : "▼"} {p.side}</span>
        <div className="pos-badges">
          {p.moonbag_mode && <span className="badge badge-moon">MOONBAG</span>}
          {p.protection_mode && <span className="badge badge-prot">PROTECT</span>}
          {!p.moonbag_mode && !p.protection_mode && <span className="badge badge-normal">NORMAL</span>}
        </div>
        <span className="pos-age">{p.age}s</span>
      </div>
      <div className="pos-grid">
        <div className="pg"><span className="pg-label">Entry</span><span className="pg-val">${p.entry?.toFixed(3)}</span></div>
        <div className="pg"><span className="pg-label">Qty</span><span className="pg-val">{p.qty?.toFixed(1)}</span></div>
        <div className="pg"><span className="pg-label">Spent</span><span className="pg-val">${p.spent?.toFixed(2)}</span></div>
        <div className="pg"><span className="pg-label">Peak</span><span className="pg-val green">+{p.peak_gain?.toFixed(1)}%</span></div>
      </div>
      <PnlGauge current={p.peak_gain || 0} />
      <div className="pos-mkt">{p.market}</div>
    </div>
  );
}

function MiniWindow({ w }) {
  const pct = w.time_left != null ? Math.max(0, (w.time_left / 300) * 100) : 100;
  return (
    <div className="mini-win">
      <div className="mw-top">
        <span className="mw-q">{w.question?.replace("Bitcoin Up or Down - ", "")}</span>
        <div className="mw-right">
          <PhaseBadge phase={w.phase} />
          <span className="mw-time">{fTime(w.time_left)}</span>
        </div>
      </div>
      <div className="mw-bar-bg"><div className="mw-bar" style={{ width: `${pct}%` }} /></div>
      <div className="mw-meta">
        <span className={w.move_pct != null ? (w.move_pct >= 0 ? "green" : "red") : ""}>{fPct(w.move_pct)}</span>
        {w.signal_fired && (
          <span className={`sig-badge ${w.signal_side === "YES" || w.signal_side === "Up" ? "sig-up" : "sig-dn"}`}>
            {w.signal_side === "Up" || w.signal_side === "YES" ? "▲" : "▼"} {w.signal_side}
          </span>
        )}
      </div>
    </div>
  );
}

/* ━━━━━━━━━━━━━━━━━━━ S3 DASHBOARD TAB ━━━━━━━━━━━━━━━━━━━ */
function S3DashTab({ s3 }) {
  if (!s3?.enabled) return <div className="tab-content"><div className="empty-card">Strategy 3 not running</div></div>;
  const st = s3.stats;
  return (
    <div className="tab-content">
      <div className="stats-row">
        <div className="sr-card"><span className="sr-big">{st.analyzed}</span><span className="sr-label">Analyzed</span></div>
        <div className="sr-card"><span className="sr-big">{st.trades}</span><span className="sr-label">Trades</span></div>
        <div className="sr-card"><span className="sr-big green">{st.wins}</span><span className="sr-label">Wins</span></div>
        <div className="sr-card"><span className="sr-big red">{st.losses}</span><span className="sr-label">Losses</span></div>
      </div>

      <div className="pnl-hero">
        <div className="pnl-main">
          <span className="pnl-label">Strategy 3 P&L</span>
          <span className={`pnl-value ${st.pnl >= 0 ? "green" : "red"}`}>{fPnl(st.pnl)}</span>
        </div>
        <div className="pnl-details">
          <div className="pnl-detail"><span className="pd-label">Win Rate</span><span>{st.win_rate}%</span></div>
          <div className="pnl-detail"><span className="pd-label">Skipped Choppy</span><span className="yellow">{st.skipped_choppy}</span></div>
          <div className="pnl-detail"><span className="pd-label">No Leader</span><span>{st.skipped_no_leader}</span></div>
          <div className="pnl-detail"><span className="pd-label">Trades</span><span>{st.trades}</span></div>
        </div>
        {st.last_action && <div className="pnl-action">{st.last_action}</div>}
      </div>

      {st.hourly_pnl && Object.keys(st.hourly_pnl).length > 0 && (
        <>
          <h3 className="section-title">📅 Hourly P&L (EST, resets daily)</h3>
          <div className="hourly-grid">
            {Object.entries(st.hourly_pnl).sort(([a],[b]) => a.localeCompare(b)).map(([hour, pnl]) => (
              <div key={hour} className={`hourly-card ${pnl > 0 ? "hourly-win" : pnl < 0 ? "hourly-loss" : "hourly-flat"}`}>
                <span className="hourly-time">{hour}</span>
                <span className={`hourly-val ${pnl > 0 ? "green" : pnl < 0 ? "red" : ""}`}>{fPnl(pnl)}</span>
              </div>
            ))}
          </div>
        </>
      )}

      <div className="rules-card">
        <div className="rule"><span className="rule-zone rule-wait">2:45→1:30</span><span className="rule-desc">Track highest Up & Down prices</span></div>
        <div className="rule"><span className="rule-zone rule-danger">SKIP</span><span className="rule-desc">Both sides hit $0.65+ → choppy, no trade</span></div>
        <div className="rule"><span className="rule-zone rule-profit">1:30 LEFT</span><span className="rule-desc">Buy whichever side is $0.70+ → hold to resolution</span></div>
      </div>

      <h3 className="section-title">Open Positions <span className="title-count">{s3.positions?.length || 0}</span></h3>
      {(!s3.positions || s3.positions.length === 0) ? (
        <div className="empty-card">No open positions</div>
      ) : s3.positions.map((p, i) => (
        <div key={i} className="pos-card">
          <div className="pos-top">
            <span className={`pos-side ${p.side === "Up" ? "side-up" : "side-down"}`}>{p.side === "Up" ? "▲" : "▼"} {p.side}</span>
            <span className="badge badge-s3">HOLD→RESOLVE</span>
            <span className="pos-age">{p.age}s</span>
          </div>
          <div className="pos-grid">
            <div className="pg"><span className="pg-label">Entry</span><span className="pg-val">${p.entry?.toFixed(3)}</span></div>
            <div className="pg"><span className="pg-label">Qty</span><span className="pg-val">{p.qty?.toFixed(1)}</span></div>
            <div className="pg"><span className="pg-label">Spent</span><span className="pg-val">${p.spent?.toFixed(2)}</span></div>
            <div className="pg"><span className="pg-label">Target</span><span className="pg-val green">$1.00</span></div>
          </div>
          <div className="pos-mkt">{p.market}</div>
        </div>
      ))}
    </div>
  );
}

function S3HistoryTab({ s3 }) {
  if (!s3?.enabled) return <div className="tab-content"><div className="empty-card">Strategy 3 not running</div></div>;
  const sorted = [...(s3.closed || [])].reverse();
  return (
    <div className="tab-content">
      <h3 className="section-title">S3 Trade History <span className="title-count">{sorted.length}</span></h3>
      {sorted.length === 0 ? (
        <div className="empty-card">No trades yet</div>
      ) : sorted.map((t, i) => {
        const isWin = t.pnl >= 0;
        return (
          <div key={i} className={`hist-card ${isWin ? "hist-win" : "hist-loss"}`}>
            <div className="hist-top">
              <span className={`hist-side ${t.side === "Up" ? "green" : "red"}`}>{t.side === "Up" ? "▲" : "▼"} {t.side}</span>
              <div className="hist-right">
                <span className={`hist-pnl ${isWin ? "green" : "red"}`}>{fPnl(t.pnl)}</span>
              </div>
            </div>
            <div className="hist-nums">
              <span>Entry ${t.entry?.toFixed(3)}</span>
              <span>Exit ${t.exit?.toFixed(3)}</span>
              <span>{t.status}</span>
            </div>
            <div className="hist-mkt">{t.market}</div>
          </div>
        );
      })}
    </div>
  );
}

/* ━━━━━━━━━━━━━━━━━━━ S2 DASHBOARD TAB ━━━━━━━━━━━━━━━━━━━ */
function S2DashTab({ s2 }) {
  if (!s2?.enabled) return <div className="tab-content"><div className="empty-card">Strategy 2 not running</div></div>;
  const st = s2.stats;
  const total = st.wins + st.losses;
  return (
    <div className="tab-content">
      {/* Stats */}
      <div className="stats-row">
        <div className="sr-card"><span className="sr-big">{st.bought}</span><span className="sr-label">Bought</span></div>
        <div className="sr-card"><span className="sr-big green">{st.sells_filled}</span><span className="sr-label">Sold @60c</span></div>
        <div className="sr-card"><span className="sr-big green">{st.wins}</span><span className="sr-label">Wins</span></div>
        <div className="sr-card"><span className="sr-big red">{st.losses}</span><span className="sr-label">Losses</span></div>
      </div>

      {/* P&L */}
      <div className="pnl-hero">
        <div className="pnl-main">
          <span className="pnl-label">Strategy 2 P&L</span>
          <span className={`pnl-value ${st.pnl >= 0 ? "green" : "red"}`}>{fPnl(st.pnl)}</span>
        </div>
        <div className="pnl-details">
          <div className="pnl-detail"><span className="pd-label">Win Rate</span><span>{st.win_rate}%</span></div>
          <div className="pnl-detail"><span className="pd-label">Bought</span><span>{st.bought}</span></div>
          <div className="pnl-detail"><span className="pd-label">Sold</span><span className="green">{st.sells_filled}</span></div>
          <div className="pnl-detail"><span className="pd-label">Total</span><span>{total}</span></div>
        </div>
        {st.last_action && <div className="pnl-action">{st.last_action}</div>}
      </div>

      {/* Hourly P&L */}
      {st.hourly_pnl && Object.keys(st.hourly_pnl).length > 0 && (
        <>
          <h3 className="section-title">📅 Hourly P&L (EST, resets daily)</h3>
          <div className="hourly-grid">
            {Object.entries(st.hourly_pnl).sort(([a],[b]) => a.localeCompare(b)).map(([hour, pnl]) => (
              <div key={hour} className={`hourly-card ${pnl > 0 ? "hourly-win" : pnl < 0 ? "hourly-loss" : "hourly-flat"}`}>
                <span className="hourly-time">{hour}</span>
                <span className={`hourly-val ${pnl > 0 ? "green" : pnl < 0 ? "red" : ""}`}>{fPnl(pnl)}</span>
              </div>
            ))}
          </div>
        </>
      )}

      {/* How it works */}
      <div className="rules-card">
        <div className="rule"><span className="rule-zone rule-profit">BUY</span><span className="rule-desc">Buy Up side of next 5 markets at $0.50-0.53</span></div>
        <div className="rule"><span className="rule-zone rule-moon">SELL</span><span className="rule-desc">Limit sell at $0.60 — sit and wait</span></div>
        <div className="rule"><span className="rule-zone rule-wait">HOLD</span><span className="rule-desc">If not filled, hold to resolution ($1 or $0)</span></div>
      </div>

      {/* Open positions */}
      <h3 className="section-title">Open Positions <span className="title-count">{s2.positions?.length || 0}</span></h3>
      {(!s2.positions || s2.positions.length === 0) ? (
        <div className="empty-card">No open positions</div>
      ) : s2.positions.map((p, i) => (
        <div key={i} className="pos-card">
          <div className="pos-top">
            <span className="pos-side side-up">▲ {p.side}</span>
            <span className="badge badge-s2">LIMIT @${p.sell_target}</span>
            <span className="pos-age">{p.age}s</span>
          </div>
          <div className="pos-grid">
            <div className="pg"><span className="pg-label">Entry</span><span className="pg-val">${p.entry?.toFixed(3)}</span></div>
            <div className="pg"><span className="pg-label">Target</span><span className="pg-val green">${p.sell_target?.toFixed(2)}</span></div>
            <div className="pg"><span className="pg-label">Qty</span><span className="pg-val">{p.qty?.toFixed(1)}</span></div>
            <div className="pg"><span className="pg-label">Spent</span><span className="pg-val">${p.spent?.toFixed(2)}</span></div>
          </div>
          <div className="pos-mkt">{p.market}</div>
        </div>
      ))}
    </div>
  );
}

/* ━━━━━━━━━━━━━━━━━━━ S2 HISTORY TAB ━━━━━━━━━━━━━━━━━━━ */
function S2HistoryTab({ s2 }) {
  if (!s2?.enabled) return <div className="tab-content"><div className="empty-card">Strategy 2 not running</div></div>;
  const sorted = [...(s2.closed || [])].reverse();
  return (
    <div className="tab-content">
      <h3 className="section-title">S2 Trade History <span className="title-count">{sorted.length}</span></h3>
      {sorted.length === 0 ? (
        <div className="empty-card">No trades yet</div>
      ) : sorted.map((t, i) => {
        const isWin = t.pnl >= 0;
        return (
          <div key={i} className={`hist-card ${isWin ? "hist-win" : "hist-loss"}`}>
            <div className="hist-top">
              <span className="hist-side green">▲ {t.side}</span>
              <div className="hist-right">
                <span className={`hist-pct ${isWin ? "green" : "red"}`}>{t.pnl_pct != null ? `${t.pnl_pct >= 0 ? "+" : ""}${t.pnl_pct}%` : ""}</span>
                <span className={`hist-pnl ${isWin ? "green" : "red"}`}>{fPnl(t.pnl)}</span>
              </div>
            </div>
            <div className="hist-nums">
              <span>Entry ${t.entry?.toFixed(3)}</span>
              <span>Exit ${t.exit?.toFixed(3)}</span>
              <span>{t.status}</span>
            </div>
            <div className="hist-mkt">{t.market}</div>
          </div>
        );
      })}
    </div>
  );
}

function ConfigRow({ label, value, sub, cls }) {
  return (
    <div className="cfg-row">
      <span className="cfg-label">{label}</span>
      <div className="cfg-right">
        <span className={`cfg-val ${cls || ""}`}>{value}</span>
        {sub && <span className="cfg-sub">{sub}</span>}
      </div>
    </div>
  );
}

export default App;
