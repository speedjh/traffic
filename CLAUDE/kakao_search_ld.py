# -*- coding: utf-8 -*-
"""
LDPlayer 에뮬레이터의 카카오맵에서 키워드를 검색하고 특정 업체를 클릭하는 자동화 스크립트.

사용법:
    python kakao_search.py "검색키워드" "업체명"
    python kakao_search.py "스타벅스" "스타벅스 제천장락DT점"
    python kakao_search.py "스타벅스" "장락DT" --partial        # 업체명 부분일치
    python kakao_search.py "스타벅스" "장락DT" --serial 127.0.0.1:5557

요구사항:
    - LDPlayer 인스턴스에서 ADB 디버깅이 켜져 있어야 함
    - pip install uiautomator2
"""
import os
import sys
import time
import math
import random
import argparse
import subprocess

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

# ── 팝업에서 누르면 닫히는 버튼 후보 (텍스트 기준) ───────────────────────────
# 주의: "닫기"/"확인"/"취소" 같은 일반 텍스트는 카카오맵 정상 UI(예: 검색창의
# 'X' 버튼은 content-desc="닫기")와 충돌하므로 넣지 않는다.
# → 팝업/권한창에서만 등장하는 고유 텍스트로만 한정한다.
POPUP_DISMISS_TEXTS = [
    # 앱 데이터 초기화 후 첫 진입 온보딩(IntroActivity) — 로그인 없이 시작
    "비로그인으로 시작하기",
    # 안드로이드 시스템 권한 다이얼로그
    "앱 사용 중에만 허용", "이번만 허용", "허용",
    "while using the app", "only this time", "allow",
    # 카카오맵/광고 이벤트 팝업 (정상 화면엔 없는 고유 문구)
    "오늘 그만보기", "오늘은 그만보기", "오늘 하루 보지 않기",
    "다시 보지 않기", "다시 보지않기", "다시 안 보기",
    "이 창 다시 보지 않기", "나중에 하기", "건너뛰기",
    "동의하고 시작하기",
    # 데이터 초기화 후 '혜택 알림 설정' 다이얼로그 (id/notYet) — 수신 거부 쪽
    "지금은 아니에요",
    # 위치 정확도 사용 제안(LocationSettingsCheckerActivity) — 거부 쪽
    # (출발지는 지도에서 수동 선택하므로 GPS 정확도 불필요)
    "아니요",
]


def setup_popup_watchers(d: u2.Device):
    """백그라운드에서 팝업을 감지해 자동으로 닫는 watcher 등록 (xpath 기반)."""
    try:
        d.watcher.reset()
    except Exception:
        pass
    for i, t in enumerate(POPUP_DISMISS_TEXTS):
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
    d.watcher.start(interval=1.0)  # 1초마다 화면 검사
    print("[*] 팝업 자동 처리 watcher 활성화")


def _adb(serial: str, *args, timeout: float = 25.0) -> subprocess.CompletedProcess:
    """`adb -s <serial> <args>` 실행 (stdout/stderr 캡처)."""
    return subprocess.run([ADB_EXE, "-s", serial, *args],
                          capture_output=True, text=True, timeout=timeout)


def toggle_mobile_data(serial: str, off_secs: float = 4.0, recover_secs: float = 8.0):
    """모바일(셀룰러) 데이터를 껐다 켜서 통신사 IP 를 재할당.
    실기기/대부분의 안드로이드에서 adb shell uid 권한으로 `svc data` 가 동작한다.
    (Wi-Fi 는 건드리지 않음. IP 변경 목적이면 기기에서 Wi-Fi 를 꺼두고 LTE 만 사용)"""
    print("[*] 모바일 데이터 OFF (svc data disable)")
    _adb(serial, "shell", "svc", "data", "disable")
    time.sleep(off_secs)
    print("[*] 모바일 데이터 ON (svc data enable)")
    _adb(serial, "shell", "svc", "data", "enable")
    print(f"[*] 네트워크 복구 대기 {recover_secs:.0f}s")
    time.sleep(recover_secs)


def connect(serial: str) -> u2.Device:
    print(f"[*] adb 연결: {serial}")
    os.system(f'"{os.path.join(LD_DIR, "adb.exe")}" connect {serial} >NUL 2>&1')
    d = u2.connect(serial)
    d.settings["wait_timeout"] = 15.0
    print(f"[+] 연결됨: {d.info.get('productName')} / {d.serial}")
    return d


def open_kakaomap(d: u2.Device):
    print("[*] 카카오맵 실행")
    d.app_start(PKG, stop=True)
    # 메인 화면의 검색 버튼이 뜰 때까지 대기 (로딩 + 초기 팝업은 watcher 가 처리)
    if not d(resourceId=RID("btn_search")).wait(timeout=30):
        # 혹시 검색버튼 id 가 안 보이면 검색 EditText 라도 대기
        d(resourceId=RID("query")).wait(timeout=10)
    time.sleep(2.0)  # 지도 초기화 안정화 (첫 탭이 무시되는 것 방지)
    print("[+] 메인 화면 로딩 완료")


def open_search_screen(d: u2.Device, retries: int = 4):
    """메인의 검색 버튼을 눌러 검색 입력창(query)이 나올 때까지 재시도."""
    for i in range(retries):
        if d(resourceId=RID("query")).exists:
            return True
        if d(resourceId=RID("btn_search")).exists:
            d(resourceId=RID("btn_search")).click()
        if d(resourceId=RID("query")).wait(timeout=6):
            return True
        print(f"    검색화면 전환 재시도 {i+1}/{retries}")
        time.sleep(1.0)
    return d(resourceId=RID("query")).exists


def search_keyword(d: u2.Device, keyword: str):
    print(f"[*] 검색창 열기 → '{keyword}' 입력")
    if not open_search_screen(d):
        raise RuntimeError("검색 입력창(query)을 열지 못했습니다.")
    query = d(resourceId=RID("query"))
    query.click()
    time.sleep(0.5)
    d.clear_text()
    d.send_keys(keyword)
    time.sleep(1.0)
    d.press("enter")  # IME 검색 실행
    # 실제 결과 항목(place_name)이 채워질 때까지 폴링 (목록 컨테이너는 먼저 생겨도 항목은 늦게 옴)
    if d(resourceId=RID("place_name")).wait(timeout=20):
        print("[+] 검색 결과 목록 표시됨")
    else:
        print("[!] 결과 항목을 확인하지 못했습니다 (단일 결과로 바로 이동했을 수 있음)")
    time.sleep(1.0)


def click_place(d: u2.Device, name: str, partial: bool = False, max_scroll: int = 8) -> bool:
    print(f"[*] 업체 클릭 시도: '{name}' (부분일치={partial})")
    # 결과 항목이 최소 1개라도 로드될 때까지 먼저 대기 (성급한 스크롤로 화면 이탈 방지)
    d(resourceId=RID("place_name")).wait(timeout=15)

    def target():
        return d(resourceId=RID("place_name"), textContains=name) if partial \
            else d(resourceId=RID("place_name"), text=name)

    for attempt in range(max_scroll + 1):
        if target().exists:
            target().click()
            time.sleep(2.5)
            print(f"[+] '{name}' 클릭 완료 → 상세 화면 이동")
            return True
        # 결과 항목 자체가 없으면(목록 이탈) 더는 스크롤하지 않음
        if not d(resourceId=RID("place_name")).exists:
            print("    결과 목록이 화면에 없어 스크롤 중단")
            break
        print(f"    [{attempt+1}/{max_scroll}] 현재 화면에 없음 → 목록 스크롤")
        # 결과 목록 영역(하단 시트)에서 위로 스와이프하여 다음 항목 로드
        d.swipe(0.5, 0.75, 0.5, 0.35, duration=0.3)
        time.sleep(1.2)
    print(f"[!] '{name}' 항목을 찾지 못했습니다.")
    return False


def route_from_map(d: u2.Device, drags: int = 3) -> bool:
    """업체 상세화면에서 길찾기 진행 → 출발지를 '지도에서 선택' →
    지도를 랜덤하게 드래그해 핀을 아무 위치로 옮긴 뒤 '출발지로' 확정."""
    print("[*] 길찾기 시작")
    # 1) 상세화면의 길찾기 버튼.
    #    업체에 따라 하단 요약시트(btn_summary_route) 또는 풀 상세페이지(btn_route)로
    #    열리므로 두 id 를 모두 시도하고, 안되면 content-desc="길찾기"로 잡는다.
    btn_route = None
    for rid in ("btn_summary_route", "btn_route"):
        el = d(resourceId=RID(rid))
        if el.wait(timeout=6):
            btn_route = el
            break
    if btn_route is None and d(description="길찾기").wait(timeout=6):
        btn_route = d(description="길찾기")
    if btn_route is None:
        print("[!] 길찾기 버튼을 찾지 못했습니다 (btn_summary_route/btn_route/길찾기).")
        return False
    btn_route.click()
    time.sleep(2.0)

    # 2) 출발지 입력칸 (도착지는 업체로 이미 채워져 있음)
    start = d(resourceId=RID("start_point"))
    if not start.wait(timeout=10):
        print("[!] 출발지 입력칸(start_point)을 찾지 못했습니다.")
        return False
    start.click()
    time.sleep(1.5)

    # 3) '지도에서 선택'
    sel_map = d(resourceId=RID("btn_select_on_map"))
    if not sel_map.wait(timeout=10):
        print("[!] '지도에서 선택' 버튼(btn_select_on_map)을 찾지 못했습니다.")
        return False
    sel_map.click()
    time.sleep(2.5)

    # 4) 핀은 화면 중앙 고정 + 지도 선택 화면의 초기 중심은 매번 동일.
    #    → 매 로테이션 출발지가 충분히 달라지도록 "랜덤 방향으로 풀스크린 스와이프를
    #    랜덤 스텝 수만큼 누적"해 멀리(다른 동네/도시) 이동시킨다.
    angle = random.uniform(0, 2 * math.pi)        # 이동 방향(매번 랜덤)
    steps = drags + random.randint(5, 14)         # 이동 거리(강도)도 매번 랜덤·크게
    r = 0.45                                       # 스와이프 진폭(풀스크린에 가깝게)
    ux, uy = math.cos(angle), math.sin(angle)
    print(f"[*] 지도 랜덤 이동: 방향 {math.degrees(angle):.0f}°, {steps}스텝")
    for i in range(steps):
        # 화면을 (방향)으로 밀면 지도는 반대로 흐르고, 중앙 고정 핀은 그 방향 지점으로 이동
        sx, sy = 0.5 + ux * r, 0.5 + uy * r
        ex, ey = 0.5 - ux * r, 0.5 - uy * r
        clampx = lambda v: min(0.95, max(0.05, v))
        clampy = lambda v: min(0.82, max(0.20, v))  # 지도 영역(헤더~하단패널) 안으로
        d.swipe(clampx(sx), clampy(sy), clampx(ex), clampy(ey), duration=0.3)
        time.sleep(0.7)
    time.sleep(1.2)  # 지도 정착 + 핀 위치 주소 갱신 대기  # 지도 정착 + 핀 위치 주소 갱신 대기

    # 현재 핀이 가리키는 위치 주소 로그 (있으면)
    name = d(resourceId=RID("name"))
    if name.exists:
        try:
            print(f"[*] 선택 위치: {name.get_text()}")
        except Exception:
            pass

    # 5) '출발지로' 버튼으로 확정 → 경로 결과 화면으로 이동
    confirm = d(resourceId=RID("btn_route_desc"))
    if not confirm.exists:
        confirm = d(text="출발지로")
    if not confirm.wait(timeout=8):
        print("[!] '출발지로' 확정 버튼을 찾지 못했습니다.")
        return False
    confirm.click()
    time.sleep(3.0)
    print("[+] 출발지 확정 → 길찾기 경로 결과로 이동")
    return True


def run_once(d: u2.Device, args) -> bool:
    """1 로테이션: 검색 → 업체 진입 → (옵션)길찾기·지도 랜덤 출발지 선택."""
    open_kakaomap(d)
    search_keyword(d, args.keyword)
    ok = click_place(d, args.place, partial=args.partial)
    if ok and args.route:
        ok = route_from_map(d, drags=args.drags)
    return ok


def main():
    ap = argparse.ArgumentParser(description="카카오맵 검색 자동화 (N회 반복)")
    ap.add_argument("keyword", help="검색 키워드")
    ap.add_argument("place", help="클릭할 업체명")
    ap.add_argument("--serial", default="127.0.0.1:5557",
                    help="adb 시리얼 (기본: LDPlayer 인스턴스1. 실기기는 USB 시리얼/IP:포트)")
    ap.add_argument("--partial", action="store_true", help="업체명 부분일치로 검색")
    ap.add_argument("--route", action="store_true",
                    help="업체 진입 후 길찾기 진행(출발지=지도에서 랜덤 선택)")
    ap.add_argument("--drags", type=int, default=3,
                    help="지도 랜덤 드래그 횟수 (기본 3)")
    ap.add_argument("--rotations", type=int, default=1,
                    help="반복(로테이션) 횟수 (기본 1)")
    ap.add_argument("--no-data-toggle", action="store_true",
                    help="로테이션 사이 모바일 데이터 OFF/ON 비활성화")
    ap.add_argument("--off-secs", type=float, default=4.0,
                    help="모바일 데이터 OFF 유지 시간(초, 기본 4)")
    ap.add_argument("--no-clear", action="store_true",
                    help="로테이션 사이 앱 데이터(pm clear) 삭제 비활성화")
    args = ap.parse_args()

    data_toggle = not args.no_data_toggle
    do_clear = not args.no_clear
    d = connect(args.serial)
    setup_popup_watchers(d)
    results = []
    try:
        for i in range(args.rotations):
            print(f"\n===== 로테이션 {i+1}/{args.rotations} =====")
            ok = run_once(d, args)
            shot = f"kakao_result_{i+1}.png" if args.rotations > 1 else "kakao_result.png"
            d.screenshot(shot)
            print(f"[*] 로테이션 {i+1} 스크린샷: {shot} (성공={ok})")
            results.append(ok)

            # 로테이션 마무리(사용자 정의 순서):
            #   카카오맵 종료 → 모바일 데이터 OFF/ON → 앱 데이터 삭제 → (카카오맵 재실행)
            print(f"[*] 로테이션 {i+1} 마무리: 카카오맵 종료")
            d.app_stop(PKG)
            if data_toggle:
                toggle_mobile_data(args.serial, off_secs=args.off_secs)
            if do_clear:
                print("[*] 앱 데이터 삭제(pm clear)")
                _adb(args.serial, "shell", "pm", "clear", PKG)

        # 사용자 요청: 데이터 토글 후 카카오맵 켜둔 상태로 종료
        print("\n[*] 전체 로테이션 완료 → 카카오맵 실행")
        open_kakaomap(d)
        print(f"[+] 완료: 성공 {sum(results)}/{len(results)} 로테이션")
        sys.exit(0 if all(results) else 2)
    finally:
        d.watcher.stop()


if __name__ == "__main__":
    main()
