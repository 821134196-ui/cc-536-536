"""核心领域服务：报名、阅读记录、借阅核对、达标审核、发章与候补。

并发约定：所有涉及库存/发章的写操作都在一个 ``BEGIN IMMEDIATE``
事务里完成，并以条件 UPDATE 作为最后一道防线 —— 两个馆员同时
点“发章”只会有一个成功。
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from secrets import token_hex

from .adapters import LoanAdapter, ReminderAdapter, now


class DomainError(Exception):
    """业务规则冲突（消息可直接展示给用户）。"""


# ---------------------------------------------------------------- 年龄

def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def age_on(birth_date: str, on: str | date) -> int:
    """周岁；边界按生日当天计。"""
    b = _parse_date(birth_date) if isinstance(birth_date, str) else birth_date
    d = _parse_date(on) if isinstance(on, str) else on
    return d.year - b.year - ((d.month, d.day) < (b.month, b.day))


# ---------------------------------------------------------------- 活动配置

class ActivityService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create_activity(
        self, name: str, age_min: int, age_max: int,
        starts_on: str, ends_on: str,
        goal_books: int = 3, loan_days: int = 14, medal_stock: int = 0,
    ) -> int:
        if age_min < 0 or age_max < age_min:
            raise DomainError("年龄区间不合法")
        if _parse_date(ends_on) < _parse_date(starts_on):
            raise DomainError("结束日期不能早于开始日期")
        if goal_books < 1:
            raise DomainError("完成书目数至少为 1")
        cur = self.conn.execute(
            """INSERT INTO activities
               (name, age_min, age_max, loan_days, goal_books, starts_on, ends_on, medal_stock)
               VALUES (?,?,?,?,?,?,?,?)""",
            (name, age_min, age_max, loan_days, goal_books, starts_on, ends_on, medal_stock),
        )
        return cur.lastrowid

    def add_book(self, title: str, author: str = "", isbn: str = "", cover: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO books (title, author, isbn, cover) VALUES (?,?,?,?)",
            (title, author, isbn, cover),
        )
        return cur.lastrowid

    def attach_book(self, activity_id: int, book_id: int) -> None:
        """加入可选书目；重复加入直接忽略（幂等）。"""
        self.conn.execute(
            "INSERT OR IGNORE INTO activity_books (activity_id, book_id) VALUES (?,?)",
            (activity_id, book_id),
        )

    def change_catalog(self, activity_id: int, book_id: int, action: str) -> None:
        """活动中途换书目。移除后已有记录保留但不再计入达标。"""
        if action == "add":
            self.attach_book(activity_id, book_id)
        elif action == "remove":
            self.conn.execute(
                "DELETE FROM activity_books WHERE activity_id=? AND book_id=?",
                (activity_id, book_id),
            )
        else:
            raise DomainError("未知目录操作")
        self.conn.execute(
            "INSERT INTO catalog_changes (activity_id, book_id, action) VALUES (?,?,?)",
            (activity_id, book_id, action),
        )

    def list_activities(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM activities ORDER BY id").fetchall()

    def list_books(self, activity_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT b.* FROM books b
               JOIN activity_books ab ON ab.book_id=b.id
               WHERE ab.activity_id=? ORDER BY b.title""",
            (activity_id,),
        ).fetchall()


# ---------------------------------------------------------------- 报名 / 记录

class ChallengeService:
    def __init__(self, conn: sqlite3.Connection, reminders: ReminderAdapter | None = None):
        self.conn = conn
        self.loans = LoanAdapter(conn)
        self.reminders = reminders or ReminderAdapter(conn)

    # ---- 报名
    def enroll(self, activity_id: int, child_id: int, parent_id: int) -> int:
        act = self.conn.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
        child = self.conn.execute("SELECT * FROM children WHERE id=?", (child_id,)).fetchone()
        if act is None or child is None:
            raise DomainError("活动或孩子不存在")
        if child["parent_id"] != parent_id:
            raise DomainError("只能为自己的孩子报名")
        # 年龄边界：以活动开始日周岁为准，闭区间 [age_min, age_max]
        age = age_on(child["birth_date"], act["starts_on"])
        if not (act["age_min"] <= age <= act["age_max"]):
            raise DomainError(
                f"孩子在活动开始日 {age} 岁，不在 {act['age_min']}-{act['age_max']} 岁组"
            )
        exists = self.conn.execute(
            "SELECT id FROM enrollments WHERE activity_id=? AND child_id=?",
            (activity_id, child_id),
        ).fetchone()
        if exists:
            return exists["id"]  # 重复报名幂等
        cur = self.conn.execute(
            "INSERT INTO enrollments (activity_id, child_id, parent_id) VALUES (?,?,?)",
            (activity_id, child_id, parent_id),
        )
        return cur.lastrowid

    # ---- 阅读记录（创建/补录/修改，全部留版本）
    def _get_enrollment(self, enrollment_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM enrollments WHERE id=?", (enrollment_id,)).fetchone()
        if row is None:
            raise DomainError("报名不存在")
        return row

    def _book_in_catalog(self, activity_id: int, book_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM activity_books WHERE activity_id=? AND book_id=?",
            (activity_id, book_id),
        ).fetchone() is not None

    def upsert_record(
        self,
        enrollment_id: int,
        book_id: int,
        user_id: int,
        note: str = "",
        read_on: str | None = None,
    ) -> int:
        """创建或修改记录。

        - 同一报名同一本书只允许一条（重复计数在入口拦掉）；
        - 已 verified 的记录锁定，不能改；
        - 每次改动写一行版本历史；改过已提交/驳回的记录需重新提交。
        """
        enr = self._get_enrollment(enrollment_id)
        child = self.conn.execute("SELECT * FROM children WHERE id=?", (enr["child_id"],)).fetchone()
        if child["parent_id"] != user_id:
            raise DomainError("只能修改自己孩子的记录")
        if not self._book_in_catalog(enr["activity_id"], book_id):
            raise DomainError("该书不在当前可选书目中（活动中途可能已调整）")
        # 去重：同 ISBN 的不同书目行视为同一本书，同一报名只计一次
        dup = self.conn.execute(
            """SELECT rr.id FROM reading_records rr
               JOIN books b1 ON b1.id=rr.book_id
               JOIN books b2 ON b2.id=?
               WHERE rr.enrollment_id=? AND rr.book_id<>?
                 AND b1.isbn<>'' AND b1.isbn=b2.isbn""",
            (book_id, enrollment_id, book_id),
        ).fetchone()
        if dup is not None:
            raise DomainError("同一本书（ISBN 相同）重复提交只计一次，已存在对应记录")
        read_on = read_on or now().date().isoformat()
        existing = self.conn.execute(
            "SELECT * FROM reading_records WHERE enrollment_id=? AND book_id=?",
            (enrollment_id, book_id),
        ).fetchone()

        if existing is not None:
            if existing["status"] == "verified":
                raise DomainError("已核验通过的记录不能修改")
            new_version = existing["current_version"] + 1
            self.conn.execute(
                """UPDATE reading_records
                   SET note=:note, read_on=:read_on, status='draft',
                       current_version=:v, updated_at=datetime('now','localtime')
                   WHERE id=:id""",
                {"id": existing["id"], "note": note, "read_on": read_on, "v": new_version},
            )
            self.conn.execute(
                """INSERT INTO reading_record_versions
                   (record_id, version, book_id, note, read_on, changed_by, change_kind)
                   VALUES (?,?,?,?,?,?, 'update')""",
                (existing["id"], new_version, book_id, note, read_on, user_id),
            )
            return existing["id"]

        cur = self.conn.execute(
            """INSERT INTO reading_records
               (enrollment_id, child_id, book_id, status, current_version, read_on)
               VALUES (?,?,?, 'draft', 1, ?)""",
            (enrollment_id, enr["child_id"], book_id, read_on),
        )
        rid = cur.lastrowid
        self.conn.execute(
            """INSERT INTO reading_record_versions
               (record_id, version, book_id, note, read_on, changed_by, change_kind)
               VALUES (?,?,?,?,?,?, 'create')""",
            (rid, 1, book_id, note, read_on, user_id),
        )
        return rid

    def submit_record(self, record_id: int, user_id: int) -> None:
        rec = self.conn.execute("SELECT * FROM reading_records WHERE id=?", (record_id,)).fetchone()
        if rec is None:
            raise DomainError("记录不存在")
        enr = self._get_enrollment(rec["enrollment_id"])
        child = self.conn.execute("SELECT * FROM children WHERE id=?", (enr["child_id"],)).fetchone()
        if child["parent_id"] != user_id:
            raise DomainError("只能提交自己孩子的记录")
        if rec["status"] == "verified":
            raise DomainError("记录已核验通过")
        self.conn.execute(
            "UPDATE reading_records SET status='submitted', updated_at=datetime('now','localtime') WHERE id=?",
            (record_id,),
        )
        self._version_event(rec, user_id, "submit")

    def record_versions(self, record_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reading_record_versions WHERE record_id=? ORDER BY version",
            (record_id,),
        ).fetchall()

    # ---- 借阅核对
    def sync_loans(self) -> int:
        return self.loans.poll()

    def verify_record(self, record_id: int, librarian_id: int) -> None:
        """馆员逐条核对：已提交 + 借阅已同步 → verified。"""
        rec = self.conn.execute("SELECT * FROM reading_records WHERE id=?", (record_id,)).fetchone()
        if rec is None:
            raise DomainError("记录不存在")
        enr = self._get_enrollment(rec["enrollment_id"])
        self._assert_not_own_child(librarian_id, enr["child_id"])
        if rec["status"] != "submitted":
            raise DomainError("只有已提交的记录可以核验")
        if not self._book_in_catalog(enr["activity_id"], rec["book_id"]):
            raise DomainError("该书已从本期书目中移除，不能核验")
        if not self.loans.is_borrowed(enr["child_id"], rec["book_id"], enr["activity_id"]):
            raise DomainError("借阅记录尚未同步或查无借阅，暂不能核验")
        self.conn.execute(
            "UPDATE reading_records SET status='verified', updated_at=datetime('now','localtime') WHERE id=?",
            (record_id,),
        )
        self._version_event(rec, librarian_id, "review")

    def reject_record(self, record_id: int, librarian_id: int, comment: str) -> None:
        rec = self.conn.execute("SELECT * FROM reading_records WHERE id=?", (record_id,)).fetchone()
        if rec is None:
            raise DomainError("记录不存在")
        enr = self._get_enrollment(rec["enrollment_id"])
        self._assert_not_own_child(librarian_id, enr["child_id"])
        if rec["status"] != "submitted":
            raise DomainError("只有已提交的记录可以处理")
        self.conn.execute(
            "UPDATE reading_records SET status='rejected', updated_at=datetime('now','localtime') WHERE id=?",
            (record_id,),
        )
        self._version_event(rec, librarian_id, "review", comment)
        self.reminders.send(
            "review", "阅读记录需补充",
            f"记录 #{record_id} 未通过核验：{comment}",
            user_id=self._parent_of(enr["child_id"]),
        )

    def _version_event(self, rec: sqlite3.Row, user_id: int, kind: str, note: str = "") -> None:
        v = self.conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS v FROM reading_record_versions WHERE record_id=?",
            (rec["id"],),
        ).fetchone()["v"]
        prev = self.conn.execute(
            "SELECT note, read_on FROM reading_record_versions WHERE record_id=? ORDER BY version DESC LIMIT 1",
            (rec["id"],),
        ).fetchone()
        self.conn.execute(
            """INSERT INTO reading_record_versions
               (record_id, version, book_id, note, read_on, changed_by, change_kind)
               VALUES (?,?,?,?,?,?,?)""",
            (rec["id"], v, rec["book_id"], note or (prev["note"] if prev else ""),
             rec["read_on"], user_id, kind),
        )
        self.conn.execute(
            "UPDATE reading_records SET current_version=? WHERE id=?", (v, rec["id"])
        )

    # ---- 回避
    def _assert_not_own_child(self, librarian_id: int, child_id: int) -> None:
        lib = self.conn.execute("SELECT * FROM users WHERE id=?", (librarian_id,)).fetchone()
        own = {int(x) for x in (lib["own_children"] or "").split(",") if x.strip()}
        if int(child_id) in own:
            raise DomainError("审核回避：不能审核自己孩子的记录")

    def _parent_of(self, child_id: int) -> int:
        return self.conn.execute("SELECT parent_id FROM children WHERE id=?", (child_id,)).fetchone()["parent_id"]

    # ---- 达标进度（去重：只统计仍在书目内的 verified 不同 book_id）
    def progress(self, enrollment_id: int) -> dict:
        enr = self._get_enrollment(enrollment_id)
        act = self.conn.execute("SELECT * FROM activities WHERE id=?", (enr["activity_id"],)).fetchone()
        rows = self.conn.execute(
            """SELECT rr.*, b.title,
                      ab.book_id IS NOT NULL AS in_catalog,
               EXISTS (SELECT 1 FROM loan_events le
                       WHERE le.child_id=rr.child_id AND le.book_id=rr.book_id
                         AND le.activity_id=(SELECT activity_id FROM enrollments WHERE id=rr.enrollment_id)
                         AND le.status='synced') AS loan_synced
               FROM reading_records rr JOIN books b ON b.id=rr.book_id
               LEFT JOIN activity_books ab ON ab.activity_id=? AND ab.book_id=rr.book_id
               WHERE rr.enrollment_id=?""",
            (enr["activity_id"], enrollment_id),
        ).fetchall()
        verified = [r for r in rows if r["status"] == "verified" and r["in_catalog"]]
        # 再按 ISBN 兜底去重（同一本书的重复书目行只算一本）
        seen_isbn: set[str] = set()
        deduped: list = []
        for r in verified:
            book = self.conn.execute("SELECT isbn FROM books WHERE id=?", (r["book_id"],)).fetchone()
            key = book["isbn"] if book and book["isbn"] else f"id:{r['book_id']}"
            if key in seen_isbn:
                continue
            seen_isbn.add(key)
            deduped.append(r)
        verified = deduped
        return {
            "activity": act,
            "enrollment": enr,
            "records": rows,
            "goal": act["goal_books"],
            "verified_count": len(verified),
            "qualified": len(verified) >= act["goal_books"],
        }

    # ---- 馆员工作台：清楚区分“已提交”与“已借阅”
    def librarian_queue(self, activity_id: int) -> list[dict]:
        enrs = self.conn.execute(
            "SELECT * FROM enrollments WHERE activity_id=? ORDER BY id", (activity_id,)
        ).fetchall()
        out = []
        for enr in enrs:
            child = self.conn.execute("SELECT * FROM children WHERE id=?", (enr["child_id"],)).fetchone()
            rec_rows = self.conn.execute(
                """SELECT rr.*, b.title,
                          MAX(COALESCE(le.status,'')='synced') AS loan_synced,
                          MIN(le.sync_after) AS sync_after
                   FROM reading_records rr JOIN books b ON b.id=rr.book_id
                   LEFT JOIN loan_events le
                     ON le.child_id=rr.child_id AND le.book_id=rr.book_id
                        AND le.activity_id=?
                   WHERE rr.enrollment_id=?
                   GROUP BY rr.id""",
                (activity_id, enr["id"]),
            ).fetchall()
            submitted = [r for r in rec_rows if r["status"] == "submitted"]
            # 已借阅（已同步）但还没有提交记录的书
            borrowed_ids = {r["id"] for r in self.conn.execute(
                """SELECT b.id FROM loan_events le JOIN books b ON b.id=le.book_id
                   JOIN activity_books ab ON ab.activity_id=? AND ab.book_id=b.id
                   WHERE le.child_id=? AND le.activity_id=? AND le.status='synced'""",
                (activity_id, enr["child_id"], activity_id),
            ).fetchall()}
            recorded_books = {r["book_id"] for r in rec_rows}
            out.append({
                "enrollment": enr,
                "child": child,
                "records": rec_rows,
                "submitted": submitted,
                "borrowed_only": sorted(borrowed_ids - recorded_books),
            })
        return out

    # ---- 达标审核 + 发章（一个事务，原子扣库存）
    def approve_and_grant(self, enrollment_id: int, librarian_id: int) -> dict:
        """达标审核：核对回避与完成条件，达标即锁定一枚纪念章。

        库存够 -> reserved；不够 -> waitlisted 并入候补（按达标时间）。
        返回 {state, voucher?}。
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            enr = self._get_enrollment(enrollment_id)
            self._assert_not_own_child(librarian_id, enr["child_id"])
            prog = self.progress(enrollment_id)
            if not prog["qualified"]:
                raise DomainError(
                    f"尚未达标：已核验 {prog['verified_count']}/{prog['goal']} 本"
                )
            act = prog["activity"]
            existing = self.conn.execute(
                "SELECT * FROM medals WHERE enrollment_id=?", (enrollment_id,)
            ).fetchone()
            if existing is not None and existing["state"] in ("reserved", "issued"):
                raise DomainError(f"该报名已有纪念章（{existing['state']}），不能重复发章")

            # 条件 UPDATE：库存 > 0 才扣减，并发下只有一个事务能扣到
            cur = self.conn.execute(
                "UPDATE activities SET medal_stock = medal_stock - 1 WHERE id=? AND medal_stock > 0",
                (act["id"],),
            )
            if cur.rowcount == 0:  # 库存不足 → 候补
                medal = existing
                if medal is None:
                    cur = self.conn.execute(
                        """INSERT INTO medals (activity_id, enrollment_id, state)
                           VALUES (?,?,'waitlisted')""",
                        (act["id"], enrollment_id),
                    )
                    medal_id = cur.lastrowid
                else:
                    medal_id = medal["id"]
                    self.conn.execute("UPDATE medals SET state='waitlisted' WHERE id=?", (medal_id,))
                pos = self.conn.execute(
                    "SELECT COALESCE(MAX(position),0)+1 AS p FROM waitlist WHERE medal_id IN "
                    "(SELECT id FROM medals WHERE activity_id=?)",
                    (act["id"],),
                ).fetchone()["p"]
                self.conn.execute(
                    "INSERT OR IGNORE INTO waitlist (medal_id, position) VALUES (?,?)",
                    (medal_id, pos),
                )
                self.conn.execute(
                    "INSERT INTO reviews (enrollment_id, librarian_id, result, comment) "
                    "VALUES (?,?,'approved','达标，库存不足进入候补')",
                    (enrollment_id, librarian_id),
                )
                self.reminders.send(
                    "waitlist", "已进入纪念章候补",
                    f"活动《{act['name']}》已达标，候补位置 #{pos}，补货后按达标时间先后通知。",
                    user_id=self._parent_of(enr["child_id"]),
                )
                self.conn.execute("COMMIT")
                return {"state": "waitlisted", "position": pos}

            # 库存充足：预留并发凭证（发章完成）
            if existing is not None:  # 之前候补、本次补货流程不该走到这；保险处理
                medal_id = existing["id"]
                self.conn.execute("UPDATE medals SET state='reserved' WHERE id=?", (medal_id,))
                self.conn.execute("DELETE FROM waitlist WHERE medal_id=?", (medal_id,))
            else:
                cur = self.conn.execute(
                    """INSERT INTO medals (activity_id, enrollment_id, state)
                       VALUES (?,?,'reserved')""",
                    (act["id"], enrollment_id),
                )
                medal_id = cur.lastrowid
            code = f"LIB-{act['id']:02d}-{token_hex(4).upper()}"
            self.conn.execute(
                "INSERT INTO pickup_vouchers (medal_id, code) VALUES (?,?)",
                (medal_id, code),
            )
            self.conn.execute(
                "INSERT INTO reviews (enrollment_id, librarian_id, result) VALUES (?,?,'approved')",
                (enrollment_id, librarian_id),
            )
            self.reminders.send(
                "voucher", "纪念章预约领取凭证已生成",
                f"活动《{act['name']}》达标完成，凭证号 {code}，请到馆领取实体纪念章。",
                user_id=self._parent_of(enr["child_id"]),
            )
            self.conn.execute("COMMIT")
            return {"state": "reserved", "voucher": code, "medal_id": medal_id}
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def restock(self, activity_id: int, amount: int) -> list[int]:
        """补货：入库后按候补顺序（达标时间先后）提升，返回发出的凭证 medal_id 列表。"""
        if amount <= 0:
            raise DomainError("补货数量必须为正")
        promoted: list[int] = []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "UPDATE activities SET medal_stock = medal_stock + ? WHERE id=?",
                (amount, activity_id),
            )
            queue = self.conn.execute(
                """SELECT m.id AS medal_id, m.enrollment_id
                   FROM waitlist w JOIN medals m ON m.id=w.medal_id
                   WHERE m.activity_id=? AND m.state='waitlisted'
                   ORDER BY m.qualified_at, w.position""",
                (activity_id,),
            ).fetchall()
            for row in queue:
                cur = self.conn.execute(
                    "UPDATE activities SET medal_stock = medal_stock - 1 WHERE id=? AND medal_stock > 0",
                    (activity_id,),
                )
                if cur.rowcount == 0:
                    break
                self.conn.execute("UPDATE medals SET state='reserved' WHERE id=?", (row["medal_id"],))
                self.conn.execute("DELETE FROM waitlist WHERE medal_id=?", (row["medal_id"],))
                enr = self.conn.execute(
                    "SELECT * FROM enrollments WHERE id=?", (row["enrollment_id"],)
                ).fetchone()
                act = self.conn.execute("SELECT name FROM activities WHERE id=?", (activity_id,)).fetchone()
                code = f"LIB-{activity_id:02d}-{token_hex(4).upper()}"
                self.conn.execute(
                    "INSERT INTO pickup_vouchers (medal_id, code) VALUES (?,?)",
                    (row["medal_id"], code),
                )
                self.reminders.send(
                    "voucher", "候补转正：纪念章预约凭证已生成",
                    f"活动《{act['name']}》补货到位，凭证号 {code}，请到馆领取。",
                    user_id=self._parent_of(enr["child_id"]),
                )
                promoted.append(row["medal_id"])
            self.conn.execute("COMMIT")
            return promoted
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def pickup(self, voucher_code: str) -> dict:
        """核销凭证领取实体章（幂等：重复核销提示已领取）。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            v = self.conn.execute(
                "SELECT * FROM pickup_vouchers WHERE code=?", (voucher_code,)
            ).fetchone()
            if v is None:
                raise DomainError("凭证不存在")
            if v["picked_up_at"]:
                raise DomainError(f"凭证已于 {v['picked_up_at']} 使用")
            self.conn.execute(
                "UPDATE pickup_vouchers SET picked_up_at=datetime('now','localtime') WHERE id=?",
                (v["id"],),
            )
            self.conn.execute(
                "UPDATE medals SET state='issued', issued_at=datetime('now','localtime') WHERE id=?",
                (v["medal_id"],),
            )
            self.conn.execute("COMMIT")
            return {"ok": True}
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def medal_status(self, enrollment_id: int) -> sqlite3.Row | None:
        row = self.conn.execute(
            """SELECT m.*, v.code, v.picked_up_at,
                      (SELECT position FROM waitlist w WHERE w.medal_id=m.id) AS wait_pos
               FROM medals m LEFT JOIN pickup_vouchers v ON v.medal_id=m.id
               WHERE m.enrollment_id=?""",
            (enrollment_id,),
        ).fetchone()
        return row
