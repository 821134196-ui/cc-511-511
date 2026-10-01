"""时段边界 + 限制时段配额测试。"""
from app import core
from tests.helpers import WardTestBase


class TestTimeWindow(WardTestBase):
    def test_boundaries_0800_and_2000(self):
        """左闭右开：07:59 拒，08:00 准，19:59 准，20:00 拒。每边界独立新码。"""
        cases = [
            (7, 59, False, "OUTSIDE_WINDOW"),
            (8, 0, True, "OK"),
            (19, 59, True, "OK"),
            (20, 0, False, "OUTSIDE_WINDOW"),
            (20, 1, False, "OUTSIDE_WINDOW"),
        ]
        for hh, mm, allowed, code_key in cases:
            _, _, code = self.issue(ws="08:00", we="20:00")
            r = core.scan_pass(self.conn, code, gate_code="HN-G1", clock=self.clock(hh, mm))
            self.assertIs(r["allowed"], allowed, f"{hh:02d}:{mm:02d} -> {r}")
            self.assertEqual(r["reason_code"], code_key)

    def test_denied_reason_is_full_text(self):
        _, _, code = self.issue(ws="08:00", we="20:00")
        r = core.scan_pass(self.conn, code, gate_code="HN-G1", clock=self.clock(6, 30))
        self.assertFalse(r["allowed"])
        self.assertIn("06:30", r["reason"])
        self.assertIn("08:00–20:00", r["reason"])

    def test_quota_blocks_overissue(self):
        """限制时段配额=1：一份有效申请/码在，第二份不得登记。"""
        pid = self.admit()
        self.issue(patient_id=pid)
        with self.assertRaises(core.RuleError) as cm:
            core.create_application_txn(
                self.conn, staff_id=self.s["hn_head"], patient_id=pid,
                companion_name="陪护乙", relationship="配偶", id_type="身份证",
                id_number="11010519900202002X", phone="13800000002",
                window_start="08:00", window_end="20:00", quota=1)
        self.assertIn("配额", str(cm.exception))

    def test_quota_two_allows_two(self):
        pid = self.admit()
        base = dict(staff_id=self.s["hn_head"], patient_id=pid,
                    id_type="身份证", window_start="08:00", window_end="20:00", quota=2)
        a1 = core.create_application_txn(
            self.conn,
            companion_name="陪护甲", relationship="子女",
            id_number="11010519900101001X",
            phone="13800000001", **base)
        a2 = core.create_application_txn(
            self.conn,
            companion_name="陪护乙", relationship="子女",
            id_number="11010519900202002X",
            phone="13800000002", **base)
        core.review_application(self.conn, staff_id=self.s["hn_head"],
                                application_id=a1, approve=True)
        core.review_application(self.conn, staff_id=self.s["hn_head"],
                                application_id=a2, approve=True)
        self.assertEqual(core.active_pass_count(self.conn, pid, "HN"), 2)
        # 第三个仍被挡
        with self.assertRaises(core.RuleError):
            core.create_application_txn(
                self.conn,
                companion_name="陪护丙", id_number="11010519900303003X",
                phone="13800000003", relationship="亲属", **base)

    def test_invalid_window_rejected(self):
        pid = self.admit()
        for ws, we in [("20:00", "08:00"), ("08:00", "08:00"), ("25:00", "09:00")]:
            with self.assertRaises(core.RuleError):
                core.create_application_txn(
                    self.conn, staff_id=self.s["hn_head"], patient_id=pid,
                    companion_name="x", relationship="x", id_type="身份证",
                    id_number="11010519900101001X", phone="13800000001",
                    window_start=ws, window_end=we, quota=1)

    def test_reject_requires_reason(self):
        pid = self.admit()
        app_id = core.create_application_txn(
            self.conn, staff_id=self.s["hn_head"], patient_id=pid,
            companion_name="x", relationship="x", id_type="身份证",
            id_number="11010519900101001X", phone="13800000001",
            window_start="08:00", window_end="20:00")
        with self.assertRaises(core.RuleError):
            core.review_application(self.conn, staff_id=self.s["hn_head"],
                                    application_id=app_id, approve=False)
