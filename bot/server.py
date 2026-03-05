"""
S3 Dashboard — serves a clean web UI with live WebSocket updates.
Includes USDC balance, PnL calendar, positions, and trades.
"""

import asyncio
import json
import logging
import time
from typing import Set, Optional

from aiohttp import web

from bot.config import cfg

log = logging.getLogger("server")

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>S3 Dashboard</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:#0a0a0f;color:#e0e0e0;font-family:'Segoe UI',system-ui,-apple-system,sans-serif;min-height:100vh}
  .top-bar{background:#111118;border-bottom:1px solid #1e1e2e;padding:14px 24px;display:flex;align-items:center;justify-content:space-between}
  .top-bar h1{font-size:17px;font-weight:600;color:#fff}
  .top-bar h1 span{color:#818cf8;margin-right:6px}
  .pills{display:flex;gap:8px;align-items:center}
  .pill{padding:3px 10px;border-radius:16px;font-size:11px;font-weight:600;letter-spacing:0.5px}
  .pill.live{background:rgba(34,197,94,0.12);color:#22c55e;border:1px solid rgba(34,197,94,0.25)}
  .pill.dry{background:rgba(234,179,8,0.12);color:#eab308;border:1px solid rgba(234,179,8,0.25)}
  .pill.hours{background:rgba(129,140,248,0.1);color:#818cf8;border:1px solid rgba(129,140,248,0.2)}
  .pill.bal{background:rgba(34,197,94,0.08);color:#22c55e;border:1px solid rgba(34,197,94,0.15);font-size:13px;font-weight:700}
  .container{max-width:1200px;margin:0 auto;padding:16px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}
  .stat{background:#111118;border:1px solid #1e1e2e;border-radius:10px;padding:14px}
  .stat .label{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#555;margin-bottom:4px}
  .stat .val{font-size:22px;font-weight:700}
  .green{color:#22c55e}.red{color:#ef4444}.blue{color:#818cf8}.yellow{color:#eab308}.dim{color:#666}
  .panel{background:#111118;border:1px solid #1e1e2e;border-radius:10px;margin-bottom:16px;overflow:hidden}
  .panel-hd{padding:12px 16px;border-bottom:1px solid #1e1e2e;font-size:12px;font-weight:600;
            text-transform:uppercase;letter-spacing:0.8px;color:#777}
  table{width:100%;border-collapse:collapse}
  th{text-align:left;padding:8px 14px;font-size:10px;text-transform:uppercase;letter-spacing:1px;
     color:#444;border-bottom:1px solid #161622}
  td{padding:8px 14px;font-size:12px;border-bottom:1px solid #0d0d14}
  tr:last-child td{border-bottom:none}
  tr:hover{background:rgba(255,255,255,0.015)}
  .pnl-p{color:#22c55e;font-weight:600}
  .pnl-n{color:#ef4444;font-weight:600}
  .badge{display:inline-block;padding:2px 7px;border-radius:5px;font-size:10px;font-weight:700}
  .b-up{background:rgba(34,197,94,0.12);color:#22c55e}
  .b-down{background:rgba(239,68,68,0.12);color:#ef4444}
  .b-tp{background:rgba(34,197,94,0.15);color:#22c55e}
  .b-sl{background:rgba(239,68,68,0.15);color:#ef4444}
  .b-res{background:rgba(100,100,120,0.12);color:#888}
  .empty{padding:24px;text-align:center;color:#333;font-size:12px}
  .last{background:#111118;border:1px solid #1e1e2e;border-radius:10px;padding:12px 16px;
        margin-bottom:16px;font-size:12px;color:#999}
  .last strong{color:#818cf8}
  .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;animation:pulse 2s ease infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
  /* Calendar */
  .cal-months{display:flex;flex-wrap:wrap;gap:6px;padding:14px}
  .cal-month{padding:8px 14px;border-radius:8px;background:#0d0d14;border:1px solid #161622;
             cursor:pointer;font-size:12px;font-weight:600;color:#555;transition:all 0.15s}
  .cal-month:hover{border-color:#818cf8;color:#818cf8}
  .cal-month.active{background:rgba(129,140,248,0.1);border-color:#818cf8;color:#818cf8}
  .cal-days{display:grid;grid-template-columns:repeat(7,1fr);gap:4px;padding:0 14px 14px}
  .cal-day-hd{text-align:center;font-size:10px;color:#333;padding:4px;text-transform:uppercase}
  .cal-day{text-align:center;padding:8px 2px;border-radius:6px;cursor:pointer;transition:all 0.12s;
           background:#0d0d14;border:1px solid transparent;min-height:48px}
  .cal-day:hover{border-color:#333}
  .cal-day.active{border-color:#818cf8}
  .cal-day .d{font-size:11px;color:#444;margin-bottom:2px}
  .cal-day .v{font-size:12px;font-weight:700}
  .cal-day.empty-day{background:transparent;cursor:default}
  .cal-detail{padding:14px}
  .cal-detail h3{font-size:13px;color:#888;margin-bottom:10px;font-weight:600}
  .hourly-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(75px,1fr));gap:6px}
  .hcell{text-align:center;padding:6px;border-radius:6px;background:#0d0d14}
  .hcell .hr{font-size:10px;color:#444;margin-bottom:2px}
  .hcell .hv{font-size:13px;font-weight:700}
  .cal-summary{padding:8px 14px;font-size:12px;color:#555;display:flex;gap:16px}
</style>
</head>
<body>
<div class="top-bar">
  <h1><span>&#9670;</span> S3 Dashboard</h1>
  <div class="pills">
    <span id="balance" class="pill bal">--</span>
    <span id="mode" class="pill dry">DRY RUN</span>
    <span id="hours" class="pill hours" style="display:none"></span>
  </div>
</div>
<div class="container">
  <div class="last" id="lastAction"><strong>Last:</strong> waiting...</div>
  <div class="grid" id="stats"></div>
  <div class="panel"><div class="panel-hd">Open Positions</div><div id="openPos"><div class="empty">No open positions</div></div></div>
  <div class="panel"><div class="panel-hd">Recent Trades</div><div id="closedTrades"><div class="empty">No trades yet</div></div></div>
  <div class="panel">
    <div class="panel-hd">P&amp;L Calendar</div>
    <div id="calendar"><div class="empty">Loading...</div></div>
  </div>
</div>
<script>
let calData={}, selectedMonth=null, selectedDay=null;
const MONTHS=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

const ws=new WebSocket(`ws://${location.host}/ws`);
ws.onmessage=(e)=>{const d=JSON.parse(e.data);render(d)};
ws.onclose=()=>setTimeout(()=>location.reload(),3000);

// Load calendar data on start and every 60s
function loadCal(){
  fetch('/api/calendar').then(r=>r.json()).then(d=>{calData=d;renderCal()}).catch(()=>{});
}
loadCal();
setInterval(loadCal,60000);

function render(d){
  const m=document.getElementById('mode');
  if(d.dry_run){m.className='pill dry';m.textContent='DRY RUN'}
  else{m.className='pill live';m.innerHTML='<span class="dot" style="background:#22c55e"></span>LIVE'}
  if(d.trade_hours){const h=document.getElementById('hours');h.style.display='';h.textContent=d.trade_hours}
  if(d.balance!==null&&d.balance!==undefined){
    document.getElementById('balance').textContent='USDC $'+parseFloat(d.balance).toFixed(2);
  }
  const s=d.stats;
  const pc=s.pnl>=0?'green':'red';
  document.getElementById('stats').innerHTML=`
    <div class="stat"><div class="label">Total P&L</div><div class="val ${pc}">$${s.pnl.toFixed(2)}</div></div>
    <div class="stat"><div class="label">Win Rate</div><div class="val blue">${s.win_rate}%</div></div>
    <div class="stat"><div class="label">W / L</div><div class="val"><span class="green">${s.wins}</span> / <span class="red">${s.losses}</span></div></div>
    <div class="stat"><div class="label">Trades</div><div class="val blue">${s.trades}</div></div>
    <div class="stat"><div class="label">TP / SL</div><div class="val"><span class="green">${s.tp_hits}</span> / <span class="red">${s.sl_hits}</span></div></div>
    <div class="stat"><div class="label">Analyzed</div><div class="val yellow">${s.analyzed}</div></div>
    <div class="stat"><div class="label">Choppy</div><div class="val dim">${s.skipped_choppy}</div></div>
    <div class="stat"><div class="label">No Leader</div><div class="val dim">${s.skipped_no_leader}</div></div>`;
  document.getElementById('lastAction').innerHTML=`<strong>Last:</strong> ${s.last_action||'waiting...'}`;
  renderOpen(d.positions);
  renderClosed(d.closed);
}

function renderOpen(pos){
  const el=document.getElementById('openPos');
  if(!pos.length){el.innerHTML='<div class="empty">No open positions</div>';return}
  let h='<table><tr><th>Side</th><th>Entry</th><th>Qty</th><th>Spent</th><th>Age</th><th>Market</th></tr>';
  for(const p of pos){
    const b=p.side==='Up'?'b-up':'b-down';
    h+=`<tr><td><span class="badge ${b}">${p.side}</span></td><td>$${p.entry.toFixed(3)}</td>
    <td>${p.qty.toFixed(1)}</td><td>$${p.spent.toFixed(2)}</td><td>${p.age}s</td>
    <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${p.market}</td></tr>`}
  el.innerHTML=h+'</table>'
}

function renderClosed(trades){
  const el=document.getElementById('closedTrades');
  if(!trades.length){el.innerHTML='<div class="empty">No trades yet</div>';return}
  let h='<table><tr><th>Side</th><th>Entry</th><th>Exit</th><th>Qty</th><th>P&L</th><th>Exit</th><th>Market</th></tr>';
  for(const t of trades.slice().reverse()){
    const b=t.side==='Up'?'b-up':'b-down';
    const pc=(t.pnl||0)>=0?'pnl-p':'pnl-n';
    const pnl=t.pnl!==null?`$${t.pnl.toFixed(2)}`:'--';
    const ex=t.exit_price!==null?`$${t.exit_price.toFixed(3)}`:'--';
    const reason=t.exit_reason||t.status||'';
    const rb=reason.includes('tp')?'b-tp':reason.includes('sl')?'b-sl':'b-res';
    const rl=reason.replace('resolved-','').toUpperCase();
    h+=`<tr><td><span class="badge ${b}">${t.side}</span></td><td>$${t.entry.toFixed(3)}</td>
    <td>${ex}</td><td>${t.qty.toFixed(1)}</td><td class="${pc}">${pnl}</td>
    <td><span class="badge ${rb}">${rl}</span></td>
    <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${t.market}</td></tr>`}
  el.innerHTML=h+'</table>'
}

function renderCal(){
  const el=document.getElementById('calendar');
  const now=new Date();
  const curYear=2026;
  if(!selectedMonth) selectedMonth=now.getMonth();
  let h='<div class="cal-months">';
  for(let i=0;i<12;i++){
    const cls=i===selectedMonth?'cal-month active':'cal-month';
    h+=`<div class="${cls}" onclick="selMonth(${i})">${MONTHS[i]}</div>`;
  }
  h+='</div>';
  // Day headers
  h+='<div class="cal-days">';
  ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'].forEach(d=>h+=`<div class="cal-day-hd">${d}</div>`);
  // Days
  const firstDay=new Date(curYear,selectedMonth,1).getDay();
  const daysInMonth=new Date(curYear,selectedMonth+1,0).getDate();
  let monthTotal=0, monthTrades=0, monthWins=0, monthLosses=0;
  for(let i=0;i<firstDay;i++) h+='<div class="cal-day empty-day"></div>';
  for(let d=1;d<=daysInMonth;d++){
    const key=`${curYear}-${String(selectedMonth+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
    const info=calData[key];
    const active=key===selectedDay?'active':'';
    if(info){
      monthTotal+=info.total; monthTrades+=info.trades; monthWins+=info.wins; monthLosses+=info.losses;
      const c=info.total>=0?'pnl-p':'pnl-n';
      h+=`<div class="cal-day ${active}" onclick="selDay('${key}')"><div class="d">${d}</div><div class="v ${c}">$${info.total.toFixed(0)}</div></div>`;
    } else {
      h+=`<div class="cal-day ${active}" onclick="selDay('${key}')"><div class="d">${d}</div><div class="v dim">-</div></div>`;
    }
  }
  h+='</div>';
  // Month summary
  const mc=monthTotal>=0?'green':'red';
  h+=`<div class="cal-summary"><span>Month: <strong class="${mc}">$${monthTotal.toFixed(2)}</strong></span>
      <span>Trades: ${monthTrades}</span><span class="green">W:${monthWins}</span><span class="red">L:${monthLosses}</span></div>`;
  // Day detail
  if(selectedDay && calData[selectedDay]){
    const dayInfo=calData[selectedDay];
    h+=`<div class="cal-detail"><h3>${selectedDay} &mdash; $${dayInfo.total.toFixed(2)} (${dayInfo.trades} trades, W:${dayInfo.wins} L:${dayInfo.losses})</h3>`;
    h+='<div class="hourly-grid">';
    for(let hr=0;hr<24;hr++){
      const hk=String(hr).padStart(2,'0');
      const val=dayInfo.hours?.[hk];
      if(val!==undefined){
        const c=val>=0?'pnl-p':'pnl-n';
        h+=`<div class="hcell"><div class="hr">${hk}:00</div><div class="hv ${c}">$${val.toFixed(2)}</div></div>`;
      } else {
        h+=`<div class="hcell"><div class="hr">${hk}:00</div><div class="hv dim">-</div></div>`;
      }
    }
    h+='</div></div>';
  }
  el.innerHTML=h;
}

function selMonth(m){selectedMonth=m;selectedDay=null;renderCal()}
function selDay(d){selectedDay=d;renderCal()}
</script>
</body>
</html>"""


class BalanceChecker:
    def __init__(self):
        self._balance: Optional[float] = None
        self._last_check: float = 0

    @property
    def balance(self) -> Optional[float]:
        return self._balance

    def check(self):
        now = time.time()
        if now - self._last_check < 30:
            return
        self._last_check = now
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams
            creds = ApiCreds(
                api_key=cfg.poly_api_key,
                api_secret=cfg.poly_api_secret,
                api_passphrase=cfg.poly_api_passphrase,
            )
            client = ClobClient(
                cfg.poly_clob_host,
                key=cfg.poly_private_key,
                chain_id=cfg.chain_id,
                creds=creds,
                signature_type=1,
            )
            params = BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=1)
            result = client.get_balance_allowance(params)
            raw = float(result.get("balance", 0))
            self._balance = raw / 1_000_000
        except Exception as exc:
            log.warning("Balance check failed: %s", exc)


class DashboardServer:

    def __init__(self, strategy3, pnl_store=None, host="0.0.0.0", port=9001):
        self._strat3 = strategy3
        self._pnl_store = pnl_store
        self._host = host
        self._port = port
        self._clients: Set[web.WebSocketResponse] = set()
        self._start_time = time.time()
        self._balance = BalanceChecker()

        self._app = web.Application()
        self._app.router.add_get("/", self._index_handler)
        self._app.router.add_get("/ws", self._ws_handler)
        self._app.router.add_get("/api/state", self._state_handler)
        self._app.router.add_get("/api/calendar", self._calendar_handler)

    def _build_state(self) -> dict:
        s3 = self._strat3
        st = s3.stats
        now = time.time()

        positions = []
        for p in s3.open_positions:
            positions.append({
                "side": p.side, "entry": p.entry_price, "qty": p.qty,
                "spent": p.spent, "age": round(now - p.entry_time),
                "market": p.market.question, "status": p.status,
            })

        closed = []
        for p in s3.closed_positions[-30:]:
            closed.append({
                "side": p.side, "entry": p.entry_price,
                "exit_price": p.exit_price, "qty": p.qty, "spent": p.spent,
                "pnl": round(p.pnl, 2) if p.pnl is not None else None,
                "market": p.market.question, "status": p.status,
                "exit_reason": p.exit_reason,
            })

        total = st.wins + st.losses
        trade_hours = ""
        if s3._trade_hours:
            sh, sm, eh, em = s3._trade_hours
            trade_hours = f"{sh:02d}:{sm:02d} - {eh:02d}:{em:02d} EST"

        self._balance.check()

        return {
            "ts": now,
            "uptime": round(now - self._start_time),
            "dry_run": cfg.dry_run,
            "trade_hours": trade_hours,
            "balance": self._balance.balance,
            "stats": {
                "analyzed": st.markets_analyzed,
                "trades": st.trades,
                "skipped_choppy": st.skipped_choppy,
                "skipped_no_leader": st.skipped_no_leader,
                "tp_hits": st.tp_hits,
                "sl_hits": st.sl_hits,
                "wins": st.wins,
                "losses": st.losses,
                "pnl": round(st.total_pnl, 2),
                "win_rate": round((st.wins / total) * 100, 1) if total > 0 else 0,
                "last_action": st.last_action,
                "hourly_pnl": dict(st.hourly_pnl),
            },
            "positions": positions,
            "closed": closed,
        }

    async def _index_handler(self, request):
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    async def _ws_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        log.info("Dashboard client connected (%d total)", len(self._clients))
        try:
            async for msg in ws:
                pass
        finally:
            self._clients.discard(ws)
        return ws

    async def _state_handler(self, request):
        return web.json_response(self._build_state())

    async def _calendar_handler(self, request):
        if self._pnl_store:
            return web.json_response(self._pnl_store.get_all())
        return web.json_response({})

    async def _broadcast_loop(self):
        while True:
            if self._clients:
                state = json.dumps(self._build_state())
                dead = set()
                for ws in self._clients:
                    try:
                        await ws.send_str(state)
                    except Exception:
                        dead.add(ws)
                self._clients -= dead
            await asyncio.sleep(1)

    async def run(self):
        runner = web.AppRunner(self._app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        log.info("Dashboard running at http://%s:%d", self._host, self._port)
        await self._broadcast_loop()
