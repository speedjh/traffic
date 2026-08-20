# -*- coding: utf-8 -*-
"""아웃룩 웹메일에서 카카오 인증번호를 자동으로 읽는다 (폰 화면 + OCR).

이 PWA(중국어 로케일)는 a11y 트리에 내용을 잘 안 내보내고 리다이렉트가
잦아, URL/노드 기반 판정이 불안정하다. 그래서 매 단계 스크린샷을 OCR 해
현재 화면을 분류하는 상태머신으로 구동한다. OCR 은 렌더된 픽셀을 읽으므로
로케일·웹뷰에 관계없이 안정적이다.

핵심 함수: read_kakao_code(serial, mail_email, mail_password) -> Optional[str]
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from typing import List, Optional, Tuple

ROOT = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(ROOT, "campaign_web", "logs")
_OCR = None


def log(msg: str):
    print(f"[outlook] {msg}", flush=True)


def _adb_bin() -> str:
    b = os.path.join(ROOT, "platform-tools", "adb")
    return b if os.path.isfile(b) else "adb"


def adb(serial: str, *args, timeout: float = 30.0) -> str:
    return subprocess.run(
        [_adb_bin(), "-s", serial, *args], capture_output=True, text=True, timeout=timeout
    ).stdout


def _tap(serial: str, x: int, y: int):
    adb(serial, "shell", "input", "tap", str(int(x)), str(int(y)))


def _type(serial: str, text: str):
    adb(serial, "shell", "input", "text", text)


def _key(serial: str, code: int):
    adb(serial, "shell", "input", "keyevent", str(code))


def _open_url(serial: str, url: str):
    adb(serial, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url, timeout=15)


def _dump(serial: str) -> str:
    """a11y 트리. 네이티브 화면(Chrome FRE / MS 로그인)의 한국어는 여기서 잡힌다."""
    for _ in range(3):
        adb(serial, "shell", "uiautomator", "dump", "/sdcard/uid.xml")
        xml = adb(serial, "shell", "cat", "/sdcard/uid.xml")
        if "<node" in xml:
            return xml
        time.sleep(0.8)
    return xml


def _url(serial: str) -> str:
    xml = _dump(serial)
    m = re.search(r'resource-id="com\.android\.chrome:id/url_bar"[^>]*text="([^"]*)"', xml)
    if not m:
        m = re.search(r'text="([^"]*)"[^>]*resource-id="com\.android\.chrome:id/url_bar"', xml)
    return m.group(1) if m else ""


def _tap_node(serial: str, xml: str, text: str) -> bool:
    """a11y 노드(text/desc 부분일치)를 탭."""
    for m in re.finditer(r"<node[^>]*>", xml):
        node = m.group()
        t = (re.search(r'text="([^"]*)"', node) or ["", ""])[1]
        d = (re.search(r'content-desc="([^"]*)"', node) or ["", ""])[1]
        if text in t or text in d:
            b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
            if b:
                x1, y1, x2, y2 = map(int, b.groups())
                if x2 <= x1 or y2 <= y1:
                    continue  # 0크기 노드 스킵
                _tap(serial, (x1 + x2) // 2, (y1 + y2) // 2)
                return True
    return False


# ── OCR ────────────────────────────────────────────────────────────────────
def _ocr_boxes(serial: str, tag: str = "shot") -> List[Tuple[int, int, str]]:
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR = RapidOCR()
    remote = "/sdcard/ocr_shot.png"
    local = os.path.join(LOGDIR, f"outlook_{tag}.png")
    adb(serial, "shell", "screencap", "-p", remote)
    adb(serial, "pull", remote, local)
    res, _ = _OCR(local)
    out = []
    for box, txt, _conf in (res or []):
        cx = int(sum(p[0] for p in box) / 4)
        cy = int(sum(p[1] for p in box) / 4)
        out.append((cx, cy, txt))
    return out


def _find_box(boxes, *needles):
    for cx, cy, t in boxes:
        for n in needles:
            if n in t:
                return (cx, cy, t)
    return None


def _text(boxes) -> str:
    return "\n".join(t for _x, _y, t in boxes)


# ── 화면 분류 ──────────────────────────────────────────────────────────────
def classify_native(serial: str) -> Tuple[str, str]:
    """a11y + url 로 네이티브 화면 판정. Returns (state, xml)."""
    xml = _dump(serial)
    url = _url(serial).lower()
    if "로그아웃 상태 유지" in xml or "나만의 Chrome" in xml:
        return "chrome_fre", xml
    # Chrome 알림 권한 프롬프트 / 구글 비번저장 등 방해 팝업
    if ("Chrome 알림" in xml or "알림으로 더" in xml) and "나중에" in xml:
        return "chrome_notif", xml
    if "비밀번호를" in xml and "저장" in xml:
        return "gpw_save", xml
    if "암호 입력" in xml:
        return "login_pw", xml
    if "로그인 상태" in xml and "유지" in xml:
        return "stay_signed", xml
    if "내 계정을 보호" in xml or "someone@example" in xml:
        return "recovery_nag", xml
    if "보안 키" in xml or "패스키" in xml:
        return "passkey", xml
    if "전자 메일" in xml and "다음" in xml:
        return "login_email", xml
    if "본인 Microsoft 계정" in xml:
        return "login_email", xml
    if ("계속 로그인" in xml or "무료 계정 만들기" in xml or "플랜 및 가격" in xml
            or ("다운로드" in xml and "Microsoft 365" in xml)):
        return "marketing", xml
    if ("outlook.live.com/mail" in url or "outlook.office.com/mail" in url) and "암호" not in xml:
        return "maybe_inbox", xml   # OCR 로 최종 확인
    return "unknown", xml


def classify(boxes) -> str:
    t = _text(boxes)
    if any(k in t for k in ("收件箱", "重点", "筛选", "받은 편지함")):
        return "inbox"
    if "收件人" in t or "答复" in t or "인증번호입니다" in t:
        return "mail"
    if "로그아웃 상태 유지" in t or "나만의 Chrome" in t:
        return "chrome_fre"
    if "암호 입력" in t or ("암호" in t and "다음" in t):
        return "login_pw"
    if "로그인 상태" in t and ("예" in t or "아니요" in t):
        return "stay_signed"
    if "내 계정을 보호" in t or "someone@example" in t:
        return "recovery_nag"
    if "보안 키" in t or "패스키" in t:
        return "passkey"
    if "전자 메일" in t and "다음" in t:
        return "login_email"
    if "계속 로그인" in t or "다운로드" in t or "지금 가입" in t:
        return "marketing"
    if "본인 Microsoft 계정" in t or ("로그인" in t and "Microsoft" in t):
        return "login_email"
    return "unknown"


# ── 로그인 ─────────────────────────────────────────────────────────────────
def ensure_logged_in(serial, mail_email, mail_password, timeout: float = 140.0) -> bool:
    """하이브리드: 네이티브 화면(FRE/로그인/넛지)은 a11y, 받은편지함은 OCR."""
    _open_url(serial, "https://outlook.live.com/mail/0/inbox")
    time.sleep(7)
    deadline = time.time() + timeout
    unknown_streak = 0
    while time.time() < deadline:
        state, xml = classify_native(serial)
        if state == "maybe_inbox":
            boxes = _ocr_boxes(serial, "login")
            if classify(boxes) == "inbox":
                log("받은편지함 진입 확인")
                return True
            state = "unknown"
        if state != "unknown":
            unknown_streak = 0
        if state == "chrome_fre":
            _tap_node(serial, xml, "로그아웃 상태 유지")
            time.sleep(2)
        elif state == "chrome_notif":
            _tap_node(serial, xml, "나중에")
            time.sleep(2)
        elif state == "gpw_save":
            # 구글 비번저장: 무시(닫기). '저장' 안 누르고 지나가면 사라진다
            _key(serial, 4)
            time.sleep(1)
        elif state == "marketing":
            tapped = (_tap_node(serial, xml, "계속 로그인")
                      or _tap_node(serial, xml, "Outlook에 로그인")
                      or _tap_node(serial, xml, "웹용 Outlook에 로그인")
                      or _tap_node(serial, xml, "로그인"))
            if tapped:
                time.sleep(5)
            else:
                _open_url(serial, "https://login.live.com/")
                time.sleep(6)
        elif state == "login_email":
            if not _tap_node(serial, xml, "전자 메일"):
                _tap(serial, 540, 612)
            time.sleep(0.6)
            _type(serial, mail_email)
            time.sleep(0.6)
            _key(serial, 66)
            time.sleep(6)
        elif state == "login_pw":
            _tap(serial, 540, 870)   # 암호 필드
            time.sleep(0.6)
            _type(serial, mail_password)
            time.sleep(0.6)
            _key(serial, 66)
            time.sleep(7)
        elif state == "stay_signed":
            _tap_node(serial, xml, "예")
            time.sleep(4)
        elif state == "recovery_nag":
            _open_url(serial, "https://outlook.live.com/mail/0/inbox")
            time.sleep(7)
        elif state == "passkey":
            _key(serial, 4)
            time.sleep(2)
        else:
            unknown_streak += 1
            # 흔한 방해 팝업 닫기 시도
            dismissed = False
            for label in ("나중에", "아니요", "취소", "No thanks", "건너뛰기", "지금은 아니에요"):
                if _tap_node(serial, xml, label):
                    dismissed = True
                    time.sleep(1.5)
                    break
            if dismissed:
                unknown_streak = 0
            elif unknown_streak == 2:
                _open_url(serial, "https://outlook.live.com/mail/0/inbox")
                time.sleep(6)
            elif unknown_streak >= 5:
                log(f"[!] 알 수 없는 화면 지속 (url={_url(serial)[:40]}) txt={_dump(serial)[:0]}")
                # 마지막 시도: 뒤로 후 재진입
                _key(serial, 4)
                time.sleep(1)
                _open_url(serial, "https://outlook.live.com/mail/0/inbox")
                time.sleep(6)
                unknown_streak = 0
            else:
                time.sleep(2)
    return False


# ── 코드 읽기 ──────────────────────────────────────────────────────────────
_DATE_RE = re.compile(r"月.?\d|\d{1,2}:\d{2}|昨天|今天|周[一二三四五六日]")
_YEARS = ("2023", "2024", "2025", "2026", "2027", "2028")


def _code_from_boxes(boxes) -> Optional[str]:
    primary, secondary, embedded = [], [], []
    for _x, _y, line in boxes:
        s = line.replace(" ", "").replace("-", "")
        if re.fullmatch(r"\d{4,8}", s) and s not in _YEARS:
            (primary if len(s) >= 6 else secondary).append(s)
        for m in re.findall(r"(?<!\d)(\d{4,8})(?!\d)", s):
            if m not in _YEARS:
                embedded.append(m)
    if primary:
        return primary[0]
    if secondary:
        return secondary[0]
    for c in embedded:
        if len(c) >= 6:
            return c
    return embedded[0] if embedded else None


def _open_top_mail(serial) -> bool:
    boxes = _ocr_boxes(serial, "inbox")
    promo = _find_box(boxes, "谢谢", "谢")
    if promo:
        _tap(serial, promo[0], promo[1])
        time.sleep(2)
        boxes = _ocr_boxes(serial, "inbox")
    rows = [(cy, cx) for cx, cy, t in boxes if 360 < cy < 1900 and _DATE_RE.search(t)]
    if not rows:
        return False
    rows.sort()
    _tap(serial, 400, rows[0][0])
    time.sleep(6)
    return True


def read_kakao_code(serial, mail_email, mail_password, *, timeout: float = 150.0,
                    poll: float = 6.0) -> Optional[str]:
    if not mail_email or not mail_password:
        log("메일 자격 없음 — 자동 수신 불가")
        return None
    if not ensure_logged_in(serial, mail_email, mail_password):
        log("[!] 아웃룩 로그인/진입 실패")
        return None
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        _open_url(serial, "https://outlook.live.com/mail/0/inbox")
        time.sleep(6)
        boxes = _ocr_boxes(serial, "inbox")
        if classify(boxes) != "inbox":
            ensure_logged_in(serial, mail_email, mail_password, timeout=70)
            continue
        if not _open_top_mail(serial):
            log(f"코드메일 열기 실패 (시도 {attempt})")
            time.sleep(poll)
            continue
        body = _ocr_boxes(serial, "body")
        if classify(body) != "mail":
            log(f"메일 본문 미확인 (시도 {attempt})")
            _key(serial, 4)
            time.sleep(poll)
            continue
        code = _code_from_boxes(body)
        _key(serial, 4)
        time.sleep(1)
        if code:
            log(f"인증번호 OCR 성공: {code}")
            return code
        log(f"OCR 코드 추출 실패 (시도 {attempt})")
        time.sleep(poll)
    return None


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("usage: python3 outlook_ocr.py <serial> <mail_email> <mail_password>")
        sys.exit(1)
    print("코드:", read_kakao_code(sys.argv[1], sys.argv[2], sys.argv[3]))
