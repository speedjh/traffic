#!/bin/bash
# 함많찾을 (Chrome→네이버 검색) 트래픽
cd "$(dirname "$0")"
export PATH="$(pwd)/platform-tools:$PATH"
python3 hamman_device.py "$@"
