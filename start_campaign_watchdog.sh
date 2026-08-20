#!/bin/bash
# 워치독을 nohup/disown 으로 항상 백그라운드에 붙임.
# 이미 살아 있으면 no-op.
cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"
LOG_DIR="$ROOT/campaign_web/logs"
mkdir -p "$LOG_DIR"
PID_FILE="$LOG_DIR/watchdog.pid"
OUT_FILE="$LOG_DIR/watchdog.out"

if [ -f "$PID_FILE" ]; then
  old="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "${old:-}" ] && kill -0 "$old" 2>/dev/null; then
    if ps -p "$old" -o command= 2>/dev/null | grep -q 'watch_campaign_web.sh'; then
      echo "watchdog already running pid=$old"
      exit 0
    fi
  fi
fi

# 고아 pid 파일 정리
rm -f "$PID_FILE"
nohup "$ROOT/watch_campaign_web.sh" >>"$OUT_FILE" 2>&1 &
disown $! 2>/dev/null || true
# 워치독이 pid 파일을 쓸 때까지 잠깐 대기
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if [ -f "$PID_FILE" ]; then
    echo "watchdog started pid=$(cat "$PID_FILE") log=$LOG_DIR/watchdog.log"
    exit 0
  fi
  sleep 0.2
done
echo "watchdog launch issued (see $OUT_FILE) — pid file not yet visible"
exit 0
