"""Web 层：stdlib http.server 服务端渲染，三端界面。

配色：白底、藏青（#16233f/#22345e）、明亮但克制的绿（#2fbf71）。
"""
from __future__ import annotations

import html
import os
import re
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from . import services as svc
from .adapters import MockBorrowAdapter, MockReminderAdapter
from .db import connect
from .seed import COVERS_DIR

STATUS_LABEL = {
    "pending": ("待核验 · 已借阅", "chip chip-green"),
    "pending_borrow": ("已提交 · 借阅未同步", "chip chip-gray"),
    "verified": ("已核验", "chip chip-green"),
    "rejected": ("已驳回", "chip chip-navy"),
    "duplicate": ("重复提交 · 不计数", "chip chip-gray"),
}
ENROLL_LABEL = {"active": "进行中", "goal_reached": "待达标审核", "completed": "已完成"}

CSS = """
:root{--navy:#16233f;--navy2:#22345e;--green:#2fbf71;--green-d:#22a05c;
--bg:#f5f7fa;--ink:#2b3448;--muted:#7a869e;--line:#e3e8f0}
*{box-sizing:border-box;margin:0}
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--ink);line-height:1.55}
a{color:var(--navy2)}
header{background:var(--navy);color:#fff;padding:14px 24px;display:flex;
align-items:center;gap:16px;flex-wrap:wrap}
header .logo{font-size:18px;font-weight:700;letter-spacing:1px}
header .logo em{color:var(--green);font-style:normal}
header nav a{color:#c9d4e8;text-decoration:none;margin-right:14px;font-size:14px}
header nav a:hover{color:#fff}
main{max-width:1080px;margin:24px auto;padding:0 20px}
h1{font-size:22px;color:var(--navy);margin-bottom:4px}
h2{font-size:17px;color:var(--navy);margin:26px 0 10px;padding-left:10px;
border-left:4px solid var(--green)}
.sub{color:var(--muted);font-size:13px;margin-bottom:14px}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;
padding:18px 20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(22,35,63,.05)}
.grid{display:grid;gap:14px}
.cols-3{grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
table{width:100%;border-collapse:collapse;font-size:14px}
th{background:var(--navy);color:#fff;text-align:left;padding:8px 10px;
font-weight:600;font-size:13px}
td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
.chip{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;
font-weight:600;white-space:nowrap}
.chip-green{background:#e3f7ec;color:var(--green-d)}
.chip-gray{background:#eef1f6;color:var(--muted)}
.chip-navy{background:#e4e9f5;color:var(--navy2)}
.chip-solid-green{background:var(--green);color:#fff}
.btn{display:inline-block;border:none;cursor:pointer;border-radius:8px;
padding:7px 16px;font-size:14px;font-weight:600;text-decoration:none}
.btn-green{background:var(--green);color:#fff}
.btn-green:hover{background:var(--green-d)}
.btn-navy{background:var(--navy);color:#fff}
.btn-navy:hover{background:var(--navy2)}
.btn-ghost{background:#fff;color:var(--navy2);border:1px solid var(--line)}
.btn:disabled{background:#c6cddb;cursor:not-allowed;color:#fff}
.btn-sm{padding:4px 12px;font-size:13px}
input,select,textarea{font:inherit;font-size:14px;padding:7px 10px;
border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--ink)}
textarea{width:100%;min-height:64px}
label{font-size:13px;color:var(--muted);display:block;margin:8px 0 3px}
.progress{height:22px;background:#e8edf5;border-radius:999px;overflow:hidden;
position:relative}
.progress>i{display:block;height:100%;background:linear-gradient(90deg,#27a964,
var(--green));border-radius:999px;transition:width .4s}
.progress b{position:absolute;inset:0;display:flex;align-items:center;
justify-content:center;font-size:12px;color:var(--navy)}
.covers{display:grid;grid-template-columns:repeat(auto-fill,minmax(128px,1fr));
gap:14px}
.cover{position:relative;border-radius:10px;overflow:hidden;
border:1px solid var(--line);background:#fff}
.cover img{width:100%;display:block}
.cover .st{position:absolute;top:6px;right:6px}
.cover .cap{font-size:12px;padding:6px 8px;color:var(--ink);text-align:center;
background:#fff}
.cover.dim img{opacity:.45;filter:grayscale(.6)}
.flash{padding:10px 16px;border-radius:10px;margin-bottom:14px;font-size:14px}
.flash.ok{background:#e3f7ec;color:var(--green-d);border:1px solid #b9e9cf}
.flash.err{background:#f3f5fa;color:#9a3b4d;border:1px solid #e5c3cb}
.banner{background:linear-gradient(135deg,var(--navy),var(--navy2));color:#fff;
border-radius:12px;padding:16px 20px;margin-bottom:16px}
.banner .code{font-size:22px;font-weight:700;letter-spacing:2px;color:var(--green)}
.muted{color:var(--muted);font-size:13px}
.row{display:flex;gap:10px;align-items:end;flex-wrap:wrap}
.pill{font-size:12px;background:#eef1f6;border-radius:6px;padding:1px 8px;
color:var(--muted)}
details{margin-top:6px}
summary{cursor:pointer;font-size:13px;color:var(--navy2)}
.role-card{text-align:center;padding:26px 14px}
.role-card .who{font-size:17px;font-weight:700;color:var(--navy);margin-bottom:4px}
"""


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def chip(status: str) -> str:
    label, cls = STATUS_LABEL.get(status, (status, "chip"))
    return f'<span class="{cls}">{esc(label)}</span>'


def page(title: str, body: str, msg="", err="") -> bytes:
    flash = ""
    if msg:
        flash = f'<div class="flash ok">✓ {esc(msg)}</div>'
    if err:
        flash = f'<div class="flash err">✗ {esc(err)}</div>'
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 图书馆阅读挑战</title><style>{CSS}</style></head><body>
<header><span class="logo">📚 图书馆<em>阅读挑战</em></span>
<nav><a href="/">首页</a></nav></header>
<main>{flash}{body}</main></body></html>""".encode("utf-8")


# ---------------------------------------------------------------- 查询助手

def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def q1(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def voucher_for(conn, enrollment_id):
    return q1(conn,
              """SELECT v.* FROM vouchers v
                 JOIN achievements a ON a.id = v.achievement_id
                 WHERE a.enrollment_id=?""", (enrollment_id,))


def waitlist_position(conn, enrollment_id):
    row = q1(conn,
             """SELECT w.id, w.activity_id, w.achieved_at FROM waitlist w
                JOIN achievements a ON a.id = w.achievement_id
                WHERE a.enrollment_id=?""", (enrollment_id,))
    if not row:
        return None
    ahead = q1(conn,
               """SELECT COUNT(*) c FROM waitlist
                  WHERE activity_id=? AND (achieved_at < ? OR (achieved_at = ? AND id < ?))""",
               (row["activity_id"], row["achieved_at"], row["achieved_at"], row["id"]))["c"]
    return ahead + 1


def book_status_map(conn, enrollment_id):
    """book_id -> 该报名下最有效的记录状态（verified > pending* > rejected）。"""
    rows = q(conn,
             """SELECT book_id, status FROM reading_records
                WHERE enrollment_id=? AND status != 'duplicate'
                ORDER BY id""", (enrollment_id,))
    rank = {"verified": 3, "pending": 2, "pending_borrow": 1, "rejected": 0}
    out = {}
    for r in rows:
        if rank.get(r["status"], -1) >= rank.get(out.get(r["book_id"], "rejected"), -1):
            out[r["book_id"]] = r["status"]
    return out


# ---------------------------------------------------------------- 页面

def view_home(conn, msg, err):
    acts = q(conn, "SELECT * FROM activities ORDER BY id")
    act_cards = ""
    for a in acts:
        inv = q1(conn, "SELECT * FROM inventory WHERE activity_id=?", (a["id"],))
        n_enr = q1(conn, "SELECT COUNT(*) c FROM enrollments WHERE activity_id=?",
                   (a["id"],))["c"]
        act_cards += f"""<div class="card"><b>{esc(a['name'])}</b>
        <div class="muted">{a['start_date']} ~ {a['end_date']} · {a['age_min']}-{a['age_max']}岁
        · 目标 {a['goal_books']} 本 · 借期 {a['borrow_days']} 天</div>
        <div class="muted">{esc(a['badge_name'])}：剩余 {inv['total']-inv['issued']}/{inv['total']}
        · 已报名 {n_enr} 人</div></div>"""

    enrollments = q(conn,
                    """SELECT e.id, c.name child_name, a.name act_name
                       FROM enrollments e JOIN children c ON c.id=e.child_id
                       JOIN activities a ON a.id=e.activity_id ORDER BY e.id""")
    child_links = "".join(
        f'<div class="card role-card"><div class="who">{esc(r["child_name"])}</div>'
        f'<div class="muted">{esc(r["act_name"])}</div>'
        f'<p style="margin-top:10px"><a class="btn btn-green btn-sm" '
        f'href="/child/{r["id"]}">我的阅读进度</a></p></div>'
        for r in enrollments)
    parents = q(conn, "SELECT * FROM parents ORDER BY id")
    parent_links = "".join(
        f'<div class="card role-card"><div class="who">{esc(p["name"])}</div>'
        f'<div class="muted">家长</div><p style="margin-top:10px">'
        f'<a class="btn btn-navy btn-sm" href="/parent/{p["id"]}">家长工作台</a></p></div>'
        for p in parents)
    libs = q(conn, "SELECT * FROM librarians ORDER BY id")
    lib_links = "".join(
        f'<div class="card role-card"><div class="who">{esc(l["name"])}</div>'
        f'<div class="muted">馆员</div><p style="margin-top:10px">'
        f'<a class="btn btn-navy btn-sm" href="/librarian/{l["id"]}">馆员工作台</a></p></div>'
        for l in libs)
    body = f"""
<h1>阅读挑战</h1><p class="sub">按年龄段参与活动，完成书目阅读，领取纪念章。</p>
<h2>进行中的活动</h2><div class="grid cols-3">{act_cards}</div>
<h2>孩子入口</h2><div class="grid cols-3">{child_links or '<p class="muted">暂无报名</p>'}</div>
<h2>家长入口</h2><div class="grid cols-3">{parent_links}</div>
<h2>馆员入口</h2><div class="grid cols-3">{lib_links}</div>"""
    return page("首页", body, msg, err)


def view_child(conn, enrollment_id, msg, err):
    prog = svc.child_progress(conn, enrollment_id)
    e = prog["enrollment"]
    child = q1(conn, "SELECT * FROM children WHERE id=?", (e["child_id"],))
    books = svc.current_book_list(conn, e["activity_id"])
    status_map = book_status_map(conn, enrollment_id)
    v = voucher_for(conn, enrollment_id)
    wl = waitlist_position(conn, enrollment_id)

    banner = ""
    if v and v["status"] == "reserved":
        banner = f"""<div class="banner">🎉 挑战完成！{esc(e['badge_name'])}预约领取凭证：
        <span class="code">{esc(v['code'])}</span>
        <div style="font-size:13px;opacity:.85">请家长凭此码到服务台领取实体纪念章</div></div>"""
    elif v and v["status"] == "claimed":
        banner = f"""<div class="banner">🏅 纪念章已领取，恭喜完成挑战！</div>"""
    elif wl:
        banner = f"""<div class="banner">🎉 挑战已完成！纪念章补货中，当前候补第
        <span class="code">{wl}</span> 位，到货后按达标顺序发放。</div>"""
    elif e["status"] == "goal_reached":
        banner = """<div class="banner">✅ 已达标，等待馆员审核发放纪念章。</div>"""

    cards = ""
    for b in books:
        st = status_map.get(b["id"])
        if st == "verified":
            badge, dim = '<span class="chip chip-solid-green st">✓ 已核验</span>', ""
        elif st in ("pending", "pending_borrow"):
            badge, dim = '<span class="chip chip-navy st">⏳ 核验中</span>', ""
        else:
            badge, dim = "", "dim"
        cards += f"""<div class="cover {dim}"><img src="/{esc(b['cover'])}" alt="{esc(b['title'])}">
        {badge}<div class="cap">{esc(b['title'])}</div></div>"""

    body = f"""{banner}
<h1>{esc(child['name'])} · {esc(e['activity_name'])}</h1>
<p class="sub">状态：{ENROLL_LABEL.get(e['status'], e['status'])} · 活动截止 {e['end_date']}</p>
<div class="card"><h2 style="margin-top:0">阅读进度</h2>
<div class="progress"><i style="width:{prog['pct']}%"></i>
<b>{prog['verified']} / {prog['goal']} 本</b></div>
<p class="muted" style="margin-top:8px">已核验 {prog['verified']} 本不同的书，目标 {prog['goal']} 本。</p></div>
<h2>活动书目</h2><div class="covers">{cards}</div>"""
    return page(f"{child['name']}的阅读", body, msg, err)


def _record_rows_for_parent(conn, enrollment_id):
    return q(conn,
             """SELECT rr.*, b.title book_title FROM reading_records rr
                JOIN books b ON b.id=rr.book_id
                WHERE rr.enrollment_id=? ORDER BY rr.id DESC""", (enrollment_id,))


def view_parent(conn, parent_id, msg, err):
    parent = q1(conn, "SELECT * FROM parents WHERE id=?", (parent_id,))
    if not parent:
        return page("未找到", "<p>家长不存在</p>", err="")
    children = q(conn, "SELECT * FROM children WHERE parent_id=? ORDER BY id", (parent_id,))

    # 待核验项汇总
    pending = q(conn,
                """SELECT rr.id, rr.status, rr.read_date, b.title, c.name child_name
                   FROM reading_records rr
                   JOIN enrollments e ON e.id=rr.enrollment_id
                   JOIN children c ON c.id=e.child_id
                   JOIN books b ON b.id=rr.book_id
                   WHERE c.parent_id=? AND rr.status IN ('pending','pending_borrow')
                   ORDER BY rr.submitted_at""", (parent_id,))
    if pending:
        rows = "".join(
            f"<tr><td>{esc(r['child_name'])}</td><td>{esc(r['title'])}</td>"
            f"<td>{r['read_date']}</td><td>{chip(r['status'])}</td></tr>" for r in pending)
        pending_html = f"""<div class="card"><h2 style="margin-top:0">待核验项（{len(pending)}）</h2>
        <table><tr><th>孩子</th><th>图书</th><th>阅读日期</th><th>状态</th></tr>{rows}</table>
        <p class="muted">「借阅未同步」的记录会在借阅数据同步后自动进入待核验。</p></div>"""
    else:
        pending_html = '<div class="card"><h2 style="margin-top:0">待核验项</h2>' \
                       '<p class="muted">暂无待核验的阅读记录。</p></div>'

    child_blocks = ""
    for c in children:
        enrs = q(conn,
                 """SELECT e.*, a.name act_name, a.goal_books, a.badge_name, a.id aid
                    FROM enrollments e JOIN activities a ON a.id=e.activity_id
                    WHERE e.child_id=? ORDER BY e.id""", (c["id"],))
        for e in enrs:
            prog = svc.child_progress(conn, e["id"])
            books = svc.current_book_list(conn, e["aid"])
            options = "".join(f'<option value="{b["id"]}">{esc(b["title"])}</option>'
                              for b in books)
            v = voucher_for(conn, e["id"])
            wl = waitlist_position(conn, e["id"])
            voucher_html = ""
            if v:
                state = "待领取" if v["status"] == "reserved" else "已领取"
                voucher_html = f"""<p>🎖️ {esc(e['badge_name'])}凭证：
                <b style="color:var(--green-d)">{esc(v['code'])}</b>
                <span class="pill">{state}</span></p>"""
            elif wl:
                voucher_html = f'<p class="muted">纪念章候补中：第 {wl} 位（按达标时间排序）</p>'

            rec_rows = ""
            for r in _record_rows_for_parent(conn, e["id"]):
                backfill = '<span class="pill">补录</span>' if r["is_backfill"] else ""
                reason = (f'<div class="muted">驳回原因：{esc(r["reject_reason"])}</div>'
                          if r["status"] == "rejected" and r["reject_reason"] else "")
                versions = q(conn,
                             "SELECT * FROM record_versions WHERE record_id=? ORDER BY version",
                             (r["id"],))
                vlist = "".join(
                    f'<li class="muted">v{vv["version"]} · {vv["changed_at"]} · '
                    f'{esc(vv["content"])}（读于 {vv["read_date"]}）</li>' for vv in versions)
                edit = ""
                if r["status"] != "duplicate":
                    edit = f"""<details><summary>修改（留版本）</summary>
                    <form method="post" action="/record/edit">
                    <input type="hidden" name="record_id" value="{r['id']}">
                    <input type="hidden" name="next" value="/parent/{parent_id}">
                    <label>阅读日期</label><input type="date" name="read_date"
                      value="{r['read_date']}" required>
                    <label>记录内容</label><textarea name="content"
                      required>{esc(r['content'])}</textarea>
                    <p style="margin-top:6px"><button class="btn btn-navy btn-sm">保存新版本</button>
                    </p></form></details>"""
                rec_rows += f"""<tr><td>{esc(r['book_title'])}</td><td>{r['read_date']} {backfill}</td>
                <td>{esc(r['content'])}{reason}
                <details><summary>历史版本（v{r['version']}）</summary><ul>{vlist}</ul></details>
                {edit}</td><td>{chip(r['status'])}</td></tr>"""
            rec_table = (f"""<table><tr><th>图书</th><th>阅读日期</th><th>记录</th>
            <th>状态</th></tr>{rec_rows}</table>""" if rec_rows else
                         '<p class="muted">还没有阅读记录。</p>')

            child_blocks += f"""<div class="card">
<h2 style="margin-top:0">{esc(c['name'])} · {esc(e['act_name'])}
<span class="pill">{ENROLL_LABEL.get(e['status'], e['status'])}</span></h2>
<div class="progress"><i style="width:{prog['pct']}%"></i>
<b>{prog['verified']} / {prog['goal']} 本</b></div>
{voucher_html}
<h2>提交阅读记录</h2>
<form method="post" action="/record/submit">
<input type="hidden" name="enrollment_id" value="{e['id']}">
<input type="hidden" name="next" value="/parent/{parent_id}">
<div class="row"><div><label>图书（当前书目）</label>
<select name="book_id">{options}</select></div>
<div><label>阅读日期</label>
<input type="date" name="read_date" value="{date.today().isoformat()}" required></div></div>
<label>简短记录</label><textarea name="content" required
  placeholder="孩子读了什么，一两句就好"></textarea>
<p style="margin-top:8px"><button class="btn btn-green">提交记录</button>
<span class="muted"> 阅读日期早于今天会标记为「补录」</span></p></form>
<h2>阅读记录</h2>{rec_table}</div>"""

    reminders = MockReminderAdapter(conn).for_parent(parent_id)
    rem_html = ""
    if reminders:
        items = "".join(f'<li class="muted">{r["sent_at"]} · {esc(r["message"])}</li>'
                        for r in reminders)
        rem_html = f'<div class="card"><h2 style="margin-top:0">提醒</h2><ul>{items}</ul></div>'

    body = f"""<h1>家长工作台 · {esc(parent['name'])}</h1>
<p class="sub">提交与修改阅读记录，跟踪核验与奖品进度。</p>
{pending_html}{child_blocks}{rem_html}"""
    return page("家长工作台", body, msg, err)


def view_librarian(conn, lib_id, msg, err):
    lib = q1(conn, "SELECT * FROM librarians WHERE id=?", (lib_id,))
    if not lib:
        return page("未找到", "<p>馆员不存在</p>")
    back = f"/librarian/{lib_id}"

    # 待核验队列
    queue = svc.librarian_queue(conn, lib_id)
    qrows = ""
    for r in queue:
        borrow_badge = ('<span class="chip chip-green">已借阅</span>'
                        if r["borrow_state"] == "borrowed"
                        else '<span class="chip chip-gray">已提交 · 借阅未同步</span>')
        backfill = ' <span class="pill">补录</span>' if r["is_backfill"] else ""
        if r["own_child"]:
            actions = '<span class="chip chip-navy">回避：本人子女</span>'
        elif r["borrow_state"] != "borrowed":
            actions = '<span class="muted">待借阅同步</span>'
        else:
            actions = f"""<form method="post" action="/verify" style="display:inline">
<input type="hidden" name="record_id" value="{r['id']}">
<input type="hidden" name="librarian_id" value="{lib_id}">
<input type="hidden" name="next" value="{back}">
<button class="btn btn-green btn-sm" name="decision" value="approve">通过</button>
<input name="reason" placeholder="驳回原因" style="width:110px;padding:4px 8px;font-size:12px">
<button class="btn btn-ghost btn-sm" name="decision" value="reject">驳回</button></form>"""
        qrows += f"""<tr><td>{esc(r['child_name'])}</td><td>{esc(r['activity_name'])}</td>
<td>{esc(r['book_title'])}</td><td>{r['read_date']}{backfill}</td>
<td>{esc(r['content'])}</td><td>{borrow_badge}</td><td>{actions}</td></tr>"""
    queue_html = (f"""<table><tr><th>孩子</th><th>活动</th><th>图书</th><th>阅读日期</th>
<th>记录</th><th>借阅状态</th><th>操作</th></tr>{qrows}</table>""" if qrows else
                  '<p class="muted">队列已清空。</p>')

    # 达标审核队列
    ach = svc.achievement_queue(conn, lib_id)
    arows = ""
    for r in ach:
        inv = q1(conn,
                 """SELECT i.* FROM inventory i JOIN enrollments e
                    ON e.activity_id=i.activity_id WHERE e.id=?""",
                 (r["enrollment_id"],))
        remain = inv["total"] - inv["issued"]
        stock = (f'<span class="chip chip-green">库存 {remain}</span>' if remain > 0
                 else '<span class="chip chip-gray">缺货 → 候补</span>')
        if r["own_child"]:
            action = '<span class="chip chip-navy">回避：本人子女</span>'
        else:
            action = f"""<form method="post" action="/achievement/review" style="display:inline">
<input type="hidden" name="enrollment_id" value="{r['enrollment_id']}">
<input type="hidden" name="librarian_id" value="{lib_id}">
<input type="hidden" name="next" value="{back}">
<button class="btn btn-green btn-sm">审核通过并发放</button></form>"""
        arows += f"""<tr><td>{esc(r['child_name'])}</td><td>{esc(r['activity_name'])}</td>
<td>{esc(r['badge_name'])}</td><td>{r['goal_reached_at']}</td><td>{stock}</td>
<td>{action}</td></tr>"""
    ach_html = (f"""<table><tr><th>孩子</th><th>活动</th><th>奖品</th><th>达标时间</th>
<th>库存</th><th>操作</th></tr>{arows}</table>""" if ach else
                '<p class="muted">暂无待达标审核。</p>')

    # 库存与候补
    inv_rows = ""
    for a in q(conn, "SELECT * FROM activities ORDER BY id"):
        inv = q1(conn, "SELECT * FROM inventory WHERE activity_id=?", (a["id"],))
        wl = q(conn,
               """SELECT w.achieved_at, c.name child_name FROM waitlist w
                  JOIN achievements ac ON ac.id=w.achievement_id
                  JOIN enrollments e ON e.id=ac.enrollment_id
                  JOIN children c ON c.id=e.child_id
                  WHERE w.activity_id=? ORDER BY w.achieved_at, w.id""", (a["id"],))
        wl_txt = "、".join(f"{i+1}.{w['child_name']}({w['achieved_at'][:10]})"
                           for i, w in enumerate(wl)) or "无"
        inv_rows += f"""<tr><td>{esc(a['name'])}</td><td>{esc(a['badge_name'])}</td>
<td>{inv['total']-inv['issued']} / {inv['total']}</td><td>{esc(wl_txt)}</td>
<td><form method="post" action="/restock" style="display:inline">
<input type="hidden" name="activity_id" value="{a['id']}">
<input type="hidden" name="next" value="{back}">
<input type="number" name="n" value="1" min="1" style="width:60px;padding:4px 8px">
<button class="btn btn-navy btn-sm">补货</button></form></td></tr>"""
    inv_html = f"""<table><tr><th>活动</th><th>奖品</th><th>剩余/总量</th>
<th>候补（按达标时间）</th><th>补货</th></tr>{inv_rows}</table>"""

    # 活动配置
    act_options = "".join(f'<option value="{a["id"]}">{esc(a["name"])}</option>'
                          for a in q(conn, "SELECT * FROM activities ORDER BY id"))
    book_checks = "".join(
        f'<label style="display:inline-block;margin:2px 10px 2px 0">'
        f'<input type="checkbox" name="book_id" value="{b["id"]}"> {esc(b["title"])}</label>'
        for b in q(conn, "SELECT * FROM books ORDER BY id"))
    config_html = f"""<details><summary>新建活动</summary>
<form method="post" action="/activity/create"><input type="hidden" name="next" value="{back}">
<div class="row">
<div><label>名称</label><input name="name" required></div>
<div><label>年龄下限</label><input type="number" name="age_min" value="4" min="0" required></div>
<div><label>年龄上限</label><input type="number" name="age_max" value="6" min="0" required></div>
<div><label>开始</label><input type="date" name="start_date" required></div>
<div><label>结束</label><input type="date" name="end_date" required></div>
<div><label>借阅期限(天)</label><input type="number" name="borrow_days" value="21" min="1"></div>
<div><label>达标本数</label><input type="number" name="goal_books" value="3" min="1"></div>
<div><label>纪念章名称</label><input name="badge_name" value="纪念章"></div>
<div><label>纪念章库存</label><input type="number" name="badge_total" value="10" min="0"></div>
</div><p style="margin-top:8px"><button class="btn btn-navy btn-sm">创建</button></p></form></details>
<details><summary>调整活动书目（中途换书目，生成新版本）</summary>
<form method="post" action="/booklist/replace"><input type="hidden" name="next" value="{back}">
<label>活动</label><select name="activity_id">{act_options}</select>
<label>生效日期</label><input type="date" name="effective_from"
  value="{date.today().isoformat()}" required>
<label>调整说明</label><input name="note" placeholder="如：替换绝版图书">
<div style="margin:6px 0">{book_checks}</div>
<p><button class="btn btn-navy btn-sm">发布新书目版本</button></p></form></details>
<details><summary>新增图书</summary>
<form method="post" action="/book/create"><input type="hidden" name="next" value="{back}">
<div class="row"><div><label>书名</label><input name="title" required></div>
<div><label>作者</label><input name="author"></div></div>
<p style="margin-top:8px"><button class="btn btn-navy btn-sm">添加（自动生成封面示意图）</button></p>
</form></details>"""

    # 凭证领取 + 借阅同步
    outbox_n = MockBorrowAdapter(conn).pending_count()
    ops_html = f"""<div class="row">
<form method="post" action="/sync"><input type="hidden" name="next" value="{back}">
<button class="btn btn-navy">同步借阅数据</button>
<span class="pill">适配器待发 {outbox_n} 条</span></form>
<form method="post" action="/claim"><input type="hidden" name="next" value="{back}">
<label style="margin:0">凭证领取</label>
<input name="code" placeholder="输入预约码" required style="text-transform:uppercase">
<button class="btn btn-green">核销发放</button></form></div>"""

    recent = q(conn,
               """SELECT v.decided_at, v.decision, v.reason, l.name lib_name,
                         b.title, c.name child_name
                  FROM verifications v
                  JOIN reading_records rr ON rr.id=v.record_id
                  JOIN enrollments e ON e.id=rr.enrollment_id
                  JOIN children c ON c.id=e.child_id
                  JOIN books b ON b.id=rr.book_id
                  JOIN librarians l ON l.id=v.librarian_id
                  ORDER BY v.id DESC LIMIT 8""")
    recent_html = ""
    if recent:
        rows = "".join(
            f"<tr><td>{r['decided_at']}</td><td>{esc(r['child_name'])}</td>"
            f"<td>{esc(r['title'])}</td><td>{'通过' if r['decision']=='approve' else '驳回'}"
            f"{'（'+esc(r['reason'])+'）' if r['reason'] else ''}</td>"
            f"<td>{esc(r['lib_name'])}</td></tr>" for r in recent)
        recent_html = f"""<h2>最近核验</h2><div class="card"><table>
<tr><th>时间</th><th>孩子</th><th>图书</th><th>结论</th><th>馆员</th></tr>{rows}</table></div>"""

    body = f"""<h1>馆员工作台 · {esc(lib['name'])}</h1>
<p class="sub">核对借阅与完成情况，审核达标，管理库存与活动。</p>
<h2>待核验队列</h2><div class="card">{queue_html}</div>
<h2>达标审核</h2><div class="card">{ach_html}</div>
<h2>库存与候补</h2><div class="card">{inv_html}</div>
<h2>日常操作</h2><div class="card">{ops_html}</div>
<h2>活动配置</h2><div class="card">{config_html}</div>
{recent_html}"""
    return page("馆员工作台", body, msg, err)


# ---------------------------------------------------------------- 动作

def act_submit_record(conn, form):
    rid = svc.submit_record(conn, int(form["enrollment_id"]), int(form["book_id"]),
                            form["read_date"], form.get("content", ""))
    st = q1(conn, "SELECT status FROM reading_records WHERE id=?", (rid,))["status"]
    if st == "duplicate":
        return "已提交：与已有记录重复，不计入进度"
    if st == "pending_borrow":
        return "已提交，等待借阅数据同步后核验"
    return "阅读记录已提交，等待馆员核验"


def act_edit_record(conn, form):
    v = svc.edit_record(conn, int(form["record_id"]), form.get("content", ""),
                        form["read_date"], changed_by="parent")
    return f"已保存为第 {v} 版"


def act_verify(conn, form):
    decision = form.get("decision", "approve")
    status, goal = svc.verify_record(
        conn, int(form["record_id"]), int(form["librarian_id"]),
        approve=(decision == "approve"), reason=form.get("reason", ""))
    if status == "verified" and goal:
        return "已通过核验；该孩子已达标，进入达标审核"
    return "已通过核验" if status == "verified" else "已驳回"


def act_review_achievement(conn, form):
    r = svc.review_achievement(conn, int(form["enrollment_id"]),
                               int(form["librarian_id"]))
    if r["result"] == "voucher":
        return f"审核通过，已发放预约领取凭证 {r['code']}"
    return "审核通过；纪念章缺货，已按达标时间进入候补"


def act_sync(conn, _form):
    moved = svc.sync_borrows(conn)
    return f"同步完成：新到 {len(moved)} 条借阅记录，待同步记录已重新评估"


def act_restock(conn, form):
    allocated = svc.restock(conn, int(form["activity_id"]), int(form.get("n", 1)))
    if allocated:
        return f"已补货，按达标顺序为 {len(allocated)} 位候补者发放了凭证"
    return "已补货"


def act_claim(conn, form):
    svc.claim_voucher(conn, form.get("code", ""))
    return "凭证已核销，纪念章已发放"


def act_create_activity(conn, form):
    svc.create_activity(
        conn, form["name"], int(form["age_min"]), int(form["age_max"]),
        form["start_date"], form["end_date"],
        borrow_days=int(form.get("borrow_days", 21)),
        goal_books=int(form.get("goal_books", 3)),
        badge_name=form.get("badge_name", "纪念章"),
        badge_total=int(form.get("badge_total", 0)))
    return "活动已创建（可在「调整活动书目」中配置书目）"


def act_replace_booklist(conn, form):
    book_ids = [int(x) for x in form.get("book_id", [])]
    version = svc.replace_book_list(conn, int(form["activity_id"]), book_ids,
                                    form["effective_from"], form.get("note", ""))
    return f"书目已更新为第 {version} 版"


def act_create_book(conn, form):
    from .seed import _cover_svg
    title = form["title"].strip()
    author = form.get("author", "").strip()
    n = q1(conn, "SELECT COUNT(*) c FROM books")["c"]
    key = re.sub(r"\W+", "-", title)[:20] or f"book{n+1}"
    name = f"{key}-{n+1}.svg"
    os.makedirs(COVERS_DIR, exist_ok=True)
    with open(os.path.join(COVERS_DIR, name), "w", encoding="utf-8") as f:
        f.write(_cover_svg(title, author, n))
    conn.execute("INSERT INTO books(title, author, cover) VALUES (?,?,?)",
                 (title, author, f"covers/{name}"))
    return f"已添加《{title}》"


# ---------------------------------------------------------------- 路由

GET_ROUTES = [
    (re.compile(r"^/$"), lambda conn, m, q_: view_home(conn, q_.get("msg", [""])[0],
                                                       q_.get("err", [""])[0])),
    (re.compile(r"^/child/(\d+)$"),
     lambda conn, m, q_: view_child(conn, int(m.group(1)), q_.get("msg", [""])[0],
                                    q_.get("err", [""])[0])),
    (re.compile(r"^/parent/(\d+)$"),
     lambda conn, m, q_: view_parent(conn, int(m.group(1)), q_.get("msg", [""])[0],
                                     q_.get("err", [""])[0])),
    (re.compile(r"^/librarian/(\d+)$"),
     lambda conn, m, q_: view_librarian(conn, int(m.group(1)), q_.get("msg", [""])[0],
                                        q_.get("err", [""])[0])),
]

POST_ROUTES = [
    (re.compile(r"^/record/submit$"), act_submit_record),
    (re.compile(r"^/record/edit$"), act_edit_record),
    (re.compile(r"^/verify$"), act_verify),
    (re.compile(r"^/achievement/review$"), act_review_achievement),
    (re.compile(r"^/sync$"), act_sync),
    (re.compile(r"^/restock$"), act_restock),
    (re.compile(r"^/claim$"), act_claim),
    (re.compile(r"^/activity/create$"), act_create_activity),
    (re.compile(r"^/booklist/replace$"), act_replace_booklist),
    (re.compile(r"^/book/create$"), act_create_book),
]


def make_handler(db_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body: bytes, status=200, ctype="text/html; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location):
            self.send_response(303)
            self.send_header("Location", location)
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path.startswith("/covers/"):
                return self._serve_cover(parsed.path)
            query = parse_qs(parsed.query)
            conn = connect(db_path)
            try:
                for rx, fn in GET_ROUTES:
                    m = rx.match(parsed.path)
                    if m:
                        return self._send(fn(conn, m, query))
                self._send(page("404", "<p>页面不存在</p>"), status=404)
            finally:
                conn.close()

        def do_POST(self):
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8")
            form = {k: v[0] if len(v) == 1 else v
                    for k, v in parse_qs(raw).items()}
            # 多选字段保持列表
            multi = parse_qs(raw)
            if "book_id" in multi and parsed.path == "/booklist/replace":
                form["book_id"] = multi["book_id"]
            nxt = form.get("next", "/")
            conn = connect(db_path)
            try:
                for rx, fn in POST_ROUTES:
                    if rx.match(parsed.path):
                        try:
                            msg = fn(conn, form)
                            sep = "&" if "?" in nxt else "?"
                            return self._redirect(f"{nxt}{sep}msg={quote(str(msg))}")
                        except svc.DomainError as e:
                            sep = "&" if "?" in nxt else "?"
                            return self._redirect(f"{nxt}{sep}err={quote(str(e))}")
                self._send(page("404", "<p>接口不存在</p>"), status=404)
            finally:
                conn.close()

        def _serve_cover(self, path):
            name = os.path.basename(path)
            full = os.path.join(COVERS_DIR, name)
            if not os.path.isfile(full):
                return self._send(b"not found", status=404, ctype="text/plain")
            with open(full, "rb") as f:
                self._send(f.read(), ctype="image/svg+xml")

    return Handler


def make_server(db_path, port=8000):
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(db_path))
