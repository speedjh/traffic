#!/bin/bash
# 체류형 다음→카카오맵 트래픽 (맥/Linux)
cd "$(dirname "$0")"
export PATH="$(pwd)/platform-tools:$PATH"
python3 daum_dwell_device.py "$@"
