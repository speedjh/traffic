# -*- coding: utf-8 -*-
"""카카오 계정 풀 — 임포트 / 할당(리스) / 결과 반영.

할당 규칙: **투입이 제일 오래된 순**.
  1) 한 번도 안 쓴 계정(last_used_at IS NULL) 최우선
  2) 그다음 last_used_at 오래된 순
로테이션(=job)마다 새 계정을 리스하므로 자연스럽게 계정이 순환한다.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .models import KakaoAccount, utcnow

# 계정 리스가 이 시간 넘게 반납 안 되면 회수 (job lease 보다 넉넉히)
ACCOUNT_LEASE_TIMEOUT_SECS = 1800

# 워커가 보고할 수 있는 결과값 → (login_state, 계정을 죽일지)
REPORT_RESULTS: Dict[str, Tuple[str, bool]] = {
    "success": ("ok", False),
    "traffic_failed": ("ok", False),      # 로그인은 됐고 트래픽만 실패
    "verify_required": ("verify_required", False),
    "bad_credential": ("bad_credential", True),
    "blocked": ("blocked", False),
    "login_failed": ("unknown", False),   # 원인 미상
}

# 연속 실패가 이 횟수에 도달하면 자동으로 status=inactive
FAIL_STREAK_LIMIT = 3


# ── 임포트 ────────────────────────────────────────────────────────────────
# 엑셀 헤더 → 모델 필드. '아웃룩이메일'/'아웃룩비번' 은 카카오 계정 정보가
# 아니므로 의도적으로 매핑하지 않는다(저장 안 함).
COLUMN_ALIASES: Dict[str, List[str]] = {
    "email": ["카카오이메일", "이메일", "아이디", "email", "id"],
    "password": ["비밀번호", "비번", "password", "pw"],
    "nickname": ["닉네임", "nickname", "nick"],
    "auth_method": ["인증방식", "auth_method"],
    "daily_limit": ["일일제한", "daily_limit"],
    "status": ["상태", "status"],
    "mail_email": ["아웃룩이메일", "아웃룩 이메일", "메일이메일", "mail_email"],
    "mail_password": ["아웃룩비번", "아웃룩 비번", "메일비번", "mail_password"],
}

# 아웃룩 메일 자격은 '카카오 인증번호 수신' 목적으로만 저장한다(로그인 계정 정보 아님).
_SKIP_COLUMNS = set()


def _norm_header(name: str) -> str:
    return re.sub(r"\s+", "", (name or "")).lower()


def _map_headers(headers: List[str]) -> Dict[str, int]:
    """엑셀 헤더 목록 → {필드명: 열 인덱스}."""
    out: Dict[str, int] = {}
    norm = [_norm_header(h) for h in headers]
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            a = _norm_header(alias)
            if a in norm:
                out[field] = norm.index(a)
                break
    return out


def _clean_status(value: str) -> str:
    v = (value or "").strip().lower()
    if v in ("active", "정상", "사용", "사용중"):
        return "active"
    if v in ("disabled", "삭제", "차단", "불가"):
        return "disabled"
    if v:
        return "inactive"
    return "active"


def parse_xlsx(data: bytes) -> Tuple[List[Dict[str, Any]], List[str]]:
    """openpyxl 없이 xlsx 첫 시트를 파싱해 계정 dict 목록을 만든다.

    Returns: (rows, skipped_columns)
    """
    import io
    import xml.etree.ElementTree as ET
    import zipfile

    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    zf = zipfile.ZipFile(io.BytesIO(data))

    shared: List[str] = []
    if "xl/sharedStrings.xml" in zf.namelist():
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in root.iter(NS + "si"):
            shared.append("".join(t.text or "" for t in si.iter(NS + "t")))

    sheet_names = [n for n in zf.namelist() if n.startswith("xl/worksheets/sheet")]
    if not sheet_names:
        raise ValueError("워크시트를 찾을 수 없습니다")
    root = ET.fromstring(zf.read(sorted(sheet_names)[0]))

    grid: List[Dict[int, str]] = []
    for row in root.iter(NS + "row"):
        cells: Dict[int, str] = {}
        for c in row:
            ref = c.get("r") or ""
            m = re.match(r"([A-Z]+)", ref)
            if not m:
                continue
            col = 0
            for ch in m.group(1):
                col = col * 26 + (ord(ch) - 64)
            col -= 1
            inline = c.find(NS + "is")
            v = c.find(NS + "v")
            if inline is not None:
                val = "".join(t.text or "" for t in inline.iter(NS + "t"))
            elif v is None:
                val = ""
            elif c.get("t") == "s":
                idx = int(v.text or 0)
                val = shared[idx] if 0 <= idx < len(shared) else ""
            else:
                val = v.text or ""
            cells[col] = (val or "").strip()
        grid.append(cells)

    if not grid:
        return [], []

    width = max((max(c) + 1) if c else 0 for c in grid)
    headers = [grid[0].get(i, "") for i in range(width)]
    idx = _map_headers(headers)
    if "email" not in idx:
        raise ValueError("'카카오이메일' 열을 찾을 수 없습니다")

    skipped = [h for h in headers if _norm_header(h) in {_norm_header(s) for s in _SKIP_COLUMNS}]

    rows: List[Dict[str, Any]] = []
    for cells in grid[1:]:
        get = lambda f: cells.get(idx[f], "") if f in idx else ""  # noqa: E731
        email = get("email").strip()
        if not email or "@" not in email:
            continue
        try:
            limit = int(float(get("daily_limit") or 3))
        except ValueError:
            limit = 3
        rows.append(
            {
                "email": email.lower(),
                "password": get("password"),
                "nickname": get("nickname"),
                "auth_method": get("auth_method"),
                "daily_limit": limit,
                "status": _clean_status(get("status")),
                "mail_email": get("mail_email").strip(),
                "mail_password": get("mail_password").strip(),
            }
        )
    return rows, skipped


def import_accounts(db: Session, rows: List[Dict[str, Any]]) -> Dict[str, int]:
    """이메일 기준 upsert. 비번/닉네임 등은 갱신하되 투입 이력은 보존."""
    created = updated = 0
    existing = {a.email: a for a in db.scalars(select(KakaoAccount)).all()}
    for row in rows:
        acc = existing.get(row["email"])
        if acc is None:
            acc = KakaoAccount(email=row["email"])
            db.add(acc)
            existing[row["email"]] = acc
            created += 1
        else:
            updated += 1
        if row.get("password"):
            acc.password = row["password"]
        acc.nickname = row.get("nickname") or acc.nickname
        acc.auth_method = row.get("auth_method") or acc.auth_method
        if row.get("mail_email"):
            acc.mail_email = row["mail_email"]
        if row.get("mail_password"):
            acc.mail_password = row["mail_password"]
        acc.daily_limit = row.get("daily_limit", acc.daily_limit)
        # 이미 disabled 로 확정된 계정은 엑셀 상태로 되살리지 않는다
        if acc.status != "disabled":
            acc.status = row.get("status", acc.status)
    db.commit()
    return {"created": created, "updated": updated, "total": len(rows)}


# ── 할당(리스) ────────────────────────────────────────────────────────────
def release_stale_leases(db: Session, now: Optional[datetime] = None) -> int:
    """반납 안 된 오래된 리스 회수."""
    now = now or utcnow()
    cutoff = now - timedelta(seconds=ACCOUNT_LEASE_TIMEOUT_SECS)
    stale = list(
        db.scalars(
            select(KakaoAccount).where(
                KakaoAccount.leased_at.is_not(None),
                KakaoAccount.leased_at < cutoff,
            )
        ).all()
    )
    for acc in stale:
        acc.leased_by = None
        acc.leased_at = None
        acc.lease_job_id = None
    if stale:
        db.commit()
    return len(stale)


def lease_account(
    db: Session,
    *,
    serial: str,
    job_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Optional[KakaoAccount]:
    """투입이 제일 오래된 계정 1개를 리스한다. 없으면 None."""
    now = now or utcnow()
    today = date.today()
    release_stale_leases(db, now)

    # 같은 기기가 이미 들고 있는 리스는 그대로 재사용(중복 점유 방지)
    held = db.scalars(
        select(KakaoAccount).where(
            KakaoAccount.leased_by == serial,
            KakaoAccount.leased_at.is_not(None),
        )
    ).first()
    if held is not None:
        if job_id is not None and held.lease_job_id == job_id:
            return held
        # 다른 job 의 잔여 리스 → 반납하고 새로 뽑는다
        held.leased_by = None
        held.leased_at = None
        held.lease_job_id = None
        db.commit()

    q = (
        select(KakaoAccount)
        .where(
            KakaoAccount.status == "active",
            KakaoAccount.password != "",
            KakaoAccount.leased_at.is_(None),
            # 일일제한: 0 이면 무제한, 날짜가 바뀌었으면 리셋된 것으로 본다
            or_(
                KakaoAccount.daily_limit <= 0,
                KakaoAccount.used_date.is_(None),
                KakaoAccount.used_date != today,
                KakaoAccount.used_today < KakaoAccount.daily_limit,
            ),
        )
        # NULL(미투입) 최우선 → 그다음 오래된 순
        .order_by(
            KakaoAccount.last_used_at.is_(None).desc(),
            KakaoAccount.last_used_at.asc(),
            KakaoAccount.id.asc(),
        )
        .limit(1)
    )
    acc = db.scalars(q).first()
    if acc is None:
        return None

    acc.leased_by = serial
    acc.leased_at = now
    acc.lease_job_id = job_id
    db.commit()
    db.refresh(acc)
    return acc


def report_account(
    db: Session,
    *,
    account_id: int,
    result: str,
    note: str = "",
    now: Optional[datetime] = None,
) -> Optional[KakaoAccount]:
    """리스 반납 + 결과 반영. 투입 시각(last_used_at)은 여기서 갱신된다."""
    now = now or utcnow()
    today = date.today()
    acc = db.get(KakaoAccount, account_id)
    if acc is None:
        return None

    login_state, hard_fail = REPORT_RESULTS.get(result, ("unknown", False))
    acc.login_state = login_state
    acc.last_result = result
    acc.last_used_at = now
    acc.total_used += 1
    if acc.used_date != today:
        acc.used_date = today
        acc.used_today = 0
    acc.used_today += 1

    if result in ("success", "traffic_failed"):
        acc.fail_streak = 0
        if acc.warmed_at is None:
            acc.warmed_at = now
    else:
        acc.fail_streak += 1

    if hard_fail or acc.fail_streak >= FAIL_STREAK_LIMIT:
        acc.status = "inactive"

    if note:
        acc.note = note

    acc.leased_by = None
    acc.leased_at = None
    acc.lease_job_id = None
    db.commit()
    db.refresh(acc)
    return acc


def account_stats(db: Session) -> Dict[str, Any]:
    today = date.today()
    total = db.scalar(select(func.count()).select_from(KakaoAccount)) or 0
    by_status = dict(
        db.execute(
            select(KakaoAccount.status, func.count()).group_by(KakaoAccount.status)
        ).all()
    )
    by_login = dict(
        db.execute(
            select(KakaoAccount.login_state, func.count()).group_by(KakaoAccount.login_state)
        ).all()
    )
    leased = db.scalar(
        select(func.count()).select_from(KakaoAccount).where(KakaoAccount.leased_at.is_not(None))
    ) or 0
    never_used = db.scalar(
        select(func.count()).select_from(KakaoAccount).where(KakaoAccount.last_used_at.is_(None))
    ) or 0
    used_today = db.scalar(
        select(func.count()).select_from(KakaoAccount).where(KakaoAccount.used_date == today)
    ) or 0
    return {
        "total": total,
        "by_status": by_status,
        "by_login_state": by_login,
        "leased": leased,
        "never_used": never_used,
        "used_today": used_today,
    }
