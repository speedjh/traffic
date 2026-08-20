@echo off
REM 단일 기기 워커:  run_worker.bat <serial>   (API는 환경변수 또는 아래 기본값)
setlocal
cd /d "%~dp0"
set "PATH=%~dp0platform-tools;%PATH%"
if "%API%"=="" set "API=https://traffic-cp.hywogur0327.workers.dev"
if exist "%~dp0.venv\Scripts\python.exe" ( set "PY=%~dp0.venv\Scripts\python.exe" ) else ( set "PY=python" )
if "%~1"=="" ( echo 사용법: run_worker.bat ^<serial^> & pause & exit /b 1 )
echo [*] 워커 시작: %~1 -> %API%
"%PY%" -m campaign_web.worker --serial %~1 --api %API%
pause
