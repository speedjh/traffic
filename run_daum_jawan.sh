#!/bin/bash
# 다음자완 트래픽 (맥/Linux)
cd "$(dirname "$0")"
export PATH="$(pwd)/platform-tools:$PATH"
python3 daum_jawan_device.py "$@"

