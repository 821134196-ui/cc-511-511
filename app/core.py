"""陪护授权核心规则。

角色边界：
- 护士（nurse/head_nurse）：只能处理 *本病区* 的申请；发码、撤销。
- 门岗（gate）：只能 scan，任何审批/发码/撤销调用都会被拒。

撤销触发（即时）：换陪护、手机丢失、通行码泄露、病区封控（该病区全部码）、
患者出院（该患者全部码）、转科（旧病区码撤销，新病区重新评估）。

并发：scan 使用 BEGIN IMMEDIATE 事务 + 状态条件更新 + 短重放窗口，
同一码在两个入口同时扫描时，只有一次入区状态变更成功，第二次被拒绝。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .security import generate_pass_code, hash_code

TIME_FMT = "%Y-%m-%d %H:%M:%S"
REPLAY_WINDOW_SECONDS = 30  # 同一码两次状态变更的最小间隔


class RuleError(Exception):
    """业务规则拒绝（信息可直接展示，必须是人话的完整原因）。"""


@dataclass
class Clock:
    """可注入时钟，便于测试时段边界。"""
    fixed: datetime | None = None

    def now(self) -> datetime:
        return self.fixed or datetime.now()


# ---- 撤销原因（固定文案，门岗页完整显示） ----
REASON_COMPANION_CHANGE = "陪护人已更换，原授权即时撤销"
REASON_PHONE_LOST = "陪护人手机丢失，通行码即时挂失撤销"
REASON_CODE_LEAKED = "通行码疑似泄露，为安全即时撤销"
REASON_WARD_LOCKDOWN = "病区封控，全部陪护权限暂停"
REASON_DISCHARGE = "患者已出院，陪护授权全部撤销"
REASON_TRANSFER = "患者转科，原病区授权撤销，需在新病区重新评估"
REASON_MANUAL = "护士手动撤销"


def _now(clock: Clock) -> str:
    return clock.now().strftime(TIME_FMT)


def _event(conn, *, pass_id, patient_id, ward_code, actor, kind, detail, clock):
    gate_code = actor[len("gate:"):] if actor.startswith("gate:") else None
    conn.execute(
        "INSERT INTO events(pass_id,patient_id,ward_code,gate_code,actor,kind,detail,at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (pass_id, patient_id, ward_code, gate_code, actor, kind, detail, _now(clock)),
    )


def _nurse(conn, staff_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM staff WHERE id=?", (staff_id,)).fetchone()
    if not row:
        raise RuleError("工作人员不存在")
    return row


def _require_nurse_ward(conn, staff_id: int, ward_code: str) -> sqlite3.Row:
    """护士只能处理本病区事务。"""
    row = _nurse(conn, staff_id)
    if row["ward_code"] != ward_code:
        raise RuleError(f"跨病区操作被拒绝：{row['display_name']} 属于 {row['ward_code']} 病区，"
                        f"无权处理 {ward_code} 病区的申请")
    return row


def _require_head_nurse_ward(conn, staff_id: int, ward_code: str) -> sqlite3.Row:
    """只有本病区护士长可以审批发码 / 转科重评。"""
    row = _require_nurse_ward(conn, staff_id, ward_code)
    if row["role"] != "head_nurse":
        raise RuleError(f"{row['display_name']} 是普通护士，陪护申请须由本病区护士长审核后才能发放通行码")
    return row


def active_pass_count(conn, patient_id: int, ward_code: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM passes WHERE patient_id=? AND ward_code=? AND status='active'",
        (patient_id, ward_code),
    ).fetchone()["c"]


def _validate_window(start: str, end: str) -> None:
    try:
        sh, sm = map(int, start.split(":"))
        eh, em = map(int, end.split(":"))
    except (ValueError, AttributeError):
        raise RuleError("通行时段格式应为 HH:MM")
    if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
        raise RuleError("通行时段时间非法")
    if (sh, sm) >= (eh, em):
        raise RuleError("通行时段结束时间必须晚于开始时间")


# ---------------- 申请 ----------------

def create_application(conn, *, staff_id: int, patient_id: int, companion_name: str,
                       relationship: str, id_type: str, id_number: str, phone: str,
                       window_start: str, window_end: str, quota: int = 1,
                       clock: Clock | None = None,
                       source_application_id: int | None = None) -> int:
    """（调用方负责事务）登记陪护申请。"""
    clock = clock or Clock()
    patient = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    if not patient:
        raise RuleError("患者不存在")
    if patient["status"] != "admitted":
        raise RuleError(f"患者当前状态为 {patient['status']}，不可提交陪护申请")
    ward_code = patient["current_ward"]
    _require_nurse_ward(conn, staff_id, ward_code)
    _validate_window(window_start, window_end)
    if quota < 1:
        raise RuleError("限制时段发证配额至少为 1")

    # 限制时段不得超额发证：名额 = 待审批申请数 + 有效通行码数；
    # 码一旦撤销/换发即释放名额。配额为患者×病区维度，取历史声明与本次申请的最大值。
    pending_n = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE patient_id=? AND ward_code=? AND status='pending'",
        (patient_id, ward_code)).fetchone()[0]
    active_n = conn.execute(
        "SELECT COUNT(*) FROM passes WHERE patient_id=? AND ward_code=? AND status='active'",
        (patient_id, ward_code)).fetchone()[0]
    used = pending_n + active_n
    max_existing = conn.execute(
        "SELECT COALESCE(MAX(quota),0) FROM applications "
        "WHERE patient_id=? AND ward_code=? AND status IN ('pending','approved')",
        (patient_id, ward_code)).fetchone()[0]
    ceiling = max(max_existing, quota)  # 允许本次申请上调患者配额
    if used >= ceiling:
        raise RuleError(f"该患者在本病区限制陪护时段的发证配额为 {ceiling}，"
                        f"已有 {used} 份有效申请，不得超额发证")

    cur = conn.execute(
        "INSERT INTO applications(patient_id,ward_code,companion_name,relationship,id_type,"
        "id_number,phone,window_start,window_end,quota,source_application_id,created_by,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (patient_id, ward_code, companion_name, relationship, id_type, id_number, phone,
         window_start, window_end, quota, source_application_id, staff_id, _now(clock)),
    )
    app_id = cur.lastrowid
    _event(conn, pass_id=None, patient_id=patient_id, ward_code=ward_code,
           actor=f"nurse:{staff_id}", kind="application_created",
           detail=f"陪护人 {companion_name}（{relationship}）申请时段 {window_start}-{window_end}",
           clock=clock)
    return app_id


def create_application_txn(conn, **kw) -> int:
    clock = kw.get("clock") or Clock()
    conn.execute("BEGIN IMMEDIATE")
    try:
        app_id = create_application(conn, clock=clock, **kw)
        conn.execute("COMMIT")
        return app_id
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------- 审批 / 发码 ----------------

def _issue_for_application(conn, app, staff_id: int, clock: Clock) -> dict:
    """（调用方负责事务）通过申请并发放临时通行码，返回明文码（仅此一次）。"""
    patient = conn.execute("SELECT * FROM patients WHERE id=?", (app["patient_id"],)).fetchone()
    if patient["status"] != "admitted" or patient["current_ward"] != app["ward_code"]:
        raise RuleError("患者已不在本病区住院，申请需在新病区重新评估")
    ceiling = conn.execute(
        "SELECT COALESCE(MAX(quota),1) FROM applications "
        "WHERE patient_id=? AND ward_code=? AND status IN ('pending','approved')",
        (app["patient_id"], app["ward_code"])).fetchone()[0]
    if active_pass_count(conn, app["patient_id"], app["ward_code"]) >= ceiling:
        raise RuleError(f"发证会超出配额 {ceiling}，已拒绝发码")

    code, code_h, code_mask = generate_pass_code()
    cur = conn.execute(
        "INSERT INTO passes(code_hash,code_mask,application_id,patient_id,ward_code,"
        "companion_name,alias_code,window_start,window_end,status,issued_at,issued_by)"
        " VALUES(?,?,?,?,?,?,?,?,?, 'active', ?,?)",
        (code_h, code_mask, app["id"], app["patient_id"], app["ward_code"],
         app["companion_name"], patient["alias_code"], app["window_start"], app["window_end"],
         _now(clock), staff_id),
    )
    pass_id = cur.lastrowid
    conn.execute("UPDATE applications SET status='approved', reviewed_by=?, reviewed_at=? WHERE id=?",
                 (staff_id, _now(clock), app["id"]))
    _event(conn, pass_id=pass_id, patient_id=app["patient_id"], ward_code=app["ward_code"],
           actor=f"nurse:{staff_id}", kind="pass_issued",
           detail=f"发放通行码 {code_mask}，时段 {app['window_start']}-{app['window_end']}",
           clock=clock)
    return {"pass_id": pass_id, "code_plain": code, "code_mask": code_mask}


def review_application(conn, *, staff_id: int, application_id: int, approve: bool,
                       reject_reason: str | None = None, clock: Clock | None = None) -> dict | None:
    clock = clock or Clock()
    conn.execute("BEGIN IMMEDIATE")
    try:
        app = conn.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
        if not app:
            raise RuleError("申请不存在")
        # 审核（批准或拒绝）仅本病区护士长可执行
        _require_head_nurse_ward(conn, staff_id, app["ward_code"])
        if app["status"] != "pending":
            raise RuleError(f"申请已审结（{app['status']}），不可重复审批")

        if not approve:
            if not reject_reason or not reject_reason.strip():
                raise RuleError("拒绝申请必须填写完整的拒绝原因")
            conn.execute(
                "UPDATE applications SET status='rejected', reject_reason=?, reviewed_by=?, reviewed_at=? WHERE id=?",
                (reject_reason.strip(), staff_id, _now(clock), app["id"]),
            )
            _event(conn, pass_id=None, patient_id=app["patient_id"], ward_code=app["ward_code"],
                   actor=f"nurse:{staff_id}", kind="application_rejected",
                   detail=reject_reason.strip(), clock=clock)
            conn.execute("COMMIT")
            return None

        issued = _issue_for_application(conn, app, staff_id, clock)
        conn.execute("COMMIT")
        return issued
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------- 撤销 ----------------

def _revoke_pass(conn, p: sqlite3.Row, reason: str, actor: str, clock: Clock,
                 replaced_by_pass_id: int | None = None) -> bool:
    """（调用方负责事务）"""
    if p["status"] != "active":
        return False
    new_status = "replaced" if replaced_by_pass_id else "revoked"
    conn.execute(
        "UPDATE passes SET status=?, revoke_reason=?, revoked_at=?, replaced_by_pass_id=? WHERE id=?",
        (new_status, reason, _now(clock), replaced_by_pass_id, p["id"]),
    )
    _event(conn, pass_id=p["id"], patient_id=p["patient_id"], ward_code=p["ward_code"],
           actor=actor, kind="pass_revoked", detail=reason, clock=clock)
    return True


def revoke_pass(conn, *, staff_id: int, pass_id: int, reason: str,
                clock: Clock | None = None) -> bool:
    """单个撤销：换陪护 / 手机丢失 / 码泄露 / 手动。"""
    clock = clock or Clock()
    conn.execute("BEGIN IMMEDIATE")
    try:
        p = conn.execute("SELECT * FROM passes WHERE id=?", (pass_id,)).fetchone()
        if not p:
            raise RuleError("通行码不存在")
        _require_nurse_ward(conn, staff_id, p["ward_code"])
        if not _revoke_pass(conn, p, reason, f"nurse:{staff_id}", clock):
            raise RuleError("该通行码已不是有效状态，无需撤销")
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


def replace_companion(conn, *, staff_id: int, old_pass_id: int,
                      new_application_fields: dict, clock: Clock | None = None) -> dict:
    """陪护人更换：旧码即时撤销（replaced），以新申请发新码，授权链相连。"""
    clock = clock or Clock()
    conn.execute("BEGIN IMMEDIATE")
    try:
        old = conn.execute("SELECT * FROM passes WHERE id=?", (old_pass_id,)).fetchone()
        if not old:
            raise RuleError("旧通行码不存在")
        _require_head_nurse_ward(conn, staff_id, old["ward_code"])

        # 先即时撤销旧码腾出配额名额（同一事务，对外原子可见）
        _revoke_pass(conn, old, REASON_COMPANION_CHANGE, f"nurse:{staff_id}", clock)
        new_app_id = create_application(conn, staff_id=staff_id, patient_id=old["patient_id"],
                                        clock=clock, **new_application_fields)
        new_app = conn.execute("SELECT * FROM applications WHERE id=?", (new_app_id,)).fetchone()
        issued = _issue_for_application(conn, new_app, staff_id, clock)
        # 补记 replaced 链
        conn.execute("UPDATE passes SET status='replaced', replaced_by_pass_id=? WHERE id=?",
                     (issued["pass_id"], old["id"]))
        conn.execute("UPDATE passes SET parent_pass_id=? WHERE id=?",
                     (old["id"], issued["pass_id"]))
        _event(conn, pass_id=issued["pass_id"], patient_id=old["patient_id"],
               ward_code=old["ward_code"], actor=f"nurse:{staff_id}",
               kind="companion_replaced",
               detail=f"旧码 {old['code_mask']} 由新码 {issued['code_mask']} 取代", clock=clock)
        conn.execute("COMMIT")
        return issued
    except Exception:
        conn.execute("ROLLBACK")
        raise


def lockdown_ward(conn, *, staff_id: int, ward_code: str, reason: str = REASON_WARD_LOCKDOWN,
                  clock: Clock | None = None) -> int:
    """病区封控：该病区全部有效码即时撤销，病区置封控状态。"""
    clock = clock or Clock()
    conn.execute("BEGIN IMMEDIATE")
    try:
        _require_head_nurse_ward(conn, staff_id, ward_code)
        conn.execute("UPDATE wards SET locked=1, locked_at=?, locked_reason=? WHERE code=?",
                     (_now(clock), reason, ward_code))
        n = 0
        for p in conn.execute("SELECT * FROM passes WHERE ward_code=? AND status='active'",
                              (ward_code,)).fetchall():
            if _revoke_pass(conn, p, reason, f"nurse:{staff_id}", clock):
                n += 1
        conn.execute("COMMIT")
        return n
    except Exception:
        conn.execute("ROLLBACK")
        raise


def unlock_ward(conn, *, staff_id: int, ward_code: str, clock: Clock | None = None) -> None:
    clock = clock or Clock()
    conn.execute("BEGIN IMMEDIATE")
    try:
        _require_head_nurse_ward(conn, staff_id, ward_code)
        conn.execute("UPDATE wards SET locked=0, locked_at=NULL, locked_reason=NULL WHERE code=?",
                     (ward_code,))
        _event(conn, pass_id=None, patient_id=None, ward_code=ward_code,
               actor=f"nurse:{staff_id}", kind="ward_unlocked", detail="病区解除封控", clock=clock)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def discharge_patient(conn, *, staff_id: int, patient_id: int,
                      clock: Clock | None = None) -> int:
    """患者出院：该患者 *所有病区* 的有效码全部撤销。"""
    clock = clock or Clock()
    conn.execute("BEGIN IMMEDIATE")
    try:
        patient = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
        if not patient:
            raise RuleError("患者不存在")
        if patient["current_ward"]:
            _require_head_nurse_ward(conn, staff_id, patient["current_ward"])
        conn.execute(
            "UPDATE patients SET status='discharged', discharged_at=?, bed_id=NULL WHERE id=?",
            (_now(clock), patient_id))
        n = 0
        for p in conn.execute("SELECT * FROM passes WHERE patient_id=? AND status='active'",
                              (patient_id,)).fetchall():
            if _revoke_pass(conn, p, REASON_DISCHARGE, f"nurse:{staff_id}", clock):
                n += 1
        conn.execute("COMMIT")
        return n
    except Exception:
        conn.execute("ROLLBACK")
        raise


def transfer_patient(conn, *, staff_id: int, patient_id: int, to_ward: str,
                     to_bed_id: int | None = None, clock: Clock | None = None) -> dict:
    """患者转科：旧病区有效码全部即时撤销；新授权须在新病区重新申请评估。"""
    clock = clock or Clock()
    conn.execute("BEGIN IMMEDIATE")
    try:
        patient = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
        if not patient:
            raise RuleError("患者不存在")
        from_ward = patient["current_ward"]
        if not from_ward:
            raise RuleError("患者未在任何病区住院")
        if from_ward == to_ward:
            raise RuleError("患者已在目标病区，无需转科")
        _require_head_nurse_ward(conn, staff_id, from_ward)
        if not conn.execute("SELECT 1 FROM wards WHERE code=?", (to_ward,)).fetchone():
            raise RuleError("目标病区不存在")
        if to_bed_id is not None:
            bed = conn.execute("SELECT * FROM beds WHERE id=?", (to_bed_id,)).fetchone()
            if not bed or bed["ward_code"] != to_ward:
                raise RuleError("目标床位不属于新病区")

        n = 0
        for p in conn.execute(
            "SELECT * FROM passes WHERE patient_id=? AND ward_code=? AND status='active'",
            (patient_id, from_ward)).fetchall():
            if _revoke_pass(conn, p, REASON_TRANSFER, f"nurse:{staff_id}", clock):
                n += 1
        # 旧病区未审结的申请一并作废
        conn.execute(
            "UPDATE applications SET status='voided', reject_reason=? "
            "WHERE patient_id=? AND ward_code=? AND status='pending'",
            (REASON_TRANSFER, patient_id, from_ward),
        )
        conn.execute("UPDATE patients SET current_ward=?, bed_id=? WHERE id=?",
                     (to_ward, to_bed_id, patient_id))
        _event(conn, pass_id=None, patient_id=patient_id, ward_code=from_ward,
               actor=f"nurse:{staff_id}", kind="patient_transferred",
               detail=f"患者由 {from_ward} 转至 {to_ward}，旧授权撤销 {n} 个，待新病区评估",
               clock=clock)
        conn.execute("COMMIT")
        return {"from_ward": from_ward, "to_ward": to_ward, "revoked": n}
    except Exception:
        conn.execute("ROLLBACK")
        raise


def reapply_after_transfer(conn, *, staff_id: int, patient_id: int,
                           source_application_id: int, new_fields: dict,
                           clock: Clock | None = None) -> dict:
    """新病区护士长对转科患者重新评估：沿用旧申请信息登记 → 审批发码。

    新 passes.parent_pass_id 指向旧码，applications.source_application_id 指向旧申请，
    交接看板据此串起授权链。
    """
    clock = clock or Clock()
    conn.execute("BEGIN IMMEDIATE")
    try:
        src = conn.execute("SELECT * FROM applications WHERE id=?",
                           (source_application_id,)).fetchone()
        if not src:
            raise RuleError("源申请不存在")
        patient = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
        if not patient or patient["status"] != "admitted":
            raise RuleError("患者不在院，无法重新评估")
        _require_head_nurse_ward(conn, staff_id, patient["current_ward"])

        fields = dict(new_fields)
        fields.setdefault("companion_name", src["companion_name"])
        fields.setdefault("relationship", src["relationship"])
        fields.setdefault("id_type", src["id_type"])
        fields.setdefault("id_number", src["id_number"])
        fields.setdefault("phone", src["phone"])
        fields.setdefault("window_start", src["window_start"])
        fields.setdefault("window_end", src["window_end"])
        fields.setdefault("quota", src["quota"])

        new_app_id = create_application(
            conn, staff_id=staff_id, patient_id=patient_id, clock=clock,
            source_application_id=source_application_id, **fields)
        new_app = conn.execute("SELECT * FROM applications WHERE id=?", (new_app_id,)).fetchone()
        issued = _issue_for_application(conn, new_app, staff_id, clock)

        old_pass = conn.execute(
            "SELECT * FROM passes WHERE application_id=? ORDER BY id DESC LIMIT 1",
            (source_application_id,)).fetchone()
        if old_pass:
            conn.execute("UPDATE passes SET parent_pass_id=? WHERE id=?",
                         (old_pass["id"], issued["pass_id"]))
        _event(conn, pass_id=issued["pass_id"], patient_id=patient_id,
               ward_code=patient["current_ward"], actor=f"nurse:{staff_id}",
               kind="transfer_reapproved",
               detail=f"转科重评通过，新码 {issued['code_mask']} 承接旧申请 #{source_application_id}",
               clock=clock)
        conn.execute("COMMIT")
        return issued
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------- 门岗核验 ----------------

def _in_window(now: datetime, start: str, end: str) -> bool:
    cur = now.strftime("%H:%M")
    # 左闭右开：08:00 准入，20:00 拒绝（边界测试依据）
    return start <= cur < end


def _deny(code: str, reason: str, *, ward_code=None, gate_code=None,
          pass_row=None, now_s=None) -> dict:
    d = {"allowed": False, "reason_code": code, "reason": reason,
         "ward_code": ward_code, "gate_code": gate_code, "scanned_at": now_s}
    if pass_row:
        d.update(alias_code=pass_row["alias_code"], companion_name=pass_row["companion_name"])
    return d


def scan_pass(conn, code_plain: str, *, gate_code: str, clock: Clock | None = None) -> dict:
    """门岗扫码。只允许核验，绝不审批。返回完整人话说理结果。

    同一码在两个入口同时扫描：BEGIN IMMEDIATE 串行化 + in_zone 条件更新 +
    REPLAY_WINDOW_SECONDS 短重放窗口，第二次扫描不会重复翻转入区状态。
    """
    clock = clock or Clock()
    nowdt = clock.now()
    now_s = nowdt.strftime(TIME_FMT)

    gate = conn.execute("SELECT * FROM gates WHERE code=?", (gate_code,)).fetchone()
    if not gate:
        return _deny("UNKNOWN_GATE", f"门岗 {gate_code} 未登记，禁止核验", now_s=now_s)
    ward_code = gate["ward_code"]

    conn.execute("BEGIN IMMEDIATE")
    try:
        p = conn.execute("SELECT * FROM passes WHERE code_hash=?",
                         (hash_code(code_plain),)).fetchone()

        if not p:
            result = _deny("CODE_NOT_FOUND", "通行码不存在或非本病区发放，禁止入区",
                           ward_code=ward_code, gate_code=gate_code, now_s=now_s)
        elif p["ward_code"] != ward_code:
            result = _deny("CROSS_WARD",
                           f"该通行码属于 {p['ward_code']} 病区，不能在 {ward_code} 病区使用，"
                           "请前往对应病区或在本病区重新申请",
                           ward_code=ward_code, gate_code=gate_code, pass_row=p, now_s=now_s)
        else:
            ward = conn.execute("SELECT * FROM wards WHERE code=?", (ward_code,)).fetchone()
            patient = conn.execute("SELECT * FROM patients WHERE id=?",
                                   (p["patient_id"],)).fetchone()
            if ward["locked"]:
                # 封控优先提示：封控期间该病区所有码（已撤销或有效）一律不放行
                result = _deny("LOCKDOWN",
                               f"病区封控中（{ward['locked_reason'] or REASON_WARD_LOCKDOWN}），"
                               "陪护暂停进出",
                               ward_code=ward_code, gate_code=gate_code, pass_row=p, now_s=now_s)
            elif p["status"] in ("revoked", "replaced", "expired"):
                label = {"revoked": "已撤销", "replaced": "已被新码取代而失效",
                         "expired": "已过期"}[p["status"]]
                result = _deny(p["status"].upper(),
                               f"通行码{label}：{p['revoke_reason'] or ''}",
                               ward_code=ward_code, gate_code=gate_code, pass_row=p, now_s=now_s)
            elif patient["status"] == "discharged":
                # 数据不变量兜底：出院应已撤销全部码
                result = _deny("DISCHARGED", REASON_DISCHARGE,
                               ward_code=ward_code, gate_code=gate_code, pass_row=p, now_s=now_s)
            elif not _in_window(nowdt, p["window_start"], p["window_end"]):
                result = _deny("OUTSIDE_WINDOW",
                               f"当前 {nowdt.strftime('%H:%M')} 不在陪护通行时段 "
                               f"{p['window_start']}–{p['window_end']} 内，请在允许时段扫码",
                               ward_code=ward_code, gate_code=gate_code, pass_row=p, now_s=now_s)
            elif p["last_zone_change_at"]:
                # 重放窗口：刚在另一入口扫过（含同时扫描落败方），短时间内不再变更状态
                last = datetime.strptime(p["last_zone_change_at"], TIME_FMT)
                if nowdt - last < timedelta(seconds=REPLAY_WINDOW_SECONDS):
                    state_txt = "入区" if p["in_zone"] else "离区"
                    result = _deny(
                        "REPLAY",
                        f"该通行码 {REPLAY_WINDOW_SECONDS} 秒内刚在 {p['last_gate_code'] or '另一入口'}"
                        f"完成{state_txt}扫码；同一码同时在两个入口扫描只允许一次入区状态变更，"
                        "本次未放行，请稍后再试",
                        ward_code=ward_code, gate_code=gate_code, pass_row=p, now_s=now_s)
                else:
                    result = _do_zone_change(conn, p, gate, now_s, clock)
            else:
                result = _do_zone_change(conn, p, gate, now_s, clock)

        if not result["allowed"]:
            conn.execute(
                "INSERT INTO events(pass_id,patient_id,ward_code,gate_code,actor,kind,detail,at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (p["id"] if p else None, p["patient_id"] if p else None, ward_code, gate_code,
                 f"gate:{gate_code}", "scan_denied", result["reason"], now_s),
            )
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _do_zone_change(conn, p, gate, now_s: str, clock: Clock) -> dict:
    """（在 scan 事务内）条件更新 in_zone —— 并发仲裁点。"""
    entering = p["in_zone"] == 0
    expected = 0 if entering else 1
    cur = conn.execute(
        "UPDATE passes SET in_zone=?, last_zone_change_at=?, last_gate_code=? "
        "WHERE id=? AND in_zone=?",
        (1 - expected, now_s, gate["code"], p["id"], expected),
    )
    if cur.rowcount != 1:
        return _deny("REPLAY",
                     "该通行码刚刚已在另一入口完成扫码，同一码同时重复扫描只允许一次入区状态变更，"
                     "本次未放行",
                     ward_code=p["ward_code"], gate_code=gate["code"], pass_row=p, now_s=now_s)
    conn.execute(
        "INSERT INTO events(pass_id,patient_id,ward_code,gate_code,actor,kind,detail,at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (p["id"], p["patient_id"], p["ward_code"], gate["code"], f"gate:{gate['code']}",
         "zone_enter" if entering else "zone_exit",
         f"在 {gate['name']} 扫码{'入区' if entering else '离区'}", now_s),
    )
    return {
        "allowed": True, "reason_code": "OK",
        "message": f"核验通过，允许{'入区' if entering else '离区'}",
        "action": "enter" if entering else "exit",
        "alias_code": p["alias_code"], "companion_name": p["companion_name"],
        "ward_code": p["ward_code"], "gate_code": gate["code"],
        "window": f"{p['window_start']}–{p['window_end']}", "scanned_at": now_s,
    }
