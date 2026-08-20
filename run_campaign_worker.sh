#!/bin/bash
# 캠페인 작업큐 디바이스 워커
# 예: ./run_campaign_worker.sh --serial R3CY80RW96Y
#     ./run_campaign_worker.sh --serial R3CY80RW96Y --dry-run
# 웹/워커 상시감시: ./start_campaign_watchdog.sh (워커 기동 시에도 자동 확인)
cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"
export PATH="$ROOT/platform-tools:$PATH"
# 화면 잠금 PIN — 기본 비움(PIN/비밀번호 없음). 필요 시만 DEVICE_PIN 설정.
export DEVICE_PIN="${DEVICE_PIN:-}"

if [ "${SKIP_WATCHDOG:-0}" != "1" ]; then
  "$ROOT/start_campaign_watchdog.sh" >/dev/null 2>&1 || true
fi

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi
exec "$PY" -m campaign_web.worker "$@"
