# -*- coding: utf-8 -*-
"""
[함많찾을 V2] Chrome → 네이버 검색 트래픽 — DevTools(CDP) 정밀 제어 + 데이터 절감판.

V1(hamman_device.py) 과 같은 흐름:
    네이버 메인 → 1차 키워드 검색 → 글 클릭 → 체류 → 2차 키워드 검색 → 글 클릭 → 체류
    → 브라우저 식별정보 초기화 → 모바일 데이터 OFF/ON(IP 변경)

V1 대비 달라진 점
  1) 클릭 대상 판정: 화면 라벨 추정이 아니라 ADB 로 붙인 크롬 DevTools 에서 링크의 '실제 목적지 URL'을
     읽어 판정한다. 광고(ader)·플레이스(m.place)·지도·연관검색어·프로필 링크는 누르기 전에 걸러지므로
     "플레이스 진입 → 즉시 뒤로" 낭비가 구조적으로 없다.
  2) 탭 정밀도: 목표 글을 화면 안으로 스크롤한 뒤, 그 좌표에 실제로 해당 링크가 있는지
     (elementFromPoint) 확인하고 누른다. 스크롤 관성으로 엉뚱한 카드를 누르는 문제 제거.
  3) 데이터 절감:
     - pm clear 대신 쿠키·스토리지만 삭제 → 네이버 정적 파일(JS/CSS/폰트/공통 이미지) HTTP 캐시 재사용
     - 네이버 메인 화면 이미지 차단(검색창만 쓰는 화면), 검색 제출 직전에 해제
     - 체류 스크롤을 '읽는 속도'로 줄여 불필요한 lazy 이미지 로딩 감소
     - 크롬 첫 화면(추천 피드) 경유 없이 about:blank 탭에서 바로 이동
  4) 단계별 수신 바이트를 로그로 남겨 어디서 데이터가 쓰이는지 추적

사용법(워커가 campaign params.engine == "v2" 일 때 호출):
    python hamman_v2_device.py 안산치과 --keyword2 "안산치과 안산미플란트치과" --serial R3CN90GN13H
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import unquote, urlparse

import daum_search_device as ds

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import uiautomator2 as u2  # noqa: E402
import urllib.request  # noqa: E402
import websocket  # websocket-client  # noqa: E402

import hamman_device as v1  # 온보딩/팝업 처리·사람형 스와이프 재사용  # noqa: E402

CHROME_PKG = v1.CHROME_PKG
ROOT = Path(__file__).resolve().parent
SHOT_DIR = ROOT / "campaign_web" / "logs" / "shots_v2"

NAVER_HOME_URL = "https://m.naver.com/"

# ── 링크 분류 ────────────────────────────────────────────────────────────
# 절대 누르지 않는 목적지 (광고·플레이스·지도·쇼핑·동영상·네이버 UI)
BAD_HOST_SUBSTR = (
    "ader.naver.com", "adcr.naver.com", "ad.search.naver.com", "m.ad.search.naver",
    "place.naver.com", "map.naver.com", "nmap.naver", "booking.naver",
    "shopping.naver", "smartstore.naver", "tv.naver.com", "clip.naver",
    "help.naver.com", "policy.naver.com", "nid.naver.com", "navercorp.com",
    "dict.naver.com", "m.naver.com", "search.naver.com", "talk.naver.com",
    "pay.naver.com", "keep.naver.com", "notify.naver",
    # 영상·SNS — 자동재생/무거운 페이지로 데이터 낭비
    "youtube.com", "youtu.be", "instagram.com", "facebook.com", "tiktok.com",
)
# 글(우선 클릭) — 경로에 글 번호가 있어야 함 (프로필/카페 홈 제외)
ARTICLE_RULES = (
    ("blog.naver.com", re.compile(r"^/[^/]+/\d{6,}|PostView", re.I)),
    ("cafe.naver.com", re.compile(r"^/[^/]+/\d{3,}|/articles/\d+", re.I)),
    ("in.naver.com", re.compile(r"/contents/", re.I)),
    ("post.naver.com", re.compile(r"viewer|/\d{5,}", re.I)),
    ("news.naver.com", re.compile(r"/article/|/mnews/", re.I)),
    ("kin.naver.com", re.compile(r"docId=|/qna/", re.I)),
)


def log(msg: str = "") -> None:
    ds.log(msg)


def adb(serial: str, *args: str, timeout: float = 25.0) -> subprocess.CompletedProcess:
    return ds._adb(serial, *args, timeout=timeout)


# ══════════════════════════════════════════════════════════════════════════
# 최소 CDP 클라이언트 (websocket-client + 수신 스레드)
# ══════════════════════════════════════════════════════════════════════════
class CDP:
    def __init__(self, ws_url: str):
        # 크롬 111+ 는 Origin 헤더가 있으면 거부 → suppress_origin
        self.ws = websocket.create_connection(ws_url, suppress_origin=True, timeout=30,
                                              enable_multithread=True)
        self._id = 0
        self._lock = threading.Lock()
        self._waiters: Dict[int, Dict[str, Any]] = {}
        self._handlers: Dict[str, List[Callable[[dict], None]]] = {}
        self._alive = True
        self._t = threading.Thread(target=self._reader, daemon=True)
        self._t.start()

    def _reader(self) -> None:
        while self._alive:
            try:
                raw = self.ws.recv()
            except Exception:
                self._alive = False
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if "id" in msg:
                w = self._waiters.get(msg["id"])
                if w is not None:
                    w["msg"] = msg
                    w["ev"].set()
            else:
                for fn in self._handlers.get(msg.get("method", ""), []):
                    try:
                        fn(msg.get("params") or {})
                    except Exception:
                        pass

    def on(self, method: str, fn: Callable[[dict], None]) -> None:
        self._handlers.setdefault(method, []).append(fn)

    def send(self, method: str, params: Optional[dict] = None, timeout: float = 20.0) -> dict:
        if not self._alive:
            raise RuntimeError("CDP 연결 끊김")
        with self._lock:
            self._id += 1
            mid = self._id
        w = {"ev": threading.Event(), "msg": None}
        self._waiters[mid] = w
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        if not w["ev"].wait(timeout):
            self._waiters.pop(mid, None)
            raise TimeoutError(f"CDP 응답 없음: {method}")
        self._waiters.pop(mid, None)
        msg = w["msg"]
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error'].get('message')}")
        return msg.get("result") or {}

    def eval(self, expr: str, timeout: float = 10.0) -> Any:
        """Runtime.enable 없이 평가 (페이지에서 디버거 부착 감지 여지를 줄임)."""
        r = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                           "awaitPromise": True}, timeout=timeout)
        if r.get("exceptionDetails"):
            raise RuntimeError(str(r["exceptionDetails"].get("text", "eval error")))
        return (r.get("result") or {}).get("value")

    def close(self) -> None:
        self._alive = False
        try:
            self.ws.close()
        except Exception:
            pass


class Chrome:
    """ADB 포워딩으로 폰 크롬 DevTools 에 붙는다."""

    def __init__(self, serial: str):
        self.serial = serial
        self.port = 0
        self.cdp: Optional[CDP] = None
        self.target_id = ""
        self.bytes = 0                   # 현재 단계 수신 바이트
        self.phase_bytes: Dict[str, int] = {}
        self.origins: set = set()        # 방문 origin (종료 시 스토리지 삭제 대상)

    # ── 연결 ──
    def forward(self) -> None:
        r = adb(self.serial, "forward", "tcp:0", "localabstract:chrome_devtools_remote")
        port = (r.stdout or "").strip()
        if not port.isdigit():
            raise RuntimeError(f"devtools 포워딩 실패: {r.stdout} {r.stderr}")
        self.port = int(port)

    def unforward(self) -> None:
        if self.port:
            try:
                adb(self.serial, "forward", "--remove", f"tcp:{self.port}")
            except Exception:
                pass

    def _http(self, path: str, method: str = "GET") -> Any:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        with urllib.request.urlopen(req, timeout=8) as r:
            body = r.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except Exception:
            return body

    def pages(self) -> List[dict]:
        try:
            return [t for t in self._http("/json/list") if t.get("type") == "page"]
        except Exception:
            return []

    def attach(self, target: dict) -> None:
        if self.cdp:
            self.cdp.close()
        self.target_id = target["id"]
        self.cdp = CDP(target["webSocketDebuggerUrl"])
        self.cdp.on("Network.loadingFinished", self._on_finished)
        self.cdp.on("Network.requestWillBeSent", self._on_request)
        self.cdp.send("Network.enable", {})
        self.cdp.send("Page.enable", {})

    def _on_finished(self, p: dict) -> None:
        self.bytes += int(p.get("encodedDataLength") or 0)

    def _on_request(self, p: dict) -> None:
        try:
            u = urlparse((p.get("request") or {}).get("url", ""))
            if u.scheme in ("http", "https") and p.get("type") == "Document":
                self.origins.add(f"{u.scheme}://{u.netloc}")
        except Exception:
            pass

    def phase(self, name: str) -> None:
        """이전 단계 바이트를 마감하고 새 단계 시작."""
        if getattr(self, "_phase", None):
            self.phase_bytes[self._phase] = self.phase_bytes.get(self._phase, 0) + self.bytes
        self._phase = name
        self.bytes = 0

    # ── 페이지 상태 ──
    def url(self) -> str:
        try:
            return self.cdp.eval("location.href", timeout=5) or ""
        except Exception:
            for t in self.pages():
                if t["id"] == self.target_id:
                    return t.get("url", "")
            return ""

    def wait_ready(self, timeout: float = 25.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.cdp.eval("document.readyState", timeout=4) in ("interactive", "complete"):
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    def wait_url(self, pred: Callable[[str], bool], timeout: float = 15.0) -> str:
        end = time.time() + timeout
        cur = ""
        while time.time() < end:
            cur = self.url()
            if cur and pred(cur):
                return cur
            time.sleep(0.3)
        return ""

    def viewport(self) -> dict:
        return self.cdp.eval("({w: innerWidth, h: innerHeight, y: scrollY, dh: document.documentElement.scrollHeight})") or {}

    # ── 입력 (실제 입력 파이프라인을 타는 터치/스크롤) ──
    def tap(self, x: float, y: float) -> None:
        pt = {"x": x, "y": y, "radiusX": random.uniform(4, 9), "radiusY": random.uniform(4, 9), "force": 1}
        self.cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [pt]})
        time.sleep(random.uniform(0.05, 0.13))
        self.cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    def scroll(self, dy: float, *, speed: Optional[int] = None) -> None:
        """dy>0 이면 아래(내용이 위로)로 스크롤. 터치 제스처로 수행.
        맨 위에서 위로 당기면 크롬 '당겨서 새로고침'이 발동하므로 남은 거리만큼만 올린다."""
        vp = self.viewport()
        w, h = vp.get("w", 400), vp.get("h", 700)
        if dy < 0:
            dy = max(dy, -(vp.get("y", 0) - 4))
        else:
            dy = min(dy, max(0, vp.get("dh", 1e9) - vp.get("y", 0) - h))
        if abs(dy) < 24:
            return
        x = w * random.uniform(0.35, 0.65)
        y = h * (random.uniform(0.62, 0.78) if dy > 0 else random.uniform(0.25, 0.4))
        self.cdp.send("Input.synthesizeScrollGesture", {
            "x": x, "y": y, "yDistance": -dy, "xDistance": random.uniform(-6, 6),
            "speed": speed or random.randint(900, 1600), "gestureSourceType": "touch",
            "preventFling": True,
        }, timeout=30)

    def insert_text(self, text: str) -> None:
        self.cdp.send("Input.insertText", {"text": text})

    def block_images(self, on: bool) -> None:
        pats = ["*.jpg*", "*.jpeg*", "*.png*", "*.webp*", "*.gif*", "*/common/?src=*", "*type=f*"] if on else []
        try:
            self.cdp.send("Network.setBlockedURLs", {"urls": pats})
        except Exception:
            try:
                self.cdp.send("Network.setBlockedURLs",
                              {"urlPatterns": [{"urlPattern": p, "block": True} for p in pats]})
            except Exception as e:
                log(f"    ... 이미지 차단 설정 실패(무시): {e}")

    def close(self) -> None:
        if self.cdp:
            self.cdp.close()
        self.unforward()


def mb(n: int) -> str:
    return f"{n / 1048576:.1f}MB"


# ══════════════════════════════════════════════════════════════════════════
# 화면 캡처 (--trace): ADB 로 단계별 실제 화면을 남긴다
# ══════════════════════════════════════════════════════════════════════════
class Tracer:
    def __init__(self, serial: str, enabled: bool):
        self.serial, self.enabled, self.n = serial, enabled, 0
        self.dir = SHOT_DIR / f"{time.strftime('%m%d_%H%M%S')}_{serial}"

    def shot(self, tag: str) -> None:
        if not self.enabled:
            return
        try:
            import device_stats as dstat
            img = dstat.screenshot_jpeg_b64(self.serial, width=540, quality=65)
            if img:
                self.dir.mkdir(parents=True, exist_ok=True)
                self.n += 1
                (self.dir / f"{self.n:02d}_{tag}.jpg").write_bytes(base64.b64decode(img))
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════
# 크롬 실행 / 초기화
# ══════════════════════════════════════════════════════════════════════════
def launch_chrome(d: u2.Device, serial: str) -> Chrome:
    try:
        from device_screen import ensure_screen_ready
        ensure_screen_ready(serial, log=log)
    except Exception as e:
        log(f"[!] 화면 준비 스킵: {e}")
    log("[*] Chrome 실행")
    adb(serial, "shell", "am", "force-stop", CHROME_PKG)
    time.sleep(0.4)
    adb(serial, "shell", "am", "start", "-a", "android.intent.action.MAIN",
        "-c", "android.intent.category.LAUNCHER", "-n", v1.CHROME_MAIN)
    ch = Chrome(serial)
    ch.forward()
    end = time.time() + 40
    fre = False
    while time.time() < end:
        # 초기화 직후라면 온보딩(FRE) 이 뜬다 — V1 과 동일하게 처리
        if ds._safe_exists(d, resourceId=f"{CHROME_PKG}:id/signin_fre_dismiss_button") \
                or ds._safe_exists(d, textContains="계정 없이"):
            fre = True
        v1.dismiss_chrome_noise(d, rounds=2)
        pages = ch.pages()
        if pages:
            ch.attach(pages[0])
            # 남은 다른 탭 정리
            for t in pages[1:]:
                try:
                    ch._http(f"/json/close/{t['id']}")
                except Exception:
                    pass
            log(f"[+] DevTools 연결 (port {ch.port}, 탭 {pages[0].get('url', '')[:40] or '-'}{', 온보딩 처리' if fre else ''})")
            return ch
        time.sleep(0.5)
    ch.close()
    raise RuntimeError("DevTools 탭을 찾지 못했습니다 (USB 디버깅/크롬 상태 확인)")


def settle_chrome_ui(d: u2.Device, secs: float = 5.0) -> None:
    """크롬 초기화 직후 늦게 뜨는 안내창(광고 개인정보 보호 등)을 조용해질 때까지 닫는다.
    한 번 확인하고 넘어가면 몇 초 뒤 뜬 창이 검색 도중 페이지를 덮는다."""
    end = time.time() + secs
    quiet = 0
    while time.time() < end and quiet < 3:
        hit = False
        for rid in v1.CHROME_FRE_IDS:
            if ds._safe_click(d, resourceId=f"{CHROME_PKG}:id/{rid}"):
                log(f"[*] Chrome 안내창 닫음: {rid}")
                hit = True
                time.sleep(0.8)
                break
        if not hit and (ds._safe_exists(d, textContains="광고 개인 정보") or ds._safe_exists(d, textContains="광고 개인정보")):
            for t in ("확인", "알겠습니다", "Got it"):
                if ds._safe_click(d, text=t):
                    log(f"[*] Chrome 광고 개인정보 안내: '{t}'")
                    hit = True
                    time.sleep(0.8)
                    break
        quiet = 0 if hit else quiet + 1
        time.sleep(0.4)


def reset_identity(ch: Chrome, *, final: bool) -> None:
    """쿠키·스토리지·세션 삭제 (HTTP 캐시는 유지). final=True 면 새 빈 탭으로 교체."""
    c = ch.cdp
    try:
        c.send("Network.clearBrowserCookies", {})
    except Exception as e:
        log(f"    ... 쿠키 삭제 실패: {e}")
    origins = set(ch.origins) | {
        "https://m.naver.com", "https://www.naver.com", "https://naver.com",
        "https://m.search.naver.com", "https://search.naver.com",
        "https://m.blog.naver.com", "https://blog.naver.com",
        "https://m.cafe.naver.com", "https://cafe.naver.com", "https://in.naver.com",
        "https://nid.naver.com", "https://m.place.naver.com",
    }
    for o in sorted(origins):
        try:
            c.send("Storage.clearDataForOrigin", {"origin": o, "storageTypes": "all"}, timeout=10)
        except Exception:
            pass
    try:
        left = len((c.send("Network.getAllCookies", {}) or {}).get("cookies", []))
    except Exception:
        left = -1
    if final:
        # 크롬 재시작 시 복원되는 탭이 네이버를 다시 불러오지 않도록 먼저 빈 페이지로
        try:
            c.send("Page.navigate", {"url": "about:blank"}, timeout=8)
            time.sleep(0.5)
        except Exception:
            pass
        # 세션스토리지·뒤로가기 기록까지 끊기 위해 새 빈 탭을 만들고 기존 탭을 닫는다
        try:
            nt = ch._http("/json/new?about:blank", method="PUT")
        except Exception:
            nt = None
        if isinstance(nt, dict) and nt.get("id"):
            old = ch.target_id
            try:
                ch._http(f"/json/close/{old}")
            except Exception:
                pass
    log(f"[*] 식별정보 초기화: 쿠키·스토리지 삭제 (origin {len(origins)}개, 남은 쿠키 {left}) · HTTP 캐시 유지")


def full_reset(serial: str) -> None:
    log("[*] Chrome 전체 초기화 (pm clear) — 주기적 완전 초기화")
    adb(serial, "shell", "am", "force-stop", CHROME_PKG)
    adb(serial, "shell", "pm", "clear", CHROME_PKG)
    time.sleep(2.0)


# ══════════════════════════════════════════════════════════════════════════
# 검색
# ══════════════════════════════════════════════════════════════════════════
JS_RECT = """(sel => { const e = document.querySelector(sel); if (!e) return null;
  const r = e.getBoundingClientRect(); return {x: r.left + r.width/2, y: r.top + r.height/2, w: r.width, h: r.height,
  top: r.top, vis: r.width > 0 && r.height > 0}; })(%s)"""


def rect_of(ch: Chrome, sel: str) -> Optional[dict]:
    try:
        return ch.cdp.eval(JS_RECT % json.dumps(sel))
    except Exception:
        return None


def focused_id(ch: Chrome) -> str:
    try:
        return ch.cdp.eval("(document.activeElement && (document.activeElement.id || document.activeElement.name)) || ''") or ""
    except Exception:
        return ""


def query_of(url: str) -> str:
    m = re.search(r"[?&]query=([^&#]*)", url or "")
    return unquote(m.group(1).replace("+", " ")).strip() if m else ""


def same_query(a: str, b: str) -> bool:
    return a.replace(" ", "").lower() == b.replace(" ", "").lower()


def type_and_submit(ch: Chrome, d: u2.Device, keyword: str, input_ids: tuple) -> None:
    fid = focused_id(ch)
    if fid not in input_ids:
        raise RuntimeError(f"검색창 포커스 실패(focus={fid or '-'})")
    # 기존 입력값 선택 → 새 키워드로 교체 (붙여넣기와 동일한 IME 확정 입력)
    ch.cdp.eval("(() => { const e = document.activeElement; if (e && e.select) e.select(); })()")
    time.sleep(random.uniform(0.15, 0.35))
    ch.insert_text(keyword)
    time.sleep(random.uniform(0.3, 0.7))
    val = ch.cdp.eval("(document.activeElement && document.activeElement.value) || ''") or ""
    if not same_query(val, keyword):
        raise RuntimeError(f"검색어 입력 불일치: '{val}'")


def search_from_home(ch: Chrome, d: u2.Device, keyword: str, tr: Tracer) -> None:
    """m.naver.com 검색창(가짜 입력창 탭 → 실제 입력창) 에 입력 후 검색."""
    log(f"[*] 네이버 메인 검색: '{keyword}'")
    # 로딩 중(뼈대 화면)에 누르면 가짜 입력창에 커서만 들어가고 검색 레이어가 안 열린다 → 완전 로딩 대기
    end = time.time() + 12
    while time.time() < end:
        try:
            if ch.cdp.eval("document.readyState", timeout=4) == "complete":
                break
        except Exception:
            pass
        time.sleep(0.4)
    for attempt in range(3):
        r = None
        for _ in range(20):
            r = rect_of(ch, "#MM_SEARCH_FAKE")
            if r and r.get("vis"):
                break
            time.sleep(0.4)
        if not r or not r.get("vis"):
            raise RuntimeError("메인 검색창(#MM_SEARCH_FAKE) 없음")
        time.sleep(random.uniform(0.8, 1.6))
        ch.tap(r["x"] + random.uniform(-r["w"] * 0.25, r["w"] * 0.25), r["y"] + random.uniform(-4, 4))
        for _ in range(15):
            if focused_id(ch) == "query":
                break
            time.sleep(0.2)
        if focused_id(ch) == "query":
            break
        log(f"    ... 검색 레이어 미전환(focus={focused_id(ch) or '-'}) → 키보드 닫고 재시도 {attempt + 1}/3")
        tr.shot(f"main_retry{attempt + 1}")
        d.press("back")          # 키보드 닫기 (페이지 이동 아님)
        time.sleep(random.uniform(1.5, 2.5))
        if "m.naver.com" not in ch.url():
            ch.cdp.send("Page.navigate", {"url": NAVER_HOME_URL, "transitionType": "typed"})
            ch.wait_ready(15)
    type_and_submit(ch, d, keyword, ("query",))
    tr.shot("main_typed")
    ch.block_images(False)            # 검색결과부터는 이미지 정상 로딩
    ch.phase("serp1")
    d.shell("input keyevent 66")       # 실제 키보드 엔터
    is_serp = lambda u: "search.naver.com" in u and same_query(query_of(u), keyword)  # noqa: E731
    cur = ch.wait_url(is_serp, 15)
    if not cur:
        # 크롬 안내창이 페이지를 덮었을 수 있음 → 닫고 다시 확인
        settle_chrome_ui(d, 4)
        cur = ch.wait_url(is_serp, 6)
    if not cur:
        raise RuntimeError(f"검색결과 URL 미확인 ('{keyword}')")
    log(f"[+] 검색결과: {cur[:90]}")
    ch.wait_ready(15)


def search_from_serp(ch: Chrome, d: u2.Device, keyword: str, tr: Tracer) -> None:
    """검색결과 상단 검색창(#nx_query) 으로 재검색."""
    log(f"[*] 검색결과 상단 검색창으로 2차 검색: '{keyword}'")
    for _ in range(12):
        r = rect_of(ch, "#nx_query")
        if r and r.get("vis") and r["top"] >= 0:
            break
        ch.scroll(-random.randint(700, 1100))
        time.sleep(random.uniform(0.3, 0.6))
    else:
        raise RuntimeError("검색결과 상단 검색창(#nx_query) 을 찾지 못함")
    time.sleep(random.uniform(0.4, 1.0))
    ch.tap(r["x"] + random.uniform(-r["w"] * 0.2, r["w"] * 0.2), r["y"] + random.uniform(-3, 3))
    for _ in range(15):
        if focused_id(ch) in ("nx_query", "query"):
            break
        time.sleep(0.2)
    type_and_submit(ch, d, keyword, ("nx_query", "query"))
    tr.shot("serp2_typed")
    ch.phase("serp2")
    d.shell("input keyevent 66")
    cur = ch.wait_url(lambda u: "search.naver.com" in u and same_query(query_of(u), keyword), 15)
    if not cur:
        raise RuntimeError(f"2차 검색결과 URL 미확인 ('{keyword}')")
    log(f"[+] 검색결과: {cur[:90]}")
    ch.wait_ready(15)


# ══════════════════════════════════════════════════════════════════════════
# 검색결과에서 글 고르기 — 링크의 실제 목적지 URL 로 판정
# ══════════════════════════════════════════════════════════════════════════
JS_LINKS = r"""(() => {
  const out = [], seen = new Set(), W = innerWidth;
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href || ''; if (!/^https?:/.test(href)) continue;
    const r = a.getBoundingClientRect();
    if (r.width < 60 || r.height < 14 || r.left < -2 || r.right > W + 2) continue;
    const st = getComputedStyle(a); if (st.visibility === 'hidden' || st.display === 'none') continue;
    const txt = (a.innerText || a.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    const adbox = !!a.closest('[class*="power"],[id*="power"],[class*="_ad_"],[data-ad],[class*="sp_ad"],.ad_section');
    out.push({href, txt: txt.slice(0, 80), top: r.top + scrollY, h: r.height, w: r.width, adbox});
  }
  return out;
})()"""


def classify(href: str) -> str:
    """'article' / 'web' / 'bad'."""
    try:
        u = urlparse(href)
    except Exception:
        return "bad"
    host = (u.hostname or "").lower()
    if not host or any(b in host for b in BAD_HOST_SUBSTR):
        return "bad"
    path = (u.path or "") + ("?" + u.query if u.query else "")
    for h, pat in ARTICLE_RULES:
        if host.endswith(h):
            return "article" if pat.search(path) else "bad"
    if host.endswith("naver.com") or host.endswith("naver.me") or host.endswith("pstatic.net"):
        return "bad"
    return "web"


def article_tier(href: str) -> int:
    """0=블로그·카페·인플루언서·포스트(최우선) 1=뉴스·지식iN 2=외부 웹문서."""
    host = (urlparse(href).hostname or "").lower()
    if any(host.endswith(h) for h in ("blog.naver.com", "cafe.naver.com", "in.naver.com", "post.naver.com")):
        return 0
    if classify(href) == "article":
        return 1
    return 2


def pick_target(ch: Chrome, stats: Optional[dict] = None) -> Optional[dict]:
    """문서 순서상 위쪽의 글 제목 링크. 블로그·카페 우선 → 뉴스·지식iN → 외부 웹문서."""
    links = ch.cdp.eval(JS_LINKS, timeout=12) or []
    by_href: Dict[str, dict] = {}
    cnt = {"links": len(links), "ad": 0, "bad": 0, "short": 0}
    for l in links:
        if l["adbox"]:
            cnt["ad"] += 1
            continue
        k = classify(l["href"])
        if k == "bad":
            cnt["bad"] += 1
            continue
        # 같은 목적지 중 '제목' 링크 = 글자 8자 이상, 높이 70 이하(본문 요약·썸네일 제외)
        if len(l["txt"]) < 8 or l["h"] > 70:
            cnt["short"] += 1
            continue
        key = l["href"].split("#")[0]
        if key not in by_href:
            by_href[key] = dict(l, kind=k, tier=article_tier(l["href"]))
    if stats is not None:
        stats.update(cnt, ok=len(by_href))
    pool = []
    for tier in (0, 1, 2):
        cands = sorted([v for v in by_href.values() if v["tier"] == tier], key=lambda v: v["top"])
        if cands:
            pool = cands[:3] if tier < 2 else cands[:2]
            break
    if not pool:
        return None
    # 상위 글 위주, 가끔 두세 번째 (사람처럼)
    weights = [0.62, 0.26, 0.12][:len(pool)]
    return random.choices(pool, weights=weights, k=1)[0]


JS_LOCATE = r"""((href, txt) => {
  for (const a of document.querySelectorAll('a[href]')) {
    if (a.href !== href) continue;
    const t = (a.innerText || a.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    if (t !== txt) continue;
    const r = a.getBoundingClientRect(); if (r.width < 10 || r.height < 10) continue;
    // 두 줄로 접힌 제목은 전체 사각형에 빈 공간이 생긴다 → 실제 글자 줄(가장 넓은 줄)을 누를 영역으로
    let box = r, best = 0;
    for (const lr of a.getClientRects()) { if (lr.width * lr.height > best && lr.height >= 10) { best = lr.width * lr.height; box = lr; } }
    return {top: r.top, bottom: r.bottom, left: r.left, right: r.right, h: innerHeight, w: innerWidth,
            tl: box.left, tr: box.right, tt: box.top, tb: box.bottom};
  }
  return null;
})(%s, %s)"""

JS_HIT = r"""((x, y, href) => { const e = document.elementFromPoint(x, y); if (!e) return 'none';
  const a = e.closest('a'); if (!a) return 'no-anchor:' + e.tagName; return a.href === href ? 'ok' : 'other:' + a.href.slice(0, 60); })(%s, %s, %s)"""


def bring_into_view(ch: Chrome, tgt: dict) -> Optional[dict]:
    """목표 링크가 화면 30~65% 높이에 오도록 터치 스크롤 (여러 번에 나눠 사람처럼)."""
    for _ in range(40):
        loc = ch.cdp.eval(JS_LOCATE % (json.dumps(tgt["href"]), json.dumps(tgt["txt"])))
        if not loc:
            return None
        h = loc["h"]
        want_lo, want_hi = h * 0.38, h * 0.70   # 상단 고정 막대(약 20%) 아래
        mid = (loc["top"] + loc["bottom"]) / 2
        if want_lo <= mid <= want_hi:
            return loc
        vp = ch.viewport()
        room_up = vp.get("y", 0)
        room_down = max(0, vp.get("dh", 0) - vp.get("y", 0) - h)
        delta = mid - h * random.uniform(0.45, 0.6)
        # 페이지 끝(위/아래)이라 더 못 움직이면, 화면 안에 충분히 보일 때 그 자리에서 누른다
        if (delta < 0 and room_up < 24) or (delta > 0 and room_down < 24):
            if loc["top"] >= h * 0.12 and loc["bottom"] <= h * 0.9:
                return loc
            return None
        step = max(-h * 0.8, min(h * 0.8, delta))
        if abs(delta) > h * 0.8:
            step = (1 if delta > 0 else -1) * h * random.uniform(0.55, 0.8)
        ch.scroll(step)
        time.sleep(random.uniform(0.35, 0.9) if abs(delta) > h else random.uniform(0.2, 0.45))
    return None


def open_article(ch: Chrome, d: u2.Device, label: str, tr: Tracer) -> bool:
    """검색결과에서 허용 글 1개를 정확히 눌러 진입. 금지 목적지는 애초에 누르지 않는다."""
    serp_url = ch.url()
    tried: set = set()
    for attempt in range(4):
        # 재시도 전에 검색결과 페이지에 있는지 확인 (다른 페이지에서 링크를 고르지 않도록)
        if attempt and "search.naver.com" not in ch.url():
            cur = ch.url()
            if classify(cur) != "bad":
                log(f"[+] 페이지 진입(지연 확인): {cur[:90]}")
                ch.wait_ready(15)
                return True
            d.press("back")
            if not ch.wait_url(lambda u: "search.naver.com" in u, 8):
                log(f"[!] {label}: 검색결과로 복귀 실패")
                return False
        tgt = None
        st: dict = {}
        for _ in range(10):
            tgt = pick_target(ch, st)
            if tgt and tgt["href"] not in tried:
                break
            tgt = None
            vp = ch.viewport()
            if vp.get("y", 0) + vp.get("h", 700) >= vp.get("dh", 0) - 50:
                break                                  # 페이지 끝
            ch.scroll(vp.get("h", 700) * random.uniform(0.7, 0.9))   # 아직 로딩 안 된 결과
            time.sleep(0.6)
        if not tgt:
            vp = ch.viewport()
            log(f"[!] {label}: 누를 수 있는 글이 없음 (링크 {st.get('links')}, 광고 {st.get('ad')}, "
                f"금지 {st.get('bad')}, 비제목 {st.get('short')}, 후보 {st.get('ok')}, "
                f"scrollY {int(vp.get('y', 0))}/{vp.get('dh')}, url {ch.url()[:60]})")
            tr.shot(f"{label}_no_target")
            return False
        tried.add(tgt["href"])
        log(f"[*] {label} 목표({tgt['kind']}): '{tgt['txt'][:50]}' → {urlparse(tgt['href']).netloc}")
        loc = bring_into_view(ch, tgt)
        if not loc:
            log("    ... 목표를 화면에 맞추지 못함 → 다른 글")
            continue
        time.sleep(random.uniform(0.5, 1.3))          # 제목 읽는 시간
        hit, x, y = "none", 0.0, 0.0
        for fix in range(3):
            # 위로 스크롤하면 네이버 상단 검색·탭 막대가 다시 내려와 제목을 덮는다 → 매번 새로 측정
            loc = ch.cdp.eval(JS_LOCATE % (json.dumps(tgt["href"]), json.dumps(tgt["txt"]))) or loc
            x = loc["tl"] + (loc["tr"] - loc["tl"]) * random.uniform(0.2, 0.7)
            y = loc["tt"] + (loc["tb"] - loc["tt"]) * random.uniform(0.35, 0.65)
            hit = ch.cdp.eval(JS_HIT % (x, y, json.dumps(tgt["href"])))
            if hit == "ok":
                break
            log(f"    ... 좌표 가림({hit[:50]}) → 살짝 내려 상단 막대 숨기고 재측정 {fix + 1}/2")
            if fix < 2:
                ch.scroll(random.uniform(90, 160))     # 아래로 스크롤 = 상단 고정 막대 숨김
                time.sleep(random.uniform(0.5, 0.8))
        if hit != "ok":
            log(f"    ... 좌표 검증 실패({hit}) → 다른 글")
            continue
        tr.shot(f"{label}_before_tap")
        ch.phase(f"{label}_page")
        ch.tap(x, y)
        cur = ch.wait_url(lambda u: u != serp_url and "search.naver.com" not in u, 12)
        if not cur:
            # 느린 기기: 탭은 먹혔지만 전환이 늦는 경우 — 조금 더 기다린 뒤 다시 확인
            # (여기서 다른 글을 고르면 이미 열린 글 안의 링크를 또 누르게 된다)
            cur = ch.wait_url(lambda u: u != serp_url and "search.naver.com" not in u, 10)
            if cur:
                log("    ... 전환 지연(느린 로딩) — 진입 확인")
        if not cur:
            # 새 탭으로 열린 경우
            for t in ch.pages():
                if t["id"] != ch.target_id and classify(t.get("url", "")) != "bad":
                    ch.attach(t)
                    cur = t.get("url", "")
                    break
        if not cur:
            log("    ... 전환 없음 → 다른 글")
            continue
        kind = classify(cur)
        if kind == "bad":
            # 이론상 발생하지 않아야 함 — 발생하면 즉시 뒤로
            log(f"[!] 예상 밖 목적지({cur[:60]}) → 뒤로")
            d.press("back")
            ch.wait_url(lambda u: "search.naver.com" in u, 8)
            continue
        log(f"[+] 페이지 진입: {cur[:90]}")
        ch.wait_ready(15)
        return True
    return False


def dwell(ch: Chrome, d: u2.Device, lo: float, hi: float, max_scrolls: int) -> None:
    """읽는 속도의 체류: 스크롤 횟수를 제한해 lazy 이미지 과다 로딩을 줄인다."""
    secs = random.uniform(min(lo, hi), max(lo, hi))
    n = random.randint(max(2, max_scrolls - 2), max_scrolls)
    log(f"[*] 체류 {secs:.1f}s (스크롤 {n}회)")
    end = time.time() + secs
    gaps = sorted(random.uniform(0.8, secs - 1.0) for _ in range(n)) if secs > 3 else []
    t0 = time.time()
    for g in gaps:
        wait = t0 + g - time.time()
        if wait > 0:
            time.sleep(wait)
        if time.time() >= end:
            break
        try:
            at_top = ch.viewport().get("y", 0) < 250
        except Exception:
            at_top = True
        # 맨 위 근처에서 위로 되돌리는 스와이프는 '당겨서 새로고침'(재로딩=데이터 낭비) 위험 → 금지
        up = at_top or random.random() < 0.85
        v1.human_swipe(d, direction="up" if up else "down", mode="dwell")
    rem = end - time.time()
    if rem > 0:
        time.sleep(rem)
    log("[+] 체류 완료")


def back_to_serp(ch: Chrome, d: u2.Device, keyword: str) -> bool:
    for _ in range(3):
        d.press("back")
        cur = ch.wait_url(lambda u: "search.naver.com" in u, 8)
        if cur:
            if same_query(query_of(cur), keyword):
                ch.wait_ready(10)
                return True
    return False


# ══════════════════════════════════════════════════════════════════════════
# 1회 실행
# ══════════════════════════════════════════════════════════════════════════
def run_once(d: u2.Device, args, tr: Tracer) -> tuple:
    log("[*] 로테이션 시작 전 데이터·IP 확인")
    if not ds.ensure_network(args.serial, timeout=args.recover_secs):
        log("[!] 연결 미확인 → Chrome 진입 생략")
        return False, False, None

    ch = launch_chrome(d, args.serial)
    try:
        # 이전 실행 잔여 쿠키가 디스크에서 되살아났을 수 있으므로 시작 전에도 한 번 비운다
        reset_identity(ch, final=False)
        ch.origins.clear()
        ch.phase("main")
        if args.block_main_images:
            ch.block_images(True)
        ch.cdp.send("Page.navigate", {"url": NAVER_HOME_URL, "transitionType": "typed"})
        if not ch.wait_url(lambda u: "m.naver.com" in u, 20):
            log("[!] 네이버 메인 로딩 실패")
            return False, True, ch
        ch.wait_ready(20)
        time.sleep(random.uniform(1.0, 2.2))
        settle_chrome_ui(d, 6)   # 초기화 직후 늦게 뜨는 크롬 안내창까지 정리
        tr.shot("main")

        # 1차
        search_from_home(ch, d, args.keyword, tr)
        time.sleep(random.uniform(1.0, 2.0))
        tr.shot("serp1")
        if not open_article(ch, d, "1차", tr):
            return False, True, ch
        tr.shot("page1")
        dwell(ch, d, args.dwell_min, args.dwell_max, args.dwell_scrolls)

        # 2차 — 뒤로 가서 검색결과 상단 검색창으로
        ch.phase("back1")
        if not back_to_serp(ch, d, args.keyword):
            log("[!] 1차 검색결과로 복귀 실패 → 메인에서 2차 검색")
            ch.cdp.send("Page.navigate", {"url": NAVER_HOME_URL, "transitionType": "typed"})
            ch.wait_url(lambda u: "m.naver.com" in u, 15)
            ch.wait_ready(15)
            search_from_home(ch, d, args.keyword2, tr)
        else:
            search_from_serp(ch, d, args.keyword2, tr)
        time.sleep(random.uniform(1.0, 2.0))
        tr.shot("serp2")
        if not open_article(ch, d, "2차", tr):
            return False, True, ch
        tr.shot("page2")
        dwell(ch, d, args.dwell_min, args.dwell_max, args.dwell_scrolls)
        return True, True, ch
    except Exception as e:
        log(f"[!] 실행 오류: {e}")
        tr.shot("error")
        return False, True, ch


def main() -> None:
    ap = argparse.ArgumentParser(description="[함많찾을 V2] CDP 정밀 클릭 + 데이터 절감")
    ap.add_argument("keyword")
    ap.add_argument("--keyword2", default="")
    ap.add_argument("--place-name", default="")
    ap.add_argument("--serial", required=True)
    ap.add_argument("--dwell-min", type=float, default=15.0)
    ap.add_argument("--dwell-max", type=float, default=20.0)
    ap.add_argument("--dwell-scrolls", type=int, default=5, help="체류 중 최대 스크롤 횟수")
    ap.add_argument("--rotations", type=int, default=1)
    ap.add_argument("--no-block-main-images", dest="block_main_images", action="store_false")
    ap.add_argument("--full-reset", action="store_true", help="이번 실행 후 pm clear (V1 방식)")
    ap.add_argument("--no-data-toggle", action="store_true")
    ap.add_argument("--off-secs", type=float, default=4.0)
    ap.add_argument("--recover-secs", type=float, default=25.0)
    ap.add_argument("--trace", action="store_true", help="단계별 화면 캡처 저장 (shots_v2/)")
    args = ap.parse_args()
    args.keyword2 = (args.keyword2 or args.place_name or args.keyword).strip()

    d = ds.connect(args.serial)
    ds.setup_popup_watchers(d)
    log(f"[*] 함많찾을 V2 키워드: '{args.keyword}' / 2차 '{args.keyword2}'")
    log(f"[*] 체류 {args.dwell_min:.0f}~{args.dwell_max:.0f}s · 스크롤≤{args.dwell_scrolls} · 메인 이미지 차단={args.block_main_images}")
    log("[*] 클릭 정책: DevTools 로 링크 목적지 확인 → 글(블로그/카페/인플루언서/뉴스)만, 광고·플레이스·지도·연관검색 원천 제외")

    results = []
    try:
        for i in range(1, max(1, args.rotations) + 1):
            ds.begin_cycle_timer()
            t0 = time.time()
            log(f"\n===== 로테이션 {i} 시작 (함많찾을 V2) =====")
            tr = Tracer(args.serial, args.trace)
            ok, opened, ch = run_once(d, args, tr)
            log(f"[*] 로테이션 {i} (작업 성공={ok}, 진입={opened})")
            if ch:
                try:
                    ch.phase("cleanup")
                    ch.phase("done")
                    parts = " · ".join(f"{k} {mb(v)}" for k, v in ch.phase_bytes.items() if v > 0)
                    log(f"[*] 크롬 수신 합계 {mb(sum(ch.phase_bytes.values()))} ({parts})")
                    reset_identity(ch, final=True)
                except Exception as e:
                    log(f"[!] 초기화 오류: {e} → pm clear 로 대체")
                    args.full_reset = True
                finally:
                    ch.close()
            time.sleep(1.5)    # 쿠키 삭제가 디스크에 반영될 시간
            adb(args.serial, "shell", "am", "force-stop", CHROME_PKG)
            if args.full_reset and opened:
                full_reset(args.serial)
            if not args.no_data_toggle:
                ds.toggle_mobile_data(args.serial, off_secs=args.off_secs, recover_timeout=args.recover_secs)
            results.append(ok)
            log(f"===== 로테이션 {i} 완료 — 소요 {time.time() - t0:.1f}s =====")
            ds.end_cycle_timer()
    finally:
        log("\n========== 최종 요약 (함많찾을 V2) ==========")
        log(f"[+] 작업 성공: {sum(results)}/{len(results)} 로테이션")
        try:
            d.watcher.stop()
        except Exception:
            pass
    if not results or not all(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
