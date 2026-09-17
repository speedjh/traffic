# -*- coding: utf-8 -*-
"""기기 상태·유심 데이터 계량·화면 방해요소 해제 (ADB).

- data_counter(): 유심(모바일) 데이터 누적 바이트. /proc/net/dev 의 rmnet/ccmni 논리 IF 합.
  * rmnet_ipa0 / rmnet_mhi0 은 물리 집계 IF 라 중복이므로 제외.
  * v4-rmnet* (464xlat) 도 기반 IF 에 이미 집계되므로 제외.
- device_info(): 배터리/충전/화면/모델/IP/통신사
- clear_screen_blockers(): 삼성 오동작 방지 필터·MTP 팝업 등 화면 위 방해 요소 제거
- screenshot_jpeg_b64(): 대시보드용 축소 캡처 (PC 회선으로 업로드 — 유심 데이터 무관)
"""
from __future__ import annotations

import base64
import io
import re
import subprocess
from pathlib import Path
from typing import Dict, Optional

_ROOT = Path(__file__).resolve().parent


def _adb_bin() -> str:
    for p in (_ROOT / "platform-tools" / "adb.exe", _ROOT / "platform-tools" / "adb",
              Path(r"C:\Android\platform-tools\adb.exe")):
        if p.is_file():
            return str(p)
    return "adb"


ADB = _adb_bin()

# 유심 데이터가 흐르는 논리 인터페이스 (물리 집계 IF·clat 은 중복이라 제외)
_DATA_IF = re.compile(r"(rmnet_data\d+|ccmni\d+|rmnet\d+|wwan\d+)$")
_DEV_LINE = re.compile(r"\s*(\S+):\s*(\d+)(?:\s+\d+){7}\s+(\d+)")


def _sh(serial: str, *args: str, timeout: float = 15.0) -> str:
    try:
        r = subprocess.run([ADB, "-s", serial, *args], capture_output=True, timeout=timeout)
        return (r.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return ""


def data_counter(serial: str) -> Optional[int]:
    """모바일 데이터 누적 바이트(rx+tx). 실패 시 None."""
    out = _sh(serial, "shell", "cat", "/proc/net/dev")
    if not out:
        return None
    total = 0
    found = False
    for line in out.splitlines():
        m = _DEV_LINE.match(line)
        if not m:
            continue
        name = m.group(1)
        if name.startswith("v4-"):
            continue
        if not _DATA_IF.fullmatch(name):
            continue
        total += int(m.group(2)) + int(m.group(3))
        found = True
    return total if found else None


def device_info(serial: str) -> Dict[str, object]:
    info: Dict[str, object] = {}
    bat = _sh(serial, "shell", "dumpsys", "battery")
    m = re.search(r"level:\s*(\d+)", bat)
    if m:
        info["battery"] = int(m.group(1))
    m = re.search(r"status:\s*(\d+)", bat)
    ac = re.search(r"AC powered:\s*(\w+)", bat)
    usb = re.search(r"USB powered:\s*(\w+)", bat)
    charging = (m and m.group(1) in ("2", "5")) or (ac and ac.group(1) == "true") or (usb and usb.group(1) == "true")
    info["charging"] = bool(charging)
    pw = _sh(serial, "shell", "dumpsys", "power")
    info["screen_on"] = "mWakefulness=Awake" in pw
    info["model"] = (_sh(serial, "shell", "getprop", "ro.product.model") or "").strip()
    info["carrier"] = (_sh(serial, "shell", "getprop", "gsm.operator.alpha") or "").strip()[:20]
    ip = ""
    for line in _sh(serial, "shell", "ip", "-o", "-4", "addr").splitlines():
        m = re.search(r"\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)", line)
        if m and _DATA_IF.fullmatch(m.group(1)) and not m.group(2).startswith("192.0.0."):
            ip = m.group(2)
            break
    info["ip"] = ip
    return info


def current_focus(serial: str) -> str:
    out = _sh(serial, "shell", "dumpsys", "window")
    m = re.search(r"mCurrentFocus=Window\{[^}]*?\s(\S+)\}", out)
    return m.group(1) if m else ""


# 화면을 덮어 자동화를 막는 창들
_POCKET_HINTS = ("UnintentionalLcdOn", "unintentional")
_POPUP_HINTS = ("MtpPopupActivity", "UsbConfirmActivity", "UsbDebuggingActivity")


def disable_pocket_mode(serial: str) -> None:
    """삼성 오동작 방지 필터(주머니 모드) 비활성화 — 자동화 중 화면 잠김 방지."""
    _sh(serial, "shell", "settings", "put", "system", "screen_off_pocket", "0")
    _sh(serial, "shell", "settings", "put", "system", "pocket_mode", "0")


def clear_screen_blockers(serial: str, log=None) -> str:
    """화면 위 방해 창을 치운다. 처리한 내용을 문자열로 돌려준다(없으면 '')."""
    _log = log or (lambda m: None)
    focus = current_focus(serial)
    acted = ""
    if any(h in focus for h in _POCKET_HINTS):
        # "터치 방지를 해제하려면 위로 미세요"
        disable_pocket_mode(serial)
        for _ in range(3):
            _sh(serial, "shell", "input", "swipe", "540", "1700", "540", "500", "250")
            if not any(h in current_focus(serial) for h in _POCKET_HINTS):
                break
        acted = "오동작방지 필터 해제"
    elif any(h in focus for h in _POPUP_HINTS):
        _sh(serial, "shell", "input", "keyevent", "KEYCODE_BACK")
        _sh(serial, "shell", "input", "keyevent", "KEYCODE_HOME")
        acted = f"시스템 팝업 닫음({focus.split('/')[-1]})"
    if acted:
        _log(f"[*] 화면 방해 요소 처리: {acted}")
    return acted


def screenshot_jpeg_b64(serial: str, width: int = 300, quality: int = 55) -> Optional[str]:
    """화면 캡처 → 축소 JPEG base64 (약 15~30KB)."""
    try:
        r = subprocess.run([ADB, "-s", serial, "exec-out", "screencap", "-p"],
                           capture_output=True, timeout=25)
        raw = r.stdout or b""
        if len(raw) < 1000:
            return None
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        h = max(1, int(im.height * width / im.width))
        im = im.resize((width, h), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def toggle_data(serial: str, off_secs: float = 4.0) -> None:
    import time
    _sh(serial, "shell", "svc", "data", "disable")
    time.sleep(off_secs)
    _sh(serial, "shell", "svc", "data", "enable")


def reboot(serial: str) -> None:
    _sh(serial, "reboot", timeout=20.0)


def dim_screen(serial: str, level: int = 12) -> None:
    """화면 밝기 최소화 — 24시간 구동 시 배터리/발열 절감 (자동화 동작에는 영향 없음)."""
    _sh(serial, "shell", "settings", "put", "system", "screen_brightness_mode", "0")
    _sh(serial, "shell", "settings", "put", "system", "screen_brightness", str(level))
