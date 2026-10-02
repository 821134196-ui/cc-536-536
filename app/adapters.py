"""模拟适配器：借阅数据延迟同步、提醒发送。

真实系统里这两块分别对接图书馆 ILS 和短信/邮件网关；
这里用数据库表 + 日志文件模拟，行为可测、可观察。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

_DATETIME_FMT = "%Y-%m-%d %H:%M:%S"


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def fmt(dt: datetime) -> str:
    return dt.strftime(_DATETIME_FMT)


class LoanAdapter:
    """模拟 ILS 借阅适配器。

    借出时事件先落 ``pending``，到 ``sync_after`` 时刻后 poll() 才置为
    ``synced`` —— 对应“借阅记录晚同步”。
    """

    def __init__(self, conn: sqlite3.Connection, delay_seconds: int = 5):
        self.conn = conn
        self.delay_seconds = delay_seconds

    def record_loan(
        self,
        *,
        child_id: int,
        book_id: int,
        activity_id: int,
        loaned_on: str | None = None,
        due_days: int = 14,
        delay_seconds: int | None = None,
    ) -> int:
        ts = now()
        loaned = ts.date() if loaned_on is None else datetime.strptime(loaned_on, "%Y-%m-%d").date()
        delay = self.delay_seconds if delay_seconds is None else delay_seconds
        cur = self.conn.execute(
            """
            INSERT INTO loan_events
                (child_id, book_id, activity_id, loaned_on, due_on, status, sync_after)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                child_id,
                book_id,
                activity_id,
                loaned.isoformat(),
                (loaned + timedelta(days=due_days)).isoformat(),
                fmt(ts + timedelta(seconds=delay)),
            ),
        )
        return cur.lastrowid

    def poll(self, at: datetime | None = None) -> int:
        """把到期的 pending 借阅同步进来，返回本次同步条数。"""
        at = at or now()
        rows = self.conn.execute(
            "SELECT id FROM loan_events WHERE status='pending' AND sync_after <= ?",
            (fmt(at),),
        ).fetchall()
        for r in rows:
            self.conn.execute(
                "UPDATE loan_events SET status='synced', synced_at=? WHERE id=? AND status='pending'",
                (fmt(at), r["id"]),
            )
        return len(rows)

    def is_borrowed(self, child_id: int, book_id: int, activity_id: int) -> bool:
        """馆员核对依据：只有已同步的借阅才算数。"""
        row = self.conn.execute(
            """
            SELECT 1 FROM loan_events
            WHERE child_id=? AND book_id=? AND activity_id=? AND status='synced'
            LIMIT 1
            """,
            (child_id, book_id, activity_id),
        ).fetchone()
        return row is not None

    def mark_returned(self, loan_id: int, returned_on: str | None = None) -> None:
        self.conn.execute(
            "UPDATE loan_events SET returned_on=? WHERE id=?",
            ((returned_on or now().date().isoformat()), loan_id),
        )


class ReminderAdapter:
    """模拟提醒：写 notifications 表并追加日志文件。"""

    def __init__(self, conn: sqlite3.Connection, log_path: str | Path = "data/reminders.log"):
        self.conn = conn
        self.log_path = Path(log_path)

    def send(self, kind: str, title: str, body: str = "", user_id: int | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO notifications (user_id, kind, title, body) VALUES (?,?,?,?)",
            (user_id, kind, title, body),
        )
        nid = cur.lastrowid
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{fmt(now())}] #{nid} to=user:{user_id} {kind} | {title} | {body}\n")
        return nid

    def list_for(self, user_id: int, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()

    def due_soon_scan(self, loan: LoanAdapter, within_days: int = 3) -> int:
        """扫描临期未还借阅，给对应家长发提醒。返回发送条数。"""
        today = now().date()
        rows = self.conn.execute(
            """
            SELECT le.due_on, c.parent_id, c.name AS child_name, b.title
            FROM loan_events le
            JOIN children c ON c.id = le.child_id
            JOIN books b ON b.id = le.book_id
            WHERE le.status='synced' AND le.returned_on IS NULL
              AND date(le.due_on) BETWEEN date(?) AND date(?, '+' || ? || ' days')
            """,
            (today.isoformat(), today.isoformat(), within_days),
        ).fetchall()
        sent = 0
        for r in rows:
            self.send(
                "reminder",
                f"借阅临期提醒：{r['child_name']} 的《{r['title']}》",
                f"应还日期 {r['due_on']}，请在期限内归还。",
                user_id=r["parent_id"],
            )
            sent += 1
        return sent
