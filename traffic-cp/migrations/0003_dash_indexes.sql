-- 옛 대시보드(캠페인별 오늘 진행) 집계용 인덱스
CREATE INDEX IF NOT EXISTS ix_jobs_camp_day ON jobs(campaign_id, schedule_date, status);
CREATE INDEX IF NOT EXISTS ix_jobs_camp_fin ON jobs(campaign_id, status, finished_at);
CREATE INDEX IF NOT EXISTS ix_jobs_fin ON jobs(status, finished_at);
