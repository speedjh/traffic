# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from .traffic import TRAFFIC_TYPES


class CampaignCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    traffic_type: str
    keyword: str = ""
    place_url: str = ""
    place_name: str = ""
    daily_quota: int = Field(..., ge=1, le=100000)
    start_date: date
    days: int = Field(..., ge=1, le=365)
    status: str = "active"
    params: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("traffic_type")
    @classmethod
    def _traffic(cls, v: str) -> str:
        if v not in TRAFFIC_TYPES:
            raise ValueError(f"unsupported traffic_type: {v}")
        return v


class CampaignUpdate(BaseModel):
    name: Optional[str] = None
    traffic_type: Optional[str] = None
    keyword: Optional[str] = None
    place_url: Optional[str] = None
    place_name: Optional[str] = None
    daily_quota: Optional[int] = Field(default=None, ge=1, le=100000)
    start_date: Optional[date] = None
    days: Optional[int] = Field(default=None, ge=1, le=365)
    status: Optional[str] = None
    params: Optional[Dict[str, Any]] = None


class CampaignResume(BaseModel):
    """재개 시 일유입량·진행 일수를 다시 받음. 시작일은 기본 오늘."""

    daily_quota: int = Field(..., ge=1, le=100000)
    days: int = Field(..., ge=1, le=365)
    start_date: Optional[date] = None


class CampaignPacingUpdate(BaseModel):
    """진행 중 캠페인 설정 즉시 반영.

    일유입량·일수(큐 재구축) 및/또는 키워드·2차키워드·체류·장소 URL.
    키워드류는 claim 시 캠페인 행을 읽으므로 큐 재구축 없이 다음 job부터 적용.
    """

    daily_quota: Optional[int] = Field(default=None, ge=1, le=100000)
    days: Optional[int] = Field(default=None, ge=1, le=365)
    keyword: Optional[str] = None
    keyword2: Optional[str] = None
    place_name: Optional[str] = None
    place_url: Optional[str] = None
    dwell_min: Optional[float] = Field(default=None, ge=1)
    dwell_max: Optional[float] = Field(default=None, ge=1)


class CampaignOut(BaseModel):
    id: int
    name: str
    traffic_type: str
    keyword: str
    place_url: str
    place_name: str
    daily_quota: int
    start_date: date
    days: int
    end_date: date
    status: str
    params: Dict[str, Any]
    created_at: datetime

    class Config:
        from_attributes = True


class JobOut(BaseModel):
    id: int
    campaign_id: int
    campaign_name: str = ""
    traffic_type: str = ""
    schedule_date: date
    queue_seq: int
    scheduled_at: Optional[datetime]
    status: str
    device_serial: Optional[str]
    worker_id: Optional[str]
    error_message: Optional[str] = None

    class Config:
        from_attributes = True


class DeviceHeartbeat(BaseModel):
    serial: str
    host: str = ""
    worker_id: str
    busy_job_id: Optional[int] = None
    adb_online: Optional[bool] = None


class DeviceOut(BaseModel):
    serial: str
    host: str
    worker_id: str
    last_heartbeat: Optional[datetime]
    busy_job_id: Optional[int]
    online: bool = False
    adb_online: Optional[bool] = None

    class Config:
        from_attributes = True


class ClaimRequest(BaseModel):
    serial: str
    worker_id: str
    host: str = ""


class JobReport(BaseModel):
    worker_id: str
    serial: str
    status: str  # success | failed
    error_message: Optional[str] = None
    log_path: Optional[str] = None


class JobRelease(BaseModel):
    worker_id: str
    serial: str
    reason: Optional[str] = None


class DashboardOut(BaseModel):
    today: date
    pending_today: int
    running: int
    success_today: int
    failed_today: int
    campaigns: List[Dict[str, Any]]
    devices: List[DeviceOut]
    queue_preview: List[JobOut]


# ── 카카오 계정 풀 ────────────────────────────────────────────────────────
class KakaoAccountOut(BaseModel):
    id: int
    email: str
    nickname: str = ""
    auth_method: str = ""
    status: str
    login_state: str
    daily_limit: int
    used_today: int
    total_used: int
    fail_streak: int
    last_used_at: Optional[datetime] = None
    last_result: str = ""
    leased_by: Optional[str] = None
    warmed_at: Optional[datetime] = None
    note: str = ""
    oauth_status: str = ""
    mail_email: str = ""

    class Config:
        from_attributes = True


class KakaoAccountCreate(BaseModel):
    email: str = Field(..., min_length=3, max_length=200)
    password: str = Field(..., min_length=1, max_length=200)
    nickname: str = ""
    auth_method: str = ""
    daily_limit: int = Field(default=3, ge=0, le=1000)
    status: str = "active"


class KakaoAccountUpdate(BaseModel):
    password: Optional[str] = None
    nickname: Optional[str] = None
    daily_limit: Optional[int] = Field(default=None, ge=0, le=1000)
    status: Optional[str] = None
    login_state: Optional[str] = None
    note: Optional[str] = None


class AccountLeaseRequest(BaseModel):
    serial: str
    worker_id: str = ""
    job_id: Optional[int] = None


class AccountReport(BaseModel):
    serial: str = ""
    result: str  # success | traffic_failed | verify_required | bad_credential | blocked | login_failed
    note: str = ""
