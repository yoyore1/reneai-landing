#!/bin/bash
# Start the launcher (port 9000) and vol_logger in screen sessions.
# Usage: ./start_launcher.sh   or   bash start_launcher.sh
# To reattach: screen -r launcher  |  screen -r vollogger

cd "$(dirname "$0")"

# ── Launcher ──
screen -S launcher -X quit 2>/dev/null || true
pkill -f "bot.launcher" 2>/dev/null || true
sleep 1
screen -dmS launcher bash -c "cd $(pwd) && python3 -m bot.launcher 2>&1 | tee -a launcher.log"
echo "Launcher starting in screen 'launcher'. Open http://YOUR_EC2_IP:9000"

# ── Vol Logger (runs 24/7 for continuous market analysis) ──
screen -S vollogger -X quit 2>/dev/null || true
pkill -f "bot.vol_logger" 2>/dev/null || true
sleep 1
screen -dmS vollogger bash -c "cd $(pwd) && python3 -m bot.vol_logger 2>&1 | tee -a vol_logger.log"
echo "Vol logger starting in screen 'vollogger'."

echo "To attach: screen -r launcher  |  screen -r vollogger"
