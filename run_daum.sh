#!/bin/bash
# 다음앱 트래픽 (맥/Linux)
cd "$(dirname "$0")"
export PATH="$(pwd)/platform-tools:$PATH"
python3 daum_search_device.py "$@"
