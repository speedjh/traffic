-- 텔레그램 알림 — 같은 문제를 반복 알리지 않도록 상태 보관 (key = '<serial>:<kind>')

CREATE TABLE IF NOT EXISTS alert_state (
  key TEXT PRIMARY KEY,
  active INTEGER NOT NULL DEFAULT 0,
  since TEXT,
  updated_at TEXT
);

-- 기기별 최근 종료 작업 조회 (연속 실패 판정)
CREATE INDEX IF NOT EXISTS ix_jobs_dev_fin ON jobs(device_serial, finished_at);
