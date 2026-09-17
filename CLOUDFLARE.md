# Cloudflare 컨트롤플레인 정보 (traffic-cp)

트래픽 시스템의 웹/API/큐/계정/캠페인/아웃룩 코드리더가 Cloudflare에 있음.
폰 워커(트래픽 실행)만 실제 PC에서 이 서버에 붙어 동작.

## 접속
- **대시보드/API URL**: https://traffic-cp.hywogur0327.workers.dev
  - `/` 대시보드, `/campaigns` 캠페인, `/accounts` 카카오 계정
- 계정: Cloudflare (email hywogur0327@gmail.com) — `npx wrangler whoami`

## 리소스 (wrangler.jsonc 에도 기록됨)
- Worker 이름: `traffic-cp`  (코드: `traffic-cp/src/`)
- **D1 데이터베이스**: 이름 `traffic-cp`, id `b8ea6672-5785-446f-8abb-9f70083c122c`, region APAC
- 바인딩: `DB`(D1), `BROWSER`(Browser Rendering — 아웃룩 코드리더)

## 배포 / 운영 명령 (traffic-cp 폴더에서)
```
npm install                                   # 최초 1회
npx wrangler deploy                           # 코드 배포
npx wrangler d1 migrations apply traffic-cp --remote   # 스키마 변경 적용
npx wrangler d1 execute traffic-cp --remote --command "SELECT ..."  # D1 직접 조회
npx wrangler tail traffic-cp                  # 실시간 로그
```

## 데이터 이전 (Mac SQLite → D1)
- 계정: `POST /api/accounts/kakao/import-json`  {accounts:[...]}
- 캠페인(설정만, 잡 미생성): `POST /api/campaigns/import-json`  {campaigns:[...]}
- 캠페인 활성화(오늘부터 잡 생성): `POST /api/campaigns/{id}/activate`
- 토큰 붙여넣기: `POST /api/accounts/kakao/tokens/paste`  {text:"카카오메일----...----client_id----refresh_token"}

## 현재 데이터 (2026-08-20 기준)
- 카카오 계정 **61개** (전부 아웃룩, 할당 = 투입 오래된 순)
- 캠페인 **30개** 설정 이전됨 (kakao_search 5, hamman_find 등 — 전부 paused; 활성화 시 큐 생성)
  - ※ hamman/daum/gs 는 .sh 스크립트라 Windows 워커 실행 불가 → 카카오 트래픽(kakao_search) 위주

## 워커 연결 (다른 PC)
```
set API=https://traffic-cp.hywogur0327.workers.dev
run_workers.bat        # 연결된 모든 adb 기기마다 워커 기동
```
아웃룩 코드는 서버(Cloudflare Browser Rendering)가 읽어 워커에 반환 — 폰은 카카오만.

## 주의
- 보안: 현재 대시보드 인증 없음(공개 URL). 필요 시 Cloudflare Access 추가 권장.
- Browser Rendering 동시 2개 제한 → 대량 코드조회 시 큐잉 가능.

## 기기관리(폰보드) — 2026-09-18 추가
- **기기관리 화면**: https://traffic-cp.hywogur0327.workers.dev/devices
  - 기기별 실시간 화면 캡처(45초 간격), 배터리/충전/화면 상태, 오늘 데이터 사용량 게이지,
    작업 성공·실패, 건당 평균 데이터, 남은 한도로 가능한 작업 수, 원격 버튼
    (화면켜기·홈·뒤로·캡처·IP변경·일시정지·한도수정·재부팅)
  - 화면 캡처는 워커 PC 회선으로 업로드 → **유심 데이터를 쓰지 않음**
- **유심 1일 데이터 한도**: `devices.daily_cap_mb` (기본 8000MB, 현재 3대 7700MB로 운영)
  - 자정~자정 24시간에 **고르게 분배**: 시각 t 까지 누적 허용량 = 한도 × (t/24h + 2%)
  - 초과 시 워커가 claim 을 멈추고 대기 → 특정 시간에 몰아쓰지 않음
  - 한도의 97% 도달 시 자정까지 완전 정지
  - 사용량은 기기 `/proc/net/dev` 의 rmnet 카운터 누적(=유심 사용량)으로 측정
- **기기별 작업 집계**: `GET /api/devices/stats?date=YYYY-MM-DD` (기기 × 캠페인 성공/실패/데이터)
- **이벤트 로그**: `GET /api/events` — 화면 꺼짐 복구, 오동작방지 필터 해제, 배터리 부족,
  adb 단절/복구, 데이터 한도 도달 등
- **캠페인 균등 분배**: claim 시 오늘 진행률(처리/일유입량)이 가장 낮은 캠페인을 우선 배정
  (예전엔 큐 순서대로라 앞 캠페인만 소화)

### 실행 (워커 PC)
```
python fleet.py              # 실기기(에뮬레이터 제외) 전부에 워커 1개씩, 죽으면 자동 재시작
                             # 하트비트 5분 이상 끊기면 해당 워커 강제 재시작
                             # 기동 시 기존 워커 프로세스 정리 → 중복 claim 방지
```

### 실측 (2026-09-18, 함많찾/갤럭시 3대)
- 1건당 약 **30~60MB**, 소요 **3~4분** → 유심 8GB 기준 **기기당 하루 약 150~250건**
- 3대 합계 하루 약 **450~700건** (현재 활성 캠페인 요구량 1,420건/일보다 적으므로
  캠페인 균등 분배로 나눠 소화됨)
