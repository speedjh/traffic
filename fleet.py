# -*- coding: utf-8 -*-
"""폰보드형 워커 감독자 — 실제 폰(에뮬레이터 제외)마다 워커 1개를 띄우고 죽으면 되살린다.

  python fleet.py                      # 연결된 실기기 전부
  python fleet.py --serials A,B        # 지정 기기만
  python fleet.py --api https://...    # 서버 주소

특징
- adb devices 를 30초마다 다시 보고 새로 꽂힌 기기의 워커를 자동 기동
- 워커 프로세스가 죽으면 10초 후 재시작 (무한 재시도)
- LDPlayer 등 emulator-* 시리얼은 제외 (유심이 없어 데이터/트래픽 대상 아님)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "campaign_web" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
ADB = str(ROOT / "platform-tools" / "adb.exe") if (ROOT / "platform-tools" / "adb.exe").is_file() else "adb"


def log(msg: str):
    line = f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_DIR / "fleet.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def physical_devices() -> list[str]:
    try:
        r = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=20)
    except Exception as e:
        log(f"[!] adb devices 실패: {e}")
        return []
    out = []
    for line in (r.stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device" and not parts[0].startswith("emulator-"):
            out.append(parts[0])
    return out


def kill_orphan_workers() -> None:
    """이전에 떠 있던 워커/트래픽 프로세스 정리 — 중복 claim 방지 (fleet 재시작 대비)."""
    me = os.getpid()
    ps = (
        "Get-CimInstance Win32_Process -Filter \"name like 'python%'\" | "
        "Where-Object { $_.CommandLine -match 'campaign_web.worker|hamman_device|hamman_v2_device|kakao_search_device' } | "
        f"Where-Object {{ $_.ProcessId -ne {me} }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }}"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=60)
        killed = [x for x in (r.stdout or "").split() if x.isdigit()]
        if killed:
            log(f"[*] 기존 워커 프로세스 정리: {', '.join(killed)}")
    except Exception as e:
        log(f"[!] 기존 워커 정리 실패: {e}")


def start_worker(serial: str, api: str) -> subprocess.Popen:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               PATH=f"{ROOT / 'platform-tools'}{os.pathsep}" + os.environ.get("PATH", ""))
    lf = open(LOG_DIR / f"worker_{serial}.log", "a", encoding="utf-8", buffering=1)
    lf.write(f"\n===== worker start {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
    p = subprocess.Popen(
        [sys.executable, "-m", "campaign_web.worker", "--serial", serial, "--api", api],
        cwd=str(ROOT), env=env, stdout=lf, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    log(f"[+] 워커 기동 {serial} (pid={p.pid})")
    return p


def stale_heartbeat_serials(api: str, max_age: float = 300.0) -> set[str]:
    """서버 기준 하트비트가 끊긴 기기 — 워커 프로세스는 살아 있지만 멈춘 경우 탐지."""
    try:
        import httpx
        from datetime import datetime, timezone
        r = httpx.get(f"{api.rstrip('/')}/api/devices", timeout=15.0)
        r.raise_for_status()
        out = set()
        now = datetime.now(timezone.utc)
        for d in r.json():
            hb = d.get("last_heartbeat")
            if not hb:
                continue
            try:
                t = datetime.fromisoformat(hb).replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if (now - t).total_seconds() > max_age:
                out.add(d["serial"])
        return out
    except Exception as e:
        log(f"[!] 하트비트 점검 실패: {e}")
        return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("API", "https://traffic-cp.hywogur0327.workers.dev"))
    ap.add_argument("--serials", default="", help="쉼표 구분. 미지정 시 연결된 실기기 전부")
    args = ap.parse_args()

    fixed = [s.strip() for s in args.serials.split(",") if s.strip()]
    procs: dict[str, subprocess.Popen] = {}
    log(f"[*] fleet 시작 api={args.api}")
    kill_orphan_workers()
    tick = 0
    while True:
        try:
            tick += 1
            wanted = fixed or physical_devices()
            # 5분 넘게 하트비트가 없는 기기의 워커는 멈춘 것으로 보고 재시작
            stalled = stale_heartbeat_serials(args.api) if tick % 4 == 0 else set()
            for serial in wanted:
                p = procs.get(serial)
                if p is None:
                    procs[serial] = start_worker(serial, args.api)
                elif p.poll() is not None:
                    log(f"[!] 워커 종료 감지 {serial} (rc={p.returncode}) → 10초 후 재시작")
                    time.sleep(10)
                    procs[serial] = start_worker(serial, args.api)
                elif serial in stalled:
                    log(f"[!] 워커 응답 없음(하트비트 5분+) {serial} → 강제 재시작")
                    try:
                        p.kill()
                        p.wait(timeout=15)
                    except Exception:
                        pass
                    procs[serial] = start_worker(serial, args.api)
            time.sleep(30)
        except KeyboardInterrupt:
            log("[*] fleet 중단 — 워커 종료")
            for p in procs.values():
                try:
                    p.terminate()
                except Exception:
                    pass
            break
        except Exception as e:
            log(f"[!] fleet 오류: {e}")
            time.sleep(15)


if __name__ == "__main__":
    main()
