#!/usr/bin/env python3
"""
Launcher: Start/Stop the bot from a web UI.
Runs on port 8900. Start the launcher once, then use it to control the bot.

  python -m bot.launcher

When bot is stopped, dashboard (8899) is down. Use launcher (8900) to Start it.
"""
import os
import subprocess
import sys
from pathlib import Path

from aiohttp import web

PORT = 8900
BOT_PORT = 8899
BOT_DIR = Path(__file__).resolve().parent.parent
BOT_CMD = [sys.executable, "-m", "bot.main", "--headless", "--s3-only"]

LAUNCHER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bot Control</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:linear-gradient(165deg,#0a0e14 0%,#141b24 100%);color:#e6edf3;min-height:100vh;padding:2rem;display:flex;align-items:center;justify-content:center}
.card{background:rgba(22,27,34,0.9);border:1px solid #30363d;border-radius:12px;padding:2rem;text-align:center;min-width:280px}
h1{font-size:1.3rem;margin-bottom:0.5rem}
.sub{color:#8b949e;font-size:0.85rem;margin-bottom:1.5rem}
.btns{display:flex;gap:1rem;justify-content:center;flex-wrap:wrap}
.btn{padding:0.75rem 1.5rem;border:none;border-radius:8px;font-size:1rem;cursor:pointer;font-weight:600}
.btn-start{background:#3fb950;color:#fff}
.btn-start:hover{background:#2ea043}
.btn-stop{background:#f85149;color:#fff}
.btn-stop:hover{background:#da3633}
.btn:disabled{opacity:0.5;cursor:not-allowed}
.dash{display:block;margin-top:1.5rem;color:#58a6ff;text-decoration:none;font-size:0.9rem}
.dash:hover{text-decoration:underline}
.status{font-size:0.8rem;color:#8b949e;margin-top:1rem}
</style>
</head>
<body>
<div class="card">
<h1>S3 Late Bot</h1>
<p class="sub">Start or stop the bot</p>
<div class="btns">
<button id="startBtn" class="btn btn-start">Start Bot</button>
<button id="stopBtn" class="btn btn-stop">Stop Bot</button>
</div>
<p class="status" id="status"></p>
<div style="margin-top:1.5rem;display:flex;flex-direction:column;gap:0.5rem;align-items:center">
<a class="dash" id="dashLink" href="#" target="_blank">Open Dashboard →</a>
<a class="dash" href="/api/logs" target="_blank">View Logs</a>
</div>
</div>
<script>
const BOT_PORT = """ + str(BOT_PORT) + """;
document.getElementById('dashLink').href = (location.protocol === 'https:' ? 'https:' : 'http:') + '//' + location.hostname + ':' + BOT_PORT;

async function pollStatus(){
 const ok = await fetch('/api/status').then(r=>r.json());
 const running = ok.bot_running;
 document.getElementById('startBtn').disabled = running;
 document.getElementById('stopBtn').disabled = !running;
 document.getElementById('status').textContent = running ? 'Bot is running' : 'Bot is stopped';
 if(running) document.querySelector('.dash').style.display = 'block';
}

document.getElementById('startBtn').onclick = async function(){
 this.disabled = true;
 document.getElementById('status').textContent = 'Starting...';
 const r = await fetch('/api/start', {method:'POST'});
 const j = await r.json();
 document.getElementById('status').textContent = j.ok ? 'Started' : (j.error||'Failed');
 setTimeout(pollStatus, 2000);
};

document.getElementById('stopBtn').onclick = async function(){
 this.disabled = true;
 document.getElementById('status').textContent = 'Stopping...';
 await fetch('/api/stop', {method:'POST'});
 document.getElementById('status').textContent = 'Stopped';
 setTimeout(pollStatus, 1000);
};

pollStatus();
setInterval(pollStatus, 5000);
</script>
</body>
</html>
"""


def _bot_running() -> bool:
    try:
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{BOT_PORT}/api/state")
        urllib.request.urlopen(req, timeout=2)
        return True
    except Exception:
        return False


def _start_bot() -> "tuple[bool, str]":
    os.chdir(BOT_DIR)
    # Load .env and pass key vars to child so DRY_RUN=false is guaranteed
    env = os.environ.copy()
    env_path = BOT_DIR / ".env"
    if env_path.exists():
        for line in open(env_path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k in ("DRY_RUN", "POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE", "POLY_PRIVATE_KEY", "POLY_FUNDER_ADDRESS", "POLY_SIGNATURE_TYPE", "S3_TRADE_START_MINUTE_EST", "S3_USDC_PER_TRADE"):
                    env[k] = v
    env.setdefault("DRY_RUN", "false")  # force live if not set
    log_file = BOT_DIR / "bot.log"
    cmd = ["screen", "-dmS", "bot", "bash", "-c", f"cd {BOT_DIR} && {sys.executable} -m bot.main --headless --s3-only --live >> {log_file} 2>&1"]
    try:
        subprocess.run(
            cmd,
            cwd=str(BOT_DIR),
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
        return True, "Started"
    except FileNotFoundError:
        # No screen, try nohup
        try:
            with open(BOT_DIR / "bot.log", "a") as f:
                subprocess.Popen(
                    BOT_CMD,
                    cwd=str(BOT_DIR),
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            return True, "Started (no screen)"
        except Exception as e:
            return False, str(e)
    except Exception as e:
        return False, str(e)


def _stop_bot() -> "tuple[bool, str]":
    # Kill screen session (and the bot inside it) - most reliable
    try:
        subprocess.run(
            ["screen", "-S", "bot", "-X", "quit"],
            capture_output=True,
            timeout=3,
        )
        return True, "Stopped"
    except Exception:
        pass
    # Fallback: kill the process directly
    try:
        subprocess.run(["pkill", "-f", "bot.main"], capture_output=True, timeout=3)
        return True, "Stopped"
    except Exception as e:
        return False, str(e)


async def status_handler(request):
    return web.json_response({"bot_running": _bot_running()})


async def logs_handler(request):
    """Serve last 500 lines of bot.log as plain text for viewing in browser."""
    log_path = BOT_DIR / "bot.log"
    if not log_path.exists():
        return web.Response(text="No log file yet. Start the bot first.", content_type="text/plain")
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        last = lines[-500:] if len(lines) > 500 else lines
        body = "".join(last)
        return web.Response(text=body, content_type="text/plain; charset=utf-8")
    except Exception as e:
        return web.Response(text=f"Error reading logs: {e}", content_type="text/plain")


async def start_handler(request):
    if _bot_running():
        return web.json_response({"ok": True, "msg": "Already running"})
    ok, msg = _start_bot()
    return web.json_response({"ok": ok, "error": None if ok else msg})


async def stop_handler(request):
    if not _bot_running():
        return web.json_response({"ok": True, "msg": "Already stopped"})
    ok, msg = _stop_bot()
    return web.json_response({"ok": ok, "error": None if ok else msg})


async def index_handler(request):
    return web.Response(text=LAUNCHER_HTML, content_type="text/html")


def main():
    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/api/status", status_handler)
    app.router.add_get("/api/logs", logs_handler)
    app.router.add_post("/api/start", start_handler)
    app.router.add_post("/api/stop", stop_handler)
    print(f"Launcher: http://0.0.0.0:{PORT}")
    print("Use Start/Stop to control the bot. Dashboard: port", BOT_PORT)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
