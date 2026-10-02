"""核心边界测试：去重、年龄边界、借阅延迟、审核回避、库存竞争，
另覆盖版本历史、中途换书目、候补按达标时间。"""
import os
import shutil
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta

from reading import services as svc
from reading.adapters import MockBorrowAdapter
from reading.db import connect, init_db

TODAY = date.today()
D = lambda n: (TODAY + timedelta(days=n)).isoformat()
NOW = lambda n=0: datetime.now() + timedelta(days=n)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        self.conn = connect(self.db)
        self.borrow = MockBorrowAdapter(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 构件 ----
    def book(self, title="书"):
        return self.conn.execute(
            "INSERT INTO books(title, cover) VALUES (?, '')", (title,)).lastrowid

    def activity(self, age_min=6, age_max=8, goal=2, stock=1, book_ids=()):
        return svc.create_activity(self.conn, "挑战", age_min, age_max, D(-10), D(10),
                                   borrow_days=14, goal_books=goal, badge_total=stock,
                                   book_ids=book_ids, now=NOW())

    def child(self, birth_date, parent_name="家长"):
        p = self.conn.execute("INSERT INTO parents(name) VALUES (?)",
                              (parent_name,)).lastrowid
        c = self.conn.execute(
            "INSERT INTO children(name, birth_date, parent_id) VALUES ('娃', ?, ?)",
            (birth_date, p)).lastrowid
        return p, c

    def librarian(self, parent_id=None, name="馆员"):
        return self.conn.execute(
            "INSERT INTO librarians(name, parent_id) VALUES (?, ?)",
            (name, parent_id)).lastrowid

    def borrowed(self, child_id, book_id, days_ago=3, visible="now"):
        va = NOW() if visible == "now" else NOW(30)
        self.borrow.stage_event(child_id, book_id, D(-days_ago), visible_after=va)
        svc.sync_borrows(self.conn)

    def enroll_and_read(self, age=7, goal=2, stock=1, n_books=2):
        """报好名、借好书、提交 n_books 条待核验记录。"""
        books = [self.book(f"书{i}") for i in range(max(n_books, goal))]
        aid = self.activity(goal=goal, stock=stock, book_ids=books)
        birth = date(TODAY.year - age, 1, 1).isoformat()
        _, cid = self.child(birth)
        lib = self.librarian()
        eid = svc.enroll(self.conn, aid, cid, now=NOW(-5))
        rids = []
        for b in books[:n_books]:
            self.borrowed(cid, b)
            rids.append(svc.submit_record(self.conn, eid, b, D(-1), "很好看", now=NOW()))
        return aid, cid, lib, eid, books, rids


class TestDedup(Base):
    def test_same_book_counts_once(self):
        aid, cid, lib, eid, books, rids = self.enroll_and_read(goal=2, n_books=1)
        # 同一本书再次提交 → duplicate，不计数
        rid2 = svc.submit_record(self.conn, eid, books[0], D(-1), "又读一遍", now=NOW())
        self.assertEqual(self._status(rid2), "duplicate")
        svc.verify_record(self.conn, rids[0], lib, approve=True)
        self.assertEqual(svc.verified_distinct_count(self.conn, eid), 1)
        # 重复记录不可核验（状态机拒绝）
        with self.assertRaises(svc.ConflictError):
            svc.verify_record(self.conn, rid2, lib, approve=True)

    def test_resubmit_after_rejection(self):
        """被驳回的记录不占用名额，同一本书可重新提交。"""
        aid, cid, lib, eid, books, rids = self.enroll_and_read(goal=2, n_books=1)
        svc.verify_record(self.conn, rids[0], lib, approve=False, reason="内容太空")
        rid2 = svc.submit_record(self.conn, eid, books[0], D(-1), "补充了细节", now=NOW())
        self.assertEqual(self._status(rid2), "pending")
        svc.verify_record(self.conn, rid2, lib, approve=True)
        self.assertEqual(svc.verified_distinct_count(self.conn, eid), 1)

    def _status(self, rid):
        return self.conn.execute(
            "SELECT status s FROM reading_records WHERE id=?", (rid,)).fetchone()["s"]


class TestAgeBoundary(Base):
    def test_boundary_inclusive(self):
        aid = self.activity(age_min=6, age_max=8, book_ids=[self.book()])
        # 恰好 6 岁（今天生日）
        _, c6 = self.child(TODAY.replace(year=TODAY.year - 6).isoformat())
        svc.enroll(self.conn, aid, c6, now=NOW())
        # 恰好 8 岁（今天生日）
        _, c8 = self.child(TODAY.replace(year=TODAY.year - 8).isoformat())
        svc.enroll(self.conn, aid, c8, now=NOW())
        # 明天才满 6 岁 → 仍 5 岁，拒绝
        _, c5 = self.child((TODAY.replace(year=TODAY.year - 6)
                            + timedelta(days=1)).isoformat())
        with self.assertRaises(svc.DomainError):
            svc.enroll(self.conn, aid, c5, now=NOW())
        # 昨天满 9 岁 → 拒绝
        _, c9 = self.child((TODAY.replace(year=TODAY.year - 9)
                            - timedelta(days=1)).isoformat())
        with self.assertRaises(svc.DomainError):
            svc.enroll(self.conn, aid, c9, now=NOW())

    def test_cross_age_group_mid_activity(self):
        """报名后跨年龄组：报名快照仍然有效，可继续提交记录。"""
        books = [self.book()]
        aid = self.activity(age_min=6, age_max=8, goal=1, book_ids=books)
        # 9 岁生日落在报名后第 4 天：报名时 8 岁，活动期间满 9 岁
        birthday = TODAY - timedelta(days=4)
        birth = birthday.replace(year=birthday.year - 9)
        _, cid = self.child(birth.isoformat())
        eid = svc.enroll(self.conn, aid, cid, now=NOW(-8))
        self.assertEqual(svc.age_on(birth, TODAY - timedelta(days=8)), 8)
        self.assertEqual(svc.age_on(birth, TODAY), 9)  # 已跨组
        self.borrowed(cid, books[0], days_ago=1)
        rid = svc.submit_record(self.conn, eid, books[0], D(0), "读完了", now=NOW())
        lib = self.librarian()
        svc.verify_record(self.conn, rid, lib, approve=True)
        self.assertEqual(svc.verified_distinct_count(self.conn, eid), 1)


class TestBorrowDelay(Base):
    def test_late_sync_revalidates(self):
        books = [self.book()]
        aid = self.activity(goal=1, book_ids=books)
        _, cid = self.child(date(TODAY.year - 7, 1, 1).isoformat())
        lib = self.librarian()
        eid = svc.enroll(self.conn, aid, cid, now=NOW(-5))
        # 先提交记录：无借阅证据 → pending_borrow，且不能通过核验
        rid = svc.submit_record(self.conn, eid, books[0], D(-2), "读完了", now=NOW())
        self.assertEqual(self._status(rid), "pending_borrow")
        with self.assertRaises(svc.DomainError):
            svc.verify_record(self.conn, rid, lib, approve=True)
        # 借阅事件还在路上（visible_after 在未来）→ 同步不到
        self.borrow.stage_event(cid, books[0], D(-3), visible_after=NOW(30))
        svc.sync_borrows(self.conn)
        self.assertEqual(self._status(rid), "pending_borrow")
        # 晚同步到达（borrowed_at 早于 read_date）→ 自动回到待核验
        self.borrow.stage_event(cid, books[0], D(-3), visible_after=NOW(-1))
        svc.sync_borrows(self.conn)
        self.assertEqual(self._status(rid), "pending")
        svc.verify_record(self.conn, rid, lib, approve=True)
        self.assertEqual(svc.verified_distinct_count(self.conn, eid), 1)

    def _status(self, rid):
        return self.conn.execute(
            "SELECT status s FROM reading_records WHERE id=?", (rid,)).fetchone()["s"]


class TestRecusal(Base):
    def test_librarian_cannot_review_own_child(self):
        books = [self.book()]
        aid = self.activity(goal=1, book_ids=books)
        pid, cid = self.child(date(TODAY.year - 7, 1, 1).isoformat())
        lib_own = self.librarian(parent_id=pid, name="馆员妈妈")
        lib_other = self.librarian(name="其他馆员")
        eid = svc.enroll(self.conn, aid, cid, now=NOW(-5))
        self.borrowed(cid, books[0])
        rid = svc.submit_record(self.conn, eid, books[0], D(-1), "读完了", now=NOW())
        with self.assertRaises(svc.RecusalError):
            svc.verify_record(self.conn, rid, lib_own, approve=True)
        svc.verify_record(self.conn, rid, lib_other, approve=True)
        # 达标审核同样回避
        with self.assertRaises(svc.RecusalError):
            svc.review_achievement(self.conn, eid, lib_own)
        r = svc.review_achievement(self.conn, eid, lib_other)
        self.assertEqual(r["result"], "voucher")


class TestInventoryRace(Base):
    def _two_goal_reached(self, stock):
        books = [self.book("甲"), self.book("乙")]
        aid = self.activity(goal=2, stock=stock, book_ids=books)
        lib = self.librarian()
        eids = []
        for i in range(2):
            _, cid = self.child(date(TODAY.year - 7, 1, 1).isoformat(),
                                parent_name=f"家长{i}")
            eid = svc.enroll(self.conn, aid, cid, now=NOW(-5))
            for b in books:
                self.borrowed(cid, b)
                rid = svc.submit_record(self.conn, eid, b, D(-1), "好", now=NOW())
                svc.verify_record(self.conn, rid, lib, approve=True)
            eids.append(eid)
        return aid, lib, eids

    def test_concurrent_review_single_badge(self):
        """两个馆员并发审核两个达标者，库存 1 枚：恰一发章、恰一候补。"""
        aid, lib, eids = self._two_goal_reached(stock=1)
        lib2 = self.librarian(name="馆员乙")
        results, errors = [], []

        def review(eid, librarian):
            conn = connect(self.db)
            try:
                results.append(svc.review_achievement(conn, eid, librarian))
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            finally:
                conn.close()

        threads = [threading.Thread(target=review, args=(eids[0], lib)),
                   threading.Thread(target=review, args=(eids[1], lib2))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        kinds = sorted(r["result"] for r in results)
        self.assertEqual(kinds, ["voucher", "waitlisted"])
        inv = self.conn.execute(
            "SELECT * FROM inventory WHERE activity_id=?", (aid,)).fetchone()
        self.assertEqual((inv["issued"], inv["total"]), (1, 1))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM vouchers").fetchone()["c"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM waitlist").fetchone()["c"], 1)

    def test_concurrent_review_same_enrollment(self):
        """两个馆员并发审核同一个达标报名：只成功一次，不产生双章。"""
        aid, lib, eids = self._two_goal_reached(stock=5)
        lib2 = self.librarian(name="馆员乙")
        results, errors = [], []

        def review(librarian):
            conn = connect(self.db)
            try:
                results.append(svc.review_achievement(conn, eids[0], librarian))
            except svc.ConflictError as e:
                errors.append(e)
            finally:
                conn.close()

        threads = [threading.Thread(target=review, args=(lib,)),
                   threading.Thread(target=review, args=(lib2,))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM achievements").fetchone()["c"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM vouchers").fetchone()["c"], 1)


class TestWaitlistOrder(Base):
    def test_restock_allocates_by_achievement_time(self):
        books = [self.book("甲")]
        aid = self.activity(goal=1, stock=0, book_ids=books)
        lib = self.librarian()
        eids = []
        for i, ago in enumerate((2, 1)):  # 先达标者排前
            _, cid = self.child(date(TODAY.year - 7, 1, 1).isoformat(),
                                parent_name=f"家长{i}")
            eid = svc.enroll(self.conn, aid, cid, now=NOW(-5))
            self.borrowed(cid, books[0])
            rid = svc.submit_record(self.conn, eid, books[0], D(-1), "好", now=NOW())
            svc.verify_record(self.conn, rid, lib, approve=True, now=NOW(-ago))
            eids.append(eid)
        for eid in eids:
            r = svc.review_achievement(self.conn, eid, lib)
            self.assertEqual(r["result"], "waitlisted")
        # 补 1 枚 → 先达标者获得
        alloc = svc.restock(self.conn, aid, 1)
        self.assertEqual(len(alloc), 1)
        got = self.conn.execute(
            """SELECT a.enrollment_id eid FROM vouchers v
               JOIN achievements a ON a.id=v.achievement_id""").fetchone()["eid"]
        self.assertEqual(got, eids[0])
        # 再补 1 枚 → 第二位获得，候补清空
        svc.restock(self.conn, aid, 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM waitlist").fetchone()["c"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM vouchers").fetchone()["c"], 2)


class TestVersions(Base):
    def test_edit_keeps_versions_and_resets_verification(self):
        aid, cid, lib, eid, books, rids = self.enroll_and_read(goal=2, n_books=1)
        rid = rids[0]
        svc.edit_record(self.conn, rid, "第二版内容", D(-1), now=NOW())
        svc.edit_record(self.conn, rid, "第三版内容", D(-1), now=NOW())
        versions = self.conn.execute(
            "SELECT COUNT(*) c FROM record_versions WHERE record_id=?",
            (rid,)).fetchone()["c"]
        self.assertEqual(versions, 3)  # 原始版 + 两次修改
        svc.verify_record(self.conn, rid, lib, approve=True)
        # 已核验记录被修改 → 回到待核验，需重新核验
        svc.edit_record(self.conn, rid, "第四版补充", D(-1), now=NOW())
        row = self.conn.execute(
            "SELECT status s, version v FROM reading_records WHERE id=?",
            (rid,)).fetchone()
        self.assertEqual((row["s"], row["v"]), ("pending", 4))
        svc.verify_record(self.conn, rid, lib, approve=True)
        self.assertEqual(svc.verified_distinct_count(self.conn, eid), 1)


class TestBooklistSwitch(Base):
    def test_mid_activity_switch(self):
        a, b = self.book("旧书"), self.book("新书")
        aid = svc.create_activity(self.conn, "挑战", 6, 8, D(-10), D(10),
                                  goal_books=1, badge_total=1, book_ids=[a], now=NOW())
        svc.replace_book_list(self.conn, aid, [b], effective_from=D(0), note="换书")
        _, cid = self.child(date(TODAY.year - 7, 1, 1).isoformat())
        lib = self.librarian()
        eid = svc.enroll(self.conn, aid, cid, now=NOW(-5))
        # 换书前读的旧书：仍然有效
        self.borrowed(cid, a, days_ago=4)
        rid_old = svc.submit_record(self.conn, eid, a, D(-3), "换书前读的", now=NOW())
        svc.verify_record(self.conn, rid_old, lib, approve=True)
        # 换书后读旧书：提交即被拒
        with self.assertRaises(svc.DomainError):
            svc.submit_record(self.conn, eid, a, D(0), "换书后读旧书", now=NOW())
        # 换书前读新书：同样不符（新书当时未生效）
        with self.assertRaises(svc.DomainError):
            svc.submit_record(self.conn, eid, b, D(-3), "太早读新书", now=NOW())
        # 换书后读新书：有效
        self.borrowed(cid, b, days_ago=0)
        rid_new = svc.submit_record(self.conn, eid, b, D(0), "新书好", now=NOW())
        svc.verify_record(self.conn, rid_new, lib, approve=True)
        self.assertEqual(svc.verified_distinct_count(self.conn, eid), 2)


if __name__ == "__main__":
    unittest.main()
