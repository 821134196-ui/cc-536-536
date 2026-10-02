"""领域服务：活动配置、报名、阅读记录、借阅核对、达标审核、库存与候补。

所有写操作在 BEGIN IMMEDIATE 事务内完成，并用「条件 UPDATE 的 rowcount」
与唯一约束兜底并发：两个馆员同时核验不会重复发章。
"""
from __future__ import annotations

import secrets
import sqlite3
from datetime import date, datetime

from .db import tx


class DomainError(Exception):
    """业务规则拒绝（表单可展示给用户）。"""


class NotFound(DomainError):
    pass


class RecusalError(DomainError):
    """馆员试图审核自己孩子的记录。"""


class ConflictError(DomainError):
    """并发冲突：记录/报名已被其他人处理。"""


# ---------------------------------------------------------------- 工具

def _d(s) -> date:
    if isinstance(s, date):
        return s
    return date.fromisoformat(str(s)[:10])


def age_on(birth: date, on: date) -> int:
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


def _now_str(now: datetime | None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _today(now: datetime | None) -> date:
    return (now or datetime.now()).date() if isinstance(now, datetime) else (now or date.today())


def _one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def _require(row, what="记录"):
    if row is None:
        raise NotFound(f"{what}不存在")
    return row


# ---------------------------------------------------------------- 活动配置

def create_activity(conn, name, age_min, age_max, start_date, end_date,
                    borrow_days=21, goal_books=3, badge_name="纪念章",
                    badge_total=0, book_ids=(), list_effective_from=None, now=None):
    if age_min > age_max:
        raise DomainError("年龄段下限不能大于上限")
    if _d(start_date) > _d(end_date):
        raise DomainError("活动开始日期不能晚于结束日期")
    if goal_books < 1:
        raise DomainError("达标书目数至少为 1")
    today = _today(now).isoformat()
    with tx(conn):
        cur = conn.execute(
            """INSERT INTO activities(name, age_min, age_max, start_date, end_date,
                                      borrow_days, goal_books, badge_name)
               VALUES (?,?,?,?,?,?,?,?)""",
            (name, age_min, age_max, str(start_date), str(end_date),
             borrow_days, goal_books, badge_name))
        aid = cur.lastrowid
        conn.execute("INSERT INTO inventory(activity_id, total, issued) VALUES (?,?,0)",
                     (aid, int(badge_total)))
        if book_ids:
            _replace_book_list_tx(conn, aid, book_ids,
                                  list_effective_from or str(start_date),
                                  note="初始书目", today=today)
        return aid


def _replace_book_list_tx(conn, activity_id, book_ids, effective_from, note, today):
    row = _one(conn, "SELECT MAX(version) v FROM book_lists WHERE activity_id=?",
               (activity_id,))
    version = (row["v"] or 0) + 1
    cur = conn.execute(
        "INSERT INTO book_lists(activity_id, version, effective_from, note) VALUES (?,?,?,?)",
        (activity_id, version, str(effective_from), note))
    list_id = cur.lastrowid
    for bid in dict.fromkeys(book_ids):  # 书目本身去重
        _require(_one(conn, "SELECT id FROM books WHERE id=?", (bid,)), "图书")
        conn.execute("INSERT INTO book_list_items(list_id, book_id) VALUES (?,?)",
                     (list_id, bid))
    return version


def replace_book_list(conn, activity_id, book_ids, effective_from, note="", now=None):
    """活动中途换书目：新增一个版本，旧记录仍按 read_date 当时的版本判定。"""
    _require(_one(conn, "SELECT id FROM activities WHERE id=?", (activity_id,)), "活动")
    if not book_ids:
        raise DomainError("书目不能为空")
    with tx(conn):
        return _replace_book_list_tx(conn, activity_id, book_ids, effective_from,
                                     note or "书目调整", today=_today(now).isoformat())


def list_id_at(conn, activity_id, on_date):
    """on_date 当天生效的书目版本。"""
    row = _one(conn,
               """SELECT id FROM book_lists
                  WHERE activity_id=? AND date(effective_from) <= date(?)
                  ORDER BY version DESC LIMIT 1""",
               (activity_id, str(on_date)))
    return row["id"] if row else None


def book_on_list(conn, activity_id, book_id, on_date) -> bool:
    lid = list_id_at(conn, activity_id, on_date)
    if lid is None:
        return False
    return _one(conn,
                "SELECT 1 x FROM book_list_items WHERE list_id=? AND book_id=?",
                (lid, book_id)) is not None


def current_book_list(conn, activity_id, today=None):
    today = str(today or date.today())
    lid = list_id_at(conn, activity_id, today)
    if lid is None:
        return []
    return conn.execute(
        """SELECT b.* FROM book_list_items li JOIN books b ON b.id=li.book_id
           WHERE li.list_id=? ORDER BY b.title""", (lid,)).fetchall()


# ---------------------------------------------------------------- 报名

def enroll(conn, activity_id, child_id, now=None):
    """年龄按报名当天计算并快照；孩子之后跨年龄组不影响本次报名。"""
    today = _today(now)
    with tx(conn):
        act = _require(_one(conn, "SELECT * FROM activities WHERE id=?", (activity_id,)), "活动")
        if act["status"] != "open":
            raise DomainError("活动已关闭报名")
        if not (_d(act["start_date"]) <= today <= _d(act["end_date"])):
            raise DomainError("不在活动报名时间内")
        child = _require(_one(conn, "SELECT * FROM children WHERE id=?", (child_id,)), "孩子")
        age = age_on(_d(child["birth_date"]), today)
        if not (act["age_min"] <= age <= act["age_max"]):
            raise DomainError(
                f"孩子当前 {age} 岁，不在活动年龄段 {act['age_min']}-{act['age_max']} 内")
        dup = _one(conn,
                   "SELECT id FROM enrollments WHERE activity_id=? AND child_id=?",
                   (activity_id, child_id))
        if dup:
            raise DomainError("该孩子已报名此活动")
        cur = conn.execute(
            """INSERT INTO enrollments(activity_id, child_id, age_at_enroll, enrolled_at)
               VALUES (?,?,?,?)""",
            (activity_id, child_id, age, today.isoformat()))
        return cur.lastrowid


# ---------------------------------------------------------------- 借阅核对

def find_borrow_evidence(conn, child_id, book_id, read_date, borrow_days):
    """read_date 是否落在某次借阅的借期内（borrowed_at .. 归还日或借出+期限）。"""
    return _one(conn,
                """SELECT id FROM borrow_events
                   WHERE child_id=? AND book_id=?
                     AND date(borrowed_at) <= date(?)
                     AND date(?) <= date(COALESCE(returned_at,
                                         date(borrowed_at, '+' || ? || ' days')))
                   LIMIT 1""",
                (child_id, book_id, str(read_date), str(read_date), borrow_days))


def sync_borrows(conn, now=None):
    """模拟适配器拉取：把到期的发件箱事件搬进 borrow_events，并重新评估
    因「借阅未同步」而挂起的记录（晚同步处理）。"""
    now_s = _now_str(now)
    moved = []
    with tx(conn):
        rows = conn.execute(
            "SELECT * FROM borrow_outbox WHERE visible_after <= ?", (now_s,)).fetchall()
        for r in rows:
            conn.execute(
                """INSERT INTO borrow_events(child_id, book_id, borrowed_at, returned_at, synced_at)
                   VALUES (?,?,?,?,?)""",
                (r["child_id"], r["book_id"], r["borrowed_at"], r["returned_at"], now_s))
            conn.execute("DELETE FROM borrow_outbox WHERE id=?", (r["id"],))
            moved.append(dict(r))
        # 重新评估 pending_borrow 记录
        pend = conn.execute(
            """SELECT rr.id, rr.book_id, rr.read_date, e.child_id, a.borrow_days
               FROM reading_records rr
               JOIN enrollments e ON e.id = rr.enrollment_id
               JOIN activities a ON a.id = e.activity_id
               WHERE rr.status='pending_borrow'""").fetchall()
        for p in pend:
            if find_borrow_evidence(conn, p["child_id"], p["book_id"],
                                    p["read_date"], p["borrow_days"]):
                conn.execute(
                    "UPDATE reading_records SET status='pending', updated_at=? WHERE id=?",
                    (now_s, p["id"]))
    return moved


# ---------------------------------------------------------------- 阅读记录

ACTIVE_STATUSES = ("pending", "pending_borrow", "verified")


def submit_record(conn, enrollment_id, book_id, read_date, content, now=None):
    """家长提交阅读记录。同一本书已有有效记录时，新记录标记 duplicate 不计数。"""
    now_s = _now_str(now)
    today = _today(now)
    read_date = str(read_date)
    content = (content or "").strip()
    if not content:
        raise DomainError("阅读记录内容不能为空")
    with tx(conn):
        enr = _require(_one(conn,
                            """SELECT e.*, a.borrow_days, a.start_date, a.end_date,
                                      a.id aid, a.status a_status
                               FROM enrollments e JOIN activities a ON a.id=e.activity_id
                               WHERE e.id=?""", (enrollment_id,)), "报名")
        if enr["a_status"] == "closed":
            raise DomainError("活动已结束")
        rd = _d(read_date)
        if rd > today:
            raise DomainError("阅读日期不能在未来")
        if rd < _d(enr["enrolled_at"]):
            raise DomainError("阅读日期早于报名日期")
        if not (_d(enr["start_date"]) <= rd <= _d(enr["end_date"])):
            raise DomainError("阅读日期不在活动周期内")
        if not book_on_list(conn, enr["aid"], book_id, read_date):
            raise DomainError("该书不在阅读日期当天生效的活动书目内")

        dup = _one(conn,
                   f"""SELECT id FROM reading_records
                       WHERE enrollment_id=? AND book_id=?
                         AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})""",
                   (enrollment_id, book_id, *ACTIVE_STATUSES))
        if dup:
            status = "duplicate"
        else:
            has_borrow = find_borrow_evidence(
                conn, enr["child_id"], book_id, read_date, enr["borrow_days"])
            status = "pending" if has_borrow else "pending_borrow"
        is_backfill = 1 if rd < today else 0
        cur = conn.execute(
            """INSERT INTO reading_records(enrollment_id, book_id, read_date, content,
                                           is_backfill, status, submitted_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (enrollment_id, book_id, read_date, content, is_backfill, status, now_s, now_s))
        rid = cur.lastrowid
        conn.execute(
            """INSERT INTO record_versions(record_id, version, content, read_date,
                                           changed_by, changed_at)
               VALUES (?,?,?,?, 'parent', ?)""",
            (rid, 1, content, read_date, now_s))
        return rid


def edit_record(conn, record_id, content, read_date, changed_by="parent", now=None):
    """修改记录：留存新版本；已核验/被驳回的记录修改后回到待核验，并按新
    阅读日期重新评估借阅证据与书目有效性。"""
    now_s = _now_str(now)
    today = _today(now)
    read_date = str(read_date)
    content = (content or "").strip()
    if not content:
        raise DomainError("阅读记录内容不能为空")
    with tx(conn):
        r = _require(_one(conn,
                          """SELECT rr.*, e.child_id, e.activity_id, a.borrow_days,
                                    a.start_date, a.end_date
                             FROM reading_records rr
                             JOIN enrollments e ON e.id=rr.enrollment_id
                             JOIN activities a ON a.id=e.activity_id
                             WHERE rr.id=?""", (record_id,)), "阅读记录")
        if r["status"] == "duplicate":
            raise DomainError("重复记录不可编辑，请编辑原始记录")
        rd = _d(read_date)
        if rd > today:
            raise DomainError("阅读日期不能在未来")
        if not (_d(r["start_date"]) <= rd <= _d(r["end_date"])):
            raise DomainError("阅读日期不在活动周期内")
        if not book_on_list(conn, r["activity_id"], r["book_id"], read_date):
            raise DomainError("该书不在阅读日期当天生效的活动书目内")
        new_version = r["version"] + 1
        has_borrow = find_borrow_evidence(conn, r["child_id"], r["book_id"],
                                          read_date, r["borrow_days"])
        new_status = "pending" if has_borrow else "pending_borrow"
        conn.execute(
            """INSERT INTO record_versions(record_id, version, content, read_date,
                                           changed_by, changed_at)
               VALUES (?,?,?,?,?,?)""",
            (record_id, new_version, content, read_date, changed_by, now_s))
        # 旧核验结论针对的是旧版本，作废（历史内容仍留在 record_versions）
        conn.execute("DELETE FROM verifications WHERE record_id=?", (record_id,))
        conn.execute(
            """UPDATE reading_records
               SET content=?, read_date=?, version=?, status=?, reject_reason='', updated_at=?
               WHERE id=?""",
            (content, read_date, new_version, new_status, now_s, record_id))
        return new_version


def verified_distinct_count(conn, enrollment_id) -> int:
    row = _one(conn,
               """SELECT COUNT(DISTINCT book_id) c FROM reading_records
                  WHERE enrollment_id=? AND status='verified'""",
               (enrollment_id,))
    return row["c"]


# ---------------------------------------------------------------- 馆员核验

def _check_recusal(conn, librarian_id, child_id):
    lib = _require(_one(conn, "SELECT * FROM librarians WHERE id=?", (librarian_id,)),
                   "馆员")
    child = _require(_one(conn, "SELECT * FROM children WHERE id=?", (child_id,)), "孩子")
    if lib["parent_id"] is not None and lib["parent_id"] == child["parent_id"]:
        raise RecusalError("馆员不能审核自己孩子的记录，已回避")
    return lib, child


def verify_record(conn, record_id, librarian_id, approve, reason="", now=None):
    """核验单条记录。通过时重查借阅证据与书目有效性，并推进达标状态。"""
    now_s = _now_str(now)
    with tx(conn):
        r = _require(_one(conn,
                          """SELECT rr.*, e.child_id, e.activity_id, a.borrow_days
                             FROM reading_records rr
                             JOIN enrollments e ON e.id=rr.enrollment_id
                             JOIN activities a ON a.id=e.activity_id
                             WHERE rr.id=?""", (record_id,)), "阅读记录")
        _check_recusal(conn, librarian_id, r["child_id"])
        if r["status"] not in ("pending", "pending_borrow"):
            raise ConflictError(f"记录当前状态为 {r['status']}，可能已被其他馆员处理")
        if approve:
            if r["status"] == "pending_borrow":
                raise DomainError("借阅记录尚未同步，暂不能通过")
            if not find_borrow_evidence(conn, r["child_id"], r["book_id"],
                                        r["read_date"], r["borrow_days"]):
                raise DomainError("未找到覆盖阅读日期的借阅记录")
            if not book_on_list(conn, r["activity_id"], r["book_id"], r["read_date"]):
                raise DomainError("该书不在阅读日期当天生效的书目内")
        # 条件更新防并发：只有仍处理中的记录能被翻转
        new_status = "verified" if approve else "rejected"
        cur = conn.execute(
            """UPDATE reading_records
               SET status=?, reject_reason=?, updated_at=?
               WHERE id=? AND status IN ('pending','pending_borrow')""",
            (new_status, "" if approve else reason, now_s, record_id))
        if cur.rowcount != 1:
            raise ConflictError("该记录已被其他馆员处理")
        conn.execute(
            "INSERT INTO verifications(record_id, librarian_id, decision, reason, decided_at)"
            " VALUES (?,?,?,?,?)",
            (record_id, librarian_id, "approve" if approve else "reject", reason, now_s))
        goal_info = None
        if approve:
            goal_info = _maybe_mark_goal_tx(conn, r["enrollment_id"], now_s)
        return new_status, goal_info


def _maybe_mark_goal_tx(conn, enrollment_id, now_s):
    enr = _one(conn,
               """SELECT e.id, e.status, a.goal_books, a.id aid
                  FROM enrollments e JOIN activities a ON a.id=e.activity_id
                  WHERE e.id=?""", (enrollment_id,))
    if enr["status"] != "active":
        return None
    count = verified_distinct_count(conn, enrollment_id)
    if count >= enr["goal_books"]:
        conn.execute(
            "UPDATE enrollments SET status='goal_reached', goal_reached_at=? WHERE id=?",
            (now_s, enrollment_id))
        return {"enrollment_id": enrollment_id, "count": count}
    return None


# ---------------------------------------------------------------- 达标审核与奖品

def review_achievement(conn, enrollment_id, librarian_id, now=None):
    """达标审核：发纪念章预约领取凭证；库存不足按达标时间进候补。

    并发安全：报名状态条件翻转 + inventory 条件扣减 + 唯一约束三重兜底。
    """
    now_s = _now_str(now)
    with tx(conn):
        enr = _require(_one(conn,
                            """SELECT e.*, a.badge_name, a.id aid
                               FROM enrollments e JOIN activities a ON a.id=e.activity_id
                               WHERE e.id=?""", (enrollment_id,)), "报名")
        _check_recusal(conn, librarian_id, enr["child_id"])
        cur = conn.execute(
            "UPDATE enrollments SET status='completed' WHERE id=? AND status='goal_reached'",
            (enrollment_id,))
        if cur.rowcount != 1:
            raise ConflictError("该报名未处于待达标审核状态，可能已被其他馆员处理")
        cur = conn.execute(
            "INSERT INTO achievements(enrollment_id, reviewed_by, achieved_at) VALUES (?,?,?)",
            (enrollment_id, librarian_id, enr["goal_reached_at"] or now_s))
        achievement_id = cur.lastrowid
        got = conn.execute(
            """UPDATE inventory SET issued = issued + 1
               WHERE activity_id=? AND issued < total""",
            (enr["aid"],)).rowcount == 1
        if got:
            code = _voucher_code(conn, enr["aid"])
            conn.execute(
                "INSERT INTO vouchers(achievement_id, code, status, created_at)"
                " VALUES (?,?, 'reserved', ?)",
                (achievement_id, code, now_s))
            return {"result": "voucher", "code": code,
                    "achievement_id": achievement_id}
        conn.execute(
            "INSERT INTO waitlist(activity_id, achievement_id, achieved_at) VALUES (?,?,?)",
            (enr["aid"], achievement_id, enr["goal_reached_at"] or now_s))
        return {"result": "waitlisted", "achievement_id": achievement_id}


def _voucher_code(conn, activity_id) -> str:
    while True:
        code = f"RD{activity_id}-{secrets.token_hex(3).upper()}"
        if not _one(conn, "SELECT 1 x FROM vouchers WHERE code=?", (code,)):
            return code


def restock(conn, activity_id, n, now=None):
    """补货：按达标时间顺序为候补者发放凭证。"""
    if n < 1:
        raise DomainError("补货数量至少为 1")
    now_s = _now_str(now)
    allocated = []
    with tx(conn):
        _require(_one(conn, "SELECT activity_id FROM inventory WHERE activity_id=?",
                      (activity_id,)), "活动库存")
        conn.execute("UPDATE inventory SET total = total + ? WHERE activity_id=?",
                     (int(n), activity_id))
        waiting = conn.execute(
            "SELECT * FROM waitlist WHERE activity_id=? ORDER BY achieved_at, id",
            (activity_id,)).fetchall()
        for w in waiting:
            got = conn.execute(
                """UPDATE inventory SET issued = issued + 1
                   WHERE activity_id=? AND issued < total""",
                (activity_id,)).rowcount == 1
            if not got:
                break
            code = _voucher_code(conn, activity_id)
            conn.execute(
                "INSERT INTO vouchers(achievement_id, code, status, created_at)"
                " VALUES (?,?, 'reserved', ?)",
                (w["achievement_id"], code, now_s))
            conn.execute("DELETE FROM waitlist WHERE id=?", (w["id"],))
            allocated.append({"achievement_id": w["achievement_id"], "code": code})
    return allocated


def claim_voucher(conn, code, now=None):
    """凭预约码领取实体纪念章。"""
    now_s = _now_str(now)
    with tx(conn):
        cur = conn.execute(
            "UPDATE vouchers SET status='claimed', claimed_at=? WHERE code=? AND status='reserved'",
            (now_s, code.strip().upper()))
        if cur.rowcount != 1:
            raise DomainError("凭证无效或已领取")
        return True


# ---------------------------------------------------------------- 查询（供界面）

def child_progress(conn, enrollment_id):
    enr = _require(_one(conn,
                        """SELECT e.*, a.name activity_name, a.goal_books, a.badge_name,
                                  a.end_date, a.borrow_days
                           FROM enrollments e JOIN activities a ON a.id=e.activity_id
                           WHERE e.id=?""", (enrollment_id,)), "报名")
    count = verified_distinct_count(conn, enrollment_id)
    return {"enrollment": enr, "verified": count,
            "goal": enr["goal_books"],
            "pct": min(100, round(100 * count / enr["goal_books"]))}


def librarian_queue(conn, librarian_id):
    """待核验队列：区分「已借阅（可核验）」与「已提交·借阅未同步」。"""
    lib = _require(_one(conn, "SELECT * FROM librarians WHERE id=?", (librarian_id,)),
                   "馆员")
    rows = conn.execute(
        """SELECT rr.id, rr.status, rr.read_date, rr.content, rr.is_backfill,
                  rr.submitted_at, b.title book_title, c.name child_name, c.parent_id,
                  a.name activity_name, a.id activity_id
           FROM reading_records rr
           JOIN enrollments e ON e.id = rr.enrollment_id
           JOIN activities a ON a.id = e.activity_id
           JOIN books b ON b.id = rr.book_id
           JOIN children c ON c.id = e.child_id
           WHERE rr.status IN ('pending','pending_borrow')
           ORDER BY rr.submitted_at""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["own_child"] = lib["parent_id"] is not None and lib["parent_id"] == r["parent_id"]
        d["borrow_state"] = "borrowed" if r["status"] == "pending" else "submitted_only"
        out.append(d)
    return out


def achievement_queue(conn, librarian_id):
    lib = _require(_one(conn, "SELECT * FROM librarians WHERE id=?", (librarian_id,)),
                   "馆员")
    rows = conn.execute(
        """SELECT e.id enrollment_id, e.goal_reached_at, c.name child_name, c.parent_id,
                  a.name activity_name, a.badge_name
           FROM enrollments e
           JOIN activities a ON a.id = e.activity_id
           JOIN children c ON c.id = e.child_id
           WHERE e.status='goal_reached'
           ORDER BY e.goal_reached_at""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["own_child"] = lib["parent_id"] is not None and lib["parent_id"] == r["parent_id"]
        out.append(d)
    return out
