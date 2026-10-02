"""python -m app [port] —— 一条命令：建库 + 种子 + 启动。"""
import sys
from pathlib import Path

from .seed import seed
from .server import serve

if __name__ == "__main__":
    if not Path("data/library.db").exists():
        seed()
        print("✓ 已初始化数据库与演示数据（含重复书目案例）")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    serve(port=port)
