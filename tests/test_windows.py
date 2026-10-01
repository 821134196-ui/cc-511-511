"""时段边界：窗口起点含、终点不含；跨夜窗口。"""
from base import WardCase


class TestWindows(WardCase):
    def test_day_window_boundaries(self):
        _, code, _ = self.make_pass(windows=("08:00-12:00",))

        r = self.scan(code, "in", 7, 59)
        self.assertFalse(r["allowed"], "窗口开始前 1 分钟应拒绝")
        self.assertIn("时段", r["deny_reason"])

        r = self.scan(code, "in", 8, 0)
        self.assertTrue(r["allowed"], "窗口起点（含）应放行")

        r = self.scan(code, "out", 8, 30)
        self.assertTrue(r["allowed"])

        r = self.scan(code, "in", 11, 59)
        self.assertTrue(r["allowed"], "窗口结束前 1 分钟应放行")

        r = self.scan(code, "out", 11, 59)
        self.assertTrue(r["allowed"])

        r = self.scan(code, "in", 12, 0)
        self.assertFalse(r["allowed"], "窗口终点（不含）应拒绝")

    def test_overnight_window(self):
        _, code, _ = self.make_pass(windows=("21:00-07:00",))

        self.assertFalse(self.scan(code, "in", 20, 59)["allowed"])
        self.assertTrue(self.scan(code, "in", 21, 0)["allowed"])
        self.assertTrue(self.scan(code, "out", 23, 30)["allowed"])
        # 跨夜后凌晨仍在窗口内
        r = self.scan(code, "in", 3, 0, day=2)
        self.assertTrue(r["allowed"], "跨夜窗口凌晨应放行")
        self.assertTrue(self.scan(code, "out", 6, 59, day=2)["allowed"])
        self.assertFalse(self.scan(code, "in", 7, 0, day=2)["allowed"],
                         "跨夜窗口终点（不含）应拒绝")

    def test_multi_window(self):
        _, code, _ = self.make_pass(windows=("08:00-12:00", "14:00-20:00"))
        self.assertTrue(self.scan(code, "in", 9, 0)["allowed"])
        self.assertTrue(self.scan(code, "out", 11, 30)["allowed"])
        self.assertFalse(self.scan(code, "in", 12, 30)["allowed"],
                         "两段窗口之间的空档入区应拒绝")
        self.assertFalse(self.scan(code, "out", 13, 0)["allowed"],
                         "两段窗口之间的空档离区同样核验时段")
        self.assertTrue(self.scan(code, "in", 14, 0)["allowed"])
