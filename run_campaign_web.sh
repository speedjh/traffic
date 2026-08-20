#!/bin/bash
# 캠페인 작업큐 웹 서버
# 기본: 안정 기동(무 reload). 개발 시 RELOAD=1 ./run_campaign_web.sh
# 상시 감시: ./start_campaign_watchdog.sh (웹 기동 시 자동 nohup).
# 중지 워치독: kill "$(cat campaign_web/logs/watchdog.pid)"
cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"
export PATH="$ROOT/platform-tools:$PATH"

# 워치독이 웹을 띄울 때(SKIP_WATCHDOG=1)는 재귀 기동 방지
if [ "${SKIP_WATCHDOG:-0}" != "1" ]; then
  "$ROOT/start_campaign_watchdog.sh" >/dev/null 2>&1 || true
fi

if [ -x ".venv/bin/uvicorn" ]; then
  UV=".venv/bin/uvicorn"
else
  UV="uvicorn"
fi
EXTRA=()
if [ "${RELOAD:-0}" = "1" ]; then
  EXTRA+=(--reload)
fi
exec "$UV" campaign_web.main:app --host 0.0.0.0 --port "${PORT:-8080}" "${EXTRA[@]}"
