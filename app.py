#!/usr/bin/env python3
"""图书馆阅读挑战系统 —— 一条命令启动：

    python3 app.py            # 首次自动建库并灌入演示数据（含重复书目案例）
    python3 app.py --reset    # 重建演示数据
    python3 app.py --port 8080

然后打开 http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import os
import sys

from reading.db import connect, init_db
from reading.seed import seed
from reading.web import make_server

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "reading.db")


def main():
    ap = argparse.ArgumentParser(description="图书馆阅读挑战系统")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--reset", action="store_true", help="删除现有数据并重新灌入演示数据")
    args = ap.parse_args()

    fresh = args.reset or not os.path.exists(args.db)
    if args.reset and os.path.exists(args.db):
        os.remove(args.db)
    os.makedirs(os.path.dirname(args.db), exist_ok=True)

    if fresh:
        init_db(args.db)
        conn = connect(args.db)
        try:
            ids = seed(conn)
        finally:
            conn.close()
        print("已灌入演示数据：")
        print("  · 三个年龄段活动（萌芽4-6 / 成长7-9 / 探索10-12），成长挑战中途换过书目")
        print("  · 小明：2 本已核验 + 1 本待核验 + 1 条【重复提交】+ 1 条【补录·借阅晚同步】")
        print("  · 小雨：3 本已核验，待达标审核（成长纪念章库存仅 1 枚 → 可演示候补）")
        print("  · 馆员陈静是小雨的家长 → 演示审核回避")
    else:
        init_db(args.db)  # 确保 schema 存在

    server = make_server(args.db, args.port)
    print(f"\n服务已启动: http://127.0.0.1:{args.port}")
    print("入口：孩子端（封面+进度条）/ 家长端（待核验项）/ 馆员端（队列·库存·配置）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    sys.exit(main())
