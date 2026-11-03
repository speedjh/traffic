-- 긴급 알림 재알림: 문제가 계속되면 마지막 알림 후 일정 시간마다 다시 알린다
ALTER TABLE alert_state ADD COLUMN notified_at TEXT;
