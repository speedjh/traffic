#!/bin/bash
# campaign_web + worker 워치독
# - 기본 30초마다 8080 헬스체크 / adb 기기별 워커 생존 / 하트비트 확인
# - 웹·워커 DOWN 시 자동 재기동, ADB 단절은 큰 소리로 로그
# 기동(권장): ./start_campaign_watchdog.sh
# 중지: kill "$(cat campaign_web/logs/watchdog.pid)"  또는  pkill -f 'watch_campaign_web.sh'
set -u
cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"
export PATH="$ROOT/platform-tools:$PATH"
LOG_DIR="$ROOT/campaign_web/logs"
mkdir -p "$LOG_DIR"
PID_FILE="$LOG_DIR/watchdog.pid"
LOG_FILE="$LOG_DIR/watchdog.log"
PORT="${PORT:-8080}"
URL="http://127.0.0.1:${PORT}/"
API="${CAMPAIGN_API:-http://127.0.0.1:${PORT}}"
INTERVAL="${WATCH_INTERVAL:-30}"
HEARTBEAT_STALE_SECS="${WATCH_HEARTBEAT_STALE:-90}"
ADB_WARN_EVERY="${ADB_OFFLINE_WARN_EVERY:-4}"  # ticks (~2min at 30s)

# 이미 다른 워치독이 살아 있으면 종료 (pid 파일 정합)
if [ -f "$PID_FILE" ]; then
  old="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "${old:-}" ] && [ "$old" != "$$" ] && kill -0 "$old" 2>/dev/null; then
    # 같은 스크립트인지 확인
    if ps -p "$old" -o command= 2>/dev/null | grep -q 'watch_campaign_web.sh'; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') watchdog already running pid=$old — exit" >>"$LOG_FILE"
      exit 0
    fi
  fi
fi
echo $$ >"$PID_FILE"
cleanup() { rm -f "$PID_FILE"; }
trap cleanup EXIT INT TERM

log() {
  line="$(date '+%Y-%m-%d %H:%M:%S') $*"
  echo "$line" >>"$LOG_FILE"
}

web_ok() {
  curl -fsS --connect-timeout 3 --max-time 5 "$URL" >/dev/null 2>&1
}

ensure_web() {
  if web_ok; then
    return 0
  fi
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    log "ALERT web: port $PORT listening but curl failed — skip blind kill"
    return 1
  fi
  log "ALERT web: DOWN — starting ./run_campaign_web.sh (no reload)"
  (
    cd "$ROOT" || exit 1
    # 워치독→웹 재기동이 워치독 재기동 루프를 만들지 않게
    SKIP_WATCHDOG=1 RELOAD=0 exec ./run_campaign_web.sh
  ) >>"$LOG_DIR/web.out" 2>&1 &
  echo $! >"$LOG_DIR/web.pid"
  sleep 3
  if web_ok; then
    log "web: BACK UP pid=$(cat "$LOG_DIR/web.pid" 2>/dev/null || echo '?')"
    return 0
  fi
  log "ALERT web: still DOWN after restart"
  return 1
}

worker_pid_alive() {
  serial="$1"
  pf="$LOG_DIR/worker_${serial}.pid"
  if [ -f "$pf" ]; then
    pid="$(cat "$pf" 2>/dev/null || true)"
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
      if ps -p "$pid" -o command= 2>/dev/null | grep -qE "campaign_web\.worker|run_campaign_worker"; then
        return 0
      fi
    fi
  fi
  # pid 파일 유실·재기동 대비
  pgrep -f "campaign_web\.worker.*${serial}" >/dev/null 2>&1
}

start_worker() {
  serial="$1"
  reason="${2:-missing}"
  log "ALERT worker: start for $serial ($reason) — ./run_campaign_worker.sh --serial $serial"
  (
    cd "$ROOT" || exit 1
    export DEVICE_PIN="${DEVICE_PIN:-}"
    exec ./run_campaign_worker.sh --serial "$serial" --api "$API"
  ) >>"$LOG_DIR/worker_${serial}.out" 2>&1 &
  echo $! >"$LOG_DIR/worker_${serial}.pid"
  # 실제 워커 로그도 /tmp 로 가는 케이스가 있어 out 파일에도 표식
  log "worker: spawned pid=$(cat "$LOG_DIR/worker_${serial}.pid" 2>/dev/null || echo '?') serial=$serial"
}

api_device_online() {
  serial="$1"
  py="$ROOT/.venv/bin/python"
  [ -x "$py" ] || py=python3
  "$py" - "$API" "$serial" "$HEARTBEAT_STALE_SECS" <<'PY' 2>/dev/null
import json, sys, urllib.request
from datetime import datetime
api, serial, stale = sys.argv[1], sys.argv[2], float(sys.argv[3])
try:
    with urllib.request.urlopen(api.rstrip("/") + "/api/devices", timeout=5) as r:
        devices = json.load(r)
except Exception:
    sys.exit(2)
now = datetime.now()
for d in devices:
    if d.get("serial") != serial:
        continue
    if d.get("online") is True:
        sys.exit(0)
    hb = d.get("last_heartbeat")
    if not hb:
        sys.exit(1)
    try:
        ts = datetime.fromisoformat(hb.replace("Z", ""))
    except Exception:
        sys.exit(1)
    age = (now - ts).total_seconds()
    sys.exit(0 if age <= stale else 1)
sys.exit(1)
PY
}

stale_strike_file() { echo "$LOG_DIR/worker_${1}.stale_strikes"; }

reset_stale_strikes() { rm -f "$(stale_strike_file "$1")" 2>/dev/null || true; }

bump_stale_strikes() {
  serial="$1"
  f="$(stale_strike_file "$serial")"
  n=0
  [ -f "$f" ] && n="$(cat "$f" 2>/dev/null || echo 0)"
  n=$((n + 1))
  echo "$n" >"$f"
  echo "$n"
}

list_adb_serials() {
  command -v adb >/dev/null 2>&1 || return 0
  adb devices 2>/dev/null | awk 'NR>1 && $2=="device" {print $1}'
}

ensure_workers() {
  command -v adb >/dev/null 2>&1 || {
    log "ALERT adb: binary missing on PATH=$PATH"
    return 0
  }
  # adb 서버가 죽은 경우 기기가 안 보일 수 있음 → 가볍게 깨움
  adb start-server >/dev/null 2>&1 || true

  serials="$(list_adb_serials)"
  if [ -z "${serials}" ]; then
    ADB_OFF_TICKS=$(( ${ADB_OFF_TICKS:-0} + 1 ))
    if [ $((ADB_OFF_TICKS % ADB_WARN_EVERY)) -eq 1 ]; then
      log "ALERT adb: NO DEVICE attached — claims paused (USB debugging / 파일전송 모드 확인). tick=$ADB_OFF_TICKS"
      # 알려진 시리얼 워커라도 heartbeat stale 이면 재기동하지 않음(디바이스 없을 때 무의미 loop만)
      # 다만 프로세스 자체가 죽은 건 복구: 최근 out/pid 시리얼 스캔
      for pf in "$LOG_DIR"/worker_*.pid; do
        [ -f "$pf" ] || continue
        base="$(basename "$pf")"
        serial="${base#worker_}"
        serial="${serial%.pid}"
        if ! worker_pid_alive "$serial"; then
          log "ALERT worker: process dead for $serial while adb empty — respawn to resume on reconnect"
          start_worker "$serial" "dead-while-adb-empty"
        fi
      done
    fi
    return 0
  fi
  ADB_OFF_TICKS=0

  echo "$serials" | while read -r serial; do
    [ -n "$serial" ] || continue
    if ! worker_pid_alive "$serial"; then
      reset_stale_strikes "$serial"
      start_worker "$serial" "process-dead"
      continue
    fi
    # 프로세스는 있는데 API heartbeat 가 stale 이면 hung/좀비 → 연속 2회 확인 후 재기동
    # 단, busy_job_id 가 있으면 job 실행 중일 수 있으므로(구버전 워커) 즉시 죽이지 않음
    if web_ok; then
      if api_device_online "$serial"; then
        reset_stale_strikes "$serial"
      else
        busy_id="$("$ROOT/.venv/bin/python" - "$API" "$serial" <<'PY' 2>/dev/null || true
import json, sys, urllib.request
api, serial = sys.argv[1], sys.argv[2]
try:
    with urllib.request.urlopen(api.rstrip("/") + "/api/devices", timeout=5) as r:
        devices = json.load(r)
except Exception:
    print("")
    raise SystemExit
for d in devices:
    if d.get("serial") == serial:
        print(d.get("busy_job_id") or "")
        break
PY
)"
        if [ -n "${busy_id}" ]; then
          log "worker: heartbeat stale but busy_job_id=$busy_id — skip kill (job in progress)"
          reset_stale_strikes "$serial"
          continue
        fi
        strikes="$(bump_stale_strikes "$serial")"
        log "ALERT worker: heartbeat STALE for $serial (strike=$strikes)"
        if [ "$strikes" -ge 2 ]; then
          log "ALERT worker: killing stale $serial + restart"
          pf="$LOG_DIR/worker_${serial}.pid"
          if [ -f "$pf" ]; then
            pid="$(cat "$pf" 2>/dev/null || true)"
            [ -n "${pid:-}" ] && kill "$pid" 2>/dev/null || true
            sleep 1
            [ -n "${pid:-}" ] && kill -9 "$pid" 2>/dev/null || true
          fi
          pkill -f "campaign_web\.worker.*${serial}" 2>/dev/null || true
          sleep 1
          reset_stale_strikes "$serial"
          start_worker "$serial" "stale-heartbeat"
        fi
      fi
    fi
  done
}

night_loop_alive() {
  pf="$LOG_DIR/night_monitor_loop.pid"
  [ -f "$pf" ] || return 1
  pid="$(tr -d ' \n\r' <"$pf" 2>/dev/null || true)"
  [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null
}

ensure_night_loop() {
  if night_loop_alive; then
    return 0
  fi
  py="$ROOT/.venv/bin/python"
  daemon="$ROOT/campaign_web/logs/night_monitor_daemon.py"
  [ -x "$py" ] && [ -f "$daemon" ] || return 0
  log "ALERT night_loop: DOWN — starting night_monitor_daemon.py"
  (
    cd "$ROOT" || exit 1
    exec "$py" "$daemon"
  ) >>"$LOG_DIR/night_monitor_loop.out" 2>&1 &
  sleep 1
  if night_loop_alive; then
    log "night_loop: BACK UP pid=$(tr -d ' \n\r' <"$LOG_DIR/night_monitor_loop.pid" 2>/dev/null || echo '?')"
  else
    log "ALERT night_loop: still DOWN after restart"
  fi
}

log "watchdog START pid=$$ interval=${INTERVAL}s url=$URL api=$API"
ensure_web || true
ensure_workers || true
ensure_night_loop || true
while true; do
  sleep "$INTERVAL" || true
  # pid 파일 유지(일부 환경에서 덮어쓰기 유실 방지)
  echo $$ >"$PID_FILE"
  ensure_web || true
  ensure_workers || true
  ensure_night_loop || true
done
