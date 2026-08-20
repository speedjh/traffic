@echo off
REM ============================================================
REM  campaign_web 다중 디바이스 워커 런처 (Windows)
REM  연결된 모든 adb 기기마다 워커 1개를 띄워 서버에서 job 할당받아 실행.
REM
REM  준비: 1) 이 저장소 clone   2) python + 워커 의존성 설치
REM        (pip install -r requirements-worker.txt)
REM        3) platform-tools(adb)를 이 폴더에 두거나 PATH에 등록
REM  실행: run_workers.bat  (또는  set API=https://서버주소 && run_workers.bat)
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "PATH=%~dp0platform-tools;%PATH%"

REM --- 서버 주소 (웹/큐). 기본값을 여기 넣거나 실행 전 set API=... 로 지정 ---
if "%API%"=="" set "API=http://127.0.0.1:8080"

REM --- 파이썬 실행자 (venv 있으면 우선) ---
if exist "%~dp0.venv\Scripts\python.exe" (
  set "PY=%~dp0.venv\Scripts\python.exe"
) else (
  where python >nul 2>nul && ( set "PY=python" ) || ( set "PY=py" )
)

echo [*] 서버 API : %API%
echo [*] 파이썬   : %PY%
echo [*] 연결 기기 스캔...
adb start-server >nul 2>nul

set COUNT=0
for /f "skip=1 tokens=1,2" %%A in ('adb devices') do (
  if "%%B"=="device" (
    set /a COUNT+=1
    echo     - 워커 시작: %%A  ^-^>  %API%
    start "worker %%A" cmd /k "%PY% -m campaign_web.worker --serial %%A --api %API%"
    REM 동시 기동 충돌 방지용 약간의 간격
    ping -n 2 127.0.0.1 >nul
  )
)

if %COUNT%==0 (
  echo [!] 연결된 adb 기기가 없습니다. USB 디버깅/연결 확인 후 다시 실행하세요.
) else (
  echo [+] 워커 %COUNT%개 기동 완료. 각 창에서 로그 확인.
)
endlocal
pause
