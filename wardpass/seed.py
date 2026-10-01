"""演示种子数据：两个病区、五类用户、若干患者与陪护。

内置两条问题样例，启动后即可在界面上看到：
- 转科样例：冯德华由内科一病区转入外科二病区，其通行码被冻结待重新评估；
- 丢码样例：周婷的通行码因"手机丢失"被即时撤销，再扫会被门岗拦截。
另含：码泄露撤销、出院撤销、夜间限制时段陪护、待审批申请、在区人员。
"""
import json
from datetime import datetime, timedelta

from . import db, idcards, services


def _id_no(i):
    """生成虚构证件号（110101 开头，明显为演示数据）。"""
    return f"1101011990{ i:02d}07{1000 + i * 7}"


def _attach_id_image(conn, escort_id):
    e = conn.execute("SELECT * FROM escorts WHERE id=?", (escort_id,)).fetchone()
    path = idcards.write_id_card(
        e["code_name"], services.mask_name(e["name"]), e["id_masked"], e["relationship"])
    conn.execute("UPDATE escorts SET id_image=? WHERE id=?", (path, escort_id))


def _apply_and_approve(conn, nurse, head, patient_id, escort, windows, reason,
                       kind="new", replaces_escort_id=None, now=None):
    app_id, escort_id = services.create_application(
        conn, nurse, patient_id, escort, windows, reason, kind, replaces_escort_id,
        now=now)
    pass_id, code = services.approve_application(conn, head, app_id, now=now)
    _attach_id_image(conn, escort_id)
    return pass_id, code, escort_id


def seed_all(conn, now=None):
    now = now or datetime.now()
    t = lambda **kw: now.replace(**kw)  # noqa: E731

    db.set_config(conn, "restricted_windows", json.dumps(["21:00-07:00"]))
    db.set_config(conn, "normal_quota", "2")
    db.set_config(conn, "restricted_quota", "1")

    with db.tx(conn):
        conn.execute("INSERT INTO wards(code,name) VALUES('A','内科一病区'),('B','外科二病区')")
        users = [
            ("wang",  "王慧",   "nurse",      1, None),
            ("li",    "李岚",   "head_nurse", 1, None),
            ("zhao",  "赵敏",   "nurse",      2, None),
            ("qian",  "钱静",   "head_nurse", 2, None),
            ("gate_east", "陈刚", "gate",     None, "东门"),
            ("gate_west", "刘洋", "gate",     None, "西门"),
        ]
        conn.executemany(
            "INSERT INTO users(username,display_name,role,ward_id,gate_name)"
            " VALUES(?,?,?,?,?)", users)
        patients = [
            (1, "01", "周建国", "患A-01"),
            (1, "02", "吴桂英", "患A-02"),
            (1, "03", "郑立新", "患A-03"),
            (1, "04", "冯德华", "患A-04"),
            (1, "05", "许文斌", "患A-05"),
            (2, "01", "孙玉梅", "患B-01"),
            (2, "02", "马志强", "患B-02"),
        ]
        conn.executemany(
            "INSERT INTO patients(ward_id,bed_no,name,code_name) VALUES(?,?,?,?)",
            patients)

    wang = services.get_user(conn, 1)
    li = services.get_user(conn, 2)
    zhao = services.get_user(conn, 3)
    qian = services.get_user(conn, 4)

    # 1) 正常陪护：01 床周建国 ← 配偶张桂芳（白天两个时段），当前在区内
    _, code1, _ = _apply_and_approve(
        conn, wang, li, 1,
        {"name": "张桂芳", "relationship": "配偶", "id_number": _id_no(1)},
        ["08:00-12:00", "14:00-20:00"], "术后生活照护",
        now=t(hour=8, minute=5))
    with db.tx(conn):
        conn.execute("UPDATE passes SET inside=1 WHERE code=?", (code1,))
        conn.execute(
            "INSERT INTO passages(pass_id,code,gate_name,direction,result,scanned_at)"
            " VALUES((SELECT id FROM passes WHERE code=?),?,'东门','in','allowed',?)",
            (code1, code1, services.now_str(t(hour=8, minute=32))))
        services.log_event(conn, "门岗·东门", "入区", f"陪A-01 经东门入区",
                           ward_id=1, patient_id=1,
                           pass_id=conn.execute("SELECT id FROM passes WHERE code=?",
                                                (code1,)).fetchone()["id"],
                           now=t(hour=8, minute=32))

    # 2) 夜间限制时段陪护：02 床吴桂英 ← 子女吴小军（21:00-07:00）
    _apply_and_approve(
        conn, wang, li, 2,
        {"name": "吴小军", "relationship": "子女", "id_number": _id_no(2)},
        ["21:00-07:00"], "夜间陪护",
        now=t(hour=9, minute=10))

    # 3) 丢码样例：01 床周建国 ← 子女周婷，发证后因手机丢失即时撤销
    lost_pass, lost_code, _ = _apply_and_approve(
        conn, wang, li, 1,
        {"name": "周婷", "relationship": "子女", "id_number": _id_no(3)},
        ["08:00-20:00"], "日间轮替照护",
        now=t(hour=9, minute=40))
    services.revoke_pass(conn, li, lost_pass, "手机丢失，通行码可能外泄，即时作废",
                         now=t(hour=10, minute=15))
    # 撤销后仍有人尝试扫码 → 门岗拦截记录
    services.verify_scan(conn, "东门", lost_code, "in", now=t(hour=10, minute=41))

    # 4) 码泄露样例：03 床郑立新 ← 兄弟郑伟，通行码泄露即时撤销
    leak_pass, _, _ = _apply_and_approve(
        conn, wang, li, 3,
        {"name": "郑伟", "relationship": "兄弟姐妹", "id_number": _id_no(4)},
        ["14:00-20:00"], "日间探视协助",
        now=t(hour=10, minute=30))
    services.revoke_pass(conn, li, leak_pass, "通行码截图外泄，即时作废",
                         now=t(hour=11, minute=5))

    # 5) 转科样例：04 床冯德华 ← 配偶刘淑兰，由 A 区转 B 区 03 床，授权冻结待评估
    _apply_and_approve(
        conn, wang, li, 4,
        {"name": "刘淑兰", "relationship": "配偶", "id_number": _id_no(5)},
        ["08:00-20:00"], "长期陪护",
        now=t(hour=7, minute=50))
    services.transfer_patient(conn, wang, 4, 2, "03", now=t(hour=11, minute=20))

    # 6) 出院样例：05 床许文斌 ← 配偶王秀珍，出院即时撤销
    _apply_and_approve(
        conn, wang, li, 5,
        {"name": "王秀珍", "relationship": "配偶", "id_number": _id_no(6)},
        ["08:00-20:00"], "住院陪护",
        now=t(hour=8, minute=20))
    services.discharge_patient(conn, wang, 5, now=t(hour=13, minute=40))

    # 7) 待审批样例：B 区 01 床孙玉梅 ← 子女孙丽，等待护士长审批
    app_id, escort_id = services.create_application(
        conn, zhao, 6,
        {"name": "孙丽", "relationship": "子女", "id_number": _id_no(7)},
        ["08:00-12:00", "14:00-18:00"], "化疗期间陪护",
        now=t(hour=13, minute=55))
    _attach_id_image(conn, escort_id)

    # 8) B 区正常陪护：02 床马志强 ← 配偶马丽
    _apply_and_approve(
        conn, zhao, qian, 7,
        {"name": "马丽", "relationship": "配偶", "id_number": _id_no(8)},
        ["08:00-20:00"], "术后陪护",
        now=t(hour=9, minute=5))

    return {"lost_code": lost_code}
