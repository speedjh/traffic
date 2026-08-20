# -*- coding: utf-8 -*-
"""
[다음 클릭형] 다음앱 검색 → 장소(플레이스) 클릭 → 카카오맵 전환 후 종료.

다음앱에서 장소를 눌러 카카오맵이 열리면 끝.
카카오맵에서 업체 재클릭·스크롤·체류 없음.

사용법:
    python daum_click_device.py --place-url "https://place.map.kakao.com/22122997"
    ./run_daum_click.sh --rotations 0
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


def run_once(d, args, place_id: str, place_name: str) -> tuple:
    """Returns: (성공 여부, 앱 진입 여부)."""
    ds.log("[*] 로테이션 시작 전 데이터·IP 확인")
    if not ds.ensure_network(args.serial, timeout=args.recover_secs):
        ds.log("[!] 연결 미확인 → 다음앱 진입 생략 (이번 로테이션 스킵)")
        return False, False

    ds.open_daum(d, args.serial)
    ds.search_keyword(d, args.keyword, timeout=args.search_timeout)
    if not ds.click_place_in_section(d, place_id, place_name,
                                      timeout=args.place_timeout):
        return False, True
    if not ds.wait_for_kakaomap_open(d, timeout=args.map_timeout):
        return False, True
    ds.log("[+] 다음 클릭형 완료 (카카오맵 전환만, 추가 액션 없음)")
    return True, True


def main():
    ap = argparse.ArgumentParser(
        description="[다음 클릭형] 다음앱 검색 → 장소 클릭 → 카카오맵 전환 후 종료",
    )
    ap.add_argument("keyword", nargs="?", default=ds.DEFAULT_KEYWORD,
                    help=f"검색 키워드 (기본: {ds.DEFAULT_KEYWORD})")
    ap.add_argument("--place-url", default=ds.DEFAULT_PLACE_URL,
                    help="카카오맵 장소 URL")
    ap.add_argument("--place-name", default="",
                    help="업체명 (URL 조회 생략)")
    ap.add_argument("--serial", default=ds.DEFAULT_SERIAL,
                    help=f"adb 시리얼 (기본: {ds.DEFAULT_SERIAL})")
    ap.add_argument("--search-timeout", type=float, default=ds.PLACE_SECTION_TIMEOUT,
                    help="다음앱 검색 결과 로딩 대기(초)")
    ap.add_argument("--place-timeout", type=float, default=ds.PLACE_SECTION_TIMEOUT,
                    help="장소 섹션 탐색 대기(초)")
    ap.add_argument("--map-timeout", type=float, default=ds.PLACE_SECTION_TIMEOUT,
                    help="카카오맵 전환 대기(초)")
    ap.add_argument("--partial", action="store_true", default=True,
                    help="업체명 부분일치 (기본 켜짐)")
    ap.add_argument("--no-partial", action="store_false", dest="partial",
                    help="업체명 완전일치")
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

    place_id = ds.parse_place_id(args.place_url)
    if args.place_name.strip():
        place_name = args.place_name.strip()
        ds.log(f"[*] 업체명 (--place-name): {place_name}")
    else:
        try:
            place_name = ds.resolve_place_name(args.place_url)
        except Exception as e:
            fallback = ds.KNOWN_PLACE_NAMES.get(place_id, "")
            if fallback:
                place_name = fallback
                ds.log(f"[!] 장소 URL 조회 실패: {e}")
                ds.log(f"[*] 알려진 place ID 폴백: '{place_name}'")
            else:
                ds.log(f"[!] 장소 URL 조회 실패: {e}")
                sys.exit(1)

    ds.log(f"[*] 장소 URL: {args.place_url}")
    ds.log(f"[*] place ID: {place_id or '(없음)'} / 업체명: {place_name}")
    ds.log("[*] 모드: 다음 클릭형 (카카오맵 전환 후 종료)")

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
            ds.log(f"\n===== 로테이션 {i}/{total} 시작 (다음 클릭형) =====")
            try:
                ok, opened = run_once(d, args, place_id, place_name)
            except Exception as e:
                ds.log(f"[!] 로테이션 {i} 오류: {e}")
                ok, opened = False, False

            if opened:
                try:
                    d.screenshot(os.path.join(ds._SCRIPT_DIR, "daum_click_result.png"))
                except Exception:
                    pass

            ds.log(f"[*] 로테이션 {i} (작업 성공={ok}, 진입={opened})")
            results.append(ok)

            ds.log(f"[*] 로테이션 {i} 마무리")
            if opened:
                d.app_stop(ds.MAP_PKG)
                d.app_stop(ds.DAUM_PKG)
            if data_toggle:
                changed = ds.toggle_mobile_data(
                    args.serial, off_secs=args.off_secs,
                    recover_timeout=args.recover_secs,
                )
                ip_changes.append(changed)
            if do_clear and opened:
                ds.clear_app_data(args.serial)
            elif do_clear:
                ds.log("[*] 앱 미진입 — pm clear 생략")

            cycle_elapsed = time.time() - cycle_start
            ipc = f", IP변경 {sum(ip_changes)}/{len(ip_changes)}" if ip_changes else ""
            ds.log(f"[누적] 작업 성공 {sum(results)}/{len(results)}{ipc}")
            ds.log(f"===== 로테이션 {i}/{total} 완료 — 소요 {cycle_elapsed:.1f}s =====")
            ds.end_cycle_timer()
    except KeyboardInterrupt:
        ds.log("\n[*] 사용자 중단(Ctrl+C)")
    finally:
        ds.log("\n========== 최종 요약 (다음 클릭형) ==========")
        ds.log(f"[+] 작업 성공: {sum(results)}/{len(results)} 로테이션")
        if ip_changes:
            ds.log(f"[+] IP 변경 성공: {sum(ip_changes)}/{len(ip_changes)} 회")
        try:
            d.watcher.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
