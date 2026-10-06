-- 캠페인 변경 이력 (키워드·2차 키워드·일유입량·일수·체류·버전·상태 등 항목별 이전값→변경값)
CREATE TABLE IF NOT EXISTS campaign_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id INTEGER NOT NULL,
  changed_at TEXT NOT NULL DEFAULT (datetime('now')),
  action TEXT NOT NULL,
  field TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  old_value TEXT,
  new_value TEXT,
  source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_hist_camp ON campaign_history(campaign_id, changed_at);
