"""同一码双入口并发扫描：只允许一次入区状态变更（码重放）。"""
import threading

from app import core
from app.db import connect
from tests.helpers import WardTestBase


class TestReplay(WardTestBase):
    def test_concurrent_two_gates_one_zone_change(self):
        _, pass_id, code = self.issue()
        results = []
        errors = []
        barrier = threading.Barrier(2)

        def scan(gate):
            conn = connect(self.db_path)  # 每线程独立连接
            try:
                barrier.wait(timeout=5)
                r = core.scan_pass(conn, code, gate_code=gate, clock=self.clock(9, 0))
                results.append((gate, r))
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            finally:
                conn.close()

        t1 = threading.Thread(target=scan, args=("HN-G1",))
        t2 = threading.Thread(target=scan, args=("HN-G2",))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(errors, [])
        allowed = [r for _, r in results if r["allowed"]]
        denied = [r for _, r in results if not r["allowed"]]
        self.assertEqual(len(allowed), 1, f"应有且仅有一次入区变更：{results}")
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["reason_code"], "REPLAY")
        self.assertIn("只允许一次入区状态变更", denied[0]["reason"])

        row = self.conn.execute("SELECT in_zone FROM passes WHERE id=?", (pass_id,)).fetchone()
        self.assertEqual(row["in_zone"], 1)  # 最终状态：在区内，没有被第二次扫描翻转

        enters = self.conn.execute(
            "SELECT COUNT(*) c FROM events WHERE pass_id=? AND kind='zone_enter'", (pass_id,)
        ).fetchone()["c"]
        self.assertEqual(enters, 1)

    def test_sequential_double_scan_within_window_is_replay(self):
        """非并发但极短时间内连扫两次（同一状态翻转请求）：第二次拒绝。"""
        _, _, code = self.issue()
        r1 = core.scan_pass(self.conn, code, gate_code="HN-G1", clock=self.clock(9, 0, 15))
        r2 = core.scan_pass(self.conn, code, gate_code="HN-G2", clock=self.clock(9, 0, 15))
        self.assertTrue(r1["allowed"])
        self.assertFalse(r2["allowed"])
        self.assertEqual(r2["reason_code"], "REPLAY")

    def test_scan_after_window_elapsed_flips_back(self):
        """超过重放窗口后再扫：正常离区，再扫则重入。"""
        _, _, code = self.issue()
        r1 = core.scan_pass(self.conn, code, gate_code="HN-G1", clock=self.clock(9, 0))
        self.assertTrue(r1["allowed"])
        r2 = core.scan_pass(self.conn, code, gate_code="HN-G1", clock=self.clock(9, 1))
        self.assertTrue(r2["allowed"])
        self.assertEqual(r2["action"], "exit")

    def test_unknown_code(self):
        r = core.scan_pass(self.conn, "WXK-0000-0000-0000", gate_code="HN-G1",
                           clock=self.clock(9, 0))
        self.assertFalse(r["allowed"])
        self.assertEqual(r["reason_code"], "CODE_NOT_FOUND")

    def test_revoked_code_replay(self):
        """泄露撤销后旧码反复扫：每次都拒绝并给出完整撤销原因。"""
        _, pass_id, code = self.issue()
        core.revoke_pass(self.conn, staff_id=self.s["hn_head"], pass_id=pass_id,
                         reason=core.REASON_CODE_LEAKED)
        for _ in range(2):
            r = core.scan_pass(self.conn, code, gate_code="HN-G1", clock=self.clock(9, 0))
            self.assertFalse(r["allowed"])
            self.assertEqual(r["reason_code"], "REVOKED")
            self.assertIn("泄露", r["reason"])
