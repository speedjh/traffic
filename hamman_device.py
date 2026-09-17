# -*- coding: utf-8 -*-
"""
[함많찾을] Chrome → 네이버 검색 트래픽 (실물 기기 ADB).

1사이클:
    Chrome 실행 → 네이버(m.naver.com) 이동
    → 메인 키워드 붙여넣기 → 유기 글(블로그/카페/웹)만 클릭 → 체류
    → 2차 키워드 붙여넣기 → 유기 글 클릭 → 체류
    → Chrome pm clear → 모바일 데이터 OFF/ON(IP 변경, 가나닭강정과 동일 경로)

사용법:
    ./run_hamman.sh
    ./run_hamman.sh "광주예물" --keyword2 "광주예물 링플레이트" --serial R3CN60HA2FV
    ./run_hamman.sh ... --dwell-min 15 --dwell-max 20 --rotations 1

요구사항:
    pip install uiautomator2
    adb devices 에 기기 연결
"""
from __future__ import annotations

import argparse
import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple
from urllib.parse import quote, unquote

import daum_search_device as ds

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import uiautomator2 as u2  # noqa: E402

CHROME_PKG = "com.android.chrome"
CHROME_MAIN = f"{CHROME_PKG}/com.google.android.apps.chrome.Main"
URL_BAR = f"{CHROME_PKG}:id/url_bar"
SEARCH_BOX = f"{CHROME_PKG}:id/search_box_text"

DEFAULT_SERIAL = ds.DEFAULT_SERIAL
DEFAULT_KEYWORD = "광주예물"
DEFAULT_KEYWORD2 = "광주예물 링플레이트"
DEFAULT_DWELL_MIN = 15.0
DEFAULT_DWELL_MAX = 20.0

# 함많찾을 전용 — Chrome→네이버·SERP 사냥은 빠르게 (daum 기본 0.5/1.5 대비 대폭 단축)
SEARCH_POST_TYPE_DELAY = 0.12   # was ds 0.5
SEARCH_POST_ENTER_DELAY = 0.35  # was ds 1.5
NAVER_LAND_SETTLE = 0.20        # 네이버 URL 확인 후 안정화 (was 1.2)
OMNIBOX_PRE_PASTE = 0.12        # 옴니박스 탭 후 (was 0.5)
SERP_URL_SETTLE = 1.60          # 검색결과 URL 확인 후 (유기결과 lazy load · 빈 url_bar 오판 방지)
# SERP 사냥 — 짧은 스와이프(오버슈트·SERP 이탈 억제)
HUNT_SWIPE_DUR = (0.16, 0.28)
HUNT_SWIPE_GAP = (0.18, 0.40)
# 수유소고기 등 플레이스 구좌 테스트용 — 로컬팩을 빠르게·멀리 통과
FAST_PLACE_HUNT_SWIPE_DUR = (0.08, 0.14)
FAST_PLACE_HUNT_SWIPE_GAP = (0.05, 0.12)
DWELL_SWIPE_DUR = (0.18, 0.35)  # 체류용 (완만)
DWELL_SWIPE_GAP = (0.35, 0.90)

# 캠페인 키워드/이름 매칭 → 플레이스 구좌 테스트 모드 (스크롤 가속·리뷰클릭 금지)
SUYU_PLACE_TUNE_KEYS = ("수유소고기", "수유고기집")

NAVER_HOME = "m.naver.com"
NAVER_HOST_HINTS = ("naver.com", "search.naver")

CHROME_FRE_TEXTS = (
    "로그아웃 상태 유지",
    "로그인 없이 사용",
    "계정 없이 사용",          # Chrome 신버전 FRE ("나만의 Chrome 만들기")
    "계정 없이 계속",
    "Without an account",
    "Use without an account",
    "Continue without an account",
    "Accept & continue",
    "동의하고 계속",
    "동의 및 계속",
)

# Chrome 온보딩 버튼 resource-id (문구가 바뀌어도 잡히도록)
CHROME_FRE_IDS = (
    "signin_fre_dismiss_button",   # 계정 없이 사용
    "more_button",                 # 광고 개인정보 안내 — 자세히
    "ack_button",                  # 광고 개인정보 안내 — 확인
    "negative_button",
)

AD_HINTS = (
    "광고", "파워링크", "파워컨텐츠", "파워 컨텐츠", "파워 링크",
    "[Ad]", "[AD]", "[광고]", "Powerlink", "PowerLink", "Power Content",
    "AD ", " Ad", "스폰서", "Sponsored", "이 광고가", "관련광고",
    "쇼핑광고", "쇼핑 광고", "브랜드검색",
)
AD_SECTION_RID_EXACT = frozenset({
    "power_link_body", "power_link", "HOME_AD",
})
AD_SECTION_RID_SUBSTR = (
    "mobilepowerlink", "powerlink", "power_link", "powercontent",
    "power_content", "specialda", "brandsearch", "shoppingad",
)

CLIP_SECTION_RID_SUBSTR = (
    "shorts", "shortform", "naverclip", "nvclip", "clip_card",
    "clipcard", "video_feed", "reel",
)
CLIP_LABEL_HINTS = (
    "클립", "숏츠", "숏폼", "shorts", "short-form", "shortform",
    "네이버TV", "네이버 tv", "재생 중", "시청하기",
)
CLIP_URL_HINTS = (
    "/shorts", "m.naver.com/shorts", "naver.me/clip", "clip.naver",
    "tv.naver.com", "m.tv.naver", "video.naver", "shortform",
    "where=m_clip", "where=clip", "ssc=tab.m.clip", "ssc=tab.clip",
)

# 글(블로그/카페/뉴스/뷰/웹문서) — 클릭 최우선
# 단독 "후기"/"뷰"는 플레이스('…후기 확인하기','리뷰')에도 걸리므로 별도 처리
ARTICLE_LABEL_HINTS = (
    "블로그", "카페", "웹문서", "인플루언서", "지식iN", "뉴스",
    "blog.naver", "cafe.naver", "news.naver", "post.naver",
    "네이버블로그", "네이버 블로그", "카페글", "리뷰글",
)
# 짧아 부분일치 위험한 힌트 — 토큰 경계(또는 탭 라벨)만
ARTICLE_LABEL_EXACT = frozenset({"뷰", "후기"})
ARTICLE_LABEL_BOUNDED = (
    # " 뷰 " / 시작·끝 / 구분기호 옆 — "리뷰"에 포함된 '뷰'는 제외
    re.compile(r"(^|[\s·|/\[\(])뷰([\s·|/\]\)]|$)"),
    re.compile(r"(^|[\s·|/\[\(])후기([\s·|/\]\)]|$)"),
)
ARTICLE_URL_HINTS = (
    "blog.naver", "cafe.naver", "news.naver", "post.naver",
    "m.blog.", "m.cafe.", "in.naver.com",
)

# 지도/플레이스 로컬팩 — 클릭·랜딩 하드 제외
MAP_PLACE_LABEL_HINTS = (
    "지도보기", "지도 보기", "다른 지역", "상세주소", "플레이스",
    "길찾기", "거리순", "관련도순", "쿠폰 있음", "영업 종료", "영업 시작",
    "방문자 리뷰", "전화하기", "예약하기", "map.naver", "place.naver",
    "지도", "로드뷰", "찾아가는길", "영업중", "영업 중", "위치보기",
    "매장정보", "영업시간", "저장한 장소", "위치 정보", "내위치", "내 위치",
    "톡톡", "네이버 예약", "쿠폰 결혼예물", "점 쿠폰", "쿠폰 외",
    # 로컬팩/플레이스·예약 카드 (글 오인 방지)
    "진료시간", "위치 ·", "위치·", "후기 확인하기", "진료를 접수",
    "굿닥에서", "네이버 예약", "예약 가능", "당일접수", "당일 접수",
    "곧 진료", "진료 시작", "진료 전", "오늘 휴무",
    "알림받기", "파우치 증정", "쿠폰 증정", "방문톡", "예약톡",
    "온&오프라인", "온오프라인", "저렴한가격", "저렴한 가격",
    # 맛집/술집 로컬팩 UI (수유소고기·민락/덕계 등 map/place 오클릭)
    "영업 전", "영업전", "별점", "휠체어 출입", "주간 인기 많은 메뉴",
    "connect+", "connect +", "리뷰이벤트", "리뷰 이벤트", "리뷰 쓰고",
    "혜택 리뷰", "포인트 적립", "인기 많은 메뉴",
)
# 플레이스 카드 리뷰·평점·메뉴 라인 — 글 오인 방지
_PLACE_RATING_RE = re.compile(
    r"(영업\s*전|별점\s*[\d.]+|리뷰\s*[\d,]+|리뷰\d|휠체어|connect\+)",
    re.I,
)
_PLACE_MENU_RE = re.compile(
    r"(주간\s*인기\s*많은\s*메뉴|인기\s*많은\s*메뉴|세트메뉴|크림생맥주|"
    r"리뷰이벤트|리뷰\s*이벤트)"
)
MAP_PLACE_RID_SUBSTR = (
    "local_pack", "localpack", "place_map", "placemap", "nmap",
    "map_section", "place_home", "placehome", "lpt_", "placeinfo",
    "place_info", "mapcanvas", "map_canvas", "staticmap", "api.map",
)
MAP_PLACE_URL_HINTS = (
    "map.naver", "place.naver", "m.place.naver", "nmap.naver",
    "pcmap.naver", "/place/", "where=m_place", "ssc=tab.m.place",
    "ssc=tab.place",
)
# 플레이스 매장명 카드 (예: "도쿄앤펄 광주점") — 글이 아님
_PLACE_STORE_RE = re.compile(
    r"(점\s*$|점\s+|지점|플래그십|매장|웨딩홀)"
)

UI_TAB_LABELS = {
    "블로그", "카페", "클립", "이미지", "지식iN", "동영상", "AI new", "쇼핑",
    "뉴스", "인플루언서", "웹문서", "사용후기", "관련검색어", "AI", "전체",
    "지도", "플레이스", "뷰",
    "연관검색어", "함께 많이 찾는", "함께 찾아본",
}
UI_BTN_LABELS = {
    "검색", "음성검색", "입력 내용 삭제", "예약하기", "홈페이지", "플레이스",
    "결혼정보", "고객후기", "Naver", "이전페이지", "이전 페이지", "렌즈",
    "AI 검색", "공유", "저장", "지도", "전화", "길찾기", "플레이스 이미지",
    "다른 지역 검색하기", "지도보기", "상세주소 열기", "쿠폰 있음",
    "배송지 설정", "새 창으로 열기", "라운지글로 이동",
}
JUNK_LABEL_SUBSTR = (
    "쿠폰 있음", "다른 지역", "상세주소", "지도보기", "영업 종료",
    "영업 시작", "© NAVER", "NAVER Corp", "거리순", "관련도순",
    "더보기", "접기", "펼치기", "공유하기", "저장하기", "예약하기",
    "길찾기", "전화하기", "리뷰 쓰기", "방문자 리뷰", "지도",
    "홈페이지", "플레이스 이미지",
    # 쇼핑/카페 UI 오클릭 (광주커플링·경주여행코스 fail spike)
    "배송지 설정", "새 창으로 열기", "라운지글로 이동", "라운지글",
    "축제정보 등록", "등록/수정 요청", "배송지",
    # 축제/호텔 UI (경주축제·경주여행코스)
    "행사종료", "행사 종료", "성급", "수영장이", "침대가",
)

# 연관/추천 검색어 구좌 — 클릭 시 query 가 바뀌어 캠페인 키워드가 이탈함
RELATED_SEARCH_SECTION_HINTS = (
    "함께 많이 찾는",
    "함께 찾아본",
    "연관검색어",
    "관련검색어",
    "관련 검색어",
    "관련 검색",
    "이런 검색어는 어때요",
    "추천 검색어",
    "Related searches",
    "People also search",
    "People Also Search",
)
RELATED_SEARCH_CHIP_HINTS = (
    "요즘 인기",
    "인기 검색어",
    "급상승 검색어",
)
RELATED_SEARCH_RID_SUBSTR = (
    "related_srch", "relatedsrch", "related_kwd", "relatedkwd",
    "related_keyword", "relatedkeyword", "nx_related", "nxrelated",
    "query_guide", "queryguide", "recommend_kwd", "recommendkwd",
    "related_query", "relatedquery", "rqkw", "rrkw", "suggest_kwd",
    "suggestkwd", "keyword_list", "keywordlist", "alsosearch",
)


def log(msg: str = ""):
    ds.log(msg)


def _parse_bounds(bounds: str) -> Optional[Tuple[int, int, int, int]]:
    try:
        nums = list(map(int, re.findall(r"-?\d+", bounds or "")))
        if len(nums) >= 4:
            return nums[0], nums[1], nums[2], nums[3]
    except Exception:
        pass
    return None


def url_bar_text(d: u2.Device) -> str:
    try:
        if d(resourceId=URL_BAR).exists(timeout=0):
            return (d(resourceId=URL_BAR).get_text() or "").strip()
    except Exception:
        pass
    return ""


def dismiss_chrome_noise(d: u2.Device, rounds: int = 6) -> None:
    """Chrome FRE / 알림권한 / 공통 팝업 거부."""
    for _ in range(rounds):
        hit = False
        # 신버전 FRE 순서: '계정 없이 사용' → 광고 개인정보 안내 '자세히' → '확인'
        for rid in CHROME_FRE_IDS:
            if ds._safe_click(d, resourceId=f"{CHROME_PKG}:id/{rid}"):
                log(f"[*] Chrome 온보딩 버튼: {rid}")
                time.sleep(1.0)
                hit = True
                break
        if hit:
            continue
        for t in CHROME_FRE_TEXTS:
            if ds._safe_click(d, text=t) or ds._safe_click(d, textContains=t):
                log(f"[*] Chrome 온보딩: '{t}'")
                time.sleep(0.9)
                hit = True
                break
        if hit:
            continue
        n = ds.dismiss_popups_now(d, "chrome")
        if n:
            hit = True
        for t in ("허용 안함", "허용 안 함", "Don't allow", "Deny", "닫기", "No thanks"):
            if ds._safe_click(d, text=t):
                log(f"[*] 권한/팝업 거부: '{t}'")
                time.sleep(0.5)
                hit = True
                break
        if not hit:
            break


def human_swipe(d: u2.Device, *, direction: str = "up",
                mode: str = "dwell", fast_place: bool = False) -> None:
    """스와이프. mode=hunt: 짧은 거리(블라인드 오버슈트 억제). mode=dwell: 체류용.
    fast_place=True: 플레이스 로컬팩 통과용 — 더 멀리·더 빠르게."""
    x = 0.50 + random.uniform(-0.10, 0.10)
    hunt = mode == "hunt"
    if direction == "up":
        if hunt and fast_place:
            # 로컬팩을 쭉 내려가기 — 긴 플링 + 짧은 duration
            y1 = random.uniform(0.78, 0.88)
            y2 = random.uniform(0.12, 0.22)
        elif hunt:
            # 긴 플링(0.72→0.14)은 SERP 이탈·url_bar 공백 유발 → 절반 정도만
            y1 = random.uniform(0.68, 0.80)
            y2 = random.uniform(0.36, 0.48)
        else:
            y1 = random.uniform(0.66, 0.80)
            y2 = random.uniform(0.26, 0.42)
    else:
        if hunt:
            # 오버슈트 보정·위로 살짝
            y1 = random.uniform(0.30, 0.40)
            y2 = random.uniform(0.55, 0.68)
        else:
            y1 = random.uniform(0.28, 0.40)
            y2 = random.uniform(0.66, 0.80)
    if hunt and fast_place:
        lo, hi = FAST_PLACE_HUNT_SWIPE_DUR
        glo, ghi = FAST_PLACE_HUNT_SWIPE_GAP
    elif hunt:
        lo, hi = HUNT_SWIPE_DUR
        glo, ghi = HUNT_SWIPE_GAP
    else:
        lo, hi = DWELL_SWIPE_DUR
        glo, ghi = DWELL_SWIPE_GAP
    dur = random.uniform(lo, hi)
    dx = random.uniform(-0.05, 0.05) if hunt else random.uniform(-0.04, 0.04)
    d.swipe(x, y1, max(0.08, min(0.92, x + dx)), y2, duration=dur)
    time.sleep(random.uniform(glo, ghi))


def _edit_text_matches(cur: str, want: str) -> bool:
    """붙여넣기 성공 판정 — 부분문자열 false-positive 방지."""
    a = (cur or "").strip().replace(" ", "")
    b = (want or "").strip().replace(" ", "")
    if not a or not b:
        return False
    return a == b or b in a


def paste_search_keyword(d: u2.Device, text: str) -> None:
    """검색창에 클립보드 붙여넣기 / set_text (글자별 입력 금지)."""
    try:
        d.clear_text()
    except Exception:
        pass
    time.sleep(0.12)

    try:
        d.set_clipboard(text)
        time.sleep(0.08)
        d.shell("input keyevent 279")
        time.sleep(0.25)
        try:
            et = d(className="android.widget.EditText")
            if et.exists(timeout=0.3):
                cur = (et.get_text() or "").strip()
                if _edit_text_matches(cur, text):
                    return
                # 실패: clear 후 set_text 로
                try:
                    d.clear_text()
                except Exception:
                    pass
        except Exception:
            pass
    except Exception as e:
        log(f"    ... clipboard paste 실패: {e}")

    try:
        et = d(className="android.widget.EditText")
        if et.exists(timeout=0.5):
            et.set_text(text)
            time.sleep(0.2)
            cur = (et.get_text() or "").strip()
            if _edit_text_matches(cur, text):
                return
    except Exception as e:
        log(f"    ... set_text 실패: {e}")

    d.send_keys(text, clear=True)
    time.sleep(0.2)


def open_chrome(d: u2.Device, serial: str) -> None:
    log("[*] Chrome 실행")
    try:
        from device_screen import ensure_screen_ready
        ensure_screen_ready(serial, log=log)
    except Exception as e:
        log(f"[!] 화면 준비 스킵: {e}")
    ds._adb(serial, "shell", "am", "force-stop", CHROME_PKG)
    time.sleep(0.4)
    ds._adb(
        serial, "shell", "am", "start",
        "-a", "android.intent.action.MAIN",
        "-c", "android.intent.category.LAUNCHER",
        "-n", CHROME_MAIN,
    )
    deadline = time.time() + 35.0
    while time.time() < deadline:
        dismiss_chrome_noise(d, rounds=2)
        if (
            ds._safe_exists(d, resourceId=SEARCH_BOX)
            or ds._safe_exists(d, resourceId=URL_BAR)
            or ds._safe_exists(d, textContains="Google 검색")
            or ds._safe_exists(d, textContains="검색 또는 URL")
        ):
            dismiss_chrome_noise(d, rounds=2)
            log("[+] Chrome 홈/주소창 준비됨")
            return
        time.sleep(0.35)
    log("[!] Chrome 로딩 지연 — 계속 진행")


def navigate_omnibox(d: u2.Device, url: str, *, timeout: float = 25.0) -> bool:
    """Chrome 옴니박스로 URL 이동."""
    log(f"[*] 옴니박스 이동: {url[:90]}")
    dismiss_chrome_noise(d, rounds=2)
    opened = False
    for kwargs in (
        {"resourceId": URL_BAR},
        {"resourceId": SEARCH_BOX},
        {"textContains": "Google 검색"},
        {"textContains": "검색 또는 URL"},
    ):
        if ds._safe_click(d, **kwargs):
            opened = True
            break
    if not opened:
        raise RuntimeError("Chrome 주소/검색창을 찾지 못했습니다.")
    time.sleep(OMNIBOX_PRE_PASTE)
    paste_search_keyword(d, url)
    time.sleep(SEARCH_POST_TYPE_DELAY)
    d.press("enter")
    deadline = time.time() + timeout
    while time.time() < deadline:
        dismiss_chrome_noise(d, rounds=1)
        cur = url_bar_text(d).lower()
        if any(h in cur for h in NAVER_HOST_HINTS):
            log(f"[+] 네이버 로딩 확인 ({url_bar_text(d)[:60]})")
            time.sleep(NAVER_LAND_SETTLE)
            return True
        time.sleep(0.18)
    log(f"[!] 네이버 URL 대기 타임아웃 (현재: {url_bar_text(d)[:80]})")
    return False


def naver_search_url(keyword: str) -> str:
    return (
        "m.search.naver.com/search.naver?where=m&sm=mtp_hty.top&query="
        + quote(keyword.strip())
    )


def find_naver_search_edit(d: u2.Device) -> Optional[Tuple[int, int]]:
    """WebView 안 네이버 검색 EditText (Chrome url_bar 제외)."""
    try:
        root = ET.fromstring(d.dump_hierarchy())
    except Exception:
        return None
    best = None
    for n in root.iter("node"):
        cls = n.get("class") or ""
        if "EditText" not in cls:
            continue
        rid = n.get("resource-id") or ""
        if rid.startswith(f"{CHROME_PKG}:id/") or rid.endswith("url_bar") \
                or rid.endswith("search_box_text"):
            continue
        rect = _parse_bounds(n.get("bounds", ""))
        if not rect:
            continue
        x1, y1, x2, y2 = rect
        if y1 < 160 or y2 - y1 < 40:
            continue
        if x2 - x1 < 120:
            continue
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        score = (x2 - x1) * (y2 - y1) - y1
        if best is None or score > best[0]:
            best = (score, cx, cy)
    if best:
        return best[1], best[2]
    return None


def _focused_is_chrome_omnibox(d: u2.Device) -> bool:
    """현재 포커스가 Chrome 주소/검색창이면 True."""
    try:
        for rid in (URL_BAR, SEARCH_BOX):
            if d(resourceId=rid, focused=True).exists(timeout=0):
                return True
        # focused EditText resource-id 검사
        try:
            root = ET.fromstring(d.dump_hierarchy())
        except Exception:
            return False
        for n in root.iter("node"):
            if n.get("focused") != "true":
                continue
            rid = n.get("resource-id") or ""
            if rid.startswith(f"{CHROME_PKG}:id/"):
                return True
            cls = n.get("class") or ""
            if "EditText" in cls and (
                rid.endswith("url_bar") or rid.endswith("search_box_text")
            ):
                return True
        return False
    except Exception:
        return False


def focus_naver_search(d: u2.Device, timeout: float = 8.0) -> bool:
    """네이버 홈/결과의 검색창 포커스 (Chrome 옴니박스 금지)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        dismiss_chrome_noise(d, rounds=1)
        # 이미 옴니박스 포커스면 ESC/뒤로로 해제 후 재시도
        if _focused_is_chrome_omnibox(d):
            try:
                d.press("back")
                time.sleep(0.15)
            except Exception:
                pass
        pt = find_naver_search_edit(d)
        if pt:
            d.click(*pt)
            time.sleep(0.18)
            if not _focused_is_chrome_omnibox(d) and find_naver_search_edit(d):
                return True
        # SERP 상단 네이버 검색바 대략 좌표 (url_bar 아래)
        # Chrome url_bar ~ y=120–180, 네이버 검색은 그 아래
        for y_frac in (0.14, 0.17, 0.20, 0.23):
            d.click(0.42, y_frac)
            time.sleep(0.12)
            if _focused_is_chrome_omnibox(d):
                d.press("back")
                time.sleep(0.12)
                continue
            if find_naver_search_edit(d):
                return True
        time.sleep(0.12)
    return False


def paste_into_naver_search(d: u2.Device, text: str) -> bool:
    """네이버 검색 EditText 에만 붙여넣기. 옴니박스면 실패."""
    if _focused_is_chrome_omnibox(d):
        log("    ... 포커스가 Chrome 주소창 — 붙여넣기 중단")
        return False
    pt = find_naver_search_edit(d)
    if not pt:
        return False
    d.click(*pt)
    time.sleep(0.12)
    if _focused_is_chrome_omnibox(d):
        return False
    paste_search_keyword(d, text)
    # 반영 확인 (네이버 EditText)
    try:
        root = ET.fromstring(d.dump_hierarchy())
        for n in root.iter("node"):
            cls = n.get("class") or ""
            if "EditText" not in cls:
                continue
            rid = n.get("resource-id") or ""
            if rid.startswith(f"{CHROME_PKG}:id/"):
                continue
            cur = (n.get("text") or "").strip()
            if _edit_text_matches(cur, text):
                return True
    except Exception:
        pass
    # set_text 재시도 (비-크롬 EditText)
    try:
        root = ET.fromstring(d.dump_hierarchy())
        for n in root.iter("node"):
            cls = n.get("class") or ""
            if "EditText" not in cls:
                continue
            rid = n.get("resource-id") or ""
            if rid.startswith(f"{CHROME_PKG}:id/"):
                continue
            rect = _parse_bounds(n.get("bounds", ""))
            if not rect:
                continue
            x1, y1, x2, y2 = rect
            d.click((x1 + x2) // 2, (y1 + y2) // 2)
            time.sleep(0.2)
            try:
                d.clear_text()
            except Exception:
                pass
            d.send_keys(text, clear=True)
            time.sleep(0.25)
            return True
    except Exception as e:
        log(f"    ... 네이버 검색창 입력 실패: {e}")
    return False


def _query_ok_in_url(cur: str, keyword: str) -> bool:
    """URL query 에 키워드(공백 무시·토큰)가 들어있는지 확인."""
    decoded = unquote(cur or "")
    compact = decoded.replace(" ", "").replace("+", "")
    want = keyword.strip()
    if not want:
        return True
    if want.replace(" ", "") in compact:
        return True
    tokens = [t for t in want.split() if len(t) >= 2]
    if len(tokens) >= 2:
        # 2차 키워드: 모든 토큰 필수 (광주예물만으로 통과 금지)
        return all(t in decoded or t in compact for t in tokens)
    return tokens[0] in decoded if tokens else True


def wait_search_results(d: u2.Device, keyword: str, timeout: float = 14.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = url_bar_text(d)
        low = cur.lower()
        if "search.naver" in low and "query=" in low:
            if _query_ok_in_url(cur, keyword):
                log(f"[+] 검색 결과 URL 확인 ({cur[:70]})")
                time.sleep(SERP_URL_SETTLE)
                return True
            log(f"    ... query 불일치 대기: '{keyword}' vs {cur[:60]}")
        time.sleep(0.18)
    return False


def _debug_shot(d: u2.Device, tag: str) -> None:
    """실패 순간 화면 캡처 저장 (원인 추적용). campaign_web/logs/shots/ 에 남는다."""
    try:
        from pathlib import Path
        import device_stats as _dstat
        out = Path(__file__).resolve().parent / "campaign_web" / "logs" / "shots"
        out.mkdir(parents=True, exist_ok=True)
        serial = getattr(d, "serial", "") or ""
        img = _dstat.screenshot_jpeg_b64(serial, width=540, quality=70)
        if not img:
            return
        import base64, time as _t
        f = out / f"{_t.strftime('%m%d_%H%M%S')}_{serial}_{tag}.jpg"
        f.write_bytes(base64.b64decode(img))
        # 오래된 캡처 정리 (최근 60장만)
        shots = sorted(out.glob("*.jpg"))
        for old in shots[:-60]:
            old.unlink(missing_ok=True)
        log(f"[*] 실패 화면 저장: {f.name}")
    except Exception:
        pass


def search_on_naver(d: u2.Device, keyword: str) -> bool:
    """네이버 검색창에만 붙여넣기 검색 (Chrome 옴니박스/URL 해킹 없음)."""
    log(f"[*] 네이버 검색창 붙여넣기: '{keyword}'")
    if not focus_naver_search(d, timeout=8.0):
        _debug_shot(d, "no_search_box")
        raise RuntimeError("네이버 검색창을 찾지 못했습니다.")
    if not paste_into_naver_search(d, keyword):
        raise RuntimeError("네이버 검색창 붙여넣기 실패(주소창 회피)")
    time.sleep(SEARCH_POST_TYPE_DELAY)
    d.press("enter")
    time.sleep(SEARCH_POST_ENTER_DELAY)
    dismiss_chrome_noise(d, rounds=1)
    if not wait_search_results(d, keyword, timeout=14.0):
        raise RuntimeError(f"검색 결과 query 미일치: '{keyword}'")
    return True


def ensure_naver_surface_for_search(d: u2.Device) -> None:
    """검색창이 보이는 네이버 홈/SERP 로 복귀. 키워드 URL 해킹 없이 홈만 옴니박스 허용."""
    leave_detail_to_clean_search(d)
    cur = url_bar_text(d).lower()
    if "search.naver" in cur or cur.endswith("m.naver.com") or cur == "m.naver.com":
        if focus_naver_search(d, timeout=4.0):
            return
    log("[*] 검색면 복귀 → m.naver.com")
    navigate_omnibox(d, NAVER_HOME)
    time.sleep(NAVER_LAND_SETTLE)

def _label_of(node) -> str:
    raw = ((node.get("content-desc") or "") + " " + (node.get("text") or "")).strip()
    return raw.replace("\xa0", " ").strip()


def _looks_like_hash(label: str) -> bool:
    s = label.strip()
    if re.fullmatch(r"[0-9a-fA-F-]{20,}", s):
        return True
    if re.fullmatch(r"\d{5,}-[0-9a-fA-F-]{10,}", s):
        return True
    return False


def _rid_short(rid: str) -> str:
    if not rid:
        return ""
    return rid.rsplit("/", 1)[-1]


def _is_ad_section_rid(rid: str) -> bool:
    short = _rid_short(rid)
    if not short:
        return False
    if short in AD_SECTION_RID_EXACT:
        return True
    low = short.lower()
    return any(s in low for s in AD_SECTION_RID_SUBSTR)


def _is_clip_section_rid(rid: str) -> bool:
    low = _rid_short(rid).lower()
    return bool(low) and any(s in low for s in CLIP_SECTION_RID_SUBSTR)


def _is_map_place_section_rid(rid: str) -> bool:
    low = _rid_short(rid).lower()
    return bool(low) and any(s in low for s in MAP_PLACE_RID_SUBSTR)


def _is_related_search_section_rid(rid: str) -> bool:
    low = _rid_short(rid).lower()
    return bool(low) and any(s in low for s in RELATED_SEARCH_RID_SUBSTR)


def _label_is_related_search(label: str) -> bool:
    """연관검색어/함께 많이 찾는 칩·섹션 헤더 — 클릭하면 query 이탈."""
    if not label:
        return False
    compact = label.strip()
    if compact in RELATED_SEARCH_SECTION_HINTS:
        return True
    if any(h in label for h in RELATED_SEARCH_SECTION_HINTS):
        return True
    if any(h in label for h in RELATED_SEARCH_CHIP_HINTS):
        return True
    return False


def _label_is_ad(label: str) -> bool:
    if not label:
        return False
    if any(a in label for a in AD_HINTS):
        return True
    compact = label.strip()
    return compact in ("광고", "AD", "Ad", "Sponsored")


def _label_is_clip(label: str) -> bool:
    if not label:
        return False
    low = label.lower()
    return any(h.lower() in low for h in CLIP_LABEL_HINTS)


def _label_is_map_place(label: str) -> bool:
    if not label:
        return False
    if label.strip() in ("지도", "플레이스", "길찾기", "전화", "관련도순", "거리순"):
        return True
    if any(h in label for h in MAP_PLACE_LABEL_HINTS):
        return True
    if _PLACE_RATING_RE.search(label) or _PLACE_MENU_RE.search(label):
        return True
    # "OO 광주점" 로컬팩 상호 — 글 힌트 있으면 제외하지 않음
    if _PLACE_STORE_RE.search(label) and len(label.strip()) <= 40:
        if not any(h in label for h in ARTICLE_LABEL_HINTS):
            return True
    return False


def _label_looks_like_place_review(label: str) -> bool:
    """플레이스 방문자 리뷰 스니펫 — 클릭하면 map/place 후 back 낭비."""
    if not label or _label_is_article(label):
        return False
    compact = label.strip()
    # 도메인/URL 있으면 웹결과로 취급
    low = compact.lower()
    if any(x in low for x in (".com", ".co.kr", ".kr", "www.", "http",
                              "blog.", "cafe.", "tistory", "naver.me")):
        return False
    # 구어체 방문후기 패턴 (문장부호·감탄·맛집 서술)
    if len(compact) >= 28 and any(
        p in compact for p in (
            "맛있", "친절", "사장", "밑반찬", "점심", "저녁", "고기",
            "예약", "웨이팅", "가성비", "ㅠ", "ㅋㅋ", "ㅎㅎ", "!!!",
            "왔어요", "갔습니다", "왔는데", "먹어", "추천",
        )
    ):
        return True
    return False


def _is_suyu_place_tune(*parts: str) -> bool:
    blob = " ".join(p or "" for p in parts)
    return any(k in blob for k in SUYU_PLACE_TUNE_KEYS)


def _label_is_junk(label: str) -> bool:
    if not label:
        return True
    compact = label.strip()
    if compact in UI_TAB_LABELS or compact in UI_BTN_LABELS:
        return True
    if any(j in label for j in JUNK_LABEL_SUBSTR):
        return True
    if _label_is_related_search(label):
        return True
    # 쿠폰 뱃지 반복
    if label.count("쿠폰") >= 2:
        return True
    return False


def _label_is_article(label: str) -> bool:
    if not label:
        return False
    compact = label.strip()
    if compact in ARTICLE_LABEL_EXACT:
        return True
    if any(h in label for h in ARTICLE_LABEL_HINTS):
        return True
    return any(p.search(label) for p in ARTICLE_LABEL_BOUNDED)


def _url_is_clip(url: str) -> bool:
    low = (url or "").lower()
    return any(h in low for h in CLIP_URL_HINTS)


def _url_is_map_place(url: str) -> bool:
    low = (url or "").lower()
    return any(h in low for h in MAP_PLACE_URL_HINTS)


def _url_is_article(url: str) -> bool:
    low = (url or "").lower()
    return any(h in low for h in ARTICLE_URL_HINTS)


def _url_is_ad(url: str) -> bool:
    low = (url or "").lower()
    return any(h in low for h in ("adcr.naver", "ad.search", "shopping.naver", "ad.naver"))


def _has_ad_section(root: ET.Element) -> bool:
    for n in root.iter("node"):
        if _is_ad_section_rid(n.get("resource-id") or ""):
            return True
        lab = _label_of(n)
        if lab.strip() in ("광고", "AD", "Ad") or "파워링크" in lab or "파워컨텐츠" in lab:
            return True
    return False


def collect_article_candidates(d: u2.Device,
                               root: Optional[ET.Element] = None
                               ) -> List[Tuple[int, int, str]]:
    """광고·지도·플레이스·클립·연관검색어·UI 제외, 글(블로그/카페/뉴스/웹문서) 우선 후보."""
    if root is None:
        try:
            root = ET.fromstring(d.dump_hierarchy())
        except Exception:
            return []
    _, h = d.window_size()
    y_min = int(h * 0.22)
    y_max = int(h * 0.92)
    cands: List[Tuple[int, int, str, int, int]] = []

    def walk(node, in_ad: bool, in_map: bool, in_clip: bool, in_related: bool):
        rid = node.get("resource-id") or ""
        label = _label_of(node)
        if _is_ad_section_rid(rid) or _label_is_ad(label):
            in_ad = True
        if _is_map_place_section_rid(rid) or any(
            hh in label for hh in (
                "지도보기", "관련도순", "거리순", "위치 정보",
                "쿠폰 있음", "다른 지역 검색", "상세주소",
                "영업 전", "별점", "방문자 리뷰", "주간 인기 많은 메뉴",
                "후기 확인하기", "휠체어 출입",
            )
        ):
            in_map = True
        if _is_clip_section_rid(rid) or (
            _label_is_clip(label) and any(
                hh in label for hh in ("숏츠", "shorts", "숏폼", "네이버TV", "클립")
            ) and label.strip() not in UI_TAB_LABELS
        ):
            in_clip = True
        if (
            _is_related_search_section_rid(rid)
            or any(hh in label for hh in RELATED_SEARCH_SECTION_HINTS)
            or any(hh in label for hh in RELATED_SEARCH_CHIP_HINTS)
        ):
            in_related = True

        children = list(node)
        # 헤더와 칩이 형제인 연관검색 구좌 — 전체 WebView가 아닌 중간 높이 컨테이너만
        if not in_related and children:
            rect_p = _parse_bounds(node.get("bounds", ""))
            if rect_p:
                _x1, py1, _x2, py2 = rect_p
                ph = py2 - py1
                if 80 <= ph <= int(h * 0.55):
                    if any(
                        any(hh in _label_of(ch) for hh in RELATED_SEARCH_SECTION_HINTS)
                        for ch in children
                    ):
                        in_related = True

        if (
            not in_ad and not in_map and not in_clip and not in_related
            and node.get("clickable") == "true"
            and not rid.startswith(f"{CHROME_PKG}:id/")
            and "systemui" not in rid
            and not _label_is_map_place(label)
            and not _label_is_clip(label)
            and not _label_is_related_search(label)
            and not _label_looks_like_place_review(label)
        ):
            rect = _parse_bounds(node.get("bounds", ""))
            if rect:
                x1, y1, x2, y2 = rect
                cy = (y1 + y2) // 2
                w, ht = x2 - x1, y2 - y1
                if (y_min <= cy <= y_max and ht >= 40 and w >= 120
                        and len(label) >= 8):
                    compact = label.strip()
                    if (
                        not _label_is_junk(label)
                        and not _looks_like_hash(compact)
                        and not (len(compact) <= 10 and " " not in compact and ht < 140)
                    ):
                        is_art = _label_is_article(label)
                        looks_web = any(
                            x in label.lower()
                            for x in (".com", ".co.kr", ".kr", "www.", "http",
                                      "blog.", "cafe.", "tistory", "naver.me")
                        )
                        # 제목만 노출되는 유기 결과(뱃지 미포함)도 후보 — 광고/지도/클립은 상단에서 이미 배제
                        organic_long = len(compact) >= 16
                        organic_mid = (
                            len(compact) >= 12
                            and (" " in compact or "·" in compact or "|" in compact)
                        )
                        if is_art or looks_web or organic_long or organic_mid:
                            cx = (x1 + x2) // 2
                            area = w * ht
                            score = len(label)
                            if is_art:
                                score += 200
                            if looks_web and not is_art:
                                score += 40
                            if organic_long and not is_art and not looks_web:
                                score += 25
                            if organic_mid and not is_art and not looks_web:
                                score += 12
                            if ht > int(h * 0.45) and not is_art:
                                score -= 120
                            cands.append((cx, cy, label[:100], area, score))

        for child in children:
            walk(child, in_ad, in_map, in_clip, in_related)

    walk(root, False, False, False, False)
    cands.sort(key=lambda x: (x[4], x[3]), reverse=True)
    out: List[Tuple[int, int, str]] = []
    used_y: List[int] = []
    for cx, cy, label, _, score in cands:
        if score < 10:
            continue
        if any(abs(cy - uy) < 70 for uy in used_y):
            continue
        out.append((cx, cy, label))
        used_y.append(cy)
    return out


def _url_query_value(url: str) -> str:
    """search.naver URL 의 query= 값(decoded). 없으면 ''."""
    m = re.search(r"[?&]query=([^&]*)", url or "", re.I)
    if not m:
        return ""
    return unquote(m.group(1).replace("+", " ")).strip()


def landing_ok(d: u2.Device, pre_url: str) -> str:
    """허용 랜딩이면 'ok' / 검색목록이면 '' / 금지면 'bad:<reason>'."""
    cur = url_bar_text(d)
    if not cur or cur == pre_url:
        return ""
    low = cur.lower()
    if "search.naver.com/search.naver" in low or (
        "search.naver" in low and "query=" in low
    ):
        # 연관검색어 칩 클릭 → SERP 유지지만 query 변경 (캠페인 키워드 이탈)
        # 원문이 부분포함돼도('대전웨딩밴드'→'대전웨딩밴드 링플레이트…') 이탈로 본다.
        pre_q = _url_query_value(pre_url)
        cur_q = _url_query_value(cur)
        if pre_q and cur_q:
            pre_c = pre_q.replace(" ", "").lower()
            cur_c = cur_q.replace(" ", "").lower()
            if cur_c != pre_c:
                return "bad:related-search"
        return ""
    if _url_is_ad(cur):
        return "bad:ad"
    if _url_is_clip(cur):
        return "bad:clip"
    if _url_is_map_place(cur):
        return "bad:map/place"
    if _url_is_article(cur):
        return "ok"
    # 외부 웹 (광고 제외)
    if "naver.com" not in low and "ad." not in low:
        return "ok"
    if "smartstore" in low or "talk.naver" in low:
        return "ok"
    # 여행/예약 등 비-플레이스 네이버 (경주여행코스 등)
    if any(h in low for h in (
        "travel.naver", "booking.naver", "hotel.naver",
        "korean.visitkorea", "visitkorea",
    )):
        return "ok"
    # 기타 naver 서브도메인 — 보수적으로 거부
    if "naver.com" in low:
        return "bad:naver-other"
    return "ok"


def wait_landing(d: u2.Device, pre_url: str, timeout: float = 4.5) -> str:
    """클릭 후 URL 전환을 짧게 폴링. 느린 랜딩을 미전환으로 오판하지 않음."""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = landing_ok(d, pre_url)
        if last == "ok" or (last and last.startswith("bad:")):
            return last
        time.sleep(0.28)
    return last


def _serp_status(d: u2.Device) -> str:
    """'serp' / 'empty' / 'other' — 빈 url_bar 는 SERP 이탈로 오판하지 않음."""
    cur = url_bar_text(d)
    if not cur:
        return "empty"
    low = cur.lower()
    if "search.naver" in low and "query=" in low:
        return "serp"
    if "search.naver" in low:
        return "serp"
    return "other"


def _ensure_serp(d: u2.Device, pre_url: str, *, soft: bool = True) -> bool:
    """검색결과면 True. 빈 주소창/일시 이탈은 대기·뒤로로 복구(하드페일 금지)."""
    st = _serp_status(d)
    if st == "serp":
        return True
    if st == "empty":
        for _ in range(4):
            time.sleep(0.45)
            if _serp_status(d) == "serp":
                return True
        # 빈 채여도 계층에 후보가 있으면 사냥 계속
        return soft
    # 이미 허용 랜딩이면 호출측에서 성공 처리하도록 False + landing 확인
    if landing_ok(d, pre_url) == "ok":
        return False
    log(f"    ... SERP 이탈({(url_bar_text(d) or '')[:50]}) → back 복구")
    try:
        d.press("back")
        time.sleep(0.55)
    except Exception:
        pass
    if _serp_status(d) == "serp":
        return True
    # 하드페일 금지 — soft면 계속 사냥
    if soft:
        log("    ... SERP 복귀 미확인 → soft continue")
        return True
    log("    ... SERP 복귀 실패")
    return False


def click_any_result(d: u2.Device, *, timeout: float = 70.0,
                     max_scroll: int = 12, initial_scroll: int = 1,
                     fast_place_scroll: bool = False,
                     no_review_click: bool = False) -> bool:
    """광고·지도·클립 제외, 아래로 스크롤하며 글 결과만 클릭.
    fast_place_scroll: 로컬팩을 빠른·긴 스와이프로 통과 (수유소고기 테스트).
    no_review_click: 리뷰형 약유기 후보 클릭 금지 (place 진입→back 방지)."""
    log("[*] 검색 결과 클릭 (글 우선 · 지도/클립/광고/연관검색어 제외)")
    if fast_place_scroll:
        log("[*] 플레이스 구좌 모드: 빠른 스크롤 + 로컬팩 깊게 통과")
    if no_review_click:
        log("[*] 리뷰내용 클릭 금지 (place 진입→뒤로 스킵)")
    # 기본은 블라인드 오버슈트 억제(≤1). 플레이스 구좌는 로컬팩 깊어 더 통과.
    if fast_place_scroll:
        initial_scroll = max(0, min(int(initial_scroll or 0), 4))
        if initial_scroll < 3:
            initial_scroll = 3
    else:
        initial_scroll = max(0, min(int(initial_scroll or 0), 1))
    # url_bar 일시 공백/유기결과 lazy load 대기
    time.sleep(1.0)
    pre = url_bar_text(d)
    if not pre:
        time.sleep(1.0)
        pre = url_bar_text(d)
    # SERP 확인 — 빈 주소는 대기만 (이탈 오판 금지)
    for _ in range(5):
        if _serp_status(d) == "serp":
            break
        time.sleep(0.4)
    else:
        log("    ... SERP URL 미확정(빈/지연) → 후보 대기 후 계속")

    def _swipe_hunt(direction: str = "up"):
        human_swipe(d, direction=direction, mode="hunt",
                    fast_place=fast_place_scroll)

    def _is_strong_label(label: str) -> bool:
        if _label_is_article(label):
            return True
        low = label.lower()
        return any(
            x in low for x in (".com", ".co.kr", ".kr", "www.", "http",
                               "blog.", "cafe.", "tistory", "naver.me")
        )

    def _ordered_cands(root: Optional[ET.Element], *, allow_weak: bool = True):
        cands = collect_article_candidates(d, root=root) if root is not None \
            else collect_article_candidates(d)
        if no_review_click:
            cands = [c for c in cands if not _label_looks_like_place_review(c[2])]
        arts = [c for c in cands if _label_is_article(c[2])]
        strong_web = [c for c in cands if c not in arts and _is_strong_label(c[2])]
        weak = [c for c in cands if c not in arts and c not in strong_web]
        # 로컬팩 구간에서는 약유기(상호 슬로건) 클릭으로 map/place 낭비하지 않음
        # no_review_click: 약유기(리뷰 본문형)는 아예 클릭하지 않음
        if allow_weak and not no_review_click:
            return arts + strong_web + weak
        return arts + strong_web

    # 렌더 직후 후보가 늦게 뜨는 경우 — 스크롤 전에 짧게 폴링
    for _ in range(4):
        try:
            root_w = ET.fromstring(d.dump_hierarchy())
        except Exception:
            root_w = None
        if _ordered_cands(root_w, allow_weak=not no_review_click):
            break
        time.sleep(0.45)

    # 초기 블라인드 스크롤은 상단 웹문서(브랜드 .com 등)를 지나칠 수 있음.
    # 뷰포트에 글/웹 후보가 없을 때만 지도·광고 패스 스크롤.
    # 플레이스 구좌(fast): 강후보만 있어도 로컬팩이면 패스(약유기만이면 무조건 패스).
    for i in range(initial_scroll):
        try:
            root0 = ET.fromstring(d.dump_hierarchy())
        except Exception:
            root0 = None
        strong0 = _ordered_cands(root0, allow_weak=False)
        if strong0 and not fast_place_scroll:
            log(f"    ... 상단 글/웹 후보 감지 → 초기 패스 스크롤 중단 ({i}/{initial_scroll})")
            break
        if strong0 and fast_place_scroll and any(_is_strong_label(c[2]) and _label_is_article(c[2]) for c in strong0):
            log(f"    ... 상단 강글 후보 감지 → 초기 패스 스크롤 중단 ({i}/{initial_scroll})")
            break
        log(f"    ... 지도/광고 패스 스크롤 {i + 1}/{initial_scroll}"
            + (" (fast-place)" if fast_place_scroll else ""))
        _swipe_hunt()

    deadline = time.time() + timeout
    scrolls = 0
    recover_up = 0
    tried_labels = set()
    empty_streak = 0
    while time.time() < deadline:
        dismiss_chrome_noise(d, rounds=1)
        # 빈 url_bar ≠ 이탈. 진짜 이탈만 soft back (하드페일 없음)
        if not _ensure_serp(d, pre, soft=True):
            if landing_ok(d, pre) == "ok":
                log(f"[+] 페이지 진입: {url_bar_text(d)[:70]}")
                return True
        try:
            root = ET.fromstring(d.dump_hierarchy())
        except Exception:
            root = None
        # 초반(로컬팩)엔 강후보만 — 약유기 상호 클릭→map/place 낭비 억제
        # no_review_click이면 끝까지 강후보만
        allow_weak = (not no_review_click) and (scrolls >= 3 or recover_up > 0)
        ordered = _ordered_cands(root, allow_weak=allow_weak)

        if not ordered:
            empty_streak += 1
            # 플레이스 구좌: 글 없으면 빨리 폴백으로 넘김
            if fast_place_scroll and empty_streak >= 5 and scrolls >= 5:
                log("    ... 글 후보 지속 없음 → 플레이스 폴백으로 넘김")
                return False
            if scrolls < max_scroll:
                scrolls += 1
                reason = "파워링크/지도만" if (root is not None and _has_ad_section(root)) \
                    else ("약유기만(패스)" if not allow_weak and _ordered_cands(root, allow_weak=True)
                          else "글 후보 없음")
                log(f"    ... {reason} → 스크롤 {scrolls}/{max_scroll}")
                _swipe_hunt()
                continue
            # 아래로만 밀어 상단 웹결과를 지나친 경우 — 위로 복구 후 재탐색
            if recover_up < 4:
                recover_up += 1
                log(f"    ... 오버슈트 복구 스크롤↑ {recover_up}/4")
                _swipe_hunt(direction="down")
                scrolls = max(0, scrolls - 2)
                tried_labels.clear()
                continue
            time.sleep(0.18)
            continue

        empty_streak = 0
        # 상위 글 우선, 섞지 않고 점수순(이미 정렬)으로 시도
        for cx, cy, label in ordered[:10]:
            key = f"{label[:40]}@{cy // 50}"
            if key in tried_labels:
                continue
            tried_labels.add(key)
            kind = "글" if _label_is_article(label) else "웹"
            log(f"[+] {kind} 결과 클릭 시도: '{label[:60]}' @ ({cx},{cy})")
            d.click(cx, cy)
            status = wait_landing(d, pre, timeout=4.5)
            if status == "ok":
                log(f"[+] 페이지 진입: {url_bar_text(d)[:70]}")
                return True
            if status.startswith("bad:"):
                log(f"    ... 금지 랜딩({status[4:]}) → 뒤로 · 다른 글 탐색")
                try:
                    d.press("back")
                    time.sleep(0.45)
                except Exception:
                    pass
                _ensure_serp(d, pre, soft=True)
                # map/place 로컬팩이면 2연타 스와이프로 빠져나가기 (스크롤 예산은 1만 소모)
                _swipe_hunt()
                if "map" in status or "place" in status or "clip" in status:
                    _swipe_hunt()
                # 연관검색어로 query 가 바뀌었으면 원 SERP 로 복구 확인
                if "related-search" in status:
                    if not _ensure_serp(d, pre, soft=True):
                        log("    ... 연관검색어 이탈 후 원 SERP 복구 재시도")
                        try:
                            d.press("back")
                            time.sleep(0.4)
                        except Exception:
                            pass
                        _ensure_serp(d, pre, soft=True)
                    # query가 여전히 다르면 더 이상 같은 화면에서 칩을 누르지 않도록 스크롤
                    cur_now = url_bar_text(d)
                    if (_url_query_value(cur_now).replace(" ", "").lower()
                            != _url_query_value(pre).replace(" ", "").lower()):
                        log("    ... query 미복구 → 스크롤로 연관칩 구간 이탈")
                        _swipe_hunt()
                scrolls = min(scrolls + 1, max_scroll)
                break
            # 미전환 = 아직 SERP. back 하면 검색목록을 이탈해 이후 후보가 전멸함.
            log("    ... 미전환 → 다른 후보 (SERP 유지, back 안 함)")
        else:
            if scrolls < max_scroll:
                scrolls += 1
                log(f"    ... 후보 소진 → 스크롤 {scrolls}/{max_scroll}")
                _swipe_hunt()
            else:
                time.sleep(0.18)
    log("[!] 글 결과 클릭 실패")
    return False


def dwell_page(d: u2.Device, min_secs: float, max_secs: float) -> None:
    if max_secs < min_secs:
        min_secs, max_secs = max_secs, min_secs
    secs = random.uniform(min_secs, max_secs)
    log(f"[*] 체류 {secs:.1f}s (사람형 스크롤)")
    end = time.time() + secs
    while time.time() < end:
        direction = "up" if random.random() < 0.82 else "down"
        human_swipe(d, direction=direction, mode="dwell")
        linger = random.uniform(0.8, 2.4)
        if time.time() + linger > end:
            time.sleep(max(0.1, end - time.time()))
            break
        time.sleep(linger)
    log("[+] 체류 완료")


def leave_detail_to_clean_search(d: u2.Device) -> None:
    """상세/피드에서 벗어나 검색 가능한 상태(홈 또는 SERP)로."""
    for _ in range(6):
        cur = url_bar_text(d).lower()
        if _url_is_clip(cur) or _url_is_map_place(cur):
            d.press("back")
            time.sleep(0.3)
            continue
        if "search.naver.com/search.naver" in cur:
            return
        if cur.endswith("m.naver.com") or cur == "m.naver.com":
            return
        d.press("back")
        time.sleep(0.3)
        dismiss_chrome_noise(d, rounds=1)


def _collect_place_card_targets(d: u2.Device, place_hint: str = ""
                                 ) -> List[Tuple[int, int, str]]:
    """SERP에서 플레이스 카드(상호/별점) 클릭 좌표."""
    try:
        root = ET.fromstring(d.dump_hierarchy())
    except Exception:
        return []
    _, h = d.window_size()
    y_min, y_max = int(h * 0.18), int(h * 0.88)
    hint = (place_hint or "").strip()
    hint_tokens = [t for t in re.split(r"\s+", hint) if len(t) >= 2]
    out: List[Tuple[int, int, str, int]] = []
    for n in root.iter("node"):
        if n.get("clickable") != "true":
            continue
        label = _label_of(n)
        if not label or len(label) < 4:
            continue
        if _label_is_junk(label) or _label_is_ad(label) or _label_is_clip(label):
            continue
        # 블로그/카페 카드는 플레이스 폴백 대상이 아님 (힌트 토큰만으로 점수↑ 오인 방지)
        if _label_is_article(label):
            continue
        # 리뷰 본문·메뉴라인은 클릭하지 않음
        if _label_looks_like_place_review(label) or _PLACE_MENU_RE.search(label):
            continue
        if "connect+" in label.lower() or "리뷰 쓰고" in label:
            continue
        rect = _parse_bounds(n.get("bounds", ""))
        if not rect:
            continue
        x1, y1, x2, y2 = rect
        cy = (y1 + y2) // 2
        if not (y_min <= cy <= y_max):
            continue
        placey = bool(
            _label_is_map_place(label)
            or _PLACE_RATING_RE.search(label)
            or _PLACE_STORE_RE.search(label)
            or ("영업" in label)
        )
        score = 0
        if hint and hint in label:
            score += 100
        elif hint_tokens and any(t in label for t in hint_tokens):
            score += 60
        if _PLACE_RATING_RE.search(label) or "영업" in label:
            score += 40
        if _PLACE_STORE_RE.search(label):
            score += 30
        if score <= 0:
            continue
        # 토큰 부분일치만으로는 블로그 제목(…청가숯불구이…)이 섞임 → 플레이스 신호 필수
        if not placey and score < 100:
            continue
        cx = (x1 + x2) // 2
        out.append((cx, cy, label[:80], score))
    out.sort(key=lambda x: x[3], reverse=True)
    return [(a, b, c) for a, b, c, _ in out[:8]]


def place_fast_scroll_dwell(d: u2.Device, *,
                            min_secs: float = 8.0,
                            max_secs: float = 14.0,
                            scrolls: int = 8) -> None:
    """플레이스 상세: 리뷰 클릭 없이 빠르게 아래로만 스크롤·체류."""
    secs = random.uniform(min_secs, max_secs)
    log(f"[*] 플레이스 빠른 스크롤 체류 {secs:.1f}s (스크롤≈{scrolls}, 리뷰클릭 없음)")
    end = time.time() + secs
    for i in range(max(1, scrolls)):
        if time.time() >= end:
            break
        # 긴 플링 + 짧은 duration — 쭉 내려감
        x = 0.50 + random.uniform(-0.06, 0.06)
        d.swipe(x, random.uniform(0.78, 0.88), x,
                random.uniform(0.14, 0.24),
                duration=random.uniform(0.06, 0.12))
        time.sleep(random.uniform(0.08, 0.18))
        log(f"    플레이스 스크롤 {i + 1}/{scrolls}")
    # 남은 시간만 짧게 대기 (추가 탭/클릭 없음)
    rem = end - time.time()
    if rem > 0.2:
        time.sleep(rem)
    log("[+] 플레이스 스크롤 체류 완료")


def click_place_and_fast_scroll(d: u2.Device, place_hint: str = "", *,
                                dwell_min: float = 8.0,
                                dwell_max: float = 14.0) -> bool:
    """수유소고기 플레이스 구좌 폴백: 플레이스 카드 클릭 → 빠른 스크롤(리뷰 미클릭)."""
    log(f"[*] 플레이스 구좌 폴백 (hint='{place_hint[:40]}')")
    pre = url_bar_text(d)
    tried_labels: set = set()
    for round_i in range(3):
        targets = _collect_place_card_targets(d, place_hint)
        if not targets:
            log("    ... 로컬팩 재노출 위해 상단 복구 스크롤")
            for _ in range(5):
                human_swipe(d, direction="down", mode="hunt", fast_place=False)
                time.sleep(0.22)
            targets = _collect_place_card_targets(d, place_hint)
        if not targets:
            log("[!] 플레이스 카드 후보 없음")
            return False

        for cx, cy, label in targets[:6]:
            key = f"{label[:40]}@{cy // 40}"
            if key in tried_labels:
                continue
            tried_labels.add(key)
            log(f"[+] 플레이스 클릭 시도: '{label[:55]}' @ ({cx},{cy})")
            d.click(cx, cy)
            # place 랜딩 대기
            deadline = time.time() + 5.0
            landed = False
            while time.time() < deadline:
                cur = url_bar_text(d)
                if cur and cur != pre and _url_is_map_place(cur):
                    landed = True
                    break
                # 외부 예약/지도 앱 스킴도 플레이스로 간주하지 않고 허용
                if cur and cur != pre and "search.naver" not in cur.lower():
                    low = cur.lower()
                    # 블로그/카페·광고·클립은 플레이스 폴백 성공으로 치지 않음
                    if (_url_is_article(cur) or _url_is_ad(cur) or _url_is_clip(cur)
                            or "review" in low or "visitor" in low):
                        log(f"    ... 비-플레이스 랜딩({cur[:50]}) → back, 다른 카드")
                        try:
                            d.press("back")
                            time.sleep(0.4)
                        except Exception:
                            pass
                        _ensure_serp(d, pre, soft=True)
                        break
                    landed = True
                    break
                time.sleep(0.28)
            if not landed:
                log("    ... 미전환 → 다른 플레이스 카드")
                continue
            log(f"[+] 플레이스 진입: {url_bar_text(d)[:70]}")
            place_fast_scroll_dwell(d, min_secs=dwell_min, max_secs=dwell_max, scrolls=8)
            return True
        # 같은 화면만 시도하다 실패한 경우 — 로컬팩 안에서 살짝 내려 다른 카드
        if round_i < 2:
            log(f"    ... 플레이스 후보 재수집 ({round_i + 2}/3)")
            human_swipe(d, direction="up", mode="hunt", fast_place=False)
            time.sleep(0.3)
    log("[!] 플레이스 클릭 실패")
    return False


def clear_chrome(serial: str) -> None:
    log("[*] Chrome 앱 데이터 삭제 (pm clear)")
    try:
        ds._adb(serial, "shell", "am", "force-stop", CHROME_PKG)
    except Exception:
        pass
    ds._adb(serial, "shell", "pm", "clear", CHROME_PKG)
    time.sleep(2.0)


def run_once(d: u2.Device, args) -> tuple:
    """Returns: (성공 여부, 앱 진입 여부)."""
    log("[*] 로테이션 시작 전 데이터·IP 확인")
    if not ds.ensure_network(args.serial, timeout=args.recover_secs):
        log("[!] 연결 미확인 → Chrome 진입 생략")
        return False, False

    open_chrome(d, args.serial)
    if not navigate_omnibox(d, NAVER_HOME):
        return False, True

    # 1차 검색 → 글 클릭 → 체류 (네이버 검색창 붙여넣기만)
    try:
        search_on_naver(d, args.keyword)
    except RuntimeError as e:
        log(f"[!] 1차 검색 실패({e}) → 홈 재진입 후 검색창 재시도")
        navigate_omnibox(d, NAVER_HOME)
        try:
            search_on_naver(d, args.keyword)
        except RuntimeError as e2:
            log(f"[!] 1차 검색 재시도 실패: {e2}")
            return False, True

    if not click_any_result(
        d, timeout=args.click_timeout,
        max_scroll=args.max_scroll,
        initial_scroll=args.initial_scroll,
        fast_place_scroll=getattr(args, "fast_place_scroll", False),
        no_review_click=getattr(args, "no_review_click", False),
    ):
        if getattr(args, "fast_place_scroll", False):
            # 글 없어도 플레이스 구좌: 상호 클릭→빠른 스크롤 (리뷰 미클릭)
            if not click_place_and_fast_scroll(
                d, args.keyword2 or args.keyword,
                dwell_min=min(args.dwell_min, 10.0),
                dwell_max=min(args.dwell_max, 15.0),
            ):
                return False, True
        else:
            return False, True
    else:
        dwell_page(d, args.dwell_min, args.dwell_max)

    # 2차 검색 — 1차와 동일: 네이버 검색창 붙여넣기만 (옴니박스 query URL 금지)
    kw2 = args.keyword2
    log(f"[*] 2차 키워드 검색 시작: '{kw2}'")
    ensure_naver_surface_for_search(d)
    try:
        search_on_naver(d, kw2)
    except RuntimeError as e:
        log(f"[!] 2차 검색 실패({e}) → 홈 재진입 후 검색창 재시도")
        navigate_omnibox(d, NAVER_HOME)
        try:
            search_on_naver(d, kw2)
        except RuntimeError as e2:
            log(f"[!] 2차 검색 재시도 실패: {e2}")
            return False, True

    cur = url_bar_text(d)
    if not _query_ok_in_url(cur, kw2):
        log(f"[!] 2차 query 미확인(현재 1차일 수 있음) → 홈 후 검색창 재검색")
        navigate_omnibox(d, NAVER_HOME)
        try:
            search_on_naver(d, kw2)
        except RuntimeError:
            return False, True
        if not _query_ok_in_url(url_bar_text(d), kw2):
            log("[!] 2차 키워드 URL 검증 실패")
            return False, True

    if not click_any_result(
        d, timeout=args.click_timeout,
        max_scroll=args.max_scroll,
        initial_scroll=args.initial_scroll,
        fast_place_scroll=getattr(args, "fast_place_scroll", False),
        no_review_click=getattr(args, "no_review_click", False),
    ):
        if getattr(args, "fast_place_scroll", False):
            # 2차(상호명) SERP는 로컬팩만인 경우가 많음 → 플레이스 구좌 폴백
            if not click_place_and_fast_scroll(
                d, kw2,
                dwell_min=min(args.dwell_min, 10.0),
                dwell_max=min(args.dwell_max, 15.0),
            ):
                return False, True
        else:
            return False, True
    else:
        dwell_page(d, args.dwell_min, args.dwell_max)
    return True, True


def main():
    ap = argparse.ArgumentParser(description="[함많찾을] Chrome→네이버 검색 트래픽")
    ap.add_argument("keyword", nargs="?", default=DEFAULT_KEYWORD,
                    help=f"메인 키워드 (기본: {DEFAULT_KEYWORD})")
    ap.add_argument("--keyword2", default="",
                    help="2차 키워드 (미지정 시 place_name / 기본값)")
    ap.add_argument("--place-name", default="",
                    help="2차 키워드 별칭 (campaign place_name)")
    ap.add_argument("--serial", default=DEFAULT_SERIAL,
                    help=f"adb 시리얼 (기본: {DEFAULT_SERIAL})")
    ap.add_argument("--dwell-min", type=float, default=DEFAULT_DWELL_MIN)
    ap.add_argument("--dwell-max", type=float, default=DEFAULT_DWELL_MAX)
    ap.add_argument("--click-timeout", type=float, default=70.0)
    ap.add_argument("--max-scroll", type=int, default=12)
    ap.add_argument("--initial-scroll", type=int, default=1,
                    help="상단 지도/광고 패스용 초기 스크롤(기본 clamp≤1, fast-place면 ≤4)")
    ap.add_argument("--fast-place-scroll", action="store_true",
                    help="플레이스 구좌 테스트: 스크롤 가속·멀리 통과")
    ap.add_argument("--no-review-click", action="store_true",
                    help="리뷰내용형 후보 클릭 금지 (place 진입→back 방지)")
    ap.add_argument("--rotations", type=int, default=1,
                    help="반복 횟수 (0=무한)")
    ap.add_argument("--no-data-toggle", action="store_true")
    ap.add_argument("--off-secs", type=float, default=4.0)
    ap.add_argument("--recover-secs", type=float, default=25.0)
    ap.add_argument("--no-clear", action="store_true",
                    help="pm clear 비활성화")
    args = ap.parse_args()

    kw2 = (args.keyword2 or args.place_name or DEFAULT_KEYWORD2).strip()
    args.keyword2 = kw2
    if args.dwell_max < args.dwell_min:
        args.dwell_min, args.dwell_max = args.dwell_max, args.dwell_min

    # 수유소고기 캠페인 자동: 빠른 플레이스 스크롤 + 리뷰클릭 금지
    auto_tune = _is_suyu_place_tune(args.keyword, kw2, args.place_name or "")
    if auto_tune:
        if not args.fast_place_scroll:
            args.fast_place_scroll = True
        if not args.no_review_click:
            args.no_review_click = True
        # 로컬팩이 깊어 스크롤 예산·타임아웃을 넉넉히
        if args.max_scroll < 18:
            args.max_scroll = 18
        if args.click_timeout < 90:
            args.click_timeout = 90.0
        # initial_scroll≥3이면 2차(상호) SERP에서 로컬팩을 지나친 채 폴백 →
        # 카드 후보 없음/상호명 블로그 오클릭이 잦음. 상단 유지(≤1).
        if args.initial_scroll > 1:
            args.initial_scroll = 1

    data_toggle = not args.no_data_toggle
    do_clear = not args.no_clear

    d = ds.connect(args.serial)
    ds.setup_popup_watchers(d)

    results = []
    ip_changes = []
    infinite = args.rotations <= 0
    total = "∞" if infinite else str(args.rotations)
    i = 0

    log(f"[*] 함많찾을 키워드: '{args.keyword}' / 2차 '{args.keyword2}'")
    log(f"[*] 체류: {args.dwell_min:.0f}~{args.dwell_max:.0f}s")
    log("[*] 클릭 정책: 글(블로그/카페/뉴스) 우선 · 지도/플레이스/클립/광고/연관검색어 제외")
    if args.fast_place_scroll or args.no_review_click:
        log(
            f"[*] 수유소고기-only 플레이스 옵션: "
            f"fast_place_scroll={args.fast_place_scroll} "
            f"no_review_click={args.no_review_click} "
            f"max_scroll={args.max_scroll} initial_scroll={args.initial_scroll}"
        )

    try:
        while infinite or i < args.rotations:
            i += 1
            ds.begin_cycle_timer()
            cycle_start = time.time()
            log(f"\n===== 로테이션 {i}/{total} 시작 (함많찾을) =====")
            try:
                ok, opened = run_once(d, args)
            except Exception as e:
                log(f"[!] 로테이션 {i} 오류: {e}")
                ok, opened = False, True

            if opened:
                try:
                    d.screenshot(os.path.join(ds._SCRIPT_DIR, "hamman_result.png"))
                except Exception:
                    pass

            log(f"[*] 로테이션 {i} (작업 성공={ok}, 진입={opened})")
            results.append(ok)

            log(f"[*] 로테이션 {i} 마무리")
            if opened:
                try:
                    d.app_stop(CHROME_PKG)
                except Exception:
                    ds._adb(args.serial, "shell", "am", "force-stop", CHROME_PKG)

            if do_clear and opened:
                clear_chrome(args.serial)
            elif do_clear:
                log("[*] Chrome 미진입 — pm clear 생략")

            # 가나닭강정(kakao/daum)과 동일: ds.toggle_mobile_data → 변경 전/후 IP 로그
            if data_toggle:
                changed = ds.toggle_mobile_data(
                    args.serial, off_secs=args.off_secs,
                    recover_timeout=args.recover_secs,
                )
                ip_changes.append(changed)

            cycle_elapsed = time.time() - cycle_start
            ipc = f", IP변경 {sum(ip_changes)}/{len(ip_changes)}" if ip_changes else ""
            log(f"[누적] 작업 성공 {sum(results)}/{len(results)}{ipc}")
            log(f"===== 로테이션 {i}/{total} 완료 — 소요 {cycle_elapsed:.1f}s =====")
            ds.end_cycle_timer()
    except KeyboardInterrupt:
        log("\n[*] 사용자 중단(Ctrl+C)")
        raise SystemExit(130)
    finally:
        log("\n========== 최종 요약 (함많찾을) ==========")
        log(f"[+] 작업 성공: {sum(results)}/{len(results)} 로테이션")
        if ip_changes:
            log(f"[+] IP 변경 성공: {sum(ip_changes)}/{len(ip_changes)} 회")
        try:
            d.watcher.stop()
        except Exception:
            pass

    if not results or not any(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
