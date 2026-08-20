# -*- coding: utf-8 -*-
"""카카오맵 로그인 자동화 (카카오계정 직접 입력).

로그인 타이밍: 앱 데이터를 지우고(pm clear) IP를 바꾼 직후, 새 작업으로
들어가기 전. 즉 `open_kakaomap()` 직후 검색을 시작하기 전에 호출한다.

화면 흐름 (실기기 확인 완료):
    카카오맵 메인 → 하단 패널 '로그인' → 시트 '카카오계정 직접 입력'
    → Chrome 커스텀탭(accounts.kakao.com)
    → [Chrome 최초 실행 화면이 뜨면 '로그아웃 상태 유지'로 통과]
    → loginId--1 / password--2 입력 → ENTER
    → 카카오맵 복귀(성공) 또는 추가 인증 요구

주의:
    - 커스텀탭 안에서 back 키를 누르면 탭이 닫히고 로그인이 취소된다.
      키보드를 내릴 때도 back 대신 ENTER 제출을 쓴다.
    - 비밀번호에 특수문자가 있으므로 adb `input text` 가 아니라
      uiautomator2 `send_keys` 로 입력한다.
"""
from __future__ import annotations

import time
from typing import Optional, Tuple

import uiautomator2 as u2

PKG = "net.daum.android.map"
CHROME_PKG = "com.android.chrome"

# 로그인 결과 코드 — 웹 API(/api/accounts/kakao/{id}/report) 의 result 값과 동일
OK = "success"
VERIFY_REQUIRED = "verify_required"
BAD_CREDENTIAL = "bad_credential"
BLOCKED = "blocked"
FAILED = "login_failed"

# 카카오 로그인 페이지 입력 필드 (웹 요소 resource-id)
ID_FIELD = "loginId--1"
PW_FIELD = "password--2"

# Chrome 최초 실행(FRE) 화면에서 눌러 통과시킬 버튼들
CHROME_FRE_DISMISS = [
    "로그아웃 상태 유지",
    "사용 안함",
    "사용 안 함",
    "동의 후 계속",
    "계속",
    "No thanks",
]

VERIFY_MARKERS = ("추가 인증이 필요", "인증 방법", "이메일 인증", "ActionPenalty")
BAD_CRED_MARKERS = (
    "비밀번호가 일치하지 않",
    "가입되지 않은",
    "아이디 또는 비밀번호",
    "다시 확인해 주세요",
)
BLOCKED_MARKERS = ("일시적으로 제한", "로그인이 제한", "잠시 후 다시")


def _log(msg: str):
    print(f"[login] {msg}", flush=True)


def _xp(d: u2.Device, rid: str):
    return d.xpath(f'//*[@resource-id="{rid}"]')


def _screen_text(d: u2.Device) -> str:
    """현재 화면의 모든 텍스트를 한 덩어리로. 상태 판정용."""
    try:
        return d.dump_hierarchy()
    except Exception:
        return ""


def _click_text(d: u2.Device, text: str, timeout: float = 0.0) -> bool:
    """텍스트/설명이 정확히 일치하는 요소를 클릭."""
    deadline = time.time() + max(timeout, 0.0)
    while True:
        for sel in ({"text": text}, {"description": text}):
            try:
                node = d(**sel)
                if node.exists:
                    node.click()
                    return True
            except Exception:
                pass
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def _current_pkg(d: u2.Device) -> str:
    try:
        return d.app_current().get("package", "")
    except Exception:
        return ""


def dismiss_chrome_fre(d: u2.Device, timeout: float = 12.0) -> None:
    """Chrome 최초 실행 화면(로그인 유도·동의)을 통과시킨다.

    함많찾을 트래픽이 Chrome 데이터를 지우면 매번 다시 나타난다.
    커스텀탭 주소창(url_bar)이 보이면 이미 통과한 것.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _xp(d, f"{CHROME_PKG}:id/url_bar").exists:
            return
        hit = False
        for label in CHROME_FRE_DISMISS:
            if _click_text(d, label):
                _log(f"Chrome 최초 실행 화면 통과: {label}")
                hit = True
                time.sleep(1.5)
                break
        if not hit:
            time.sleep(0.8)


PERM_DENY = ("허용 안함", "허용 안 함", "이번에는 허용 안함", "Don't allow", "Deny", "나중에", "아니요")


def _dismiss_perms(d: u2.Device, rounds: int = 4):
    """pm clear 후 뜨는 알림/위치 권한 다이얼로그를 거부(닫기)."""
    for _ in range(rounds):
        hit = False
        for t in PERM_DENY:
            if _click_text(d, t):
                hit = True
                time.sleep(0.8)
                break
        if not hit:
            break


def _login_form_present(d: u2.Device) -> bool:
    return _xp(d, ID_FIELD).exists


def open_login_page(d: u2.Device, timeout: float = 55.0) -> bool:
    """카카오맵 메인 → 카카오계정 로그인 폼(loginId--1)까지 진입.

    fresh 진입 시 나타나는 모든 인터스티셜(권한 다이얼로그 / Chrome 최초실행 /
    이전 계정 동의화면)을 매 반복마다 판별·처리하며, 진입 버튼(로그인 →
    카카오계정 직접 입력)을 재시도한다. 타이밍 편차에 강하도록 폴링 방식.
    """
    deadline = time.time() + timeout
    last_login_click = 0.0
    last_direct_click = 0.0
    while time.time() < deadline:
        if _login_form_present(d):
            _log("카카오계정 로그인 페이지 도달")
            return True
        try:
            xml = d.dump_hierarchy()
        except Exception:
            xml = ""
        now = time.time()

        # 1) 권한 다이얼로그(알림/위치) 거부
        for t in PERM_DENY:
            if f'text="{t}"' in xml:
                _click_text(d, t)
                time.sleep(0.8)
                break

        # 2) Chrome 최초실행(FRE) — '로그아웃 상태 유지' 등
        for t in CHROME_FRE_DISMISS:
            if t in xml:
                _click_text(d, t)
                _log(f"Chrome 최초실행 통과: {t}")
                time.sleep(1.5)
                break

        # 3) 이전 계정 기억한 동의화면 → 다른 계정으로
        if "다른 카카오계정으로 로그인" in xml:
            _log("동의화면 — '다른 카카오계정으로 로그인'")
            _click_text(d, "다른 카카오계정으로 로그인")
            time.sleep(2.5)
            continue

        # 4) 로그인 방식 시트: '카카오계정 직접 입력'
        if "카카오계정 직접 입력" in xml and (now - last_direct_click) > 3:
            _click_text(d, "카카오계정 직접 입력")
            last_direct_click = now
            time.sleep(2.5)
            continue

        # 5) 메인 하단 패널 '로그인'
        if 'text="로그인"' in xml and (now - last_login_click) > 3:
            _click_text(d, "로그인")
            last_login_click = now
            time.sleep(2.0)
            continue

        time.sleep(1.0)
    _log("[!] 로그인 페이지 로딩 실패")
    return False


def fill_and_submit(d: u2.Device, email: str, password: str) -> bool:
    """아이디·비밀번호 입력 후 ENTER 로 제출.

    입력 후 레이아웃이 바뀌므로(TIP 문구 등) 매번 resource-id 로 다시 찾는다.
    """
    field = _xp(d, ID_FIELD)
    if not field.exists:
        return False
    field.click()
    time.sleep(0.5)
    d.send_keys(email, clear=True)
    time.sleep(1.0)

    field = _xp(d, PW_FIELD)   # 레이아웃 변동 → 재조회 필수
    if not field.exists:
        _log("[!] 비밀번호 입력칸을 찾지 못함")
        return False
    field.click()
    time.sleep(0.5)
    d.send_keys(password, clear=True)
    time.sleep(1.0)

    # 커스텀탭에서 back 은 탭을 닫아버리므로 ENTER 로 제출한다
    d.press("enter")
    _log(f"제출: {email}")
    return True


def wait_login_result(d: u2.Device, timeout: float = 40.0) -> Tuple[str, str]:
    """제출 후 결과 판정. Returns (result_code, 설명)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pkg = _current_pkg(d)

        # 카카오맵으로 복귀 = 로그인 완료(또는 인증 후 부가화면). 인내심 있게 재확인.
        if pkg == PKG and not _xp(d, ID_FIELD).exists:
            for _ in range(6):
                if _logged_in(d):
                    return OK, "로그인 성공"
                # 인증 성공 후 '전화번호 등록/혜택 동의' 등 부가화면 스킵
                dismissed = False
                for t in ("다음에 할게요", "나중에", "지금은 아니에요", "건너뛰기"):
                    if _click_text(d, t):
                        dismissed = True
                        break
                time.sleep(1.5 if dismissed else 1.2)
            if _logged_in(d):
                return OK, "로그인 성공"
            return FAILED, "카카오맵 복귀했으나 로그인 상태 미확인"

        xml = _screen_text(d)
        for marker in VERIFY_MARKERS:
            if marker in xml:
                return VERIFY_REQUIRED, "추가 인증 요구 (평소와 다른 기기·네트워크)"
        for marker in BLOCKED_MARKERS:
            if marker in xml:
                return BLOCKED, "로그인 일시 제한"
        for marker in BAD_CRED_MARKERS:
            if marker in xml:
                return BAD_CREDENTIAL, "아이디 또는 비밀번호 오류"

        time.sleep(1.5)
    return FAILED, "결과 판정 타임아웃"


def _logged_in(d: u2.Device) -> bool:
    """카카오맵 메인에서 로그인 상태인지. 하단 패널에 '로그인' 이 없으면 로그인됨.

    fresh 진입 직후 하단 패널 로드 지연으로 오탐할 수 있어, 메인이 확실히
    뜬 상태에서만 판정한다(btn_search 존재 확인).
    """
    try:
        xml = d.dump_hierarchy()
    except Exception:
        return False
    main = (f'{PKG}:id/btn_search' in xml) or (f'{PKG}:id/query' in xml)
    if not main:
        return False
    # 로그인 안내가 보이면(하단 '로그인' / 계정 선택 등) 미로그인
    if 'text="로그인"' in xml or "카카오계정 직접 입력" in xml or "카카오 로그인" in xml:
        return False
    return True


def is_logged_in(d: u2.Device) -> bool:
    """외부에서 쓰는 로그인 상태 확인."""
    return _logged_in(d)


def submit_passcode(d: u2.Device, code: str) -> bool:
    """추가인증 패스코드 화면에 코드 입력·제출."""
    field = _xp(d, "passcode--8")
    if not field.exists:
        for rid in ("passcode", "code", "verifyCode"):
            n = d.xpath(f'//*[contains(@resource-id,"{rid}")]')
            if n.exists:
                field = n
                break
    if not field.exists:
        return False
    field.click()
    time.sleep(0.4)
    d.send_keys(code, clear=True)
    time.sleep(0.6)
    d.press("enter")
    time.sleep(3)
    if _screen_text(d).find("passcode") >= 0 and _xp(d, "passcode--8").exists:
        _click_text(d, "확인")
    time.sleep(6)
    return True


def request_email_passcode(d: u2.Device) -> bool:
    """'이메일 인증' → 메일 선택 → '다음' 으로 코드 발송."""
    if not _click_text(d, "이메일 인증", timeout=8.0):
        return False
    time.sleep(3)
    radio = d.xpath('//*[starts-with(@resource-id,"r_email_")]')
    if radio.exists:
        radio.click()
        time.sleep(1)
    if not _click_text(d, "다음", timeout=8.0):
        return False
    time.sleep(4)
    return True


def login(
    d: u2.Device,
    email: str,
    password: str,
    *,
    result_timeout: float = 40.0,
    code_fetcher=None,
    force: bool = False,
) -> Tuple[str, str]:
    """카카오맵 로그인 전체 흐름. Returns (result_code, 설명).

    앱 데이터 삭제 + IP 변경 직후, 카카오맵 메인이 떠 있는 상태에서 호출한다.
    force=True 면 로그인 상태 체크를 건너뛰고 무조건 로그인 폼으로 진입한다
    (pm clear 직후엔 실제로 로그아웃 상태이나 하단 패널 로드 지연으로 _logged_in 이
    오탐할 수 있으므로).
    """
    if not force and _logged_in(d):
        _log("이미 로그인 상태 — 건너뜀")
        return OK, "이미 로그인 상태"

    if not open_login_page(d):
        return FAILED, "로그인 페이지 진입 실패"

    if not fill_and_submit(d, email, password):
        return FAILED, "계정 정보 입력 실패"

    result, detail = wait_login_result(d, timeout=result_timeout)
    _log(f"결과: {result} — {detail}")

    # 추가인증이 뜨고 code_fetcher(서버 API)가 있으면 자동으로 코드 입력
    if result == VERIFY_REQUIRED and code_fetcher is not None:
        _log("추가인증 — 서버에서 코드 수신 시도")
        if request_email_passcode(d):
            code = ""
            try:
                code = code_fetcher() or ""
            except Exception as e:
                _log(f"code_fetcher 오류: {e}")
            if code and submit_passcode(d, code):
                result, detail = _wait_after_passcode(d, timeout=result_timeout)
                _log(f"인증 후 결과: {result} — {detail}")
    return result, detail


POST_VERIFY_SKIP = (
    "다음에 할게요", "나중에", "지금은 아니에요", "건너뛰기", "나중에 하기",
    "다음에", "취소",
)


def _wait_after_passcode(d, timeout: float = 45.0):
    """패스코드 통과 후: 전화번호등록·마케팅동의 등 선택화면을 스킵하며 로그인 완료 판정."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _logged_in(d):
            return OK, "로그인 성공(인증 후)"
        xml = _screen_text(d)
        # 코드 오류/만료면 즉시 실패
        if "인증번호가 일치하지 않" in xml or "만료" in xml or "다시 시도" in xml:
            return VERIFY_REQUIRED, "인증번호 오류/만료"
        # 카카오맵 메인 복귀 확인(팝업 위에서도)
        pkg = _current_pkg(d)
        if pkg == PKG and (f'{PKG}:id/btn_search' in xml or f'{PKG}:id/query' in xml):
            # 남은 팝업 정리
            for t in POST_VERIFY_SKIP + ("모두 동의하기",):
                pass
            for t in POST_VERIFY_SKIP:
                if _click_text(d, t):
                    time.sleep(1.5)
            if _logged_in(d):
                return OK, "로그인 성공(인증 후)"
        # 선택 화면 스킵
        skipped = False
        for t in POST_VERIFY_SKIP:
            if _click_text(d, t):
                _log(f"인증 후 화면 스킵: {t}")
                skipped = True
                time.sleep(2)
                break
        if not skipped:
            time.sleep(1.5)
    # 타임아웃이어도 로그인됐으면 성공
    return (OK, "로그인 성공(지연확인)") if _logged_in(d) else (FAILED, "인증 후 판정 타임아웃")


def abandon_login(d: u2.Device, serial: Optional[str] = None) -> None:
    """로그인 실패 시 커스텀탭을 정리하고 카카오맵으로 돌아간다."""
    try:
        if _current_pkg(d) == CHROME_PKG:
            if not _click_text(d, "탭 닫기"):
                d.app_stop(CHROME_PKG)
            time.sleep(1.5)
    except Exception:
        pass
