# -*- coding: utf-8 -*-
"""
[우리동네GS 클릭형] 우리동네GS 앱에서 검색 → GS25배달 상품 1개 클릭 → 종료 = 1사이클.

- 다음 트래픽과 동일한 네트워크 확인 / 데이터 OFF-ON / IP 변경 확인 / pm clear 흐름을 재사용
- 앱만 우리동네GS(com.gsr.gs25)로 변경

사용법:
    ./run_gs_click.sh --rotations 0
    ./run_gs_click.sh "폴라레티" --rotations 5
"""
import argparse
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

import uiautomator2 as u2

import daum_search_device as ds

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


GS_PKG = "com.gsr.gs25"
GS_MAIN = f"{GS_PKG}/.MainActivity"

DEFAULT_KEYWORD = "폴라레티"
GS_TYPE_CHAR_DELAY = 0.03
GS_POST_TYPE_DELAY = 0.12


def _bounds_center(bounds: str):
    x1, y1, x2, y2 = map(int, re.findall(r"-?\d+", bounds)[:4])
    return (x1 + x2) // 2, (y1 + y2) // 2


def dismiss_gs_popups(d: u2.Device, label: str = "") -> int:
    """우리동네GS 팝업/권한 — 거부·닫기만 (허용/설정/확인 금지)."""
    hit = ds.dismiss_popups_now(d, label)
    for t in (
        "닫기", "오늘은 닫기", "오늘 하루 보지 않기", "취소", "나중에", "다음에",
        *ds.PERMISSION_DENY_TEXTS,
        *ds.POPUP_SKIP_TEXTS,
    ):
        for sel in ({"text": t}, {"description": t}):
            el = d(**sel)
            if el.exists:
                try:
                    el.click()
                    hit += 1
                    time.sleep(0.35)
                except Exception:
                    pass
    for rid in ds.PERMISSION_DENY_RIDS:
        try:
            el = d(resourceId=rid)
            if el.exists:
                el.click()
                hit += 1
                time.sleep(0.35)
        except Exception:
            pass
    if hit and label:
        ds.log(f"[팝업 {label}] 처리 {hit}회")
    return hit


def _parse_bounds(bounds: str):
    try:
        return tuple(map(int, re.findall(r"-?\d+", bounds)[:4]))
    except Exception:
        return None


def _on_login_screen(d: u2.Device) -> bool:
    return (
        d(packageName=GS_PKG, descriptionContains="카카오로 계속하기").exists(timeout=0)
        or d(packageName=GS_PKG, descriptionContains="네이버로 계속하기").exists(timeout=0)
        or d(packageName=GS_PKG, descriptionContains="가입/로그인").exists(timeout=0)
    )


def _recover_from_login(d: u2.Device) -> bool:
    """로그인 유도 화면이면 뒤로 → 하단 홈."""
    if not _on_login_screen(d):
        return False
    ds.log("[*] 로그인 화면 감지 → 뒤로가기 (로그인/동의 안 함)")
    d.press("back")
    time.sleep(1.0)
    _click_bottom_nav(d, "홈")
    time.sleep(0.8)
    return True


def _clear_text_best_effort(d: u2.Device, backspaces: int = 60):
    """ADB 키보드 clear_text 실패 대비: 지우기 best-effort."""
    try:
        d.clear_text()
        return
    except Exception:
        pass
    # 백스페이스 연타로 최대한 삭제 (권한/IME 상태에 따라 clear_text가 실패할 수 있음)
    for _ in range(backspaces):
        try:
            d.press("del")
        except Exception:
            break


def open_gs(d: u2.Device, serial: str):
    ds.log("[*] 우리동네GS 실행 (커버화면 display 0)")
    ds._adb(serial, "shell", "cmd", "device_state", "state", str(ds.CLOSED_STATE))
    ds._adb(serial, "shell", "am", "force-stop", GS_PKG)
    time.sleep(0.2)
    ds._adb(
        serial, "shell", "am", "start",
        "--display", str(ds.COVER_DISPLAY),
        "-a", "android.intent.action.MAIN",
        "-c", "android.intent.category.LAUNCHER",
        "-n", GS_MAIN,
    )
    # 로딩 대기는 search_keyword()에서 '검색탭→검색창' 기준으로 처리 (더 빠르게 선행 클릭)
    time.sleep(0.8)
    ds.dismiss_popups_now(d, "우리동네GS로딩")
    dismiss_gs_popups(d, "우리동네GS로딩")


def _gs_ready(d: u2.Device) -> bool:
    # 하단 탭(홈/검색)이 보이거나, 검색 EditText 가 보이면 ready로 간주
    if d(packageName=GS_PKG, description="검색").exists(timeout=0):
        return True
    if d(packageName=GS_PKG, description="홈").exists(timeout=0):
        return True
    if d(packageName=GS_PKG, className="android.widget.EditText").exists(timeout=0):
        return True
    return False


def _dump_gs(d: u2.Device, *, max_depth: int = 45):
    try:
        xml = d.dump_hierarchy(compressed=True, max_depth=max_depth)
        return ET.fromstring(xml)
    except Exception:
        return None


def _click_bottom_nav(d: u2.Device, label: str) -> bool:
    """하단 탭바(홈/검색 등)만 클릭 — 홈 본문의 GS25 배달 버튼과 구분."""
    _, h = d.window_size()
    y_min = int(h * 0.87)
    root = _dump_gs(d, max_depth=40)
    if root is not None:
        for n in root.iter("node"):
            if n.get("package") != GS_PKG:
                continue
            if n.get("clickable") != "true":
                continue
            desc = (n.get("content-desc") or "").strip()
            if desc != label:
                continue
            rect = _parse_bounds(n.get("bounds", ""))
            if rect and rect[1] >= y_min:
                cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
                ds.log(f"[*] 하단 탭 클릭: '{label}' @ ({cx},{cy})")
                d.click(cx, cy)
                return True
    # 폴백 좌표 (1080x2520 기준 5탭 중 2번째=검색)
    fallback = {"홈": (108, 2319), "검색": (324, 2319)}
    if label in fallback:
        x, y = fallback[label]
        ds.log(f"[*] 하단 탭 클릭(폴백): '{label}' @ ({x},{y})")
        d.click(x, y)
        return True
    return False


def _click_search_tab(d: u2.Device) -> bool:
    return _click_bottom_nav(d, "검색")


def _click_search_field(d: u2.Device) -> bool:
    # 검색 결과/검색 화면 상단의 EditText 우선
    el = d(packageName=GS_PKG, className="android.widget.EditText")
    if el.exists(timeout=0):
        el.click()
        return True
    # 폴백: 화면 상단 중앙 탭
    d.click(540, 180)
    return True


def _read_search_field_text(d: u2.Device) -> str:
    el = d(packageName=GS_PKG, className="android.widget.EditText")
    if not el.exists(timeout=0):
        return ""
    try:
        return (el.get_text() or "").strip()
    except Exception:
        try:
            return (el.info.get("text") or "").strip()
        except Exception:
            return ""


def _type_keyword(d: u2.Device, keyword: str) -> bool:
    """검색창에 키워드 입력 후 EditText 값이 일치하는지 검증 (복붙 없이)."""
    _click_search_field(d)
    time.sleep(0.15)
    for attempt in range(3):
        _clear_text_best_effort(d)
        time.sleep(0.08)
        # 혹시 남은 글자 있으면 추가 삭제
        cur = _read_search_field_text(d)
        if cur:
            for _ in range(len(cur) + 5):
                d.press("del")
            time.sleep(0.05)
        for ch in keyword:
            d.send_keys(ch)
            time.sleep(GS_TYPE_CHAR_DELAY)
        time.sleep(GS_POST_TYPE_DELAY)
        typed = _read_search_field_text(d)
        ds.log(f"    ... 입력 검증 attempt={attempt + 1} got='{typed}'")
        if typed == keyword or typed.replace(" ", "") == keyword.replace(" ", ""):
            return True
        # 일부 IME는 조합 중 마지막 글자가 늦게 반영
        if keyword.startswith(typed) and len(typed) >= max(1, len(keyword) - 1):
            time.sleep(0.35)
            typed = _read_search_field_text(d)
            if typed == keyword:
                return True
    ds.log(f"[!] 검색어 입력 실패 (기대='{keyword}', 실제='{_read_search_field_text(d)}')")
    return False


def search_keyword(d: u2.Device, keyword: str) -> bool:
    ds.log(f"[*] GS 검색: '{keyword}'")
    t0 = time.time()
    deadline = t0 + 18.0
    throttle = ds._Throttle()
    while time.time() < deadline:
        throttle.dismiss(d, "GS검색전")
        dismiss_gs_popups(d, "GS검색전")
        if _recover_from_login(d):
            continue
        _click_search_tab(d)
        if d(packageName=GS_PKG, className="android.widget.EditText").exists(timeout=0):
            break
        time.sleep(0.25)

    if not d(packageName=GS_PKG, className="android.widget.EditText").exists(timeout=0):
        ds.log("[!] 검색창이 뜨지 않아 검색을 진행하지 못했습니다.")
        return False

    if not _type_keyword(d, keyword):
        return False

    d.press("enter")
    # 결과(키워드 포함 상품)가 뜰 때까지 대기
    wait_deadline = time.time() + 12.0
    while time.time() < wait_deadline:
        dismiss_gs_popups(d, "GS검색후")
        if _recover_from_login(d):
            return False
        root = _dump_gs(d, max_depth=45)
        if root is not None:
            for n in root.iter("node"):
                if n.get("package") != GS_PKG:
                    continue
                lab = ((n.get("content-desc") or "") + " " + (n.get("text") or "")).strip()
                if keyword in lab and ("원" in lab or "주문당" in lab or "품절" in lab):
                    ds.log("[*] GS 검색 실행 완료 (키워드 상품 확인)")
                    return True
        time.sleep(0.45)

    ds.log("[!] 검색 결과에 키워드 상품이 나타나지 않았습니다.")
    return False


def _is_product_card(node) -> bool:
    label = ((node.get("content-desc") or "") + " " + (node.get("text") or "")).strip()
    if len(label) < 2:
        return False
    if any(x in label for x in ("더보기", "장바구니", "탭", "번째", "카카오", "네이버", "검색")):
        return False
    return "원" in label or "품절" in label or "주문당" in label


def _find_delivery_section_product(root, *, w: int, h: int, keyword: str = ""):
    """검색결과 '전체' 탭에서 GS25배달 구역(픽업 아래) 첫 상품.

    keyword가 있으면 상품명에 반드시 포함되어야 한다.
    """
    products = []
    delivery_y = None
    stock_y = None
    pickup_y = None

    for n in root.iter("node"):
        if n.get("package") != GS_PKG:
            continue
        desc = (n.get("content-desc") or "").replace("\n", " ").strip()
        # 필터 탭("…탭 N개 중…") / 홈 버튼 제외 — 본문 섹션 헤더만
        if "탭" in desc or "번째" in desc:
            pass
        else:
            rect = _parse_bounds(n.get("bounds", ""))
            if rect and int(h * 0.18) <= rect[1] <= int(h * 0.85):
                if "GS25 배달" in desc:
                    delivery_y = max(delivery_y or 0, rect[3])
                if "GS25 픽업" in desc:
                    pickup_y = max(pickup_y or 0, rect[3])
                if "매장 재고찾기" in desc:
                    stock_y = rect[1]
        if n.get("clickable") != "true":
            continue
        if not _is_product_card(n):
            continue
        rect = _parse_bounds(n.get("bounds", ""))
        if not rect:
            continue
        x1, y1, x2, y2 = rect
        if (x2 - x1) < int(w * 0.18) or (y2 - y1) < int(h * 0.05):
            continue
        label = ((n.get("content-desc") or "") + " " + (n.get("text") or "")).strip()
        if keyword and keyword not in label:
            continue
        products.append((y1, y2, n.get("bounds", ""), label[:100]))

    if not products:
        return None

    products.sort(key=lambda x: x[0])

    # 배달 구역: 픽업 아래 ~ 재고찾기 위. 헤더 없으면 주문당(배달 표시) 우선
    y_lo = (delivery_y + 40) if delivery_y else ((pickup_y + 40) if pickup_y else int(h * 0.45))
    y_hi = (stock_y - 20) if stock_y else int(h * 0.82)

    # 1) 배달 대역 + 주문당
    for y1, y2, bounds, label in products:
        cy = (y1 + y2) // 2
        if y_lo <= cy <= y_hi and "주문당" in label:
            return bounds, label
    # 2) 배달 대역
    for y1, y2, bounds, label in products:
        cy = (y1 + y2) // 2
        if y_lo <= cy <= y_hi:
            return bounds, label
    # 3) 주문당 표기 상품(구역 헤더 누락 대비)
    for y1, y2, bounds, label in products:
        if "주문당" in label:
            return bounds, label
    # 4) 키워드 매칭 상품 중 가장 아래쪽(보통 배달 슬롯)
    if keyword:
        return products[-1][2], products[-1][3]
    return None


def click_gs25_delivery_product(d: u2.Device, *, keyword: str = "",
                                timeout: float = 18.0) -> bool:
    """검색 결과에서 GS25배달 구역 상품 클릭 (키워드 일치 필수)."""
    ds.log(f"[*] GS25배달 영역 상품 클릭 (keyword='{keyword}')")
    t0 = time.time()
    deadline = t0 + timeout
    w, h = d.window_size()
    throttle = ds._Throttle()
    tries = 0

    while time.time() < deadline:
        throttle.dismiss(d, "GS상품탐색")
        dismiss_gs_popups(d, "GS상품탐색")
        if _recover_from_login(d):
            ds.log("[!] 로그인 화면으로 빠짐 — 검색부터 다시 필요")
            return False

        root = _dump_gs(d, max_depth=50)
        if root is None:
            time.sleep(0.4)
            continue

        found = _find_delivery_section_product(root, w=w, h=h, keyword=keyword)
        if found:
            bounds, label = found
            if keyword and keyword not in label:
                ds.log(f"[!] 키워드 불일치 후보 스킵: '{label}'")
            else:
                cx, cy = _bounds_center(bounds)
                ds.log(f"[+] GS25배달 상품 클릭: '{label}' @ {bounds}")
                d.click(cx, cy)
                time.sleep(0.7)
                dismiss_gs_popups(d, "GS상품클릭후")
                return True

        tries += 1
        if tries <= 4:
            d.swipe(0.5, 0.72, 0.5, 0.40, duration=0.25)
            time.sleep(0.45)
        else:
            time.sleep(0.4)

    ds.log("[!] GS25배달 상품을 찾지 못했습니다.")
    return False


def run_once(d, args) -> tuple:
    """Returns: (성공 여부, 앱 진입 여부)."""
    ds.log("[*] 로테이션 시작 전 데이터·IP 확인")
    if not ds.ensure_network(args.serial, timeout=args.recover_secs):
        ds.log("[!] 연결 미확인 → 우리동네GS 진입 생략 (이번 로테이션 스킵)")
        return False, False

    open_gs(d, args.serial)
    if not search_keyword(d, args.keyword):
        return False, True
    ok = click_gs25_delivery_product(
        d, keyword=args.keyword, timeout=args.click_timeout
    )
    return ok, True


def main():
    ap = argparse.ArgumentParser(description="[우리동네GS 클릭형] 검색→GS25배달 상품 클릭")
    ap.add_argument("keyword", nargs="?", default=DEFAULT_KEYWORD,
                    help=f"검색 키워드 (기본: {DEFAULT_KEYWORD})")
    ap.add_argument("--serial", default=ds.DEFAULT_SERIAL,
                    help=f"adb 시리얼 (기본: {ds.DEFAULT_SERIAL})")
    ap.add_argument("--click-timeout", type=float, default=18.0,
                    help="상품 클릭 후보 탐색 대기(초)")
    ap.add_argument("--rotations", type=int, default=0,
                    help="반복 횟수 (0=무한)")
    ap.add_argument("--no-data-toggle", action="store_true",
                    help="모바일 데이터 OFF/ON 비활성화")
    ap.add_argument("--off-secs", type=float, default=4.0,
                    help="데이터 OFF 유지 시간(초)")
    ap.add_argument("--recover-secs", type=float, default=25.0,
                    help="데이터 ON 후 연결 복구 대기(초)")
    ap.add_argument("--no-clear", action="store_true",
                    help="pm clear 비활성화")
    args = ap.parse_args()

    data_toggle = not args.no_data_toggle
    do_clear = not args.no_clear

    d = ds.connect(args.serial)
    ds.setup_popup_watchers(d)

    results = []
    ip_changes = []
    infinite = args.rotations <= 0
    total = "∞" if infinite else str(args.rotations)
    i = 0

    try:
        while infinite or i < args.rotations:
            i += 1
            ds.begin_cycle_timer()
            cycle_start = time.time()
            ds.log(f"\n===== 로테이션 {i}/{total} 시작 (우리동네GS) =====")
            try:
                ok, opened = run_once(d, args)
            except Exception as e:
                ds.log(f"[!] 로테이션 {i} 오류: {e}")
                ok, opened = False, False

            if opened:
                try:
                    d.screenshot(os.path.join(ds._SCRIPT_DIR, "gs_click_result.png"))
                except Exception:
                    pass

            ds.log(f"[*] 로테이션 {i} (작업 성공={ok}, 진입={opened})")
            results.append(ok)

            ds.log(f"[*] 로테이션 {i} 마무리")
            if opened:
                try:
                    d.app_stop(GS_PKG)
                except Exception:
                    pass
            if data_toggle:
                changed = ds.toggle_mobile_data(
                    args.serial, off_secs=args.off_secs, recover_timeout=args.recover_secs
                )
                ip_changes.append(changed)
            if do_clear:
                # 매 회전마다 설치 직후 수준으로 초기화
                ds.log("[*] 우리동네GS 데이터 삭제(pm clear)")
                try:
                    d.app_stop(GS_PKG)
                except Exception:
                    pass
                ds._adb(args.serial, "shell", "pm", "clear", GS_PKG)
                time.sleep(0.9)

            cycle_elapsed = time.time() - cycle_start
            ipc = f", IP변경 {sum(ip_changes)}/{len(ip_changes)}" if ip_changes else ""
            ds.log(f"[누적] 작업 성공 {sum(results)}/{len(results)}{ipc}")
            ds.log(f"===== 로테이션 {i}/{total} 완료 — 소요 {cycle_elapsed:.1f}s =====")
            ds.end_cycle_timer()

    except KeyboardInterrupt:
        ds.log("\n[*] 사용자 중단(Ctrl+C)")
    finally:
        ds.log("\n========== 최종 요약 (우리동네GS) ==========")
        ds.log(f"[+] 작업 성공: {sum(results)}/{len(results)} 로테이션")
        if ip_changes:
            ds.log(f"[+] IP 변경 성공: {sum(ip_changes)}/{len(ip_changes)} 회")
        try:
            d.watcher.stop()
        except Exception:
            pass

    # 워커가 failed 로 집계하도록: 전부 성공이 아니면 non-zero
    if not results or not all(results):
        sys.exit(1)


if __name__ == "__main__":
    main()

