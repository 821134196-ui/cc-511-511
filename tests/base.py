"""测试基座：每个用例一套独立的临时 SQLite 库与最小夹具。"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wardpass import db, services  # noqa: E402


class WardCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.dir, "t.db")
        self.conn = db.connect(self.db_path)
        db.init_db(self.conn)
        db.set_config(self.conn, "restricted_windows", json.dumps(["21:00-07:00"]))
        db.set_config(self.conn, "normal_quota", "2")
        db.set_config(self.conn, "restricted_quota", "1")
        with db.tx(self.conn):
            self.conn.execute(
                "INSERT INTO wards(code,name) VALUES('A','内科一病区'),('B','外科二病区')")
            self.conn.executemany(
                "INSERT INTO users(username,display_name,role,ward_id,gate_name)"
                " VALUES(?,?,?,?,?)",
                [("wang", "王慧", "nurse", 1, None),
                 ("li", "李岚", "head_nurse", 1, None),
                 ("zhao", "赵敏", "nurse", 2, None),
                 ("qian", "钱静", "head_nurse", 2, None),
                 ("gate_east", "陈刚", "gate", None, "东门")])
            self.conn.executemany(
                "INSERT INTO patients(ward_id,bed_no,name,code_name) VALUES(?,?,?,?)",
                [(1, "01", "周建国", "患A-01"),
                 (2, "01", "孙玉梅", "患B-01")])
        self.nurse_a = services.get_user(self.conn, 1)
        self.head_a = services.get_user(self.conn, 2)
        self.nurse_b = services.get_user(self.conn, 3)
        self.head_b = services.get_user(self.conn, 4)
        self.gate = services.get_user(self.conn, 5)
        self._escort_seq = 0

    def tearDown(self):
        self.conn.close()

    def at(self, hh, mm, day=1):
        """构造一个固定的"当前时间"用于时段边界测试。"""
        return datetime(2026, 10, day, hh, mm)

    def make_pass(self, patient_id=1, windows=("08:00-12:00",), name=None,
                  nurse=None, head=None):
        """走完整申请-审批链，返回 (pass_id, code, escort_id)。"""
        self._escort_seq += 1
        nurse = nurse or (self.nurse_a if patient_id == 1 else self.nurse_b)
        head = head or (self.head_a if patient_id == 1 else self.head_b)
        app_id, escort_id = services.create_application(
            self.conn, nurse, patient_id,
            {"name": name or f"测试陪护{self._escort_seq}",
             "relationship": "配偶",
             "id_number": f"1101011990010{1000 + self._escort_seq}"},
            list(windows), "测试申请")
        pass_id, code = services.approve_application(self.conn, head, app_id)
        return pass_id, code, escort_id

    def scan(self, code, direction="in", hh=9, mm=0, gate="东门", day=1):
        return services.verify_scan(self.conn, gate, code, direction,
                                    self.at(hh, mm, day))

    def pass_row(self, code):
        return self.conn.execute(
            "SELECT * FROM passes WHERE code=?", (code,)).fetchone()
