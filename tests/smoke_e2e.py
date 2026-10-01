"""端到端冒烟脚本：启动服务器后校验全部交付场景。"""
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from app.server import Handler

PASS = FAIL = 0


def check(name, got, want, status=None, got_status=None):
    global PASS, FAIL
    ok = want in got and (status is None or status == got_status)
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"  (want {want!r}/{status})"))
    PASS += ok
    FAIL += not ok


class Browser:
    def __init__(self, base):
        self.cj = CookieJar()

        class NoRedir(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        self.op = urllib.request.build_opener(NoRedir, urllib.request.HTTPCookieProcessor(self.cj))
        self.base = base

    def req(self, path, data=None):
        url = self.base + path
        if data is not None:
            r = urllib.request.Request(url, urllib.parse.urlencode(data).encode(), method="POST")
        else:
            r = urllib.request.Request(url)
        try:
            resp = self.op.open(r)
            return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()


def main():
    import tempfile, os
    from app.seed import seed
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "smoke.db")
    demo = seed(db)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.db_path = db
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    time.sleep(0.2)

    check("首页身份选择", Browser(base).req("/")[1], "请选择工作身份")

    g = Browser(base)
    g.req("/session", {"kind": "gate", "gate_code": "HN-G1"})
    s, b = g.req("/gate/scan", {"code": demo["zhou_active"]["code"]})
    check("有效码入区大字结果", b, "核验通过，允许入区")
    check("门岗大字代号", b, "A-0217")
    check("姓名默认遮挡", b, "周*燕")
    check("闸机绿灯", b, "OPEN · GREEN")
    s2, b2 = g.req("/gate/scan", {"code": demo["zhou_active"]["code"]})
    check("重放窗口二次扫描拒绝", b2, "只允许一次入区状态变更")
    check("跨病区拒绝全文", g.req("/gate/scan", {"code": demo["zheng_cross_ward"]["code"]})[1],
          "不能在 HN 病区使用")
    check("手机丢码撤销全文", g.req("/gate/scan", {"code": demo["chen_lost_phone"]["code"]})[1],
          "手机丢失")
    check("转科旧码拒绝全文", g.req("/gate/scan", {"code": demo["lin_transfer"]["old_code"]})[1],
          "患者转科")
    sg, bg = g.req("/nurse/review", {"application_id": 1, "decision": "approve"})
    check("门岗审批403", bg, "门岗仅有核验权限", 403, sg)
    s403, b403 = g.req("/handoff")
    check("门岗交接看板403", b403, "门岗无此权限", 403, s403)

    gx = Browser(base)
    gx.req("/session", {"kind": "gate", "gate_code": "XH-G1"})
    check("转科新码新病区通过",
          gx.req("/gate/scan", {"code": demo["lin_transfer"]["new_code"]})[1],
          "核验通过，允许入区")
    check("HN码在XH门岗拒绝",
          gx.req("/gate/scan", {"code": demo["zhou_active"]["code"]})[1], "属于 HN 病区")

    import sqlite3
    c = sqlite3.connect(db)
    hn_id = c.execute("SELECT id FROM staff WHERE username='wangl'").fetchone()[0]
    xh_id = c.execute("SELECT id FROM staff WHERE username='zhangmin'").fetchone()[0]
    n_id = c.execute("SELECT id FROM staff WHERE username='liqiang'").fetchone()[0]
    pending = c.execute("SELECT id FROM applications WHERE status='pending'").fetchone()[0]

    hn = Browser(base)
    hn.req("/session", {"kind": "staff", "staff_id": str(hn_id)})
    check("护士按床位组织", hn.req("/nurse")[1], "床位视图")
    check("待审批清单", hn.req("/nurse")[1], "待护士长审批")
    check("交接看板转科链", hn.req("/handoff")[1], "授权链已承接")
    check("交接看板默认遮罩", hn.req("/handoff")[1], "林*斌")
    sr, _ = hn.req("/nurse/review", {"application_id": pending, "decision": "approve"})
    check("护士长审批303", "", "", 303, sr)

    # 普通护士：可登记，自审必须 409
    nr = Browser(base)
    nr.req("/session", {"kind": "staff", "staff_id": str(n_id)})
    pid = c.execute("SELECT id FROM patients WHERE alias_code='A-0522'").fetchone()[0]
    nr.req("/nurse/apply", {"patient_id": pid, "companion_name": "临时护工", "relationship": "护工",
                            "id_type": "身份证", "id_number": "11010519660101999X",
                            "phone": "13711112222", "window_start": "08:00", "window_end": "20:00",
                            "quota": "1"})
    new_app = c.execute("SELECT id FROM applications WHERE companion_name='临时护工'").fetchone()[0]
    s409, b409 = nr.req("/nurse/review", {"application_id": new_app, "decision": "approve"})
    check("普通护士审批409", b409, "护士长", 409, s409)

    # XH 护士长不能动 HN 的申请（跨病区隔离）
    xh = Browser(base)
    xh.req("/session", {"kind": "staff", "staff_id": str(xh_id)})
    sx, bx = xh.req("/nurse/review", {"application_id": new_app, "decision": "approve"})
    check("跨病区护士长审批409", bx, "跨病区", 409, sx)

    srv.shutdown()
    print(f"==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
