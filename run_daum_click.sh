#!/bin/bash
# 다음 클릭형 트래픽 (맥/Linux)
cd "$(dirname "$0")"
export PATH="$(pwd)/platform-tools:$PATH"
python3 daum_click_device.py "$@"
