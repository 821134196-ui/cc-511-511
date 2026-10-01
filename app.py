#!/usr/bin/env python3
"""本地启动入口：初始化数据库（首次自动灌入含转科、丢码样例的演示数据），
然后启动 Web 服务。用法：

    python3 app.py            # 启动，默认 8000 端口
    python3 app.py --port 8080
    python3 app.py --reseed   # 清空并重新生成演示数据
"""
import os
import sys

from wardpass import db, seed, web


def main():
    port = 8000
    reseed = "--reseed" in sys.argv
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    if reseed and os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(db.DB_PATH + suffix):
                os.remove(db.DB_PATH + suffix)

    conn = db.connect()
    try:
        db.init_db(conn)
        empty = conn.execute("SELECT COUNT(*) c FROM wards").fetchone()["c"] == 0
        if empty:
            print("首次启动，正在生成演示数据（含转科样例、丢码样例）……")
            demo = seed.seed_all(conn)
            print(f"演示数据就绪。丢码样例通行码：{demo['lost_code']}（已被撤销，扫码将被拦截）")
    finally:
        conn.close()

    web.serve(port)


if __name__ == "__main__":
    main()
