"""重建演示数据库（全部为虚构数据），并打印各场景演示码。

场景覆盖：正常发码、待审批、跨病区、手机丢失撤销、转科旧码撤销 + 新病区重评。
明文凭据只输出到控制台与 data/demo_codes.json（仅本地演示用），数据库只存哈希。
"""
from __future__ import annotations

import json
import os

from . import core
from .db import connect, init_db, DB_PATH
from .security import generate_badge_image

DEMO_CODES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "demo_codes.json")

FIXED_NOW = None  # 用真实时间，保证演示码处于有效状态


def seed(db_path: str | None = None) -> dict:
    path = db_path or DB_PATH
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)

    conn = connect(path)
    init_db(conn)
    clock = core.Clock()

    # ---- 病区 ----
    conn.execute("INSERT INTO wards(code,name) VALUES('HN','呼吸内科病区')")
    conn.execute("INSERT INTO wards(code,name) VALUES('XH','消化内科病区')")

    # ---- 护士 ----
    staff = {}
    for username, name, role, ward in [
        ("wangl", "王丽君", "head_nurse", "HN"),
        ("liqiang", "李强", "nurse", "HN"),
        ("zhangmin", "张敏", "head_nurse", "XH"),
    ]:
        cur = conn.execute(
            "INSERT INTO staff(username,display_name,role,ward_code) VALUES(?,?,?,?)",
            (username, name, role, ward))
        staff[username] = cur.lastrowid

    # ---- 门岗（每病区两个入口，用于双入口重放演示） ----
    gates = {}
    for code, name, ward in [
        ("HN-G1", "呼吸内科·东门", "HN"),
        ("HN-G2", "呼吸内科·西门", "HN"),
        ("XH-G1", "消化内科·东门", "XH"),
    ]:
        cur = conn.execute("INSERT INTO gates(code,name,ward_code) VALUES(?,?,?)",
                           (code, name, ward))
        gates[code] = cur.lastrowid

    # ---- 床位 ----
    beds = {}
    for ward, no in [("HN", "08"), ("HN", "12"), ("HN", "15"),
                     ("XH", "03"), ("XH", "06")]:
        cur = conn.execute("INSERT INTO beds(ward_code,bed_no) VALUES(?,?)", (ward, no))
        beds[f"{ward}-{no}"] = cur.lastrowid

    def admit(name, alias, ward, bed_key):
        cur = conn.execute(
            "INSERT INTO patients(name,alias_code,current_ward,bed_id,status)"
            " VALUES(?,?,?,?,'admitted')",
            (name, alias, ward, beds[bed_key]))
        return cur.lastrowid

    def admit_unplaced(name, alias, ward):
        cur = conn.execute(
            "INSERT INTO patients(name,alias_code,current_ward,bed_id,status)"
            " VALUES(?,?,?,NULL,'admitted')",
            (name, alias, ward))
        return cur.lastrowid

    p_zhou = admit("周建国", "A-0217", "HN", "HN-12")
    p_wu = admit_unplaced("吴桂芳", "A-0308", "HN")   # 等 08 床转出后入住
    p_zheng = admit("郑海涛", "A-0419", "XH", "XH-03")
    p_chen = admit("陈玉兰", "A-0522", "HN", "HN-15")
    p_lin = admit("林文斌", "A-0630", "HN", "HN-08")
    conn.commit()

    demo = {}

    def fields(companion, rel, idno, phone, ws="08:00", we="20:00", quota=1, idtype="身份证"):
        return dict(companion_name=companion, relationship=rel, id_type=idtype,
                    id_number=idno, phone=phone, window_start=ws, window_end=we, quota=quota)

    # 1) 周建国：女儿周晓燕，已批，有效码（正常入区/离区演示）
    app1 = core.create_application_txn(
        conn, staff_id=staff["wangl"], patient_id=p_zhou,
        **fields("周晓燕", "女儿", "110105199203172246", "13800001234"))
    iss1 = core.review_application(conn, staff_id=staff["wangl"],
                                   application_id=app1, approve=True)
    demo["zhou_active"] = {"patient": "周建国 A-0217", "companion": "周晓燕",
                           "code": iss1["code_plain"], "desc": "呼吸内科 有效码（08:00–20:00）"}
    generate_badge_image("A-0217", "周晓燕", "女儿", "身份证",
                         "110105199203172246", "呼吸内科病区")

    # 2) 吴桂芳（暂未排床）：女婿孙志明，待护士长审批
    app2 = core.create_application_txn(
        conn, staff_id=staff["liqiang"], patient_id=p_wu,
        **fields("孙志明", "女婿", "310104198807210033", "13900005678"))
    demo["wu_pending"] = {"patient": "吴桂芳 A-0308", "companion": "孙志明",
                          "application_id": app2, "desc": "待审批申请"}

    # 3) 郑海涛（消化内科）：弟弟郑海峰，有效码 —— 跨病区拒绝演示用
    app3 = core.create_application_txn(
        conn, staff_id=staff["zhangmin"], patient_id=p_zheng,
        **fields("郑海峰", "弟弟", "440305199112056617", "13700009012"))
    iss3 = core.review_application(conn, staff_id=staff["zhangmin"],
                                   application_id=app3, approve=True)
    demo["zheng_cross_ward"] = {"patient": "郑海涛 A-0419", "companion": "郑海峰",
                                "code": iss3["code_plain"],
                                "desc": "消化内科有效码，拿到呼吸内科门岗扫 → 跨病区拒绝"}
    generate_badge_image("A-0419", "郑海峰", "弟弟", "身份证",
                         "440305199112056617", "消化内科病区")

    # 4) 陈玉兰：孙女陈晨，码已发 → 手机丢失，即时挂失撤销
    app4 = core.create_application_txn(
        conn, staff_id=staff["wangl"], patient_id=p_chen,
        **fields("陈晨", "孙女", "320106200405198824", "13600003456"))
    iss4 = core.review_application(conn, staff_id=staff["wangl"],
                                   application_id=app4, approve=True)
    core.revoke_pass(conn, staff_id=staff["wangl"], pass_id=iss4["pass_id"],
                     reason=core.REASON_PHONE_LOST)
    demo["chen_lost_phone"] = {"patient": "陈玉兰 A-0522", "companion": "陈晨",
                               "code": iss4["code_plain"],
                               "desc": "手机丢失已撤销 → 门岗扫旧码拒绝并显示完整原因"}

    # 5) 林文斌：呼吸内科发码（配偶林芳）→ 转科至消化内科 06 床，旧码即时撤销，
    #    旧申请作废；消化内科护士长调阅旧申请重新评估，发新码，授权链相连。
    app5 = core.create_application_txn(
        conn, staff_id=staff["wangl"], patient_id=p_lin,
        **fields("林芳", "配偶", "510104198609304428", "13500007890"))
    iss5 = core.review_application(conn, staff_id=staff["wangl"],
                                   application_id=app5, approve=True)
    core.transfer_patient(conn, staff_id=staff["wangl"], patient_id=p_lin,
                          to_ward="XH", to_bed_id=beds["XH-06"])
    iss5b = core.reapply_after_transfer(
        conn, staff_id=staff["zhangmin"], patient_id=p_lin,
        source_application_id=app5, new_fields={})
    demo["lin_transfer"] = {
        "patient": "林文斌 A-0630", "companion": "林芳",
        "old_code": iss5["code_plain"], "new_code": iss5b["code_plain"],
        "desc": "转科样例：旧码（HN）已撤销，新码（XH）重评后有效；旧码在 HN 扫→拒绝，新码在 XH 扫→通过"}
    generate_badge_image("A-0630", "林芳", "配偶", "身份证",
                         "510104198609304428", "消化内科病区（转科重评）")

    # 林文斌转出后，08 床给待审批的吴桂芳
    conn.execute("UPDATE patients SET bed_id=? WHERE id=?", (beds["HN-08"], p_wu))
    conn.commit()

    conn.close()

    os.makedirs(os.path.dirname(DEMO_CODES_PATH), exist_ok=True)
    with open(DEMO_CODES_PATH, "w", encoding="utf-8") as f:
        json.dump(demo, f, ensure_ascii=False, indent=2)

    return demo


def main() -> None:
    demo = seed()
    print("=" * 72)
    print("演示数据库已重建（虚构数据）：", os.path.abspath(DB_PATH))
    print("-" * 72)
    for key, d in demo.items():
        print(f"[{key}] {d['desc']}")
        for k, v in d.items():
            if k.endswith("code"):
                print(f"    {k} = {v}")
    print("-" * 72)
    print("演示码 JSON：", os.path.abspath(DEMO_CODES_PATH))
    print("=" * 72)


if __name__ == "__main__":
    main()
