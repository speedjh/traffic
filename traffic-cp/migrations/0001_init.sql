-- traffic-cp 컨트롤플레인 스키마 (campaign_web 파이썬 모델 미러링)

CREATE TABLE IF NOT EXISTS kakao_accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE,
  password TEXT NOT NULL DEFAULT '',
  nickname TEXT NOT NULL DEFAULT '',
  auth_method TEXT NOT NULL DEFAULT '',
  mail_email TEXT NOT NULL DEFAULT '',
  mail_password TEXT NOT NULL DEFAULT '',
  oauth_client_id TEXT NOT NULL DEFAULT '',
  oauth_refresh_token TEXT NOT NULL DEFAULT '',
  oauth_status TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  login_state TEXT NOT NULL DEFAULT 'unknown',
  daily_limit INTEGER NOT NULL DEFAULT 3,
  used_today INTEGER NOT NULL DEFAULT 0,
  used_date TEXT,
  total_used INTEGER NOT NULL DEFAULT 0,
  fail_streak INTEGER NOT NULL DEFAULT 0,
  last_used_at TEXT,
  last_result TEXT NOT NULL DEFAULT '',
  leased_by TEXT,
  leased_at TEXT,
  lease_job_id INTEGER,
  warmed_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_acc_pick ON kakao_accounts(status, last_used_at);

CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  traffic_type TEXT NOT NULL,
  keyword TEXT NOT NULL DEFAULT '',
  place_url TEXT NOT NULL DEFAULT '',
  place_name TEXT NOT NULL DEFAULT '',
  daily_quota INTEGER NOT NULL,
  start_date TEXT NOT NULL,
  days INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  params_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id INTEGER NOT NULL,
  schedule_date TEXT NOT NULL,
  queue_seq INTEGER NOT NULL,
  scheduled_at TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  device_serial TEXT,
  worker_id TEXT,
  leased_at TEXT,
  started_at TEXT,
  finished_at TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_jobs_claim ON jobs(status, schedule_date, queue_seq);
CREATE INDEX IF NOT EXISTS ix_jobs_campaign ON jobs(campaign_id);

CREATE TABLE IF NOT EXISTS devices (
  serial TEXT PRIMARY KEY,
  host TEXT NOT NULL DEFAULT '',
  worker_id TEXT NOT NULL DEFAULT '',
  last_heartbeat TEXT,
  busy_job_id INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
