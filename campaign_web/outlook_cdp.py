# -*- coding: utf-8 -*-
"""서버 사이드 아웃룩 코드 리더 — CDP + 계정별 영구 프로필 (OAuth 미사용).

핵심(사용자 확인): '내 계정을 보호하세요'(proofs/Add) 벽은 OAuth 로 새 앱에
메일 접근 권한을 줄 때만 확정적으로 뜬다. plain 웹 로그인(outlook.live.com)은
그 벽이 없다. 그래서 여기서는 OAuth 를 쓰지 않고, 서버의 Chrome 을 CDP 로 몰아
plain 웹 로그인 → 받은편지함 DOM 에서 코드를 텍스트로 읽는다.

트러스트 축적: 계정마다 전용 영구 프로필(--user-data-dir) + '로그인 유지=예'
→ MS 가 그 기기를 신뢰 → 이후 로그인 깨끗. (낯선 기기/IP 리스크 화면은
확률적으로 뜰 수 있고, 그 경우 실패로 처리해 상위에서 재시도/반자동 폴백.)

CDP 는 DOM 을 직접 보므로 좌표 탭·OCR 이 필요 없다.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request

try:
    import websocket  # websocket-client
except ImportError:
    websocket = None

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
]
PROFILE_ROOT = os.environ.get("OUTLOOK_PROFILE_ROOT", "/tmp/outlook_profiles")
MAIL_URL = "https://outlook.live.com/mail/0/inbox"


def _chrome() -> str:
    for c in CHROME_CANDIDATES:
        if os.path.isfile(c):
            return c
    return "google-chrome"


def log(m):
    print(f"[outlook-cdp] {m}", flush=True)


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, max_size=None, timeout=30)
        self._id = 0

    def call(self, method, params=None, timeout=30):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(0.5, deadline - time.time()))
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})
        raise TimeoutError(method)

    def ev(self, expr, timeout=30):
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True},
                      timeout=timeout)
        return r.get("result", {}).get("value")

    def navigate(self, url):
        self.call("Page.navigate", {"url": url})

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


class ChromeSession:
    """계정별 영구 프로필로 headless Chrome 를 띄우고 CDP 로 제어."""

    def __init__(self, account_key: str, port: int = 0, headless: bool = True):
        self.account_key = re.sub(r"[^a-zA-Z0-9_.-]", "_", account_key)
        self.profile = os.path.join(PROFILE_ROOT, self.account_key)
        os.makedirs(self.profile, exist_ok=True)
        # 포트: 프로필별 고정(동시 실행 충돌 방지) — account_key 해시 기반
        self.port = port or (9300 + (abs(hash(self.account_key)) % 400))
        self.headless = headless
        self.proc = None
        self.cdp = None

    def start(self) -> bool:
        subprocess.run(["pkill", "-f", f"remote-debugging-port={self.port}"], capture_output=True)
        time.sleep(1)
        args = [
            _chrome(), f"--remote-debugging-port={self.port}", "--remote-allow-origins=*",
            f"--user-data-dir={self.profile}", "--no-first-run", "--no-default-browser-check",
            "--disable-features=Translate,AutomationControlled", "--disable-blink-features=AutomationControlled",
            "--window-size=1200,2000", "about:blank",
        ]
        if self.headless:
            args.insert(3, "--headless=new")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=2):
                    break
            except Exception:
                time.sleep(0.5)
        else:
            return False
        # page 타깃 연결
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/json/new", method="PUT")
            tab = json.load(urllib.request.urlopen(req, timeout=5))
        except Exception:
            targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=5))
            tab = next((t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
        if not tab:
            return False
        self.cdp = CDP(tab["webSocketDebuggerUrl"])
        self.cdp.call("Page.enable")
        self.cdp.call("Runtime.enable")
        return True

    def stop(self):
        if self.cdp:
            self.cdp.close()
        subprocess.run(["pkill", "-f", f"remote-debugging-port={self.port}"], capture_output=True)


# ── 웹 로그인 상태머신 (DOM 기반) ──────────────────────────────────────────
def _body(cdp) -> str:
    return cdp.ev("document.body ? document.body.innerText : ''") or ""


def _state(cdp) -> str:
    info = cdp.ev(r"""(function(){
      var b=document.body?document.body.innerText:'';
      var url=location.href;
      return JSON.stringify({
        url:url,
        inbox: (url.indexOf('outlook.live.com/mail')>=0 || url.indexOf('outlook.office.com/mail')>=0)
               && !!(document.querySelector('[role=listbox],[aria-label],div[data-app-section]')) && b.indexOf('암호')<0,
        hasEmail: (function(){var e=document.querySelector('input[type=email]')||document.querySelector('input[name=loginfmt]');return !!(e&&e.offsetParent!==null);})(),
        hasPw: (function(){var p=document.querySelector('input[type=password]');return !!(p&&p.offsetParent!==null);})(),
        stay: b.indexOf('로그인 상태를 유지')>=0 || b.indexOf('Stay signed in')>=0,
        proofs: url.indexOf('proofs')>=0 || b.indexOf('내 계정을 보호')>=0,
        marketing: b.indexOf('무료 계정 만들기')>=0 || b.indexOf('다운로드')>=0 || url.indexOf('microsoft.com')>=0
      });
    })()""")
    try:
        return json.loads(info)
    except Exception:
        return {}


_SETVAL = r"""
function setVal(el,val){var p=window.HTMLInputElement.prototype;var s=Object.getOwnPropertyDescriptor(p,'value').set;
s.call(el,val);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));}
"""


def _click_by_text(cdp, texts) -> str:
    arr = json.dumps(texts)
    return cdp.ev(r"""(function(){
      var want=%s;
      var els=[].slice.call(document.querySelectorAll('a,button,input[type=submit],div[role=button],span'));
      for(var i=0;i<els.length;i++){var t=(els[i].value||els[i].innerText||els[i].getAttribute('aria-label')||'').trim();
        for(var j=0;j<want.length;j++){ if(t===want[j] || t.indexOf(want[j])>=0){ els[i].click(); return t; } }
      }
      return '';
    })()""" % arr)


def ensure_logged_in(cdp, email, password, timeout=100) -> bool:
    cdp.navigate(MAIL_URL)
    time.sleep(6)
    deadline = time.time() + timeout
    last=None
    nonlocal_bypass=[0]
    while time.time() < deadline:
        st = _state(cdp)
        key=(st.get('url','')[:50], st.get('inbox'), st.get('hasEmail'), st.get('hasPw'), st.get('stay'), st.get('marketing'), st.get('proofs'))
        last=key  # (상세 상태 로그는 필요 시 활성화)
        if st.get("inbox"):
            log("받은편지함 진입")
            return True
        if st.get("proofs"):
            # 세션은 이미 인증됨 → 보안정보 넛지를 메일 URL 로 우회 (수동 통과와 동일)
            nonlocal_bypass[0] += 1
            if nonlocal_bypass[0] > 3:
                log("[!] proofs 반복 — 리스크 화면으로 판단, 실패")
                return False
            log(f"proofs 넛지 우회({nonlocal_bypass[0]}) → 메일 URL 재진입")
            cdp.navigate(MAIL_URL)
            time.sleep(6)
            continue
        if st.get("stay"):
            _click_by_text(cdp, ["예", "Yes"]) or cdp.ev("var b=document.querySelector('#idSIButton9');b&&b.click();")
            time.sleep(4)
        elif st.get("hasPw"):
            cdp.ev(_SETVAL + r"""(function(){var p=document.querySelector('input[type=password]');
              if(p){setVal(p,%s);} var n=document.querySelector('#idSIButton9')||document.querySelector('button[type=submit]')||document.querySelector('input[type=submit]');
              if(n)n.click();})()""" % json.dumps(password))
            time.sleep(5)
        elif st.get("hasEmail"):
            cdp.ev(_SETVAL + r"""(function(){var e=document.querySelector('input[type=email]')||document.querySelector('input[name=loginfmt]');
              if(e){setVal(e,%s);} var n=document.querySelector('#idSIButton9')||document.querySelector('button[type=submit]')||document.querySelector('input[type=submit]');
              if(n)n.click();})()""" % json.dumps(email))
            time.sleep(5)
        elif st.get("marketing"):
            clicked = _click_by_text(cdp, ["계속 로그인", "로그인", "Sign in"])
            if not clicked:
                cdp.navigate(MAIL_URL)
                time.sleep(5)
            else:
                time.sleep(4)
        else:
            time.sleep(2.5)
    return bool(_state(cdp).get("inbox"))


# ── 코드 읽기 (DOM innerText → 정규식) ─────────────────────────────────────
_YEARS = {"2023", "2024", "2025", "2026", "2027", "2028"}


def _extract_code(text: str):
    for m in re.finditer(r"(?<!\d)(\d{4,8})(?!\d)", text):
        w = text[max(0, m.start() - 40): m.end() + 20]
        if any(h in w for h in ("인증", "verification", "code")) and m.group(1) not in _YEARS:
            return m.group(1)
    for m in re.finditer(r"(?<!\d)(\d{6,8})(?!\d)", text):
        if m.group(1) not in _YEARS:
            return m.group(1)
    return None


def read_latest_code(cdp, tries: int = 5, gap: float = 8.0) -> str:
    """받은편지함에서 최신 카카오 인증번호 메일을 열어 DOM 텍스트로 코드 추출.
    코드 메일이 아직 안 왔을 수 있어 새로고침하며 재시도."""
    cdp.navigate(MAIL_URL)
    time.sleep(10)  # SPA 메일목록 로드 대기
    for attempt in range(tries):
        if attempt > 0:
            # 새로고침(받은편지함 재진입)
            cdp.navigate(MAIL_URL)
            time.sleep(gap)
        code = _read_once(cdp)
        if code:
            return code
    return ""


def _read_once(cdp) -> str:
    # 최신 카카오 메일 열기: 제목에 인증번호/카카오 포함한 행 클릭
    opened = cdp.ev(r"""(function(){
      var rows=[].slice.call(document.querySelectorAll('[role=option],[role=listitem],div[data-convid],div[aria-label]'));
      // 1순위: '인증번호' 메일(=코드), 상단(최신)부터
      for(var i=0;i<rows.length;i++){var t=(rows[i].innerText||rows[i].getAttribute('aria-label')||'');
        if(t.indexOf('인증번호')>=0){ rows[i].click(); return t.slice(0,60); }
      }
      // 2순위: 카카오 발신
      for(var j=0;j<rows.length;j++){var u=(rows[j].innerText||rows[j].getAttribute('aria-label')||'');
        if(u.indexOf('카카오')>=0){ rows[j].click(); return u.slice(0,60); }
      }
      return '';
    })()""")
    time.sleep(4)
    body = _body(cdp)
    code = _extract_code(body)
    if code:
        return code
    # 목록 미리보기에서 바로 시도
    return _extract_code(cdp.ev("document.body.innerText") or "") or ""


def fetch_code(email, password, *, headless=True, login_timeout=100) -> (str, str):
    """전체 흐름: 세션(프로필) 로그인 보장 → 코드 읽기. Returns (code|'' , detail)."""
    if websocket is None:
        return "", "websocket_client_missing"
    sess = ChromeSession(email, headless=headless)
    if not sess.start():
        return "", "chrome_launch_failed"
    try:
        if not ensure_logged_in(sess.cdp, email, password, timeout=login_timeout):
            return "", "login_failed_or_risk_screen"
        code = read_latest_code(sess.cdp)
        return (code, "ok" if code else "no_code")
    finally:
        sess.stop()


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python3 outlook_cdp.py <email> <password> [--show]")
        sys.exit(1)
    hl = "--show" not in sys.argv
    c, d = fetch_code(sys.argv[1], sys.argv[2], headless=hl)
    print("code:", c, "|", d)
