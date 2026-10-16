#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# BfixF watchdog — keeps the bot + website alive without systemd.
#
#   bash tools/watchdog.sh                 # একবার চেক করে (cron-এর জন্য আদর্শ)
#   bash tools/watchdog.sh --loop 60       # প্রতি ৬০ সেকেন্ডে চেক (nohup/screen)
#
# কী দেখে:
#   1. প্রসেস আছে কি না (pid ফাইল অথবা `pgrep -f "python main.py"`)
#   2. ওয়েবসাইটের /health ঠিক আছে কি না (ডিফল্ট http://127.0.0.1:$PORT/health)
#   3. না থাকলে আবার চালু করে, আর logs/watchdog.log-এ লিখে রাখে
#
# cron (প্রতি ৫ মিনিটে):
#   */5 * * * * cd /home/bfixf/BfixF && bash tools/watchdog.sh >> logs/watchdog.log 2>&1
# ---------------------------------------------------------------------------
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

LOOP=0
[[ "${1:-}" == "--loop" ]] && LOOP="${2:-60}"

PORT="${PORT:-$(grep -E '^\s*PORT\s*=' config.env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' "'"'"'')}"
PORT="${PORT:-8080}"
HEALTH_URL="http://127.0.0.1:${PORT}/health"
PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if [[ -x .venv/bin/python ]]; then PYTHON=".venv/bin/python"; else PYTHON="python3"; fi
fi
PID_FILE="logs/bfixf.pid"
LOG_FILE="logs/watchdog.log"
mkdir -p logs
STAMP() { date '+%Y-%m-%d %H:%M:%S'; }
say() { echo "[$(STAMP)] $*" | tee -a "$LOG_FILE"; }

running() {
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    return 0
  fi
  pgrep -f "python(3)? (main|run|bot)\.py" >/dev/null 2>&1
}

healthy() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 8 "$HEALTH_URL" >/dev/null 2>&1
  else
    "$PYTHON" - "$HEALTH_URL" <<'PY' >/dev/null 2>&1
import sys, urllib.request
urllib.request.urlopen(sys.argv[1], timeout=8).read()
PY
  fi
}

start_it() {
  say "🚀 চালু করা হচ্ছে: $PYTHON main.py (port $PORT)"
  nohup "$PYTHON" main.py >> logs/stdout.log 2>&1 &
  echo $! >"$PID_FILE"
  sleep 4
}

check_once() {
  if ! running; then
    say "⚠️ প্রসেস চলছে না — আবার চালু করছি।"
    start_it
    return
  fi
  if ! healthy; then
    say "⚠️ প্রসেস আছে কিন্তু $HEALTH_URL উত্তর দিচ্ছে না — রিস্টার্ট করছি।"
    [[ -f "$PID_FILE" ]] && kill "$(cat "$PID_FILE")" 2>/dev/null
    pkill -f "python(3)? (main|run|bot)\.py" 2>/dev/null
    sleep 2
    start_it
    return
  fi
  say "✅ ঠিক আছে (pid $(cat "$PID_FILE" 2>/dev/null || echo '?'), $HEALTH_URL)"
}

if [[ "$LOOP" == "0" ]]; then
  check_once
  exit 0
fi

say "👀 ওয়াচডগ শুরু — প্রতি ${LOOP} সেকেন্ডে চেক"
while true; do
  check_once
  sleep "$LOOP"
done
