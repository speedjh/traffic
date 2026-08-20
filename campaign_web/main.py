# -*- coding: utf-8 -*-
"""캠페인 작업큐 웹 API + UI."""
from __future__ import annotations

import re

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .accounts import (
    account_stats,
    import_accounts,
    lease_account,
    parse_xlsx,
    release_stale_leases as release_stale_account_leases,
    report_account,
)
from .config import HEARTBEAT_STALE_SECS, LEASE_TIMEOUT_SECS
from .db import SessionLocal, get_db, init_db
from .models import Campaign, CampaignDay, Device, Job, KakaoAccount, utcnow
from .scheduler import (
    claim_next_job,
    expire_past_campaigns,
    job_still_runnable,
    materialize_campaign,
    mark_job_running,
    pause_campaign,
    rebuild_all_active,
    rebuild_day_queue,
    release_job_to_pending,
    release_stale_leases,
    report_job,
    resume_campaign,
    update_campaign_pacing,
)
from .schemas import (
    AccountLeaseRequest,
    AccountReport,
    CampaignCreate,
    CampaignOut,
    CampaignPacingUpdate,
    CampaignResume,
    CampaignUpdate,
    ClaimRequest,
    DashboardOut,
    DeviceHeartbeat,
    DeviceOut,
    JobOut,
    JobRelease,
    JobReport,
    KakaoAccountCreate,
    KakaoAccountOut,
    KakaoAccountUpdate,
)
from .traffic import TRAFFIC_TYPES, traffic_list

app = FastAPI(title="Campaign Queue", version="1.0.0")
BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.on_event("startup")
def _startup():
    init_db()
    # 기간 지난 active 캠페인을 즉시 paused (워커 claim 전 UI에서도 반영)
    db = SessionLocal()
    try:
        expire_past_campaigns(db)
    finally:
        db.close()


def _local_day_range(day: date) -> tuple[datetime, datetime]:
    """로컬(KST) 캘린더일의 [start, end) 시각."""
    start = datetime.combine(day, datetime.min.time())
    return start, start + timedelta(days=1)


def _count_jobs_finished_today(
    db: Session,
    *,
    day: date,
    status: str,
    campaign_id: Optional[int] = None,
) -> int:
    """finished_at 기준(로컬일)으로 완료 건수. schedule_date 어제 leftover도 오늘 집계에 포함."""
    start, end = _local_day_range(day)
    q = (
        select(func.count())
        .select_from(Job)
        .where(
            Job.status == status,
            Job.finished_at.is_not(None),
            Job.finished_at >= start,
            Job.finished_at < end,
        )
    )
    if campaign_id is not None:
        q = q.where(Job.campaign_id == campaign_id)
    return db.scalar(q) or 0


def _list_adb_serials() -> List[str]:
    try:
        import subprocess
        from .config import ROOT_DIR

        adb = ROOT_DIR / "platform-tools" / "adb"
        bin_ = str(adb) if adb.is_file() else "adb"
        r = subprocess.run([bin_, "devices"], capture_output=True, text=True, timeout=8)
    except Exception:
        return []
    out: List[str] = []
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            out.append(parts[0])
    return out


def _suggest_worker_serial(db: Session, adb_serials: Optional[List[str]] = None) -> str:
    """안내 메시지용 시리얼: 실제 연결된 ADB → 최근 online heartbeat → 최근 heartbeat → DEVICE."""
    serials = adb_serials if adb_serials is not None else _list_adb_serials()
    if serials:
        return serials[0]
    devices = list(db.scalars(select(Device)).all())
    devices.sort(
        key=lambda d: d.last_heartbeat or datetime.min,
        reverse=True,
    )
    for d in devices:
        if d.last_heartbeat and (utcnow() - d.last_heartbeat).total_seconds() <= HEARTBEAT_STALE_SECS:
            return d.serial
    if devices and devices[0].last_heartbeat:
        return devices[0].serial
    return "DEVICE"


def _campaign_out(c: Campaign) -> CampaignOut:
    return CampaignOut(
        id=c.id,
        name=c.name,
        traffic_type=c.traffic_type,
        keyword=c.keyword,
        place_url=c.place_url,
        place_name=c.place_name,
        daily_quota=c.daily_quota,
        start_date=c.start_date,
        days=c.days,
        end_date=c.end_date,
        status=c.status,
        params=c.params,
        created_at=c.created_at,
    )


def _job_out(j: Job) -> JobOut:
    return JobOut(
        id=j.id,
        campaign_id=j.campaign_id,
        campaign_name=j.campaign.name if j.campaign else "",
        traffic_type=j.campaign.traffic_type if j.campaign else "",
        schedule_date=j.schedule_date,
        queue_seq=j.queue_seq,
        scheduled_at=j.scheduled_at,
        status=j.status,
        device_serial=j.device_serial,
        worker_id=j.worker_id,
        error_message=j.error_message,
    )


# 워커가 heartbeat 로 보고한 최근 adb 연결 상태 (DB 컬럼 없이 프로세스 메모리)
_ADB_ONLINE: dict[str, bool] = {}


def _device_out(d: Device) -> DeviceOut:
    online = False
    if d.last_heartbeat:
        online = (utcnow() - d.last_heartbeat).total_seconds() <= HEARTBEAT_STALE_SECS
    return DeviceOut(
        serial=d.serial,
        host=d.host,
        worker_id=d.worker_id,
        last_heartbeat=d.last_heartbeat,
        busy_job_id=d.busy_job_id,
        online=online,
        adb_online=_ADB_ONLINE.get(d.serial),
    )


# ── JSON API ───────────────────────────────────────────────────────────


@app.get("/api/traffic-types")
def api_traffic_types():
    return [
        {
            "key": t.key,
            "label": t.label,
            "description": t.description,
            "needs_place": t.needs_place,
            "needs_place_name": t.needs_place_name,
        }
        for t in traffic_list()
    ]


@app.get("/api/campaigns", response_model=List[CampaignOut])
def api_list_campaigns(db: Session = Depends(get_db)):
    rows = db.scalars(select(Campaign).order_by(Campaign.id.desc())).all()
    return [_campaign_out(c) for c in rows]


@app.post("/api/campaigns", response_model=CampaignOut)
def api_create_campaign(body: CampaignCreate, db: Session = Depends(get_db)):
    c = Campaign(
        name=body.name,
        traffic_type=body.traffic_type,
        keyword=body.keyword,
        place_url=body.place_url,
        place_name=body.place_name,
        daily_quota=body.daily_quota,
        start_date=body.start_date,
        days=body.days,
        status=body.status,
        params_json="{}",
    )
    c.params = body.params
    db.add(c)
    db.commit()
    db.refresh(c)
    if c.status == "active":
        materialize_campaign(db, c)
        # Other campaigns' days overlapping must re-interleave
        rebuild_all_active(db, from_day=c.start_date)
    return _campaign_out(c)


@app.get("/api/campaigns/{campaign_id}", response_model=CampaignOut)
def api_get_campaign(campaign_id: int, db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404, "campaign not found")
    return _campaign_out(c)


@app.patch("/api/campaigns/{campaign_id}", response_model=CampaignOut)
def api_update_campaign(
    campaign_id: int, body: CampaignUpdate, db: Session = Depends(get_db)
):
    c = db.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404, "campaign not found")
    data = body.model_dump(exclude_unset=True)
    params = data.pop("params", None)
    if "traffic_type" in data and data["traffic_type"] not in TRAFFIC_TYPES:
        raise HTTPException(400, "unsupported traffic_type")

    # 일유입량·일수 변경은 전용 경로로 큐를 즉시 재구축
    pacing_quota = data.pop("daily_quota", None)
    pacing_days = data.pop("days", None)
    if pacing_quota is not None or pacing_days is not None:
        try:
            c = update_campaign_pacing(
                db,
                campaign_id,
                daily_quota=pacing_quota if pacing_quota is not None else c.daily_quota,
                days=pacing_days if pacing_days is not None else c.days,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not c:
            raise HTTPException(404, "campaign not found")

    for k, v in data.items():
        setattr(c, k, v)
    if params is not None:
        c.params = params
    if data or params is not None:
        db.commit()
        db.refresh(c)
        if c.status == "active" and not (pacing_quota is not None or pacing_days is not None):
            rebuild_all_active(db, from_day=min(c.start_date, date.today()))
    return _campaign_out(c)


def _apply_campaign_traffic_fields(c: Campaign, body: CampaignPacingUpdate) -> bool:
    """키워드·2차키워드·장소·체류 필드를 캠페인에 반영. 변경 여부 반환.

    워커는 claim 시 Campaign 행을 읽므로 큐 재구축 불필요.
    hamman_find 의 keyword2 는 params.keyword2 와 place_name 을 동기화
    (build_command 가 params → place_name 순으로 폴백).
    """
    touched = False
    params = dict(c.params or {})

    if body.keyword is not None:
        c.keyword = body.keyword.strip()
        touched = True

    if body.place_url is not None:
        c.place_url = body.place_url.strip()
        touched = True

    # keyword2 우선, 없으면 place_name (생성 폼의 「업체명 / 2차 키워드」와 동일)
    dual = body.keyword2 if body.keyword2 is not None else body.place_name
    if dual is not None:
        val = dual.strip()
        c.place_name = val
        if c.traffic_type == "hamman_find" or "keyword2" in params:
            if val:
                params["keyword2"] = val
            else:
                params.pop("keyword2", None)
        touched = True

    dmin = body.dwell_min
    dmax = body.dwell_max
    if dmin is not None or dmax is not None:
        cur_min = float(params.get("dwell_min", 15))
        cur_max = float(params.get("dwell_max", 20))
        new_min = float(dmin) if dmin is not None else cur_min
        new_max = float(dmax) if dmax is not None else cur_max
        if new_max < new_min:
            new_min, new_max = new_max, new_min
        params["dwell_min"] = new_min
        params["dwell_max"] = new_max
        touched = True

    if touched:
        c.params = params
    return touched


@app.post("/api/campaigns/{campaign_id}/settings", response_model=CampaignOut)
def api_update_campaign_settings(
    campaign_id: int, body: CampaignPacingUpdate, db: Session = Depends(get_db)
):
    """일유입·일수(큐 재구축) 및/또는 키워드·체류·장소 즉시 반영."""
    c = db.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404, "campaign not found")
    if c.status not in ("active", "paused"):
        raise HTTPException(400, "campaign must be active or paused")

    pacing = body.daily_quota is not None or body.days is not None
    traffic = any(
        getattr(body, f) is not None
        for f in (
            "keyword",
            "keyword2",
            "place_name",
            "place_url",
            "dwell_min",
            "dwell_max",
        )
    )
    if not pacing and not traffic:
        raise HTTPException(400, "no fields to update")

    if pacing:
        try:
            c = update_campaign_pacing(
                db,
                campaign_id,
                daily_quota=body.daily_quota if body.daily_quota is not None else c.daily_quota,
                days=body.days if body.days is not None else c.days,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not c:
            raise HTTPException(404, "campaign not found")

    if traffic:
        # pacing 이후 세션 객체 갱신
        c = db.get(Campaign, campaign_id)
        if not c:
            raise HTTPException(404, "campaign not found")
        if _apply_campaign_traffic_fields(c, body):
            db.commit()
            db.refresh(c)

    return _campaign_out(c)


@app.post("/api/campaigns/{campaign_id}/pause", response_model=CampaignOut)
@app.post("/api/campaigns/{campaign_id}/stop", response_model=CampaignOut)
def api_pause_campaign(campaign_id: int, db: Session = Depends(get_db)):
    """중단 = 일시정지 (재개 가능). /stop 은 호환용 alias."""
    c = pause_campaign(db, campaign_id)
    if not c:
        raise HTTPException(404, "campaign not found")
    return _campaign_out(c)


@app.post("/api/campaigns/{campaign_id}/resume", response_model=CampaignOut)
def api_resume_campaign(
    campaign_id: int,
    body: Optional[CampaignResume] = Body(default=None),
    db: Session = Depends(get_db),
):
    """재개. body 에 일유입량·일수 권장. 없으면 기존 값으로 오늘부터 재시작(호환)."""
    c = db.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404, "campaign not found")
    if c.status not in ("paused", "stopped"):
        raise HTTPException(400, "campaign is not paused")
    try:
        if body is not None:
            c = resume_campaign(
                db,
                campaign_id,
                daily_quota=body.daily_quota,
                days=body.days,
                start_date=body.start_date or date.today(),
            )
        else:
            c = resume_campaign(
                db,
                campaign_id,
                daily_quota=c.daily_quota,
                days=c.days,
                start_date=date.today(),
            )
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not c:
        raise HTTPException(404, "campaign not found")
    return _campaign_out(c)


@app.post("/api/scheduler/rebuild")
def api_rebuild(
    day: Optional[date] = None,
    db: Session = Depends(get_db),
):
    if day:
        n = rebuild_day_queue(db, day)
        return {"rebuilt_jobs": n, "day": str(day)}
    n = rebuild_all_active(db, from_day=date.today())
    return {"rebuilt_jobs": n}


@app.get("/api/jobs", response_model=List[JobOut])
def api_jobs(
    day: Optional[date] = None,
    limit: int = Query(200, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    day = day or date.today()
    rows = db.scalars(
        select(Job)
        .where(Job.schedule_date == day)
        .order_by(Job.queue_seq.asc())
        .limit(limit)
    ).all()
    return [_job_out(j) for j in rows]


@app.post("/api/devices/heartbeat", response_model=DeviceOut)
def api_heartbeat(body: DeviceHeartbeat, db: Session = Depends(get_db)):
    d = db.scalars(select(Device).where(Device.serial == body.serial)).first()
    if not d:
        d = Device(serial=body.serial)
        db.add(d)
    d.host = body.host
    d.worker_id = body.worker_id
    d.last_heartbeat = utcnow()
    d.busy_job_id = body.busy_job_id
    if body.adb_online is not None:
        _ADB_ONLINE[body.serial] = bool(body.adb_online)
    db.commit()
    db.refresh(d)
    return _device_out(d)


@app.get("/api/devices", response_model=List[DeviceOut])
def api_devices(db: Session = Depends(get_db)):
    rows = db.scalars(select(Device).order_by(Device.serial)).all()
    return [_device_out(d) for d in rows]


@app.post("/api/worker/claim")
def api_claim(body: ClaimRequest, db: Session = Depends(get_db)):
    release_stale_leases(db, LEASE_TIMEOUT_SECS)
    # upsert device
    d = db.scalars(select(Device).where(Device.serial == body.serial)).first()
    if not d:
        d = Device(serial=body.serial)
        db.add(d)
    d.host = body.host
    d.worker_id = body.worker_id
    d.last_heartbeat = utcnow()
    db.commit()

    job = claim_next_job(db, serial=body.serial, worker_id=body.worker_id)
    if not job:
        d.busy_job_id = None
        db.commit()
        return {"job": None}

    # claim 시에는 leased 유지. 실제 스크립트 시작 때 /start 로 running.
    # (페이싱 대기·큐 rebuild 중 다른 캠페인 끼어들기 가능)
    d = db.scalars(select(Device).where(Device.serial == body.serial)).first()
    if d:
        d.busy_job_id = job.id
        db.commit()

    c = job.campaign
    return {
        "job": {
            "id": job.id,
            "campaign_id": job.campaign_id,
            "campaign_name": c.name if c else "",
            "traffic_type": c.traffic_type if c else "",
            "keyword": c.keyword if c else "",
            "place_url": c.place_url if c else "",
            "place_name": c.place_name if c else "",
            "params": c.params if c else {},
            "schedule_date": str(job.schedule_date),
            "queue_seq": job.queue_seq,
            "scheduled_at": job.scheduled_at.isoformat() if job.scheduled_at else None,
        }
    }


@app.post("/api/worker/jobs/{job_id}/start")
def api_job_start(job_id: int, db: Session = Depends(get_db)):
    if not job_still_runnable(db, job_id):
        raise HTTPException(409, "job not runnable")
    job = mark_job_running(db, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return {"ok": True, "job_id": job_id, "status": job.status}


@app.get("/api/worker/jobs/{job_id}/runnable")
def api_job_runnable(job_id: int, db: Session = Depends(get_db)):
    return {"job_id": job_id, "runnable": job_still_runnable(db, job_id)}


@app.post("/api/worker/jobs/{job_id}/report")
def api_report(job_id: int, body: JobReport, db: Session = Depends(get_db)):
    try:
        job = report_job(
            db,
            job_id,
            status=body.status,
            error_message=body.error_message,
            log_path=body.log_path,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not job:
        raise HTTPException(404, "job not found")
    d = db.scalars(select(Device).where(Device.serial == body.serial)).first()
    if d and d.busy_job_id == job_id:
        d.busy_job_id = None
        d.last_heartbeat = utcnow()
        db.commit()
    return {"ok": True, "job_id": job_id, "status": job.status}


@app.post("/api/worker/jobs/{job_id}/release")
def api_release(job_id: int, body: JobRelease, db: Session = Depends(get_db)):
    """기기 offline 등 — fail 집계 없이 pending 으로 되돌림."""
    job = release_job_to_pending(db, job_id, reason=body.reason)
    if not job:
        raise HTTPException(404, "job not found")
    d = db.scalars(select(Device).where(Device.serial == body.serial)).first()
    if d and d.busy_job_id == job_id:
        d.busy_job_id = None
        d.last_heartbeat = utcnow()
        db.commit()
    return {"ok": True, "job_id": job_id, "status": job.status}


@app.get("/api/dashboard", response_model=DashboardOut)
def api_dashboard(db: Session = Depends(get_db)):
    today = date.today()
    expire_past_campaigns(db)
    pending_today = db.scalar(
        select(func.count())
        .select_from(Job)
        .join(Campaign, Campaign.id == Job.campaign_id)
        .where(Job.schedule_date == today, Job.status == "pending", Campaign.status == "active")
    ) or 0
    running = db.scalar(
        select(func.count())
        .select_from(Job)
        .join(Campaign, Campaign.id == Job.campaign_id)
        .where(Job.status.in_(["leased", "running"]), Campaign.status == "active")
    ) or 0
    # 자정 이후 leftover(schedule_date=어제)도 finished_at 로컬일로 집계
    success_today = _count_jobs_finished_today(db, day=today, status="success")
    failed_today = _count_jobs_finished_today(db, day=today, status="failed")

    campaigns = []
    for c in db.scalars(select(Campaign).order_by(Campaign.id.desc())).all():
        cd = db.scalars(
            select(CampaignDay).where(CampaignDay.campaign_id == c.id, CampaignDay.date == today)
        ).first()
        campaigns.append(
            {
                "id": c.id,
                "name": c.name,
                "traffic_type": c.traffic_type,
                "status": c.status,
                "daily_quota": c.daily_quota,
                "planned_today": cd.planned_count if cd else 0,
                "done_today": _count_jobs_finished_today(
                    db, day=today, status="success", campaign_id=c.id
                ),
                "failed_today": _count_jobs_finished_today(
                    db, day=today, status="failed", campaign_id=c.id
                ),
                "start_date": str(c.start_date),
                "days": c.days,
            }
        )

    preview = db.scalars(
        select(Job).where(Job.schedule_date == today).order_by(Job.queue_seq.asc()).limit(40)
    ).all()
    devices = [_device_out(d) for d in db.scalars(select(Device)).all()]
    return DashboardOut(
        today=today,
        pending_today=pending_today,
        running=running,
        success_today=success_today,
        failed_today=failed_today,
        campaigns=campaigns,
        devices=devices,
        queue_preview=[_job_out(j) for j in preview],
    )


# ── HTML UI ────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def ui_dashboard(request: Request, db: Session = Depends(get_db)):
    data = api_dashboard(db)
    online = any(d.online for d in data.devices)
    adb_serials = _list_adb_serials()
    adb_offline = not bool(adb_serials)
    worker_serial = _suggest_worker_serial(db, adb_serials)
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "dash": data,
            "traffic": TRAFFIC_TYPES,
            "worker_online": online,
            "adb_offline": adb_offline,
            "worker_serial": worker_serial,
        },
    )


@app.get("/campaigns/new", response_class=HTMLResponse)
def ui_new_campaign(request: Request):
    return templates.TemplateResponse(
        "campaign_form.html",
        {
            "request": request,
            "traffic_types": traffic_list(),
            "today": date.today().isoformat(),
            "campaign": None,
            "error": None,
        },
    )


@app.post("/campaigns/new")
async def ui_create_campaign(
    request: Request,
    name: str = Form(...),
    traffic_type: str = Form(...),
    keyword: str = Form(""),
    place_url: str = Form(""),
    place_name: str = Form(""),
    daily_quota: int = Form(...),
    start_date: str = Form(...),
    days: int = Form(...),
    dwell_min: Optional[float] = Form(None),
    dwell_max: Optional[float] = Form(None),
    db: Session = Depends(get_db),
):
    try:
        params: Dict[str, Any] = {}
        if traffic_type in ("kakao_search", "hamman_find"):
            dmin = float(dwell_min) if dwell_min is not None else 15.0
            dmax = float(dwell_max) if dwell_max is not None else 20.0
            if dmax < dmin:
                dmin, dmax = dmax, dmin
            params["dwell_min"] = dmin
            params["dwell_max"] = dmax
            if traffic_type == "hamman_find" and place_name.strip():
                params["keyword2"] = place_name.strip()
        body = CampaignCreate(
            name=name.strip(),
            traffic_type=traffic_type,
            keyword=keyword.strip(),
            place_url=place_url.strip(),
            place_name=place_name.strip(),
            daily_quota=daily_quota,
            start_date=date.fromisoformat(start_date),
            days=days,
            params=params,
        )
        api_create_campaign(body, db)
        return RedirectResponse("/", status_code=303)
    except Exception as e:
        return templates.TemplateResponse(
            "campaign_form.html",
            {
                "request": request,
                "traffic_types": traffic_list(),
                "today": date.today().isoformat(),
                "campaign": None,
                "error": str(e),
            },
            status_code=400,
        )


@app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
def ui_campaign_detail(
    campaign_id: int,
    request: Request,
    db: Session = Depends(get_db),
    applied: Optional[int] = Query(None),
):
    c = db.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404, "campaign not found")
    today = date.today()
    day_start, day_end = _local_day_range(today)
    done_today = _count_jobs_finished_today(
        db, day=today, status="success", campaign_id=c.id
    )
    failed_today = _count_jobs_finished_today(
        db, day=today, status="failed", campaign_id=c.id
    )
    pending_today = (
        db.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.campaign_id == c.id, Job.schedule_date == today, Job.status == "pending")
        )
        or 0
    )
    cd = db.scalars(
        select(CampaignDay).where(CampaignDay.campaign_id == c.id, CampaignDay.date == today)
    ).first()
    planned = cd.planned_count if cd else c.daily_quota
    t = TRAFFIC_TYPES.get(c.traffic_type)
    recent_fails = db.scalars(
        select(Job)
        .where(
            Job.campaign_id == c.id,
            Job.status == "failed",
            Job.finished_at.is_not(None),
            Job.finished_at >= day_start,
            Job.finished_at < day_end,
        )
        .order_by(Job.finished_at.desc())
        .limit(8)
    ).all()
    run_cmd = ""
    try:
        from .traffic import build_command
        run_cmd = " ".join(
            build_command(
                c.traffic_type,
                keyword=c.keyword or "",
                serial=_suggest_worker_serial(db),
                place_url=c.place_url or "",
                place_name=c.place_name or "",
                params=c.params or {},
            )
        )
    except Exception:
        run_cmd = ""
    flash = "변경사항이 즉시 반영됩니다." if applied else None
    params = c.params or {}
    return templates.TemplateResponse(
        "campaign_detail.html",
        {
            "request": request,
            "c": c,
            "traffic_label": t.label if t else c.traffic_type,
            "done_today": done_today,
            "failed_today": failed_today,
            "pending_today": pending_today,
            "planned_today": planned,
            "recent_fails": recent_fails,
            "run_cmd": run_cmd,
            "flash": flash,
            "error": None,
            "today": today.isoformat(),
            "show_dwell": c.traffic_type in ("kakao_search", "hamman_find"),
            "keyword2_value": params.get("keyword2") or c.place_name or "",
            "dwell_min": params.get("dwell_min", 15),
            "dwell_max": params.get("dwell_max", 20),
        },
    )


@app.post("/campaigns/{campaign_id}/settings")
async def ui_update_campaign_settings(
    campaign_id: int,
    request: Request,
    daily_quota: Optional[int] = Form(None),
    days: Optional[int] = Form(None),
    keyword: Optional[str] = Form(None),
    keyword2: Optional[str] = Form(None),
    place_name: Optional[str] = Form(None),
    place_url: Optional[str] = Form(None),
    dwell_min: Optional[float] = Form(None),
    dwell_max: Optional[float] = Form(None),
    db: Session = Depends(get_db),
):
    c = db.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404, "campaign not found")
    try:
        body = CampaignPacingUpdate(
            daily_quota=daily_quota,
            days=days,
            keyword=keyword,
            keyword2=keyword2,
            place_name=place_name,
            place_url=place_url,
            dwell_min=dwell_min,
            dwell_max=dwell_max,
        )
        api_update_campaign_settings(campaign_id, body, db)
        return RedirectResponse(
            f"/campaigns/{campaign_id}?applied=1", status_code=303
        )
    except Exception as e:
        today = date.today()
        t = TRAFFIC_TYPES.get(c.traffic_type)
        err = getattr(e, "detail", None) or str(e)
        return templates.TemplateResponse(
            "campaign_detail.html",
            {
                "request": request,
                "c": c,
                "traffic_label": t.label if t else c.traffic_type,
                "done_today": 0,
                "failed_today": 0,
                "pending_today": 0,
                "planned_today": c.daily_quota,
                "recent_fails": [],
                "run_cmd": "",
                "flash": None,
                "error": err,
                "today": today.isoformat(),
                "show_dwell": c.traffic_type in ("kakao_search", "hamman_find"),
                "keyword2_value": (c.params or {}).get("keyword2") or c.place_name or "",
                "dwell_min": (c.params or {}).get("dwell_min", 15),
                "dwell_max": (c.params or {}).get("dwell_max", 20),
            },
            status_code=400,
        )


@app.post("/campaigns/{campaign_id}/pause")
@app.post("/campaigns/{campaign_id}/stop")
def ui_pause(campaign_id: int, db: Session = Depends(get_db)):
    api_pause_campaign(campaign_id, db)
    return RedirectResponse("/", status_code=303)


@app.get("/campaigns/{campaign_id}/resume", response_class=HTMLResponse)
def ui_resume_form(campaign_id: int, request: Request, db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404, "campaign not found")
    if c.status not in ("paused", "stopped"):
        return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)
    return templates.TemplateResponse(
        "campaign_resume.html",
        {
            "request": request,
            "c": c,
            "today": date.today().isoformat(),
            "error": None,
        },
    )


@app.post("/campaigns/{campaign_id}/resume")
async def ui_resume(
    campaign_id: int,
    request: Request,
    daily_quota: int = Form(...),
    days: int = Form(...),
    db: Session = Depends(get_db),
):
    c = db.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404, "campaign not found")
    try:
        body = CampaignResume(daily_quota=daily_quota, days=days, start_date=date.today())
        api_resume_campaign(campaign_id, body, db)
        return RedirectResponse("/", status_code=303)
    except Exception as e:
        return templates.TemplateResponse(
            "campaign_resume.html",
            {
                "request": request,
                "c": c,
                "today": date.today().isoformat(),
                "error": str(e),
            },
            status_code=400,
        )


@app.get("/queue", response_class=HTMLResponse)
def ui_queue(
    request: Request,
    day: Optional[str] = None,
    db: Session = Depends(get_db),
):
    d = date.fromisoformat(day) if day else date.today()
    jobs = api_jobs(day=d, limit=500, db=db)
    return templates.TemplateResponse(
        "queue.html",
        {
            "request": request,
            "day": d.isoformat(),
            "jobs": jobs,
            "traffic": TRAFFIC_TYPES,
        },
    )


# ══════════════════════════════════════════════════════════════════════════
# 카카오 계정 풀
# ══════════════════════════════════════════════════════════════════════════
def _account_query(
    db: Session,
    *,
    status: Optional[str] = None,
    login_state: Optional[str] = None,
    q: Optional[str] = None,
):
    stmt = select(KakaoAccount)
    if status:
        stmt = stmt.where(KakaoAccount.status == status)
    if login_state:
        stmt = stmt.where(KakaoAccount.login_state == login_state)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            (KakaoAccount.email.like(like)) | (KakaoAccount.nickname.like(like))
        )
    # 투입 오래된 순 = 다음에 뽑힐 순서
    return stmt.order_by(
        KakaoAccount.last_used_at.is_(None).desc(),
        KakaoAccount.last_used_at.asc(),
        KakaoAccount.id.asc(),
    )


@app.get("/api/accounts/kakao", response_model=List[KakaoAccountOut])
def api_accounts_list(
    status: Optional[str] = None,
    login_state: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(default=500, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    rows = db.scalars(_account_query(db, status=status, login_state=login_state, q=q).limit(limit)).all()
    return list(rows)


@app.get("/api/accounts/kakao/stats")
def api_accounts_stats(db: Session = Depends(get_db)):
    return account_stats(db)


@app.post("/api/accounts/kakao", response_model=KakaoAccountOut)
def api_account_create(body: KakaoAccountCreate, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    if db.scalars(select(KakaoAccount).where(KakaoAccount.email == email)).first():
        raise HTTPException(400, "이미 등록된 이메일입니다")
    acc = KakaoAccount(
        email=email,
        password=body.password,
        nickname=body.nickname,
        auth_method=body.auth_method,
        daily_limit=body.daily_limit,
        status=body.status,
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return acc


@app.patch("/api/accounts/kakao/{account_id}", response_model=KakaoAccountOut)
def api_account_update(account_id: int, body: KakaoAccountUpdate, db: Session = Depends(get_db)):
    acc = db.get(KakaoAccount, account_id)
    if acc is None:
        raise HTTPException(404, "account not found")
    for field in ("password", "nickname", "daily_limit", "status", "login_state", "note"):
        val = getattr(body, field)
        if val is not None:
            setattr(acc, field, val)
    db.commit()
    db.refresh(acc)
    return acc


@app.delete("/api/accounts/kakao/{account_id}")
def api_account_delete(account_id: int, db: Session = Depends(get_db)):
    acc = db.get(KakaoAccount, account_id)
    if acc is None:
        raise HTTPException(404, "account not found")
    db.delete(acc)
    db.commit()
    return {"ok": True}


@app.post("/api/accounts/kakao/import")
async def api_accounts_import(file: UploadFile = File(...), db: Session = Depends(get_db)):
    data = await file.read()
    try:
        rows, skipped = parse_xlsx(data)
    except Exception as e:
        raise HTTPException(400, f"엑셀 파싱 실패: {e}")
    result = import_accounts(db, rows)
    result["skipped_columns"] = skipped
    return result


@app.post("/api/accounts/kakao/lease")
def api_account_lease(body: AccountLeaseRequest, db: Session = Depends(get_db)):
    """워커가 로테이션 시작 시 계정 1개를 빌린다 (투입 오래된 순)."""
    acc = lease_account(db, serial=body.serial, job_id=body.job_id)
    if acc is None:
        return {"account": None}
    return {
        "account": {
            "id": acc.id,
            "email": acc.email,
            "password": acc.password,
            "nickname": acc.nickname,
            "warmed": acc.warmed_at is not None,
            "login_state": acc.login_state,
            "mail_email": acc.mail_email,
            "mail_password": acc.mail_password,
        }
    }


@app.post("/api/accounts/kakao/{account_id}/report")
def api_account_report(account_id: int, body: AccountReport, db: Session = Depends(get_db)):
    acc = report_account(db, account_id=account_id, result=body.result, note=body.note)
    if acc is None:
        raise HTTPException(404, "account not found")
    return {
        "ok": True,
        "id": acc.id,
        "status": acc.status,
        "login_state": acc.login_state,
        "fail_streak": acc.fail_streak,
    }


@app.post("/api/accounts/kakao/{account_id}/token")
def api_account_set_token(account_id: int, body: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    """계정에 아웃룩 OAuth 토큰 저장 (client_id + refresh_token). 저장 즉시 유효성 검증."""
    from .outlook_api import check_token

    acc = db.get(KakaoAccount, account_id)
    if acc is None:
        raise HTTPException(404, "account not found")
    cid = (body.get("client_id") or "").strip()
    rt = (body.get("refresh_token") or "").strip()
    if not cid or not rt:
        raise HTTPException(400, "client_id, refresh_token 필요")
    acc.oauth_client_id = cid
    acc.oauth_refresh_token = rt
    ok, detail = check_token(cid, rt)
    acc.oauth_status = "valid" if ok else "invalid"
    db.commit()
    return {"ok": ok, "detail": detail, "oauth_status": acc.oauth_status}


@app.get("/api/accounts/kakao/oauth-url")
def api_oauth_url(client_id: str):
    """PC 브라우저에서 열 로그인 URL 생성."""
    from .outlook_api import authorize_url, DEFAULT_REDIRECT
    return {"url": authorize_url(client_id), "redirect": DEFAULT_REDIRECT}


@app.post("/api/accounts/kakao/{account_id}/oauth-code")
def api_oauth_code(account_id: int, body: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    """PC 로그인 후 리다이렉트 URL(또는 code)을 받아 refresh_token 으로 교환·저장."""
    from .outlook_api import exchange_code, check_token, DEFAULT_REDIRECT
    acc = db.get(KakaoAccount, account_id)
    if acc is None:
        raise HTTPException(404, "account not found")
    client_id = (body.get("client_id") or acc.oauth_client_id or "").strip()
    raw = (body.get("code") or body.get("url") or "").strip()
    if not client_id or not raw:
        raise HTTPException(400, "client_id, code(또는 url) 필요")
    # 리다이렉트 URL 전체를 붙여넣어도 code= 만 뽑는다
    m = re.search(r"[?&]code=([^&\s]+)", raw)
    code = m.group(1) if m else raw
    redirect = (body.get("redirect_uri") or DEFAULT_REDIRECT)
    rt, detail = exchange_code(client_id, code, redirect)
    if not rt:
        return {"ok": False, "detail": detail}
    acc.oauth_client_id = client_id
    acc.oauth_refresh_token = rt
    ok, vdetail = check_token(client_id, rt)
    acc.oauth_status = "valid" if ok else "invalid"
    db.commit()
    return {"ok": ok, "detail": vdetail, "refresh_token_saved": True}


@app.post("/api/accounts/kakao/tokens/paste")
def api_accounts_paste_tokens(body: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    """계정 소스 형식 붙여넣기 파싱·저장.

    각 줄: 카카오메일----카카오비번----닉네임----아웃룩메일----아웃룩비번----client_id----refresh_token
    (구분자 ---- 또는 탭/쉼표 허용). 카카오메일 기준으로 계정을 찾아 토큰·메일자격 저장.
    저장 시 토큰 유효성도 함께 검증(validate=false 로 끄기 가능).
    """
    from .outlook_api import check_token

    text = body.get("text") or ""
    validate = body.get("validate", True)
    matched = created_tokens = valid = invalid = 0
    unmatched = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"----|\t|,", line)
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) < 2:
            continue
        kakao_email = parts[0].lower()
        # client_id(GUID)와 refresh_token(M. 으로 시작) 을 유연하게 탐지
        client_id = ""
        refresh_token = ""
        mail_email = ""
        mail_password = ""
        for p_ in parts[1:]:
            if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", p_):
                client_id = p_
            elif p_.startswith("M.") and len(p_) > 40:
                refresh_token = p_
            elif "@" in p_ and "kakao" not in p_.lower():
                mail_email = p_
        acc = db.scalars(select(KakaoAccount).where(KakaoAccount.email == kakao_email)).first()
        if acc is None:
            unmatched.append(kakao_email)
            continue
        matched += 1
        if mail_email:
            acc.mail_email = mail_email
        if client_id and refresh_token:
            acc.oauth_client_id = client_id
            acc.oauth_refresh_token = refresh_token
            created_tokens += 1
            if validate:
                ok, _ = check_token(client_id, refresh_token)
                acc.oauth_status = "valid" if ok else "invalid"
                valid += int(ok)
                invalid += int(not ok)
            else:
                acc.oauth_status = "unchecked"
    db.commit()
    return {"matched": matched, "tokens_saved": created_tokens,
            "valid": valid, "invalid": invalid, "unmatched": unmatched}


@app.get("/api/accounts/kakao/{account_id}/fetch-code")
def api_account_fetch_code(account_id: int, wait: int = 90, db: Session = Depends(get_db)):
    """서버가 아웃룩 메일함에서 이 계정의 카카오 인증번호를 읽어 반환.

    워커가 카카오 추가인증 화면에서 호출한다. 토큰 없으면 available=False.
    """
    from .outlook_api import fetch_kakao_code

    acc = db.get(KakaoAccount, account_id)
    if acc is None:
        raise HTTPException(404, "account not found")
    # 1순위: OAuth 토큰(IMAP) — 유효 토큰이 있으면 브라우저 없이 API 로 빠르게.
    #        (대량 프로그램이 토큰을 넣어준 계정)
    if acc.oauth_client_id and acc.oauth_refresh_token and acc.oauth_status != "invalid":
        code, detail = fetch_kakao_code(
            acc.mail_email or acc.email, acc.oauth_client_id, acc.oauth_refresh_token,
            newer_than_secs=600, timeout=max(10, min(wait, 120)),
        )
        if detail.startswith("token_refresh_failed") or detail.startswith("no_access_token"):
            acc.oauth_status = "invalid"
            db.commit()
        elif code:
            return {"available": True, "code": code, "detail": f"oauth:{detail}", "method": "oauth"}

    # 2순위: CDP + 계정 영구 프로필 (plain 웹로그인, OAuth 벽 회피). 세션 재사용.
    if acc.mail_email and acc.mail_password:
        try:
            from .outlook_cdp import fetch_code as cdp_fetch
            code, detail = cdp_fetch(acc.mail_email, acc.mail_password, login_timeout=max(60, min(wait, 180)))
            return {"available": True, "code": code, "detail": f"cdp:{detail}", "method": "cdp"}
        except Exception as e:
            return {"available": True, "code": None, "detail": f"cdp_error:{e}", "method": "cdp"}

    return {"available": False, "code": None, "detail": "no_mail_cred_no_token"}


@app.post("/api/accounts/kakao/release-stale")
def api_accounts_release_stale(db: Session = Depends(get_db)):
    return {"released": release_stale_account_leases(db)}


@app.get("/accounts", response_class=HTMLResponse)
def ui_accounts(
    request: Request,
    status: Optional[str] = None,
    login_state: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
):
    rows = db.scalars(_account_query(db, status=status, login_state=login_state, q=q).limit(1000)).all()
    return templates.TemplateResponse(
        "accounts.html",
        {
            "request": request,
            "accounts": list(rows),
            "stats": account_stats(db),
            "filter_status": status or "",
            "filter_login_state": login_state or "",
            "q": q or "",
        },
    )
