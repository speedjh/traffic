# -*- coding: utf-8 -*-
"""
[실물 폴드 기기용] 카카오맵 검색·업체 진입 자동화. 로테이션마다 모바일 데이터
OFF/ON + 앱 데이터 삭제 후 재진입.

모드 (--mode):
    route  — 길찾기: 상세화면 '도착' → 출발지 입력 → 경로 결과
    search — 일반트래픽: 상세화면 업체명 클릭 → 메뉴/사진/후기/블로그
             랜덤 클릭·스크롤 체류 (--dwell-min/--dwell-max, 도착 미사용)

LD용은 kakao_search_ld.py 참고. 이 파일은 갤럭시 폴드(SM-F966N) 등
멀티 디스플레이 기기의 "외부 커버화면(display 0, 1080x2520, 폰 레이아웃)"을
타깃으로 한다.

폴드 대응 핵심:
    - 커버화면(display 0)을 강제: `cmd device_state state 0`(CLOSED)
    - 카카오맵을 display 0 에 명시 실행: `am start --display 0 -n PKG/MainActivity`
      (그냥 app_start 하면 내부화면(display 1)에 떠서 uiautomator2 가 못 봄)

사용법:
    python kakao_search_device.py "키워드" "업체명" --mode route --partial
    python kakao_search_device.py "키워드" "업체명" --mode search --partial
    python kakao_search_device.py ... --serial R3CY80RW96Y

요구사항:
    - 기기 USB 디버깅 ON, `adb devices` 에 기기가 잡혀 있어야 함
    - pip install uiautomator2
"""
import os
import re
import sys
import time
import random
import argparse
import subprocess
import xml.etree.ElementTree as ET
from typing import Optional

# Windows 콘솔에서 한글 로그가 깨지지 않도록 UTF-8 출력 강제
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# uiautomator2(adbutils)가 LDPlayer 내장 adb 를 찾도록 PATH 에 추가
LD_DIR = r"C:\LDPlayer\LDPlayer9"
if os.path.isdir(LD_DIR):
    os.environ["PATH"] = LD_DIR + os.pathsep + os.environ.get("PATH", "")

# adb 실행 파일: LDPlayer 동봉 adb 우선, 없으면 PATH 의 표준 adb (실기기 USB/무선용)
_LD_ADB = os.path.join(LD_DIR, "adb.exe")
ADB_EXE = _LD_ADB if os.path.isfile(_LD_ADB) else "adb"

import uiautomator2 as u2  # noqa: E402

PKG = "net.daum.android.map"
RID = lambda name: f"{PKG}:id/{name}"  # noqa: E731
MAIN_ACTIVITY = "com.kakao.map.main.view.MainActivity"
COVER_DISPLAY = 0        # 폴드 외부 커버화면 디스플레이 id
CLOSED_STATE = 0         # device_state: CLOSED(접힘=커버화면)
DEFAULT_SERIAL = "R3CY80RW96Y"   # 실물 기기(SM-F966N) USB 시리얼

# 일반트래픽(search): 진입 빠르게, 홈탭 체류 없음
SEARCH_CHAR_DELAY = 0.03
SEARCH_POST_TYPE_DELAY = 0.08
SEARCH_RESULT_POLL = 0.05       # u2 wait 간격 (초)
SEARCH_RESULT_TIMEOUT = 45.0   # 네트워크 지연 허용 (초)
SEARCH_DETAIL_VERIFY_TIMEOUT = 8.0

# 출발지로 쓸 전국 행정구역명 (특별·광역시 + 도별 시·군). 매 로테이션 랜덤 선택 →
# 출발지 칸에 입력 → 자동완성 결과(행정구역) 중 아무거나 선택. 한국 내 100% 보장.
KOREA_REGIONS = [
    # 특별시·광역시·특별자치시
    "서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시",
    "대전광역시", "울산광역시", "세종특별자치시",
    # 경기
    "수원시", "성남시", "고양시", "용인시", "부천시", "안산시", "안양시",
    "남양주시", "화성시", "평택시", "의정부시", "파주시", "김포시", "광명시",
    "군포시", "이천시", "양주시", "안성시", "구리시", "포천시", "여주시",
    "동두천시", "가평군", "양평군", "연천군",
    # 강원
    "춘천시", "원주시", "강릉시", "동해시", "태백시", "속초시", "삼척시",
    "홍천군", "영월군", "평창군", "정선군", "철원군", "화천군", "양구군",
    "인제군", "고성군", "양양군",
    # 충북
    "청주시", "충주시", "제천시", "보은군", "옥천군", "영동군", "증평군",
    "진천군", "괴산군", "음성군", "단양군",
    # 충남
    "천안시", "공주시", "보령시", "아산시", "서산시", "논산시", "계룡시",
    "당진시", "금산군", "부여군", "서천군", "청양군", "홍성군", "예산군", "태안군",
    # 전북
    "전주시", "군산시", "익산시", "정읍시", "남원시", "김제시", "완주군",
    "진안군", "무주군", "장수군", "임실군", "순창군", "고창군", "부안군",
    # 전남
    "목포시", "여수시", "순천시", "나주시", "광양시", "담양군", "곡성군",
    "구례군", "고흥군", "보성군", "화순군", "장흥군", "강진군", "해남군",
    "영암군", "무안군", "함평군", "영광군", "장성군", "완도군", "진도군", "신안군",
    # 경북
    "포항시", "경주시", "김천시", "안동시", "구미시", "영주시", "영천시",
    "상주시", "문경시", "경산시", "의성군", "청송군", "영양군", "영덕군",
    "청도군", "고령군", "성주군", "칠곡군", "예천군", "봉화군", "울진군", "울릉군",
    # 경남
    "창원시", "진주시", "통영시", "사천시", "김해시", "밀양시", "거제시",
    "양산시", "의령군", "함안군", "창녕군", "남해군", "하동군", "산청군",
    "함양군", "거창군", "합천군",
    # 제주
    "제주시", "서귀포시",
]

# ── 팝업에서 누르면 닫히는 버튼 후보 (텍스트 기준) ───────────────────────────
# 주의: "닫기"/"확인"/"취소" 같은 일반 텍스트는 카카오맵 정상 UI(예: 검색창의
# 'X' 버튼은 content-desc="닫기")와 충돌하므로 넣지 않는다.
# → 팝업/권한창에서만 등장하는 고유 텍스트로만 한정한다.
POPUP_DISMISS_TEXTS = [
    # 앱 데이터 초기화 후 첫 진입 온보딩(IntroActivity) — 로그인 없이 시작
    "비로그인으로 시작하기",
    # 안드로이드 시스템 위치 권한 다이얼로그 — 반드시 '허용 안함'(거부)으로 닫는다.
    # (GPS 연결 방지. '허용' 류는 절대 넣지 말 것 — 위치 권한이 허용돼 버린다)
    "허용 안함", "허용 안 함", "이번에는 허용 안함",
    "Don't allow", "Deny",
    # 카카오맵/광고 이벤트 팝업 (정상 화면엔 없는 고유 문구)
    "오늘 그만보기", "오늘은 그만보기", "오늘 하루 보지 않기",
    "다시 보지 않기", "다시 보지않기", "다시 안 보기",
    "이 창 다시 보지 않기", "나중에 하기", "건너뛰기",
    "동의하고 시작하기",
    # 데이터 초기화 후 '혜택 알림 설정' 다이얼로그 (id/notYet) — 수신 거부 쪽
    "지금은 아니에요",
    # '선택 마케팅 알림 수신 동의' 팝업 — 거부 쪽('동의하기' 아님)
    "나중에",
    # 위치 정확도 사용 제안(LocationSettingsCheckerActivity) — 거부 쪽
    # (출발지는 지도에서 수동 선택하므로 GPS 정확도 불필요)
    "아니요",
]


def setup_popup_watchers(d: u2.Device, login_mode: bool = False):
    """백그라운드에서 팝업을 감지해 자동으로 닫는 watcher 등록 (xpath 기반).

    login_mode 면 '비로그인으로 시작하기' 는 누르지 않는다 (로그인 흐름과 충돌).
    """
    try:
        d.watcher.reset()
    except Exception:
        pass
    texts = POPUP_DISMISS_TEXTS
    if login_mode:
        # 로그인 모드: '비로그인 시작'·'동의하고 시작하기'(마케팅 동의)는 누르지 않는다.
        # 대신 인증 성공 후 '전화번호 등록/혜택 동의' 는 거부(나중에/다음에 할게요)로 넘긴다.
        texts = [t for t in texts if t not in ("비로그인으로 시작하기", "동의하고 시작하기")]
        texts = ["다음에 할게요", "나중에"] + texts
    for i, t in enumerate(texts):
        # 텍스트 정확/부분 일치 + content-desc 일치 모두 커버
        xpath = (
            f'//*[@text="{t}"]'
            f'|//*[@content-desc="{t}"]'
            f'|//android.widget.Button[contains(@text,"{t}")]'
        )
        d.watcher(f"popup_text_{i}").when(xpath).click()
    # 카카오맵 자체 머티리얼 다이얼로그(알림 허용 등)의 긍정 버튼은 id로 처리.
    # 정상 검색/길찾기 화면엔 없는 id 라 "확인" 텍스트와 달리 충돌하지 않음.
    d.watcher("dialog_positive").when(
        f'//*[@resource-id="{RID("md_button_positive")}"]'
    ).click()
    # 계정 선택 화면("카카오맵을 이용할 계정을 선택해 주세요")은 클릭이 아니라 뒤로가기로
    # 닫는다. 간헐적으로 진입/검색 등 어느 단계에서든 뜰 수 있으므로 watcher 로 상시 감시.
    d.watcher("account_select").when(
        '//*[contains(@text,"계정을 선택")]'
    ).press("back")
    # 인증 성공 후 '놓치기 아까운 장소와 혜택'/'전화번호 등록' 화면 → 거부 버튼 우선
    if login_mode:
        d.watcher("benefit_consent").when(
            '//*[contains(@text,"혜택") or contains(@text,"놓치기") or contains(@text,"연락처 전화번호")]'
        ).click()
        # 위 컨텍스트에서 실제로 누를 버튼은 '나중에'/'다음에 할게요'
        d.watcher("later_btn").when('//*[@text="나중에"]').click()
        d.watcher("later2_btn").when('//*[@text="다음에 할게요"]').click()
    d.watcher.start(interval=1.0)  # 1초마다 화면 검사
    print("[*] 팝업 자동 처리 watcher 활성화")


def _adb(serial: str, *args, timeout: float = 25.0) -> subprocess.CompletedProcess:
    """`adb -s <serial> <args>` 실행 (stdout/stderr 캡처)."""
    return subprocess.run([ADB_EXE, "-s", serial, *args],
                          capture_output=True, text=True, timeout=timeout)


def get_public_ip(serial: str, curl_timeout: int = 8) -> str:
    """기기의 현재 공인 IPv4 조회 (api.ipify.org). 실패 시 빈 문자열.
    주의: Wi-Fi 가 켜져 있으면 Wi-Fi 공인 IP 가 잡혀 데이터 토글해도 안 바뀐다.
    IP 변경 목적이면 기기에서 Wi-Fi 를 끄고 LTE(모바일 데이터)만 사용해야 한다."""
    try:
        r = _adb(serial, "shell", "curl", "-s", "--max-time", str(curl_timeout),
                 "https://api.ipify.org", timeout=curl_timeout + 5)
        ip = (r.stdout or "").strip()
        if ip and re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
            return ip
        return ""
    except Exception:
        return ""


def _mobile_data_enabled(serial: str) -> bool:
    """settings global mobile_data 상태 (1=켜짐)."""
    try:
        r = _adb(serial, "shell", "settings", "get", "global", "mobile_data", timeout=5)
        return (r.stdout or "").strip().lower() in ("1", "true")
    except Exception:
        return False


def ensure_network(serial: str, timeout: float = 20.0) -> bool:
    """모바일 데이터 ON + 공인 IP 조회로 연결 판정.

    ping 은 갤럭시 등에서 LTE 가 켜져 있어도 adb shell 에서 실패하는 경우가
    많아 사용하지 않는다. (실제로 IP 는 조회되는데 ping 만 막히는 증상)"""
    if not _mobile_data_enabled(serial):
        print("[*] 모바일 데이터 OFF 감지 → ON")
        _adb(serial, "shell", "svc", "data", "enable")
        time.sleep(2.0)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _mobile_data_enabled(serial):
            _adb(serial, "shell", "svc", "data", "enable")
            time.sleep(1.5)
            continue
        ip = get_public_ip(serial, curl_timeout=6)
        if ip:
            print(f"[+] 데이터·IP 연결 확인 ({ip})")
            return True
        time.sleep(2.0)

    state = "ON" if _mobile_data_enabled(serial) else "OFF"
    print(f"[!] 데이터/IP 연결 확인 실패 (mobile_data={state}, LTE ON 이어도 IP 미조회)")
    return False


def toggle_mobile_data(serial: str, off_secs: float = 4.0,
                       recover_timeout: float = 25.0) -> bool:
    """모바일 데이터 OFF 1회 → ON 1회 → IP 조회로 복구 확인. 이중 OFF 방지."""
    was_on = _mobile_data_enabled(serial)
    ip_before = get_public_ip(serial) if was_on else ""
    print(f"[*] 변경 전 IP: {ip_before or '(조회 실패 / OFF)'}")

    if was_on:
        print("[*] 모바일 데이터 OFF (svc data disable, 1회)")
        _adb(serial, "shell", "svc", "data", "disable")
        time.sleep(off_secs)
    else:
        print("[*] 데이터 이미 OFF — OFF 단계 생략 (이중 OFF 방지)")

    print("[*] 모바일 데이터 ON (svc data enable, 1회)")
    _adb(serial, "shell", "svc", "data", "enable")

    if not ensure_network(serial, timeout=recover_timeout):
        print("[!] 복구 지연 — enable 1회 재시도")
        _adb(serial, "shell", "svc", "data", "enable")
        if not ensure_network(serial, timeout=15.0):
            ip_retry = get_public_ip(serial)
            if ip_retry:
                print(f"[+] IP 직접 조회 성공 ({ip_retry}) — 연결 OK")
            else:
                print("[!] 복구 실패 — 다음 로테이션에서 재시도")

    ip_after = get_public_ip(serial)
    print(f"[*] 변경 후 IP: {ip_after or '(조회 실패)'}")
    changed = bool(ip_after) and ip_after != ip_before
    mark = "+" if changed else "!"
    status = "성공" if changed else ("실패(동일)" if ip_after else "실패(조회불가)")
    print(f"[{mark}] IP 변경 {status}: {ip_before or '?'} → {ip_after or '?'}")
    return changed


def connect(serial: str) -> u2.Device:
    # USB 직결 실기기는 adb connect 불필요 (TCP 무선이면 serial 에 ip:port 주고
    # 아래 connect 한 줄을 살리면 됨)
    print(f"[*] 기기 연결: {serial}")
    try:
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from device_screen import ensure_screen_ready
        ensure_screen_ready(serial, log=lambda m: print(m, flush=True))
    except Exception as e:
        print(f"[!] 화면 준비 스킵: {e}", flush=True)
    d = u2.connect(serial)
    d.settings["wait_timeout"] = 15.0
    print(f"[+] 연결됨: {d.info.get('productName')} / {d.serial} / {d.window_size()}")
    return d


def open_kakaomap(d: u2.Device, serial: str):
    """폴드 커버화면(display 0)에 카카오맵을 강제로 띄운다.
    - device_state 0(CLOSED): 펼친 상태여도 OS 가 커버화면을 활성 디스플레이로 인식
    - am start --display 0: 카카오맵을 커버화면에 실행(내부화면으로 새지 않게)
    실사용 시 기기를 실제로 접어두면 device_state 강제 없이도 동일하게 동작한다."""
    print("[*] 카카오맵 실행 (커버화면 display 0)")
    _adb(serial, "shell", "cmd", "device_state", "state", str(CLOSED_STATE))
    # GPS(위치 서비스) 자체를 OFF → 위치 권한 허용 여부와 무관하게 실제 위치를 못 잡는다.
    # (출발지는 지명 입력이라 GPS 불필요. 실제 위치가 잡히면 안 되므로 매 진입마다 끈다)
    _adb(serial, "shell", "settings", "put", "secure", "location_mode", "0")
    time.sleep(1.0)
    # 데이터 삭제 직후엔 앱이 한 번에 안 뜨고 홈으로 빠지는 경우가 있어 최대 3회 재실행.
    for attempt in range(3):
        _adb(serial, "shell", "am", "force-stop", PKG)
        time.sleep(1.0)
        # LAUNCHER intent 로 정상 진입(데이터 삭제 후 온보딩 흐름 보장) + 커버화면(display 0)
        _adb(serial, "shell", "am", "start", "--display", str(COVER_DISPLAY),
             "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER",
             "-n", f"{PKG}/{MAIN_ACTIVITY}")
        # app_current() 는 폴드 멀티디스플레이에서 부정확하므로 UI 트리로 로딩 판단
        for _ in range(35):
            # 계정 선택/온보딩/권한 팝업은 watcher 가 상시 처리(back/click)하므로
            # 여기선 메인(검색버튼/검색창)이 뜨기만 기다린다.
            if d(resourceId=RID("btn_search")).exists or d(resourceId=RID("query")).exists:
                time.sleep(2.0)  # 지도 초기화 안정화 (첫 탭이 무시되는 것 방지)
                print("[+] 메인 화면 로딩 완료")
                return
            time.sleep(1.0)
        print(f"[!] 메인 미확인 → 카카오맵 재실행 ({attempt+1}/3)")
    print("[!] 메인 화면 로딩 실패")


def open_search_screen(d: u2.Device, retries: int = 6, serial: Optional[str] = None):
    """메인의 검색 버튼을 눌러 검색 입력창(query)이 나올 때까지 재시도.

    최근 실기기에서 btn_search 탭이 무시되거나 다른 레이어에 가려
    query 가 안 뜨는 경우가 있어, 설명/텍스트 폴백·back·중간 재진입을 둔다.
    """
    for i in range(retries):
        if d(resourceId=RID("query")).exists:
            return True
        clicked = False
        if d(resourceId=RID("btn_search")).exists:
            try:
                d(resourceId=RID("btn_search")).click()
                clicked = True
            except Exception:
                pass
        if not clicked:
            for sel in (
                {"descriptionContains": "검색"},
                {"description": "검색"},
                {"text": "검색"},
                {"textContains": "검색"},
            ):
                try:
                    node = d(**sel)
                    if node.exists:
                        node.click()
                        clicked = True
                        break
                except Exception:
                    continue
        if d(resourceId=RID("query")).wait(timeout=7):
            return True
        print(f"    검색화면 전환 재시도 {i+1}/{retries}")
        # 팝업/시트에 막힌 경우 back 후 재시도
        try:
            d.press("back")
            time.sleep(0.6)
        except Exception:
            pass
        # 중반에 한 번 카카오맵 재진입(메인 상태 리셋)
        if serial and i == max(1, retries // 2 - 1):
            print("    검색 전환 실패 지속 → 카카오맵 재진입")
            try:
                open_kakaomap(d, serial)
            except Exception as e:
                print(f"    재진입 실패: {e}")
        time.sleep(1.0)
    return d(resourceId=RID("query")).exists


def search_keyword(d: u2.Device, keyword: str, fast: bool = False, serial: Optional[str] = None):
    """검색어만 입력·실행. 업체 노출 확인·클릭은 click_place 가 담당."""
    print(f"[*] 검색창 열기 → '{keyword}' 입력")
    if not open_search_screen(d, serial=serial or getattr(d, "serial", None)):
        raise RuntimeError("검색 입력창(query)을 열지 못했습니다.")
    query = d(resourceId=RID("query"))
    query.click()
    time.sleep(0.2 if fast else 0.5)
    d.clear_text()
    char_delay = SEARCH_CHAR_DELAY if fast else 0.12
    for ch in keyword:
        d.send_keys(ch)
        time.sleep(char_delay)
    time.sleep(SEARCH_POST_TYPE_DELAY if fast else 0.8)
    d.press("enter")
    print("[*] 검색 실행 → 목표 업체 확인 후 즉시 클릭")


def at_detail(d: u2.Device) -> bool:
    """업체 상세화면(단일 결과 직행 포함)인지: 하단에 '도착' 버튼이 있으면 상세화면."""
    return d(text="도착").exists


def _bounds_center(b: str):
    """'[x1,y1][x2,y2]' → (cx, cy)."""
    x1, y1, x2, y2 = map(int, re.findall(r"-?\d+", b)[:4])
    return (x1 + x2) // 2, (y1 + y2) // 2


def _norm_text(text: str) -> str:
    """Compose UI 제목 등에 섞인 zero-width·테스트 키 제거."""
    t = (text or "").replace("\u200b", "").strip()
    # Compose semantics 키가 text 뒤에 붙는 경우 (완산돈MarginKey0ComposableKey0...)
    t = re.sub(r"(?:MarginKey|ComposableKey)\d+", "", t)
    return t.strip()


def _name_matches(text: str, name: str, partial: bool) -> bool:
    t = _norm_text(text)
    if not t:
        return False
    return name in t if partial else t == name


def _list_target_el(d: u2.Device, name: str, partial: bool):
    """목록 place_name — u2 selector (dump 없음, 가장 빠름)."""
    if partial:
        return d(resourceId=RID("place_name"), textContains=name)
    return d(resourceId=RID("place_name"), text=name)


def _list_matching_places(d: u2.Device, name: str, partial: bool):
    """dump fallback: zero-width 등으로 u2 selector 가 miss 할 때만."""
    matches = []
    root = ET.fromstring(d.dump_hierarchy())
    for n in root.iter("node"):
        if n.get("package") != PKG:
            continue
        if not n.get("resource-id", "").endswith("/place_name"):
            continue
        t = _norm_text(n.get("text", ""))
        if t and _name_matches(t, name, partial):
            matches.append((t, n.get("bounds", "")))
    return matches


def _detail_place_title(d: u2.Device, name: str, partial: bool):
    """상세 제목 dump 파싱 (느림 — fallback 전용)."""
    # Compose 목록/시트 공통: text·content-desc 이름 매칭
    hits = _find_name_match_nodes(d, name, partial)
    if hits:
        return hits[0][3]
    root = ET.fromstring(d.dump_hierarchy())
    for n in root.iter("node"):
        if n.get("package") != PKG:
            continue
        desc = _norm_text(n.get("content-desc", ""))
        if desc.endswith("제목"):
            title = desc.split(",")[0]
            if _name_matches(title, name, partial):
                return title
        raw = n.get("text", "") or ""
        if "panelTitle" in raw:
            t = _norm_text(raw.split("panelTitle")[0])
            if _name_matches(t, name, partial):
                return t
    return None


def _place_match_keys(name: str, partial: bool):
    """업체 매칭용 키워드 (긴 것 우선). Compose content-desc 부분일치용."""
    keys = []
    n = _norm_text(name)
    if n:
        keys.append(n)
    if partial and len(n) > 4:
        for size in (6, 5, 4, 3):
            if len(n) >= size:
                keys.append(n[-size:])
    seen = set()
    out = []
    for k in keys:
        if k not in seen and len(k) >= 2:
            seen.add(k)
            out.append(k)
    return out


def _sheet_title_xpath(key: str) -> str:
    return (
        f'//*[@package="{PKG}"][contains(@content-desc,"제목")]'
        f'[contains(@content-desc,"{key}")]'
    )


def _sheet_title_confirmed(d: u2.Device, name: str, partial: bool) -> bool:
    """하단 시트/상세에 목표 업체명이 보이면 True. '도착' 버튼 대기 불필요."""
    if _list_target_el(d, name, partial).exists:
        return True
    for key in _place_match_keys(name, partial):
        try:
            if d.xpath(_sheet_title_xpath(key)).exists:
                return True
        except Exception:
            pass
    return False


def _click_sheet_title(d: u2.Device, name: str, partial: bool) -> bool:
    """시트 제목 행 클릭 — xpath 우선."""
    for key in _place_match_keys(name, partial):
        try:
            el = d.xpath(_sheet_title_xpath(key))
            if el.exists:
                el.click()
                return True
        except Exception:
            pass
    root = ET.fromstring(d.dump_hierarchy())
    cands = []
    for n in root.iter("node"):
        if n.get("package") != PKG:
            continue
        desc = _norm_text(n.get("content-desc", ""))
        text = _norm_text(n.get("text", ""))
        title_hit = (
            (desc.endswith("제목") and _name_matches(desc.split(",")[0], name, partial))
            or (name in (n.get("text") or "") and "panelTitle" in (n.get("text") or ""))
        )
        if not title_hit:
            continue
        b = n.get("bounds", "")
        if b:
            cx, cy = _bounds_center(b)
            cands.append((cy, cx, cy, desc or text[:30]))
    if cands:
        cands.sort()
        _, cx, cy, label = cands[0]
        d.click(cx, cy)
        print(f"[+] 업체명 클릭 (좌표, '{label}')")
        return True
    return False


def _detail_title_quick(d: u2.Device, name: str, partial: bool, allow_dump: bool = True):
    """상세/시트 제목 확인. 도착 버튼 없이 업체명만 보여도 통과."""
    if _sheet_title_confirmed(d, name, partial):
        return name
    if allow_dump and not d(resourceId=RID("place_name")).exists:
        return _detail_place_title(d, name, partial)
    return None


def _wait_entered(d: u2.Device, name: str, partial: bool,
                  timeout: float = SEARCH_DETAIL_VERIFY_TIMEOUT,
                  poll: float = SEARCH_RESULT_POLL):
    """클릭 후 목표 업체 화면 진입 확인 (업체명 노출 기준)."""
    deadline = time.time() + timeout
    dump_after = time.time() + 0.8
    while time.time() < deadline:
        if d(text="도착").exists:
            return name
        if _list_target_el(d, name, partial).exists:
            return name
        titled = False
        for key in _place_match_keys(name, partial):
            try:
                if d.xpath(_sheet_title_xpath(key)).exists:
                    titled = True
                    break
            except Exception:
                pass
        if titled:
            return name
        if time.time() >= dump_after:
            t = _detail_place_title(d, name, partial)
            if t and not _is_search_chrome_label(t):
                # 검색 키워드 전체('전주 완산돈') 거절 — 업체명 자체/접미만 허용
                if t == name or t.endswith(name) or (" " not in t and name in t):
                    return t
        time.sleep(poll)
    return None


def _try_click_list_item(d: u2.Device, name: str, partial: bool) -> bool:
    """목록에서 목표 업체 클릭. u2 우선, 필요 시 dump 좌표."""
    el = _list_target_el(d, name, partial)
    if el.exists:
        el.click()
        return True
    if d(resourceId=RID("place_name")).exists:
        matches = _list_matching_places(d, name, partial)
        if matches:
            cx, cy = _bounds_center(matches[0][1])
            d.click(cx, cy)
            return True
    # Compose/신규 목록: place_name RID 없이 text/content-desc 만 있는 경우
    return _click_any_name_match(d, name, partial)


def _is_search_chrome_label(label: str) -> bool:
    """검색창·자동완성·수정 힌트 등 결과 POI가 아닌 UI 문구."""
    t = label or ""
    bad = (
        "검색어를 수정",
        "두 번 탭",
        "검색창",
        "최근 검색",
        "자동완성",
        "삭제 버튼",
        "음성 검색",
        "뒤로 가기",
        "지도 보기",
        "필터",
    )
    return any(b in t for b in bad)


def _find_name_match_nodes(d: u2.Device, name: str, partial: bool):
    """dump 에서 업체명 매칭 노드 (text / content-desc). place_name RID 없어도 동작."""
    matches = []
    try:
        root = ET.fromstring(d.dump_hierarchy())
    except Exception:
        return matches
    w = getattr(d, "window_size", lambda: (1080, 2400))()
    try:
        screen_h = int(w[1])
    except Exception:
        screen_h = 2400
    # 상단 검색바·하단 탭바 제외
    y_min = int(screen_h * 0.18)
    y_max = int(screen_h * 0.92)

    for n in root.iter("node"):
        if n.get("package") != PKG:
            continue
        rid = n.get("resource-id", "") or ""
        if rid.endswith("/query") or rid.endswith("/search_text") or "edit_search" in rid:
            continue
        text = _norm_text(n.get("text", ""))
        desc = _norm_text(n.get("content-desc", ""))
        title = ""
        score = 0
        if rid.endswith("/place_name") and text and _name_matches(text, name, partial):
            title = text
            score = 100
        elif desc.endswith("제목"):
            head = desc.split(",")[0].strip()
            if _name_matches(head, name, partial):
                title = head
                score = 90
        elif text and _name_matches(text, name, partial) and not _is_search_chrome_label(text):
            title = text
            score = 50
        elif desc and not _is_search_chrome_label(desc):
            head = desc.split(",")[0].strip()
            if _name_matches(head, name, partial) or _name_matches(desc, name, partial):
                title = head or desc
                score = 40
        if not title or _is_search_chrome_label(title):
            continue
        # 검색 키워드 전체가 라벨이면(예: '전주 완산돈') 검색창/칩일 확률 높음 → place_name 아니면 강등
        if title != name and name in title and not title.startswith(name):
            # '전주 완산돈' matches place '완산돈' — demote unless place_name/제목
            if score < 90 and (" " in title or len(title) > len(name) + 1):
                score -= 30
        if score < 40:
            continue
        b = n.get("bounds", "")
        if not b:
            continue
        x1, y1, x2, y2 = map(int, re.findall(r"-?\d+", b)[:4])
        if (x2 - x1) < 60 or (y2 - y1) < 30:
            continue
        if y1 < y_min or y2 > y_max:
            continue
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        matches.append((-score, y1, cx, cy, title))
    matches.sort()
    # normalize to (y1, cx, cy, title)
    return [(y1, cx, cy, title) for _, y1, cx, cy, title in matches]


def _click_any_name_match(d: u2.Device, name: str, partial: bool) -> bool:
    hits = _find_name_match_nodes(d, name, partial)
    if not hits:
        return False
    _, cx, cy, title = hits[0]
    d.click(cx, cy)
    print(f"[+] 이름매칭 클릭: '{title}' @({cx},{cy})")
    return True


def click_place(d: u2.Device, name: str, partial: bool = False,
                max_scroll: int = 8, fast: bool = False,
                search_timeout: float = SEARCH_RESULT_TIMEOUT) -> bool:
    """목표 업체 확인 즉시 클릭 — 길찾기 '도착'처럼 u2.wait 로 뜨자마자 반응."""
    print(f"[*] 업체 진입: '{name}' (부분일치={partial})")
    wait_slice = SEARCH_RESULT_POLL if fast else 0.15
    timeout = search_timeout if fast else min(search_timeout, 30.0)
    post_timeout = SEARCH_DETAIL_VERIFY_TIMEOUT if fast else 12.0
    scroll_wait = 0.3 if fast else 1.2
    list_el = _list_target_el(d, name, partial)

    print(f"[*] '{name}' 검색 결과 확인 (최대 {timeout:.0f}s)")
    t0 = time.time()
    deadline = t0 + timeout
    last_log = t0
    last_dump = 0.0

    while time.time() < deadline:
        # ① 목록: u2.wait → 뜨는 즉시 클릭 (도착 방식과 동일)
        slice_t = min(wait_slice, max(0.01, deadline - time.time()))
        if list_el.wait(timeout=slice_t):
            list_el.click()
            title = _wait_entered(d, name, partial, timeout=post_timeout)
            if title:
                print(f"[+] '{title}' 클릭 → 진입 ({time.time() - t0:.2f}s)")
                if not fast:
                    time.sleep(1.0)
                return True
            print("[!] 클릭 후 목표 업체 미확인 (무효 클릭)")
            return False

        # ② 단일 결과/시트: 업체명만 보여도 즉시 통과 (도착 버튼 대기 X)
        if _sheet_title_confirmed(d, name, partial):
            print(f"[+] 업체 확인: '{name}' ({time.time() - t0:.2f}s)")
            return True

        # ③ u2 miss + 목록 컨테이너만 있는 경우 dump 1회
        if d(resourceId=RID("place_name")).exists:
            matches = _list_matching_places(d, name, partial)
            if matches:
                cx, cy = _bounds_center(matches[0][1])
                d.click(cx, cy)
                title = _wait_entered(d, name, partial, timeout=post_timeout)
                if title:
                    print(f"[+] '{title}' 클릭 → 진입 ({time.time() - t0:.2f}s)")
                    if not fast:
                        time.sleep(1.0)
                    return True
                print("[!] 클릭 후 목표 업체 미확인 (무효 클릭)")
                return False

        # ④ Compose 등 place_name 없는 UI — 주기적 dump 매칭
        now = time.time()
        if now - last_dump >= 1.2:
            last_dump = now
            if _click_any_name_match(d, name, partial):
                title = _wait_entered(d, name, partial, timeout=post_timeout)
                if title:
                    print(f"[+] '{title}' 클릭 → 진입 ({time.time() - t0:.2f}s)")
                    if not fast:
                        time.sleep(1.0)
                    return True
                # 시트만 열린 경우도 성공으로
                if _sheet_title_confirmed(d, name, partial):
                    print(f"[+] 업체 확인(시트): '{name}' ({time.time() - t0:.2f}s)")
                    return True
                print("[!] 이름매칭 클릭 후 미확인 — 재시도")

        if now - last_log >= 5.0:
            print(f"    ... 결과 로딩 대기 ({now - t0:.0f}s)")
            last_log = now

    # 스크롤: place_name 유무와 무관하게 결과 영역 탐색
    for attempt in range(max_scroll):
        print(f"    [{attempt+1}/{max_scroll}] 목록 스크롤")
        d.swipe(0.5, 0.75, 0.5, 0.35, duration=0.3)
        time.sleep(scroll_wait)
        if _try_click_list_item(d, name, partial):
            title = _wait_entered(d, name, partial, timeout=post_timeout)
            if title:
                print(f"[+] '{title}' 클릭 → 진입 ({time.time() - t0:.2f}s)")
                if not fast:
                    time.sleep(1.0)
                return True
            if _sheet_title_confirmed(d, name, partial):
                print(f"[+] 업체 확인(시트): '{name}' ({time.time() - t0:.2f}s)")
                return True
            print("[!] 클릭 후 목표 업체 미확인 (무효 클릭)")
            return False

    print(f"[!] '{name}' 검색 결과 확인 실패 (timeout {timeout:.0f}s)")
    return False


def pick_departure_suggestion(d: u2.Device, region: str):
    """출발지 자동완성 목록(suggest_list)에서 region 으로 시작하는 행정구역 후보를
    랜덤으로 골라 좌표 클릭. (항목은 resource-id 가 없고 글자에 zero-width 가 섞여
    있어 text selector 가 불안정 → 좌표 클릭) 선택한 텍스트 반환, 없으면 None."""
    root = ET.fromstring(d.dump_hierarchy())
    cands = []
    for n in root.iter("node"):
        if n.get("resource-id", "").endswith("/suggest_list"):
            for c in n.iter("node"):
                t = c.get("text", "").replace("​", "").strip()
                b = c.get("bounds", "")
                # 행정구역 후보: 입력 지역명으로 시작 + 거리(km)/긴 주소줄이 아님
                if t.startswith(region) and "km" not in t and len(t) <= len(region) + 6:
                    cands.append((t, b))
            break
    if cands:
        # 행정구역 우선 선택: 정확일치('칠곡군') > '○○청'(시청/군청) > 나머지.
        # 법원/주유소 같은 일반 POI 는 클릭 시 '선택' 버튼이 없는 결과목록으로 빠져
        # 출발지 확정이 안 되므로 피한다.
        def rank(t):
            if t == region:
                return 0
            if t.endswith("청"):
                return 1
            return 2
        cands.sort(key=lambda x: rank(x[0]))
        t, b = cands[0]
        cx, cy = _bounds_center(b)
        d.click(cx, cy)
        # 자동완성 클릭 후 화면 전환/로딩에 시간이 걸리고(폴드 dump 는 로딩 중 비어 보임),
        # 항목 종류에 따라 '선택' 버튼이 뜨는 화면 또는 바로 경로화면으로 간다.
        # 넉넉히 폴링하며 '선택'이 보이면 눌러 출발지를 확정한다.
        for _ in range(10):
            time.sleep(1.2)
            if d(text="선택").exists:
                d(text="선택").click()
                time.sleep(1.5)
                break
            if d(text="안내시작").exists:   # 이미 경로화면 = 출발지 확정됨
                break
        return t
    # fallback: 자동완성 후보가 없으면 검색 결과 목록의 첫 '선택' 버튼으로 확정
    for _ in range(8):
        time.sleep(1.2)
        if d(text="선택").exists:
            d(text="선택").click()
            time.sleep(1.5)
            return f"{region} (검색결과 선택)"
        if d(text="안내시작").exists:
            return f"{region} (직행)"
    return None


def route_from_map(d: u2.Device, regions=None) -> bool:
    """길찾기: 상세화면 '도착' → 출발지칸에 랜덤 한국 지역명 입력 →
    자동완성 결과(행정구역) 중 아무거나 선택 → 경로 결과. (한국 내 100% 보장)"""
    regions = regions or KOREA_REGIONS
    print("[*] 길찾기 시작 (상세화면 '도착' 버튼)")
    # 1) 업체 상세 하단 '도착' 버튼 = 이 업체를 도착지로 길찾기 진입 (id 없어 text 클릭)
    dest = d(text="도착")
    if not dest.wait(timeout=10):
        print("[!] '도착' 버튼을 찾지 못했습니다.")
        return False
    dest.click()
    time.sleep(3.0)

    # 2) 출발지 입력칸(start_point) 클릭 → 출발지 선택화면
    start = d(resourceId=RID("start_point"))
    if not start.wait(timeout=10):
        print("[!] 출발지 입력칸(start_point)을 찾지 못했습니다.")
        return False
    start.click()
    time.sleep(1.5)

    # 3) 출발지 검색칸(query)에 랜덤 한국 지역명 입력 (타이핑)
    sq = d(resourceId=RID("query"))
    if not sq.wait(timeout=8):
        print("[!] 출발지 검색칸(query)을 찾지 못했습니다.")
        return False
    region = random.choice(regions)
    sq.click()
    time.sleep(0.4)
    d.clear_text()
    for ch in region:
        d.send_keys(ch)
        time.sleep(0.1)
    print(f"[*] 출발지 입력: '{region}'")

    # 4) 자동완성 결과가 뜨면 그 지역 행정구역 항목 중 아무거나 선택
    picked = None
    for _ in range(8):
        time.sleep(1.0)
        picked = pick_departure_suggestion(d, region)
        if picked:
            break
    if not picked:
        print(f"[!] '{region}' 자동완성 결과를 찾지 못했습니다.")
        return False
    # 경로 결과 화면까지만 도달하고 종료 (안내시작은 누르지 않음).
    # 이후 앱 종료 → 데이터 OFF/ON → 앱데이터 삭제는 로테이션 마무리가 처리.
    print(f"[+] 출발지 선택: '{picked}' → 경로 결과 도달 (안내시작 미사용)")
    time.sleep(2.5)
    return True


def _find_home_tab_coords(d: u2.Device):
    """풀 상세 탭바 '홈' 좌표. 시스템 내비 홈과 구분."""
    _, h = d.window_size()
    root = ET.fromstring(d.dump_hierarchy())
    cands = []
    for n in root.iter("node"):
        if n.get("package") != PKG:
            continue
        if n.get("text", "").strip() != "홈":
            continue
        b = n.get("bounds", "")
        if not b:
            continue
        cx, cy = _bounds_center(b)
        if cy < h * 0.35 or cy > h * 0.88:
            continue
        cands.append((cy, cx, cy))
    if not cands:
        return None
    cands.sort()
    _, cx, cy = cands[0]
    return cx, cy


def _wait_detail_home_tab(d: u2.Device, timeout: float = 4.0) -> bool:
    """업체명 클릭 후 풀 상세 탭바(홈)가 뜰 때까지 폴링."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _find_home_tab_coords(d):
            return True
        time.sleep(0.25)
    return False


def click_place_name_on_detail(d: u2.Device, name: str, partial: bool = False,
                               fast: bool = False) -> bool:
    """하단 시트/상세화면의 업체명(제목) 클릭 → 풀 상세 페이지 진입. '도착'은 누르지 않음."""
    print(f"[*] 상세화면 업체명 클릭: '{name}' (부분일치={partial})")
    if not _sheet_title_confirmed(d, name, partial):
        print(f"[!] 화면에서 '{name}' 업체명을 확인하지 못했습니다.")
        return False

    def after_click():
        if fast:
            if _wait_detail_home_tab(d, timeout=6.0):
                return
            time.sleep(0.3)
        else:
            time.sleep(2.5)

    list_el = _list_target_el(d, name, partial)
    if list_el.exists:
        list_el.click()
        after_click()
        print(f"[+] 업체명 클릭 완료 (목록)")
        return True

    if _click_sheet_title(d, name, partial):
        after_click()
        print("[+] 업체명 클릭 완료 (시트 제목)")
        return True

    print(f"[!] '{name}' 업체명 클릭 실패")
    return False


def click_home_tab(d: u2.Device) -> bool:
    """풀 상세 페이지 탭바의 '홈' 1회 클릭."""
    print("[*] 홈 탭 클릭")
    coords = _find_home_tab_coords(d)
    if coords:
        cx, cy = coords
        d.click(cx, cy)
        time.sleep(0.15)
        print("[+] 홈 탭 클릭 완료")
        return True

    print("[!] '홈' 탭을 찾지 못했습니다. (업체명 클릭 후 풀 상세 진입 여부 확인)")
    return False


# ── 체류형: 풀 상세 탭(홈·사진·후기 등) 클릭 + 스크롤 ─────────────────────
DETAIL_DWELL_TABS = ("홈", "메뉴", "사진", "후기", "블로그", "랭킹", "정보")
# 일반트래픽(search): 홈/랭킹/정보 제외 — 메뉴·사진·후기·블로그만
GENERAL_TRAFFIC_TABS = ("홈", "메뉴", "사진", "후기", "예약", "혜택소식", "블로그", "랭킹", "정보", "주변")
DEFAULT_DWELL_MIN = 15.0
DEFAULT_DWELL_MAX = 20.0
TAB_Y_MIN_RATIO = 0.20   # 시스템 내비·하단 '도착' 버튼 제외
TAB_Y_MAX_RATIO = 0.80   # 탭바는 상세 패널 상단~중단
_DWELL_PATTERN_LOG: list = []   # 최근 로테이션별 탭 방문 기록 (패턴 중복 완화)
_MAX_DWELL_PATTERN_LOG = 24


def _collect_visible_tabs(d: u2.Device, candidates) -> list:
    """화면에 **지금 보이는** 탭만 수집 (탭바 슬라이드 없음, 클릭 가능한 것만)."""
    _, h = d.window_size()
    y_min, y_max = h * TAB_Y_MIN_RATIO, h * TAB_Y_MAX_RATIO
    seen = set()
    ordered = []
    try:
        root = ET.fromstring(d.dump_hierarchy())
    except Exception:
        root = None
    if root is not None:
        for n in root.iter("node"):
            if n.get("package") != PKG:
                continue
            t = _norm_text(n.get("text", ""))
            if t not in candidates or t in seen:
                continue
            b = n.get("bounds", "")
            if not b:
                continue
            _, cy = _bounds_center(b)
            if not (y_min <= cy <= y_max):
                continue
            seen.add(t)
            ordered.append(t)
    if not ordered:
        for name in candidates:
            if _detail_tab_visible(d, name):
                ordered.append(name)
    return ordered


def _find_detail_tab_coords(d: u2.Device, tab_name: str):
    """풀 상세 탭바에서 tab_name 좌표. 시스템 '홈' 내비와 구분."""
    _, h = d.window_size()
    root = ET.fromstring(d.dump_hierarchy())
    cands = []
    for n in root.iter("node"):
        if n.get("package") != PKG:
            continue
        t = _norm_text(n.get("text", ""))
        if t != tab_name:
            continue
        b = n.get("bounds", "")
        if not b:
            continue
        cx, cy = _bounds_center(b)
        if cy < h * TAB_Y_MIN_RATIO or cy > h * TAB_Y_MAX_RATIO:
            continue
        cands.append((cy, cx, cy))
    if not cands:
        return None
    cands.sort()
    _, cx, cy = cands[0]
    return cx, cy


def _detail_tab_visible(d: u2.Device, tab_name: str) -> bool:
    return _find_detail_tab_coords(d, tab_name) is not None


def click_detail_tab(d: u2.Device, tab_name: str) -> bool:
    """풀 상세 탭바에서 tab_name 클릭.

    '사진' 등은 갤러리 캡션과 문자열이 겹칠 수 있어, dump 좌표(탭바 y대역)를 우선한다.
    """
    coords = _find_detail_tab_coords(d, tab_name)
    if coords:
        cx, cy = coords
        d.click(cx, cy)
        time.sleep(0.45)
        return True
    # 폴백: 탭바 y 대역 안의 text 노드만
    el = d(text=tab_name, packageName=PKG)
    if el.exists(timeout=0):
        _, h = d.window_size()
        try:
            info = el.info
            bounds = info.get("bounds") or {}
            cy = (bounds.get("top", 0) + bounds.get("bottom", 0)) / 2
            cx = (bounds.get("left", 0) + bounds.get("right", 0)) / 2
            if h * TAB_Y_MIN_RATIO <= cy <= h * TAB_Y_MAX_RATIO:
                d.click(int(cx), int(cy))
                time.sleep(0.45)
                return True
        except Exception:
            pass
    return False


def _scroll_detail_to_top(d: u2.Device, times: int = 3):
    """상세 본문을 위로 끌어올려 상단 메인 탭바를 다시 노출시킨다."""
    sx = random.uniform(0.46, 0.54)
    for _ in range(times):
        d.swipe(sx, random.uniform(0.30, 0.38), sx, random.uniform(0.70, 0.80),
                duration=random.uniform(0.30, 0.45))
        time.sleep(0.25)


def _swipe_tabbar_horizontally(d: u2.Device):
    """탭바(y 대역)를 좌우로 스와이프해 우측 탭(블로그/랭킹 등)을 노출."""
    _, h = d.window_size()
    y = h * random.uniform(0.24, 0.34)
    if random.random() < 0.5:
        d.swipe(0.75, y / h, 0.25, y / h, duration=0.35)   # 좌로 → 우측탭 노출
    else:
        d.swipe(0.25, y / h, 0.75, y / h, duration=0.35)   # 우로 → 좌측탭 복귀
    time.sleep(0.4)


def _scroll_detail_content(d: u2.Device, duration: float = None):
    """상세 **본문** 세로 스크롤 (탭바는 건드리지 않음)."""
    if duration is None:
        duration = random.uniform(0.35, 0.55)
    sx = random.uniform(0.46, 0.54)
    sy1 = random.uniform(0.68, 0.76)
    sy2 = random.uniform(0.32, 0.42)
    d.swipe(sx, sy1, sx, sy2, duration=duration)


def _dwell_tab_weights(visible: list, last_tab: Optional[str]) -> dict:
    """최근 로테이션·직전 탭을 반영한 가중치 (자주 쓴 탭은 확률↓)."""
    counts = {t: 0 for t in visible}
    for seq in _DWELL_PATTERN_LOG[-_MAX_DWELL_PATTERN_LOG:]:
        for i, t in enumerate(seq):
            if t not in counts:
                continue
            counts[t] += 1
            if i == 0:
                counts[t] += 0.5
    weights = {}
    for t in visible:
        w = 1.0 / (1.0 + counts[t] * random.uniform(0.6, 1.0))
        if t == last_tab:
            w *= 0.1
        weights[t] = max(w, 0.05)
    return weights


def _pick_dwell_tab_click(visible: list, last_tab: Optional[str]) -> Optional[str]:
    """보이는 탭 중 가중치 랜덤 선택 (슬라이드 없이 클릭만)."""
    if not visible:
        return None
    pool = [t for t in visible if t != last_tab] or list(visible)
    weights = _dwell_tab_weights(pool, last_tab)
    wlist = [weights[t] for t in pool]
    return random.choices(pool, weights=wlist, k=1)[0]


def _sequence_too_similar(candidate: list, recent: list) -> bool:
    """최근 체류와 앞 3탭 순서가 같으면 True."""
    if not candidate or not recent:
        return False
    head = tuple(candidate[:3])
    for old in recent[-8:]:
        if tuple(old[:3]) == head:
            return True
    return False


def dwell_on_place_detail(d: u2.Device, min_secs: float = 15.0, max_secs: float = 20.0,
                          tabs=None) -> bool:
    """풀 상세: **보이는 탭만 랜덤 클릭** + 본문 스크롤 (탭바 슬라이드 없음)."""
    tabs = tabs or DETAIL_DWELL_TABS
    target = random.uniform(min_secs, max_secs)
    print(f"[*] 체류 시작 (목표 {target:.1f}s, 클릭만·슬라이드 없음)")
    t0 = time.time()
    visited = []
    last_tab = None
    tried_home = False

    def remaining() -> float:
        return max(0.0, target - (time.time() - t0))

    allow_home_fallback = "홈" in tabs
    visible = _collect_visible_tabs(d, tabs)
    if not visible and allow_home_fallback and not tried_home:
        click_home_tab(d)
        tried_home = True
        time.sleep(random.uniform(0.3, 0.55))
        visible = _collect_visible_tabs(d, tabs)

    if visible:
        plan = list(visible)
        random.shuffle(plan)
        for _ in range(12):
            if not _sequence_too_similar(plan, _DWELL_PATTERN_LOG):
                break
            random.shuffle(plan)
        print(f"    보이는 탭: {visible} / 1차 순서: {plan[:4]}")

    while remaining() > 0.3:
        visible = _collect_visible_tabs(d, tabs)
        if not visible:
            if allow_home_fallback and not tried_home:
                click_home_tab(d)
                tried_home = True
                time.sleep(random.uniform(0.3, 0.5))
                visible = _collect_visible_tabs(d, tabs)
        if not visible:
            _scroll_detail_content(d)
            time.sleep(min(random.uniform(0.8, 1.4), remaining()))
            continue

        tab = _pick_dwell_tab_click(visible, last_tab)
        if tab and click_detail_tab(d, tab):
            last_tab = tab
            visited.append(tab)
            print(f"    탭 클릭 '{tab}' (보임={visible}, {remaining():.1f}s 남음)")
            time.sleep(min(random.uniform(0.45, 1.1), remaining()))
            # 탭 내부를 잠깐 스크롤하며 체류
            n_scroll = random.randint(1, random.randint(2, 4))
            for _ in range(n_scroll):
                if remaining() <= 0.2:
                    break
                _scroll_detail_content(d)
                time.sleep(min(random.uniform(0.55, 1.4), remaining()))
            # 다음 탭을 위해 상단 메인 탭바를 다시 노출 (안 그러면 같은 섹션만 반복)
            if remaining() > 1.0:
                _scroll_detail_to_top(d)
                if random.random() < 0.4:
                    _swipe_tabbar_horizontally(d)  # 우측 탭(블로그/랭킹) 노출
        else:
            # 탭을 못 찾으면 상단으로 올려 탭바 재노출 후 재시도
            _scroll_detail_to_top(d, times=2)
            if random.random() < 0.5:
                _swipe_tabbar_horizontally(d)
            time.sleep(min(random.uniform(0.5, 1.0), remaining()))

    if visited:
        _DWELL_PATTERN_LOG.append(tuple(visited))
        if len(_DWELL_PATTERN_LOG) > _MAX_DWELL_PATTERN_LOG:
            del _DWELL_PATTERN_LOG[: len(_DWELL_PATTERN_LOG) - _MAX_DWELL_PATTERN_LOG]

    elapsed = time.time() - t0
    print(f"[+] 체류 완료 ({elapsed:.1f}s, 클릭 탭={visited or ['본문 스크롤만']})")
    return True


def general_traffic_from_detail(
    d: u2.Device,
    name: str,
    partial: bool = False,
    *,
    dwell_min: float = DEFAULT_DWELL_MIN,
    dwell_max: float = DEFAULT_DWELL_MAX,
) -> bool:
    """일반트래픽: 업체명 클릭 → 메뉴/사진/후기/블로그 랜덤 클릭·스크롤 체류."""
    if dwell_max < dwell_min:
        dwell_min, dwell_max = dwell_max, dwell_min
    print(
        f"[*] 일반트래픽 시작 (도착 미사용, 체류 {dwell_min:.0f}~{dwell_max:.0f}s, "
        f"탭={list(GENERAL_TRAFFIC_TABS)})"
    )
    # 업체명 클릭으로 풀 상세 확장 시도(실패해도 진행 — 이미 탭이 있을 수 있음)
    clicked_name = click_place_name_on_detail(d, name, partial=partial, fast=True)
    if not clicked_name:
        print("[*] 업체명 클릭 실패 — 현재 상세에서 탭 확인 후 진행")
    # 풀 상세 탭바가 뜰 때까지 대기(스크롤로 상단 노출도 시도)
    deadline = time.time() + 8.0
    tabbar = False
    while time.time() < deadline:
        if _collect_visible_tabs(d, GENERAL_TRAFFIC_TABS):
            tabbar = True
            break
        _scroll_detail_to_top(d, times=1)
        time.sleep(0.4)
    if not tabbar:
        print("[!] 상세 탭바 미확인 — 그래도 체류 시도(스크롤 위주)")
    return dwell_on_place_detail(
        d,
        min_secs=dwell_min,
        max_secs=dwell_max,
        tabs=GENERAL_TRAFFIC_TABS,
    )


def make_code_fetcher(code_api: str, account_id: int):
    """추가인증 시 서버(컨트롤플레인)가 아웃룩 메일함에서 코드를 읽어오게 하는 콜백."""
    if not code_api or not account_id:
        return None
    import json as _json
    import urllib.request as _u

    def fetch():
        base = code_api.rstrip("/")
        for attempt in range(3):
            url = f"{base}/api/accounts/kakao/{account_id}/fetch-code?wait=120"
            try:
                req = _u.Request(url, headers={"User-Agent": "Mozilla/5.0 (worker; kakao-traffic)"})
                with _u.urlopen(req, timeout=200) as r:
                    j = _json.load(r)
            except Exception as e:
                print(f"[code] 서버조회 오류({attempt+1}/3): {e}", flush=True)
                time.sleep(10)
                continue
            if not j.get("available"):
                print(f"[code] 서버조회 불가: {j.get('detail')}", flush=True)
                return ""
            if j.get("code"):
                print(f"[code] 서버조회 성공: {j['code']} ({j.get('detail')})", flush=True)
                return j["code"]
            print(f"[code] 코드 미도착({attempt+1}/3): {j.get('detail')} — 재시도", flush=True)
            time.sleep(10)
        return ""

    return fetch


def run_once(d: u2.Device, args) -> tuple:
    """1 로테이션. Returns: (작업 성공 여부, 카카오맵 실행 여부)."""
    print("[*] 로테이션 시작 전 데이터·IP 확인")
    if not ensure_network(args.serial, timeout=args.recover_secs):
        print("[!] 연결 미확인 → 카카오맵 진입 생략 (이번 로테이션 스킵)")
        return False, False
    fast_entry = args.mode == "search"
    login_mode = bool(getattr(args, "kakao_id", "") and getattr(args, "kakao_pw", ""))
    if not login_mode:
        open_kakaomap(d, args.serial)
    else:
        # 로그인 모드: leased 계정으로 매번 새로 로그인. 진입 전 카카오맵+Chrome
        # 데이터를 지워 이전 계정 세션·쿠키를 제거하고, 실패 시 최대 3회 재시도.
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        import kakao_login

        cfetch = make_code_fetcher(getattr(args, "code_api", ""), getattr(args, "account_id", 0))
        result, detail = "login_failed", ""
        for attempt in range(1, 4):
            print(f"[*] 로그인 시도 {attempt}/3 — 카카오맵+Chrome 데이터 삭제")
            _adb(args.serial, "shell", "pm", "clear", PKG)
            _adb(args.serial, "shell", "pm", "clear", "com.android.chrome")
            time.sleep(2.5)
            open_kakaomap(d, args.serial)
            result, detail = kakao_login.login(d, args.kakao_id, args.kakao_pw, code_fetcher=cfetch, force=True)
            if result == kakao_login.OK:
                break
            if result == kakao_login.BAD_CREDENTIAL:
                break  # 재시도 무의미
            print(f"[!] 로그인 결과 {result} — {'재시도' if attempt < 3 else '최종 실패'}")
            kakao_login.abandon_login(d, args.serial)
            time.sleep(2.0)
        args.login_result = result
        args.login_detail = detail
        if result != kakao_login.OK:
            print(f"[!] 로그인 실패({result}) → 이번 로테이션 스킵")
            return False, True
        print("[+] 로그인 완료 — 트래픽 시작")

    search_keyword(d, args.keyword, fast=fast_entry, serial=args.serial)
    ok = click_place(d, args.place, partial=args.partial, fast=fast_entry,
                     search_timeout=args.search_timeout)
    if ok and args.mode == "route":
        ok = route_from_map(d)
    elif ok and args.mode == "search":
        ok = general_traffic_from_detail(
            d,
            args.place,
            partial=args.partial,
            dwell_min=args.dwell_min,
            dwell_max=args.dwell_max,
        )
    return ok, True


def main():
    ap = argparse.ArgumentParser(description="카카오맵 검색 자동화 (N회 반복)")
    ap.add_argument("keyword", nargs="?", default="영월 닭강정 가나닭강정",
                    help="검색 키워드 (기본: 영월 닭강정 가나닭강정)")
    ap.add_argument("place", nargs="?", default="영월가나닭강정",
                    help="클릭할 업체명 (기본: 영월가나닭강정)")
    ap.add_argument("--serial", default=DEFAULT_SERIAL,
                    help=f"adb 시리얼 (기본: 실물 기기 {DEFAULT_SERIAL})")
    ap.add_argument("--partial", action=argparse.BooleanOptionalAction, default=True,
                    help="업체명 부분일치로 검색 (기본 켜짐, --no-partial 로 끔)")
    ap.add_argument("--mode", choices=("route", "search"), default="route",
                    help="route=길찾기(도착), search=일반트래픽(업체명+탭체류, 기본 route)")
    ap.add_argument("--route", action=argparse.BooleanOptionalAction, default=None,
                    help="(호환) --route/--no-route → --mode route/search")
    ap.add_argument("--dwell-min", type=float, default=DEFAULT_DWELL_MIN,
                    help=f"일반트래픽 체류 최소(초, 기본 {DEFAULT_DWELL_MIN:.0f})")
    ap.add_argument("--dwell-max", type=float, default=DEFAULT_DWELL_MAX,
                    help=f"일반트래픽 체류 최대(초, 기본 {DEFAULT_DWELL_MAX:.0f})")
    ap.add_argument("--drags", type=int, default=3,
                    help="(route 모드/LD용) 지도 랜덤 드래그 횟수")
    ap.add_argument("--search-timeout", type=float, default=SEARCH_RESULT_TIMEOUT,
                    help="검색 후 목표 업체 확인 대기(초, 기본 45)")
    ap.add_argument("--rotations", type=int, default=0,
                    help="반복 횟수 (기본 0 = 무한 반복, Ctrl+C 로 중단)")
    ap.add_argument("--no-data-toggle", action="store_true",
                    help="로테이션 사이 모바일 데이터 OFF/ON 비활성화")
    ap.add_argument("--off-secs", type=float, default=4.0,
                    help="모바일 데이터 OFF 유지 시간(초, 기본 4)")
    ap.add_argument("--recover-secs", type=float, default=25.0,
                    help="데이터 ON 후 연결 복구 대기(초, ping 확인, 기본 25)")
    ap.add_argument("--no-clear", action="store_true",
                    help="로테이션 사이 앱 데이터(pm clear) 삭제 비활성화")
    ap.add_argument("--kakao-id", default="",
                    help="카카오 로그인 계정(이메일). 지정하면 로그인 기반 트래픽")
    ap.add_argument("--kakao-pw", default="",
                    help="카카오 로그인 비밀번호")
    ap.add_argument("--account-id", type=int, default=0,
                    help="웹 계정 id — 추가인증 시 서버가 이 계정 메일함에서 코드를 읽어옴")
    ap.add_argument("--code-api", default="",
                    help="서버 API 주소(예: http://SERVER:8080) — 추가인증 코드 서버조회용")
    args = ap.parse_args()
    if args.route is not None:
        args.mode = "route" if args.route else "search"

    data_toggle = not args.no_data_toggle
    do_clear = not args.no_clear
    args.login_result = ""
    args.login_detail = ""
    login_mode = bool(args.kakao_id and args.kakao_pw)
    d = connect(args.serial)
    setup_popup_watchers(d, login_mode=login_mode)
    if login_mode:
        print(f"[*] 로그인 모드: {args.kakao_id}")
    results = []        # 로테이션별 작업 성공 여부
    ip_changes = []     # 로테이션별 IP 변경 성공 여부
    infinite = args.rotations <= 0   # 0 이하면 무한 반복(Ctrl+C 로 중단)
    total = "∞" if infinite else str(args.rotations)
    mode_label = "길찾기" if args.mode == "route" else "일반트래픽"
    print(f"[*] 모드: {mode_label} (--mode {args.mode})")
    if args.mode == "search":
        print(f"[*] 체류: {args.dwell_min:.0f}~{args.dwell_max:.0f}s (메뉴/사진/후기/블로그)")
    i = 0
    try:
        while infinite or i < args.rotations:
            i += 1
            print(f"\n===== 로테이션 {i}/{total} =====")
            try:
                ok, opened = run_once(d, args)
            except Exception as e:
                print(f"[!] 로테이션 {i} 작업 중 오류: {e}")
                ok, opened = False, False
            # 스크린샷은 항상 같은 파일에 덮어써 최근 결과만 보관(무한 반복 시 누적 방지)
            if opened:
                try:
                    d.screenshot("kakao_result.png")
                except Exception:
                    pass
            if not opened:
                print(f"[*] 로테이션 {i} (네트워크 미연결 — 카카오맵 미진입)")
            else:
                print(f"[*] 로테이션 {i} (작업 성공={ok})")
            results.append(ok)

            print(f"[*] 로테이션 {i} 마무리")
            if opened:
                d.app_stop(PKG)
            if data_toggle:
                changed = toggle_mobile_data(
                    args.serial, off_secs=args.off_secs,
                    recover_timeout=args.recover_secs)
                ip_changes.append(changed)
            if do_clear and opened:
                print("[*] 앱 데이터 삭제(pm clear)")
                _adb(args.serial, "shell", "pm", "clear", PKG)
                time.sleep(2.5)
            elif do_clear and not opened:
                print("[*] 카카오맵 미진입 — pm clear 생략")

            # 누적 진행 요약 (무한 반복 중 현재 성적 확인용)
            ipc = f", IP변경 {sum(ip_changes)}/{len(ip_changes)}" if ip_changes else ""
            print(f"[누적] 작업 성공 {sum(results)}/{len(results)}{ipc}")
    except KeyboardInterrupt:
        print("\n[*] 사용자 중단(Ctrl+C)")
    finally:
        # ── 최종 요약 ─────────────────────────────────────────────
        print("\n========== 최종 요약 ==========")
        print(f"[+] 작업 성공: {sum(results)}/{len(results)} 로테이션")
        if getattr(args, "login_result", ""):
            print(f"LOGIN_RESULT={args.login_result} {args.login_detail}")
        if ip_changes:
            print(f"[+] IP 변경 성공: {sum(ip_changes)}/{len(ip_changes)} 회")
        try:
            d.watcher.stop()
        except Exception:
            pass

    # 워커는 exit code 로 success/fail 판정 — 실제 작업 실패를 반드시 비-0 으로
    ok_n = sum(1 for r in results if r)
    if not results or ok_n == 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
