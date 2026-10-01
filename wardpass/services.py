"""病区陪护通行码核心业务规则。

规则落点：
- 门岗只核验不审批；护士/护士长只能操作本病区（越权抛 ServiceError(403)）。
- 限制陪护时段（默认 21:00-07:00）每位患者限 1 名陪护，发证时校验配额。
- 同一通行码并发入区：靠条件 UPDATE 的原子性，只允许一次状态变更。
- 陪护人更换 / 手机丢失 / 码泄露 / 病区封控 / 患者出院：即时撤销。
- 患者转科：旧授权冻结为 suspended，需新病区护士长重新评估。
"""
import json
import secrets
from datetime import datetime

from . import db

# ---------------------------------------------------------------- 常量与工具

ROLE_NURSE = "nurse"
ROLE_HEAD = "head_nurse"
ROLE_GATE = "gate"

PASS_ACTIVE = "active"
PASS_SUSPENDED = "suspended"
PASS_REVOKED = "revoked"

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉易混淆字符

TIME_FMT = "%Y-%m-%d %H:%M:%S"


class ServiceError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def now_str(now=None):
    return (now or datetime.now()).strftime(TIME_FMT)


def mask_id(id_number):
    """证件号遮罩：保留前 4 位与后 4 位。"""
    s = str(id_number)
    if len(s) <= 8:
        return s[0] + "*" * (len(s) - 2) + s[-1]
    return s[:4] + "*" * (len(s) - 8) + s[-4:]


def mask_name(name):
    """姓名遮罩：张桂芳 -> 张*芳，张明 -> 张*。"""
    if len(name) <= 1:
        return name
    if len(name) == 2:
        return name[0] + "*"
    return name[0] + "*" * (len(name) - 2) + name[-1]


# ---------------------------------------------------------------- 时段窗口

def _parse_hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def parse_window(w):
    """'08:00-12:00' -> (480, 720)；支持跨夜 '21:00-07:00'。"""
    a, b = w.split("-")
    return _parse_hhmm(a), _parse_hhmm(b)


def split_intervals(windows):
    """把窗口列表展开为 [0,1440) 内的区间列表（跨夜拆两段）。"""
    out = []
    for w in windows:
        s, e = parse_window(w)
        if s < e:
            out.append((s, e))
        elif s > e:  # 跨夜
            out.append((s, 1440))
            out.append((0, e))
        else:  # 起止相同视为全天
            out.append((0, 1440))
    return out


def windows_contain(windows, hhmm):
    """开始时刻含、结束时刻不含：[start, end)。"""
    t = _parse_hhmm(hhmm)
    return any(s <= t < e for s, e in split_intervals(windows))


def windows_overlap(wa, wb):
    for s1, e1 in split_intervals(wa):
        for s2, e2 in split_intervals(wb):
            if s1 < e2 and s2 < e1:
                return True
    return False


def restricted_windows(conn):
    return json.loads(db.get_config(conn, "restricted_windows", '["21:00-07:00"]'))


def quotas(conn):
    return (
        int(db.get_config(conn, "normal_quota", "2")),
        int(db.get_config(conn, "restricted_quota", "1")),
    )


# ---------------------------------------------------------------- 权限

def get_user(conn, user_id):
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def require_role(user, *roles):
    if user is None:
        raise ServiceError("未登录", 401)
    if user["role"] not in roles:
        raise ServiceError("当前角色无权执行此操作", 403)


def require_ward(user, ward_id):
    """护士/护士长只能处理本病区；门岗无病区归属。"""
    if user["role"] == ROLE_GATE:
        raise ServiceError("门岗仅负责核验，不能审批或管理病区人员", 403)
    if user["ward_id"] != ward_id:
        raise ServiceError("只能处理本病区的申请与人员", 403)


def _actor(user):
    role_name = {"nurse": "护士", "head_nurse": "护士长", "gate": "门岗"}[user["role"]]
    return f"{user['display_name']}（{role_name}）"


def log_event(conn, user, type_, detail, ward_id=None, patient_id=None, pass_id=None,
              now=None):
    conn.execute(
        "INSERT INTO events(ward_id,patient_id,pass_id,actor,type,detail,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (ward_id, patient_id, pass_id, _actor(user) if isinstance(user, dict) or hasattr(user, "keys") else str(user),
         type_, detail, now_str(now)),
    )


# ---------------------------------------------------------------- 申请与审批

def _gen_pass_code(conn):
    while True:
        code = "P-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if not conn.execute("SELECT 1 FROM passes WHERE code=?", (code,)).fetchone():
            return code


def _next_escort_code(conn, ward_id):
    """陪护代号：登记病区字母 + 全局序号（转科后也不会与旧病区代号撞号）。"""
    ward = conn.execute("SELECT code FROM wards WHERE id=?", (ward_id,)).fetchone()
    n = conn.execute("SELECT COUNT(*) c FROM escorts").fetchone()["c"]
    return f"陪{ward['code']}-{n + 1:02d}"


def create_application(conn, user, patient_id, escort, windows, reason="",
                       kind="new", replaces_escort_id=None, now=None):
    """护士为本病区患者登记陪护申请。escort: {name, relationship, id_number}"""
    require_role(user, ROLE_NURSE, ROLE_HEAD)
    patient = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    if not patient:
        raise ServiceError("患者不存在", 404)
    require_ward(user, patient["ward_id"])
    if patient["status"] != "admitted":
        raise ServiceError("患者已出院，不能新增陪护申请")
    if not windows:
        raise ServiceError("请至少选择一个可通行时段")
    for w in windows:
        parse_window(w)  # 校验格式
    if not escort.get("name") or not escort.get("id_number"):
        raise ServiceError("请填写陪护人姓名与证件号")
    if kind == "replace" and not replaces_escort_id:
        raise ServiceError("更换陪护必须指定被更换的陪护人")

    with db.tx(conn):
        cur = conn.execute(
            "INSERT INTO escorts(patient_id,name,code_name,relationship,id_number,id_masked)"
            " VALUES(?,?,?,?,?,?)",
            (patient_id, escort["name"], _next_escort_code(conn, patient["ward_id"]),
             escort.get("relationship", "其他"), escort["id_number"],
             mask_id(escort["id_number"])),
        )
        escort_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO applications(patient_id,escort_id,kind,replaces_escort_id,"
            "windows,reason,status,created_by,created_at)"
            " VALUES(?,?,?,?,?,?, 'pending', ?,?)",
            (patient_id, escort_id, kind, replaces_escort_id,
             json.dumps(windows, ensure_ascii=False), reason,
             user["id"], now_str(now)),
        )
        app_id = cur.lastrowid
        log_event(conn, user, "申请",
                  f"为 {patient['bed_no']} 床 {patient['name']} 登记陪护 "
                  f"{mask_name(escort['name'])}（{escort.get('relationship','其他')}），"
                  f"时段 {'、'.join(windows)}",
                  ward_id=patient["ward_id"], patient_id=patient_id, now=now)
    return app_id, escort_id


def _check_quota(conn, patient_id, windows):
    """限制陪护时段不得超额发证；普通时段同样受总配额约束。"""
    normal_quota, restricted_quota = quotas(conn)
    rows = conn.execute(
        "SELECT windows FROM passes WHERE patient_id=? AND status='active'",
        (patient_id,),
    ).fetchall()
    if len(rows) >= normal_quota:
        raise ServiceError(
            f"该患者已有 {len(rows)} 张有效通行码，达到每患者上限 {normal_quota} 张")
    restricted = restricted_windows(conn)
    if windows_overlap(windows, restricted):
        n = sum(1 for r in rows
                if windows_overlap(json.loads(r["windows"]), restricted))
        if n >= restricted_quota:
            raise ServiceError(
                f"限制陪护时段（{'、'.join(restricted)}）每位患者仅允许 "
                f"{restricted_quota} 名陪护，当前已发 {n} 张，不得超额发证")


def approve_application(conn, user, app_id, note="", now=None):
    """护士长审批通过并发放临时通行码（含配额与封控校验）。"""
    app = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not app:
        raise ServiceError("申请不存在", 404)
    patient = conn.execute("SELECT * FROM patients WHERE id=?",
                           (app["patient_id"],)).fetchone()
    require_role(user, ROLE_HEAD)
    require_ward(user, patient["ward_id"])
    if app["status"] != "pending":
        raise ServiceError("该申请已处理，不能重复审批")
    ward = conn.execute("SELECT * FROM wards WHERE id=?",
                        (patient["ward_id"],)).fetchone()
    if ward["locked_down"]:
        raise ServiceError("病区封控中，暂停审批发证")
    if patient["status"] != "admitted":
        raise ServiceError("患者已出院，不能发证")
    windows = json.loads(app["windows"])
    _check_quota(conn, app["patient_id"], windows)

    with db.tx(conn):
        code = _gen_pass_code(conn)
        # 陪护人更换：旧陪护的通行码即时撤销
        if app["kind"] == "replace" and app["replaces_escort_id"]:
            old = app["replaces_escort_id"]
            conn.execute(
                "UPDATE passes SET status='revoked', inside=0, revoked_at=?,"
                " revoke_reason='陪护人更换，旧码即时作废'"
                " WHERE escort_id=? AND status IN ('active','suspended')",
                (now_str(now), old))
            conn.execute("UPDATE escorts SET active=0 WHERE id=?", (old,))
            log_event(conn, user, "更换陪护", "旧陪护通行码即时撤销",
                      ward_id=patient["ward_id"], patient_id=app["patient_id"], now=now)
        cur = conn.execute(
            "INSERT INTO passes(code,application_id,escort_id,patient_id,ward_id,"
            "windows,status,issued_by,issued_at) VALUES(?,?,?,?,?,?, 'active', ?,?)",
            (code, app_id, app["escort_id"], app["patient_id"], patient["ward_id"],
             app["windows"], user["id"], now_str(now)),
        )
        pass_id = cur.lastrowid
        conn.execute(
            "UPDATE applications SET status='approved', reviewed_by=?, reviewed_at=?,"
            " review_note=? WHERE id=?",
            (user["id"], now_str(now), note, app_id))
        log_event(conn, user, "审批发证",
                  f"批准申请 #{app_id}，发放通行码 {code}，"
                  f"时段 {'、'.join(windows)}" + (f"；备注：{note}" if note else ""),
                  ward_id=patient["ward_id"], patient_id=app["patient_id"],
                  pass_id=pass_id, now=now)
    return pass_id, code


def reject_application(conn, user, app_id, note="", now=None):
    app = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not app:
        raise ServiceError("申请不存在", 404)
    patient = conn.execute("SELECT * FROM patients WHERE id=?",
                           (app["patient_id"],)).fetchone()
    require_role(user, ROLE_HEAD)
    require_ward(user, patient["ward_id"])
    if app["status"] != "pending":
        raise ServiceError("该申请已处理")
    with db.tx(conn):
        conn.execute(
            "UPDATE applications SET status='rejected', reviewed_by=?, reviewed_at=?,"
            " review_note=? WHERE id=?",
            (user["id"], now_str(now), note, app_id))
        log_event(conn, user, "审批拒绝",
                  f"拒绝申请 #{app_id}" + (f"：{note}" if note else ""),
                  ward_id=patient["ward_id"], patient_id=app["patient_id"], now=now)


# ---------------------------------------------------------------- 门岗核验

def verify_scan(conn, gate_name, code, direction="auto", now=None):
    """门岗核验。返回展示用 dict；同一码并发入区只允许一次状态变更。

    direction: 'in' / 'out' / 'auto'（按当前在区状态推断）。
    """
    now = now or datetime.now()
    ts = now_str(now)
    row = conn.execute(
        "SELECT p.*, e.code_name AS escort_code, e.name AS escort_name,"
        " e.id_masked, e.id_number, e.relationship,"
        " pt.name AS patient_name, pt.code_name AS patient_code, pt.bed_no,"
        " pt.status AS patient_status, w.name AS ward_name, w.locked_down"
        " FROM passes p"
        " JOIN escorts e ON e.id=p.escort_id"
        " JOIN patients pt ON pt.id=p.patient_id"
        " JOIN wards w ON w.id=p.ward_id"
        " WHERE p.code=?", (code,)).fetchone()

    def deny(reason, pass_id=None, direc=None, display=None):
        conn.execute(
            "INSERT INTO passages(pass_id,code,gate_name,direction,result,deny_reason,"
            "scanned_at) VALUES(?,?,?,?, 'denied', ?,?)",
            (pass_id, code, gate_name, direc, reason, ts))
        return {"allowed": False, "deny_reason": reason, "gate": gate_name,
                "code": code, "scanned_at": ts, **(display or {})}

    if not row:
        return deny("通行码不存在或已注销")

    display = {
        "escort_code": row["escort_code"],
        "ward_name": row["ward_name"],
        "bed_no": row["bed_no"],
        "patient_code": row["patient_code"],
        "relationship": row["relationship"],
        # 敏感信息：默认遮罩，完整值仅供授权场景调接口查看
        "escort_name_masked": mask_name(row["escort_name"]),
        "id_masked": row["id_masked"],
        "windows": "、".join(json.loads(row["windows"])),
    }

    if row["patient_status"] == "discharged":
        return deny("患者已出院，通行权限已即时撤销", row["id"], display=display)
    if row["locked_down"]:
        return deny("病区封控中，暂停一切陪护通行", row["id"], display=display)
    if row["status"] == PASS_SUSPENDED:
        return deny("患者已转科，该授权冻结待重新评估", row["id"], display=display)
    if row["status"] == PASS_REVOKED:
        return deny(f"通行码已撤销（{row['revoke_reason'] or '未注明原因'}）",
                    row["id"], display=display)
    if not windows_contain(json.loads(row["windows"]), now.strftime("%H:%M")):
        return deny(f"当前时间不在可通行时段内（可通行时段：{display['windows']}）",
                    row["id"], display=display)

    if direction == "auto":
        direction = "out" if row["inside"] else "in"

    if direction == "in":
        # 原子翻转：并发扫描同一码时只有一个 UPDATE 命中 inside=0
        cur = conn.execute(
            "UPDATE passes SET inside=1 WHERE id=? AND inside=0 AND status='active'",
            (row["id"],))
        if cur.rowcount == 0:
            return deny("重复入区被拦截：该通行码已在区内或状态已变化",
                        row["id"], "in", display)
    else:
        cur = conn.execute(
            "UPDATE passes SET inside=0 WHERE id=? AND inside=1 AND status='active'",
            (row["id"],))
        if cur.rowcount == 0:
            return deny("重复离区被拦截：该通行码当前不在区内",
                        row["id"], "out", display)

    conn.execute(
        "INSERT INTO passages(pass_id,code,gate_name,direction,result,scanned_at)"
        " VALUES(?,?,?,?, 'allowed', ?)",
        (row["id"], code, gate_name, direction, ts))
    log_event(conn, f"门岗·{gate_name}", "入区" if direction == "in" else "离区",
              f"{row['escort_code']} 经{gate_name}{'入区' if direction == 'in' else '离区'}",
              ward_id=row["ward_id"], patient_id=row["patient_id"],
              pass_id=row["id"], now=now)
    return {"allowed": True, "direction": direction, "gate": gate_name,
            "code": code, "scanned_at": ts, **display}


# ---------------------------------------------------------------- 撤销

REVOKE_REASONS = ("手机丢失", "通行码泄露", "陪护人更换", "主动注销")


def revoke_pass(conn, user, pass_id, reason, now=None):
    p = conn.execute("SELECT * FROM passes WHERE id=?", (pass_id,)).fetchone()
    if not p:
        raise ServiceError("通行码不存在", 404)
    require_role(user, ROLE_NURSE, ROLE_HEAD)
    require_ward(user, p["ward_id"])
    if p["status"] == PASS_REVOKED:
        raise ServiceError("该通行码已撤销，无需重复操作")
    with db.tx(conn):
        conn.execute(
            "UPDATE passes SET status='revoked', inside=0, revoked_at=?,"
            " revoke_reason=? WHERE id=?",
            (now_str(now), reason, pass_id))
        log_event(conn, user, "撤销",
                  f"通行码 {p['code']} 即时撤销，原因：{reason}",
                  ward_id=p["ward_id"], patient_id=p["patient_id"],
                  pass_id=pass_id, now=now)


def discharge_patient(conn, user, patient_id, now=None):
    """患者出院：全部有效/冻结授权即时撤销。"""
    patient = conn.execute("SELECT * FROM patients WHERE id=?",
                           (patient_id,)).fetchone()
    if not patient:
        raise ServiceError("患者不存在", 404)
    require_role(user, ROLE_NURSE, ROLE_HEAD)
    require_ward(user, patient["ward_id"])
    if patient["status"] == "discharged":
        raise ServiceError("患者已出院")
    with db.tx(conn):
        conn.execute(
            "UPDATE patients SET status='discharged', discharged_at=? WHERE id=?",
            (now_str(now), patient_id))
        cur = conn.execute(
            "UPDATE passes SET status='revoked', inside=0, revoked_at=?,"
            " revoke_reason='患者出院，权限即时撤销'"
            " WHERE patient_id=? AND status IN ('active','suspended')",
            (now_str(now), patient_id))
        n = cur.rowcount
        log_event(conn, user, "出院",
                  f"{patient['bed_no']} 床 {patient['name']} 出院，"
                  f"{n} 张通行码即时撤销",
                  ward_id=patient["ward_id"], patient_id=patient_id, now=now)
    return n


def transfer_patient(conn, user, patient_id, to_ward_id, bed_no, now=None):
    """患者转科：旧授权冻结为待评估，需新病区护士长重新评估后恢复或注销。"""
    patient = conn.execute("SELECT * FROM patients WHERE id=?",
                           (patient_id,)).fetchone()
    if not patient:
        raise ServiceError("患者不存在", 404)
    require_role(user, ROLE_NURSE, ROLE_HEAD)
    require_ward(user, patient["ward_id"])
    if patient["status"] != "admitted":
        raise ServiceError("患者已出院，不能转科")
    target = conn.execute("SELECT * FROM wards WHERE id=?", (to_ward_id,)).fetchone()
    if not target:
        raise ServiceError("目标病区不存在", 404)
    if to_ward_id == patient["ward_id"]:
        raise ServiceError("患者已在该病区")
    with db.tx(conn):
        old_ward = conn.execute("SELECT name FROM wards WHERE id=?",
                                (patient["ward_id"],)).fetchone()["name"]
        conn.execute("UPDATE patients SET ward_id=?, bed_no=? WHERE id=?",
                     (to_ward_id, bed_no, patient_id))
        cur = conn.execute(
            "UPDATE passes SET status='suspended', ward_id=?"
            " WHERE patient_id=? AND status='active'",
            (to_ward_id, patient_id))
        n = cur.rowcount
        log_event(conn, user, "转科",
                  f"{patient['name']} 由{old_ward}转入{target['name']}（{bed_no} 床），"
                  f"{n} 张通行码冻结待重新评估",
                  ward_id=to_ward_id, patient_id=patient_id, now=now)
    return n


def reevaluate_pass(conn, user, pass_id, decision, now=None):
    """转科冻结码的重新评估：restore 恢复 / revoke 注销。限新病区护士长。"""
    p = conn.execute("SELECT * FROM passes WHERE id=?", (pass_id,)).fetchone()
    if not p:
        raise ServiceError("通行码不存在", 404)
    require_role(user, ROLE_HEAD)
    require_ward(user, p["ward_id"])
    if p["status"] != PASS_SUSPENDED:
        raise ServiceError("仅转科冻结的通行码需要重新评估")
    patient = conn.execute("SELECT * FROM patients WHERE id=?",
                           (p["patient_id"],)).fetchone()
    ward = conn.execute("SELECT * FROM wards WHERE id=?", (p["ward_id"],)).fetchone()
    with db.tx(conn):
        if decision == "restore":
            if ward["locked_down"]:
                raise ServiceError("病区封控中，不能恢复通行权限")
            if patient["status"] != "admitted":
                raise ServiceError("患者已出院，不能恢复")
            _check_quota(conn, p["patient_id"], json.loads(p["windows"]))
            conn.execute("UPDATE passes SET status='active' WHERE id=?", (pass_id,))
            log_event(conn, user, "评估恢复",
                      f"转科后重新评估通过，通行码 {p['code']} 恢复有效",
                      ward_id=p["ward_id"], patient_id=p["patient_id"],
                      pass_id=pass_id, now=now)
        elif decision == "revoke":
            conn.execute(
                "UPDATE passes SET status='revoked', inside=0, revoked_at=?,"
                " revoke_reason='转科后评估不通过，授权注销' WHERE id=?",
                (now_str(now), pass_id))
            log_event(conn, user, "评估注销",
                      f"转科后重新评估不通过，通行码 {p['code']} 注销",
                      ward_id=p["ward_id"], patient_id=p["patient_id"],
                      pass_id=pass_id, now=now)
        else:
            raise ServiceError("decision 必须是 restore 或 revoke")


def set_lockdown(conn, user, ward_id, on, now=None):
    """病区封控：即时撤销本病区全部通行权限；解封不自动恢复，需重新申请。"""
    require_role(user, ROLE_HEAD)
    require_ward(user, ward_id)
    ward = conn.execute("SELECT * FROM wards WHERE id=?", (ward_id,)).fetchone()
    with db.tx(conn):
        conn.execute("UPDATE wards SET locked_down=? WHERE id=?",
                     (1 if on else 0, ward_id))
        n = 0
        if on:
            cur = conn.execute(
                "UPDATE passes SET status='revoked', inside=0, revoked_at=?,"
                " revoke_reason='病区封控，权限即时撤销'"
                " WHERE ward_id=? AND status IN ('active','suspended')",
                (now_str(now), ward_id))
            n = cur.rowcount
        log_event(conn, user, "封控" if on else "解封",
                  (f"{ward['name']}封控，{n} 张通行码即时撤销" if on
                   else f"{ward['name']}解除封控（已撤销的通行码不自动恢复，需重新申请）"),
                  ward_id=ward_id, now=now)
    return n
