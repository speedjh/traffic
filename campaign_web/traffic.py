# -*- coding: utf-8 -*-
"""트래픽 종류 정의 + 실행 커맨드 매핑."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import ROOT_DIR


@dataclass(frozen=True)
class TrafficType:
    key: str
    label: str
    description: str
    needs_place: bool = False
    needs_place_name: bool = False
    needs_account: bool = False   # 카카오 계정 풀에서 계정을 리스해 로그인


TRAFFIC_TYPES: Dict[str, TrafficType] = {
    "daum_jawan": TrafficType(
        key="daum_jawan",
        label="다음 자완",
        description="다음앱 검색 후 파워링크·관련검색어·장소 제외, 결과 글(블로그/웹/뉴스 등)을 클릭합니다.",
    ),
    "daum_click": TrafficType(
        key="daum_click",
        label="다음 클릭형",
        description="다음앱 검색 → 장소(플레이스) 클릭 → 카카오맵 전환만 확인하고 종료합니다. 맵 안 추가 액션 없음.",
        needs_place=True,
    ),
    "daum_dwell": TrafficType(
        key="daum_dwell",
        label="다음 체류형",
        description="다음앱에서 장소 클릭 후 카카오맵으로 들어가 업체 상세 탭을 클릭·스크롤하며 체류합니다.",
        needs_place=True,
    ),
    "daum_full": TrafficType(
        key="daum_full",
        label="다음 풀플로우",
        description="다음앱 장소 클릭 → 카카오맵 전환 + 업체 클릭 + 상세 스크롤까지 수행합니다.",
        needs_place=True,
    ),
    "gs_click": TrafficType(
        key="gs_click",
        label="우리동네GS 클릭",
        description="우리동네GS 앱에서 하단 검색 → 키워드 입력 → GS25배달 구역 상품 1개를 클릭합니다.",
    ),
    "kakao_search": TrafficType(
        key="kakao_search",
        label="카카오맵 일반트래픽",
        description=(
            "카카오맵 검색 → 업체 진입 → 업체명 클릭 후 메뉴/사진/후기/블로그를 랜덤 클릭·스크롤하며 "
            "체류합니다. 앱 데이터 삭제·IP 변경 후 진입할 때 카카오 계정으로 로그인하며, "
            "로테이션마다 '투입이 가장 오래된' 계정으로 교체됩니다. 체류시간은 캠페인에서 설정."
        ),
        needs_place_name=True,
        needs_account=True,
    ),
    "kakao_route": TrafficType(
        key="kakao_route",
        label="카카오맵 길찾기",
        description="카카오맵 앱을 직접 켜고 검색 → 업체 진입 → 도착 → 출발지 입력 → 경로 결과까지 진행합니다.",
        needs_place_name=True,
    ),
    "hamman_find": TrafficType(
        key="hamman_find",
        label="함많찾을",
        description="Chrome에서 네이버 검색 → 결과 클릭·체류 → 2차 키워드 검색·클릭·체류 → Chrome 초기화 → IP 변경.",
        needs_place_name=True,
    ),
}


def traffic_list() -> List[TrafficType]:
    return list(TRAFFIC_TYPES.values())


def needs_account(traffic_type: str) -> bool:
    t = TRAFFIC_TYPES.get(traffic_type)
    return bool(t and t.needs_account)


def build_command(
    traffic_type: str,
    *,
    keyword: str,
    serial: str,
    place_url: str = "",
    place_name: str = "",
    params: Optional[Dict[str, Any]] = None,
    account: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """워커가 subprocess로 실행할 argv 목록.

    account: {"email": ..., "password": ...} — kakao_search 로그인용.
    """
    params = params or {}
    root = ROOT_DIR

    common = ["--serial", serial, "--rotations", "1"]

    if traffic_type == "daum_jawan":
        return [str(root / "run_daum_jawan.sh"), keyword, *common]

    if traffic_type == "daum_click":
        cmd = [str(root / "run_daum_click.sh"), keyword, "--place-url", place_url or "", *common]
        if place_name:
            cmd.extend(["--place-name", place_name])
        return cmd

    if traffic_type == "daum_dwell":
        cmd = [str(root / "run_daum_dwell.sh"), keyword, "--place-url", place_url or "", *common]
        if place_name:
            cmd.extend(["--place-name", place_name])
        return cmd

    if traffic_type == "daum_full":
        cmd = [str(root / "run_daum.sh"), keyword, "--place-url", place_url or "", *common]
        if place_name:
            cmd.extend(["--place-name", place_name])
        return cmd

    if traffic_type == "gs_click":
        return [str(root / "run_gs_click.sh"), keyword, *common]

    if traffic_type in ("kakao_search", "kakao_route"):
        mode = "search" if traffic_type == "kakao_search" else "route"
        py = root / "CLAUDE" / "kakao_search_device.py"
        place = place_name or keyword
        cmd = [
            "python3",
            str(py),
            keyword,
            place,
            "--mode",
            mode,
            "--partial",
            *common,
        ]
        if traffic_type == "kakao_search":
            dwell_min = params.get("dwell_min", 15)
            dwell_max = params.get("dwell_max", 20)
            cmd.extend(["--dwell-min", str(dwell_min), "--dwell-max", str(dwell_max)])
            if account and account.get("email") and account.get("password"):
                cmd.extend([
                    "--kakao-id", str(account["email"]),
                    "--kakao-pw", str(account["password"]),
                ])
                if account.get("id"):
                    cmd.extend(["--account-id", str(account["id"])])
                if account.get("api"):
                    cmd.extend(["--code-api", str(account["api"])])
        return cmd

    if traffic_type == "hamman_find":
        kw2 = (
            params.get("keyword2")
            or place_name
            or ""
        )
        dwell_min = params.get("dwell_min", 15)
        dwell_max = params.get("dwell_max", 20)
        cmd = [str(root / "run_hamman.sh"), keyword, *common]
        if kw2:
            cmd.extend(["--keyword2", str(kw2)])
        cmd.extend(["--dwell-min", str(dwell_min), "--dwell-max", str(dwell_max)])
        return cmd

    raise ValueError(f"unknown traffic_type: {traffic_type}")
