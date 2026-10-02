"""种子数据与本地 SVG 封面生成。

一条命令即可得到包含全部边界场景的演示库：
- 重复书目（同 ISBN 两行）
- 年龄边界（恰好 6 岁 / 恰好 8 岁 / 大一天出组）
- 借阅延迟同步（pending 等适配器轮询）
- 馆员自己孩子（审核回避）
- 库存 1 枚、多名达标者（候补竞争）
- 跨年龄组孩子
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

from .adapters import ReminderAdapter, now
from .db import connect, init_db
from .service import ActivityService, ChallengeService

NAVY = "#1d335c"
GREEN = "#2f9e6e"
TINTS = ["#e8f3ee", "#eef1f7", "#f4f8f0", "#f7f3ea", "#eef4f6", "#f5eff4"]


def cover_svg(title: str, author: str, idx: int) -> str:
    tint = TINTS[idx % len(TINTS)]
    short = title if len(title) <= 9 else title[:9] + "…"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="200" height="280" viewBox="0 0 200 280">
  <rect width="200" height="280" fill="white" stroke="{NAVY}" stroke-width="2"/>
  <rect x="0" y="0" width="200" height="64" fill="{NAVY}"/>
  <rect x="14" y="86" width="172" height="130" rx="6" fill="{tint}"/>
  <rect x="14" y="86" width="8" height="130" rx="4" fill="{GREEN}"/>
  <text x="100" y="40" font-family="sans-serif" font-size="17" font-weight="bold"
        fill="white" text-anchor="middle">{short}</text>
  <text x="100" y="146" font-family="sans-serif" font-size="15" fill="{NAVY}"
        text-anchor="middle">📖</text>
  <text x="100" y="180" font-family="sans-serif" font-size="11" fill="#5a6577"
        text-anchor="middle">{author[:14]}</text>
  <rect x="14" y="232" width="60" height="8" rx="4" fill="{GREEN}"/>
  <text x="14" y="262" font-family="sans-serif" font-size="9" fill="#9aa3b2">
    阅读挑战 · 示意封面</text>
</svg>
"""


def seed(db_path: str = "data/library.db", cover_dir: str = "app/web/static/covers") -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    Path(db_path).unlink(missing_ok=True)
    conn = connect(db_path)
    init_db(conn)
    conn.execute("BEGIN")
    cdir = Path(cover_dir)
    cdir.mkdir(parents=True, exist_ok=True)

    act_svc = ActivityService(conn)
    chl = ChallengeService(conn)

    # ---- 人员
    users = [
        # name, role, own_children(名字，稍后解析 id)
        ("王岚", "parent", ""),
        ("李伟", "parent", ""),
        ("张敏", "parent", ""),
        ("周梅", "librarian", "周小宝"),
        ("陈静", "librarian", ""),
        ("高帆", "parent", ""),
    ]
    for name, role, own in users:
        conn.execute(
            "INSERT INTO users (name, role, own_children) VALUES (?,?,?)",
            (name, role, own),
        )

    children = [
        # name, birth, parent_name
        ("王小朵", "2018-03-10", "王岚"),    # 8 岁
        ("赵小界", "2017-09-14", "王岚"),    # 开始日恰好 9 岁，出低龄组；可进 9-12
        ("李边界", "2020-09-15", "李伟"),    # 开始日恰好 6 岁，下界
        ("陈慢慢", "2019-06-01", "张敏"),    # 7 岁
        ("周小宝", "2018-05-20", "周梅"),    # 8 岁，馆员孩子
        ("钱正好", "2018-09-15", "高帆"),    # 开始日恰好 8 岁，上界
        ("孙远航", "2014-11-11", "高帆"),    # 11 岁
    ]
    name_to_uid = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM users")}
    for name, birth, pname in children:
        conn.execute(
            "INSERT INTO children (name, birth_date, parent_id) VALUES (?,?,?)",
            (name, birth, name_to_uid[pname]),
        )
    cid = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM children")}
    # 馆员 own_children 字符串填真实孩子 id
    conn.execute("UPDATE users SET own_children=? WHERE name='周梅'", (str(cid["周小宝"]),))

    # ---- 活动
    a_junior = act_svc.create_activity(
        "小小书虫（6-8岁）", 6, 8, "2026-09-15", "2026-12-15",
        goal_books=3, loan_days=14, medal_stock=1,
    )
    a_senior = act_svc.create_activity(
        "阅读冒险家（9-12岁）", 9, 12, "2026-09-15", "2026-12-31",
        goal_books=3, loan_days=21, medal_stock=3,
    )

    # ---- 书目（含一对同 ISBN 重复条目）
    books = [
        ("猜猜我有多爱你", "山姆·麦克布雷尼", "9787533274269"),
        ("猜猜我有多爱你（重复书目）", "山姆·麦克布雷尼", "9787533274269"),
        ("好饿的毛毛虫", "艾瑞·卡尔", "9787544257060"),
        ("大卫，不可以", "大卫·香农", "9787543472197"),
        ("我爸爸", "安东尼·布朗", "9787543464615"),
        ("棕色的熊，你在看什么", "比尔·马丁", "9787020136681"),
        ("夏洛的网", "E.B.怀特", "9787532770625"),
        ("长袜子皮皮", "林格伦", "9787514808384"),
        ("时代广场的蟋蟀", "乔治·塞尔登", "9787530782740"),
        ("查理和巧克力工厂", "罗尔德·达尔", "9787570810420"),
    ]
    book_ids = []
    for i, (title, author, isbn) in enumerate(books):
        p = cdir / f"book{i+1}.svg"
        p.write_text(cover_svg(title, author, i), encoding="utf-8")
        bid = act_svc.add_book(title, author, isbn, f"/static/covers/book{i+1}.svg")
        book_ids.append(bid)
    for bid in book_ids[:6]:
        act_svc.attach_book(a_junior, bid)
    for bid in book_ids[6:]:
        act_svc.attach_book(a_senior, bid)
    bid_by_isbn_title = {r["title"]: r["id"] for r in conn.execute("SELECT id,title FROM books")}

    # ---- 报名
    def enroll(name: str, act: int) -> int:
        uid = conn.execute(
            "SELECT parent_id FROM children WHERE id=?", (cid[name],)
        ).fetchone()["parent_id"]
        return chl.enroll(act, cid[name], uid)

    e_duo = enroll("王小朵", a_junior)
    e_border6 = enroll("李边界", a_junior)
    e_slow = enroll("陈慢慢", a_junior)
    e_staffkid = enroll("周小宝", a_junior)
    e_border8 = enroll("钱正好", a_junior)
    e_sail = enroll("孙远航", a_senior)
    e_cross = enroll("赵小界", a_senior)   # 跨年龄组：在低龄组超龄、在高龄组合格

    # ---- 借阅：synced 直接可见；pending 等适配器轮询（借阅晚同步）
    def loan(name: str, title: str, act: int, *, synced: bool, due_in_days: int | None = None):
        ts = now()
        due = (ts + timedelta(days=due_in_days if due_in_days is not None else 14)).date().isoformat()
        sync_after = (ts + timedelta(seconds=45)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT INTO loan_events
               (child_id, book_id, activity_id, loaned_on, due_on, status, sync_after, synced_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (cid[name], bid_by_isbn_title[title], act, ts.date().isoformat(), due,
             "synced" if synced else "pending",
             "2026-09-15 00:00:00" if synced else sync_after,
             "2026-09-16 09:00:00" if synced else None),
        )

    # 李边界：3 本已借已核 → 直接达标可审核
    for t in ["好饿的毛毛虫", "大卫，不可以", "我爸爸"]:
        loan("李边界", t, a_junior, synced=True)
    # 钱正好：3 本已借已核 → 也达标（库存只有 1，第二个审核进候补）
    for t in ["猜猜我有多爱你", "好饿的毛毛虫", "棕色的熊，你在看什么"]:
        loan("钱正好", t, a_junior, synced=True)
    # 王小朵：2 本已借已提交（其中一本是“重复书目”行），1 本草稿；一本临期（提醒演示）
    loan("王小朵", "猜猜我有多爱你（重复书目）", a_junior, synced=True, due_in_days=2)
    loan("王小朵", "大卫，不可以", a_junior, synced=True)
    # 陈慢慢：3 本已提交但借阅还在同步延迟中
    for t in ["好饿的毛毛虫", "大卫，不可以", "我爸爸"]:
        loan("陈慢慢", t, a_junior, synced=False)
    # 周小宝：2 本已提交，1 本只借了没提交（馆员列表两栏区分）
    loan("周小宝", "猜猜我有多爱你", a_junior, synced=True)
    loan("周小宝", "好饿的毛毛虫", a_junior, synced=True)
    loan("周小宝", "我爸爸", a_junior, synced=True)
    # 高龄组
    loan("孙远航", "长袜子皮皮", a_senior, synced=True)
    loan("赵小界", "夏洛的网", a_senior, synced=True, due_in_days=1)

    # ---- 阅读记录
    def record(enr: int, name: str, title: str, status: str, note: str,
               parent: str, *, versions: int = 1):
        bid = bid_by_isbn_title[title]
        uid = name_to_uid[parent]
        rid = chl.upsert_record(enr, bid, uid, note=note)
        if versions > 1:  # 家长补录/修改：再多留一个版本
            chl.upsert_record(enr, bid, uid, note=note + "（已补录心得）")
        chl.submit_record(rid, uid)
        if status == "verified":
            # 用与孩子无关的馆员陈静核验
            chl.verify_record(rid, name_to_uid["陈静"])
        return rid

    for t in ["好饿的毛毛虫", "大卫，不可以", "我爸爸"]:
        record(e_border6, "李边界", t, "verified", "孩子很喜欢，已读完。", "李伟")
    for t in ["猜猜我有多爱你", "好饿的毛毛虫", "棕色的熊，你在看什么"]:
        record(e_border8, "钱正好", t, "verified", "读完并画了画。", "高帆")

    # 王小朵：重复书目行 + 一条有两个版本的补录 + 一条草稿
    r1 = chl.upsert_record(e_duo, bid_by_isbn_title["猜猜我有多爱你（重复书目）"],
                           name_to_uid["王岚"], note="和妈妈一起读")
    chl.submit_record(r1, name_to_uid["王岚"])
    r2 = chl.upsert_record(e_duo, bid_by_isbn_title["大卫，不可以"],
                           name_to_uid["王岚"], note="初读记录")
    chl.upsert_record(e_duo, bid_by_isbn_title["大卫，不可以"],
                      name_to_uid["王岚"], note="补录：能复述故事，还学着说“不可以”")
    chl.submit_record(r2, name_to_uid["王岚"])
    chl.upsert_record(e_duo, bid_by_isbn_title["我爸爸"],
                      name_to_uid["王岚"], note="刚开始读")  # draft

    # 陈慢慢：全部 submitted，等借阅同步
    for t in ["好饿的毛毛虫", "大卫，不可以", "我爸爸"]:
        record(e_slow, "陈慢慢", t, "submitted", "已读完，等馆员核对借阅。", "张敏")

    # 周小宝：2 条 submitted（周梅不能审，陈静可以）
    for t in ["猜猜我有多爱你", "好饿的毛毛虫"]:
        record(e_staffkid, "周小宝", t, "submitted", "孩子读完。", "周梅")

    # 高龄组
    record(e_sail, "孙远航", "长袜子皮皮", "verified", "很喜欢皮皮。", "高帆")
    record(e_cross, "赵小界", "夏洛的网", "submitted", "读到一半？已读完。", "王岚")

    # 一条目录调整审计（活动中途换书目示例）
    conn.execute(
        "INSERT INTO catalog_changes (activity_id, book_id, action) VALUES (?,?, 'add')",
        (a_junior, book_ids[5]),
    )

    conn.execute("COMMIT")

    ReminderAdapter(conn).send(
        "review", "欢迎使用阅读挑战",
        "演示数据已就绪：重复书目、延迟借阅、审核回避与库存候补场景均可体验。",
        user_id=name_to_uid["王岚"],
    )
    conn.close()


if __name__ == "__main__":
    seed()
    print("种子数据已写入 data/library.db，封面在 app/web/static/covers/")
