"""演示数据：一条命令启动即可看到完整流程，含「同一本书重复提交」案例。"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta

from . import services as svc
from .adapters import MockBorrowAdapter

COVERS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "static", "covers")

_NAVY = ["#16233f", "#1d3a5f", "#22345e", "#142c4a"]
_GREEN = "#2fbf71"


def _cover_svg(title: str, author: str, idx: int) -> str:
    bg = _NAVY[idx % len(_NAVY)]
    band = _NAVY[(idx + 2) % len(_NAVY)]
    short = title.strip("《》")
    # 标题按 4 字一行折行
    lines = [short[i:i + 4] for i in range(0, len(short), 4)][:3]
    tspans = "".join(
        f'<tspan x="90" dy="{0 if i == 0 else 34}">{ln}</tspan>'
        for i, ln in enumerate(lines))
    y0 = 150 - 17 * (len(lines) - 1)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="180" height="240" viewBox="0 0 180 240">
  <rect width="180" height="240" rx="10" fill="{bg}"/>
  <rect x="14" y="14" width="152" height="212" rx="6" fill="none" stroke="{_GREEN}" stroke-opacity="0.55" stroke-width="2"/>
  <circle cx="140" cy="52" r="18" fill="{_GREEN}" fill-opacity="0.85"/>
  <rect x="0" y="196" width="180" height="44" rx="0" fill="{band}"/>
  <text x="90" y="{y0}" font-family="sans-serif" font-size="26" font-weight="700"
        fill="#ffffff" text-anchor="middle">{tspans}</text>
  <text x="90" y="216" font-family="sans-serif" font-size="12" fill="#c9d4e8"
        text-anchor="middle">{author}</text>
</svg>
"""


def generate_covers(books, covers_dir=COVERS_DIR):
    """本地生成封面示意图（SVG），返回 {book_key: 相对路径}。"""
    os.makedirs(covers_dir, exist_ok=True)
    paths = {}
    for i, (key, title, author) in enumerate(books):
        name = f"{key}.svg"
        path = os.path.join(covers_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(_cover_svg(title, author, i))
        paths[key] = f"covers/{name}"
    return paths


def seed(conn, covers_dir=COVERS_DIR):
    """灌入演示数据。日期均相对今天，保证任何时候启动演示都成立。"""
    today = date.today()
    d = lambda n: (today + timedelta(days=n)).isoformat()
    ts = lambda n: (datetime.now() + timedelta(days=n)).strftime("%Y-%m-%d %H:%M:%S")

    books_def = [
        ("moon", "月亮晚安", "李·贝拉"),
        ("forest", "森林图书馆", "王一梅"),
        ("redhat", "小红帽新编", "改编自格林童话"),
        ("space", "太空种子", "刘洋"),
        ("sea", "海的来信", "陈汐"),
        ("insect", "昆虫记·少年版", "法布尔"),
        ("mountain", "山的另一边", "苏岚"),
        ("time", "时间便利店", "韩松"),
    ]
    covers = generate_covers(books_def, covers_dir)
    book_id = {}
    for key, title, author in books_def:
        cur = conn.execute(
            "INSERT INTO books(title, author, isbn, cover) VALUES (?,?,?,?)",
            (title, author, f"978-7-{abs(hash(key)) % 90000 + 10000}", covers[key]))
        book_id[key] = cur.lastrowid

    # 家长 / 孩子 / 馆员（陈静既是馆员也是小雨的家长 → 回避案例）
    p_wang = conn.execute("INSERT INTO parents(name) VALUES ('王芳')").lastrowid
    p_chen = conn.execute("INSERT INTO parents(name) VALUES ('陈静')").lastrowid
    lib_zhang = conn.execute(
        "INSERT INTO librarians(name, parent_id) VALUES ('张敏', NULL)").lastrowid
    lib_chen = conn.execute(
        "INSERT INTO librarians(name, parent_id) VALUES ('陈静', ?)",
        (p_chen,)).lastrowid

    def child(name, age, parent_id):
        # 生日固定在年初，保证报名回溯期内年龄稳定
        birth = date(today.year - age, 1, 15).isoformat()
        return conn.execute(
            "INSERT INTO children(name, birth_date, parent_id) VALUES (?,?,?)",
            (name, birth, parent_id)).lastrowid

    c_ming = child("小明", 8, p_wang)
    c_hong = child("小红", 10, p_wang)
    c_yu = child("小雨", 8, p_chen)

    # 活动与书目（成长挑战中途换过书目：v2 用《海的来信》替换《太空种子》）
    a_sprout = svc.create_activity(
        conn, "萌芽阅读挑战（4-6岁）", 4, 6, d(-20), d(40),
        borrow_days=21, goal_books=2, badge_name="萌芽纪念章", badge_total=3,
        book_ids=[book_id["insect"], book_id["mountain"]], now=datetime.now())
    a_grow = svc.create_activity(
        conn, "成长阅读挑战（7-9岁）", 7, 9, d(-20), d(40),
        borrow_days=21, goal_books=3, badge_name="成长纪念章", badge_total=1,
        book_ids=[book_id["moon"], book_id["forest"], book_id["redhat"],
                  book_id["space"]],
        now=datetime.now())
    svc.replace_book_list(
        conn, a_grow,
        [book_id["moon"], book_id["forest"], book_id["redhat"], book_id["sea"]],
        effective_from=d(-5), note="中途调整：《太空种子》→《海的来信》")
    a_explore = svc.create_activity(
        conn, "探索阅读挑战（10-12岁）", 10, 12, d(-20), d(40),
        borrow_days=21, goal_books=3, badge_name="探索纪念章", badge_total=5,
        book_ids=[book_id["sea"], book_id["mountain"], book_id["time"]],
        now=datetime.now())

    # 报名
    e_ming = svc.enroll(conn, a_grow, c_ming, now=datetime.now() - timedelta(days=18))
    e_hong = svc.enroll(conn, a_explore, c_hong, now=datetime.now() - timedelta(days=18))
    e_yu = svc.enroll(conn, a_grow, c_yu, now=datetime.now() - timedelta(days=15))

    # 借阅：已同步的历史借阅
    borrow = MockBorrowAdapter(conn)
    for cid, keys, days_ago in [
        (c_ming, ["moon", "forest", "redhat"], 15),
        (c_yu, ["moon", "forest", "redhat"], 12),
        (c_hong, ["sea"], 8),
    ]:
        for k in keys:
            borrow.stage_event(cid, book_id[k], d(-days_ago), visible_after=ts(-days_ago))
    svc.sync_borrows(conn)

    # 小明：《太空种子》已借出但借阅系统晚同步（点击「同步借阅」后可见）
    borrow.stage_event(c_ming, book_id["space"], d(-6), visible_after=ts(0))

    def read_and_verify(enrollment, key, read_days_ago, librarian=lib_zhang):
        rid = svc.submit_record(conn, enrollment, book_id[key], d(-read_days_ago),
                                f"读完了《{dict((k, t) for k, t, _ in books_def)[key]}》，很有意思。",
                                now=datetime.now() - timedelta(days=read_days_ago))
        svc.verify_record(conn, rid, librarian, approve=True,
                          now=datetime.now() - timedelta(days=read_days_ago))
        return rid

    # 小明：2 本已核验 + 1 本待核验 + 1 条重复提交（重复书目案例）+ 1 条借阅未同步
    read_and_verify(e_ming, "moon", 14)
    read_and_verify(e_ming, "forest", 10)
    svc.submit_record(conn, e_ming, book_id["redhat"], d(-3),
                      "小红帽这次用地图找到了外婆家。", now=datetime.now() - timedelta(days=3))
    svc.submit_record(conn, e_ming, book_id["moon"], d(-2),
                      "又读了一遍《月亮晚安》。(重复提交示例)",
                      now=datetime.now() - timedelta(days=2))
    svc.submit_record(conn, e_ming, book_id["space"], d(-6),
                      "太空种子在太空站发芽了！(补录 + 借阅同步中)",
                      now=datetime.now())

    # 小雨：3 本全部核验 → 已达「待达标审核」（成长挑战库存仅 1 枚）
    for k, ago in [("moon", 11), ("forest", 9), ("redhat", 6)]:
        read_and_verify(e_yu, k, ago)

    # 小红：1 本已核验
    read_and_verify(e_hong, "sea", 7)

    return {
        "parents": {"王芳": p_wang, "陈静": p_chen},
        "librarians": {"张敏": lib_zhang, "陈静": lib_chen},
        "children": {"小明": c_ming, "小红": c_hong, "小雨": c_yu},
        "activities": {"萌芽": a_sprout, "成长": a_grow, "探索": a_explore},
        "enrollments": {"小明": e_ming, "小红": e_hong, "小雨": e_yu},
        "books": book_id,
    }
