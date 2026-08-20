# -*- coding: utf-8 -*-
"""카카오 인증번호 자동 수신 — 메일함에서 6자리 코드를 읽어온다.

카카오 추가 인증번호는 계정에 등록된 메일로만 오고 자동 수신이 어렵다.
메일 종류별로 프로그램 접근 방식이 다르다:

  아웃룩(outlook.*)  : Microsoft 가 basic auth(IMAP/POP)를 껐다.
                       Microsoft Graph API + OAuth 리프레시 토큰만 가능.
  네이버(naver.com)  : 계정별로 웹메일에서 IMAP 을 켜야 하고,
                       카카오 비번이 아니라 '메일 비번'이 필요하다.

토큰/자격이 없으면 read_code() 는 None 을 돌려주고, 호출부(kakao_warmup)는
사람이 코드를 넣는 반자동 모드로 자연스럽게 떨어진다.

리프레시 토큰 위치(우선순위):
  1) 환경변수 MS_REFRESH_TOKEN / MS_CLIENT_ID (+ MS_CLIENT_SECRET, MS_TENANT)
  2) 프로젝트 루트의 ms_token.json  {"refresh_token","client_id",...}
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from typing import List, Optional, Tuple

ROOT = os.path.dirname(os.path.abspath(__file__))

# 카카오 인증 메일에서 6자리(간혹 4~8자리) 코드를 뽑는다
_CODE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
_KAKAO_HINTS = ("카카오", "kakao", "인증", "verification", "인증번호", "passcode")


def extract_code(text: str) -> Optional[str]:
    """메일 본문/제목에서 인증번호로 보이는 숫자를 뽑는다."""
    if not text:
        return None
    # 카카오 관련 문구 근처의 6자리를 우선
    for m in _CODE_RE.finditer(text):
        window = text[max(0, m.start() - 60): m.end() + 20].lower()
        if any(h.lower() in window for h in _KAKAO_HINTS):
            return m.group(1)
    m = _CODE_RE.search(text)
    return m.group(1) if m else None


# ── Microsoft Graph (아웃룩) ───────────────────────────────────────────────
def _load_ms_config() -> Optional[dict]:
    cfg = {
        "refresh_token": os.environ.get("MS_REFRESH_TOKEN", ""),
        "client_id": os.environ.get("MS_CLIENT_ID", ""),
        "client_secret": os.environ.get("MS_CLIENT_SECRET", ""),
        "tenant": os.environ.get("MS_TENANT", "consumers"),
    }
    path = os.path.join(ROOT, "ms_token.json")
    if os.path.isfile(path):
        try:
            data = json.load(open(path, encoding="utf-8"))
            for k in ("refresh_token", "client_id", "client_secret", "tenant"):
                cfg[k] = cfg[k] or data.get(k, "")
        except Exception:
            pass
    if cfg["refresh_token"] and cfg["client_id"]:
        cfg["tenant"] = cfg["tenant"] or "consumers"
        return cfg
    return None


def _ms_access_token(cfg: dict) -> Optional[str]:
    url = f"https://login.microsoftonline.com/{cfg['tenant']}/oauth2/v2.0/token"
    body = {
        "client_id": cfg["client_id"],
        "grant_type": "refresh_token",
        "refresh_token": cfg["refresh_token"],
        "scope": "https://graph.microsoft.com/Mail.Read offline_access",
    }
    if cfg.get("client_secret"):
        body["client_secret"] = cfg["client_secret"]
    data = urllib.parse.urlencode(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r).get("access_token")
    except Exception as e:
        print(f"[mail] MS 토큰 갱신 실패: {e}", flush=True)
        return None


def _graph_recent_messages(access_token: str, mailbox: str, top: int = 5) -> List[str]:
    """mailbox 사서함의 최근 메일 제목+본문 미리보기 목록."""
    base = "https://graph.microsoft.com/v1.0"
    # 공유 사서함이 아니라 토큰 소유자 사서함이면 /me 사용
    who = "me"
    url = (
        f"{base}/{who}/messages?$top={top}"
        f"&$select=subject,bodyPreview,receivedDateTime&$orderby=receivedDateTime desc"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            items = json.load(r).get("value", [])
    except Exception as e:
        print(f"[mail] Graph 조회 실패: {e}", flush=True)
        return []
    return [f"{it.get('subject','')}\n{it.get('bodyPreview','')}" for it in items]


# ── 네이버 IMAP ────────────────────────────────────────────────────────────
def _naver_recent_messages(email: str, mail_password: str, top: int = 5) -> List[str]:
    import imaplib
    import email as emaillib

    user = email.split("@")[0]
    try:
        m = imaplib.IMAP4_SSL("imap.naver.com", 993)
        m.login(user, mail_password)
        m.select("INBOX")
        _, ids = m.search(None, "ALL")
        out: List[str] = []
        for msg_id in ids[0].split()[-top:]:
            _, data = m.fetch(msg_id, "(RFC822)")
            msg = emaillib.message_from_bytes(data[0][1])
            subject = str(emaillib.header.make_header(emaillib.header.decode_header(msg.get("Subject", ""))))
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body += part.get_payload(decode=True).decode(errors="replace")
            else:
                body = msg.get_payload(decode=True).decode(errors="replace")
            out.append(f"{subject}\n{body}")
        m.logout()
        return out
    except Exception as e:
        print(f"[mail] 네이버 IMAP 실패: {e}", flush=True)
        return []


# ── 공개 API ───────────────────────────────────────────────────────────────
def available_for(email: str, mail_password: str = "") -> Tuple[bool, str]:
    """이 계정의 코드를 자동으로 읽을 수 있는지. (가능여부, 방식)."""
    domain = email.split("@")[-1].lower()
    if "naver" in domain:
        return (bool(mail_password), "naver-imap")
    # outlook 계열은 Graph 토큰이 있어야
    return (bool(_load_ms_config()), "ms-graph")


def read_code(
    email: str,
    *,
    mail_password: str = "",
    since_ts: float = 0.0,
    timeout: float = 90.0,
    poll: float = 5.0,
) -> Optional[str]:
    """인증 메일이 도착할 때까지 폴링하며 코드를 읽어 반환. 실패 시 None.

    since_ts 는 참고용(현재 구현은 최근 메일 상위 N개를 훑는다).
    """
    domain = email.split("@")[-1].lower()
    deadline = time.time() + timeout

    if "naver" in domain:
        if not mail_password:
            return None
        while time.time() < deadline:
            for text in _naver_recent_messages(email, mail_password):
                code = extract_code(text)
                if code:
                    return code
            time.sleep(poll)
        return None

    # outlook 계열 → Graph
    cfg = _load_ms_config()
    if not cfg:
        return None
    token = _ms_access_token(cfg)
    if not token:
        return None
    while time.time() < deadline:
        for text in _graph_recent_messages(token, email):
            code = extract_code(text)
            if code:
                return code
        time.sleep(poll)
    return None


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python3 mail_reader.py <email> [mail_password]")
        sys.exit(1)
    email = sys.argv[1]
    pw = sys.argv[2] if len(sys.argv) > 2 else ""
    ok, method = available_for(email, pw)
    print(f"자동 수신 가능={ok} 방식={method}")
    if ok:
        print("코드:", read_code(email, mail_password=pw, timeout=30))
