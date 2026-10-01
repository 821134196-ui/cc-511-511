"""撤销触发与转科交接：换陪护 / 手机丢失 / 码泄露 / 封控 / 出院 / 转科重评。"""
from app import core
from tests.helpers import WardTestBase


class TestRevocation(WardTestBase):
    def test_phone_lost_revokes_immediately(self):
        _, pass_id, code = self.issue()
        core.revoke_pass(self.conn, staff_id=self.s["hn_head"], pass_id=pass_id,
                         reason=core.REASON_PHONE_LOST)
        r = core.scan_pass(self.conn, code, gate_code="HN-G1", clock=self.clock(9, 0))
        self.assertFalse(r["allowed"])
        self.assertEqual(r["reason_code"], "REVOKED")
        self.assertIn("手机丢失", r["reason"])

    def test_code_leak_revokes_immediately(self):
        _, pass_id, code = self.issue()
        core.revoke_pass(self.conn, staff_id=self.s["hn_nurse"], pass_id=pass_id,
                         reason=core.REASON_CODE_LEAKED)
        r = core.scan_pass(self.conn, code, gate_code="HN-G1", clock=self.clock(9, 0))
        self.assertFalse(r["allowed"])
        self.assertIn("泄露", r["reason"])

    def test_double_revoke_is_error(self):
        _, pass_id, _ = self.issue()
        core.revoke_pass(self.conn, staff_id=self.s["hn_head"], pass_id=pass_id,
                         reason=core.REASON_PHONE_LOST)
        with self.assertRaises(core.RuleError):
            core.revoke_pass(self.conn, staff_id=self.s["hn_head"], pass_id=pass_id,
                             reason=core.REASON_MANUAL)

    def test_lockdown_revokes_all_and_gate_denies(self):
        pid1 = self.admit(alias="L-1")
        pid2 = self.admit(alias="L-2", bed="HN-02")
        _, _, code1 = self.issue(patient_id=pid1)
        _, _, code2 = self.issue(patient_id=pid2)
        n = core.lockdown_ward(self.conn, staff_id=self.s["hn_head"], ward_code="HN")
        self.assertEqual(n, 2)
        for code in (code1, code2):
            r = core.scan_pass(self.conn, code, gate_code="HN-G1", clock=self.clock(9, 0))
            self.assertFalse(r["allowed"])
            self.assertEqual(r["reason_code"], "LOCKDOWN")
            self.assertIn("封控", r["reason"])
        # 解锁后旧码不复活
        core.unlock_ward(self.conn, staff_id=self.s["hn_head"], ward_code="HN")
        r = core.scan_pass(self.conn, code1, gate_code="HN-G1", clock=self.clock(9, 0))
        self.assertFalse(r["allowed"])
        self.assertEqual(r["reason_code"], "REVOKED")

    def test_discharge_revokes_all_passes_across_wards(self):
        """出院撤销：患者所有病区（若曾转科遗留）的有效码全部失效。"""
        pid = self.admit()
        _, pass_id, code = self.issue(patient_id=pid)
        n = core.discharge_patient(self.conn, staff_id=self.s["hn_head"], patient_id=pid)
        self.assertEqual(n, 1)
        r = core.scan_pass(self.conn, code, gate_code="HN-G1", clock=self.clock(9, 0))
        self.assertFalse(r["allowed"])
        self.assertIn("出院", r["reason"])
        # 出院后不可再申请
        with self.assertRaises(core.RuleError) as cm:
            core.create_application_txn(
                self.conn, staff_id=self.s["hn_head"], patient_id=pid,
                companion_name="x", relationship="x", id_type="身份证",
                id_number="11010519900101001X", phone="13800000001",
                window_start="08:00", window_end="20:00")
        self.assertIn("discharged", str(cm.exception))

    def test_replace_companion_chain(self):
        """换陪护：旧码 replaced 失效，新码可用，replaced_by / parent 链可追溯。"""
        pid = self.admit()
        _, old_pass_id, old_code = self.issue(patient_id=pid, companion="旧陪护")
        new_fields = dict(companion_name="新陪护", relationship="配偶", id_type="身份证",
                          id_number="31010419880101001X", phone="13900000000",
                          window_start="08:00", window_end="20:00", quota=2)
        issued = core.replace_companion(conn=self.conn, staff_id=self.s["hn_head"],
                                        old_pass_id=old_pass_id,
                                        new_application_fields=new_fields)
        # 旧码拒绝
        r = core.scan_pass(self.conn, old_code, gate_code="HN-G1", clock=self.clock(9, 0))
        self.assertFalse(r["allowed"])
        self.assertEqual(r["reason_code"], "REPLACED")
        # 新码准入
        r2 = core.scan_pass(self.conn, issued["code_plain"], gate_code="HN-G1",
                            clock=self.clock(9, 0))
        self.assertTrue(r2["allowed"])
        self.assertEqual(r2["companion_name"], "新陪护")
        # 链
        old = self.conn.execute("SELECT * FROM passes WHERE id=?", (old_pass_id,)).fetchone()
        self.assertEqual(old["status"], "replaced")
        self.assertEqual(old["replaced_by_pass_id"], issued["pass_id"])
        new = self.conn.execute("SELECT * FROM passes WHERE id=?",
                                (issued["pass_id"],)).fetchone()
        self.assertEqual(new["parent_pass_id"], old_pass_id)


class TestTransfer(WardTestBase):
    def _admitted_with_pass(self):
        pid = self.admit()
        app_id, pass_id, code = self.issue(patient_id=pid, companion="林芳")
        # 再留一个 pending 申请，验证转科时一并作废
        pending_id = core.create_application_txn(
            self.conn, staff_id=self.s["hn_head"], patient_id=pid,
            companion_name="替补陪护", relationship="侄子", id_type="身份证",
            id_number="11010519900505005X", phone="13800000005",
            window_start="08:00", window_end="20:00", quota=2)
        return pid, app_id, pass_id, code, pending_id

    def test_transfer_revokes_old_pending_voided(self):
        pid, app_id, pass_id, code, pending_id = self._admitted_with_pass()
        r = core.transfer_patient(self.conn, staff_id=self.s["hn_head"], patient_id=pid,
                                  to_ward="XH", to_bed_id=self.bed["XH-01"])
        self.assertEqual(r["from_ward"], "HN")
        self.assertEqual(r["to_ward"], "XH")
        self.assertEqual(r["revoked"], 1)
        # 旧码在旧病区门岗 → 拒绝（转科原因全文）
        res = core.scan_pass(self.conn, code, gate_code="HN-G1", clock=self.clock(9, 0))
        self.assertFalse(res["allowed"])
        self.assertIn("转科", res["reason"])
        # pending 已作废
        st = self.conn.execute("SELECT status,reject_reason FROM applications WHERE id=?",
                               (pending_id,)).fetchone()
        self.assertEqual(st["status"], "voided")
        self.assertIn("转科", st["reject_reason"])
        # 患者已属新病区
        p = self.conn.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
        self.assertEqual(p["current_ward"], "XH")
        self.assertEqual(p["bed_id"], self.bed["XH-01"])

    def test_old_ward_cannot_reapply_after_transfer(self):
        """旧病区护士长不能替已转走患者重评（跨病区隔离）。"""
        pid, app_id, _, _, _ = self._admitted_with_pass()
        core.transfer_patient(self.conn, staff_id=self.s["hn_head"], patient_id=pid,
                              to_ward="XH", to_bed_id=self.bed["XH-01"])
        with self.assertRaises(core.RuleError):
            core.reapply_after_transfer(
                self.conn, staff_id=self.s["hn_head"], patient_id=pid,
                source_application_id=app_id, new_fields={})

    def test_new_ward_reapproves_chain(self):
        """新病区护士长调阅旧登记重评 → 新码在新病区可用、在旧病区拒绝，链相连。"""
        pid, app_id, _, old_code, _ = self._admitted_with_pass()
        core.transfer_patient(self.conn, staff_id=self.s["hn_head"], patient_id=pid,
                              to_ward="XH", to_bed_id=self.bed["XH-01"])
        issued = core.reapply_after_transfer(
            self.conn, staff_id=self.s["xh_head"], patient_id=pid,
            source_application_id=app_id, new_fields={})
        # 新码 XH 通过
        r = core.scan_pass(self.conn, issued["code_plain"], gate_code="XH-G1",
                           clock=self.clock(9, 0))
        self.assertTrue(r["allowed"])
        self.assertEqual(r["ward_code"], "XH")
        # 新码拿到 HN 扫 → 跨病区
        r2 = core.scan_pass(self.conn, issued["code_plain"], gate_code="HN-G1",
                            clock=self.clock(9, 1))
        self.assertFalse(r2["allowed"])
        self.assertEqual(r2["reason_code"], "CROSS_WARD")
        # 旧码在 HN 仍是转科撤销
        r3 = core.scan_pass(self.conn, old_code, gate_code="HN-G1", clock=self.clock(9, 2))
        self.assertFalse(r3["allowed"])
        # 授权链：source_application_id 与 parent_pass_id
        new_app = self.conn.execute("SELECT * FROM applications WHERE id="
                                    "(SELECT application_id FROM passes WHERE id=?)",
                                    (issued["pass_id"],)).fetchone()
        self.assertEqual(new_app["source_application_id"], app_id)
        new_pass = self.conn.execute("SELECT * FROM passes WHERE id=?",
                                     (issued["pass_id"],)).fetchone()
        self.assertIsNotNone(new_pass["parent_pass_id"])

    def test_transfer_same_ward_rejected(self):
        pid = self.admit()
        with self.assertRaises(core.RuleError):
            core.transfer_patient(self.conn, staff_id=self.s["hn_head"], patient_id=pid,
                                  to_ward="HN")

    def test_transfer_bed_must_belong_to_target_ward(self):
        pid = self.admit()
        with self.assertRaises(core.RuleError):
            core.transfer_patient(self.conn, staff_id=self.s["hn_head"], patient_id=pid,
                                  to_ward="XH", to_bed_id=self.bed["HN-02"])
