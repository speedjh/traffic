-- 기기관리(폰보드) + 유심 데이터 8GB/일 페이싱 + 기기별 작업 집계

ALTER TABLE devices ADD COLUMN label TEXT NOT NULL DEFAULT '';
ALTER TABLE devices ADD COLUMN model TEXT NOT NULL DEFAULT '';
ALTER TABLE devices ADD COLUMN battery INTEGER;
ALTER TABLE devices ADD COLUMN charging INTEGER NOT NULL DEFAULT 0;
ALTER TABLE devices ADD COLUMN screen_on INTEGER NOT NULL DEFAULT 0;
ALTER TABLE devices ADD COLUMN ip TEXT NOT NULL DEFAULT '';
ALTER TABLE devices ADD COLUMN carrier TEXT NOT NULL DEFAULT '';
ALTER TABLE devices ADD COLUMN daily_cap_mb INTEGER NOT NULL DEFAULT 8000;
ALTER TABLE devices ADD COLUMN paused INTEGER NOT NULL DEFAULT 0;
ALTER TABLE devices ADD COLUMN note TEXT NOT NULL DEFAULT '';
ALTER TABLE devices ADD COLUMN state TEXT NOT NULL DEFAULT '';
ALTER TABLE devices ADD COLUMN state_detail TEXT NOT NULL DEFAULT '';
ALTER TABLE devices ADD COLUMN screenshot TEXT;
ALTER TABLE devices ADD COLUMN screenshot_at TEXT;

-- 기기별 일일 데이터 사용량 (유심 카운터 누적)
CREATE TABLE IF NOT EXISTS device_usage (
  serial TEXT NOT NULL,
  date TEXT NOT NULL,                       -- KST 날짜
  bytes INTEGER NOT NULL DEFAULT 0,         -- 오늘 누적 모바일 데이터
  last_counter INTEGER,                     -- 마지막 기기 카운터(리셋 감지용)
  jobs_ok INTEGER NOT NULL DEFAULT 0,
  jobs_fail INTEGER NOT NULL DEFAULT 0,
  job_secs INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT,
  PRIMARY KEY (serial, date)
);

-- 기기 이벤트 로그 (화면꺼짐 복구/배터리/오프라인/한도 등)
CREATE TABLE IF NOT EXISTS device_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  serial TEXT NOT NULL,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON device_events(ts DESC);

-- 원격 명령 큐 (대시보드 → 워커)
CREATE TABLE IF NOT EXISTS device_commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  serial TEXT NOT NULL,
  cmd TEXT NOT NULL,
  args TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  done_at TEXT,
  result TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_cmd_pending ON device_commands(serial, status);

ALTER TABLE jobs ADD COLUMN bytes_used INTEGER;
ALTER TABLE jobs ADD COLUMN duration_secs INTEGER;
