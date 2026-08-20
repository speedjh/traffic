# -*- coding: utf-8 -*-
"""캠페인 → 일자별 Job 펼침 + 라운드로빈 인터리브."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, datetime, timedelta, time
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import case, delete, select
from sqlalchemy.orm import Session

from .config import WINDOW_END_HOUR, WINDOW_START_HOUR
from .models import Campaign, CampaignDay, Device, Job, utcnow


def date_range(start: date, days: int) -> List[date]:
    days = max(int(days), 1)
    return [start + timedelta(days=i) for i in range(days)]


def interleave_round_robin(campaign_buckets: Dict[int, Sequence[int]]) -> List[int]:
    """
    campaign_id → [job placeholders...] 를 A1,B1,C1,A2,B2... 순으로 병합.
    값이 다른 캠페인 id 리스트를 반환 (순서가 곧 큐 순서).
    """
    queues: List[Deque[int]] = [
        deque(bucket) for _, bucket in sorted(campaign_buckets.items(), key=lambda x: x[0])
    ]
    # Preserve insertion fairness: also accept unsorted by iterating original ids
    ordered_ids = list(campaign_buckets.keys())
    queues = [deque(campaign_buckets[cid]) for cid in ordered_ids]
    result: List[int] = []
    while any(queues):
        for q in queues:
            if q:
                result.append(q.popleft())
    return result


# 오늘 남은 job 간격 상한 — 기기 1대면 하루 창에 너무 넓게 퍼져 idle 되는 것 방지
MAX_PACE_GAP_SECS = 90.0


def spread_scheduled_at(
    day: date,
    count: int,
    *,
    start_hour: int = WINDOW_START_HOUR,
    end_hour: int = WINDOW_END_HOUR,
) -> List[datetime]:
    """하루 운영 창에 균등 분산된 scheduled_at 목록.

    오늘이면 시작점을 now 로 잡는다(새벽/창 시작 전에도 바로 돌릴 수 있게).
    과거 일자는 해당일 운영 시작 시각부터.
    오늘 간격이 MAX_PACE_GAP_SECS 보다 커지면 더 촘촘히 잡아 idle 을 줄인다.
    """
    if count <= 0:
        return []
    now = datetime.now()
    if day == now.date():
        start = now
    else:
        start = datetime.combine(day, time(hour=max(0, min(23, start_hour))))
    end_h = max(start.hour, min(23, end_hour))
    end = datetime.combine(day, time(hour=end_h, minute=59, second=59))
    if end <= start:
        # 창이 거의 끝났으면 지금부터 짧은 간격
        end = start + timedelta(minutes=max(count, 1))
    span = (end - start).total_seconds()
    if count == 1:
        return [start]
    step = span / (count - 1)
    if day == now.date() and step > MAX_PACE_GAP_SECS:
        step = MAX_PACE_GAP_SECS
    return [start + timedelta(seconds=step * i) for i in range(count)]


def _active_campaigns_for_day(db: Session, day: date) -> List[Campaign]:
    rows = db.scalars(select(Campaign).where(Campaign.status == "active")).all()
    out = []
    for c in rows:
        if c.start_date <= day <= c.end_date:
            out.append(c)
    return out


def rebuild_day_queue(db: Session, day: date) -> int:
    """
    해당 일자의 pending/leased 가 아닌 실행 중·완료 job은 보존하고,
    아직 실행되지 않은 슬롯을 캠페인 기준으로 재생성·인터리브한다.

    MVP: 가장 단순하고 예측 가능한 방식으로,
    해당 일자의 pending|leased job을 지운 뒤 active 캠페인으로 재생성.
    (running/success/failed는 유지하고 queue_seq는 pending만 재배치)
    """
    # Keep terminal/running jobs; remove reclaimable ones for rebuild
    db.execute(
        delete(Job).where(
            Job.schedule_date == day,
            Job.status.in_(["pending", "leased"]),
        )
    )
    db.flush()

    existing = db.scalars(
        select(Job).where(
            Job.schedule_date == day,
            Job.status.in_(["running", "success", "failed"]),
        )
    ).all()
    # 일유입량 충족은 성공분만 차감. failed 는 재생성 대상으로 남김.
    # running 은 중복 생성 방지를 위해 슬롯 점유.
    done_by_campaign: Dict[int, int] = defaultdict(int)
    for j in existing:
        if j.status in ("success", "running"):
            done_by_campaign[j.campaign_id] += 1
    # failed 기존 row 는 큐 순서를 보존하기 위해 삭제 후 재생성을 허용
    db.execute(delete(Job).where(Job.schedule_date == day, Job.status == "failed"))
    db.flush()
    existing = [j for j in existing if j.status != "failed"]

    campaigns = _active_campaigns_for_day(db, day)
    buckets: Dict[int, List[int]] = {}
    planned: Dict[int, int] = {}

    for c in campaigns:
        already = done_by_campaign.get(c.id, 0)
        need = max(c.daily_quota - already, 0)
        planned[c.id] = c.daily_quota
        buckets[c.id] = [c.id] * need
        # upsert CampaignDay
        cd = db.scalars(
            select(CampaignDay).where(CampaignDay.campaign_id == c.id, CampaignDay.date == day)
        ).first()
        if cd is None:
            cd = CampaignDay(campaign_id=c.id, date=day, planned_count=c.daily_quota)
            db.add(cd)
        else:
            cd.planned_count = c.daily_quota

    # If no campaigns needed, still clear day stats for inactive? leave as-is
    if not any(buckets.values()):
        db.commit()
        return 0

    order = interleave_round_robin(buckets)
    # paused 등 남은 row 의 queue_seq 와 겹치지 않게, 해당 일자 전체 max 사용
    remaining = db.scalars(select(Job).where(Job.schedule_date == day)).all()
    max_seq = max((j.queue_seq for j in remaining), default=0)
    times = spread_scheduled_at(day, len(order))
    for i, campaign_id in enumerate(order):
        db.add(
            Job(
                campaign_id=campaign_id,
                schedule_date=day,
                queue_seq=max_seq + i + 1,
                scheduled_at=times[i],
                status="pending",
            )
        )
    db.commit()
    return len(order)


def materialize_campaign(db: Session, campaign: Campaign) -> int:
    """캠페인 기간 전체 일자에 대해 큐를 반영(해당 일들 rebuild)."""
    total = 0
    for day in date_range(campaign.start_date, campaign.days):
        total += rebuild_day_queue(db, day)
    return total


def rebuild_all_active(db: Session, *, from_day: Optional[date] = None) -> int:
    """오늘 이후(또는 from_day부터) 관련 모든 일자의 큐 재구축."""
    campaigns = db.scalars(select(Campaign).where(Campaign.status == "active")).all()
    if not campaigns:
        return 0
    start = from_day or date.today()
    days = set()
    for c in campaigns:
        for d in date_range(c.start_date, c.days):
            if d >= start:
                days.add(d)
    total = 0
    for d in sorted(days):
        total += rebuild_day_queue(db, d)
    return total


def release_stale_leases(db: Session, timeout_secs: int) -> int:
    cutoff = utcnow() - timedelta(seconds=timeout_secs)
    rows = db.scalars(
        select(Job).where(Job.status.in_(["leased", "running"]), Job.leased_at.is_not(None))
    ).all()
    n = 0
    for j in rows:
        if j.leased_at and j.leased_at < cutoff:
            camp = j.campaign
            j.status = "paused" if camp and camp.status in ("paused", "stopped") else "pending"
            j.device_serial = None
            j.worker_id = None
            j.leased_at = None
            j.started_at = None
            n += 1
    if n:
        db.commit()
    return n


def pause_campaign(db: Session, campaign_id: int) -> Optional[Campaign]:
    """중단(일시정지): 남은 pending/leased 를 paused 로 보관. 재개 가능."""
    c = db.get(Campaign, campaign_id)
    if not c:
        return None
    c.status = "paused"
    devices = db.scalars(select(Device)).all()
    for j in db.scalars(
        select(Job).where(
            Job.campaign_id == campaign_id,
            Job.status.in_(["pending", "leased", "running"]),
        )
    ).all():
        for d in devices:
            if d.busy_job_id == j.id:
                d.busy_job_id = None
        j.status = "paused"
        j.device_serial = None
        j.worker_id = None
        j.leased_at = None
        j.started_at = None
    db.commit()
    # 다른 active 캠페인 큐만 다시 인터리브
    rebuild_all_active(db, from_day=date.today())
    db.refresh(c)
    return c


def resume_campaign(
    db: Session,
    campaign_id: int,
    *,
    daily_quota: Optional[int] = None,
    days: Optional[int] = None,
    start_date: Optional[date] = None,
) -> Optional[Campaign]:
    """재개: 유입수·일수를 다시 받아 기간을 새로 잡고 큐를 재구축.

    daily_quota / days 미지정 시(구 API 호환) 기존 값 유지하되
    start_date 는 오늘로 옮겨 만료된 캠페인도 다시 돌 수 있게 한다.
    """
    c = db.get(Campaign, campaign_id)
    if not c:
        return None
    if c.status not in ("paused", "stopped"):
        return c

    today = date.today()
    new_start = start_date or today
    new_quota = int(daily_quota) if daily_quota is not None else int(c.daily_quota)
    new_days = int(days) if days is not None else int(c.days)
    if new_quota < 1 or new_days < 1:
        raise ValueError("daily_quota and days must be >= 1")

    # 이전 기간 leftover(pending/paused/leased) 는 새 기간과 섞이지 않게 정리
    devices = db.scalars(select(Device)).all()
    for j in db.scalars(
        select(Job).where(
            Job.campaign_id == campaign_id,
            Job.status.in_(["pending", "paused", "leased", "running"]),
        )
    ).all():
        for d in devices:
            if d.busy_job_id == j.id:
                d.busy_job_id = None
        db.delete(j)
    db.flush()

    c.daily_quota = new_quota
    c.days = new_days
    c.start_date = new_start
    c.status = "active"
    db.commit()
    try:
        materialize_campaign(db, c)
        rebuild_all_active(db, from_day=new_start)
    except Exception:
        c.status = "paused"
        db.commit()
        raise
    db.refresh(c)
    return c


def update_campaign_pacing(
    db: Session,
    campaign_id: int,
    *,
    daily_quota: int,
    days: int,
) -> Optional[Campaign]:
    """진행 중(또는 일시정지) 캠페인의 일유입량·일수를 바꾸고 큐를 즉시 재구축.

    - start_date 는 유지 (재개와 달리 기간을 오늘부터 리셋하지 않음)
    - 새 end_date 밖 reclaimable(pending/paused/leased) job 은 제거
    - active 이면 남은 기간 큐를 rebuild 해 워커가 바로 새 페이싱을 잡음
    - paused 이면 설정만 갱신 (재개 전까지 claim 대상 아님)
    """
    c = db.get(Campaign, campaign_id)
    if not c:
        return None

    new_quota = int(daily_quota)
    new_days = int(days)
    if new_quota < 1 or new_days < 1:
        raise ValueError("daily_quota and days must be >= 1")
    if new_days > 365:
        raise ValueError("days must be <= 365")

    c.daily_quota = new_quota
    c.days = new_days
    new_end = c.end_date
    today = date.today()

    devices = db.scalars(select(Device)).all()
    for j in db.scalars(
        select(Job).where(
            Job.campaign_id == campaign_id,
            Job.status.in_(["pending", "paused", "leased"]),
        )
    ).all():
        # 기간 밖 + 과거 일자 leftover 제거.
        # (유입량 축소 후에도 예전 daily_quota 잔량이 claim 되면 안 됨)
        if (
            j.schedule_date < c.start_date
            or j.schedule_date > new_end
            or j.schedule_date < today
        ):
            for d in devices:
                if d.busy_job_id == j.id:
                    d.busy_job_id = None
            db.delete(j)
    db.commit()

    if c.status == "active":
        # 기간이 오늘 이전이면 즉시 paused
        expire_past_campaigns(db)
        db.refresh(c)
        if c.status == "active":
            rebuild_all_active(db, from_day=date.today())
    db.refresh(c)
    return c


def expire_past_campaigns(db: Session) -> int:
    """기간이 지난 active 캠페인을 paused 로 내리고 leftover pending 을 paused 로 보관.

    end_date < 오늘(로컬/KST 캘린더일) 이면 자동 중단. claim 대상에서 빠진다.
    """
    today = date.today()
    n = 0
    for c in db.scalars(select(Campaign).where(Campaign.status == "active")).all():
        if c.end_date >= today:
            continue
        c.status = "paused"
        devices = db.scalars(select(Device)).all()
        for j in db.scalars(
            select(Job).where(
                Job.campaign_id == c.id,
                Job.status.in_(["pending", "leased", "running"]),
            )
        ).all():
            for d in devices:
                if d.busy_job_id == j.id:
                    d.busy_job_id = None
            j.status = "paused"
            j.device_serial = None
            j.worker_id = None
            j.leased_at = None
            j.started_at = None
        n += 1
    if n:
        db.commit()
    return n


def _next_after(sorted_ids: Sequence[int], last_id: Optional[int]) -> Optional[int]:
    """오름차순 목록에서 last_id 다음(없으면 wrap → 첫 항목)."""
    if not sorted_ids:
        return None
    if last_id is None:
        return sorted_ids[0]
    after = [x for x in sorted_ids if x > last_id]
    return after[0] if after else sorted_ids[0]


def _next_traffic_type(types: Sequence[str], last_type: Optional[str]) -> Optional[str]:
    """트래픽 타입 라운드로빈 (정렬 후 직전 타입 다음으로 wrap)."""
    if not types:
        return None
    ordered = sorted(types)
    if last_type is None or last_type not in ordered:
        return ordered[0]
    idx = ordered.index(last_type)
    return ordered[(idx + 1) % len(ordered)]


def _pick_round_robin_job(
    candidates: Sequence[Job],
    *,
    last_campaign_id: Optional[int],
    last_traffic_type: Optional[str],
    last_campaign_by_type: Optional[Dict[str, int]] = None,
    today: date,
) -> Optional[Job]:
    """due 후보 중 트래픽타입 → 캠페인 2단 라운드로빈.

    오늘 due 가 있으면 오늘만 대상으로 돌리고, 없으면 과거 leftover.
    1) 직전 traffic_type 다음 타입 (hamman/kakao 등) — kakao 가 hamman 잔여에 굶지 않음
    2) 해당 타입 안에서는 직전 *같은 타입* 캠페인 다음 id
       (타입을 막 전환해도 hamman 쪽 last 를 별도 기억해 15번 고착 방지)
    후보 풀이 queue_seq 상위 N개로 잘려도 호출 전에 캠페인 단위로 채우므로
    한 캠페인 backlog 가 풀을 독점하지 않음.
    """
    if not candidates:
        return None
    today_pool = [j for j in candidates if j.schedule_date == today]
    pool = today_pool if today_pool else list(candidates)
    by_camp: Dict[int, List[Job]] = {}
    camp_type: Dict[int, str] = {}
    for j in pool:
        by_camp.setdefault(j.campaign_id, []).append(j)
        if j.campaign is not None:
            camp_type[j.campaign_id] = j.campaign.traffic_type
    if not by_camp:
        return None

    types = sorted({camp_type.get(cid, "") for cid in by_camp if camp_type.get(cid)})
    if not types:
        # traffic_type 없을 때 캠페인 RR 만
        chosen = _next_after(sorted(by_camp.keys()), last_campaign_id)
        return by_camp[chosen][0] if chosen is not None else None

    chosen_type = _next_traffic_type(types, last_traffic_type)
    camps_of_type = sorted(
        cid for cid, tt in camp_type.items() if tt == chosen_type and cid in by_camp
    )
    if not camps_of_type:
        chosen = _next_after(sorted(by_camp.keys()), last_campaign_id)
        return by_camp[chosen][0] if chosen is not None else None

    by_type = last_campaign_by_type or {}
    last_same = by_type.get(chosen_type)
    if last_same is None and camp_type.get(last_campaign_id or -1) == chosen_type:
        last_same = last_campaign_id
    chosen_camp = _next_after(camps_of_type, last_same)
    if chosen_camp is None:
        return None
    return by_camp[chosen_camp][0]


def claim_next_job(
    db: Session,
    *,
    serial: str,
    worker_id: str,
    now: Optional[datetime] = None,
) -> Optional[Job]:
    """기기당 1 job.

    - 기간 지난 캠페인은 자동 paused
    - 오늘(및 도래한) job 우선 — 과거 leftover 때문에 오늘 캠페인이 밀리지 않음
    - 미래(schedule_date > today) job 은 미리 실행하지 않음
    - due 가 여러 트래픽/캠페인에 있으면 타입 RR → 캠페인 RR (한 캠페인 backlog 독점 금지)
    """
    now = now or utcnow()
    today = date.today()
    expire_past_campaigns(db)

    # already busy? 캠페인이 중단/만료됐으면 즉시 반납 후 다른 job claim
    busy = db.scalars(
        select(Job).where(
            Job.device_serial == serial,
            Job.status.in_(["leased", "running"]),
        )
    ).first()
    if busy:
        camp = busy.campaign
        if camp is not None and camp.status == "active" and camp.end_date >= today:
            return busy
        busy.status = "paused" if camp and camp.status in ("paused", "stopped") else "pending"
        if camp and camp.end_date < today:
            busy.status = "paused"
        busy.device_serial = None
        busy.worker_id = None
        busy.leased_at = None
        busy.started_at = None
        db.commit()

    due_camp_filter = (
        Job.status == "pending",
        Campaign.status == "active",
        Job.schedule_date <= today,
        (Job.scheduled_at.is_(None)) | (Job.scheduled_at <= now),
    )

    # 오늘 due 캠페인 우선. 없으면 과거 leftover 캠페인.
    today_camp_ids = list(
        db.scalars(
            select(Job.campaign_id)
            .join(Campaign, Campaign.id == Job.campaign_id)
            .where(*due_camp_filter)
            .where(Job.schedule_date == today)
            .distinct()
        ).all()
    )
    if today_camp_ids:
        scope_camp_ids = today_camp_ids
        prefer_today = True
    else:
        scope_camp_ids = list(
            db.scalars(
                select(Job.campaign_id)
                .join(Campaign, Campaign.id == Job.campaign_id)
                .where(*due_camp_filter)
                .distinct()
            ).all()
        )
        prefer_today = False

    if not scope_camp_ids:
        return None

    # 캠페인마다 가장 due 한 job 1개씩만 모아 RR — queue_seq 상위 N 독점(limit 300) 방지
    day_priority = case((Job.schedule_date == today, 0), else_=1)
    candidates: List[Job] = []
    for cid in scope_camp_ids:
        q = (
            select(Job)
            .join(Campaign, Campaign.id == Job.campaign_id)
            .where(*due_camp_filter)
            .where(Job.campaign_id == cid)
        )
        if prefer_today:
            q = q.where(Job.schedule_date == today)
        job_one = db.scalars(
            q.order_by(day_priority.asc(), Job.schedule_date.asc(), Job.queue_seq.asc()).limit(1)
        ).first()
        if (
            job_one is not None
            and job_one.campaign is not None
            and job_one.campaign.status == "active"
            and job_one.campaign.end_date >= today
        ):
            candidates.append(job_one)

    if not candidates:
        return None

    last_job = db.scalars(
        select(Job)
        .where(Job.device_serial == serial)
        .where(Job.leased_at.is_not(None))
        .order_by(Job.leased_at.desc())
        .limit(1)
    ).first()
    last_campaign_id = last_job.campaign_id if last_job else None
    last_traffic_type = (
        last_job.campaign.traffic_type
        if last_job is not None and last_job.campaign is not None
        else None
    )
    # 타입별 직전 캠페인 (타입 RR 로 전환돼도 hamman 내부 RR 유지)
    last_campaign_by_type: Dict[str, int] = {}
    recent = db.scalars(
        select(Job)
        .where(Job.device_serial == serial)
        .where(Job.leased_at.is_not(None))
        .order_by(Job.leased_at.desc())
        .limit(40)
    ).all()
    for rj in recent:
        if rj.campaign is None:
            continue
        tt = rj.campaign.traffic_type
        if tt not in last_campaign_by_type:
            last_campaign_by_type[tt] = rj.campaign_id

    job = _pick_round_robin_job(
        candidates,
        last_campaign_id=last_campaign_id,
        last_traffic_type=last_traffic_type,
        last_campaign_by_type=last_campaign_by_type,
        today=today,
    )
    if not job:
        return None

    job.status = "leased"
    job.device_serial = serial
    job.worker_id = worker_id
    job.leased_at = now
    try:
        db.commit()
    except Exception:
        db.rollback()
        return None
    db.refresh(job)
    return job


def mark_job_running(db: Session, job_id: int) -> Optional[Job]:
    job = db.get(Job, job_id)
    if not job:
        return None
    job.status = "running"
    job.started_at = utcnow()
    db.commit()
    db.refresh(job)
    return job


def release_job_to_pending(
    db: Session,
    job_id: int,
    *,
    reason: Optional[str] = None,
) -> Optional[Job]:
    """기기 offline 등으로 실행 못 한 job 을 fail 없이 pending 으로 되돌림."""
    job = db.get(Job, job_id)
    if not job:
        return None
    if job.status in ("success", "failed", "paused"):
        return job
    job.status = "pending"
    job.device_serial = None
    job.worker_id = None
    job.leased_at = None
    job.started_at = None
    job.finished_at = None
    if reason:
        job.error_message = f"released: {reason}"
    db.commit()
    db.refresh(job)
    return job


def report_job(
    db: Session,
    job_id: int,
    *,
    status: str,
    error_message: Optional[str] = None,
    log_path: Optional[str] = None,
) -> Optional[Job]:
    if status not in ("success", "failed"):
        raise ValueError("status must be success|failed")
    job = db.get(Job, job_id)
    if not job:
        return None
    # 중단되어 paused 로 바뀐 job 은 결과 집계하지 않음
    if job.status == "paused":
        return job
    camp = job.campaign
    if camp is not None and camp.status != "active":
        job.status = "paused"
        job.device_serial = None
        job.worker_id = None
        job.finished_at = utcnow()
        job.error_message = "campaign paused"
        db.commit()
        db.refresh(job)
        return job
    job.status = status
    job.finished_at = utcnow()
    job.error_message = error_message
    job.log_path = log_path
    # CampaignDay counters
    cd = db.scalars(
        select(CampaignDay).where(
            CampaignDay.campaign_id == job.campaign_id,
            CampaignDay.date == job.schedule_date,
        )
    ).first()
    if cd:
        if status == "success":
            cd.done_count += 1
        else:
            cd.failed_count += 1
    db.commit()
    db.refresh(job)
    return job


def job_still_runnable(db: Session, job_id: int) -> bool:
    job = db.get(Job, job_id)
    if not job:
        return False
    if job.status not in ("leased", "running"):
        return False
    camp = job.campaign
    if camp is None or camp.status != "active":
        return False
    return camp.end_date >= date.today()
