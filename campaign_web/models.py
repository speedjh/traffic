# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    """로컬 나이지 시각 (운영 PC 타임존 기준). 함수명은 호환용."""
    return datetime.now()


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    traffic_type: Mapped[str] = mapped_column(String(64), nullable=False)
    keyword: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    place_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    place_name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    daily_quota: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    days: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    jobs: Mapped[list["Job"]] = relationship(back_populates="campaign", cascade="all, delete-orphan")
    day_stats: Mapped[list["CampaignDay"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )

    @property
    def params(self) -> dict:
        try:
            return json.loads(self.params_json or "{}")
        except Exception:
            return {}

    @params.setter
    def params(self, value: Any) -> None:
        self.params_json = json.dumps(value or {}, ensure_ascii=False)

    @property
    def end_date(self) -> date:
        from datetime import timedelta

        return self.start_date + timedelta(days=max(self.days, 1) - 1)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("schedule_date", "queue_seq", name="uq_day_queue_seq"),
        Index("ix_jobs_claim", "status", "schedule_date", "queue_seq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    schedule_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    queue_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    device_serial: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    worker_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    leased_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    log_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    campaign: Mapped["Campaign"] = relationship(back_populates="jobs")


class CampaignDay(Base):
    __tablename__ = "campaign_days"
    __table_args__ = (UniqueConstraint("campaign_id", "date", name="uq_campaign_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    date: Mapped[date] = mapped_column(Date, nullable=False)
    planned_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    done_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    campaign: Mapped["Campaign"] = relationship(back_populates="day_stats")


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    serial: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    host: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    worker_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    last_heartbeat: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    busy_job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class KakaoAccount(Base):
    """카카오맵 로그인 트래픽용 계정 풀.

    할당 규칙: '투입이 제일 오래된 순' — last_used_at ASC (미투입 계정이 최우선).
    아웃룩 이메일·비번은 카카오 계정 정보가 아니므로 저장하지 않는다.
    """

    __tablename__ = "kakao_accounts"
    __table_args__ = (
        Index("ix_kakao_accounts_pick", "status", "last_used_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, nullable=False, index=True)
    password: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    nickname: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    auth_method: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    # 코드 자동 수신(OCR)용 메일 자격 — 카카오 인증번호가 도착하는 사서함.
    # 아웃룩 계정만 채워지며, 오직 인증번호 읽기에만 쓴다.
    mail_email: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    mail_password: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # 서버 사이드 코드 수신용 OAuth (Microsoft) — refresh_token 으로 IMAP XOAUTH2 접근
    oauth_client_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    oauth_refresh_token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    oauth_status: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    # active 만 할당 대상. inactive=수동 보류, disabled=사용 불가로 확정
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    # unknown=미시도, ok=로그인 성공, verify_required=추가인증 요구,
    # bad_credential=아이디/비번 오류, blocked=일시 잠금
    login_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")

    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    used_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    total_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fail_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 투입 이력 — 할당 정렬 기준
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    last_result: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    # 리스(대여) 상태 — 워커가 로테이션 동안 점유
    leased_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    leased_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    lease_job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # 이 기기에서 최초 1회 추가인증을 통과시킨 시각(예열 완료)
    warmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    @property
    def available_today(self) -> bool:
        if self.status != "active":
            return False
        if self.daily_limit <= 0:
            return True
        if self.used_date != date.today():
            return True
        return self.used_today < self.daily_limit
