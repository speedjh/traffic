# -*- coding: utf-8 -*-
"""
멀티 디바이스 워커: API에서 job claim → 기존 run_*.sh / kakao 스크립트 실행 → report.

사용:
  python -m campaign_web.worker --serial R3CY80RW96Y
  python -m campaign_web.worker --serial R3CY80RW96Y --api http://127.0.0.1:8080 --dry-run
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from .config import LOG_DIR, ROOT_DIR
from .traffic import build_command, needs_account

try:
    from device_screen import ensure_screen_ready, keep_screen_on
except ImportError:
    import sys as _sys
    _sys.path.insert(0, str(ROOT_DIR))
    from device_screen import ensure_screen_ready, keep_screen_on

import sys as _sys2
if str(ROOT_DIR) not in _sys2.path:
    _sys2.path.insert(0, str(ROOT_DIR))
import device_stats as dstat

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _adb_bin() -> str:
    return dstat.ADB


def adb_device_online(serial: str) -> bool:
    """adb devices 에 serial 이 'device' 상태로 보이는지."""
    try:
        r = subprocess.run(
            [_adb_bin(), "devices"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return False
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == serial and parts[1] == "device":
            return True
    return False


class QueueWorker:
    def __init__(
        self,
        *,
        api: str,
        serial: str,
        worker_id: str,
        host: str,
        poll_secs: float = 3.0,
        dry_run: bool = False,
    ):
        self.api = api.rstrip("/")
        self.serial = serial
        self.worker_id = worker_id
        self.host = host
        self.poll_secs = poll_secs
        self.dry_run = dry_run
        self.busy_job_id: Optional[int] = None
        self.leased_account: Optional[Dict[str, Any]] = None
        self.paused = False
        self.budget: Dict[str, Any] = {}
        self.pending_events: List[Dict[str, str]] = []
        self.last_shot = 0.0
        self.state = "idle"
        self.state_detail = ""
        self.last_battery: Optional[int] = None

    def event(self, kind: str, message: str = ""):
        """대시보드 이벤트 로그 (다음 heartbeat 에 함께 전송)."""
        log(f"[event] {kind} {message}".rstrip())
        self.pending_events.append({"kind": kind, "message": message[:300]})

    def heartbeat(self):
        """상태·유심 데이터 카운터 보고 → 일시정지/데이터예산/원격명령 수신."""
        payload: Dict[str, Any] = {
            "serial": self.serial,
            "host": self.host,
            "worker_id": self.worker_id,
            "busy_job_id": self.busy_job_id,
            "state": self.state,
            "state_detail": self.state_detail,
        }
        try:
            payload.update(dstat.device_info(self.serial))
            payload["data_counter"] = dstat.data_counter(self.serial)
        except Exception as e:
            log(f"[!] 기기 상태 수집 실패: {e}")
        if self.pending_events:
            payload["events"] = self.pending_events[:10]
        try:
            r = httpx.post(f"{self.api}/api/devices/heartbeat", json=payload, timeout=15.0)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log(f"[!] heartbeat 실패: {e}")
            return
        # 전송 성공한 이벤트만 비움
        if self.pending_events:
            self.pending_events = self.pending_events[10:]
        self.paused = bool(data.get("paused"))
        self.budget = data.get("budget") or {}
        bat = payload.get("battery")
        if isinstance(bat, int):
            self.last_battery = bat
        if isinstance(bat, int) and bat <= 15 and not payload.get("charging"):
            self.event("battery_low", f"배터리 {bat}% · 충전 안 됨")
        for cmd in data.get("commands") or []:
            self.exec_command(cmd)

    # ── 원격 명령 (대시보드 → 기기) ────────────────────────────────────
    def exec_command(self, cmd: Dict[str, Any]):
        name = str(cmd.get("cmd") or "")
        cid = cmd.get("id")
        log(f"[*] 원격 명령: {name}")
        ok, result = True, ""
        try:
            if name == "wake":
                ensure_screen_ready(self.serial, log=log)
                dstat.clear_screen_blockers(self.serial, log=log)
            elif name == "home":
                subprocess.run([_adb_bin(), "-s", self.serial, "shell", "input", "keyevent", "KEYCODE_HOME"], timeout=15)
            elif name == "back":
                subprocess.run([_adb_bin(), "-s", self.serial, "shell", "input", "keyevent", "KEYCODE_BACK"], timeout=15)
            elif name == "screenshot":
                result = "업로드 " + ("성공" if self.upload_screenshot(force=True) else "실패")
            elif name == "screen_fix":
                result = dstat.clear_screen_blockers(self.serial, log=log) or "방해 요소 없음"
            elif name == "data_toggle":
                dstat.toggle_data(self.serial)
                result = "IP 변경 시도"
            elif name == "reboot":
                dstat.reboot(self.serial)
                result = "재부팅 명령 전송"
            elif name == "stop_job":
                result = "미지원(실행 중 작업은 타임아웃까지 대기)"
                ok = False
            else:
                ok, result = False, "알 수 없는 명령"
        except Exception as e:
            ok, result = False, str(e)[:200]
        if cid is not None:
            try:
                httpx.post(f"{self.api}/api/devices/{self.serial}/cmd/{cid}/done",
                           json={"ok": ok, "result": result}, timeout=10.0)
            except Exception:
                pass

    def upload_screenshot(self, force: bool = False) -> bool:
        """축소 캡처 업로드 (PC 회선 사용 — 유심 데이터와 무관)."""
        if not force and time.time() - self.last_shot < 45:
            return False
        self.last_shot = time.time()
        img = dstat.screenshot_jpeg_b64(self.serial)
        if not img:
            return False
        try:
            httpx.post(f"{self.api}/api/devices/{self.serial}/screenshot",
                       json={"image": img}, timeout=25.0)
            return True
        except Exception as e:
            log(f"[!] 캡처 업로드 실패: {e}")
            return False

    def data_used_bytes(self) -> Optional[int]:
        try:
            return dstat.data_counter(self.serial)
        except Exception:
            return None

    def claim(self) -> Optional[Dict[str, Any]]:
        r = httpx.post(
            f"{self.api}/api/worker/claim",
            json={
                "serial": self.serial,
                "worker_id": self.worker_id,
                "host": self.host,
            },
            timeout=30.0,
        )
        r.raise_for_status()
        return r.json().get("job")

    def report(self, job_id: int, status: str, error: str = "", log_path: str = "",
               bytes_used: Optional[int] = None, duration_secs: Optional[float] = None):
        httpx.post(
            f"{self.api}/api/worker/jobs/{job_id}/report",
            json={
                "worker_id": self.worker_id,
                "serial": self.serial,
                "status": status,
                "error_message": error or None,
                "log_path": log_path or None,
                "bytes_used": bytes_used,
                "duration_secs": duration_secs,
            },
            timeout=30.0,
        ).raise_for_status()

    def job_runnable(self, job_id: int) -> bool:
        try:
            r = httpx.get(f"{self.api}/api/worker/jobs/{job_id}/runnable", timeout=10.0)
            r.raise_for_status()
            return bool(r.json().get("runnable"))
        except Exception as e:
            log(f"[!] runnable 확인 실패: {e}")
            return True

    def mark_start(self, job_id: int) -> bool:
        try:
            r = httpx.post(f"{self.api}/api/worker/jobs/{job_id}/start", timeout=10.0)
            if r.status_code == 409:
                return False
            r.raise_for_status()
            return True
        except Exception as e:
            log(f"[!] start 표시 실패: {e}")
            return True

    def release_pending(self, job_id: int, reason: str = "device offline"):
        """실행 불가 시 fail 소진 대신 pending 으로 되돌림."""
        try:
            httpx.post(
                f"{self.api}/api/worker/jobs/{job_id}/release",
                json={
                    "worker_id": self.worker_id,
                    "serial": self.serial,
                    "reason": reason,
                },
                timeout=15.0,
            ).raise_for_status()
            log(f"[*] job#{job_id} pending 환원 ({reason})")
        except Exception as e:
            log(f"[!] job#{job_id} release 실패: {e}")

    # ── 카카오 계정 풀 ────────────────────────────────────────────────
    def lease_account(self, job_id: int) -> Optional[Dict[str, Any]]:
        """투입이 가장 오래된 계정 1개를 빌려온다. 로테이션마다 계정이 바뀐다."""
        try:
            r = httpx.post(
                f"{self.api}/api/accounts/kakao/lease",
                json={"serial": self.serial, "worker_id": self.worker_id, "job_id": job_id},
                timeout=20.0,
            )
            r.raise_for_status()
            return r.json().get("account")
        except Exception as e:
            log(f"[!] 계정 리스 실패: {e}")
            return None

    def report_account(self, account_id: int, result: str, note: str = ""):
        """계정 반납 + 결과 반영 (여기서 '마지막 투입' 시각이 갱신된다)."""
        try:
            httpx.post(
                f"{self.api}/api/accounts/kakao/{account_id}/report",
                json={"serial": self.serial, "result": result, "note": note[:300]},
                timeout=20.0,
            ).raise_for_status()
        except Exception as e:
            log(f"[!] 계정 반납 실패(id={account_id}): {e}")

    @staticmethod
    def _login_result_from_log(log_path) -> tuple:
        """스크립트 로그에서 LOGIN_RESULT= 줄을 읽어 (result, detail)."""
        try:
            body = log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return "", ""
        for line in reversed(body.splitlines()):
            if line.startswith("LOGIN_RESULT="):
                rest = line[len("LOGIN_RESULT="):].strip()
                parts = rest.split(" ", 1)
                return parts[0], (parts[1] if len(parts) > 1 else "")
        return "", ""

    def settle_account(self, job_status: str, log_path: str):
        """job 종료 후 리스한 계정을 반납하고 로그인 결과를 반영한다.

        스크립트가 남긴 LOGIN_RESULT 를 우선 쓰고, 없으면 job 결과로 대체한다.
        로그인은 됐는데 트래픽만 실패한 경우 계정을 벌주지 않는다.
        """
        account = self.leased_account
        self.leased_account = None
        if not account:
            return
        result, detail = ("", "")
        if log_path:
            result, detail = self._login_result_from_log(Path(log_path))
        if not result:
            if job_status == "success":
                result = "success"
            elif job_status == "release":
                result = "login_failed"
                detail = "기기 문제로 미실행"
            else:
                result = "login_failed"
                detail = "로그인 결과 기록 없음"
        elif result == "success" and job_status != "success":
            # 로그인은 성공, 트래픽에서 실패 → 계정 탓이 아님
            result = "traffic_failed"
        log(f"[*] 계정 반납: {account['email']} → {result} {detail}".rstrip())
        self.report_account(account["id"], result, detail)

    def run_job(self, job: Dict[str, Any]) -> tuple:
        """returns (status, error, log_path, bytes_used, duration_secs)"""
        started = time.time()
        bytes_before = self.data_used_bytes()

        def done(status: str, err: str = "", lp: str = "") -> tuple:
            after = self.data_used_bytes()
            used = None
            if bytes_before is not None and after is not None and after >= bytes_before:
                used = after - bytes_before
            return status, err, lp, used, round(time.time() - started, 1)

        job_id = job["id"]
        traffic = job["traffic_type"]
        if not adb_device_online(self.serial):
            log(f"[!] adb offline — job#{job_id} 실행 보류")
            return done("release", "device offline")
        try:
            ensure_screen_ready(self.serial, log=log)
        except Exception as e:
            log(f"[!] 화면 준비 실패: {e}")
        if not adb_device_online(self.serial):
            return done("release", "device offline")
        if not self.job_runnable(job_id):
            log(f"[*] job#{job_id} 캠페인 중단 — 실행 스킵")
            return done("failed", "campaign paused")
        if not self.mark_start(job_id):
            log(f"[*] job#{job_id} 큐에서 제외됨 — 실행 스킵")
            return done("failed", "job reclaimed")
        account: Optional[Dict[str, Any]] = None
        if needs_account(traffic):
            account = self.lease_account(job_id)
            if not account:
                log(f"[!] 사용 가능한 카카오 계정 없음 — job#{job_id} 보류")
                return done("release", "no kakao account available")
            account["api"] = self.api
            self.leased_account = account
            log(f"[*] 계정 리스: {account['email']} (id={account['id']})")

        cmd = build_command(
            traffic,
            keyword=job.get("keyword") or "",
            serial=self.serial,
            place_url=job.get("place_url") or "",
            place_name=job.get("place_name") or "",
            params=job.get("params") or {},
            account=account,
        )
        log_path = LOG_DIR / f"job_{job_id}_{self.serial}.log"
        shown = list(cmd)
        if "--kakao-pw" in shown:
            shown[shown.index("--kakao-pw") + 1] = "***"
        log(f"[*] job#{job_id} {traffic} queue={job.get('queue_seq')} → {' '.join(shown)}")

        if self.dry_run:
            log(f"[dry-run] skip execute, pretend success")
            log_path.write_text(f"DRY RUN\n{' '.join(cmd)}\n", encoding="utf-8")
            return done("success", "", str(log_path))

        env = os.environ.copy()
        env["PATH"] = f"{ROOT_DIR / 'platform-tools'}{os.pathsep}" + env.get("PATH", "")
        # Windows 기본 cp949 로는 한글 로그 출력이 깨지거나 죽는다
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(f"$ {' '.join(cmd)}\n\n")
            lf.flush()
            # start_new_session: timeout 시 셸+python 자식 전체 종료 (고아 hang 방지)
            proc: Optional[subprocess.Popen] = None
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(ROOT_DIR),
                    env=env,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    rc = proc.wait(timeout=60 * 8)  # hang 방지 (팝업 루프 등) — 8분
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, 15)
                    except Exception:
                        proc.kill()
                    try:
                        proc.wait(timeout=10)
                    except Exception:
                        try:
                            os.killpg(proc.pid, 9)
                        except Exception:
                            pass
                    return done("failed", "timeout", str(log_path))
                ok = rc == 0
                # 기기 단절 traceback 은 fail 소진 대신 재시도
                if not ok:
                    try:
                        body = log_path.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        body = ""
                    if (
                        "not online" in body
                        or "device offline" in body.lower()
                        or ("device '" in body and "not found" in body)
                    ):
                        return done("release", "device offline", str(log_path))
                return done("success" if ok else "failed", "" if ok else f"exit={rc}", str(log_path))
            except Exception as e:
                if proc and proc.poll() is None:
                    try:
                        os.killpg(proc.pid, 9)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                return done("failed", str(e), str(log_path))

    # ── 화면 감시 스레드: 꺼지면 강제로 켜고, 방해 창은 치운다 ──────────
    def screen_watchdog(self, stop: threading.Event):
        while not stop.wait(25.0):
            try:
                if not adb_device_online(self.serial):
                    continue
                info = dstat.device_info(self.serial)
                if not info.get("screen_on"):
                    self.event("screen_off", "화면 꺼짐 감지 → 강제 켜기")
                    keep_screen_on(self.serial)
                    ensure_screen_ready(self.serial, log=log)
                blocked = dstat.clear_screen_blockers(self.serial, log=log)
                if blocked:
                    self.event("screen_blocker", blocked)
                self.upload_screenshot()
            except Exception as e:
                log(f"[!] 화면 감시 오류: {e}")

    # ── 유심 데이터 예산 ──────────────────────────────────────────────
    def budget_hold_secs(self) -> float:
        """예산 초과면 대기할 초. 0 이면 작업 가능."""
        b = self.budget or {}
        if not b.get("over"):
            return 0.0
        return float(min(max(b.get("wait_secs") or 60, 30), 600))

    def loop(self):
        log(f"[*] worker start serial={self.serial} id={self.worker_id} api={self.api}")
        offline_logged = False
        try:
            if adb_device_online(self.serial):
                dstat.disable_pocket_mode(self.serial)
                keep_screen_on(self.serial)
                ensure_screen_ready(self.serial, log=log)
                dstat.clear_screen_blockers(self.serial, log=log)
                dstat.dim_screen(self.serial)
                log("[+] 화면 유지 설정 적용 (USB stay-on + 오동작방지 해제 + 밝기 최소)")
                self.event("worker_start", f"{self.host} 워커 기동")
            else:
                log("[!] adb 기기 미연결 — USB 디버깅/파일전송 모드 확인 필요")
        except Exception as e:
            log(f"[!] 화면 유지 초기화 실패: {e}")

        stop_wd = threading.Event()
        threading.Thread(target=self.screen_watchdog, args=(stop_wd,),
                         name="screen-watchdog", daemon=True).start()
        hold_logged = ""
        while True:
            try:
                self.heartbeat()
                if not adb_device_online(self.serial):
                    if not offline_logged:
                        log("[!] adb offline — claim 중지 (실패 소진 방지). 폰 USB 연결·디버깅 허용 후 대기")
                        self.event("device_offline", "adb 연결 끊김 — 작업 중지")
                        offline_logged = True
                    self.state, self.state_detail = "offline", "adb 연결 끊김"
                    time.sleep(max(self.poll_secs, 8.0))
                    continue
                if offline_logged:
                    log("[+] adb 재연결 감지")
                    self.event("device_online", "adb 재연결 — 작업 재개")
                    offline_logged = False
                    try:
                        dstat.disable_pocket_mode(self.serial)
                        keep_screen_on(self.serial)
                        ensure_screen_ready(self.serial, log=log)
                    except Exception as e:
                        log(f"[!] 재연결 후 화면 준비 실패: {e}")

                bat = self.last_battery
                if bat is not None and bat <= 10:
                    if hold_logged != "battery":
                        log(f"[*] 배터리 {bat}% — 방전 방지로 작업 보류 (20% 회복 시 재개)")
                        self.event("battery_hold", f"배터리 {bat}% — 작업 보류")
                        hold_logged = "battery"
                    self.state, self.state_detail = "hold", f"배터리 부족 {bat}%"
                    time.sleep(120.0)
                    continue
                if hold_logged == "battery" and bat is not None and bat >= 20:
                    self.event("battery_resume", f"배터리 {bat}% — 작업 재개")
                    hold_logged = ""

                if self.paused:
                    if hold_logged != "paused":
                        log("[*] 대시보드에서 일시정지됨 — 대기")
                        hold_logged = "paused"
                    self.state, self.state_detail = "hold", "대시보드 일시정지"
                    time.sleep(15.0)
                    continue

                hold = self.budget_hold_secs()
                if hold:
                    b = self.budget
                    key = "cap" if b.get("exhausted") else "pace"
                    if hold_logged != key:
                        log(f"[*] 유심 데이터 {b.get('used_mb')}MB / {b.get('cap_mb')}MB "
                            f"(지금 허용 {b.get('pace_mb')}MB) → {int(hold)}초 대기")
                        if key == "cap":
                            self.event("data_cap", f"1일 한도 도달 {b.get('used_mb')}MB — 자정까지 정지")
                        hold_logged = key
                    self.state = "hold"
                    self.state_detail = ("1일 데이터 한도 도달" if key == "cap"
                                         else f"데이터 페이스 대기 ({b.get('used_mb')}/{b.get('pace_mb')}MB)")
                    time.sleep(min(hold, 60.0))
                    continue
                hold_logged = ""

                job = self.claim()
                if not job:
                    self.state, self.state_detail = "idle", "대기 중(작업 없음)"
                    time.sleep(self.poll_secs)
                    continue
                self.busy_job_id = job["id"]
                self.state = "running"
                self.state_detail = f"{job.get('campaign_name','')} · {job.get('traffic_type','')}"
                # job 실행 중에도 heartbeat 유지 (워치독이 stale 로 죽이지 않게)
                stop_hb = threading.Event()

                def _hb_while_busy():
                    while not stop_hb.wait(25.0):
                        self.heartbeat()

                hb_thread = threading.Thread(
                    target=_hb_while_busy, name="job-heartbeat", daemon=True
                )
                hb_thread.start()
                try:
                    self.heartbeat()
                    status, err, log_path, used, secs = self.run_job(job)
                finally:
                    stop_hb.set()
                    hb_thread.join(timeout=2.0)
                self.settle_account(status, log_path)
                if status == "release":
                    self.release_pending(job["id"], err or "device offline")
                else:
                    self.report(job["id"], status, err, log_path, used, secs)
                    mb = f"{used/1048576:.1f}MB" if used is not None else "?"
                    log(f"[+] job#{job['id']} → {status} ({secs:.0f}s, 데이터 {mb})")
                self.busy_job_id = None
            except KeyboardInterrupt:
                log("[*] 중단")
                stop_wd.set()
                break
            except Exception as e:
                log(f"[!] loop error: {e}")
                try:
                    self.settle_account("failed", "")
                except Exception:
                    pass
                self.busy_job_id = None
                time.sleep(self.poll_secs)


def main(argv: Optional[List[str]] = None):
    ap = argparse.ArgumentParser(description="Campaign queue device worker")
    ap.add_argument("--serial", required=True, help="adb device serial")
    ap.add_argument("--api", default=os.environ.get("CAMPAIGN_API", "http://127.0.0.1:8080"))
    ap.add_argument("--worker-id", default="", help="default: host-serial")
    ap.add_argument("--poll", type=float, default=3.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    host = socket.gethostname()
    worker_id = args.worker_id or f"{host}-{args.serial}"
    w = QueueWorker(
        api=args.api,
        serial=args.serial,
        worker_id=worker_id,
        host=host,
        poll_secs=args.poll,
        dry_run=args.dry_run,
    )
    w.loop()


if __name__ == "__main__":
    main()
