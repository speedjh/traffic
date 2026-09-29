# -*- coding: utf-8 -*-
"""
[실물 폴드 기기용] 다음앱 검색 → 장소 클릭 → 카카오맵 업체 클릭·스크롤 자동화.
(카카오맵 전환만 필요하면 daum_click_device.py / 탭 체류는 daum_dwell_device.py)

플로우 (1 로테이션):
    다음앱 실행 → 검색어 입력 → '장소' 섹션까지 스크롤
    → --place-url 로 지정한 카카오맵 장소 클릭 (다음앱 장소 섹션)
    → 카카오맵 전환 후 업체 클릭 + 상세 스크롤 (kakao_search_device.py 검색 트래픽과 동일)
    → 다음앱·카카오맵 종료 → 모바일 데이터 OFF/ON → 두 앱 pm clear

장소 매칭:
    place.map.kakao.com URL 에서 place ID 추출.
    UI 덤프에 ID 가 없으면 URL og:title 로 업체명을 조회해
    '장소' 섹션 아래 content-desc 일치 항목을 클릭한다.

사용법:
    python daum_search_device.py --place-url "https://place.map.kakao.com/22122997"
    python daum_search_device.py "영월 닭강정 가나닭강정" --place-url "https://place.map.kakao.com/22122997"
    python daum_search_device.py ... --serial R3CY80RW96Y --rotations 5

요구사항:
    pip install uiautomator2
    adb devices 에 기기 연결
"""
import os
import re
import sys
import time
import random
import argparse
import subprocess
import xml.etree.ElementTree as ET
import urllib.request
from datetime import datetime
from typing import Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ── 타임스탬프 로그 (로테이션 구간은 +경과초 함께 표시) ─────────────────────
_cycle_t0: Optional[float] = None


def begin_cycle_timer():
    global _cycle_t0
    _cycle_t0 = time.time()


def end_cycle_timer():
    global _cycle_t0
    _cycle_t0 = None


def log(msg: str = ""):
    ts = datetime.now().strftime("%H:%M:%S")
    if _cycle_t0 is not None:
        elapsed = time.time() - _cycle_t0
        print(f"[{ts} +{elapsed:6.1f}s] {msg}", flush=True)
    else:
        print(f"[{ts}] {msg}", flush=True)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_KAKAO_MODULE_DIR = os.path.join(_SCRIPT_DIR, "CLAUDE")
if _KAKAO_MODULE_DIR not in sys.path:
    sys.path.insert(0, _KAKAO_MODULE_DIR)
_LOCAL_ADB = os.path.join(_SCRIPT_DIR, "platform-tools", "adb")
LD_DIR = r"C:\LDPlayer\LDPlayer9"
if os.path.isdir(LD_DIR):
    os.environ["PATH"] = LD_DIR + os.pathsep + os.environ.get("PATH", "")
_LD_ADB = os.path.join(LD_DIR, "adb.exe")
if os.path.isfile(_LOCAL_ADB):
    ADB_EXE = _LOCAL_ADB
elif os.path.isfile(_LD_ADB):
    ADB_EXE = _LD_ADB
else:
    ADB_EXE = "adb"

import uiautomator2 as u2  # noqa: E402

DAUM_PKG = "net.daum.android.daum"
DAUM_MAIN = f"{DAUM_PKG}/.DaumActivity"
MAP_PKG = "net.daum.android.map"
MAP_RID = lambda name: f"{MAP_PKG}:id/{name}"  # noqa: E731
COVER_DISPLAY = 0
CLOSED_STATE = 0
DEFAULT_SERIAL = "R3CY80RW96Y"
DEFAULT_KEYWORD = "동대문맛집 왕코등갈비본점"
DEFAULT_PLACE_URL = "https://place.map.kakao.com/22122997?referrer=daumsearch_local"
# URL 조회(맥 인터넷/DNS) 실패 시 place ID 로 폴백
KNOWN_PLACE_NAMES = {
    "22122997": "영월가나닭강정",
}

POLL_FAST = 0.12              # UI 준비 폴링 (네트워크 구간은 timeout 으로 흡수)
POLL_SLOW = 0.35              # dump·스크롤 등 무거운 작업 간격
DISMISS_INTERVAL = 2.0        # 팝업 동기 처리 최소 간격 (매 루프 dump 방지)
PLACE_SECTION_TIMEOUT = 45.0  # 장소 섹션·카카오맵 로딩 (네트워크 지연 허용)
MAP_CONTENT_STABLE = 0.12     # 업체 UI 확인 직후 짧은 안정화
DUMP_FALLBACK_EVERY = 4       # u2 빠른 경로 실패 N회마다 dump 1회
SEARCH_CHAR_DELAY = 0.05      # 다음앱 검색 — 글자별 입력(리얼 타이핑)
SEARCH_POST_TYPE_DELAY = 0.5  # 입력 완료 후 검색 실행 전
SEARCH_POST_ENTER_DELAY = 1.5 # 엔터 후 결과 로딩 대기

# ── 팝업/권한: 무조건 거부·건너뛰기 (허용/동의 버튼은 절대 넣지 말 것) ────────
PERMISSION_DENY_TEXTS = [
    "허용 안함", "허용 안 함", "허용안함", "허용하지 않음", "허용 하지 않음",
    "이번에는 허용 안함", "이번만 허용 안함", "다시 묻지 않음",
    "Don't allow", "Don't Allow", "Deny", "DENY", "Not now",
    "거부", "거절", "받지 않기", "알림 받지 않기",
]

POPUP_SKIP_TEXTS = [
    "비로그인으로 시작하기",
    "다음 시작하기",           # 다음앱 '접근 권한 안내' — 권한 켜지 않고 진행
    "오늘 그만보기", "오늘은 그만보기", "오늘 하루 보지 않기",
    "다시 보지 않기", "다시 보지않기", "다시 안 보기",
    "이 창 다시 보지 않기", "나중에 하기", "건너뛰기",
    "지금은 아니에요", "나중에", "나중에 받기", "다음에",
    "아니요", "괜찮습니다",
    "취소",                    # 권한/알림 제안에서 거부 쪽
]

# 안드로이드 시스템 권한 다이얼로그 거부 버튼 (resource-id)
PERMISSION_DENY_RIDS = [
    "com.android.permissioncontroller:id/permission_deny_button",
    "com.android.permissioncontroller:id/permission_deny_and_dont_ask_again_button",
    "com.android.permissioncontroller:id/permission_deny_radio_button",
    "com.android.permissioncontroller:id/permission_deny_and_dont_ask_again_radio_button",
    "com.samsung.android.permissioncontroller:id/permission_deny_button",
    "com.samsung.android.permissioncontroller:id/permission_deny_and_dont_ask_again_button",
]

BLOG_SKIP_HINTS = (
    "블로그", "네이버", "blog.naver", "통합웹 더보기",
    "카톡공유", "현위치", "솔직 후기",
)

# 검색 결과에서 "클릭하면 안 되는" UI(탭/공유/버튼류) 제외 키워드
SEARCH_RESULT_EXCLUDE_HINTS = (
    "검색어 지우기",
    "옵션",
    "기간필터",
    "통합웹 더보기",
    "카톡공유",
    "현위치",
    "장소",          # '장소' 섹션 타이틀/버튼
    "장소정보",
    "리뷰",
    "전화",
    "길찾기",
    "공유",
    "지도 보기",
    "카카오맵(외부 앱으로 연결)",
    "MY메뉴",
    "메뉴 더보기",
    "홈으로 이동",
    "이전 페이지 이동",
    "다음 페이지 이동",
)

# 자완: XML subtree 기준 제외 구역 (파워링크·관련검색어·장소·트렌드)
# n0vlColl(구) / n0nlColl(신) = 파워링크·관련광고
JAWAN_EXCLUDED_SECTION_RIDS = frozenset({
    "rtlColl",
    "l7tColl",
    "n0vlColl",
    "n0nlColl",
    "netizenColl_top",
    "netizenColl_bottom",
    "netizen_lists_top",
    "netizen_lists_bottom",
})

# 자완: 섹션 밖 UI 버튼/탭 (글 제목과 겹치지 않게 좁게)
JAWAN_UI_EXCLUDE_HINTS = (
    "검색어 지우기",
    "옵션",
    "기간필터",
    "카톡공유",
    "현위치",
    "공유하기메뉴",
    "관련 검색어",
    "관련광고",
    "관련 파워링크",
    "파워링크",
    "파워링크 광고",
    "실시간 트렌드",
    "Daum 메인페이지로 가기",
    "MY메뉴",
    "메뉴 더보기",
    "홈으로 이동",
    "검색 더보기",
    "트렌드 랭킹 더보기",
    "통합웹 더보기",
    "이전 페이지 이동",
    "다음 페이지 이동",
    "유저썸네일",
)

JAWAN_TAB_LABELS = frozenset({
    "통합", "통합웹", "동영상", "이미지", "뉴스", "전체", "블로그", "카페", "웹문서",
})


def _bounds_center(bounds: str):
    x1, y1, x2, y2 = map(int, re.findall(r"-?\d+", bounds)[:4])
    return (x1 + x2) // 2, (y1 + y2) // 2


def _click_node(d: u2.Device, n) -> bool:
    bounds = n.get("bounds", "")
    if not bounds:
        return False
    cx, cy = _bounds_center(bounds)
    d.click(cx, cy)
    return True


def _click_by_text(d: u2.Device, text: str, exact: bool = True) -> bool:
    root = ET.fromstring(d.dump_hierarchy())
    for n in root.iter("node"):
        t = (n.get("text") or "").strip()
        desc = (n.get("content-desc") or "").strip()
        hit = (t == text if exact else text in t or text in desc)
        if not hit:
            continue
        if n.get("clickable") == "true":
            return _click_node(d, n)
        if bounds := n.get("bounds", ""):
            cx, cy = _bounds_center(bounds)
            d.click(cx, cy)
            return True
    return False


def _safe_exists(d: u2.Device, timeout: float = 0, **kwargs) -> bool:
    try:
        return d(**kwargs).exists(timeout=timeout)
    except Exception:
        return False


def _safe_click(d: u2.Device, timeout: float = 0, **kwargs) -> bool:
    try:
        el = d(**kwargs)
        if el.exists(timeout=timeout):
            el.click()
            return True
    except Exception:
        pass
    return False


class _Throttle:
    """단계별 팝업·dump 호출 빈도 제한 (폴드에서 dump 는 0.5~2s 소요)."""

    def __init__(self):
        self._last_dismiss = 0.0

    def dismiss(self, d: u2.Device, label: str = "", interval: float = DISMISS_INTERVAL) -> int:
        now = time.time()
        if now - self._last_dismiss < interval:
            return 0
        self._last_dismiss = now
        return dismiss_popups_now(d, label)


def _wait_until(check, timeout: float, poll: float = POLL_FAST,
                throttle: Optional[_Throttle] = None, dismiss_label: str = "",
                d: Optional[u2.Device] = None) -> bool:
    """조건 충족까지 폴링. 네트워크는 timeout, 준비되면 즉시 통과."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if throttle and d and dismiss_label:
            throttle.dismiss(d, dismiss_label)
        if check():
            return True
        time.sleep(poll)
    return False


def _watcher_pause(d: u2.Device):
    try:
        d.watcher.stop()
    except Exception:
        pass


def _watcher_resume(d: u2.Device):
    try:
        d.watcher.start(interval=1.0)
    except Exception:
        pass


def dismiss_popups_now(d: u2.Device, label: str = "") -> int:
    """화면에 보이는 권한/팝업을 즉시 거부·건너뛰기. 처리 건수 반환."""
    _watcher_pause(d)
    try:
        return _dismiss_popups_impl(d, label)
    finally:
        _watcher_resume(d)


def _dismiss_popups_impl(d: u2.Device, label: str = "", *, _depth: int = 0, _budget: int = 4) -> int:
    """권한/팝업 1회 스윕. 동일 버튼 무한 재클릭 방지(_budget)."""
    if _depth >= _budget:
        return 0
    acted = 0
    prefix = f"[팝업{(' '+label) if label else ''}]"

    for rid in PERMISSION_DENY_RIDS:
        if _safe_click(d, resourceId=rid):
            acted += 1
            log(f"{prefix} 시스템 권한 거부 ({rid.split('/')[-1]})")
            time.sleep(0.8)
            return acted + _dismiss_popups_impl(d, label, _depth=_depth + 1, _budget=_budget)

    for text in PERMISSION_DENY_TEXTS:
        if _safe_click(d, text=text):
            acted += 1
            log(f"{prefix} 권한 거부: '{text}'")
            time.sleep(0.8)
            return acted + _dismiss_popups_impl(d, label, _depth=_depth + 1, _budget=_budget)

    for text in POPUP_SKIP_TEXTS:
        if _safe_click(d, text=text):
            acted += 1
            log(f"{prefix} 건너뛰기: '{text}'")
            time.sleep(0.8)
            # '건너뛰기' 등이 사라져야 함. 남으면 재귀 budget으로 멈춤.
            return acted + _dismiss_popups_impl(d, label, _depth=_depth + 1, _budget=_budget)

    try:
        root = ET.fromstring(d.dump_hierarchy())
        screen_text = ET.tostring(root, encoding="unicode")
    except Exception:
        screen_text = ""

    # 다음앱 '접근 권한 안내' — 스위치 건드리지 않고 하단 '다음 시작하기'만 탭
    if "접근 권한 안내" in screen_text:
        if _click_by_text(d, "다음 시작하기"):
            acted += 1
            log(f"{prefix} 다음앱 권한 안내 → '다음 시작하기'")
            time.sleep(1.0)
            return acted + _dismiss_popups_impl(d, label, _depth=_depth + 1, _budget=_budget)

    # 카카오맵 머티리얼 다이얼로그 — negative(거부)만
    if _safe_click(d, resourceId=MAP_RID("md_button_negative")):
        acted += 1
        log(f"{prefix} 카카오맵 다이얼로그 거부")
        time.sleep(0.8)
        return acted + _dismiss_popups_impl(d, label, _depth=_depth + 1, _budget=_budget)

    if _safe_exists(d, textContains="계정을 선택"):
        try:
            d.press("back")
            acted += 1
            log(f"{prefix} 계정 선택 → 뒤로")
            time.sleep(0.8)
        except Exception:
            pass

    return acted


def setup_popup_watchers(d: u2.Device):
    """백그라운드 watcher — dismiss_popups_now 와 겹치지 않는 항목만 등록.

    시스템 권한/건너뛰기는 메인 스레드에서만 처리한다. watcher 에서 동시에
    permission_deny_button 등을 wait 하면 UI 전환 중 -32002 RPC 충돌이 난다.
    """
    try:
        d.watcher.reset()
    except Exception:
        pass

    d.watcher("account_select").when(
        '//*[contains(@text,"계정을 선택")]'
    ).press("back")

    d.watcher.start(interval=1.0)
    log("[*] 팝업 watcher 활성화 (계정선택만, 권한/건너뛰기는 동기 처리)")


def _adb(serial: str, *args, timeout: float = 25.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [ADB_EXE, "-s", serial, *args],
        capture_output=True, text=True, timeout=timeout,
    )


PUBLIC_IP_URLS = (
    "https://api.ipify.org",
    "https://icanhazip.com",
    "https://ifconfig.me/ip",
)


def get_public_ip(serial: str, curl_timeout: int = 5) -> str:
    """기기의 현재 공인 IPv4 조회. 한 곳이 느리거나 막혀도 연결을 '끊김'으로 오판하지 않도록
    여러 조회처를 순서대로 시도한다. 실패 시 빈 문자열."""
    for url in PUBLIC_IP_URLS:
        try:
            r = _adb(serial, "shell", "curl", "-s", "-4", "--max-time", str(curl_timeout),
                     url, timeout=curl_timeout + 5)
            ip = (r.stdout or "").strip()
            if ip and re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
                return ip
        except Exception:
            continue
    return ""


def network_ok(serial: str, timeout: int = 6) -> bool:
    """공인 IP 조회가 안 되더라도 실제 통신이 되는지 확인 (네이버 응답 코드).
    IP 에코 서비스가 느리거나 막혀 '연결 끊김'으로 오판하는 것을 막는다."""
    for url in ("https://m.naver.com", "https://www.google.com/generate_204"):
        try:
            r = _adb(serial, "shell", "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     "--max-time", str(timeout), url, timeout=timeout + 5)
            code = (r.stdout or "").strip()[-3:]
            if code.isdigit() and int(code) in (200, 204, 301, 302):
                return True
        except Exception:
            continue
    return False


def _mobile_data_enabled(serial: str) -> bool:
    """settings global mobile_data 상태 (1=켜짐)."""
    try:
        r = _adb(serial, "shell", "settings", "get", "global", "mobile_data", timeout=5)
        return (r.stdout or "").strip().lower() in ("1", "true")
    except Exception:
        return False


def _adb_device_online(serial: str) -> bool:
    """adb devices 목록에 serial 이 device 상태인지."""
    try:
        r = subprocess.run(
            [ADB_EXE, "devices"], capture_output=True, text=True, timeout=8,
        )
    except Exception:
        return False
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == serial and parts[1] == "device":
            return True
    return False


def ensure_network(serial: str, timeout: float = 20.0) -> bool:
    """모바일 데이터 ON + 공인 IP 조회로 연결 판정 (kakao_search_device.py 와 동일).

    ping 은 갤럭시 등에서 LTE 가 켜져 있어도 adb shell 에서 실패하는 경우가
    많아 사용하지 않는다. IP 미조회 시 svc data 를 반복 호출하지 않고 대기만 한다.
    ADB 단절 시 즉시 실패(장시간 hang 방지)."""
    if not _adb_device_online(serial):
        log("[!] 데이터/IP 연결 확인 실패 (adb device offline/not found)")
        return False

    if not _mobile_data_enabled(serial):
        log("[*] 모바일 데이터 OFF 감지 → ON")
        _adb(serial, "shell", "svc", "data", "enable", timeout=8)
        time.sleep(2.0)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _adb_device_online(serial):
            log("[!] 데이터/IP 연결 확인 실패 (adb device offline/not found)")
            return False
        if not _mobile_data_enabled(serial):
            _adb(serial, "shell", "svc", "data", "enable", timeout=8)
            time.sleep(1.5)
            continue
        ip = get_public_ip(serial, curl_timeout=4)
        if ip:
            log(f"[+] 데이터·IP 연결 확인 ({ip})")
            return True
        time.sleep(2.0)

    state = "ON" if _mobile_data_enabled(serial) else "OFF"
    log(f"[!] 데이터/IP 연결 확인 실패 (mobile_data={state}, LTE ON 이어도 IP 미조회)")
    return False


def toggle_mobile_data(serial: str, off_secs: float = 4.0,
                       recover_timeout: float = 25.0) -> bool:
    """모바일 데이터 OFF 1회 → ON 1회 → IP 조회로 복구 확인. 이중 OFF 방지."""
    was_on = _mobile_data_enabled(serial)
    ip_before = get_public_ip(serial) if was_on else ""
    log(f"[*] 변경 전 IP: {ip_before or '(조회 실패 / OFF)'}")

    if was_on:
        log("[*] 모바일 데이터 OFF (svc data disable, 1회)")
        _adb(serial, "shell", "svc", "data", "disable")
        time.sleep(off_secs)
    else:
        log("[*] 데이터 이미 OFF — OFF 단계 생략 (이중 OFF 방지)")

    log("[*] 모바일 데이터 ON (svc data enable, 1회)")
    _adb(serial, "shell", "svc", "data", "enable")

    if not ensure_network(serial, timeout=recover_timeout):
        log("[!] 복구 지연 — enable 1회 재시도")
        _adb(serial, "shell", "svc", "data", "enable")
        if not ensure_network(serial, timeout=15.0):
            ip_retry = get_public_ip(serial)
            if ip_retry:
                log(f"[+] IP 직접 조회 성공 ({ip_retry}) — 연결 OK")
            else:
                log("[!] 복구 실패 — 다음 로테이션에서 재시도")

    ip_after = get_public_ip(serial)
    log(f"[*] 변경 후 IP: {ip_after or '(조회 실패)'}")
    changed = bool(ip_after) and ip_after != ip_before
    mark = "+" if changed else "!"
    status = "성공" if changed else ("실패(동일)" if ip_after else "실패(조회불가)")
    log(f"[{mark}] IP 변경 {status}: {ip_before or '?'} → {ip_after or '?'}")
    return changed


def connect(serial: str) -> u2.Device:
    log(f"[*] 기기 연결: {serial}")
    try:
        from device_screen import ensure_screen_ready
        ensure_screen_ready(serial, log=log)
    except Exception as e:
        log(f"[!] 화면 준비 스킵: {e}")
    d = u2.connect(serial)
    d.settings["wait_timeout"] = 15.0
    log(f"[+] 연결됨: {d.info.get('productName')} / {d.serial} / {d.window_size()}")
    return d


def parse_place_id(place_url: str) -> str:
    m = re.search(r"place\.map\.kakao\.com/(\d+)", place_url or "")
    return m.group(1) if m else ""


def resolve_place_name(place_url: str, timeout: float = 15.0) -> str:
    """카카오맵 장소 URL → og:title 업체명."""
    req = urllib.request.Request(
        place_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; daum-traffic/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        html = resp.read().decode("utf-8", "replace")
    m = re.search(r'property="og:title"\s+content="([^"]+)"', html)
    if not m:
        raise RuntimeError(f"og:title 을 찾지 못했습니다: {place_url}")
    return m.group(1).strip()


def _place_section_y(root: ET.Element) -> Optional[int]:
    for n in root.iter("node"):
        if (n.get("text") or "").strip() == "장소":
            return int(re.findall(r"-?\d+", n.get("bounds", ""))[1])
    return None


def _is_blog_node(desc: str, text: str) -> bool:
    blob = f"{desc} {text}"
    return any(h in blob for h in BLOG_SKIP_HINTS)


def find_place_click_target(root: ET.Element, place_id: str, place_name: str):
    """장소 섹션 아래 목표 업체 클릭 대상 노드 반환."""
    if place_id:
        for n in root.iter("node"):
            if n.get("clickable") != "true":
                continue
            blob = " ".join([
                n.get("text", ""), n.get("content-desc", ""), n.get("resource-id", ""),
            ])
            if place_id in blob:
                return n, "place_id"

    sec_y = _place_section_y(root)
    if sec_y is None:
        return None, "no_section"

    best = None
    for n in root.iter("node"):
        if n.get("clickable") != "true":
            continue
        bounds = n.get("bounds", "")
        if not bounds:
            continue
        y1 = int(re.findall(r"-?\d+", bounds)[1])
        if y1 < sec_y:
            continue
        desc = (n.get("content-desc") or "").strip()
        text = (n.get("text") or "").strip()
        if _is_blog_node(desc, text):
            continue
        if desc == place_name or text == place_name:
            return n, "name_exact"
        if desc and place_name in desc and len(desc) <= len(place_name) + 30:
            best = (n, "name_desc")
    return best if best else (None, "not_found")


def _daum_main_ready(d: u2.Device) -> bool:
    return (_safe_exists(d, description="검색창")
            or _safe_exists(d, text="검색어 입력"))


def _daum_results_ready(d: u2.Device) -> bool:
    """검색 결과 페이지 로딩 — '장소' 섹션 또는 통합검색 결과 UI."""
    if _safe_exists(d, text="장소"):
        return True
    if _safe_exists(d, textContains="검색결과"):
        return True
    if _safe_exists(d, text="통합") and not _safe_exists(d, text="검색어 입력"):
        return True
    return False


def _try_click_place_fast(d: u2.Device, place_id: str, place_name: str) -> bool:
    """dump 없이 u2 selector 로 장소 항목 클릭 시도."""
    if place_id:
        for kwargs in (
            {"descriptionContains": place_id},
            {"textContains": place_id},
        ):
            if _safe_click(d, **kwargs):
                return True
    if place_name:
        if _safe_click(d, description=place_name):
            return True
        if _safe_click(d, descriptionContains=place_name):
            return True
    return False


def open_daum(d: u2.Device, serial: str):
    log("[*] 다음앱 실행 (커버화면 display 0)")
    try:
        from device_screen import ensure_screen_ready
        ensure_screen_ready(serial, log=log)
    except Exception as e:
        log(f"[!] 화면 준비 스킵: {e}")
    _adb(serial, "shell", "cmd", "device_state", "state", str(CLOSED_STATE))
    _adb(serial, "shell", "settings", "put", "secure", "location_mode", "0")
    _adb(serial, "shell", "am", "force-stop", DAUM_PKG)
    _adb(serial, "shell", "am", "force-stop", MAP_PKG)
    time.sleep(0.5)
    _adb(
        serial, "shell", "am", "start", "--display", str(COVER_DISPLAY),
        "-a", "android.intent.action.MAIN",
        "-c", "android.intent.category.LAUNCHER",
        "-n", DAUM_MAIN,
    )
    throttle = _Throttle()
    t0 = time.time()
    deadline = t0 + 40.0
    while time.time() < deadline:
        throttle.dismiss(d, "다음앱로딩")
        if _daum_main_ready(d):
            throttle.dismiss(d, "다음앱메인", interval=0)
            log(f"[+] 다음앱 메인 로딩 완료 ({time.time() - t0:.1f}s)")
            return
        time.sleep(POLL_FAST)
    log("[!] 다음앱 메인 로딩 지연")


def search_keyword(d: u2.Device, keyword: str, timeout: float = PLACE_SECTION_TIMEOUT):
    """다음앱 검색 — 글자별 타이핑 + 고정 대기 (다음 트래픽 원본 타이밍)."""
    log(f"[*] 검색: '{keyword}'")
    dismiss_popups_now(d, "검색전")
    if not _safe_exists(d, timeout=8, description="검색창"):
        # 온보딩/권한 직후 메인 미도달 — 한 번 더 팝업 처리 후 재시도
        dismiss_popups_now(d, "검색전재시도")
        time.sleep(1.0)
    if not _safe_exists(d, timeout=10, description="검색창"):
        raise RuntimeError("다음앱 검색창을 찾지 못했습니다.")
    _safe_click(d, description="검색창")
    time.sleep(1.0)
    if not _safe_click(d, text="검색어 입력"):
        _safe_click(d, description="통합검색")
    time.sleep(0.3)
    d.clear_text()
    for ch in keyword:
        d.send_keys(ch)
        time.sleep(SEARCH_CHAR_DELAY)
    time.sleep(SEARCH_POST_TYPE_DELAY)
    d.press("enter")
    time.sleep(SEARCH_POST_ENTER_DELAY)
    dismiss_popups_now(d, "검색후")
    log("[*] 검색 실행 완료")


def click_place_in_section(d: u2.Device, place_id: str, place_name: str,
                           timeout: float = PLACE_SECTION_TIMEOUT) -> bool:
    log(f"[*] 장소 섹션 탐색 (id={place_id or '-'}, name='{place_name}')")
    t0 = time.time()
    deadline = t0 + timeout
    scroll_i = 0
    throttle = _Throttle()
    fast_miss = 0
    last_log = t0

    while time.time() < deadline:
        throttle.dismiss(d, "장소탐색")

        if _try_click_place_fast(d, place_id, place_name):
            log(f"[+] 장소 클릭 (u2 빠른경로, {time.time() - t0:.1f}s)")
            return True

        fast_miss += 1
        use_dump = (fast_miss % DUMP_FALLBACK_EVERY == 0) or scroll_i == 0
        if use_dump:
            root = ET.fromstring(d.dump_hierarchy())
            node, how = find_place_click_target(root, place_id, place_name)
            if node is not None:
                bounds = node.get("bounds", "")
                cx, cy = _bounds_center(bounds)
                label = (node.get("content-desc") or node.get("text") or place_name)[:40]
                log(f"[+] 장소 클릭 ({how}): '{label}' @ {bounds} ({time.time() - t0:.1f}s)")
                d.click(cx, cy)
                return True

        scroll_i += 1
        now = time.time()
        if now - last_log >= 4.0:
            log(f"    ... 장소 섹션 탐색 중 ({now - t0:.0f}s, 스크롤 {scroll_i})")
            last_log = now
        d.swipe(0.5, 0.78, 0.5, 0.25, duration=0.3)
        time.sleep(POLL_SLOW)

    log(f"[!] 장소 '{place_name}' 를 찾지 못했습니다. (timeout {timeout:.0f}s)")
    return False


def _in_bounds(bounds: str, *, y_min: int, y_max: int) -> bool:
    try:
        y1 = int(re.findall(r"-?\d+", bounds)[1])
        y2 = int(re.findall(r"-?\d+", bounds)[3])
    except Exception:
        return False
    return y2 >= y_min and y1 <= y_max


def _node_label(n) -> str:
    return ((n.get("content-desc") or "") + " " + (n.get("text") or "")).strip()


def _parse_bounds(bounds: str):
    try:
        return tuple(map(int, re.findall(r"-?\d+", bounds)[:4]))
    except Exception:
        return None


def _bounds_height(bounds: str) -> int:
    rect = _parse_bounds(bounds)
    if not rect:
        return 0
    return rect[3] - rect[1]


def _section_rid_short(rid: str) -> str:
    if not rid:
        return ""
    return rid.rsplit("/", 1)[-1]


def _section_rid_excluded(rid: str) -> bool:
    short = _section_rid_short(rid)
    if not short:
        return False
    if short in JAWAN_EXCLUDED_SECTION_RIDS:
        return True
    # 파워링크류: n0vlColl / n0nlColl / n0xxColl 패턴
    if short.startswith("n0") and short.endswith("Coll"):
        return True
    return short.startswith("netizen")


def _jawan_ui_excluded(label: str, bounds: str) -> bool:
    if any(hint in label for hint in JAWAN_UI_EXCLUDE_HINTS):
        return True
    compact = label.strip()
    if compact in JAWAN_TAB_LABELS and _bounds_height(bounds) < 90:
        return True
    if compact in ("더보기", "펼쳐보기") and _bounds_height(bounds) < 120:
        return True
    # 관련검색어 칩(짧은 단어) 제외
    if len(compact) <= 12 and " " not in compact and "blog" not in compact.lower():
        if _bounds_height(bounds) < 100:
            return True
    return False


def _jawan_is_content_label(label: str) -> bool:
    """블로그/웹 본문·제목으로 보이는 라벨인지."""
    lab = (label or "").strip()
    if len(lab) < 16:
        return False
    low = lab.lower()
    if any(x in low for x in ("blog.naver", "tistory", "cafe.daum", "cafe.naver")):
        return True
    if any(x in lab for x in ("블로그", "카페", "뉴스", "웹문서", "후기", "리뷰", "인테리어", "맛집")):
        return True
    # 문장형 본문
    if len(lab) >= 24:
        return True
    return False


def _collect_jawan_candidates(root, *, y_min: int, y_max: int) -> list:
    """제외 구역 밖의 clickable 노드 수집 (WebView dump 기준)."""
    cands = []

    def walk(node, in_excluded: bool):
        rid = node.get("resource-id", "")
        if _section_rid_excluded(rid):
            in_excluded = True

        if (
            not in_excluded
            and node.get("package") == DAUM_PKG
            and node.get("clickable") == "true"
            and not (node.get("resource-id") or "").endswith(":id/back_button")
        ):
            bounds = node.get("bounds", "")
            label = _node_label(node)
            if (
                bounds
                and _parse_bounds(bounds)
                and _in_bounds(bounds, y_min=y_min, y_max=y_max)
                and len(label) >= 4
                and not _jawan_ui_excluded(label, bounds)
                and _jawan_is_content_label(label)
            ):
                cands.append((bounds, label[:100]))

        for child in node:
            walk(child, in_excluded)

    walk(root, False)

    # 중첩 clickable 중 긴 라벨(카드 본문) 우선, 겹치는 작은 노드는 제거
    cands.sort(key=lambda x: len(x[1]), reverse=True)
    picked = []
    used_centers = []
    for bounds, label in cands:
        cx, cy = _bounds_center(bounds)
        if any(abs(cx - px) < 40 and abs(cy - py) < 70 for px, py in used_centers):
            continue
        picked.append((bounds, label))
        used_centers.append((cx, cy))
    return picked


def _jawan_has_power_section(root) -> bool:
    for n in root.iter("node"):
        if _section_rid_excluded(n.get("resource-id", "")):
            short = _section_rid_short(n.get("resource-id", ""))
            if short.startswith("n0") and short.endswith("Coll"):
                return True
        lab = _node_label(n)
        if "파워링크" in lab or "관련광고" in lab:
            return True
    return False


def _jawan_dump_ui(d: u2.Device, *, max_depth: int = 50) -> Optional[ET.Element]:
    """자완용 UI dump (compressed + depth 제한으로 속도 우선)."""
    t0 = time.time()
    try:
        xml = d.dump_hierarchy(compressed=True, max_depth=max_depth)
        root = ET.fromstring(xml)
    except Exception:
        return None
    elapsed = time.time() - t0
    if elapsed > 1.5:
        log(f"    ... 자완 dump {elapsed:.1f}s (depth={max_depth})")
    return root


def _jawan_random_content_tap(d: u2.Device, *, y_min: int, y_max: int) -> tuple:
    """스크롤 후 본문 영역 랜덤 탭 (dump 없이)."""
    w, _ = d.window_size()
    x = random.randint(int(w * 0.10), int(w * 0.90))
    y = random.randint(y_min, y_max)
    d.click(x, y)
    return x, y


def _jawan_try_fast_taps(d: u2.Device, *, y_min: int, y_max: int,
                         attempts: int = 2) -> bool:
    """dump 없이 본문 영역 랜덤 탭 → 페이지 전환 확인."""
    for n in range(attempts):
        x, y = _jawan_random_content_tap(d, y_min=y_min, y_max=y_max)
        log(f"[+] 자완 빠른탭 {n + 1}/{attempts} @ ({x},{y})")
        if _jawan_result_page_opened(d, "", y_min=y_min, y_max=y_max, fast=True):
            return True
    return False


def _search_results_on_screen(root, *, y_min: int, y_max: int) -> bool:
    """검색결과 고정 구역(장소·관련검색어·파워링크)이 화면에 보이는지."""
    for n in root.iter("node"):
        if n.get("package") != DAUM_PKG:
            continue
        rid = _section_rid_short(n.get("resource-id", ""))
        if not (
            rid in {"l7tColl", "n0vlColl", "n0nlColl", "twaColl",
                    "netizenColl_top", "netizenColl_bottom"}
            or rid.startswith("netizen")
            or (rid.startswith("n0") and rid.endswith("Coll"))
        ):
            continue
        bounds = n.get("bounds", "")
        if bounds and _in_bounds(bounds, y_min=y_min, y_max=y_max):
            return True
    return False


def _jawan_result_page_opened(d: u2.Device, pre_activity: str, *,
                              y_min: int, y_max: int,
                              fast: bool = False) -> bool:
    """클릭 후 검색결과 목록을 벗어났는지 확인."""
    waits = (0.40,) if fast else (0.50, 0.65)
    depth = 28 if fast else 45

    for extra in waits:
        time.sleep(extra)
        root = _jawan_dump_ui(d, max_depth=depth)
        if root is None:
            continue
        if not _search_results_on_screen(root, y_min=y_min, y_max=y_max):
            log("[+] 자완: 검색결과 섹션 이탈 확인")
            return True

        long_text = sum(
            1 for n in root.iter("node")
            if n.get("package") == DAUM_PKG and len((n.get("text") or "")) > 120
        )
        section_rids = {n.get("resource-id", "") for n in root.iter("node")}
        if long_text >= 2 and "l7tColl" not in section_rids:
            log("[+] 자완: 본문 페이지 로딩 확인")
            return True

    if not fast and pre_activity:
        try:
            activity = d.app_current().get("activity", "")
            if activity and activity != pre_activity:
                log("[+] 자완: 다음앱 액티비티 전환")
                return True
        except Exception:
            pass

    return False


def click_any_search_result(d: u2.Device, *, timeout: float = 18.0,
                            max_scroll: int = 5,
                            initial_scroll: int = 1) -> bool:
    """검색 결과에서 제외 구역(파워링크·관련검색어·장소) 밖 아무 글이나 랜덤 클릭.

    빠른탭(좌표 랜덤)은 파워링크를 자주 치므로 사용하지 않는다.
    dump 후보(유기적 글)만 클릭한다.
    """
    log("[*] 자완: 검색 결과 랜덤 클릭 (파워링크 제외·dump 후보만)")
    t0 = time.time()
    deadline = t0 + timeout
    _, h = d.window_size()
    y_min = int(h * 0.28)
    y_max = int(h * 0.86)
    throttle = _Throttle()

    scroll_t0 = time.time()
    for i in range(initial_scroll):
        log(f"    ... 자완 초기 스크롤 {i + 1}/{initial_scroll}")
        d.swipe(0.5, 0.76, 0.5, 0.30, duration=0.22)
        time.sleep(0.45)
    if initial_scroll:
        log(f"    ... 자완 초기 스크롤 완료 ({time.time() - scroll_t0:.1f}s)")

    scrolls = 0
    while time.time() < deadline:
        throttle.dismiss(d, "자완탐색")

        root = _jawan_dump_ui(d, max_depth=55)
        if root is None:
            time.sleep(0.3)
            continue

        if _jawan_has_power_section(root):
            log("    ... 파워링크 구역 감지 → 스크롤로 유기 결과 탐색")

        cands = _collect_jawan_candidates(root, y_min=y_min, y_max=y_max)
        if cands:
            random.shuffle(cands)
            try:
                pre_activity = d.app_current().get("activity", "")
            except Exception:
                pre_activity = ""
            for bounds, label in cands[:5]:
                cx, cy = _bounds_center(bounds)
                log(f"[+] 자완 클릭 시도: '{label}' @ {bounds}")
                d.click(cx, cy)
                if _jawan_result_page_opened(d, pre_activity, y_min=y_min, y_max=y_max):
                    log(f"[+] 자완 완료 (총 {time.time() - t0:.1f}s)")
                    dismiss_popups_now(d, "자완후")
                    return True
                log("    ... 자완: 페이지 미전환 → 다른 후보 시도")
                try:
                    d.press("back")
                    time.sleep(0.45)
                except Exception:
                    pass
                throttle.dismiss(d, "자완재시도")

        if scrolls < max_scroll:
            scrolls += 1
            log(f"    ... 자완 후보 없음/미전환 → 스크롤 {scrolls}/{max_scroll}")
            d.swipe(0.5, 0.76, 0.5, 0.30, duration=0.22)
            time.sleep(0.45)
            continue

        time.sleep(0.4)

    log("[!] 자완: 랜덤 클릭 후보를 찾지 못했습니다.")
    return False


def _kakao_map_foreground(d: u2.Device, allow_dump: bool = False) -> bool:
    """폴드 멀티디스플레이: package selector 우선, dump 는 보조."""
    if d(packageName=MAP_PKG).exists(timeout=0):
        return True
    if not allow_dump:
        return False
    try:
        root = ET.fromstring(d.dump_hierarchy())
    except Exception:
        return False
    return any(n.get("package") == MAP_PKG for n in root.iter("node"))


def _kakao_place_content_ready(d: u2.Device, place_name: str, partial: bool) -> tuple:
    """카카오맵 콘텐츠 로딩 확인. Returns: (ready, reason)."""
    km = _import_kakao_traffic()
    if km._sheet_title_confirmed(d, place_name, partial):
        return True, "업체명 노출"
    if km.at_detail(d):
        return True, "상세화면(도착)"
    if d(resourceId=MAP_RID("place_name")).exists(timeout=0):
        return True, "검색결과 목록"
    return False, ""


def _import_kakao_traffic():
    import kakao_search_device as km
    return km


def wait_for_kakaomap_ready(d: u2.Device, place_name: str, *,
                            partial: bool = True,
                            timeout: float = PLACE_SECTION_TIMEOUT) -> bool:
    """카카오맵 전환 + 업체 화면 로딩까지 확인 (네트워크 지연은 timeout 으로 흡수)."""
    log(f"[*] 카카오맵 전환·로딩 확인 (최대 {timeout:.0f}s)")
    t0 = time.time()
    deadline = t0 + timeout
    last_log = t0
    saw_map = False
    throttle = _Throttle()
    poll_i = 0

    while time.time() < deadline:
        throttle.dismiss(d, "카카오맵대기")
        poll_i += 1
        allow_dump = (poll_i % DUMP_FALLBACK_EVERY == 0)
        if _kakao_map_foreground(d, allow_dump=allow_dump):
            saw_map = True
            ready, reason = _kakao_place_content_ready(d, place_name, partial)
            if ready:
                elapsed = time.time() - t0
                log(f"[+] 카카오맵 진입 + {reason} 확인 ({elapsed:.1f}s)")
                if MAP_CONTENT_STABLE > 0:
                    time.sleep(MAP_CONTENT_STABLE)
                return True

        now = time.time()
        if now - last_log >= 3.0:
            if saw_map:
                log(f"    ... 카카오맵 열림, 콘텐츠 로딩 중 ({now - t0:.0f}s)")
            else:
                log(f"    ... 카카오맵 전환 대기 ({now - t0:.0f}s)")
            last_log = now
        time.sleep(POLL_FAST)

    if saw_map:
        log("[!] 카카오맵은 열렸으나 업체 화면 미확인 (네트워크 지연 또는 로딩 실패)")
    else:
        log("[!] 카카오맵 전환 실패")
    return False


def wait_for_kakaomap_open(d: u2.Device, *,
                           timeout: float = PLACE_SECTION_TIMEOUT) -> bool:
    """다음 장소 클릭 후 카카오맵 앱 전환만 확인 (업체 재클릭·체류 없음)."""
    log(f"[*] 카카오맵 전환 확인 (최대 {timeout:.0f}s, 클릭형)")
    t0 = time.time()
    deadline = t0 + timeout
    last_log = t0
    throttle = _Throttle()
    poll_i = 0

    while time.time() < deadline:
        throttle.dismiss(d, "카카오맵전환")
        poll_i += 1
        allow_dump = (poll_i % DUMP_FALLBACK_EVERY == 0)
        if _kakao_map_foreground(d, allow_dump=allow_dump):
            elapsed = time.time() - t0
            log(f"[+] 카카오맵 전환 확인 ({elapsed:.1f}s) — 추가 액션 없음")
            return True

        now = time.time()
        if now - last_log >= 3.0:
            log(f"    ... 카카오맵 전환 대기 ({now - t0:.0f}s)")
            last_log = now
        time.sleep(POLL_FAST)

    log("[!] 카카오맵 전환 실패")
    return False


def kakao_place_click_and_scroll(d: u2.Device, place_name: str, *,
                                 partial: bool = True,
                                 search_timeout: float = PLACE_SECTION_TIMEOUT,
                                 scrolls: int = 1,
                                 scroll_pause: float = 0.8) -> bool:
    """카카오맵: 업체 진입(필요 시) → 상세 스크롤."""
    km = _import_kakao_traffic()
    log(f"[*] 카카오맵 업체 동작 (partial={partial})")
    dismiss_popups_now(d, "카카오맵업체")

    on_place = (km._sheet_title_confirmed(d, place_name, partial)
                or km.at_detail(d))
    if on_place:
        log("[*] 업체 화면 이미 로드됨 — 목록 검색·클릭 생략 (다음→카카오 딥링크)")
    elif not km.click_place(
        d, place_name, partial=partial, fast=True,
        search_timeout=search_timeout,
    ):
        log("[!] 카카오맵 업체 클릭 실패")
        return False

    dismiss_popups_now(d, "카카오맵상세")
    if km.click_place_name_on_detail(d, place_name, partial=partial, fast=True):
        log("[+] 카카오맵 상세 업체명 클릭 완료")
    else:
        log("[*] 상세 업체명 추가 클릭 생략 (이미 상세 화면)")

    if not km._sheet_title_confirmed(d, place_name, partial) and not km.at_detail(d):
        log("[!] 스크롤 전 업체 화면 미확인 — 작업 실패")
        return False

    scroll_kakaomap_detail(d, scrolls=scrolls, pause=scroll_pause)
    return True


def scroll_kakaomap_detail(d: u2.Device, scrolls: int = 1, pause: float = 0.8):
    """카카오맵 업체 상세 화면 스크롤 (기본 1회)."""
    log(f"[*] 카카오맵 상세 스크롤 ({scrolls}회)")
    for i in range(scrolls):
        dismiss_popups_now(d, "스크롤")
        d.swipe(0.5, 0.78, 0.5, 0.20, duration=0.45)
        time.sleep(pause)
        log(f"    스크롤 {i + 1}/{scrolls}")
    log("[+] 스크롤 완료")


def kakao_place_enter_and_dwell(d: u2.Device, place_name: str, *,
                                partial: bool = True,
                                search_timeout: float = PLACE_SECTION_TIMEOUT,
                                dwell_min: float = 15.0,
                                dwell_max: float = 20.0) -> bool:
    """카카오맵: 업체 진입 → 풀 상세 → 탭 클릭·스크롤 체류."""
    km = _import_kakao_traffic()
    log(f"[*] 카카오맵 체류 트래픽 (partial={partial}, {dwell_min:.0f}~{dwell_max:.0f}s)")
    dismiss_popups_now(d, "카카오맵업체")

    on_place = (km._sheet_title_confirmed(d, place_name, partial)
                or km.at_detail(d))
    if on_place:
        log("[*] 업체 화면 이미 로드됨 — 목록 검색·클릭 생략")
    elif not km.click_place(
        d, place_name, partial=partial, fast=True,
        search_timeout=search_timeout,
    ):
        log("[!] 카카오맵 업체 클릭 실패")
        return False

    dismiss_popups_now(d, "카카오맵상세")
    if not km.click_place_name_on_detail(d, place_name, partial=partial, fast=True):
        log("[*] 업체명 클릭 생략 — 시트/상세에서 체류 진행")

    if (not km._sheet_title_confirmed(d, place_name, partial)
            and not km.at_detail(d)
            and not km._detail_tab_visible(d, "홈")):
        log("[!] 체류 전 업체 화면 미확인 — 작업 실패")
        return False

    dismiss_popups_now(d, "체류")
    km.dwell_on_place_detail(d, min_secs=dwell_min, max_secs=dwell_max)
    return True


def clear_app_data(serial: str):
    for pkg, label in ((DAUM_PKG, "다음앱"), (MAP_PKG, "카카오맵")):
        log(f"[*] {label} 데이터 삭제(pm clear)")
        _adb(serial, "shell", "pm", "clear", pkg)
    time.sleep(2.5)


def run_once(d: u2.Device, args, place_id: str, place_name: str) -> tuple:
    """Returns: (성공 여부, 앱 진입 여부)."""
    log("[*] 로테이션 시작 전 데이터·IP 확인")
    if not ensure_network(args.serial, timeout=args.recover_secs):
        log("[!] 연결 미확인 → 다음앱 진입 생략 (이번 로테이션 스킵)")
        return False, False

    open_daum(d, args.serial)
    search_keyword(d, args.keyword, timeout=args.search_timeout)
    if not click_place_in_section(d, place_id, place_name,
                                   timeout=args.place_timeout):
        return False, True
    if not wait_for_kakaomap_ready(
        d, place_name,
        partial=args.partial,
        timeout=args.map_timeout,
    ):
        return False, True
    if not kakao_place_click_and_scroll(
        d, place_name,
        partial=args.partial,
        search_timeout=args.map_timeout,
        scrolls=args.scrolls,
        scroll_pause=args.scroll_pause,
    ):
        return False, True
    return True, True


def main():
    ap = argparse.ArgumentParser(description="다음앱 검색 → 장소 → 카카오맵 업체 클릭·스크롤")
    ap.add_argument("keyword", nargs="?", default=DEFAULT_KEYWORD,
                    help=f"검색 키워드 (기본: {DEFAULT_KEYWORD})")
    ap.add_argument("--place-url", default=DEFAULT_PLACE_URL,
                    help="카카오맵 장소 URL (place ID 로 매칭)")
    ap.add_argument("--place-name", default="",
                    help="업체명 (지정 시 URL 조회 생략, 맥 인터넷 없을 때 사용)")
    ap.add_argument("--serial", default=DEFAULT_SERIAL,
                    help=f"adb 시리얼 (기본: {DEFAULT_SERIAL})")
    ap.add_argument("--search-timeout", type=float, default=PLACE_SECTION_TIMEOUT,
                    help="다음앱 검색 결과 로딩 대기(초, 기본 45)")
    ap.add_argument("--place-timeout", type=float, default=PLACE_SECTION_TIMEOUT,
                    help="장소 섹션 탐색 대기(초)")
    ap.add_argument("--map-timeout", type=float, default=PLACE_SECTION_TIMEOUT,
                    help="카카오맵 전환·업체 UI·클릭 대기(초, 기본 45)")
    ap.add_argument("--partial", action="store_true", default=True,
                    help="업체명 부분일치 (기본 켜짐)")
    ap.add_argument("--no-partial", action="store_false", dest="partial",
                    help="업체명 완전일치")
    ap.add_argument("--scrolls", type=int, default=1,
                    help="카카오맵 상세 스크롤 횟수 (기본 1)")
    ap.add_argument("--scroll-pause", type=float, default=0.8,
                    help="스크롤 간 대기(초)")
    ap.add_argument("--rotations", type=int, default=0,
                    help="반복 횟수 (0=무한, Ctrl+C 중단)")
    ap.add_argument("--no-data-toggle", action="store_true",
                    help="모바일 데이터 OFF/ON 비활성화")
    ap.add_argument("--off-secs", type=float, default=4.0,
                    help="데이터 OFF 유지 시간(초)")
    ap.add_argument("--recover-secs", type=float, default=25.0,
                    help="데이터 ON 후 연결 복구 대기(초, IP 확인, 기본 25)")
    ap.add_argument("--no-clear", action="store_true",
                    help="pm clear 비활성화")
    args = ap.parse_args()

    place_id = parse_place_id(args.place_url)
    if args.place_name.strip():
        place_name = args.place_name.strip()
        log(f"[*] 업체명 (--place-name): {place_name}")
    else:
        try:
            place_name = resolve_place_name(args.place_url)
        except Exception as e:
            fallback = KNOWN_PLACE_NAMES.get(place_id, "")
            if fallback:
                place_name = fallback
                log(f"[!] 장소 URL 조회 실패 (맥 인터넷/DNS): {e}")
                log(f"[*] 알려진 place ID 폴백 사용: '{place_name}'")
            else:
                log(f"[!] 장소 URL 조회 실패: {e}")
                log("[!] 맥에서 인터넷 연결을 확인하거나 --place-name \"업체명\" 을 지정하세요.")
                sys.exit(1)

    log(f"[*] 장소 URL: {args.place_url}")
    log(f"[*] place ID: {place_id or '(없음)'} / 업체명: {place_name}")

    data_toggle = not args.no_data_toggle
    do_clear = not args.no_clear
    d = connect(args.serial)
    setup_popup_watchers(d)

    results = []
    ip_changes = []
    infinite = args.rotations <= 0
    total = "∞" if infinite else str(args.rotations)
    i = 0

    try:
        while infinite or i < args.rotations:
            i += 1
            begin_cycle_timer()
            cycle_start = time.time()
            log(f"\n===== 로테이션 {i}/{total} 시작 =====")
            try:
                ok, opened = run_once(d, args, place_id, place_name)
            except Exception as e:
                log(f"[!] 로테이션 {i} 오류: {e}")
                ok, opened = False, False

            if opened:
                try:
                    d.screenshot(os.path.join(_SCRIPT_DIR, "daum_result.png"))
                except Exception:
                    pass

            log(f"[*] 로테이션 {i} (작업 성공={ok}, 진입={opened})")
            results.append(ok)

            log(f"[*] 로테이션 {i} 마무리")
            if opened:
                d.app_stop(MAP_PKG)
                d.app_stop(DAUM_PKG)
            if data_toggle:
                changed = toggle_mobile_data(
                    args.serial, off_secs=args.off_secs,
                    recover_timeout=args.recover_secs,
                )
                ip_changes.append(changed)
            if do_clear and opened:
                clear_app_data(args.serial)
            elif do_clear:
                log("[*] 앱 미진입 — pm clear 생략")

            cycle_elapsed = time.time() - cycle_start
            ipc = f", IP변경 {sum(ip_changes)}/{len(ip_changes)}" if ip_changes else ""
            log(f"[누적] 작업 성공 {sum(results)}/{len(results)}{ipc}")
            log(f"===== 로테이션 {i}/{total} 완료 — 소요 {cycle_elapsed:.1f}s =====")
            end_cycle_timer()
    except KeyboardInterrupt:
        log("\n[*] 사용자 중단(Ctrl+C)")
    finally:
        log("\n========== 최종 요약 ==========")
        log(f"[+] 작업 성공: {sum(results)}/{len(results)} 로테이션")
        if ip_changes:
            log(f"[+] IP 변경 성공: {sum(ip_changes)}/{len(ip_changes)} 회")
        try:
            d.watcher.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
