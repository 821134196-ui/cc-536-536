"""零依赖 HTTP 服务：JSON API + 静态前端。"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .adapters import LoanAdapter, ReminderAdapter
from .db import connect
from .service import ActivityService, ChallengeService, DomainError

WEB_ROOT = Path(__file__).parent / "web"
DB_PATH = "data/library.db"

_local = threading.local()


def get_conn():
    """每个工作线程一个独立连接（独立事务，发章靠 BEGIN IMMEDIATE 串行竞争）。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = connect(DB_PATH)
        _local.conn = conn
    return conn


def row(r):
    return dict(r) if r is not None else None


def rows(rs):
    return [dict(r) for r in rs]


class Handler(BaseHTTPRequestHandler):
    server_version = "ReadingChallenge/1.0"

    # ---------- 基础
    def _send(self, code: int, payload, ctype: str = "application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or ctype == "application/json" else ""))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

    def log_message(self, fmt, *args):
        pass  # 安静一点

    # ---------- 路由
    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            if p == "/" or p == "/index.html":
                return self._send(200, (WEB_ROOT / "index.html").read_bytes(), "text/html")
            if p.startswith("/static/"):
                return self._static(p)
            if p == "/api/activities":
                return self._activities()
            if p == "/api/people":
                return self._people()
            if p == "/api/children":
                return self._children(q)
            if p == "/api/progress":
                return self._progress(int(q["enrollment_id"][0]))
            if p == "/api/queue":
                return self._queue(int(q["activity_id"][0]))
            if p == "/api/versions":
                return self._versions(int(q["record_id"][0]))
            if p == "/api/notifications":
                return self._notifications(int(q["uid"][0]))
            if p == "/api/enrollments":
                return self._enrollments(q)
            self._send(404, {"error": "not found"})
        except DomainError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path.rstrip("/")
        try:
            body = self._json_body()
            conn = get_conn()
            act = ActivityService(conn)
            chl = ChallengeService(conn)
            m = re.fullmatch(r"/api/records/(\d+)/(submit|verify|reject)", p)
            if p == "/api/activities":
                aid = act.create_activity(
                    body["name"], int(body["age_min"]), int(body["age_max"]),
                    body["starts_on"], body["ends_on"],
                    int(body.get("goal_books", 3)), int(body.get("loan_days", 14)),
                    int(body.get("medal_stock", 0)),
                )
                for bid in body.get("book_ids", []):
                    act.attach_book(aid, int(bid))
                return self._send(200, {"id": aid})
            if p == "/api/books":
                bid = act.add_book(body["title"], body.get("author", ""), body.get("isbn", ""))
                return self._send(200, {"id": bid})
            if p == "/api/catalog":
                act.change_catalog(int(body["activity_id"]), int(body["book_id"]), body["action"])
                return self._send(200, {"ok": True})
            if p == "/api/enroll":
                eid = chl.enroll(int(body["activity_id"]), int(body["child_id"]), int(body["uid"]))
                return self._send(200, {"enrollment_id": eid})
            if p == "/api/records":
                rid = chl.upsert_record(
                    int(body["enrollment_id"]), int(body["book_id"]), int(body["uid"]),
                    body.get("note", ""), body.get("read_on"),
                )
                return self._send(200, {"record_id": rid})
            if m:
                rid, action = int(m.group(1)), m.group(2)
                if action == "submit":
                    chl.submit_record(rid, int(body["uid"]))
                elif action == "verify":
                    chl.verify_record(rid, int(body["uid"]))
                else:
                    chl.reject_record(rid, int(body["uid"]), body.get("comment", ""))
                return self._send(200, {"ok": True})
            if p == "/api/loans":
                lid = LoanAdapter(conn, int(body.get("delay_seconds", 45))).record_loan(
                    child_id=int(body["child_id"]), book_id=int(body["book_id"]),
                    activity_id=int(body["activity_id"]),
                    due_days=int(body.get("due_days", 14)),
                )
                return self._send(200, {"loan_id": lid})
            if p == "/api/loans/sync":
                n = chl.sync_loans()
                return self._send(200, {"synced": n})
            if p == "/api/approve":
                return self._send(200, chl.approve_and_grant(int(body["enrollment_id"]), int(body["uid"])))
            if p == "/api/restock":
                ids = chl.restock(int(body["activity_id"]), int(body["amount"]))
                return self._send(200, {"promoted": ids})
            if p == "/api/pickup":
                return self._send(200, chl.pickup(body["code"]))
            self._send(404, {"error": "not found"})
        except DomainError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _static(self, p: str):
        rel = p.removeprefix("/static/").replace("..", "")
        fp = (WEB_ROOT / "static" / rel).resolve()
        if not str(fp).startswith(str((WEB_ROOT / "static").resolve())) or not fp.is_file():
            return self._send(404, {"error": "not found"})
        ctype = "image/svg+xml" if fp.suffix == ".svg" else "application/octet-stream"
        self._send(200, fp.read_bytes(), ctype)

    # ---------- GET 处理
    def _activities(self):
        conn = get_conn()
        out = []
        for a in conn.execute("SELECT * FROM activities ORDER BY age_min"):
            d = dict(a)
            d["books"] = rows(conn.execute(
                """SELECT b.*, ab.activity_id FROM books b
                   JOIN activity_books ab ON ab.book_id=b.id WHERE ab.activity_id=?""",
                (a["id"],)))
            out.append(d)
        self._send(200, out)

    def _people(self):
        conn = get_conn()
        self._send(200, {
            "parents": rows(conn.execute("SELECT * FROM users WHERE role='parent' ORDER BY id")),
            "librarians": rows(conn.execute("SELECT * FROM users WHERE role='librarian' ORDER BY id")),
        })

    def _children(self, q):
        conn = get_conn()
        if "parent_id" in q:
            rs = conn.execute("SELECT * FROM children WHERE parent_id=? ORDER BY id",
                              (int(q["parent_id"][0]),)).fetchall()
        else:
            rs = conn.execute("SELECT * FROM children ORDER BY id").fetchall()
        self._send(200, rows(rs))

    def _enrollments(self, q):
        conn = get_conn()
        sql = "SELECT * FROM enrollments"
        args = ()
        if "child_id" in q:
            sql += " WHERE child_id=?"
            args = (int(q["child_id"][0]),)
        elif "parent_id" in q:
            sql += " WHERE parent_id=?"
            args = (int(q["parent_id"][0]),)
        self._send(200, rows(conn.execute(sql, args)))

    def _progress(self, enrollment_id: int):
        conn = get_conn()
        chl = ChallengeService(conn)
        prog = chl.progress(enrollment_id)
        d = {
            "goal": prog["goal"],
            "verified_count": prog["verified_count"],
            "qualified": prog["qualified"],
            "activity": row(prog["activity"]),
            "enrollment": row(prog["enrollment"]),
            "records": [],
        }
        for r in prog["records"]:
            rd = dict(r)
            rd["loan_synced"] = bool(rd.get("loan_synced"))
            rd["in_catalog"] = bool(rd.get("in_catalog"))
            d["records"].append(rd)
        m = chl.medal_status(enrollment_id)
        d["medal"] = row(m) if m else None
        self._send(200, d)

    def _queue(self, activity_id: int):
        conn = get_conn()
        chl = ChallengeService(conn)
        queue = chl.librarian_queue(activity_id)
        out = []
        for item in queue:
            recs = []
            for r in item["records"]:
                rd = dict(r)
                rd["loan_synced"] = bool(r["loan_synced"])
                recs.append(rd)
            books = {b["id"]: dict(b) for b in conn.execute("SELECT * FROM books")}
            out.append({
                "enrollment": dict(item["enrollment"]),
                "child": dict(item["child"]),
                "records": recs,
                "submitted_ids": [r["id"] for r in item["submitted"]],
                "borrowed_only": [books[i] for i in item["borrowed_only"] if i in books],
            })
        act = conn.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
        wait = conn.execute(
            """SELECT w.position, m.enrollment_id, c.name AS child_name, m.qualified_at
               FROM waitlist w JOIN medals m ON m.id=w.medal_id
               JOIN enrollments e ON e.id=m.enrollment_id JOIN children c ON c.id=e.child_id
               WHERE m.activity_id=? ORDER BY w.position""",
            (activity_id,),
        ).fetchall()
        self._send(200, {"activity": dict(act), "queue": out, "waitlist": rows(wait)})

    def _versions(self, record_id: int):
        conn = get_conn()
        chl = ChallengeService(conn)
        vs = chl.record_versions(record_id)
        out = []
        for v in vs:
            d = dict(v)
            u = conn.execute("SELECT name FROM users WHERE id=?", (v["changed_by"],)).fetchone()
            d["changed_by_name"] = u["name"] if u else "?"
            out.append(d)
        self._send(200, out)

    def _notifications(self, uid: int):
        self._send(200, rows(ReminderAdapter(get_conn()).list_for(uid, 30)))


def serve(host: str = "127.0.0.1", port: int = 8000):
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"📚 阅读挑战已启动：http://{host}:{port}")
    print("   孩子端看封面与进度条 · 家长端报名/补录 · 馆员端核验/发章/候补")
    httpd.serve_forever()


if __name__ == "__main__":
    serve()
