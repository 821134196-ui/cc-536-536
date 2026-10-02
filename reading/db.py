"""SQLite 数据层：连接、schema、事务助手。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS parents (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS librarians (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  -- 馆员本人也可能是家长；审核时需回避自己孩子的记录
  parent_id INTEGER REFERENCES parents(id)
);

CREATE TABLE IF NOT EXISTS children (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  birth_date TEXT NOT NULL,          -- YYYY-MM-DD
  parent_id INTEGER NOT NULL REFERENCES parents(id)
);

CREATE TABLE IF NOT EXISTS activities (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  age_min INTEGER NOT NULL,
  age_max INTEGER NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  borrow_days INTEGER NOT NULL DEFAULT 21,   -- 借阅期限：借出后多少天内阅读有效
  goal_books INTEGER NOT NULL,               -- 完成条件：达标所需的不同书目数量
  badge_name TEXT NOT NULL DEFAULT '纪念章',
  status TEXT NOT NULL DEFAULT 'open'        -- open / closed
);

CREATE TABLE IF NOT EXISTS books (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  isbn TEXT NOT NULL DEFAULT '',
  cover TEXT NOT NULL DEFAULT ''             -- 本地封面文件路径（static/covers/...）
);

-- 书目按版本管理，支持活动中途换书目；read_date 落在哪个版本有效期就按哪个版本判定
CREATE TABLE IF NOT EXISTS book_lists (
  id INTEGER PRIMARY KEY,
  activity_id INTEGER NOT NULL REFERENCES activities(id),
  version INTEGER NOT NULL,
  effective_from TEXT NOT NULL,              -- 该版本自哪天起生效
  note TEXT NOT NULL DEFAULT '',
  UNIQUE(activity_id, version)
);

CREATE TABLE IF NOT EXISTS book_list_items (
  list_id INTEGER NOT NULL REFERENCES book_lists(id),
  book_id INTEGER NOT NULL REFERENCES books(id),
  PRIMARY KEY (list_id, book_id)
);

CREATE TABLE IF NOT EXISTS enrollments (
  id INTEGER PRIMARY KEY,
  activity_id INTEGER NOT NULL REFERENCES activities(id),
  child_id INTEGER NOT NULL REFERENCES children(id),
  age_at_enroll INTEGER NOT NULL,            -- 报名时年龄快照：跨年龄组不迁移
  enrolled_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',     -- active / goal_reached / completed
  goal_reached_at TEXT,
  UNIQUE(activity_id, child_id)
);

CREATE TABLE IF NOT EXISTS reading_records (
  id INTEGER PRIMARY KEY,
  enrollment_id INTEGER NOT NULL REFERENCES enrollments(id),
  book_id INTEGER NOT NULL REFERENCES books(id),
  read_date TEXT NOT NULL,
  content TEXT NOT NULL,
  is_backfill INTEGER NOT NULL DEFAULT 0,    -- 家长补录（read_date 早于提交日）
  -- pending(已借阅待核验) / pending_borrow(已提交待借阅同步) / verified / rejected / duplicate
  status TEXT NOT NULL DEFAULT 'pending',
  reject_reason TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  submitted_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_enroll ON reading_records(enrollment_id, status);

-- 每次修改留一个版本，含原始版本
CREATE TABLE IF NOT EXISTS record_versions (
  id INTEGER PRIMARY KEY,
  record_id INTEGER NOT NULL REFERENCES reading_records(id),
  version INTEGER NOT NULL,
  content TEXT NOT NULL,
  read_date TEXT NOT NULL,
  changed_by TEXT NOT NULL,                  -- parent / librarian
  changed_at TEXT NOT NULL,
  UNIQUE(record_id, version)
);

-- 借阅事件（由模拟适配器同步进来）；synced_at 可能远晚于 borrowed_at（晚同步）
CREATE TABLE IF NOT EXISTS borrow_events (
  id INTEGER PRIMARY KEY,
  child_id INTEGER NOT NULL,
  book_id INTEGER NOT NULL,
  borrowed_at TEXT NOT NULL,
  returned_at TEXT,
  synced_at TEXT NOT NULL
);

-- 适配器发件箱：模拟外部借阅系统延迟同步，visible_after 之前不可见
CREATE TABLE IF NOT EXISTS borrow_outbox (
  id INTEGER PRIMARY KEY,
  child_id INTEGER NOT NULL,
  book_id INTEGER NOT NULL,
  borrowed_at TEXT NOT NULL,
  returned_at TEXT,
  visible_after TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verifications (
  id INTEGER PRIMARY KEY,
  record_id INTEGER NOT NULL REFERENCES reading_records(id),
  librarian_id INTEGER NOT NULL REFERENCES librarians(id),
  decision TEXT NOT NULL,                    -- approve / reject
  reason TEXT NOT NULL DEFAULT '',
  decided_at TEXT NOT NULL,
  UNIQUE(record_id)                          -- 一条记录只会有一个核验结论（并发兜底）
);

CREATE TABLE IF NOT EXISTS achievements (
  id INTEGER PRIMARY KEY,
  enrollment_id INTEGER NOT NULL UNIQUE REFERENCES enrollments(id),
  reviewed_by INTEGER NOT NULL REFERENCES librarians(id),
  achieved_at TEXT NOT NULL                  -- 达标时间（goal_reached_at），候补排序依据
);

CREATE TABLE IF NOT EXISTS inventory (
  activity_id INTEGER PRIMARY KEY REFERENCES activities(id),
  total INTEGER NOT NULL DEFAULT 0,
  issued INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vouchers (
  id INTEGER PRIMARY KEY,
  achievement_id INTEGER NOT NULL UNIQUE REFERENCES achievements(id),
  code TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,                      -- reserved / claimed
  created_at TEXT NOT NULL,
  claimed_at TEXT
);

-- 库存不足时按达标时间进入候补
CREATE TABLE IF NOT EXISTS waitlist (
  id INTEGER PRIMARY KEY,
  activity_id INTEGER NOT NULL REFERENCES activities(id),
  achievement_id INTEGER NOT NULL UNIQUE REFERENCES achievements(id),
  achieved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY,
  parent_id INTEGER,
  message TEXT NOT NULL,
  sent_at TEXT NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # 手动控制事务
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path: str) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE：写事务在进入时就拿到写锁，并发核验在此串行化。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
