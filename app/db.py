"""SQLite schema 与连接管理。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 人员：家长 / 孩子 / 馆员（馆员用 staff 标记）
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('parent','librarian')),
    -- 馆员本人是哪些孩子的家长（审核回避用），多个 id 逗号分隔
    own_children TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS children (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    birth_date  TEXT NOT NULL,            -- YYYY-MM-DD
    parent_id   INTEGER NOT NULL REFERENCES users(id)
);

-- 阅读挑战活动（按年龄段）
CREATE TABLE IF NOT EXISTS activities (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    age_min      INTEGER NOT NULL,        -- 含边界
    age_max      INTEGER NOT NULL,        -- 含边界
    loan_days    INTEGER NOT NULL DEFAULT 14,   -- 借阅期限
    goal_books   INTEGER NOT NULL DEFAULT 3,    -- 完成条件：去重书目数
    starts_on    TEXT NOT NULL,
    ends_on      TEXT NOT NULL,
    medal_stock  INTEGER NOT NULL DEFAULT 0    -- 实体纪念章库存
);

-- 可选书目
CREATE TABLE IF NOT EXISTS books (
    id         INTEGER PRIMARY KEY,
    title      TEXT NOT NULL,
    author     TEXT NOT NULL DEFAULT '',
    isbn       TEXT NOT NULL DEFAULT '',
    cover      TEXT NOT NULL DEFAULT ''   -- /static/covers/xxx.svg
);

CREATE TABLE IF NOT EXISTS activity_books (
    activity_id INTEGER NOT NULL REFERENCES activities(id),
    book_id     INTEGER NOT NULL REFERENCES books(id),
    PRIMARY KEY (activity_id, book_id)
);

-- 报名
CREATE TABLE IF NOT EXISTS enrollments (
    id           INTEGER PRIMARY KEY,
    activity_id  INTEGER NOT NULL REFERENCES activities(id),
    child_id     INTEGER NOT NULL REFERENCES children(id),
    parent_id    INTEGER NOT NULL REFERENCES users(id),
    enrolled_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (activity_id, child_id)
);

-- 阅读记录（一条 = 某孩子对某本书的当前版本）
CREATE TABLE IF NOT EXISTS reading_records (
    id            INTEGER PRIMARY KEY,
    enrollment_id INTEGER NOT NULL REFERENCES enrollments(id),
    child_id      INTEGER NOT NULL REFERENCES children(id),
    book_id       INTEGER NOT NULL REFERENCES books(id),
    status        TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft','submitted','verified','rejected')),
    current_version INTEGER NOT NULL DEFAULT 1,
    note         TEXT NOT NULL DEFAULT '',
    read_on       TEXT,                               -- 实际阅读日期
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (enrollment_id, book_id)                   -- 同一活动同一本书只算一条
);

-- 阅读记录版本历史（每次修改留版本）
CREATE TABLE IF NOT EXISTS reading_record_versions (
    id          INTEGER PRIMARY KEY,
    record_id   INTEGER NOT NULL REFERENCES reading_records(id),
    version     INTEGER NOT NULL,
    book_id     INTEGER NOT NULL REFERENCES books(id),
    note        TEXT NOT NULL DEFAULT '',
    read_on     TEXT,
    changed_by  INTEGER NOT NULL REFERENCES users(id),
    change_kind TEXT NOT NULL,                 -- create / update / submit / review
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (record_id, version)
);

-- 模拟 ILS（图书管理系统）侧的借阅事件：pending 延迟同步 -> synced
CREATE TABLE IF NOT EXISTS loan_events (
    id           INTEGER PRIMARY KEY,
    child_id     INTEGER NOT NULL REFERENCES children(id),
    book_id      INTEGER NOT NULL REFERENCES books(id),
    activity_id  INTEGER NOT NULL REFERENCES activities(id),
    loaned_on    TEXT NOT NULL,
    due_on       TEXT NOT NULL,
    returned_on  TEXT,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','synced')),
    sync_after   TEXT NOT NULL,               -- 该时刻之后适配器才同步
    synced_at    TEXT
);

-- 审核记录
CREATE TABLE IF NOT EXISTS reviews (
    id           INTEGER PRIMARY KEY,
    enrollment_id INTEGER NOT NULL REFERENCES enrollments(id),
    librarian_id INTEGER NOT NULL REFERENCES users(id),
    result       TEXT NOT NULL CHECK (result IN ('approved','rejected')),
    comment      TEXT NOT NULL DEFAULT '',
    reviewed_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 纪念章（每个达标报名一行，发放与并发控制都在这）
CREATE TABLE IF NOT EXISTS medals (
    id            INTEGER PRIMARY KEY,
    activity_id   INTEGER NOT NULL REFERENCES activities(id),
    enrollment_id INTEGER NOT NULL REFERENCES enrollments(id) UNIQUE,
    state         TEXT NOT NULL DEFAULT 'reserved'
                  CHECK (state IN ('reserved','waitlisted','issued')),
    qualified_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    issued_at     TEXT,
    UNIQUE (activity_id, enrollment_id)
);

-- 候补队列：库存不足时按达标时间排队
CREATE TABLE IF NOT EXISTS waitlist (
    id          INTEGER PRIMARY KEY,
    medal_id    INTEGER NOT NULL REFERENCES medals(id) UNIQUE,
    position    INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 预约领取凭证
CREATE TABLE IF NOT EXISTS pickup_vouchers (
    id          INTEGER PRIMARY KEY,
    medal_id    INTEGER NOT NULL REFERENCES medals(id) UNIQUE,
    code        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    picked_up_at TEXT
);

-- 活动中途换书目审计
CREATE TABLE IF NOT EXISTS catalog_changes (
    id          INTEGER PRIMARY KEY,
    activity_id INTEGER NOT NULL REFERENCES activities(id),
    book_id     INTEGER NOT NULL REFERENCES books(id),
    action      TEXT NOT NULL CHECK (action IN ('add','remove')),
    changed_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 模拟提醒适配器落点
CREATE TABLE IF NOT EXISTS notifications (
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER REFERENCES users(id),
    kind      TEXT NOT NULL,          -- reminder / waitlist / voucher / review
    title     TEXT NOT NULL,
    body      TEXT NOT NULL DEFAULT '',
    sent_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_records_child ON reading_records(child_id);
CREATE INDEX IF NOT EXISTS idx_loans_sync ON loan_events(status, sync_after);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
