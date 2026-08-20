# -*- coding: utf-8 -*-
"""CDP(Chrome DevTools Protocol)로 아웃룩 OAuth 토큰을 무인 발급.

Mac 의 Chrome 을 --remote-debugging-port 로 띄우고, DOM 조작(Runtime.evaluate)으로
로그인 폼을 채워 authorize 흐름을 완료 → 리다이렉트 URL 의 code 를 잡아
서버 API(exchange)로 refresh_token 을 발급·저장한다. 좌표 탭이 아니라 실제 DOM
조작이라 안정적이다.

사용:
  python3 cdp_outlook_token.py <account_id> <mail_email> <mail_password> [client_id]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request

import websocket  # websocket-client

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEBUG_PORT = 9222
DEFAULT_CLIENT = "dbc8e03a-b00c-46bd-ae65-b683e7707cb0"
REDIRECT = "https://login.live.com/oauth20_desktop.srf"
API = os.environ.get("CAMPAIGN_API", "http://127.0.0.1:8080")
USER_DIR = "/tmp/cdp_chrome_profile"


_AUTH_URL = [""]


def log(m):
    print(f"[cdp] {m}", flush=True)


def authorize_url(client_id):
    import urllib.parse
    q = urllib.parse.urlencode({
        "client_id": client_id, "scope": "wl.imap wl.offline_access",
        "response_type": "code", "redirect_uri": REDIRECT,
    })
    return f"https://login.live.com/oauth20_authorize.srf?{q}"


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, max_size=None)
        self._id = 0

    def send(self, method, params=None, timeout=20):
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

    def eval(self, expr, timeout=20):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True},
                      timeout=timeout)
        return r.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def launch_chrome():
    subprocess.run(["pkill", "-f", f"remote-debugging-port={DEBUG_PORT}"],
                   capture_output=True)
    time.sleep(1)
    subprocess.Popen(
        [CHROME, f"--remote-debugging-port={DEBUG_PORT}",
         "--remote-allow-origins=*",
         f"--user-data-dir={USER_DIR}", "--no-first-run", "--no-default-browser-check",
         "--disable-features=Translate", "--headless=new", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=2):
                return True
        except Exception:
            time.sleep(0.5)
    return False


def new_tab():
    # 최신 Chrome: /json/new 는 PUT. 실패하면 기존 page 타깃을 재사용.
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{DEBUG_PORT}/json/new", method="PUT")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)
    except Exception:
        pass
    with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=5) as r:
        targets = json.load(r)
    for t in targets:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return t
    raise RuntimeError("no page target")


def current_url(cdp):
    return cdp.eval("location.href") or ""


_JS_SETVAL = r"""
function setVal(el, val){
  var proto = el.tagName==='TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  var setter = Object.getOwnPropertyDescriptor(proto,'value').set;
  setter.call(el, val);
  el.dispatchEvent(new Event('input',{bubbles:true}));
  el.dispatchEvent(new Event('change',{bubbles:true}));
}
"""

def _fill_email(cdp, email):
    return cdp.eval(_JS_SETVAL + r"""(function(){
      var e=document.querySelector('input[type=email]')||document.querySelector('input[name=loginfmt]')||document.querySelector('#i0116')||document.querySelector('input[type=text]:not([type=hidden])');
      if(!e) return 'no_field';
      setVal(e, %s);
      var n=document.querySelector('#idSIButton9')||document.querySelector('input[type=submit]')||document.querySelector('button[type=submit]');
      if(n){n.click();} else {e.form&&e.form.submit();}
      return 'ok';
    })()""" % json.dumps(email))

def _fill_pw(cdp, password):
    return cdp.eval(_JS_SETVAL + r"""(function(){
      var p=document.querySelector('input[type=password]')||document.querySelector('#i0118');
      if(!p) return 'no_field';
      setVal(p, %s);
      var n=document.querySelector('#idSIButton9')||document.querySelector('input[type=submit]')||document.querySelector('button[type=submit]');
      if(n){n.click();} else {p.form&&p.form.submit();}
      return 'ok';
    })()""" % json.dumps(password))

def _click_positive(cdp):
    return cdp.eval(r"""(function(){
      var ids=['#idSIButton9','#acceptButton','#idBtn_Accept','#declineButton'];
      // 수락/예/다음 우선
      var pos=['#idSIButton9','#acceptButton','#idBtn_Accept'];
      for(var i=0;i<pos.length;i++){var b=document.querySelector(pos[i]); if(b){b.click(); return pos[i];}}
      var btns=[].slice.call(document.querySelectorAll('button,input[type=submit],a[role=button]'));
      for(var j=0;j<btns.length;j++){var t=(btns[j].value||btns[j].innerText||'').trim();
        if(['예','수락','다음','계속','확인','Yes','Accept','Continue','Next'].indexOf(t)>=0){btns[j].click(); return t;}}
      return '';
    })()""")

def fill_login(cdp, email, password) -> str:
    """authorize 흐름을 DOM 조작으로 진행. Returns 최종 URL (code 포함 기대)."""
    deadline = time.time() + 120
    bypass_count = 0
    while time.time() < deadline:
        url = current_url(cdp)
        if REDIRECT.split("//")[1] in url and "code=" in url:
            return url
        # 복구메일/보안정보 넛지 → authorize 재진입으로 우회 (세션은 이미 인증됨)
        if ("proofs/Add" in url or "account.live.com/proofs" in url
                or "내 계정을 보호" in (cdp.eval("document.body?document.body.innerText:''") or "")):
            bypass_count += 1
            if bypass_count > 4:
                return url
            log(f"보안정보 넛지 우회({bypass_count}) → authorize 재진입")
            cdp.send("Page.navigate", {"url": _AUTH_URL[0]})
            time.sleep(5)
            continue
        try:
            state = cdp.eval(r"""(function(){
                var b = document.body ? document.body.innerText : '';
                return JSON.stringify({
                  b: b.slice(0,150),
                  hasEmail: !!(document.querySelector('input[type=email]')||document.querySelector('input[name=loginfmt]')||document.querySelector('#i0116')),
                  hasPw: !!(document.querySelector('input[type=password]')||document.querySelector('#i0118')),
                  err: (b.indexOf('유효한')>=0||b.indexOf('찾을 수 없')>=0||b.indexOf('올바르지')>=0)
                });
              })()""")
            st = json.loads(state or "{}")
        except Exception:
            st = {}
        if st.get("hasPw"):
            r = _fill_pw(cdp, password)
            log(f"pw fill: {r}")
            pw_done = True
            time.sleep(5)
        elif st.get("hasEmail"):
            r = _fill_email(cdp, email)
            log(f"email fill: {r}")
            email_done = True
            time.sleep(4)
        else:
            r = _click_positive(cdp)
            if r:
                log(f"click: {r}")
            time.sleep(2.5)
    return current_url(cdp)


def main():
    if len(sys.argv) < 4:
        print("usage: python3 cdp_outlook_token.py <account_id> <mail_email> <mail_password> [client_id]")
        sys.exit(1)
    account_id = sys.argv[1]
    email, password = sys.argv[2], sys.argv[3]
    client_id = sys.argv[4] if len(sys.argv) > 4 else DEFAULT_CLIENT

    if not launch_chrome():
        log("[!] Chrome 디버그 포트 기동 실패")
        sys.exit(1)
    log("Chrome(headless) 기동")
    tab = new_tab()
    cdp = CDP(tab["webSocketDebuggerUrl"])
    try:
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        _AUTH_URL[0] = authorize_url(client_id)
        cdp.send("Page.navigate", {"url": _AUTH_URL[0]})
        time.sleep(4)
        final_url = fill_login(cdp, email, password)
        log(f"최종 URL: {final_url[:80]}...")
        m = re.search(r"[?&]code=([^&\s]+)", final_url)
        if not m:
            log(f"[!] code 미획득. 현재 URL: {final_url[:120]}")
            body = cdp.eval("document.body ? document.body.innerText.slice(0,300) : ''")
            log(f"화면: {body!r}")
            sys.exit(2)
        code = m.group(1)
        log(f"authorization code 획득: {code[:16]}...")
    finally:
        cdp.close()

    # 서버로 교환·저장
    data = json.dumps({"client_id": client_id, "code": code, "redirect_uri": REDIRECT}).encode()
    req = urllib.request.Request(
        f"{API}/api/accounts/kakao/{account_id}/oauth-code",
        data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=40) as r:
        result = json.load(r)
    log(f"서버 교환 결과: {result}")
    sys.exit(0 if result.get("ok") else 3)


if __name__ == "__main__":
    main()
