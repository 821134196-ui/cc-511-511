"""出院撤销：出院即时撤销全部授权，门岗即刻拦截。"""
from base import WardCase
from wardpass import services


class TestDischarge(WardCase):
    def test_discharge_revokes_all_passes_immediately(self):
        _, code1, _ = self.make_pass()
        _, code2, _ = self.make_pass()  # 同一患者第二张（配额内）
        self.assertTrue(self.scan(code1, "in", 9, 0)["allowed"])  # 一人在区内

        n = services.discharge_patient(self.conn, self.nurse_a, 1)
        self.assertEqual(n, 2, "出院应撤销该患者全部有效通行码")

        for code in (code1, code2):
            row = self.pass_row(code)
            self.assertEqual(row["status"], "revoked")
            self.assertEqual(row["inside"], 0, "在区状态必须清零")
            self.assertIn("出院", row["revoke_reason"])

            r = self.scan(code, "in", 9, 30)
            self.assertFalse(r["allowed"], "出院后扫码必须拦截")
            self.assertIn("出院", r["deny_reason"])

    def test_discharge_blocks_new_application_and_approval(self):
        app_id, _ = services.create_application(
            self.conn, self.nurse_a, 1,
            {"name": "张桂芳", "relationship": "配偶",
             "id_number": "110101199001011234"}, ["08:00-12:00"])
        services.discharge_patient(self.conn, self.nurse_a, 1)

        with self.assertRaises(services.ServiceError):
            services.approve_application(self.conn, self.head_a, app_id)
        with self.assertRaises(services.ServiceError):
            services.create_application(
                self.conn, self.nurse_a, 1,
                {"name": "周婷", "relationship": "子女",
                 "id_number": "110101199002022345"}, ["08:00-12:00"])

    def test_authorization_chain_events(self):
        """授权链：申请→发证→入区→出院撤销，每步都有事件。"""
        _, code, _ = self.make_pass()
        self.scan(code, "in", 9, 0)
        services.discharge_patient(self.conn, self.nurse_a, 1)
        types = [r["type"] for r in self.conn.execute(
            "SELECT type FROM events WHERE patient_id=1 ORDER BY id").fetchall()]
        self.assertEqual(types, ["申请", "审批发证", "入区", "出院"])
