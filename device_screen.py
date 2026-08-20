# -*- coding: utf-8 -*-
"""화면 켜짐 유지 + 잠금화면 PIN 해제 (ADB).

환경변수:
  DEVICE_PIN  — 잠금 PIN (미설정 시 잠금해제는 스킵, 화면 유지만)
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent
_LOCAL_ADB = _ROOT / "platform-tools" / "adb"
ADB_EXE = str(_LOCAL_ADB) if _LOCAL_ADB.is_file() else "adb"

# 최대 화면 타임아웃 (약 24일). USB 연결 시 stay_on 과 함께 씀.
SCREEN_OFF_MS = 2_147_483_647
# AC=1 USB=2 WIRELESS=4 → 전부
STAY_ON_WHILE_PLUGGED = 7


def _adb(serial: str, *args: str, timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [ADB_EXE, "-s", serial, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _adb_out(serial: str, *args: str) -> str:
    try:
        r = _adb(serial, *args)
        return (r.stdout or "") + (r.stderr or "")
    except Exception:
        return ""


def device_pin() -> str:
    pin = (os.environ.get("DEVICE_PIN") or os.environ.get("LOCK_PIN") or "").strip()
    if pin:
        return pin
    # 로컬 전용 파일 (gitignore 권장) — run 스크립트 env 유실 대비
    for name in ("device_pin.local", ".device_pin"):
        p = Path(__file__).resolve().parent / name
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        except Exception:
            pass
    return ""


def keep_screen_on(serial: str) -> None:
    """가능한 한 화면이 꺼지지 않게 설정 (USB 연결 기준)."""
    try:
        _adb(serial, "shell", "settings", "put", "system", "screen_off_timeout", str(SCREEN_OFF_MS))
        _adb(serial, "shell", "settings", "put", "global", "stay_on_while_plugged_in", str(STAY_ON_WHILE_PLUGGED))
        # USB 연결 중 화면 유지 (충전/디버깅)
        _adb(serial, "shell", "svc", "power", "stayon", "usb")
        _adb(serial, "shell", "svc", "power", "stayon", "true")
    except Exception:
        pass


def is_screen_on(serial: str) -> bool:
    out = _adb_out(serial, "shell", "dumpsys", "power")
    if "mWakefulness=Awake" in out:
        return True
    if "mWakefulness=Asleep" in out or "mWakefulness=Dozing" in out:
        return False
    # fallback
    out2 = _adb_out(serial, "shell", "dumpsys", "display")
    return "mScreenState=ON" in out2 or "mState=ON" in out2


def is_locked(serial: str) -> bool:
    out = _adb_out(serial, "shell", "dumpsys", "window")
    if "mDreamingLockscreen=true" in out or "isStatusBarKeyguard=true" in out:
        return True
    if "mShowingLockscreen=true" in out:
        return True
    kg = _adb_out(serial, "shell", "dumpsys", "statusbar")
    if "Keyguard showing: true" in kg or "mKeyguardShowing=true" in kg:
        return True
    return False


def wake_screen(serial: str) -> None:
    _adb(serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP")
    time.sleep(0.4)
    # 일부 기기: 전원 키 토글 대신 wake만으로 부족할 때
    if not is_screen_on(serial):
        _adb(serial, "shell", "input", "keyevent", "26")  # POWER
        time.sleep(0.5)


def _swipe_up(serial: str) -> None:
    # 커버/폴드 해상도 대응 — 하단→상단 스와이프
    _adb(serial, "shell", "input", "swipe", "540", "2000", "540", "600", "300")
    time.sleep(0.4)


def _enter_pin_digits(serial: str, pin: str) -> None:
    """잠금화면은 input text 가 막히는 경우가 많아 keyevent 로 입력."""
    for ch in pin:
        if not ch.isdigit():
            continue
        # KEYCODE_0=7 ... KEYCODE_9=16
        code = 7 + int(ch)
        _adb(serial, "shell", "input", "keyevent", str(code))
        time.sleep(0.12)
    time.sleep(0.2)
    _adb(serial, "shell", "input", "keyevent", "66")  # ENTER
    time.sleep(0.5)


def unlock_with_pin(serial: str, pin: Optional[str] = None) -> bool:
    pin = (pin if pin is not None else device_pin()).strip()
    if not pin:
        return False
    wake_screen(serial)
    time.sleep(0.3)
    _swipe_up(serial)
    # 메뉴 키로 잠금 UI 올리는 경우
    _adb(serial, "shell", "input", "keyevent", "82")
    time.sleep(0.35)
    _enter_pin_digits(serial, pin)
    # 한 번 더 실패 시 재시도
    if is_locked(serial):
        _swipe_up(serial)
        time.sleep(0.3)
        _enter_pin_digits(serial, pin)
    locked = is_locked(serial)
    return not locked


def ensure_screen_ready(serial: str, *, pin: Optional[str] = None, log=None) -> None:
    """화면 유지 설정 + 꺼져 있으면 깨우고 + 잠기면 PIN 해제."""
    _log = log or (lambda m: None)
    keep_screen_on(serial)
    if not is_screen_on(serial):
        _log("[*] 화면 OFF → 깨우기")
        wake_screen(serial)
        time.sleep(0.5)
    # 잠금 여부 — 느린 dumpsys이므로 화면 직후만 검사
    try:
        locked = is_locked(serial)
    except Exception:
        locked = False
    if locked:
        use_pin = (pin if pin is not None else device_pin()).strip()
        if use_pin:
            _log("[*] 잠금화면 → PIN 해제")
            ok = unlock_with_pin(serial, use_pin)
            _log("[+] 잠금 해제" if ok else "[!] 잠금 해제 실패(재시도는 다음 주기)")
        else:
            # PIN/비밀번호 없음 — 스와이프만으로 잠금 해제
            _log("[*] 잠금화면 → 스와이프 해제 (PIN 없음)")
            wake_screen(serial)
            time.sleep(0.2)
            _swipe_up(serial)
            _adb(serial, "shell", "input", "keyevent", "82")  # MENU
            time.sleep(0.35)
            if is_locked(serial):
                _swipe_up(serial)
                time.sleep(0.3)
            ok = not is_locked(serial)
            _log("[+] 잠금 해제" if ok else "[!] 스와이프 해제 실패(재시도는 다음 주기)")
    # 유지 설정 재적용 (앱이 바꿀 수 있음)
    keep_screen_on(serial)
