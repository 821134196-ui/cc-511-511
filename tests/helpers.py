"""测试夹具：临时库 + 最小病区组织数据。"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime

from app import core
from app.db import connect, init_db


class WardTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.conn = connect(self.db_path)
        init_db(self.conn)
        self._bootstrap()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _bootstrap(self):
        c = self.conn
        c.execute("INSERT INTO wards(code,name) VALUES('HN','呼吸内科病区')")
        c.execute("INSERT INTO wards(code,name) VALUES('XH','消化内科病区')")
        staff = {}
        for uname, nm, role, ward in [
            ("hn_head", "王护长", "head_nurse", "HN"),
            ("hn_nurse", "李护士", "nurse", "HN"),
            ("xh_head", "张护长", "head_nurse", "XH"),
        ]:
            cur = c.execute("INSERT INTO staff(username,display_name,role,ward_code) VALUES(?,?,?,?)",
                            (uname, nm, role, ward))
            staff[uname] = cur.lastrowid
        self.s = staff
        self.g = {}
        for code, nm, ward in [("HN-G1", "HN东门", "HN"), ("HN-G2", "HN西门", "HN"),
                               ("XH-G1", "XH东门", "XH")]:
            cur = c.execute("INSERT INTO gates(code,name,ward_code) VALUES(?,?,?)", (code, nm, ward))
            self.g[code] = cur.lastrowid
        self.bed = {}
        for ward, no in [("HN", "01"), ("HN", "02"), ("XH", "01")]:
            cur = c.execute("INSERT INTO beds(ward_code,bed_no) VALUES(?,?)", (ward, no))
            self.bed[f"{ward}-{no}"] = cur.lastrowid
        c.commit()

    def admit(self, name="测试患者", alias=None, ward="HN", bed="HN-01"):
        alias = alias or f"A-{ward}-{self._seq()}"
        cur = self.conn.execute(
            "INSERT INTO patients(name,alias_code,current_ward,bed_id,status) VALUES(?,?,?,?,'admitted')",
            (name, alias, ward, self.bed[bed]))
        return cur.lastrowid

    def _seq(self) -> int:
        self._n = getattr(self, "_n", 0) + 1
        return self._n

    def issue(self, *, patient_id=None, staff="hn_head", ward="HN", bed=None,
              ws="08:00", we="20:00", quota=1, companion="陪护甲"):
        """登记并审批，返回 (application_id, pass_id, plain_code)。"""
        patient_id = patient_id or self.admit(alias=f"T-{ward}-{self._seq()}",
                                              ward=ward, bed=bed or f"{ward}-01")
        f = dict(companion_name=companion, relationship="子女", id_type="身份证",
                 id_number="11010519900101001X", phone="13800000001",
                 window_start=ws, window_end=we, quota=quota)
        app_id = core.create_application_txn(
            self.conn, staff_id=self.s[staff], patient_id=patient_id, **f)
        r = core.review_application(self.conn, staff_id=self.s[staff],
                                    application_id=app_id, approve=True)
        return app_id, r["pass_id"], r["code_plain"]

    @staticmethod
    def clock(hh, mm=0, day=15):
        return core.Clock(datetime(2026, 9, day, hh, mm, 0))
