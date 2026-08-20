#!/bin/bash
# 우리동네GS 클릭형 트래픽 (맥/Linux)
cd "$(dirname "$0")"
export PATH="$(pwd)/platform-tools:$PATH"
python3 gs_click_device.py "$@"

