# -*- coding: utf-8 -*-
"""
[다음자완] 다음앱 검색 → 결과(블로그/카페/웹문서 등) 아무거나 랜덤 클릭 = 1사이클.

- 기존 다음 트래픽과 동일한 네트워크 확인 / 앱 실행 / 검색어 타이핑을 사용
- 장소 섹션/카카오맵은 사용하지 않는다

사용법:
    ./run_daum_jawan.sh --rotations 0
    ./run_daum_jawan.sh "영월 닭강정 가나닭강정" --rotations 5
"""
import argparse
import os
import sys
import time

import daum_search_device as ds

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def run_once(d, args) -> tuple:
    """Returns: (성공 여부, 앱 진입 여부)."""
    ds.log("[*] 로테이션 시작 전 데이터·IP 확인")
    if not ds.ensure_network(args.serial, timeout=args.recover_secs):
        ds.log("[!] 연결 미확인 → 다음앱 진입 생략 (이번 로테이션 스킵)")
        return False, False

    ds.open_daum(d, args.serial)
    try:
        ds.search_keyword(d, args.keyword, timeout=args.search_timeout)
    except RuntimeError as e:
        # 검색창 미발견 — 앱 재실행 1회 재시도
        ds.log(f"[!] 검색 실패({e}) → 다음앱 재실행 후 1회 재시도")
        ds.open_daum(d, args.serial)
        ds.search_keyword(d, args.keyword, timeout=args.search_timeout)
    ok = ds.click_any_search_result(
        d, timeout=args.click_timeout, max_scroll=args.max_scroll,
        initial_scroll=args.initial_scroll,
    )
    return ok, True


def main():
    ap = argparse.ArgumentParser(description="[다음자완] 검색 후 결과 아무거나 랜덤 클릭")
    ap.add_argument("keyword", nargs="?", default=ds.DEFAULT_KEYWORD,
                    help=f"검색 키워드 (기본: {ds.DEFAULT_KEYWORD})")
    ap.add_argument("--serial", default=ds.DEFAULT_SERIAL,
                    help=f"adb 시리얼 (기본: {ds.DEFAULT_SERIAL})")
    ap.add_argument("--search-timeout", type=float, default=ds.PLACE_SECTION_TIMEOUT,
                    help="검색 실행 후 대기(초)")
    ap.add_argument("--click-timeout", type=float, default=18.0,
                    help="랜덤 클릭 후보 탐색 대기(초)")
    ap.add_argument("--max-scroll", type=int, default=5,
                    help="후보 없을 때 스크롤 횟수")
    ap.add_argument("--initial-scroll", type=int, default=2,
                    help="클릭 전 초기 스크롤 횟수 (파워링크 아래로 넘기기)")
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
            ds.log(f"\n===== 로테이션 {i}/{total} 시작 (다음자완) =====")
            try:
                ok, opened = run_once(d, args)
            except Exception as e:
                ds.log(f"[!] 로테이션 {i} 오류: {e}")
                ok, opened = False, False

            if opened:
                try:
                    d.screenshot(os.path.join(ds._SCRIPT_DIR, "daum_jawan_result.png"))
                except Exception:
                    pass

            ds.log(f"[*] 로테이션 {i} (작업 성공={ok}, 진입={opened})")
            results.append(ok)

            ds.log(f"[*] 로테이션 {i} 마무리")
            if opened:
                d.app_stop(ds.DAUM_PKG)
            if data_toggle:
                changed = ds.toggle_mobile_data(
                    args.serial, off_secs=args.off_secs,
                    recover_timeout=args.recover_secs,
                )
                ip_changes.append(changed)
            if do_clear and opened:
                ds.log("[*] 다음앱 데이터 삭제(pm clear)")
                ds._adb(args.serial, "shell", "pm", "clear", ds.DAUM_PKG)
                time.sleep(2.5)
            elif do_clear:
                ds.log("[*] 앱 미진입 — pm clear 생략")

            cycle_elapsed = time.time() - cycle_start
            ipc = f", IP변경 {sum(ip_changes)}/{len(ip_changes)}" if ip_changes else ""
            ds.log(f"[누적] 작업 성공 {sum(results)}/{len(results)}{ipc}")
            ds.log(f"===== 로테이션 {i}/{total} 완료 — 소요 {cycle_elapsed:.1f}s =====")
            ds.end_cycle_timer()
    except KeyboardInterrupt:
        ds.log("\n[*] 사용자 중단(Ctrl+C)")
        raise SystemExit(130)
    finally:
        ds.log("\n========== 최종 요약 (다음자완) ==========")
        ds.log(f"[+] 작업 성공: {sum(results)}/{len(results)} 로테이션")
        if ip_changes:
            ds.log(f"[+] IP 변경 성공: {sum(ip_changes)}/{len(ip_changes)} 회")
        try:
            d.watcher.stop()
        except Exception:
            pass

    if not results or not any(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

