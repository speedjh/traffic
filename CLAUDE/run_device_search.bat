@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

cd /d "%~dp0"

if exist "C:\LDPlayer\LDPlayer9\adb.exe" (
    set "PATH=C:\LDPlayer\LDPlayer9;%PATH%"
)

REM ============================================================
REM  카카오맵 자동화 — 실물 기기 / 일반트래픽 (--mode search)
REM  1사이클: 검색 → 업체 진입 → 업체명 클릭 → 홈 탭 1회
REM  길찾기 모드는 run_device.bat 사용
REM ============================================================

set "KEYWORD=영월 닭강정 가나닭강정"
set "PLACE=영월가나닭강정"
set "SERIAL=R3CY80RW96Y"
set "ROTATIONS=0"
REM ROTATIONS=0 이면 무한 반복, Ctrl+C 로 중단

REM 첫 테스트만: set "EXTRA_ARGS=--rotations 1 --no-data-toggle --no-clear"
set "EXTRA_ARGS="

echo.
echo [run_device_search] 카카오맵 실물 기기 — 일반트래픽
echo   KEYWORD= "%KEYWORD%"
echo   PLACE=   "%PLACE%"
echo   SERIAL=  %SERIAL%
echo   ROTATIONS= %ROTATIONS%  [0=무한]
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [오류] python 을 찾을 수 없습니다.
    goto :end
)

python -c "import uiautomator2" >nul 2>&1
if errorlevel 1 (
    echo [*] uiautomator2 설치 중...
    python -m pip install uiautomator2
    if errorlevel 1 (
        echo [오류] uiautomator2 설치 실패
        goto :end
    )
)

echo [*] 연결된 ADB 기기:
adb devices
echo.

python kakao_search_device.py "%KEYWORD%" "%PLACE%" ^
    --mode search ^
    --serial %SERIAL% ^
    --rotations %ROTATIONS% ^
    %EXTRA_ARGS%

set "EXIT_CODE=!ERRORLEVEL!"
echo.
if !EXIT_CODE! neq 0 (
    echo [종료] 오류 코드 !EXIT_CODE!
) else (
    echo [종료] 정상 완료
)

:end
pause
endlocal
