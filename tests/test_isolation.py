"""跨病区隔离与角色边界：护士/护士长只能处理本病区，门岗不能审批。"""
from base import WardCase
from wardpass import services


class TestIsolation(WardCase):
    def _escort(self):
        return {"name": "张桂芳", "relationship": "配偶",
                "id_number": "110101199001011234"}

    def test_nurse_cannot_apply_for_other_ward(self):
        with self.assertRaises(services.ServiceError) as ctx:
            services.create_application(
                self.conn, self.nurse_b, 1, self._escort(), ["08:00-12:00"])
        self.assertEqual(ctx.exception.status, 403)

    def test_head_cannot_approve_other_ward(self):
        app_id, _ = services.create_application(
            self.conn, self.nurse_a, 1, self._escort(), ["08:00-12:00"])
        with self.assertRaises(services.ServiceError) as ctx:
            services.approve_application(self.conn, self.head_b, app_id)
        self.assertEqual(ctx.exception.status, 403)

    def test_gate_cannot_approve_apply_revoke_or_lockdown(self):
        app_id, _ = services.create_application(
            self.conn, self.nurse_a, 1, self._escort(), ["08:00-12:00"])
        pass_id, code, _ = self.make_pass()

        for fn in (
            lambda: services.approve_application(self.conn, self.gate, app_id),
            lambda: services.create_application(
                self.conn, self.gate, 1, self._escort(), ["08:00-12:00"]),
            lambda: services.revoke_pass(self.conn, self.gate, pass_id, "越权"),
            lambda: services.set_lockdown(self.conn, self.gate, 1, True),
            lambda: services.discharge_patient(self.conn, self.gate, 1),
        ):
            with self.assertRaises(services.ServiceError) as ctx:
                fn()
            self.assertEqual(ctx.exception.status, 403, "门岗不能审批或管理人员")

    def test_nurse_cannot_revoke_other_ward_pass(self):
        _, code, _ = self.make_pass(patient_id=2)  # B 区患者的通行码
        pass_id = self.pass_row(code)["id"]
        with self.assertRaises(services.ServiceError) as ctx:
            services.revoke_pass(self.conn, self.nurse_a, pass_id, "跨区撤销")
        self.assertEqual(ctx.exception.status, 403)

    def test_head_cannot_lockdown_other_ward(self):
        with self.assertRaises(services.ServiceError) as ctx:
            services.set_lockdown(self.conn, self.head_a, 2, True)
        self.assertEqual(ctx.exception.status, 403)

    def test_gate_can_verify_any_ward(self):
        """门岗可以核验任意病区的码（核验≠审批）。"""
        _, code_a, _ = self.make_pass(patient_id=1)
        _, code_b, _ = self.make_pass(patient_id=2)
        self.assertTrue(self.scan(code_a, "in", 9, 0)["allowed"])
        self.assertTrue(self.scan(code_b, "in", 9, 0)["allowed"])
