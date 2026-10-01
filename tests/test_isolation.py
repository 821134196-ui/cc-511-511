"""跨病区隔离 + 门岗/护士角色边界。"""
from app import core
from tests.helpers import WardTestBase


class TestIsolation(WardTestBase):
    def test_xh_pass_denied_at_hn_gate(self):
        """XH 病区的码在 HN 门岗扫：拒绝，且给出完整指引文字。"""
        _, _, code = self.issue(staff="xh_head", ward="XH", bed="XH-01")
        r = core.scan_pass(self.conn, code, gate_code="HN-G1", clock=self.clock(9, 0))
        self.assertFalse(r["allowed"])
        self.assertEqual(r["reason_code"], "CROSS_WARD")
        self.assertIn("XH", r["reason"])
        self.assertIn("HN", r["reason"])
        self.assertIn("重新申请", r["reason"])

    def test_hn_pass_works_at_both_hn_gates(self):
        _, _, code = self.issue()
        r = core.scan_pass(self.conn, code, gate_code="HN-G1", clock=self.clock(9, 0))
        self.assertTrue(r["allowed"])
        # 重放窗口外另一门岗扫 → 正常离区
        r2 = core.scan_pass(self.conn, code, gate_code="HN-G2", clock=self.clock(9, 1))
        self.assertTrue(r2["allowed"])
        self.assertEqual(r2["action"], "exit")

    def test_nurse_cannot_touch_other_ward_application(self):
        """HN 护士登记 XH 患者申请 → 拒绝。"""
        pid = self.admit(ward="XH", bed="XH-01")
        with self.assertRaises(core.RuleError) as cm:
            core.create_application_txn(
                self.conn, staff_id=self.s["hn_nurse"], patient_id=pid,
                companion_name="x", relationship="x", id_type="身份证",
                id_number="11010519900101001X", phone="13800000001",
                window_start="08:00", window_end="20:00")
        self.assertIn("跨病区", str(cm.exception))

    def test_nurse_cannot_review_other_ward(self):
        pid = self.admit(ward="XH", bed="XH-01")
        app_id = core.create_application_txn(
            self.conn, staff_id=self.s["xh_head"], patient_id=pid,
            companion_name="x", relationship="x", id_type="身份证",
            id_number="11010519900101001X", phone="13800000001",
            window_start="08:00", window_end="20:00")
        with self.assertRaises(core.RuleError):
            core.review_application(self.conn, staff_id=self.s["hn_head"],
                                    application_id=app_id, approve=True)

    def test_nurse_cannot_revoke_other_ward_pass(self):
        _, pass_id, _ = self.issue(staff="xh_head", ward="XH", bed="XH-01")
        with self.assertRaises(core.RuleError):
            core.revoke_pass(self.conn, staff_id=self.s["hn_head"], pass_id=pass_id,
                             reason=core.REASON_MANUAL)

    def test_regular_nurse_can_register_but_not_review(self):
        """普通护士可在本病区登记申请；审批发码只属于护士长。"""
        pid = self.admit()
        app_id = core.create_application_txn(
            self.conn, staff_id=self.s["hn_nurse"], patient_id=pid,
            companion_name="x", relationship="x", id_type="身份证",
            id_number="11010519900101001X", phone="13800000001",
            window_start="08:00", window_end="20:00")
        with self.assertRaises(core.RuleError) as cm:
            core.review_application(self.conn, staff_id=self.s["hn_nurse"],
                                    application_id=app_id, approve=True)
        self.assertIn("护士长", str(cm.exception))
        # 本病区护士长可以批
        r = core.review_application(self.conn, staff_id=self.s["hn_head"],
                                    application_id=app_id, approve=True)
        self.assertTrue(r["code_plain"].startswith("WXK-"))

    def test_regular_nurse_can_urgent_revoke_in_own_ward(self):
        """紧急撤销（手机丢失/码泄露）本病区护士可即时执行，但不能碰别病区。"""
        _, pass_id, _ = self.issue()
        core.revoke_pass(self.conn, staff_id=self.s["hn_nurse"], pass_id=pass_id,
                         reason=core.REASON_PHONE_LOST)
        _, pass_id2, _ = self.issue(staff="xh_head", ward="XH", bed="XH-01")
        with self.assertRaises(core.RuleError):
            core.revoke_pass(self.conn, staff_id=self.s["hn_nurse"], pass_id=pass_id2,
                             reason=core.REASON_CODE_LEAKED)

    def test_unknown_gate_denied(self):
        _, _, code = self.issue()
        r = core.scan_pass(self.conn, code, gate_code="ZZ-G9", clock=self.clock(9, 0))
        self.assertFalse(r["allowed"])
        self.assertEqual(r["reason_code"], "UNKNOWN_GATE")
