"""端到端领域测试：去重、年龄边界、借阅延迟、审核回避、库存并发竞争等。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.adapters import LoanAdapter, ReminderAdapter, fmt, now
from app.db import connect, init_db
from app.service import ActivityService, ChallengeService, DomainError, age_on


class ChallengeTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "test.db")
        self.conn = connect(self.db_path)
        init_db(self.conn)
        self.acts = ActivityService(self.conn)
        self.chl = ChallengeService(self.conn, ReminderAdapter(self.conn, ":memory:"))
        # reminders.log 会落到默认路径，测试里重定向到临时目录
        self.chl.reminders.log_path = Path(self.tmpdir.name) / "reminders.log"
        self._build()

    def tearDown(self):
        self.conn.close()
        self.tmpdir.cleanup()

    def svc(self):
        """每个并发馆员用独立连接（独立会话）。"""
        c = connect(self.db_path)
        return c, ChallengeService(c, ReminderAdapter(c, Path(self.tmpdir.name) / "reminders.log"))

    def _build(self):
        # 一个 6-8 岁活动，2026-09-15 开始，目标 3 本，库存 1
        self.aid = self.acts.create_activity(
            "6-8挑战", 6, 8, "2026-09-15", "2026-12-15",
            goal_books=3, loan_days=14, medal_stock=1,
        )
        self.bids = [
            self.acts.add_book("书A", "作者", "ISBN-A"),
            self.acts.add_book("书A副本", "作者", "ISBN-A"),   # 重复书目
            self.acts.add_book("书B", "作者", "ISBN-B"),
            self.acts.add_book("书C", "作者", "ISBN-C"),
            self.acts.add_book("书D", "作者", "ISBN-D"),
        ]
        for b in self.bids:
            self.acts.attach_book(self.aid, b)
        # 用户：家长 p1、p2；馆员 lib（自己孩子 kid_self）、lib2
        self.p1 = self.conn.execute("INSERT INTO users(name,role) VALUES('家长一','parent')").lastrowid
        self.p2 = self.conn.execute("INSERT INTO users(name,role) VALUES('家长二','parent')").lastrowid
        self.lib = self.conn.execute("INSERT INTO users(name,role,own_children) VALUES('馆员甲','librarian','')").lastrowid
        self.lib2 = self.conn.execute("INSERT INTO users(name,role) VALUES('馆员乙','librarian')").lastrowid

    def add_child(self, name, birth, parent):
        return self.conn.execute(
            "INSERT INTO children(name,birth_date,parent_id) VALUES(?,?,?)",
            (name, birth, parent),
        ).lastrowid

    def enroll(self, child):
        return self.chl.enroll(self.aid, child,
                               self.conn.execute("SELECT parent_id FROM children WHERE id=?",
                                                 (child,)).fetchone()["parent_id"])

    def synced_loan(self, child, book):
        """直接造一条已同步借阅。"""
        LoanAdapter(self.conn).record_loan(child_id=child, book_id=book,
                                           activity_id=self.aid, delay_seconds=-3600)
        self.chl.sync_loans()

    def read_and_verify(self, enr, child, bid, *, librarian=None):
        """家长记录+提交，馆员核验一条龙。"""
        parent = self.conn.execute("SELECT parent_id FROM children WHERE id=?", (child,)).fetchone()["parent_id"]
        rid = self.chl.upsert_record(enr, bid, parent, note="读完了")
        self.chl.submit_record(rid, parent)
        self.chl.verify_record(rid, librarian or self.lib2)
        return rid


class TestDedup(ChallengeTestBase):
    def test_same_isbn_rejected(self):
        """同一本书重复计数：换重复书目行再提交应被拒绝。"""
        kid = self.add_child("朵朵", "2018-03-01", self.p1)
        enr = self.enroll(kid)
        self.synced_loan(kid, self.bids[0])
        parent = self.p1
        rid = self.chl.upsert_record(enr, self.bids[0], parent, "读了书A")
        self.chl.submit_record(rid, parent)
        with self.assertRaises(DomainError) as cm:
            self.chl.upsert_record(enr, self.bids[1], parent, "想用重复条目再算一本")
        self.assertIn("重复", str(cm.exception))

    def test_same_book_id_idempotent_but_versioned(self):
        """同一 book_id 第二次提交是修改，不留第二条，版本 +1。"""
        kid = self.add_child("朵朵", "2018-03-01", self.p1)
        enr = self.enroll(kid)
        r1 = self.chl.upsert_record(enr, self.bids[2], self.p1, "初录")
        r2 = self.chl.upsert_record(enr, self.bids[2], self.p1, "补录心得")
        self.assertEqual(r1, r2)
        rec = self.conn.execute("SELECT * FROM reading_records WHERE id=?", (r1,)).fetchone()
        self.assertEqual(rec["current_version"], 2)
        self.assertEqual(rec["status"], "draft")  # 修改后需重新提交
        versions = self.chl.record_versions(r1)
        self.assertEqual([v["change_kind"] for v in versions], ["create", "update"])

    def test_dedup_in_progress_count(self):
        """即使绕过入口，达标计数按 ISBN 去重。"""
        kid = self.add_child("朵朵", "2018-03-01", self.p1)
        enr = self.enroll(kid)
        # 直接插两条 verified 的同 ISBN 记录（绕过服务层）
        for bid in (self.bids[0], self.bids[1]):
            rid = self.conn.execute(
                "INSERT INTO reading_records(enrollment_id,child_id,book_id,status,current_version) "
                "VALUES(?,?,?,'verified',1)", (enr, kid, bid)).lastrowid
            self.conn.execute(
                "INSERT INTO reading_record_versions(record_id,version,book_id,changed_by,change_kind)"
                " VALUES(?,?,?,?,'create')", (rid, 1, bid, self.p1))
        prog = self.chl.progress(enr)
        self.assertEqual(prog["verified_count"], 1)  # 同 ISBN 只算一本
        self.assertFalse(prog["qualified"])


class TestAgeBoundary(ChallengeTestBase):
    def test_boundaries(self):
        # 开始日 2026-09-15
        self.assertEqual(age_on("2020-09-15", "2026-09-15"), 6)   # 恰好下界
        self.assertEqual(age_on("2018-09-15", "2026-09-15"), 8)   # 恰好上界
        self.assertEqual(age_on("2020-09-16", "2026-09-15"), 5)   # 晚一天出生，差一天满6岁
        self.assertEqual(age_on("2018-09-14", "2026-09-15"), 8)
        self.assertEqual(age_on("2018-09-16", "2026-09-15"), 7)

    def test_enroll_rejects_out_of_range(self):
        too_young = self.add_child("小点", "2020-09-16", self.p1)  # 5岁
        too_old = self.add_child("大点", "2017-09-14", self.p2)    # 9岁
        with self.assertRaises(DomainError):
            self.enroll(too_young)
        with self.assertRaises(DomainError):
            self.enroll(too_old)

    def test_cross_age_group_enrolls_other_activity(self):
        """孩子跨年龄组：低龄组报不进，高龄组可以。"""
        aid2 = self.acts.create_activity("9-12挑战", 9, 12, "2026-09-15", "2026-12-31",
                                         goal_books=1, medal_stock=0)
        kid = self.add_child("跨界", "2017-09-14", self.p1)  # 开始日恰好9岁
        with self.assertRaises(DomainError):
            self.chl.enroll(self.aid, kid, self.p1)
        eid = self.chl.enroll(aid2, kid, self.p1)
        self.assertGreater(eid, 0)


class TestLoanDelay(ChallengeTestBase):
    def test_verify_blocked_until_sync(self):
        kid = self.add_child("慢慢", "2019-05-01", self.p1)
        enr = self.enroll(kid)
        # 借阅事件 60 秒后才同步
        LoanAdapter(self.conn, delay_seconds=60).record_loan(
            child_id=kid, book_id=self.bids[2], activity_id=self.aid)
        rid = self.chl.upsert_record(enr, self.bids[2], self.p1)
        self.chl.submit_record(rid, self.p1)
        with self.assertRaises(DomainError) as cm:
            self.chl.verify_record(rid, self.lib2)
        self.assertIn("借阅", str(cm.exception))
        # 时间未到，poll 无效果
        self.assertEqual(self.chl.sync_loans(), 0)
        # 时间到了再轮询
        future = now() + timedelta(seconds=61)
        n = LoanAdapter(self.conn).poll(at=future)
        self.assertEqual(n, 1)
        self.chl.verify_record(rid, self.lib2)  # 不再报错
        self.assertEqual(
            self.conn.execute("SELECT status FROM reading_records WHERE id=?", (rid,)).fetchone()["status"],
            "verified")


class TestReviewerRecusal(ChallengeTestBase):
    def test_librarian_cannot_review_own_child(self):
        kid = self.add_child("管娃", "2018-04-01", self.lib)  # 馆员的孩子也挂在其名下
        self.conn.execute("UPDATE users SET own_children=? WHERE id=?", (str(kid), self.lib))
        enr = self.chl.enroll(self.aid, kid, self.lib)
        self.synced_loan(kid, self.bids[2])
        rid = self.chl.upsert_record(enr, self.bids[2], self.lib)
        self.chl.submit_record(rid, self.lib)
        with self.assertRaises(DomainError) as cm:
            self.chl.verify_record(rid, self.lib)
        self.assertIn("回避", str(cm.exception))
        # 驳回同样禁止
        with self.assertRaises(DomainError):
            self.chl.reject_record(rid, self.lib, "x")
        # 达标审核也禁止
        self.synced_loan(kid, self.bids[3])
        self.synced_loan(kid, self.bids[4])
        for b in (self.bids[3], self.bids[4]):
            r = self.chl.upsert_record(enr, b, self.lib)
            self.chl.submit_record(r, self.lib)
            self.chl.verify_record(r, self.lib2)
        with self.assertRaises(DomainError):
            self.chl.approve_and_grant(enr, self.lib)
        # 第一本由无关联的馆员乙核验
        self.chl.verify_record(rid, self.lib2)
        # 别的馆员可以
        out = self.chl.approve_and_grant(enr, self.lib2)
        self.assertEqual(out["state"], "reserved")
        self.assertTrue(out["voucher"].startswith("LIB-"))

    def test_parent_cannot_touch_other_child(self):
        k1 = self.add_child("甲娃", "2018-01-01", self.p1)
        k2 = self.add_child("乙娃", "2018-01-01", self.p2)
        e1 = self.enroll(k1)
        with self.assertRaises(DomainError):
            self.chl.upsert_record(e1, self.bids[2], self.p2)  # p2 不是家长


class TestMedalCompetition(ChallengeTestBase):
    """库存竞争：两名达标者 + 库存 1，并发审核只发出一枚，另一人候补。"""

    def _qualified_enrollment(self, idx, birth="2018-01-01"):
        kid = self.add_child(f"选手{idx}", birth, self.p1 if idx == 1 else self.p2)
        enr = self.enroll(kid)
        for b in (self.bids[2], self.bids[3], self.bids[4]):
            self.synced_loan(kid, b)
            self.read_and_verify(enr, kid, b, librarian=self.lib2)
        return enr

    def test_sequential_grant_then_waitlist(self):
        e1 = self._qualified_enrollment(1)
        e2 = self._qualified_enrollment(2)
        r1 = self.chl.approve_and_grant(e1, self.lib2)
        r2 = self.chl.approve_and_grant(e2, self.lib2)
        self.assertEqual(r1["state"], "reserved")
        self.assertEqual(r2["state"], "waitlisted")
        self.assertEqual(r2["position"], 1)
        # 不能重复发章
        with self.assertRaises(DomainError):
            self.chl.approve_and_grant(e1, self.lib2)
        # 补货后候补转正
        promoted = self.chl.restock(self.aid, 1)
        self.assertEqual(len(promoted), 1)
        m2 = self.chl.medal_status(e2)
        self.assertEqual(m2["state"], "reserved")
        self.assertIsNotNone(m2["code"])
        # 凭证核销幂等
        self.chl.pickup(m2["code"])
        with self.assertRaises(DomainError):
            self.chl.pickup(m2["code"])

    def test_concurrent_grants_only_one_medal(self):
        """两个馆员并发点发章：恰好一枚 reserved、一名 waitlisted。"""
        e1 = self._qualified_enrollment(1)
        e2 = self._qualified_enrollment(2)
        results = {}
        barrier = threading.Barrier(2)

        def grant(enr, lib, key):
            conn, chl = self.svc()
            barrier.wait()
            try:
                results[key] = chl.approve_and_grant(enr, lib)
            except Exception as ex:  # noqa: BLE001
                results[key] = ex
            finally:
                conn.close()

        t1 = threading.Thread(target=grant, args=(e1, self.lib, "a"))
        t2 = threading.Thread(target=grant, args=(e2, self.lib2, "b"))
        t1.start(); t2.start(); t1.join(); t2.join()

        states = sorted(
            (v["state"] if isinstance(v, dict) else f"ERR:{v}") for v in results.values()
        )
        self.assertEqual(states, ["reserved", "waitlisted"], results)
        stock = self.conn.execute("SELECT medal_stock FROM activities WHERE id=?",
                                  (self.aid,)).fetchone()["medal_stock"]
        self.assertEqual(stock, 0)
        medals = self.conn.execute("SELECT state, COUNT(*) c FROM medals GROUP BY state").fetchall()
        counts = {r["state"]: r["c"] for r in medals}
        self.assertEqual(counts.get("reserved"), 1)
        self.assertEqual(counts.get("waitlisted"), 1)
        vouchers = self.conn.execute("SELECT COUNT(*) c FROM pickup_vouchers").fetchone()["c"]
        self.assertEqual(vouchers, 1)  # 只有预留的才有凭证

    def test_concurrent_same_enrollment_double_grant(self):
        """两个馆员同时给同一个达标报名发章：只能有一个成功。"""
        e1 = self._qualified_enrollment(1)
        results = []
        barrier = threading.Barrier(2)
        lock = threading.Lock()

        def grant(lib):
            barrier.wait()
            conn, chl = self.svc()
            try:
                out = chl.approve_and_grant(e1, lib)
                with lock:
                    results.append(("ok", out["state"]))
            except Exception as ex:  # noqa: BLE001
                with lock:
                    results.append(("err", str(ex)))
            finally:
                conn.close()

        threads = [threading.Thread(target=grant, args=(self.lib,)),
                   threading.Thread(target=grant, args=(self.lib2,))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        oks = [r for r in results if r[0] == "ok" and r[1] == "reserved"]
        self.assertEqual(len(oks), 1, results)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM medals WHERE enrollment_id=?", (e1,)).fetchone()["c"],
            1)


class TestCatalogChange(ChallengeTestBase):
    def test_remove_book_mid_activity(self):
        kid = self.add_child("中途", "2018-02-01", self.p1)
        enr = self.enroll(kid)
        self.synced_loan(kid, self.bids[2])
        rid = self.chl.upsert_record(enr, self.bids[2], self.p1)
        self.chl.submit_record(rid, self.p1)
        self.chl.verify_record(rid, self.lib2)
        # 活动中途撤下书B：已核验的一本不再计入
        self.acts.change_catalog(self.aid, self.bids[2], "remove")
        prog = self.chl.progress(enr)
        self.assertEqual(prog["verified_count"], 0)
        # 撤下后不能新建该书记录
        with self.assertRaises(DomainError):
            self.chl.upsert_record(enr, self.bids[2], self.p1, "再录一次")


if __name__ == "__main__":
    unittest.main(verbosity=2)
