"""HTTP 层：门岗不能审批、护士跨病区被拒、门岗大字页遮挡与全文拒绝原因。"""
import json
import threading
import urllib.parse
import urllib.request
import urllib.error
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from app import core
from app.server import Handler
from tests.helpers import WardTestBase


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


class _Browser:
    """带 Cookie 的极简 HTTP 客户端（不跟随重定向，303 原样返回）。"""
    def __init__(self, base):
        self.base = base
        self.cj = CookieJar()
        self.opener = urllib.request.build_opener(
            _NoRedirect, urllib.request.HTTPCookieProcessor(self.cj))

    def get(self, path):
        return self._open(self.base + path)

    def post(self, path, data: dict):
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        return self._open(req)

    def _open(self, what):
        try:
            r = self.opener.open(what)
            return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")


import urllib.parse  # noqa: E402


class TestHttp(WardTestBase):
    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.srv.db_path = self.db_path
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def _login(self, br, kind, ident):
        br.post("/session", {"kind": kind, "staff_id": ident, "gate_code": ident})

    def test_gate_cannot_approve_personnel(self):
        """门岗会话调用任何护士写接口 → 403，页面明示无审批权。"""
        pid = self.admit()
        app_id = core.create_application_txn(
            self.conn, staff_id=self.s["hn_head"], patient_id=pid,
            companion_name="x", relationship="x", id_type="身份证",
            id_number="11010519900101001X", phone="13800000001",
            window_start="08:00", window_end="20:00")
        br = _Browser(self.base)
        self._login(br, "gate", "HN-G1")
        status, body = br.post("/nurse/review",
                               {"application_id": app_id, "decision": "approve"})
        self.assertEqual(status, 403)
        self.assertIn("门岗仅有核验权限", body)
        # 撤销也不行：先由护士长把申请批成有效码
        issued = core.review_application(self.conn, staff_id=self.s["hn_head"],
                                         application_id=app_id, approve=True)
        status2, body2 = br.post("/nurse/revoke",
                                 {"pass_id": issued["pass_id"], "reason": core.REASON_PHONE_LOST})
        self.assertEqual(status2, 403)

    def test_gate_page_big_text_and_masking(self):
        _, _, code = self.issue(companion="周晓燕")
        br = _Browser(self.base)
        self._login(br, "gate", "HN-G1")
        # 手输码默认大写化在 core 内；页面 POST 原文
        status, body = br.post("/gate/scan", {"code": code.lower()})
        self.assertEqual(status, 200)
        self.assertIn("核验通过", body)
        self.assertIn("T-HN-", body)          # 患者代号大字
        self.assertIn("HN 病区", body)
        self.assertIn("周*燕", body)           # 姓名默认遮挡
        self.assertNotIn(">周晓燕<", body)

    def test_gate_page_full_deny_reason(self):
        _, pass_id, code = self.issue(companion="陈晨")
        core.revoke_pass(self.conn, staff_id=self.s["hn_head"], pass_id=pass_id,
                         reason=core.REASON_PHONE_LOST)
        br = _Browser(self.base)
        self._login(br, "gate", "HN-G1")
        status, body = br.post("/gate/scan", {"code": code})
        self.assertEqual(status, 200)
        self.assertIn("禁止通行", body)
        self.assertIn("手机丢失", body)        # 完整文字原因，非错误码
        self.assertNotIn("REVOKED", body)      # 不拿内部代码糊弄人

    def test_cross_ward_nurse_http_denied(self):
        pid = self.admit(ward="XH", bed="XH-01")
        app_id = core.create_application_txn(
            self.conn, staff_id=self.s["xh_head"], patient_id=pid,
            companion_name="x", relationship="x", id_type="身份证",
            id_number="11010519900101001X", phone="13800000001",
            window_start="08:00", window_end="20:00")
        br = _Browser(self.base)
        self._login(br, "staff", str(self.s["hn_head"]))
        status, body = br.post("/nurse/review",
                               {"application_id": app_id, "decision": "approve"})
        self.assertEqual(status, 409)
        self.assertIn("跨病区", body)

    def test_nurse_view_organized_by_bed(self):
        self.issue()
        br = _Browser(self.base)
        self._login(br, "staff", str(self.s["hn_head"]))
        status, body = br.get("/nurse")
        self.assertEqual(status, 200)
        self.assertIn("01 床", body)
        self.assertIn("床位视图", body)

    def test_discharge_flow_over_http(self):
        pid = self.admit()
        _, pass_id, code = self.issue(patient_id=pid)
        br = _Browser(self.base)
        self._login(br, "staff", str(self.s["hn_head"]))
        status, _ = br.post("/nurse/discharge", {"patient_id": pid})
        self.assertEqual(status, 303)
        # 门岗再扫 → 拒绝
        g = _Browser(self.base)
        self._login(g, "gate", "HN-G1")
        _, body = g.post("/gate/scan", {"code": code})
        self.assertIn("出院", body)

    def test_handoff_board_shows_transfer_chain(self):
        """转科后交接看板展示旧→新链路；默认遮罩人名、按本病区过滤。"""
        pid = self.admit()
        app_id, _, old_code = self.issue(patient_id=pid, companion="林芳")
        core.transfer_patient(self.conn, staff_id=self.s["hn_head"], patient_id=pid,
                              to_ward="XH", to_bed_id=self.bed["XH-01"])
        core.reapply_after_transfer(
            self.conn, staff_id=self.s["xh_head"], patient_id=pid,
            source_application_id=app_id, new_fields={})
        # 新病区护士长可见链路，姓名默认遮挡
        br = _Browser(self.base)
        self._login(br, "staff", str(self.s["xh_head"]))
        status, body = br.get("/handoff")
        self.assertEqual(status, 200)
        self.assertIn("转科授权链", body)
        self.assertIn("HN", body)
        self.assertIn("XH", body)
        self.assertIn("授权链已承接", body)
        self.assertIn("林*", body)
        self.assertNotIn(">林芳<", body)
        # 旧病区护士长也可见（转出链路）
        br2 = _Browser(self.base)
        self._login(br2, "staff", str(self.s["hn_head"]))
        s2, body2 = br2.get("/handoff")
        self.assertEqual(s2, 200)
        self.assertIn("林*", body2)
        # 门岗无权访问交接看板
        g0 = _Browser(self.base)
        self._login(g0, "gate", "HN-G1")
        s3, body3 = g0.get("/handoff")
        self.assertEqual(s3, 403)
        self.assertIn("门岗无此权限", body3)
        # 旧码在旧门岗仍被拒
        g = _Browser(self.base)
        self._login(g, "gate", "HN-G1")
        _, gb = g.post("/gate/scan", {"code": old_code})
        self.assertIn("转科", gb)

    def test_apply_permission_boundary_over_http(self):
        """普通护士可登记申请但审批 409；护士长审批 303。"""
        pid = self.admit()
        br = _Browser(self.base)
        self._login(br, "staff", str(self.s["hn_nurse"]))
        status, _ = br.post("/nurse/apply", {
            "patient_id": pid, "companion_name": "护工甲", "relationship": "护工",
            "id_type": "身份证", "id_number": "11010519660101999X",
            "phone": "13711112222", "window_start": "08:00", "window_end": "20:00",
            "quota": "1"})
        self.assertEqual(status, 303)
        app_id = self.conn.execute(
            "SELECT id FROM applications WHERE companion_name='护工甲'").fetchone()[0]
        status2, body = br.post("/nurse/review",
                                {"application_id": app_id, "decision": "approve"})
        self.assertEqual(status2, 409)
        self.assertIn("护士长", body)

    def test_reject_without_reason_over_http(self):
        pid = self.admit()
        app_id = core.create_application_txn(
            self.conn, staff_id=self.s["hn_head"], patient_id=pid,
            companion_name="x", relationship="x", id_type="身份证",
            id_number="11010519900101001X", phone="13800000001",
            window_start="08:00", window_end="20:00")
        br = _Browser(self.base)
        self._login(br, "staff", str(self.s["hn_head"]))
        status, body = br.post("/nurse/review",
                               {"application_id": app_id, "decision": "reject", "reason": ""})
        self.assertEqual(status, 409)
        self.assertIn("拒绝原因", body)
