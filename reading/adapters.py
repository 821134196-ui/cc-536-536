"""模拟适配器：外部借阅系统（延迟同步）与提醒通知。

真实部署时替换为对接图书馆 ILS / 短信网关的客户端；
这里用 SQLite 发件箱模拟「借阅数据晚同步」，用 reminders 表 + 控制台模拟提醒。
"""
from __future__ import annotations

from datetime import datetime

from .db import tx


class MockBorrowAdapter:
    """模拟外部借阅系统：事件先进入发件箱，到 visible_after 后才可被同步。"""

    def __init__(self, conn):
        self.conn = conn

    def stage_event(self, child_id, book_id, borrowed_at, returned_at=None,
                    visible_after=None):
        """登记一条借阅事件；visible_after 之前同步不到（模拟延迟）。"""
        def fmt(v):
            if isinstance(v, datetime):
                return v.strftime("%Y-%m-%d %H:%M:%S")
            return str(v) if v is not None else None

        visible_after = fmt(visible_after) or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with tx(self.conn):
            cur = self.conn.execute(
                """INSERT INTO borrow_outbox(child_id, book_id, borrowed_at, returned_at,
                                             visible_after)
                   VALUES (?,?,?,?,?)""",
                (child_id, book_id, fmt(borrowed_at), fmt(returned_at), visible_after))
            return cur.lastrowid

    def pending_count(self):
        row = self.conn.execute("SELECT COUNT(*) c FROM borrow_outbox").fetchone()
        return row["c"]


class MockReminderAdapter:
    """模拟提醒通道：写入 reminders 表并打印到控制台。"""

    def __init__(self, conn):
        self.conn = conn

    def send(self, parent_id, message, now=None):
        sent_at = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        with tx(self.conn):
            self.conn.execute(
                "INSERT INTO reminders(parent_id, message, sent_at) VALUES (?,?,?)",
                (parent_id, message, sent_at))
        print(f"[提醒] -> 家长#{parent_id}: {message}")

    def for_parent(self, parent_id):
        return self.conn.execute(
            "SELECT * FROM reminders WHERE parent_id=? ORDER BY id DESC LIMIT 20",
            (parent_id,)).fetchall()
