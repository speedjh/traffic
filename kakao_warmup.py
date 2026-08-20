# -*- coding: utf-8 -*-
"""카카오 계정 '예열' — 이 기기에서 최초 1회 추가 인증까지 통과시킨다.

카카오는 처음 보는 기기·네트워크에서의 로그인에 추가 인증을 요구한다
(selectVerificationMethodForActionPenalty). 인증번호는 계정에 등록된 메일로만
오고 자동 수신이 불가하므로, 이 도구는 메일함을 사람이 확인해 코드를 넣는
반자동 방식으로 동작한다.

사용:
    python3 kakao_warmup.py --email a@kakao.com --password 'pw!'
    python3 kakao_warmup.py --account-id 42          # 웹 DB에서 계정 정보를 읽어옴
    python3 kakao_warmup.py --account-id 42 --keep-ip # IP 변경 없이

성공하면 웹 API에 success 로 보고되어 '예열' 표시가 붙는다.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from typing import Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ["PATH"] = os.path.join(ROOT, "platform-tools") + os.pathsep + os.environ.get("PATH", "")

import uiautomator2 as u2  # noqa: E402

import kakao_login  # noqa: E402

PKG = kakao_login.PKG
CHROME_PKG = kakao_login.CHROME_PKG
DEFAULT_API = os.environ.get("CAMPAIGN_API", "http://127.0.0.1:8080")


def log(msg: str):
    print(f"[warmup] {msg}", flush=True)


def adb(serial: str, *args, timeout: float = 30.0) -> str:
    adb_bin = os.path.join(ROOT, "platform-tools", "adb")
    if not os.path.isfile(adb_bin):
        adb_bin = "adb"
    return subprocess.run(
        [adb_bin, "-s", serial, *args], capture_output=True, text=True, timeout=timeout
    ).stdout


def fetch_credentials(account_id: int) -> tuple:
    """계정 이메일·비번·메일자격을 웹 DB 에서 직접 읽는다.
    Returns (email, password, mail_email, mail_password)."""
    import sqlite3

    db = os.path.join(ROOT, "campaign_web", "data", "campaign_queue.db")
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT email, password, mail_email, mail_password "
            "FROM kakao_accounts WHERE id=?", (account_id,)
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise SystemExit(f"계정 id={account_id} 을 찾을 수 없습니다")
    return row[0], row[1], row[2] or "", row[3] or ""


def report(api: str, account_id: Optional[int], result: str, note: str = ""):
    if account_id is None:
        return
    import json
    import urllib.request

    url = f"{api.rstrip('/')}/api/accounts/kakao/{account_id}/report"
    body = json.dumps({"result": result, "note": note[:300]}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        log(f"웹에 보고 완료: {result}")
    except Exception as e:
        log(f"[!] 웹 보고 실패: {e}")


def open_fresh_app(d: u2.Device, serial: str, clear: bool = True):
    if clear:
        log("앱 데이터 삭제 (카카오맵 + Chrome)")
        adb(serial, "shell", "pm", "clear", PKG)
        adb(serial, "shell", "pm", "clear", CHROME_PKG)
        time.sleep(3)
    adb(serial, "shell", "settings", "put", "secure", "location_mode", "0")
    adb(serial, "shell", "am", "force-stop", PKG)
    time.sleep(1)
    adb(
        serial, "shell", "am", "start",
        "-a", "android.intent.action.MAIN",
        "-c", "android.intent.category.LAUNCHER",
        "-n", f"{PKG}/com.kakao.map.main.view.MainActivity",
    )
    for _ in range(40):
        if d.xpath(f'//*[@resource-id="{PKG}:id/btn_search"]').exists:
            log("카카오맵 메인 도달")
            time.sleep(2)
            return True
        time.sleep(1)
    log("[!] 메인 화면 로딩 실패")
    return False


def request_email_code(d: u2.Device, timeout: float = 30.0) -> bool:
    """'이메일 인증' → 메일 선택 → '다음' 으로 인증번호를 발송시킨다."""
    if not kakao_login._click_text(d, "이메일 인증", timeout=8.0):
        log("[!] '이메일 인증' 버튼 없음")
        return False
    time.sleep(3)

    # 메일 주소 라디오가 여러 개면 첫 번째 선택
    radio = d.xpath('//*[starts-with(@resource-id,"r_email_")]')
    if radio.exists:
        target = radio.get_text() or ""
        radio.click()
        log(f"인증 메일 주소: {target}")
        time.sleep(1)

    if not kakao_login._click_text(d, "다음", timeout=8.0):
        log("[!] '다음' 버튼 없음")
        return False
    log("인증번호 발송 요청 완료")
    time.sleep(4)
    return True


def _find_code_field(d: u2.Device):
    """인증번호 입력칸 찾기 (id 가 화면마다 달라 여러 방식으로 탐색)."""
    for xp in (
        '//*[contains(@resource-id,"passcode")]',   # 실제 화면: passcode--8
        '//*[starts-with(@resource-id,"code")]',
        '//*[contains(@resource-id,"verifyCode")]',
        '//*[contains(@resource-id,"authCode")]',
        '//android.widget.EditText',
    ):
        node = d.xpath(xp)
        if node.exists:
            return node
    return None


def submit_email_code(d: u2.Device, code: str) -> bool:
    field = _find_code_field(d)
    if field is None:
        log("[!] 인증번호 입력칸을 찾지 못했습니다. 현재 화면:")
        print(d.dump_hierarchy()[:4000])
        return False
    field.click()
    time.sleep(0.5)
    d.send_keys(code, clear=True)
    time.sleep(1)
    # 키보드가 올라와 있으면 '확인' 버튼 높이가 0 이 되어 클릭이 빗나간다.
    # 커스텀탭에서 back 은 탭을 닫으므로, ENTER 제출을 먼저 쓴다.
    d.press("enter")
    time.sleep(3)
    if _find_code_field(d) is not None:
        kakao_login._click_text(d, "확인")
    log(f"인증번호 제출: {code}")
    time.sleep(8)
    return True


def main():
    ap = argparse.ArgumentParser(description="카카오 계정 예열 (최초 1회 추가 인증 통과)")
    ap.add_argument("--serial", default=os.environ.get("DEVICE_SERIAL", "R3CN60HA2FV"))
    ap.add_argument("--email", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--account-id", type=int, default=None, help="웹 DB의 계정 id")
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--no-clear", action="store_true", help="앱 데이터 삭제 생략")
    ap.add_argument("--change-ip", action="store_true", help="시작 전 모바일 데이터 토글로 IP 변경")
    ap.add_argument("--send-only", action="store_true",
                    help="1단계: 로그인 + 인증번호 발송까지만 하고 종료 (폰 화면은 그대로 둔다)")
    ap.add_argument("--code", default="",
                    help="2단계: 메일로 받은 인증번호. 폰에 떠 있는 입력칸에 바로 넣는다")
    ap.add_argument("--auto-code", action="store_true",
                    help="인증번호를 메일함에서 자동 수신해 입력 (아웃룩=Graph토큰 / 네이버=--mail-pw)")
    ap.add_argument("--mail-pw", default="",
                    help="네이버 계정 자동 수신용 메일 비밀번호(카카오 비번과 다를 수 있음)")
    args = ap.parse_args()

    email, password = args.email, args.password
    mail_email, mail_password = "", args.mail_pw
    if args.account_id is not None:
        db_email, db_pw, db_me, db_mp = fetch_credentials(args.account_id)
        email = email or db_email
        password = password or db_pw
        mail_email = mail_email or db_me
        mail_password = mail_password or db_mp
    if not email or not password:
        raise SystemExit("--email/--password 또는 --account-id 가 필요합니다")

    if args.change_ip:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "ksd", os.path.join(ROOT, "CLAUDE", "kakao_search_device.py")
        )
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        log("IP 변경 시도")
        m.toggle_mobile_data(args.serial, off_secs=6.0, recover_timeout=30.0)

    d = u2.connect(args.serial)
    d.settings["wait_timeout"] = 15.0
    kakao_login_mode_watchers(d)

    # 2단계 단독 실행: 이미 인증번호 화면이 떠 있는 상태
    if args.code:
        if not submit_email_code(d, args.code):
            report(args.api, args.account_id, "verify_required", "인증번호 입력 실패")
            sys.exit(1)
        result, detail = kakao_login.wait_login_result(d, timeout=45.0)
        log(f"최종 결과: {result} — {detail}")
        report(args.api, args.account_id, result, detail)
        sys.exit(0 if result == kakao_login.OK else 1)

    if not open_fresh_app(d, args.serial, clear=not args.no_clear):
        report(args.api, args.account_id, "login_failed", "앱 진입 실패")
        sys.exit(1)

    if not kakao_login.open_login_page(d):
        report(args.api, args.account_id, "login_failed", "로그인 페이지 진입 실패")
        sys.exit(1)
    if not kakao_login.fill_and_submit(d, email, password):
        report(args.api, args.account_id, "login_failed", "계정 정보 입력 실패")
        sys.exit(1)

    result, detail = kakao_login.wait_login_result(d)
    log(f"1차 결과: {result} — {detail}")

    if result == kakao_login.OK:
        log("추가 인증 없이 바로 로그인됨 — 예열 불필요")
        report(args.api, args.account_id, "success", "추가 인증 없음")
        return

    if result != kakao_login.VERIFY_REQUIRED:
        report(args.api, args.account_id, result, detail)
        sys.exit(1)

    if not request_email_code(d):
        report(args.api, args.account_id, "verify_required", "인증번호 발송 실패")
        sys.exit(1)

    # 인증번호 확보: 자동 수신 → 실패 시 반자동(사람 입력)
    code = ""
    if args.auto_code and args.account_id is not None:
        # 코드 수신은 100% 서버(PC)에서. 폰은 카카오만 — 폰에서 아웃룩 접근 안 함.
        code = server_fetch_code(args.api, args.account_id)
    if False:  # (폰 OCR 폴백 제거됨)
        mail_domain = (mail_email or email).split("@")[-1].lower()
        if "outlook" in mail_domain or "hotmail" in mail_domain or "live" in mail_domain:
            if not mail_email or not mail_password:
                log(f"[!] {email} 아웃룩 메일 자격 없음 — 자동 수신 불가")
            else:
                import outlook_ocr

                log(f"인증번호 자동 수신 (아웃룩 OCR: {mail_email})…")
                # 카카오 CCT(패스코드 화면)를 잠깐 떠나 아웃룩을 읽고 돌아온다
                code = outlook_ocr.read_kakao_code(
                    args.serial, mail_email, mail_password, timeout=120, poll=6
                ) or ""
                _return_to_kakao_passcode(args.serial)
                if code:
                    log(f"자동 수신 성공: {code}")
                else:
                    log("[!] 아웃룩 OCR 자동 수신 실패")
        else:
            import mail_reader

            ok, method = mail_reader.available_for(email, mail_password)
            if ok:
                log(f"인증번호 자동 수신 대기 ({method})…")
                code = mail_reader.read_code(
                    email, mail_password=mail_password, timeout=120, poll=5
                ) or ""
            else:
                log(f"[!] {email} 자동 수신 불가 ({method})")

    if not code and args.send_only:
        print()
        print("=" * 62)
        print(f"  {email} 계정으로 인증번호를 발송했습니다.")
        print("  메일함에서 번호를 확인한 뒤 아래 명령으로 이어서 진행하세요:")
        idpart = f" --account-id {args.account_id}" if args.account_id else ""
        print(f"    python3 kakao_warmup.py --serial {args.serial}{idpart} --code 123456")
        print("  (폰 화면을 건드리지 마세요 — 인증번호 입력칸이 떠 있어야 합니다)")
        print("=" * 62)
        return

    if not code:
        print()
        print("=" * 62)
        print(f"  {email} 로 인증번호가 발송되었습니다.")
        print("  메일함에서 인증번호를 확인해 아래에 입력하세요. (취소: 빈 줄 + Enter)")
        print("=" * 62)
        try:
            code = input("인증번호> ").strip()
        except EOFError:
            code = ""
    if not code:
        log("취소됨/미입력")
        report(args.api, args.account_id, "verify_required", "인증번호 미입력")
        sys.exit(1)

    if not submit_email_code(d, code):
        report(args.api, args.account_id, "verify_required", "인증번호 입력 실패")
        sys.exit(1)

    result, detail = kakao_login.wait_login_result(d, timeout=45.0)
    log(f"최종 결과: {result} — {detail}")
    report(args.api, args.account_id, result if result == kakao_login.OK else result, detail)
    sys.exit(0 if result == kakao_login.OK else 1)


def server_fetch_code(api: str, account_id: int, wait: int = 90, retries: int = 3,
                      gap: float = 12.0) -> str:
    """서버(PC)가 아웃룩 메일함에서 코드를 읽어오게 요청. 코드는 발송 직후 도착이
    지연될 수 있어 서버에서 재시도한다(폰은 관여하지 않음). 실패 시 빈 문자열."""
    import json as _json
    import urllib.request as _u
    for attempt in range(1, retries + 1):
        url = f"{api.rstrip('/')}/api/accounts/kakao/{account_id}/fetch-code?wait={wait}"
        try:
            with _u.urlopen(url, timeout=wait + 30) as r:
                j = _json.load(r)
        except Exception as e:
            log(f"서버 코드조회 오류({attempt}/{retries}): {e}")
            time.sleep(gap)
            continue
        if not j.get("available"):
            log(f"서버 코드조회 불가: {j.get('detail')}")
            return ""
        if j.get("code"):
            log(f"서버 코드조회 성공: {j['code']} ({j.get('detail')})")
            return j["code"]
        log(f"서버 코드조회 미도착({attempt}/{retries}): {j.get('detail')} — 재시도")
        time.sleep(gap)
    return ""


def _return_to_kakao_passcode(serial: str, timeout: float = 30.0):
    """아웃룩을 읽고 난 뒤 카카오 패스코드 화면(Chrome 커스텀탭)으로 복귀.

    카카오 로그인은 kakaomap 이 띄운 Chrome 커스텀탭에서 진행되고,
    아웃룩은 별도 Chrome 탭이라 둘이 공존한다. 커스텀탭을 다시 앞으로
    가져오기 위해 여러 전략을 순서대로 시도하고, passcode 입력칸이
    보이는지로 성공을 판정한다.
    """
    import subprocess

    def adb(*a):
        adb_bin = os.path.join(ROOT, "platform-tools", "adb")
        if not os.path.isfile(adb_bin):
            adb_bin = "adb"
        return subprocess.run([adb_bin, "-s", serial, *a],
                              capture_output=True, text=True, timeout=25).stdout

    d = u2.connect(serial)

    def at_passcode():
        try:
            xml = d.dump_hierarchy()
        except Exception:
            return False
        return "passcode" in xml or "인증번호를 입력" in xml or "인증번호" in xml

    deadline = time.time() + timeout
    strat = 0
    while time.time() < deadline:
        if at_passcode():
            log("카카오 패스코드 화면 복귀 확인")
            return True
        # 전략 순환: 최근앱 토글 → kakaomap 앞으로 → 커스텀탭 액티비티 직접
        if strat % 3 == 0:
            adb("shell", "input", "keyevent", "187")  # APP_SWITCH
            time.sleep(1.5)
            adb("shell", "input", "keyevent", "187")  # 다시 눌러 이전 앱 토글
        elif strat % 3 == 1:
            adb("shell", "am", "start", "-n",
                "net.daum.android.map/com.kakao.map.main.view.MainActivity")
        else:
            adb("shell", "am", "start", "-n",
                "com.android.chrome/org.chromium.chrome.browser.customtabs.CustomTabActivity")
        strat += 1
        time.sleep(2.5)
    log("[!] 패스코드 화면 복귀 실패")
    return at_passcode()


def kakao_login_mode_watchers(d: u2.Device):
    """예열 중에는 '비로그인으로 시작하기' 를 누르면 안 되므로 최소한만 감시."""
    try:
        d.watcher.reset()
    except Exception:
        pass
    for i, t in enumerate(["허용 안함", "허용 안 함", "이번에는 허용 안함", "Don't allow", "Deny",
                           "오늘 그만보기", "다시 보지 않기", "지금은 아니에요", "나중에"]):
        d.watcher(f"pop_{i}").when(f'//*[@text="{t}"]|//*[@content-desc="{t}"]').click()
    d.watcher.start(interval=1.0)


if __name__ == "__main__":
    main()
