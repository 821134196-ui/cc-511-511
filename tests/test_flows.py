"""业务流程：限制时段配额、封控、转科再评估、陪护人更换。"""
from base import WardCase
from wardpass import services


class TestFlows(WardCase):
    def test_restricted_window_quota(self):
        """限制陪护时段（21:00-07:00）每患者限 1 名陪护，不得超额发证。"""
        self.make_pass(windows=("21:00-07:00",))
        with self.assertRaises(services.ServiceError) as ctx:
            self.make_pass(windows=("22:00-06:00",))
        self.assertIn("限制陪护时段", ctx.exception.message)
        # 不触碰限制时段的日间码不受此限
        _, code, _ = self.make_pass(windows=("08:00-12:00",))
        self.assertTrue(self.scan(code, "in", 9, 0)["allowed"])

    def test_normal_quota(self):
        self.make_pass(windows=("08:00-12:00",))
        self.make_pass(windows=("14:00-20:00",))
        with self.assertRaises(services.ServiceError) as ctx:
            self.make_pass(windows=("08:00-12:00",))
        self.assertIn("上限", ctx.exception.message)

    def test_lockdown_revokes_and_blocks(self):
        _, code, _ = self.make_pass()
        n = services.set_lockdown(self.conn, self.head_a, 1, True)
        self.assertEqual(n, 1)

        r = self.scan(code, "in", 9, 0)
        self.assertFalse(r["allowed"])
        self.assertIn("封控", r["deny_reason"])

        # 封控中不能审批发证
        app_id, _ = services.create_application(
            self.conn, self.nurse_a, 1,
            {"name": "周婷", "relationship": "子女",
             "id_number": "110101199002022345"}, ["08:00-12:00"])
        with self.assertRaises(services.ServiceError) as ctx:
            services.approve_application(self.conn, self.head_a, app_id)
        self.assertIn("封控", ctx.exception.message)

        # 解封不自动恢复，旧码仍是撤销态
        services.set_lockdown(self.conn, self.head_a, 1, False)
        r = self.scan(code, "in", 9, 30)
        self.assertFalse(r["allowed"])
        self.assertIn("撤销", r["deny_reason"])

    def test_transfer_suspends_then_restore_by_new_ward(self):
        _, code, _ = self.make_pass()
        n = services.transfer_patient(self.conn, self.nurse_a, 1, 2, "03")
        self.assertEqual(n, 1)

        row = self.pass_row(code)
        self.assertEqual(row["status"], "suspended")
        self.assertEqual(row["ward_id"], 2, "冻结授权随患者划入新病区")

        r = self.scan(code, "in", 9, 0)
        self.assertFalse(r["allowed"])
        self.assertIn("转科", r["deny_reason"])

        # 原病区护士长无权再评估，新病区护士长恢复
        with self.assertRaises(services.ServiceError):
            services.reevaluate_pass(self.conn, self.head_a, row["id"], "restore")
        services.reevaluate_pass(self.conn, self.head_b, row["id"], "restore")
        self.assertEqual(self.pass_row(code)["status"], "active")
        self.assertTrue(self.scan(code, "in", 9, 30)["allowed"])

    def test_transfer_then_reject(self):
        _, code, _ = self.make_pass()
        services.transfer_patient(self.conn, self.nurse_a, 1, 2, "03")
        pass_id = self.pass_row(code)["id"]
        services.reevaluate_pass(self.conn, self.head_b, pass_id, "revoke")
        self.assertEqual(self.pass_row(code)["status"], "revoked")
        self.assertFalse(self.scan(code, "in", 9, 0)["allowed"])

    def test_replace_escort_revokes_old_pass(self):
        _, old_code, old_escort = self.make_pass(name="张桂芳")
        app_id, new_escort = services.create_application(
            self.conn, self.nurse_a, 1,
            {"name": "周婷", "relationship": "子女",
             "id_number": "110101199002022345"},
            ["08:00-12:00"], "家属轮替", kind="replace",
            replaces_escort_id=old_escort)
        _, new_code = services.approve_application(self.conn, self.head_a, app_id)

        old = self.pass_row(old_code)
        self.assertEqual(old["status"], "revoked")
        self.assertIn("更换", old["revoke_reason"])
        self.assertEqual(self.conn.execute(
            "SELECT active FROM escorts WHERE id=?", (old_escort,)).fetchone()["active"], 0)

        self.assertFalse(self.scan(old_code, "in", 9, 0)["allowed"],
                         "旧陪护的码更换后立即失效")
        self.assertTrue(self.scan(new_code, "in", 9, 0)["allowed"])
