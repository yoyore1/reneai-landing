#!/usr/bin/env python3
"""
Launcher — web UI on port 9000 to manage Test + Official S3 bots.
  Test bot:     dry run on port 9001
  Official bot: live trading on port 9002 (12:20 AM – 7:00 AM EST)
"""

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Optional

from aiohttp import web

from bot.config import cfg

log = logging.getLogger("launcher")

LAUNCHER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>S3 Bot Launcher</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:#0a0a0f;color:#e0e0e0;font-family:'Segoe UI',system-ui,-apple-system,sans-serif;
       min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px;gap:20px}
  .balance-bar{background:#111118;border:1px solid #1e1e2e;border-radius:14px;padding:16px 32px;
               display:flex;align-items:center;gap:16px;box-shadow:0 10px 40px rgba(0,0,0,0.3)}
  .balance-bar .lbl{font-size:12px;color:#555;text-transform:uppercase;letter-spacing:1px}
  .balance-bar .amt{font-size:28px;font-weight:700;color:#22c55e}
  .balance-bar .wallet{font-size:10px;color:#333;font-family:monospace}
  .wrapper{display:flex;gap:20px;flex-wrap:wrap;justify-content:center;align-items:flex-start}
  .card{background:#111118;border:1px solid #1e1e2e;border-radius:20px;padding:36px;
        width:380px;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,0.5)}
  .card.official{border-color:#22c55e20}
  .logo{font-size:40px;margin-bottom:8px}
  h2{font-size:18px;font-weight:700;color:#fff;margin-bottom:2px}
  .subtitle{font-size:12px;color:#555;margin-bottom:24px}
  .tag{display:inline-block;padding:3px 10px;border-radius:8px;font-size:11px;font-weight:700;
       letter-spacing:0.8px;margin-bottom:20px}
  .tag.test{background:rgba(234,179,8,0.12);color:#eab308;border:1px solid rgba(234,179,8,0.2)}
  .tag.live{background:rgba(34,197,94,0.12);color:#22c55e;border:1px solid rgba(34,197,94,0.2)}
  .status-box{padding:12px;border-radius:10px;margin-bottom:20px;font-size:13px;font-weight:600;
              display:flex;align-items:center;justify-content:center;gap:8px}
  .status-box.stopped{background:rgba(100,100,120,0.08);border:1px solid #1e1e2e;color:#666}
  .status-box.running{background:rgba(34,197,94,0.08);border:1px solid rgba(34,197,94,0.2);color:#22c55e}
  .dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
  .dot.off{background:#444}
  .dot.on{background:#22c55e;animation:pulse 2s ease infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
  .actions{display:flex;flex-direction:column;gap:8px}
  button{padding:12px;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;
         border:none;transition:all 0.15s;letter-spacing:0.3px;width:100%}
  button:active{transform:scale(0.97)}
  .btn-start{background:#22c55e;color:#000}
  .btn-start:hover{background:#16a34a}
  .btn-start:disabled{background:#1a3a28;color:#444;cursor:not-allowed}
  .btn-stop{background:rgba(239,68,68,0.12);color:#ef4444;border:1px solid rgba(239,68,68,0.25)}
  .btn-stop:hover{background:rgba(239,68,68,0.22)}
  .btn-stop:disabled{background:#131015;color:#333;cursor:not-allowed;border-color:#1a1a1a}
  .btn-dash{background:rgba(129,140,248,0.1);color:#818cf8;border:1px solid rgba(129,140,248,0.25);
            text-decoration:none;display:block;padding:12px;border-radius:10px;font-size:13px;
            font-weight:600;letter-spacing:0.3px;transition:all 0.15s;text-align:center}
  .btn-dash:hover{background:rgba(129,140,248,0.2)}
  .btn-dash.hidden{display:none}
  .meta{margin-top:12px;font-size:11px;color:#333}
  .log-box{margin-top:14px;text-align:left;background:#0a0a0f;border:1px solid #151520;
           border-radius:8px;padding:10px;max-height:120px;overflow-y:auto;font-size:10px;
           font-family:'Cascadia Code','Fira Code',monospace;color:#444;line-height:1.5}
  .log-box .l{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .log-box .i{color:#6366f1}
  .log-box .e{color:#ef4444}
  .log-box .w{color:#eab308}
</style>
</head>
<body>
<div class="balance-bar">
  <div><div class="lbl">USDC Balance</div><div class="amt" id="balance">--</div></div>
  <div class="wallet" id="wallet"></div>
</div>
<div class="wrapper">
  <!-- TEST BOT -->
  <div class="card">
    <div class="logo">&#9671;</div>
    <h2>S3 Test Bot</h2>
    <div class="subtitle">Dry run &middot; Port 9001</div>
    <div class="tag test">DRY RUN</div>
    <div id="status-test" class="status-box stopped">
      <span class="dot off" id="dot-test"></span>
      <span id="txt-test">Stopped</span>
    </div>
    <div class="actions">
      <button class="btn-start" id="start-test" onclick="api('test','start')">Start Test</button>
      <button class="btn-stop" id="stop-test" onclick="api('test','stop')" disabled>Stop Test</button>
      <a class="btn-dash hidden" id="dash-test" href="" target="_blank">Open Dashboard &#8594;</a>
    </div>
    <div class="meta" id="meta-test"></div>
    <div class="log-box" id="log-test"></div>
  </div>

  <!-- OFFICIAL BOT -->
  <div class="card official">
    <div class="logo">&#9670;</div>
    <h2>S3 Official Bot</h2>
    <div class="subtitle">Live trading &middot; 12:20 AM – 7:00 AM EST &middot; Port 9002</div>
    <div class="tag live">LIVE MONEY</div>
    <div id="status-official" class="status-box stopped">
      <span class="dot off" id="dot-official"></span>
      <span id="txt-official">Stopped</span>
    </div>
    <div class="actions">
      <button class="btn-start" id="start-official" onclick="api('official','start')">Start Official</button>
      <button class="btn-stop" id="stop-official" onclick="api('official','stop')" disabled>Stop Official</button>
      <a class="btn-dash hidden" id="dash-official" href="" target="_blank">Open Dashboard &#8594;</a>
    </div>
    <div class="meta" id="meta-official"></div>
    <div class="log-box" id="log-official"></div>
  </div>
</div>
<script>
function poll() {
  fetch('/api/status').then(r=>r.json()).then(d => {
    render('test', d.test);
    render('official', d.official);
    if(d.balance!==undefined){
      document.getElementById('balance').textContent='$'+parseFloat(d.balance).toFixed(2);
      document.getElementById('balance').style.color=parseFloat(d.balance)>0?'#22c55e':'#ef4444';
    }
    if(d.wallet) document.getElementById('wallet').textContent=d.wallet;
  }).catch(()=>{});
}

function render(id, s) {
  const box = document.getElementById('status-'+id);
  const dot = document.getElementById('dot-'+id);
  const txt = document.getElementById('txt-'+id);
  const startBtn = document.getElementById('start-'+id);
  const stopBtn = document.getElementById('stop-'+id);
  const dash = document.getElementById('dash-'+id);
  const meta = document.getElementById('meta-'+id);

  if (s.running) {
    box.className='status-box running'; dot.className='dot on'; txt.textContent='Running';
    startBtn.disabled=true; stopBtn.disabled=false;
    dash.classList.remove('hidden');
    dash.href=`http://${location.hostname}:${s.port}`;
    const m=Math.floor(s.uptime/60), sec=s.uptime%60;
    meta.textContent=`Uptime: ${m}m ${sec}s`;
  } else {
    box.className='status-box stopped'; dot.className='dot off'; txt.textContent='Stopped';
    startBtn.disabled=false; stopBtn.disabled=true;
    dash.classList.add('hidden');
    meta.textContent='';
  }
  renderLogs(id, s.logs||[]);
}

function renderLogs(id, logs) {
  const box=document.getElementById('log-'+id);
  let h='';
  for(const l of logs.slice(-12)){
    const c=l.includes('ERROR')?'e':l.includes('WARN')?'w':l.includes('INFO')?'i':'';
    h+=`<div class="l ${c}">${esc(l)}</div>`;
  }
  box.innerHTML=h;
  box.scrollTop=box.scrollHeight;
}

function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}

function api(bot, action) {
  document.getElementById((action==='start'?'start':'stop')+'-'+bot).disabled=true;
  fetch(`/api/${action}/${bot}`,{method:'POST'}).then(()=>setTimeout(poll,500));
}

poll();
setInterval(poll,2000);
</script>
</body>
</html>"""


class BotProcess:
    """Manages an S3 bot subprocess."""

    def __init__(self, name: str, cmd_args: list, port: int):
        self.name = name
        self.port = port
        self._cmd_args = cmd_args
        self._proc: Optional[subprocess.Popen] = None
        self._start_time: float = 0
        self._logs: list = []
        self._max_logs = 200
        self._reader_thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def uptime(self) -> int:
        if not self.running:
            return 0
        return int(time.time() - self._start_time)

    def start(self):
        if self.running:
            return
        self._logs.clear()
        self._start_time = time.time()
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "bot.main"] + self._cmd_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        self._reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self._reader_thread.start()
        log.info("[%s] Started (pid=%d)", self.name, self._proc.pid)

    def _read_output(self):
        try:
            for line in self._proc.stdout:
                self._logs.append(line.rstrip())
                if len(self._logs) > self._max_logs:
                    self._logs = self._logs[-self._max_logs:]
        except Exception:
            pass

    def stop(self):
        if not self.running:
            return
        pid = self._proc.pid
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None
        log.info("[%s] Stopped (pid=%d)", self.name, pid)

    def status_dict(self) -> dict:
        return {
            "running": self.running,
            "uptime": self.uptime,
            "port": self.port,
            "logs": self._logs[-30:],
        }


class BalanceChecker:
    """Fetches USDC balance from Polymarket CLOB periodically."""

    def __init__(self):
        self._balance: Optional[float] = None
        self._wallet = os.getenv("POLY_FUNDER_ADDRESS", "")
        self._last_check: float = 0
        self._check_interval = 30  # seconds

    @property
    def balance(self) -> Optional[float]:
        return self._balance

    @property
    def wallet(self) -> str:
        return self._wallet

    def check(self):
        now = time.time()
        if now - self._last_check < self._check_interval:
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
            self._balance = raw / 1_000_000  # USDC has 6 decimals
        except Exception as exc:
            log.warning("Balance check failed: %s", exc)


class LauncherServer:

    def __init__(self, host="0.0.0.0", port=9000):
        self._host = host
        self._port = port
        self._balance_checker = BalanceChecker()
        self._bots = {
            "test": BotProcess("test", ["--port", "9001", "--pnl-file", "pnl_test.json"], port=9001),
            "official": BotProcess(
                "official",
                ["--port", "9002", "--live", "--trade-start", "00:20", "--trade-end", "07:00",
                 "--pnl-file", "pnl_official.json"],
                port=9002,
            ),
        }
        self._app = web.Application()
        self._app.router.add_get("/", self._index)
        self._app.router.add_get("/api/status", self._status)
        self._app.router.add_post("/api/start/{bot}", self._start)
        self._app.router.add_post("/api/stop/{bot}", self._stop)

    async def _index(self, request):
        return web.Response(text=LAUNCHER_HTML, content_type="text/html")

    async def _status(self, request):
        self._balance_checker.check()
        data = {name: bot.status_dict() for name, bot in self._bots.items()}
        data["balance"] = self._balance_checker.balance
        data["wallet"] = self._balance_checker.wallet
        return web.json_response(data)

    async def _start(self, request):
        name = request.match_info["bot"]
        bot = self._bots.get(name)
        if bot:
            bot.start()
        return web.json_response({"ok": True})

    async def _stop(self, request):
        name = request.match_info["bot"]
        bot = self._bots.get(name)
        if bot:
            bot.stop()
        return web.json_response({"ok": True})

    async def run(self):
        runner = web.AppRunner(self._app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        log.info("Launcher running at http://%s:%d", self._host, self._port)

        while True:
            await asyncio.sleep(1)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    log.info("Starting S3 Launcher on port 9000...")
    asyncio.run(LauncherServer().run())


if __name__ == "__main__":
    main()
