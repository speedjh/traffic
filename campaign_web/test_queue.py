# -*- coding: utf-8 -*-
"""인터리브 스케줄 + 멀티 claim 검증."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

# Isolate DB before importing app modules that bind engine
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["CAMPAIGN_DB_URL"] = f"sqlite:///{_tmp.name}"

from fastapi.testclient import TestClient  # noqa: E402

from campaign_web.db import SessionLocal, init_db  # noqa: E402
from campaign_web.main import app  # noqa: E402
from campaign_web.models import Campaign, Job  # noqa: E402
from campaign_web.scheduler import (  # noqa: E402
    claim_next_job,
    date_range,
    interleave_round_robin,
    materialize_campaign,
    rebuild_day_queue,
)


class InterleaveTests(unittest.TestCase):
    def test_round_robin_mixes_campaigns(self):
        order = interleave_round_robin({1: [1, 1, 1], 2: [2, 2], 3: [3]})
        self.assertEqual(order, [1, 2, 3, 1, 2, 1])

    def test_date_range_days(self):
        days = date_range(date(2026, 7, 15), 5)
        self.assertEqual(
            days,
            [
                date(2026, 7, 15),
                date(2026, 7, 16),
                date(2026, 7, 17),
                date(2026, 7, 18),
                date(2026, 7, 19),
            ],
        )


class QueueMaterializeTests(unittest.TestCase):
    def setUp(self):
        init_db()
        self.db = SessionLocal()
        # clean tables between tests
        for tbl in (Job, Campaign):
            self.db.query(tbl).delete()
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_mixed_queue_not_block_by_campaign(self):
        start = date.today()
        for name, tid, quota in (("A", "daum_jawan", 3), ("B", "gs_click", 3), ("C", "daum_click", 3)):
            c = Campaign(
                name=name,
                traffic_type=tid,
                keyword="kw",
                place_url="https://place.map.kakao.com/1" if "click" in tid else "",
                daily_quota=quota,
                start_date=start,
                days=1,
                status="active",
            )
            self.db.add(c)
        self.db.commit()
        rebuild_day_queue(self.db, start)
        jobs = (
            self.db.query(Job)
            .filter(Job.schedule_date == start)
            .order_by(Job.queue_seq.asc())
            .all()
        )
        names = [j.campaign.name for j in jobs]
        self.assertEqual(len(names), 9)
        # Must not be AAA BBB CCC
        self.assertNotEqual(names, ["A"] * 3 + ["B"] * 3 + ["C"] * 3)
        # First 3 should be one of each (round-robin)
        self.assertEqual(set(names[:3]), {"A", "B", "C"})

    def test_daily_quota_times_days(self):
        start = date.today()
        c = Campaign(
            name="Solo",
            traffic_type="gs_click",
            keyword="폴라레티",
            daily_quota=2,
            start_date=start,
            days=3,
            status="active",
        )
        self.db.add(c)
        self.db.commit()
        materialize_campaign(self.db, c)
        total = self.db.query(Job).count()
        self.assertEqual(total, 6)


class MultiDeviceClaimTests(unittest.TestCase):
    def setUp(self):
        init_db()
        self.client = TestClient(app)
        db = SessionLocal()
        for tbl in (Job, Campaign):
            db.query(tbl).delete()
        db.commit()
        # create A/B campaigns with future-or-now scheduled jobs by rebuilding with past window
        start = date.today()
        for name, tt in (("A", "daum_jawan"), ("B", "gs_click")):
            c = Campaign(
                name=name,
                traffic_type=tt,
                keyword="k",
                daily_quota=4,
                start_date=start,
                days=1,
                status="active",
            )
            db.add(c)
        db.commit()
        rebuild_day_queue(db, start)
        # Make all jobs claimable now
        for j in db.query(Job).all():
            j.scheduled_at = datetime.now() - timedelta(minutes=1)
        db.commit()
        db.close()

    def test_two_devices_claim_different_jobs(self):
        r1 = self.client.post(
            "/api/worker/claim",
            json={"serial": "DEV-A", "worker_id": "w-a", "host": "h1"},
        )
        r2 = self.client.post(
            "/api/worker/claim",
            json={"serial": "DEV-B", "worker_id": "w-b", "host": "h1"},
        )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        j1 = r1.json()["job"]
        j2 = r2.json()["job"]
        self.assertIsNotNone(j1)
        self.assertIsNotNone(j2)
        self.assertNotEqual(j1["id"], j2["id"])
        # Same device cannot get second while busy
        r3 = self.client.post(
            "/api/worker/claim",
            json={"serial": "DEV-A", "worker_id": "w-a", "host": "h1"},
        )
        self.assertEqual(r3.json()["job"]["id"], j1["id"])

        # report and claim next
        self.client.post(
            f"/api/worker/jobs/{j1['id']}/report",
            json={"worker_id": "w-a", "serial": "DEV-A", "status": "success"},
        )
        r4 = self.client.post(
            "/api/worker/claim",
            json={"serial": "DEV-A", "worker_id": "w-a", "host": "h1"},
        )
        self.assertIsNotNone(r4.json()["job"])
        self.assertNotEqual(r4.json()["job"]["id"], j1["id"])


class ClaimRoundRobinTests(unittest.TestCase):
    def setUp(self):
        init_db()
        self.db = SessionLocal()
        for tbl in (Job, Campaign):
            self.db.query(tbl).delete()
        self.db.commit()
        start = date.today()
        for name in ("A", "B", "C"):
            self.db.add(
                Campaign(
                    name=name,
                    traffic_type="hamman_find",
                    keyword=name,
                    daily_quota=3,
                    start_date=start,
                    days=1,
                    status="active",
                )
            )
        self.db.commit()
        rebuild_day_queue(self.db, start)
        for j in self.db.query(Job).all():
            j.scheduled_at = datetime.now() - timedelta(minutes=1)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_claim_rotates_across_campaigns(self):
        serial = "RR-DEV"
        seen = []
        for i in range(6):
            job = claim_next_job(self.db, serial=serial, worker_id=f"w-{i}")
            self.assertIsNotNone(job)
            seen.append(job.campaign.name)
            # finish so next claim is allowed
            job.status = "success"
            job.finished_at = datetime.now()
            self.db.commit()
        # Must not be AAA BBB — should rotate A,B,C,A,B,C (by campaign id order)
        self.assertEqual(seen[:3], ["A", "B", "C"])
        self.assertEqual(seen[3:6], ["A", "B", "C"])


class ClaimTrafficTypeFairnessTests(unittest.TestCase):
    """hamman backlog 가 queue_seq 앞을 독점해도 kakao 가 claim 되게."""

    def setUp(self):
        init_db()
        self.db = SessionLocal()
        for tbl in (Job, Campaign):
            self.db.query(tbl).delete()
        self.db.commit()
        start = date.today()
        ham = Campaign(
            name="HamMany",
            traffic_type="hamman_find",
            keyword="ham",
            place_name="ham2",
            daily_quota=1,
            start_date=start,
            days=1,
            status="active",
        )
        kak = Campaign(
            name="KakaoOne",
            traffic_type="kakao_search",
            keyword="kak",
            place_name="place",
            daily_quota=1,
            start_date=start,
            days=1,
            status="active",
        )
        self.db.add(ham)
        self.db.add(kak)
        self.db.commit()
        # 예전 bug: queue_seq 낮은 hamman 수백 건이 limit(300) 을 채워 kakao 제외
        past = start - timedelta(days=3)
        for i in range(350):
            self.db.add(
                Job(
                    campaign_id=ham.id,
                    schedule_date=past,
                    queue_seq=i + 1,
                    scheduled_at=datetime.now() - timedelta(minutes=5),
                    status="pending",
                )
            )
        self.db.add(
            Job(
                campaign_id=kak.id,
                schedule_date=past,
                queue_seq=9000,
                scheduled_at=datetime.now() - timedelta(minutes=5),
                status="pending",
            )
        )
        self.db.commit()
        self.ham_id = ham.id
        self.kak_id = kak.id

    def tearDown(self):
        self.db.close()

    def test_kakao_not_starved_by_hamman_backlog(self):
        serial = "FAIR-DEV"
        types = []
        for i in range(4):
            job = claim_next_job(self.db, serial=serial, worker_id=f"w-{i}")
            self.assertIsNotNone(job)
            types.append(job.campaign.traffic_type)
            job.status = "success"
            job.finished_at = datetime.now()
            self.db.commit()
        # traffic-type RR: hamman / kakao 교대
        self.assertIn("kakao_search", types)
        self.assertEqual(types.count("kakao_search"), 1)  # only 1 kakao job
        # 첫 두 claim 에 서로 다른 타입이 나와야 함
        self.assertEqual(set(types[:2]), {"hamman_find", "kakao_search"})


class ExpireResumeTests(unittest.TestCase):
    def setUp(self):
        init_db()
        self.db = SessionLocal()
        for tbl in (Job, Campaign):
            self.db.query(tbl).delete()
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_expire_past_pauses_and_blocks_claim(self):
        from campaign_web.scheduler import expire_past_campaigns, resume_campaign

        past = date.today() - timedelta(days=10)
        c = Campaign(
            name="Old",
            traffic_type="gs_click",
            keyword="k",
            daily_quota=2,
            start_date=past,
            days=2,
            status="active",
        )
        self.db.add(c)
        self.db.commit()
        rebuild_day_queue(self.db, past)
        for j in self.db.query(Job).all():
            j.scheduled_at = datetime.now() - timedelta(minutes=1)
        self.db.commit()

        n = expire_past_campaigns(self.db)
        self.assertEqual(n, 1)
        self.db.refresh(c)
        self.assertEqual(c.status, "paused")
        job = claim_next_job(self.db, serial="EXP-DEV", worker_id="w")
        self.assertIsNone(job)

    def test_resume_rebuilds_with_new_quota_days(self):
        from campaign_web.scheduler import resume_campaign

        past = date.today() - timedelta(days=10)
        c = Campaign(
            name="ResumeMe",
            traffic_type="gs_click",
            keyword="k",
            daily_quota=2,
            start_date=past,
            days=2,
            status="paused",
        )
        self.db.add(c)
        self.db.commit()
        # leftover paused jobs from old period
        self.db.add(
            Job(
                campaign_id=c.id,
                schedule_date=past,
                queue_seq=1,
                scheduled_at=datetime.now(),
                status="paused",
            )
        )
        self.db.commit()

        out = resume_campaign(
            self.db, c.id, daily_quota=3, days=2, start_date=date.today()
        )
        self.assertIsNotNone(out)
        self.assertEqual(out.status, "active")
        self.assertEqual(out.daily_quota, 3)
        self.assertEqual(out.days, 2)
        self.assertEqual(out.start_date, date.today())
        # old leftover gone; new period jobs exist
        old = (
            self.db.query(Job)
            .filter(Job.campaign_id == c.id, Job.schedule_date == past)
            .count()
        )
        self.assertEqual(old, 0)
        new_count = (
            self.db.query(Job)
            .filter(Job.campaign_id == c.id, Job.status == "pending")
            .count()
        )
        self.assertEqual(new_count, 6)  # 3/day * 2 days

    def test_update_pacing_quota_and_days_immediate(self):
        from campaign_web.scheduler import update_campaign_pacing

        start = date.today()
        c = Campaign(
            name="PaceMe",
            traffic_type="gs_click",
            keyword="k",
            daily_quota=2,
            start_date=start,
            days=2,
            status="active",
        )
        self.db.add(c)
        self.db.commit()
        rebuild_day_queue(self.db, start)
        rebuild_day_queue(self.db, start + timedelta(days=1))
        before = (
            self.db.query(Job)
            .filter(Job.campaign_id == c.id, Job.status == "pending")
            .count()
        )
        self.assertEqual(before, 4)  # 2*2

        out = update_campaign_pacing(self.db, c.id, daily_quota=3, days=3)
        self.assertIsNotNone(out)
        self.assertEqual(out.daily_quota, 3)
        self.assertEqual(out.days, 3)
        pending = (
            self.db.query(Job)
            .filter(Job.campaign_id == c.id, Job.status == "pending")
            .count()
        )
        self.assertEqual(pending, 9)  # 3/day * 3 days
        # shortened: drop day beyond new end
        out2 = update_campaign_pacing(self.db, c.id, daily_quota=3, days=1)
        self.assertEqual(out2.days, 1)
        day2 = (
            self.db.query(Job)
            .filter(
                Job.campaign_id == c.id,
                Job.schedule_date == start + timedelta(days=1),
                Job.status == "pending",
            )
            .count()
        )
        self.assertEqual(day2, 0)
        today_pending = (
            self.db.query(Job)
            .filter(
                Job.campaign_id == c.id,
                Job.schedule_date == start,
                Job.status == "pending",
            )
            .count()
        )
        self.assertEqual(today_pending, 3)


if __name__ == "__main__":
    unittest.main()
