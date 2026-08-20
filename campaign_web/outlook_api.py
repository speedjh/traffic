# -*- coding: utf-8 -*-
"""서버 사이드 아웃룩 코드 리더 — OAuth 토큰으로 메일함에서 카카오 인증번호를 읽는다.

폰에서 웹 로그인을 자동화하는 대신, 서버가 계정별로 저장한 리프레시 토큰으로
Microsoft 메일함에 접근해 카카오 인증번호를 읽어 워커에 넘긴다.

접근 방식 (대량 아웃룩 계정 표준):
  refresh_token → login.live.com/oauth20_token.srf (scope: wl.imap wl.offline_access)
    → access_token → IMAP XOAUTH2 (outlook.office365.com:993) → INBOX 검색 → 코드 추출

토큰이 없거나 만료면 명확한 사유와 함께 실패를 돌려준다(워커는 반자동/스킵으로).
"""
from __future__ import annotations

import email as emaillib
import imaplib
import json
import re
import time
import urllib.parse
import urllib.request
from email.header import decode_header, make_header
from typing import Optional, Tuple

TOKEN_URL = "https://login.live.com/oauth20_token.srf"
IMAP_HOST = "outlook.office365.com"
IMAP_PORT = 993
IMAP_SCOPE = "wl.imap wl.offline_access"

# 카카오 인증 메일 식별 + 코드 추출
_KAKAO_SENDERS = ("kakao", "카카오")
_CODE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
_YEARS = {"2023", "2024", "2025", "2026", "2027", "2028"}


class OutlookError(Exception):
    pass


def get_access_token(client_id: str, refresh_token: str) -> str:
    """refresh_token → access_token (wl.imap). 실패 시 OutlookError."""
    body = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": IMAP_SCOPE,
    }
    data = urllib.parse.urlencode(body).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        raise OutlookError(f"token_refresh_failed: {detail}")
    except Exception as e:
        raise OutlookError(f"token_refresh_error: {e}")
    tok = payload.get("access_token")
    if not tok:
        raise OutlookError(f"no_access_token: {json.dumps(payload)[:150]}")
    return tok


def _xoauth2(user: str, access_token: str) -> bytes:
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01".encode()


def _extract_code(text: str) -> Optional[str]:
    if not text:
        return None
    # '인증' 근처 우선
    for m in _CODE_RE.finditer(text):
        window = text[max(0, m.start() - 60): m.end() + 20]
        if any(h in window for h in ("인증", "verification", "code", "카카오")):
            if m.group(1) not in _YEARS:
                return m.group(1)
    for m in _CODE_RE.finditer(text):
        if m.group(1) not in _YEARS and len(m.group(1)) >= 6:
            return m.group(1)
    return None


def _message_text(msg) -> str:
    subject = str(make_header(decode_header(msg.get("Subject", ""))))
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct in ("text/plain", "text/html"):
                try:
                    body += part.get_payload(decode=True).decode(errors="replace")
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode(errors="replace")
        except Exception:
            body = str(msg.get_payload())
    # HTML 태그 제거(대략)
    body = re.sub(r"<[^>]+>", " ", body)
    return f"{subject}\n{body}"


def fetch_kakao_code(
    email_addr: str,
    client_id: str,
    refresh_token: str,
    *,
    newer_than_secs: int = 600,
    timeout: float = 90.0,
    poll: float = 6.0,
) -> Tuple[Optional[str], str]:
    """메일함에서 최근 카카오 인증번호를 읽어 반환. Returns (code|None, detail).

    newer_than_secs: 이 시간(초) 안에 도착한 메일만 유효(오래된 코드 재사용 방지).
    """
    if not client_id or not refresh_token:
        return None, "no_token"
    try:
        access = get_access_token(client_id, refresh_token)
    except OutlookError as e:
        return None, str(e)

    deadline = time.time() + timeout
    last_detail = "no_code_mail"
    while time.time() < deadline:
        try:
            M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            M.authenticate("XOAUTH2", lambda x: _xoauth2(email_addr, access))
        except Exception as e:
            return None, f"imap_auth_failed: {repr(e)[:150]}"
        try:
            M.select("INBOX")
            # 최근 메일부터 검사 (카카오 발신 + 인증)
            typ, data = M.search(None, "ALL")
            ids = data[0].split()
            for mid in reversed(ids[-15:]):
                typ, msg_data = M.fetch(mid, "(RFC822 INTERNALDATE)")
                raw = None
                internal = None
                for part in msg_data:
                    if isinstance(part, tuple):
                        raw = part[1]
                if raw is None:
                    continue
                msg = emaillib.message_from_bytes(raw)
                # 신선도 체크
                try:
                    dt = emaillib.utils.parsedate_to_datetime(msg.get("Date"))
                    age = time.time() - dt.timestamp()
                except Exception:
                    age = 0
                text = _message_text(msg)
                if not any(s in text.lower() or s in text for s in _KAKAO_SENDERS):
                    continue
                if age > newer_than_secs:
                    last_detail = f"only_stale_code(age={int(age)}s)"
                    continue
                code = _extract_code(text)
                if code:
                    M.logout()
                    return code, f"ok(age={int(age)}s)"
        finally:
            try:
                M.logout()
            except Exception:
                pass
        time.sleep(poll)
    return None, last_detail


DEFAULT_REDIRECT = "https://login.live.com/oauth20_desktop.srf"


def authorize_url(client_id: str, redirect_uri: str = DEFAULT_REDIRECT) -> str:
    """PC 브라우저에서 열 로그인/동의 URL. 로그인 후 redirect_uri?code=... 로 이동한다."""
    q = urllib.parse.urlencode({
        "client_id": client_id,
        "scope": IMAP_SCOPE,
        "response_type": "code",
        "redirect_uri": redirect_uri,
    })
    return f"https://login.live.com/oauth20_authorize.srf?{q}"


def exchange_code(client_id: str, code: str, redirect_uri: str = DEFAULT_REDIRECT) -> Tuple[str, str]:
    """authorization code → refresh_token. Returns (refresh_token, detail)."""
    body = {
        "client_id": client_id,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "scope": IMAP_SCOPE,
    }
    data = urllib.parse.urlencode(body).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        return "", f"exchange_failed: {e.read().decode(errors='replace')[:200]}"
    except Exception as e:
        return "", f"exchange_error: {e}"
    rt = payload.get("refresh_token")
    if not rt:
        return "", f"no_refresh_token: {json.dumps(payload)[:150]}"
    return rt, "ok"


def check_token(client_id: str, refresh_token: str) -> Tuple[bool, str]:
    """토큰 유효성만 빠르게 확인."""
    try:
        get_access_token(client_id, refresh_token)
        return True, "valid"
    except OutlookError as e:
        return False, str(e)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 4:
        print("usage: python3 outlook_api.py <email> <client_id> <refresh_token>")
        sys.exit(1)
    email_addr, cid, rt = sys.argv[1], sys.argv[2], sys.argv[3]
    ok, detail = check_token(cid, rt)
    print("token:", ok, detail)
    if ok:
        code, d = fetch_kakao_code(email_addr, cid, rt, newer_than_secs=86400, timeout=30)
        print("code:", code, "|", d)
