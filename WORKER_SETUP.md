# 다중 디바이스 워커 셋업 (Windows PC)

구조: **중앙 서버(웹+큐+아웃룩 코드리더)** 1대 + **워커 PC(여러 대 가능, 각 PC에 여러 기기)**.
워커 PC는 서버에서 job을 할당받아 자기 기기들로 트래픽을 실행한다. 코드(카카오 추가인증)는
서버가 아웃룩 메일함에서 읽어 워커에 내려준다 — 워커 PC/폰은 아웃룩에 접근하지 않는다.

```
[워커 PC들] --run_workers.bat--> 기기별 워커 --HTTP--> [중앙 서버 campaign_web]
   (adb 기기 N)                    claim/실행           (큐/DB/대시보드/아웃룩 CDP 코드리더)
```

## 중앙 서버 (1대, 폰 없어도 됨 / 이 Mac)
```
./run_campaign_web.sh              # 웹+큐 (기본 :8080)
# 외부 워커가 붙게 하려면 공개 주소 필요:
#  - LAN 이면 서버 PC의 IP:8080
#  - 인터넷이면 cloudflared 터널 URL (아래)
```
아웃룩 코드리더(CDP)는 서버에서 돈다 → 서버 PC에 Chrome 필요.

## 워커 PC (Windows)
1) 저장소 clone
2) 파이썬 설치 후 워커 의존성:
   ```
   pip install -r requirements-worker.txt
   ```
   (uiautomator2 최초 실행 시 기기에 atx-agent 설치됨)
3) `platform-tools`(adb)를 저장소 폴더에 두거나 PATH 등록
4) 기기 USB 디버깅 ON + `adb devices` 로 잡히는지 확인
5) 서버 주소 지정 후 실행:
   ```
   set API=https://<서버-공개주소>
   run_workers.bat
   ```
   → 연결된 **모든 기기마다 워커 창**이 뜨고, 각자 서버에서 job을 할당받아 실행.

## 동작 요약
- 워커는 3초마다 서버에 claim → 할당된 캠페인 job 실행
- 카카오 로그인 트래픽(kakao_search): 기기에서 로그인 → 추가인증 뜨면
  서버 `/api/accounts/kakao/{id}/fetch-code` 호출 → 서버가 CDP로 코드 읽어 반환 → 기기 입력
- 계정 할당은 서버가 **투입 오래된 순**으로 배분(로테이션마다 교체)

## 주의
- 함많찾/다음/GS 트래픽은 bash(.sh) 스크립트라 Windows에서 직접 실행 불가.
  Windows 워커는 **카카오 트래픽(kakao_search/route)** 용. (함많찾은 macOS/Linux 워커에서)
