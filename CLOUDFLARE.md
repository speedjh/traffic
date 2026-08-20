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
