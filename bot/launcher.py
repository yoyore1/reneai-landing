#!/usr/bin/env python3
"""
Launcher: Start/Stop the bot from a web UI.
Runs on port 8900. Start the launcher once, then use it to control the bot.

  python -m bot.launcher

When bot is stopped, dashboard (8899) is down. Use launcher (8900) to Start it.
"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import aiohttp
from aiohttp import web

PORT = 8900
BOT_PORT = 8899
TEST_BOT_PORT = 8898
PERFECT_TEST_BOT_PORT = 8896
INVERSE_TEST_BOT_PORT = 8897
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
<p class="sub">Start or stop the live bot</p>
<div class="btns">
<button id="startBtn" class="btn btn-start">Start Live Bot</button>
<button id="stopBtn" class="btn btn-stop">Stop Live Bot</button>
</div>
<p class="status" id="status"></p>
<hr style="border-color:#30363d;margin:1.5rem 0">
<p class="sub" style="margin-bottom:0.5rem">Test bot (fake money, all day)</p>
<div class="btns">
<button id="testStartBtn" class="btn btn-start" style="background:#d29922">Start Test Bot</button>
<button id="testStopBtn" class="btn btn-stop">Stop Test Bot</button>
</div>
<p class="status" id="testStatus"></p>
<hr style="border-color:#30363d;margin:1.5rem 0">
<p class="sub" style="margin-bottom:0.5rem">Perfect test bot (safer S3, fake money, all day)</p>
<div class="btns">
<button id="perfectStartBtn" class="btn btn-start" style="background:#238636">Start Perfect Test</button>
<button id="perfectStopBtn" class="btn btn-stop">Stop Perfect Test</button>
</div>
<p class="status" id="perfectStatus"></p>
<hr style="border-color:#30363d;margin:1.5rem 0">
<p class="sub" style="margin-bottom:0.5rem">Inverse test bot (underdog rules, fake money, all day)</p>
<div class="btns">
<button id="inverseStartBtn" class="btn btn-start" style="background:#a371f7">Start Inverse Test</button>
<button id="inverseStopBtn" class="btn btn-stop">Stop Inverse Test</button>
</div>
<p class="status" id="inverseStatus"></p>
<div style="margin-top:1.5rem;display:flex;flex-direction:column;gap:0.5rem;align-items:center">
<a class="dash" id="dashLink" href="#" target="_blank">Live Dashboard →</a>
<a class="dash" id="testDashLink" href="#" target="_blank">Test Dashboard →</a>
<a class="dash" id="perfectDashLink" href="#" target="_blank">Perfect Test Dashboard →</a>
<a class="dash" id="inverseDashLink" href="#" target="_blank">Inverse Test Dashboard →</a>
<a class="dash" href="/api/logs" target="_blank">View Live Logs</a>
<a class="dash" href="/api/test/logs" target="_blank">View Test Logs</a>
<a class="dash" href="/api/inverse/logs" target="_blank">View Inverse Test Logs</a>
</div>
</div>
<script>
const BOT_PORT = """ + str(BOT_PORT) + """;
const TEST_BOT_PORT = """ + str(TEST_BOT_PORT) + """;
const PERFECT_TEST_BOT_PORT = """ + str(PERFECT_TEST_BOT_PORT) + """;
const INVERSE_TEST_BOT_PORT = """ + str(INVERSE_TEST_BOT_PORT) + """;
document.getElementById('dashLink').href = (location.protocol === 'https:' ? 'https:' : 'http:') + '//' + location.hostname + ':' + BOT_PORT;
document.getElementById('testDashLink').href = (location.protocol === 'https:' ? 'https:' : 'http:') + '//' + location.hostname + ':' + (location.port || '8900') + '/test/';
document.getElementById('perfectDashLink').href = (location.protocol === 'https:' ? 'https:' : 'http:') + '//' + location.hostname + ':' + (location.port || '8900') + '/perfect/';
document.getElementById('inverseDashLink').href = (location.protocol === 'https:' ? 'https:' : 'http:') + '//' + location.hostname + ':' + (location.port || '8900') + '/inverse/';

async function pollStatus(){
 const ok = await fetch('/api/status').then(r=>r.json());
 const testOk = await fetch('/api/test/status').then(r=>r.json());
 const perfectOk = await fetch('/api/perfect/status').then(r=>r.json());
 const inverseOk = await fetch('/api/inverse/status').then(r=>r.json());
 document.getElementById('startBtn').disabled = ok.bot_running;
 document.getElementById('stopBtn').disabled = !ok.bot_running;
 document.getElementById('status').textContent = ok.bot_running ? 'Live bot running' : 'Live bot stopped';
 document.getElementById('testStartBtn').disabled = testOk.test_bot_running;
 document.getElementById('testStopBtn').disabled = !testOk.test_bot_running;
 document.getElementById('testStatus').textContent = testOk.test_bot_running ? 'Test bot running' : 'Test bot stopped';
 document.getElementById('perfectStartBtn').disabled = perfectOk.perfect_bot_running;
 document.getElementById('perfectStopBtn').disabled = !perfectOk.perfect_bot_running;
 document.getElementById('perfectStatus').textContent = perfectOk.perfect_bot_running ? 'Perfect test bot running' : 'Perfect test bot stopped';
 document.getElementById('inverseStartBtn').disabled = inverseOk.inverse_bot_running;
 document.getElementById('inverseStopBtn').disabled = !inverseOk.inverse_bot_running;
 document.getElementById('inverseStatus').textContent = inverseOk.inverse_bot_running ? 'Inverse test bot running' : 'Inverse test bot stopped';
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
 if (!confirm('Stop the live bot?')) return;
 this.disabled = true;
 document.getElementById('status').textContent = 'Stopping...';
 await fetch('/api/stop', {method:'POST'});
 document.getElementById('status').textContent = 'Stopped';
 setTimeout(pollStatus, 1000);
};

document.getElementById('testStartBtn').onclick = async function(){
 this.disabled = true;
 document.getElementById('testStatus').textContent = 'Starting...';
 const r = await fetch('/api/test/start', {method:'POST'});
 const j = await r.json();
 document.getElementById('testStatus').textContent = j.ok ? 'Started' : (j.error||'Failed');
 setTimeout(pollStatus, 2000);
};

document.getElementById('testStopBtn').onclick = async function(){
 if (!confirm('Stop the test bot?')) return;
 this.disabled = true;
 document.getElementById('testStatus').textContent = 'Stopping...';
 await fetch('/api/test/stop', {method:'POST'});
 document.getElementById('testStatus').textContent = 'Stopped';
 setTimeout(pollStatus, 1000);
};

document.getElementById('perfectStartBtn').onclick = async function(){
 this.disabled = true;
 document.getElementById('perfectStatus').textContent = 'Starting...';
 const r = await fetch('/api/perfect/start', {method:'POST'});
 const j = await r.json();
 document.getElementById('perfectStatus').textContent = j.ok ? 'Started' : (j.error||'Failed');
 setTimeout(pollStatus, 2000);
};

document.getElementById('perfectStopBtn').onclick = async function(){
 if (!confirm('Stop the perfect test bot?')) return;
 this.disabled = true;
 document.getElementById('perfectStatus').textContent = 'Stopping...';
 await fetch('/api/perfect/stop', {method:'POST'});
 document.getElementById('perfectStatus').textContent = 'Stopped';
 setTimeout(pollStatus, 1000);
};

document.getElementById('inverseStartBtn').onclick = async function(){
 this.disabled = true;
 document.getElementById('inverseStatus').textContent = 'Starting...';
 const r = await fetch('/api/inverse/start', {method:'POST'});
 const j = await r.json();
 document.getElementById('inverseStatus').textContent = j.ok ? 'Started' : (j.error||'Failed');
 setTimeout(pollStatus, 2000);
};

document.getElementById('inverseStopBtn').onclick = async function(){
 if (!confirm('Stop the inverse test bot?')) return;
 this.disabled = true;
 document.getElementById('inverseStatus').textContent = 'Stopping...';
 await fetch('/api/inverse/stop', {method:'POST'});
 document.getElementById('inverseStatus').textContent = 'Stopped';
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


def _test_bot_running() -> bool:
    try:
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{TEST_BOT_PORT}/api/state")
        urllib.request.urlopen(req, timeout=2)
        return True
    except Exception:
        return False


def _inverse_test_bot_running() -> bool:
    try:
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{INVERSE_TEST_BOT_PORT}/api/state")
        urllib.request.urlopen(req, timeout=2)
        return True
    except Exception:
        return False


def _perfect_test_bot_running() -> bool:
    try:
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{PERFECT_TEST_BOT_PORT}/api/state")
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


def _start_test_bot() -> "tuple[bool, str]":
    os.chdir(BOT_DIR)
    log_file = BOT_DIR / "test_bot.log"
    cmd = ["screen", "-dmS", "testbot", "bash", "-c", f"cd {BOT_DIR} && {sys.executable} -m bot.main --headless --test >> {log_file} 2>&1"]
    try:
        subprocess.run(cmd, cwd=str(BOT_DIR), capture_output=True, text=True, timeout=5)
        return True, "Started"
    except FileNotFoundError:
        try:
            with open(log_file, "a") as f:
                subprocess.Popen(
                    [sys.executable, "-m", "bot.main", "--headless", "--test"],
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


def _stop_test_bot() -> "tuple[bool, str]":
    try:
        subprocess.run(["screen", "-S", "testbot", "-X", "quit"], capture_output=True, timeout=3)
        return True, "Stopped"
    except Exception:
        pass
    try:
        subprocess.run(["pkill", "-f", "bot.main --headless --test"], capture_output=True, timeout=3)
        return True, "Stopped"
    except Exception as e:
        return False, str(e)


def _start_inverse_test_bot() -> "tuple[bool, str]":
    os.chdir(BOT_DIR)
    log_file = BOT_DIR / "inverse_test_bot.log"
    cmd = ["screen", "-dmS", "inversebot", "bash", "-c", f"cd {BOT_DIR} && {sys.executable} -m bot.main --headless --test-inverse >> {log_file} 2>&1"]
    try:
        subprocess.run(cmd, cwd=str(BOT_DIR), capture_output=True, text=True, timeout=5)
        return True, "Started"
    except FileNotFoundError:
        try:
            with open(log_file, "a") as f:
                subprocess.Popen(
                    [sys.executable, "-m", "bot.main", "--headless", "--test-inverse"],
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


def _stop_inverse_test_bot() -> "tuple[bool, str]":
    try:
        subprocess.run(["screen", "-S", "inversebot", "-X", "quit"], capture_output=True, timeout=3)
        return True, "Stopped"
    except Exception:
        pass
    try:
        subprocess.run(["pkill", "-f", "bot.main --headless --test-inverse"], capture_output=True, timeout=3)
        return True, "Stopped"
    except Exception as e:
        return False, str(e)


def _start_perfect_test_bot() -> "tuple[bool, str]":
    os.chdir(BOT_DIR)
    log_file = BOT_DIR / "perfect_test_bot.log"
    cmd = [
        "screen",
        "-dmS",
        "perfectbot",
        "bash",
        "-c",
        f"cd {BOT_DIR} && {sys.executable} -m bot.main --headless --test-perfect >> {log_file} 2>&1",
    ]
    try:
        subprocess.run(cmd, cwd=str(BOT_DIR), capture_output=True, text=True, timeout=5)
        return True, "Started"
    except FileNotFoundError:
        try:
            with open(log_file, "a") as f:
                subprocess.Popen(
                    [sys.executable, "-m", "bot.main", "--headless", "--test-perfect"],
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


def _stop_perfect_test_bot() -> "tuple[bool, str]":
    try:
        subprocess.run(["screen", "-S", "perfectbot", "-X", "quit"], capture_output=True, timeout=3)
        return True, "Stopped"
    except Exception:
        pass
    try:
        subprocess.run(["pkill", "-f", "bot.main --headless --test-perfect"], capture_output=True, timeout=3)
        return True, "Stopped"
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
    # Fallback: kill only the live bot (--live or --s3-only without --test)
    try:
        subprocess.run(["pkill", "-f", "bot.main --headless --s3-only --live"], capture_output=True, timeout=3)
        return True, "Stopped"
    except Exception as e:
        return False, str(e)


async def status_handler(request):
    return web.json_response({"bot_running": _bot_running()})


async def test_status_handler(request):
    return web.json_response({"test_bot_running": _test_bot_running()})


async def test_start_handler(request):
    if _test_bot_running():
        return web.json_response({"ok": True, "msg": "Already running"})
    ok, msg = _start_test_bot()
    return web.json_response({"ok": ok, "error": None if ok else msg})


async def test_stop_handler(request):
    if not _test_bot_running():
        return web.json_response({"ok": True, "msg": "Already stopped"})
    ok, msg = _stop_test_bot()
    return web.json_response({"ok": ok, "error": None if ok else msg})


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


async def test_logs_handler(request):
    """Serve last 500 lines of test_bot.log."""
    log_path = BOT_DIR / "test_bot.log"
    if not log_path.exists():
        return web.Response(text="No test log yet. Start the test bot first.", content_type="text/plain")
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        last = lines[-500:] if len(lines) > 500 else lines
        body = "".join(last)
        return web.Response(text=body, content_type="text/plain; charset=utf-8")
    except Exception as e:
        return web.Response(text=f"Error reading logs: {e}", content_type="text/plain")


async def inverse_logs_handler(request):
    """Serve last 500 lines of inverse_test_bot.log."""
    log_path = BOT_DIR / "inverse_test_bot.log"
    if not log_path.exists():
        return web.Response(text="No inverse test log yet. Start the inverse test bot first.", content_type="text/plain")
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        last = lines[-500:] if len(lines) > 500 else lines
        body = "".join(last)
        return web.Response(text=body, content_type="text/plain; charset=utf-8")
    except Exception as e:
        return web.Response(text=f"Error reading logs: {e}", content_type="text/plain")


async def perfect_logs_handler(request):
    """Serve last 500 lines of perfect_test_bot.log."""
    log_path = BOT_DIR / "perfect_test_bot.log"
    if not log_path.exists():
        return web.Response(text="No perfect test log yet. Start the perfect test bot first.", content_type="text/plain")
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        last = lines[-500:] if len(lines) > 500 else lines
        body = "".join(last)
        return web.Response(text=body, content_type="text/plain; charset=utf-8")
    except Exception as e:
        return web.Response(text=f"Error reading logs: {e}", content_type="text/plain")


async def inverse_status_handler(request):
    return web.json_response({"inverse_bot_running": _inverse_test_bot_running()})


async def perfect_status_handler(request):
    return web.json_response({"perfect_bot_running": _perfect_test_bot_running()})


async def inverse_start_handler(request):
    if _inverse_test_bot_running():
        return web.json_response({"ok": True, "msg": "Already running"})
    ok, msg = _start_inverse_test_bot()
    return web.json_response({"ok": ok, "error": None if ok else msg})


async def inverse_stop_handler(request):
    if not _inverse_test_bot_running():
        return web.json_response({"ok": True, "msg": "Already stopped"})
    ok, msg = _stop_inverse_test_bot()
    return web.json_response({"ok": ok, "error": None if ok else msg})


async def perfect_start_handler(request):
    if _perfect_test_bot_running():
        return web.json_response({"ok": True, "msg": "Already running"})
    ok, msg = _start_perfect_test_bot()
    return web.json_response({"ok": ok, "error": None if ok else msg})


async def perfect_stop_handler(request):
    if not _perfect_test_bot_running():
        return web.json_response({"ok": True, "msg": "Already stopped"})
    ok, msg = _stop_perfect_test_bot()
    return web.json_response({"ok": ok, "error": None if ok else msg})


def _run_deploy() -> "tuple[bool, str]":
    """Run git pull in BOT_DIR. Returns (success, message)."""
    try:
        r = subprocess.run(
            ["git", "pull", "origin", "main"],
            cwd=str(BOT_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = (r.stdout or "").strip() + "\n" + (r.stderr or "").strip()
        if r.returncode != 0:
            return False, out or f"git pull exited {r.returncode}"
        return True, out or "ok"
    except Exception as e:
        return False, str(e)


async def deploy_handler(request):
    """POST /api/deploy: run git pull in project dir (so server gets latest code)."""
    ok, msg = _run_deploy()
    return web.json_response({"ok": ok, "message": msg})


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


_TEST_BOT_URL = "http://127.0.0.1:8898"
_PERFECT_TEST_BOT_URL = "http://127.0.0.1:8896"
_INVERSE_TEST_BOT_URL = "http://127.0.0.1:8897"


async def _proxy_to_test_bot(request, path: str):
    """Proxy request to test bot. Returns 503 if test bot not running."""
    url = f"{_TEST_BOT_URL}/{path}"
    try:
        async with aiohttp.ClientSession() as session:
            if request.method == "GET":
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    body = await resp.read()
                    return web.Response(body=body, status=resp.status, content_type=resp.content_type)
            elif request.method == "POST":
                body = await request.read()
                async with session.post(url, data=body, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return web.Response(status=resp.status)
    except Exception as e:
        return web.Response(text=f"Test bot not reachable: {e}", status=503)


async def test_dashboard_handler(request):
    """Serve test dashboard HTML (proxied from test bot), rewritten to use proxy paths."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{_TEST_BOT_URL}/", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                html = await resp.text()
                # Rewrite API/WS paths to go through our proxy (avoids needing port 8898 open)
                html = html.replace("'/api/state'", "'/test-api/state'")
                html = html.replace("'/api/verify-trades'", "'/test-api/verify-trades'")
                html = html.replace("'/api/stop'", "'/test-api/stop'")
                html = html.replace("+'/ws'", "+'/test-ws'")
                return web.Response(text=html, content_type="text/html")
    except Exception:
        return web.Response(
            text="<html><body><h2>Test bot not running</h2><p>Start it from the launcher.</p></body></html>",
            status=503,
            content_type="text/html",
        )


async def test_proxy_ws_handler(request):
    """WebSocket proxy to test bot."""
    ws_client = web.WebSocketResponse()
    await ws_client.prepare(request)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect("ws://127.0.0.1:8898/ws") as ws_server:
                async def forward_from_server():
                    async for msg in ws_server:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await ws_client.send_str(msg.data)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            break

                async def forward_from_client():
                    async for msg in ws_client:
                        if msg.type == web.WSMsgType.TEXT:
                            await ws_server.send_str(msg.data)
                        elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                            break

                await asyncio.gather(forward_from_server(), forward_from_client())
    except Exception:
        pass
    finally:
        await ws_client.close()
    return ws_client


async def _proxy_to_perfect_bot(request, path: str):
    """Proxy request to perfect test bot on 8896."""
    url = f"{_PERFECT_TEST_BOT_URL}/{path}"
    try:
        async with aiohttp.ClientSession() as session:
            if request.method == "GET":
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    body = await resp.read()
                    return web.Response(body=body, status=resp.status, content_type=resp.content_type)
            elif request.method == "POST":
                body = await request.read()
                async with session.post(url, data=body, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return web.Response(status=resp.status)
    except Exception as e:
        return web.Response(text=f"Perfect test bot not reachable: {e}", status=503)


async def perfect_dashboard_handler(request):
    """Serve perfect test dashboard HTML (proxied from perfect bot on 8896)."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{_PERFECT_TEST_BOT_URL}/", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                html = await resp.text()
                html = html.replace("'/api/state'", "'/perfect-api/state'")
                html = html.replace("'/api/verify-trades'", "'/perfect-api/verify-trades'")
                html = html.replace("'/api/stop'", "'/perfect-api/stop'")
                html = html.replace("+'/ws'", "+'/perfect-ws'")
                return web.Response(text=html, content_type="text/html")
    except Exception:
        return web.Response(
            text="<html><body><h2>Perfect test bot not running</h2><p>Start it from the launcher.</p></body></html>",
            status=503,
            content_type="text/html",
        )


async def perfect_proxy_ws_handler(request):
    """WebSocket proxy to perfect test bot."""
    ws_client = web.WebSocketResponse()
    await ws_client.prepare(request)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect("ws://127.0.0.1:8896/ws") as ws_server:
                async def forward_from_server():
                    async for msg in ws_server:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await ws_client.send_str(msg.data)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            break

                async def forward_from_client():
                    async for msg in ws_client:
                        if msg.type == web.WSMsgType.TEXT:
                            await ws_server.send_str(msg.data)
                        elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                            break

                await asyncio.gather(forward_from_server(), forward_from_client())
    except Exception:
        pass
    finally:
        await ws_client.close()
    return ws_client


async def _proxy_to_inverse_bot(request, path: str):
    """Proxy request to inverse test bot on 8897."""
    url = f"{_INVERSE_TEST_BOT_URL}/{path}"
    try:
        async with aiohttp.ClientSession() as session:
            if request.method == "GET":
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    body = await resp.read()
                    return web.Response(body=body, status=resp.status, content_type=resp.content_type)
            elif request.method == "POST":
                body = await request.read()
                async with session.post(url, data=body, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return web.Response(status=resp.status)
    except Exception as e:
        return web.Response(text=f"Inverse test bot not reachable: {e}", status=503)


async def inverse_dashboard_handler(request):
    """Serve inverse test dashboard HTML (proxied from inverse bot on 8897)."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{_INVERSE_TEST_BOT_URL}/", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                html = await resp.text()
                html = html.replace("'/api/state'", "'/inverse-api/state'")
                html = html.replace("'/api/verify-trades'", "'/inverse-api/verify-trades'")
                html = html.replace("'/api/stop'", "'/inverse-api/stop'")
                html = html.replace("+'/ws'", "+'/inverse-ws'")
                return web.Response(text=html, content_type="text/html")
    except Exception:
        return web.Response(
            text="<html><body><h2>Inverse test bot not running</h2><p>Start it from the launcher.</p></body></html>",
            status=503,
            content_type="text/html",
        )


async def inverse_proxy_ws_handler(request):
    """WebSocket proxy to inverse test bot."""
    ws_client = web.WebSocketResponse()
    await ws_client.prepare(request)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect("ws://127.0.0.1:8897/ws") as ws_server:
                async def forward_from_server():
                    async for msg in ws_server:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await ws_client.send_str(msg.data)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            break

                async def forward_from_client():
                    async for msg in ws_client:
                        if msg.type == web.WSMsgType.TEXT:
                            await ws_server.send_str(msg.data)
                        elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                            break

                await asyncio.gather(forward_from_server(), forward_from_client())
    except Exception:
        pass
    finally:
        await ws_client.close()
    return ws_client


def main():
    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/test", test_dashboard_handler)
    app.router.add_get("/test/", test_dashboard_handler)
    app.router.add_get("/perfect", perfect_dashboard_handler)
    app.router.add_get("/perfect/", perfect_dashboard_handler)
    app.router.add_get("/inverse", inverse_dashboard_handler)
    app.router.add_get("/inverse/", inverse_dashboard_handler)
    app.router.add_get("/test-api/state", lambda r: _proxy_to_test_bot(r, "api/state"))
    app.router.add_get("/test-api/verify-trades", lambda r: _proxy_to_test_bot(r, "api/verify-trades"))
    app.router.add_post("/test-api/stop", lambda r: _proxy_to_test_bot(r, "api/stop"))
    app.router.add_get("/test-ws", test_proxy_ws_handler)
    app.router.add_get("/perfect-api/state", lambda r: _proxy_to_perfect_bot(r, "api/state"))
    app.router.add_get("/perfect-api/verify-trades", lambda r: _proxy_to_perfect_bot(r, "api/verify-trades"))
    app.router.add_post("/perfect-api/stop", lambda r: _proxy_to_perfect_bot(r, "api/stop"))
    app.router.add_get("/perfect-ws", perfect_proxy_ws_handler)
    app.router.add_get("/inverse-api/state", lambda r: _proxy_to_inverse_bot(r, "api/state"))
    app.router.add_get("/inverse-api/verify-trades", lambda r: _proxy_to_inverse_bot(r, "api/verify-trades"))
    app.router.add_post("/inverse-api/stop", lambda r: _proxy_to_inverse_bot(r, "api/stop"))
    app.router.add_get("/inverse-ws", inverse_proxy_ws_handler)
    app.router.add_get("/api/status", status_handler)
    app.router.add_get("/api/test/status", test_status_handler)
    app.router.add_get("/api/perfect/status", perfect_status_handler)
    app.router.add_get("/api/inverse/status", inverse_status_handler)
    app.router.add_get("/api/logs", logs_handler)
    app.router.add_get("/api/test/logs", test_logs_handler)
    app.router.add_get("/api/perfect/logs", perfect_logs_handler)
    app.router.add_get("/api/inverse/logs", inverse_logs_handler)
    app.router.add_post("/api/start", start_handler)
    app.router.add_post("/api/stop", stop_handler)
    app.router.add_post("/api/test/start", test_start_handler)
    app.router.add_post("/api/test/stop", test_stop_handler)
    app.router.add_post("/api/perfect/start", perfect_start_handler)
    app.router.add_post("/api/perfect/stop", perfect_stop_handler)
    app.router.add_post("/api/inverse/start", inverse_start_handler)
    app.router.add_post("/api/inverse/stop", inverse_stop_handler)
    app.router.add_post("/api/deploy", deploy_handler)
    print(f"Launcher: http://0.0.0.0:{PORT}")
    print("Live dashboard: port", BOT_PORT, "| Test: port", TEST_BOT_PORT, "| Inverse test: port", INVERSE_TEST_BOT_PORT)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
