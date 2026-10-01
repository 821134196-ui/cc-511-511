"""码重放：同一通行码并发/重复扫描，只允许一次入区状态变更。"""
import threading

from base import WardCase
from wardpass import db, services


class TestReplay(WardCase):
    def setUp(self):
        super().setUp()
        db.set_config(self.conn, "normal_quota", "100")  # 本组用例不关心配额

    def test_concurrent_entry_two_gates(self):
        """两个入口闸机同时扫同一码：恰好放行一次（跑 10 轮防偶发）。"""
        for _ in range(10):
            _, code, _ = self.make_pass()
            results, errors = [], []
            barrier = threading.Barrier(2)

            def worker(gate_name):
                conn = db.connect(self.db_path)
                try:
                    barrier.wait(timeout=5)
                    r = services.verify_scan(conn, gate_name, code, "in", self.at(9, 0))
                    results.append(r["allowed"])
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
                finally:
                    conn.close()

            threads = [threading.Thread(target=worker, args=(g,))
                       for g in ("东门", "西门")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertFalse(errors, f"并发扫描出现异常：{errors}")
            self.assertEqual(sorted(results), [False, True],
                             "同一码并发入区必须恰好一次状态变更")
            self.assertEqual(self.pass_row(code)["inside"], 1)
            # 清理，避免影响下一轮配额无关的计数
            self.conn.execute(
                "UPDATE passes SET status='revoked', inside=0 WHERE code=?", (code,))

    def test_sequential_replay(self):
        """顺序重放：重复入区、重复离区都被拦截；出区后可再次入区。"""
        _, code, _ = self.make_pass()

        self.assertTrue(self.scan(code, "in", 9, 0)["allowed"])

        r = self.scan(code, "in", 9, 1)
        self.assertFalse(r["allowed"])
        self.assertIn("重复入区", r["deny_reason"])

        self.assertTrue(self.scan(code, "out", 9, 2)["allowed"])

        r = self.scan(code, "out", 9, 3)
        self.assertFalse(r["allowed"])
        self.assertIn("重复离区", r["deny_reason"])

        self.assertTrue(self.scan(code, "in", 9, 4)["allowed"],
                        "正常离区后再次入区应放行")

    def test_denied_scans_are_recorded(self):
        """被拦截的重放也要留痕，便于审计。"""
        _, code, _ = self.make_pass()
        self.scan(code, "in", 9, 0)
        self.scan(code, "in", 9, 1)
        rows = self.conn.execute(
            "SELECT result FROM passages WHERE code=? ORDER BY id", (code,)).fetchall()
        self.assertEqual([r["result"] for r in rows], ["allowed", "denied"])
