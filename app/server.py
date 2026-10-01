"""病区陪护通行管理 · 本地 Web 界面（标准库零依赖）。

三类视图，互不可越权：
- /gate   门岗核验台：只能扫码核验，页面与接口均无审批入口；大字显示。
- /nurse  护士视图：按床位组织，只能操作本病区；审批/撤销/转科/出院/封控。
- /records 通行记录；/handoff 转科交接看板。
敏感信息默认遮挡（陪护姓名打码、证件/手机遮罩），经护士勾选才在授权终端显示。
"""
from __future__ import annotations

import html
import os
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import core
from .db import connect, init_db, DB_PATH
from .gate_adapter import SimulatedGateAdapter
from .security import mask_id, mask_name, mask_phone

TEAL = "#0E7C86"
adapter = SimulatedGateAdapter()


# ---------------- 会话（本地演示，无密码） ----------------

def parse_session(handler: BaseHTTPRequestHandler) -> dict:
    cookie = SimpleCookie(handler.headers.get("Cookie", ""))
    raw = cookie["ward_session"].value if "ward_session" in cookie else ""
    if raw.startswith("staff:"):
        return {"type": "staff", "id": int(raw.split(":", 1)[1])}
    if raw.startswith("gate:"):
        return {"type": "gate", "code": raw.split(":", 1)[1]}
    return {}


# ---------------- 查询辅助 ----------------

class Views:
    def __init__(self, conn):
        self.conn = conn

    def staff(self, staff_id: int):
        return self.conn.execute("SELECT * FROM staff WHERE id=?", (staff_id,)).fetchone()

    def gate(self, code: str):
        return self.conn.execute("SELECT * FROM gates WHERE code=?", (code,)).fetchone()

    def ward_board(self, ward_code: str):
        """按床位组织本病区视图。"""
        ward = self.conn.execute("SELECT * FROM wards WHERE code=?", (ward_code,)).fetchone()
        beds = self.conn.execute(
            "SELECT * FROM beds WHERE ward_code=? ORDER BY CAST(bed_no AS INTEGER)", (ward_code,)
        ).fetchall()
        board = []
        for bed in beds:
            patient = self.conn.execute(
                "SELECT * FROM patients WHERE bed_id=? AND status='admitted'", (bed["id"],)
            ).fetchone()
            card = {"bed": bed, "patient": patient, "apps": [], "passes": []}
            if patient:
                card["apps"] = self.conn.execute(
                    "SELECT a.*, s.display_name reviewer FROM applications a "
                    "LEFT JOIN staff s ON s.id=a.reviewed_by "
                    "WHERE a.patient_id=? ORDER BY a.id", (patient["id"],)).fetchall()
                card["passes"] = self.conn.execute(
                    "SELECT p.*, s.display_name issuer FROM passes p "
                    "LEFT JOIN staff s ON s.id=p.issued_by "
                    "WHERE p.patient_id=? ORDER BY p.id DESC", (patient["id"],)).fetchall()
            board.append(card)
        # 本病区暂无床位的在院患者（转科入区未排床等）
        placed = {c["patient"]["id"] for c in board if c["patient"]}
        orphans = self.conn.execute(
            "SELECT * FROM patients WHERE current_ward=? AND status='admitted' AND bed_id IS NULL",
            (ward_code,)).fetchall()
        return ward, board, orphans

    def pending(self, ward_code: str):
        return self.conn.execute(
            "SELECT a.*, p.alias_code, p.name patient_name, s.display_name creator "
            "FROM applications a JOIN patients p ON p.id=a.patient_id "
            "LEFT JOIN staff s ON s.id=a.created_by "
            "WHERE a.ward_code=? AND a.status='pending' ORDER BY a.id", (ward_code,)).fetchall()

    def other_wards(self, ward_code: str):
        return self.conn.execute("SELECT * FROM wards WHERE code!=? ORDER BY code",
                                 (ward_code,)).fetchall()

    def events(self, ward_code: str | None = None, limit: int = 200):
        if ward_code:
            return self.conn.execute(
                "SELECT e.*, p.alias_code FROM events e "
                "LEFT JOIN patients p ON p.id=e.patient_id "
                "WHERE e.ward_code=? ORDER BY e.id DESC LIMIT ?", (ward_code, limit)).fetchall()
        return self.conn.execute(
            "SELECT e.*, p.alias_code FROM events e "
            "LEFT JOIN patients p ON p.id=e.patient_id "
            "ORDER BY e.id DESC LIMIT ?", (limit,)).fetchall()

    def handoff(self, ward_code: str | None = None):
        """交接看板：转科链（旧码撤销 → 新病区重评）、出院、封控、待评估。

        以「转科原因撤销的码」为链头：旧申请 pending（已作废）或 approved
        （码已撤销）都能展示；新申请通过 source_application_id 承接旧链。
        传 ward_code 时只返回与本病区相关的链路（转出或转入）。
        """
        sql = (
            "SELECT op.application_id old_app_id, op.ward_code from_ward, "
            "  op.companion_name, oa.relationship, op.window_start, op.window_end, "
            "  pt.id patient_id, pt.name patient_name, pt.alias_code, pt.current_ward to_ward, "
            "  b.bed_no to_bed, na.id new_app_id, np.id new_pass_id, np.code_mask new_code_mask, "
            "  np.status new_pass_status, op.id old_pass_id "
            "FROM passes op "
            "JOIN applications oa ON oa.id=op.application_id "
            "JOIN patients pt ON pt.id=op.patient_id "
            "LEFT JOIN beds b ON b.id=pt.bed_id "
            "LEFT JOIN applications na ON na.source_application_id=op.application_id "
            "LEFT JOIN passes np ON np.application_id=na.id "
            "WHERE op.revoke_reason=? {ward} "
            "ORDER BY op.id DESC")
        params: list = [core.REASON_TRANSFER]
        ward_clause = ""
        if ward_code:
            ward_clause = "AND (op.ward_code=? OR pt.current_ward=?)"
            params += [ward_code, ward_code]
        transfers = self.conn.execute(sql.format(ward=ward_clause), params).fetchall()
        discharged = self.conn.execute(
            "SELECT * FROM patients WHERE status='discharged' ORDER BY discharged_at DESC LIMIT 10"
        ).fetchall()
        lockdowns = self.conn.execute("SELECT * FROM wards WHERE locked=1").fetchall()
        return transfers, discharged, lockdowns


# ---------------- HTML ----------------

PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root{{--teal:{teal};--ink:#1F2D2E;--mut:#5D6B6D;--line:#D8DEE0;--bg:#F4F6F7;}}
*{{box-sizing:border-box}}
body{{margin:0;font-family:"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink);}}
a{{color:var(--teal);text-decoration:none}}
header{{background:#fff;border-bottom:1px solid var(--line);padding:14px 24px;display:flex;
 align-items:center;gap:18px;flex-wrap:wrap}}
header .brand{{font-weight:700;color:var(--teal);font-size:18px}}
header nav a{{margin-right:14px;color:var(--mut)}}
header nav a:hover{{color:var(--teal)}}
.who{{margin-left:auto;color:var(--mut);font-size:14px}}
main{{padding:24px;max-width:1180px;margin:0 auto}}
.card{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:18px;margin-bottom:16px}}
h2{{font-size:17px;margin:0 0 12px}} h3{{font-size:15px;margin:14px 0 8px;color:var(--teal)}}
.bed-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}}
.bed{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px}}
.bed .no{{display:inline-block;background:var(--teal);color:#fff;border-radius:6px;padding:2px 10px;
 font-weight:700;font-size:14px}}
.empty-bed{{color:var(--mut);border-style:dashed;text-align:center;padding:26px 10px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{color:var(--mut);font-weight:600;background:#FAFBFB}}
.tag{{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px}}
.t-active{{background:#E3F2F1;color:#0B5E66}} .t-pending{{background:#FDF3D7;color:#8A6100}}
.t-revoked,.t-rejected,.t-voided{{background:#F9E2E0;color:#9B2C22}}
.t-replaced{{background:#ECEFF1;color:#455A64}} .t-approved{{background:#E3F2F1;color:#0B5E66}}
.t-expired{{background:#ECEFF1;color:#455A64}} .t-in{{background:#DFF0E4;color:#1E6B33}}
.t-out{{background:#ECEFF1;color:#455A64}}
.btn{{display:inline-block;border:1px solid var(--teal);background:var(--teal);color:#fff;
 padding:7px 14px;border-radius:6px;font-size:14px;cursor:pointer}}
.btn.sec{{background:#fff;color:var(--teal)}} .btn.danger{{border-color:#9B2C22;background:#9B2C22}}
.btn.mini{{padding:3px 9px;font-size:12px}}
input,select,textarea{{font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:#fff}}
label.fld{{display:block;font-size:13px;color:var(--mut);margin:6px 0}}
.row{{display:flex;gap:10px;flex-wrap:wrap;align-items:center}}
.muted{{color:var(--mut);font-size:13px}}
.flash{{background:#FDF3D7;border:1px solid #E8C96A;border-radius:8px;padding:10px 14px;margin-bottom:14px}}
.err{{background:#F9E2E0;border:1px solid #D9A39C;border-radius:8px;padding:10px 14px;margin-bottom:14px;color:#7A2019}}
.ok{{background:#E3F2F1;border:1px solid #9CC8C5;border-radius:8px;padding:10px 14px;margin-bottom:14px;color:#0B5E66}}
.masked{{letter-spacing:1px;color:#7A2019;font-weight:700}}
/* 门岗大字页 */
.gate-stage{{max-width:900px}}
.scanbox{{font-size:22px;padding:16px 20px;width:100%;letter-spacing:3px;margin:8px 0}}
.verdict{{border-radius:16px;padding:34px 30px;text-align:center;margin-top:18px}}
.verdict.allow{{background:#E3F2F1;border:3px solid var(--teal)}}
.verdict.deny{{background:#F9E2E0;border:3px solid #9B2C22}}
.verdict .big-word{{font-size:64px;font-weight:800;letter-spacing:10px}}
.verdict.allow .big-word{{color:#0B5E66}} .verdict.deny .big-word{{color:#9B2C22}}
.alias{{font-size:72px;font-weight:800;color:var(--teal);letter-spacing:4px;margin:10px 0}}
.wardline{{font-size:34px;font-weight:700;margin:8px 0}}
.reason{{font-size:27px;line-height:1.5;color:#7A2019;margin-top:14px;font-weight:600}}
.meta{{font-size:21px;color:var(--mut);margin-top:16px}}
.pick{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px}}
.pick a{{display:block;background:#fff;border:1px solid var(--line);border-radius:10px;padding:18px}}
.pick a:hover{{border-color:var(--teal)}}
.pick .t{{font-weight:700;font-size:16px}} .pick .s{{color:var(--mut);font-size:13px;margin-top:6px}}
code{{background:#F0F2F3;padding:1px 6px;border-radius:4px;font-size:13px}}
</style></head><body>
<header><span class="brand">温溪康和医院 · 病区陪护通行管理</span>{nav}<span class="who">{who}</span></header>
<main>{body}</main></body></html>"""


def esc(x) -> str:
    return html.escape("" if x is None else str(x))


def tag(status_zh: str, cls: str) -> str:
    return f'<span class="tag {cls}">{esc(status_zh)}</span>'


STATUS_TAG = {
    "pending": ("待审批", "t-pending"), "approved": ("已批准", "t-approved"),
    "rejected": ("已拒绝", "t-rejected"), "voided": ("已作废", "t-voided"),
    "active": ("有效", "t-active"), "revoked": ("已撤销", "t-revoked"),
    "replaced": ("已换码", "t-replaced"), "expired": ("已过期", "t-expired"),
}


def page(title, body, nav="", who=""):
    return PAGE.format(title=esc(title), teal=TEAL, nav=nav, who=who, body=body)


# ---------------- HTTP 服务 ----------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _conn(self):
        return connect(self.server.db_path)

    def _send(self, content: str, status: int = 200, cookies: list[str] | None = None,
              ctype="text/html; charset=utf-8", redirect: str | None = None):
        if redirect:
            self.send_response(303)
            self.send_header("Location", redirect)
            self.send_header("Content-Length", "0")
            if cookies:
                for c in cookies:
                    self.send_header("Set-Cookie", c)
            self.end_headers()
            return
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if cookies:
            for c in cookies:
                self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(data)

    def _form(self) -> dict[str, str]:
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def _chrome(self, sess, views: Views | None = None):
        if sess.get("type") == "staff" and views:
            st = views.staff(sess["id"])
            nav = (f'<nav><a href="/nurse">床位视图</a><a href="/records">通行记录</a>'
                   f'<a href="/handoff">交接看板</a></nav>')
            who = (f'{esc(st["display_name"])}（{"护士长" if st["role"]=="head_nurse" else "护士"}'
                   f'·{esc(st["ward_code"])}） <a href="/logout">退出</a>')
            return nav, who
        if sess.get("type") == "gate":
            nav = f'<nav><a href="/gate">门岗核验台</a></nav>'
            who = f'门岗 {esc(sess["code"])} <a href="/logout">退出</a>'
            return nav, who
        return "", '<a href="/">选择身份</a>'

    # ---- 路由 ----
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path, qs = url.path, urllib.parse.parse_qs(url.query)
        sess = parse_session(self)
        try:
            if path == "/":
                return self._send(self.home())
            if path == "/logout":
                return self._send("", redirect="/",
                                  cookies=["ward_session=; Max-Age=0; Path=/"])
            if path == "/gate":
                return self.gate_page(sess)
            if path == "/nurse":
                return self.nurse_page(sess, qs)
            if path == "/records":
                return self.records_page(sess, qs)
            if path == "/handoff":
                return self.handoff_page(sess, qs)
            self._send(page("未找到", "<p>页面不存在。</p>"), 404)
        except Exception as e:  # noqa: BLE001
            self._send(page("服务错误", f'<div class="err">{esc(e)}</div>'), 500)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        sess = parse_session(self)
        form = self._form()
        try:
            if path == "/session":
                if form.get("kind") == "staff":
                    cookie = f'ward_session=staff:{int(form["staff_id"])}; Path=/; HttpOnly'
                else:
                    cookie = f'ward_session=gate:{urllib.parse.quote(form["gate_code"])}; Path=/; HttpOnly'
                return self._send("", redirect="/nurse" if form.get("kind") == "staff" else "/gate",
                                  cookies=[cookie])
            if path == "/gate/scan":
                return self.gate_scan(sess, form)

            # 以下全部为护士操作：门岗会话一律 403（门岗不能审批人员）
            if sess.get("type") != "staff":
                return self._send(
                    page("越权拒绝",
                         '<div class="err"><b>门岗仅有核验权限。</b><br>'
                         '门岗账户不能审批、发码、撤销或变更任何人员授权。</div>'), 403)
            conn = self._conn()
            try:
                staff_id = sess["id"]
                if path == "/nurse/apply":
                    core.create_application_txn(
                        conn, staff_id=staff_id, patient_id=int(form["patient_id"]),
                        companion_name=form["companion_name"], relationship=form["relationship"],
                        id_type=form.get("id_type", "身份证"), id_number=form["id_number"],
                        phone=form["phone"], window_start=form["window_start"],
                        window_end=form["window_end"], quota=int(form.get("quota", 1)))
                    msg = "陪护申请已登记，等待护士长审批。"
                elif path == "/nurse/review":
                    if form["decision"] == "approve":
                        core.review_application(conn, staff_id=staff_id,
                                                application_id=int(form["application_id"]),
                                                approve=True)
                        msg = "审批通过，临时通行码已发放（码值仅在发卡时出示给陪护人）。"
                    else:
                        core.review_application(conn, staff_id=staff_id,
                                                application_id=int(form["application_id"]),
                                                approve=False, reject_reason=form["reason"])
                        msg = "申请已拒绝，拒绝原因已记录并对门岗完整展示。"
                elif path == "/nurse/revoke":
                    core.revoke_pass(conn, staff_id=staff_id, pass_id=int(form["pass_id"]),
                                     reason=form["reason"])
                    msg = f"通行码已即时撤销：{esc(form['reason'])}"
                elif path == "/nurse/replace":
                    core.replace_companion(
                        conn, staff_id=staff_id, old_pass_id=int(form["old_pass_id"]),
                        new_application_fields=dict(
                            companion_name=form["companion_name"],
                            relationship=form["relationship"],
                            id_type=form.get("id_type", "身份证"),
                            id_number=form["id_number"], phone=form["phone"],
                            window_start=form["window_start"], window_end=form["window_end"],
                            quota=int(form.get("quota", 1))))
                    msg = "陪护人已更换：旧码即时撤销并由新码取代，授权链已记录。"
                elif path == "/nurse/discharge":
                    n = core.discharge_patient(conn, staff_id=staff_id,
                                               patient_id=int(form["patient_id"]))
                    msg = f"患者已办理出院，{n} 个有效通行码已全部即时撤销。"
                elif path == "/nurse/transfer":
                    bed_id = int(form["to_bed_id"]) if form.get("to_bed_id") else None
                    r = core.transfer_patient(conn, staff_id=staff_id,
                                              patient_id=int(form["patient_id"]),
                                              to_ward=form["to_ward"], to_bed_id=bed_id)
                    msg = (f"患者已转科：{r['from_ward']} → {r['to_ward']}，"
                           f"旧病区 {r['revoked']} 个通行码即时撤销，等待新病区护士长重新评估。")
                elif path == "/nurse/reapply":
                    core.reapply_after_transfer(
                        conn, staff_id=staff_id, patient_id=int(form["patient_id"]),
                        source_application_id=int(form["source_application_id"]), new_fields={})
                    msg = "转科重评通过：已沿用原陪护登记信息发放本病区新通行码，授权链已承接。"
                elif path == "/nurse/lockdown":
                    n = core.lockdown_ward(conn, staff_id=staff_id, ward_code=form["ward_code"])
                    msg = f"病区已封控：{n} 个有效通行码全部即时撤销，门岗暂停放行。"
                elif path == "/nurse/unlock":
                    core.unlock_ward(conn, staff_id=staff_id, ward_code=form["ward_code"])
                    msg = "病区已解除封控。此前撤销的码不会自动恢复，需重新申请审批。"
                else:
                    return self._send(page("未找到", "<p>操作不存在。</p>"), 404)
            finally:
                conn.close()
            return self._send("", redirect="/nurse?msg=" + urllib.parse.quote(msg))
        except core.RuleError as e:
            return self._send(page("操作被拒绝",
                                   f'<div class="err"><b>未执行：</b>{esc(e)}</div>'
                                   '<p><a href="javascript:history.back()">返回上一页</a></p>'), 409)
        except Exception as e:  # noqa: BLE001
            return self._send(page("服务错误", f'<div class="err">{esc(e)}</div>'), 500)

    # ---- 首页：身份选择 ----
    def home(self):
        conn = self._conn()
        try:
            staff_rows = conn.execute("SELECT * FROM staff ORDER BY ward_code, id").fetchall()
            gates = conn.execute("SELECT * FROM gates ORDER BY code").fetchall()
        finally:
            conn.close()
        b = ['<h2>请选择工作身份（本地演示，无需密码）</h2><div class="pick">']
        for s in staff_rows:
            role = "护士长" if s["role"] == "head_nurse" else "护士"
            b.append(f'<form method="post" action="/session">'
                     f'<input type="hidden" name="kind" value="staff">'
                     f'<input type="hidden" name="staff_id" value="{s["id"]}">'
                     f'<button class="t" style="all:unset;cursor:pointer;width:100%">'
                     f'<div class="t">{esc(s["display_name"])} · {role}</div>'
                     f'<div class="s">{esc(s["ward_code"])} 病区 · 仅可审批本病区申请</div>'
                     f'</button></form>')
        for g in gates:
            b.append(f'<form method="post" action="/session">'
                     f'<input type="hidden" name="kind" value="gate">'
                     f'<input type="hidden" name="gate_code" value="{esc(g["code"])}">'
                     f'<button class="t" style="all:unset;cursor:pointer;width:100%">'
                     f'<div class="t">门岗 {esc(g["name"])}</div>'
                     f'<div class="s">{esc(g["code"])} · 仅可扫码核验，不能审批</div>'
                     f'</button></form>')
        b.append("</div>")
        return page("身份选择", "".join(b))

    # ---- 门岗 ----
    def gate_page(self, sess, result=None, reveal=False, error=None):
        if sess.get("type") != "gate":
            body = ('<div class="err">请先从首页选择门岗身份。</div><p><a href="/">返回首页</a></p>')
            return self._send(page("门岗核验台", body))
        conn = self._conn()
        try:
            views = Views(conn)
            gate = views.gate(sess["code"])
            ward = conn.execute("SELECT * FROM wards WHERE code=?", (gate["ward_code"],)).fetchone()
            nav, who = self._chrome(sess)
        finally:
            conn.close()

        b = [f'<div class="gate-stage">',
             f'<h2>{esc(gate["name"])} · 门岗核验台</h2>',
             '<p class="muted">门岗仅有核验权限，不能审批人员或发放通行码。</p>']
        if ward["locked"]:
            b.append(f'<div class="err"><b>本病区当前封控中：</b>{esc(ward["locked_reason"])}'
                     '，所有陪护扫码一律拒绝。</div>')
        if error:
            b.append(f'<div class="err">{esc(error)}</div>')

        b.append('<div class="card"><form method="post" action="/gate/scan">'
                 '<label class="fld">请陪护人出示临时通行码，扫码或手输：</label>'
                 '<input class="scanbox" name="code" autofocus required '
                 'placeholder="WXK-XXXX-XXXX-XXXX" autocomplete="off">'
                 '<label class="fld"><input type="checkbox" name="reveal" value="1"> '
                 '临时显示陪护人完整姓名（默认遮挡；证件号在门岗任何情况下都不显示）</label>'
                 '<button class="btn" style="font-size:22px;padding:12px 34px">核 验</button>'
                 '</form></div>')
        if result:
            b.append(self._render_verdict(result, gate, reveal))
        b.append("</div>")
        return self._send(page("门岗核验台", "".join(b), nav, who))

    def _render_verdict(self, r, gate, reveal):
        companion = r.get("companion_name") or "—"
        shown = companion if reveal else mask_name(companion)
        name_cls = "" if reveal else "masked"
        if r["allowed"]:
            sig = adapter.present(gate["code"], r)
            return (
                '<div class="verdict allow">'
                f'<div class="big-word">✓ {esc(r["message"])}</div>'
                f'<div class="alias">{esc(r.get("alias_code","—"))}</div>'
                f'<div class="wardline">{esc(r["ward_code"])} 病区 · {esc(gate["name"])}</div>'
                f'<div class="meta">陪护人：<span class="{name_cls}">{esc(shown)}</span>'
                f'　|　通行时段 {esc(r["window"])}　|　闸机 {sig.barrier} · {sig.lamp}</div>'
                f'<div class="meta">核验时间 {esc(r["scanned_at"])}</div></div>')
        # 拒绝：原因用文字完整显示，不截断、不用代码代替
        sig = adapter.present(gate["code"], r)
        alias = r.get("alias_code", "—")
        return (
            '<div class="verdict deny">'
            '<div class="big-word">✗ 禁止通行</div>'
            f'<div class="alias" style="color:#9B2C22">{esc(alias)}</div>'
            f'<div class="wardline">{esc(r["ward_code"] or gate["ward_code"])} 病区'
            f' · {esc(gate["name"])}</div>'
            f'<div class="reason">{esc(r["reason"])}</div>'
            f'<div class="meta">陪护人：<span class="{name_cls}">{esc(shown)}</span>'
            f'　|　闸机 {sig.barrier} · {sig.lamp}</div>'
            f'<div class="meta">核验时间 {esc(r["scanned_at"])}</div></div>')

    def gate_scan(self, sess, form):
        if sess.get("type") != "gate":
            return self.gate_page(sess, error="请先选择门岗身份。")
        code = form.get("code", "").strip()
        reveal = form.get("reveal") == "1"
        if not code:
            return self.gate_page(sess, error="请输入通行码。", reveal=reveal)
        conn = self._conn()
        try:
            r = core.scan_pass(conn, code.upper(), gate_code=sess["code"])
        finally:
            conn.close()
        return self.gate_page(sess, result=r, reveal=reveal)

    # ---- 护士视图 ----
    def nurse_page(self, sess, qs):
        if sess.get("type") != "staff":
            body = ('<div class="err">请先从首页选择护士身份。</div><p><a href="/">返回首页</a></p>')
            return self._send(page("护士视图", body))
        conn = self._conn()
        try:
            views = Views(conn)
            st = views.staff(sess["id"])
            nav, who = self._chrome(sess, views)
            ward, board, orphans = views.ward_board(st["ward_code"])
            pending = views.pending(st["ward_code"])
            others = views.other_wards(st["ward_code"])
            reveal = qs.get("reveal", ["0"])[0] == "1"
            msg = qs.get("msg", [""])[0]

            b = []
            if msg:
                b.append(f'<div class="ok">{esc(msg)}</div>')
            lock = ('<div class="err"><b>本病区已封控：</b>'
                    f'{esc(ward["locked_reason"])} · 始于 {esc(ward["locked_at"])}</div>')
            if ward["locked"]:
                b.append(lock)
            b.append('<div class="row" style="justify-content:space-between"><h2 style="margin:0">'
                     f'{esc(ward["name"])}（{esc(ward["code"])}）· 床位视图</h2>'
                     '<span class="row">')
            b.append(f'<a class="btn sec" href="/nurse?reveal={"0" if reveal else "1"}">'
                     f'{"隐藏敏感信息" if reveal else "显示完整证件/手机（授权终端）"}</a>')
            if ward["locked"]:
                b.append(self._post_btn("/nurse/unlock", {"ward_code": ward["code"]},
                                        "解除封控", "sec"))
            else:
                b.append(self._post_btn("/nurse/lockdown", {"ward_code": ward["code"]},
                                        "病区封控（撤销全部码）", "danger"))
            b.append("</span></div>")

            # 待审批
            b.append('<div class="card"><h2>待护士长审批</h2>')
            if pending:
                b.append('<table><tr><th>患者</th><th>陪护人/关系</th><th>证件（遮罩）</th>'
                         '<th>手机（遮罩）</th><th>申请时段/配额</th><th>登记人</th><th>操作</th></tr>')
                for a in pending:
                    idn = esc(a["id_number"]) if reveal else esc(mask_id(a["id_number"], a["id_type"]))
                    ph = esc(a["phone"]) if reveal else esc(mask_phone(a["phone"]))
                    cn = esc(a["companion_name"]) if reveal else esc(mask_name(a["companion_name"]))
                    b.append(
                        "<tr>"
                        f'<td>{esc(a["alias_code"])}<br><span class="muted">{esc(a["patient_name"]) if reveal else esc(mask_name(a["patient_name"]))}</span></td>'
                        f'<td><b>{cn}</b><br>{esc(a["relationship"])}</td>'
                        f'<td>{esc(a["id_type"])}<br><code>{idn}</code></td>'
                        f'<td><code>{ph}</code></td>'
                        f'<td>{esc(a["window_start"])}–{esc(a["window_end"])}<br>'
                        f'<span class="muted">配额 {a["quota"]}</span></td>'
                        f'<td>{esc(a["creator"])}</td>'
                        '<td><form method="post" action="/nurse/review" class="row">'
                        f'<input type="hidden" name="application_id" value="{a["id"]}">'
                        '<button class="btn mini" name="decision" value="approve">批准发码</button>'
                        '</form>'
                        '<form method="post" action="/nurse/review" style="margin-top:6px">'
                        f'<input type="hidden" name="application_id" value="{a["id"]}">'
                        '<input type="hidden" name="decision" value="reject">'
                        '<input name="reason" required placeholder="拒绝原因（必填，全文展示）" '
                        'style="width:230px">'
                        '<button class="btn mini danger">拒绝</button></form></td></tr>')
                b.append("</table>")
            else:
                b.append('<p class="muted">暂无待审批申请。</p>')
            b.append("</div>")

            # 床位
            b.append('<div class="bed-grid">')
            for c in board:
                b.append(self._bed_card(c, reveal, others, conn))
            b.append("</div>")

            for p in orphans:
                b.append(f'<div class="card"><h2>未排床患者 {esc(p["alias_code"])}</h2>'
                         + self._apply_form(p)
                         + self._patient_actions(p, others, conn) + "</div>")
            return self._send(page("护士视图", "".join(b), nav, who))
        finally:
            conn.close()

    def _bed_card(self, c, reveal, others, conn):
        bed, p = c["bed"], c["patient"]
        if not p:
            return f'<div class="bed empty-bed">{esc(bed["bed_no"])} 床 · 空床</div>'
        pname = esc(p["name"]) if reveal else esc(mask_name(p["name"]))
        s = [f'<div class="bed"><span class="no">{esc(bed["bed_no"])} 床</span> '
             f'<b style="font-size:17px">{esc(p["alias_code"])}</b> '
             f'<span>{pname}</span>']
        for a in c["apps"]:
            zh, cls = STATUS_TAG[a["status"]]
            extra = ""
            if a["status"] == "rejected":
                extra = f'<div class="muted">拒绝原因：{esc(a["reject_reason"])}</div>'
            elif a["status"] == "voided":
                extra = f'<div class="muted">{esc(a["reject_reason"])}</div>'
            cn = esc(a["companion_name"]) if reveal else esc(mask_name(a["companion_name"]))
            s.append(f'<div style="margin-top:10px;padding-top:8px;border-top:1px solid #ECEFF1">'
                     f'{tag(zh, cls)} <b>{cn}</b>（{esc(a["relationship"])}）'
                     f'<span class="muted"> {esc(a["window_start"])}–{esc(a["window_end"])}</span>'
                     f'{extra}</div>')
        for ps in c["passes"]:
            zh, cls = STATUS_TAG[ps["status"]]
            inzone = tag("在区内", "t-in") if ps["in_zone"] else tag("已离区", "t-out")
            cn = esc(ps["companion_name"]) if reveal else esc(mask_name(ps["companion_name"]))
            s.append(f'<div style="margin-top:6px">{tag(zh, cls)} {inzone} '
                     f'<code>{esc(ps["code_mask"])}</code> {cn}')
            if ps["status"] != "active":
                s.append(f'<div class="muted">{esc(ps["revoke_reason"])} · {esc(ps["revoked_at"] or "")}</div>')
            else:
                s.append(self._pass_forms(ps))
            s.append("</div>")
        s.append(self._apply_form(p))
        s.append(self._patient_actions(p, others, conn, compact=True))
        s.append("</div>")
        return "".join(s)

    def _apply_form(self, p):
        """本病区护士为该床位患者登记陪护申请（仍需护士长审批发码）。"""
        return (
            '<details style="margin-top:10px"><summary class="muted" style="cursor:pointer">'
            '＋ 登记陪护申请（提交后待护士长审批）</summary>'
            f'<form method="post" action="/nurse/apply" style="margin-top:8px">'
            f'<input type="hidden" name="patient_id" value="{p["id"]}">'
            '<div class="row"><input name="companion_name" required placeholder="陪护人姓名">'
            '<input name="relationship" required placeholder="与患者关系" style="width:100px"></div>'
            '<div class="row" style="margin-top:6px">'
            '<select name="id_type"><option>身份证</option><option>护照</option><option>其他</option></select>'
            '<input name="id_number" required placeholder="身份证明号码（虚构演示）" style="flex:1"></div>'
            '<div class="row" style="margin-top:6px">'
            '<input name="phone" required placeholder="手机号" style="width:150px">'
            '<label class="muted">时段</label>'
            '<input name="window_start" value="08:00" style="width:80px"> – '
            '<input name="window_end" value="20:00" style="width:80px">'
            '<label class="muted">配额</label>'
            '<input name="quota" value="1" style="width:56px" title="限制时段发证上限"></div>'
            '<div style="margin-top:8px"><button class="btn mini">提交申请</button></div>'
            '</form></details>')

    def _pass_forms(self, ps):
        revoke = lambda reason, label, cls="sec": self._post_btn(
            "/nurse/revoke", {"pass_id": ps["id"], "reason": reason}, label, "mini " + cls)
        return (
            '<div class="row" style="margin-top:6px">'
            + revoke(core.REASON_PHONE_LOST, "手机丢失·撤销")
            + revoke(core.REASON_CODE_LEAKED, "码泄露·撤销", "danger")
            + f'<a class="btn mini sec" href="#replace-{ps["id"]}">更换陪护人</a></div>'
            f'<details id="replace-{ps["id"]}"><summary class="muted">为 {esc(ps["code_mask"])} '
            '登记新陪护人（旧码即时撤销换发新码）</summary>'
            f'<form method="post" action="/nurse/replace" style="margin-top:8px">'
            f'<input type="hidden" name="old_pass_id" value="{ps["id"]}">'
            '<div class="row"><input name="companion_name" required placeholder="新陪护人姓名">'
            '<input name="relationship" required placeholder="关系" style="width:90px"></div>'
            '<div class="row" style="margin-top:6px"><input name="id_number" required placeholder="身份证号（虚构）">'
            '<input name="phone" required placeholder="手机号" style="width:150px"></div>'
            '<div class="row" style="margin-top:6px">'
            '<input name="window_start" value="08:00" style="width:80px"> – '
            '<input name="window_end" value="20:00" style="width:80px">'
            '<input name="quota" value="1" style="width:60px" title="配额">'
            '<button class="btn mini danger">旧码撤销并换发</button></div></form></details>')

    def _patient_actions(self, p, others, conn, compact=False):
        title = "" if compact else '<h3>患者操作</h3>'
        opts = []
        for w in others:
            beds = conn.execute(
                "SELECT * FROM beds WHERE ward_code=? ORDER BY CAST(bed_no AS INTEGER)",
                (w["code"],)).fetchall()
            bed_opts = "".join(f'<option value="{b["id"]}">{esc(b["bed_no"])} 床</option>'
                               for b in beds)
            opts.append(
                f'<span class="card" style="padding:8px;display:inline-block;margin:4px 0">'
                f'转至 {esc(w["name"])} '
                f'<form method="post" action="/nurse/transfer" class="row" style="display:inline">'
                f'<input type="hidden" name="patient_id" value="{p["id"]}">'
                f'<input type="hidden" name="to_ward" value="{w["code"]}">'
                f'<select name="to_bed_id"><option value="">不排床</option>{bed_opts}</select>'
                f'<button class="btn mini">转科（撤销旧码）</button></form></span>')
        return (title + "".join(opts) +
                '<div class="row" style="margin-top:8px">'
                + self._post_btn("/nurse/discharge", {"patient_id": p["id"]},
                                 "办理出院（撤销全部码）", "danger") +
                '</div>')

    @staticmethod
    def _post_btn(action, hidden: dict, label, cls=""):
        ins = "".join(f'<input type="hidden" name="{k}" value="{esc(v)}">'
                      for k, v in hidden.items())
        return (f'<form method="post" action="{action}" style="display:inline">{ins}'
                f'<button class="btn {cls}">{esc(label)}</button></form>')

    # ---- 通行记录 ----
    def records_page(self, sess, qs):
        conn = self._conn()
        try:
            views = Views(conn)
            ward = None
            if sess.get("type") == "staff":
                st = views.staff(sess["id"])
                ward = st["ward_code"]
            elif sess.get("type") == "gate":
                g = views.gate(sess["code"])
                ward = g["ward_code"]
            else:
                body = '<div class="err">请先选择身份。</div><p><a href="/">返回首页</a></p>'
                return self._send(page("通行记录", body))
            nav, who = self._chrome(sess, views)
            # 跨病区隔离：无论查询参数如何，只允许查看本人所属病区记录
            ward_filter = ward
            rows = views.events(ward_filter)
            b = [f'<h2>通行与授权记录 · {esc(ward_filter)} 病区</h2>',
                 '<div class="card"><table><tr><th>时间</th><th>事件</th><th>患者代号</th>'
                 '<th>门岗/操作人</th><th>详情（拒绝原因全文）</th></tr>']
            kind_zh = {"zone_enter": "入区", "zone_exit": "离区", "scan_denied": "扫码拒绝",
                       "pass_issued": "发码", "pass_revoked": "撤销", "application_created": "申请登记",
                       "application_rejected": "拒绝申请", "companion_replaced": "更换陪护人",
                       "patient_transferred": "转科", "ward_unlocked": "解封",
                       "transfer_reapproved": "转科重评发码"}
            for e in rows:
                cls = ""
                if e["kind"] == "zone_enter":
                    cls = "t-in"
                elif e["kind"] == "scan_denied":
                    cls = "t-revoked"
                elif e["kind"] in ("zone_exit",):
                    cls = "t-out"
                actor = esc(e["gate_code"] or e["actor"])
                b.append(f'<tr><td>{esc(e["at"])}</td><td>{tag(kind_zh.get(e["kind"], e["kind"]), cls or "t-pending")}</td>'
                         f'<td>{esc(e["alias_code"] or "—")}</td><td>{actor}</td>'
                         f'<td>{esc(e["detail"])}</td></tr>')
            b.append("</table></div>")
            return self._send(page("通行记录", "".join(b), nav, who))
        finally:
            conn.close()

    # ---- 交接看板 ----
    def handoff_page(self, sess, qs):
        if sess.get("type") != "staff":
            body = ('<div class="err">交接看板仅供护士在授权终端查看，门岗无此权限。</div>'
                    '<p><a href="/">返回首页</a></p>')
            return self._send(page("交接看板", body), 403)
        conn = self._conn()
        try:
            views = Views(conn)
            st = views.staff(sess["id"])
            nav, who = self._chrome(sess, views)
            reveal = qs.get("reveal", ["0"])[0] == "1"
            # 默认只看本病区链路；护士长可主动查看全院
            scope = qs.get("scope", ["mine"])[0] if st["role"] == "head_nurse" else "mine"
            transfers, discharged, lockdowns = (
                views.handoff(None if scope == "all" else st["ward_code"]))

            def nm(x):
                return esc(x) if reveal else esc(mask_name(x))

            b = ['<div class="row" style="justify-content:space-between"><h2 style="margin:0">'
                 '转科与撤销交接看板</h2><span class="row">'
                 f'<a class="btn sec" href="/handoff?reveal={"0" if reveal else "1"}&scope={scope}">'
                 f'{"隐藏姓名" if reveal else "显示完整姓名"}</a>']
            if st["role"] == "head_nurse":
                other_scope = "mine" if scope == "all" else "all"
                b.append(f'<a class="btn sec" href="/handoff?scope={other_scope}'
                         f'&reveal={1 if reveal else 0}">'
                         f'{"只看本病区" if scope == "all" else "查看全院链路"}</a>')
            b.append("</span></div>")
            b.append('<p class="muted">敏感姓名默认遮挡；普通护士仅见本病区链路。</p>')

            b.append('<div class="card"><h2>① 转科授权链（旧病区撤销 → 新病区重新评估）</h2>')
            if transfers:
                b.append('<table><tr><th>患者</th><th>转科路径/床位</th><th>原陪护登记</th>'
                         '<th>旧申请时段</th><th>新病区评估</th><th>操作</th></tr>')
                for t in transfers:
                    if t["new_pass_id"]:
                        zh, cls = ("重评通过·" + STATUS_TAG[t["new_pass_status"]][0],
                                   "t-active" if t["new_pass_status"] == "active" else "t-replaced")
                        status = tag(zh, cls) + f'<br><code>{esc(t["new_code_mask"])}</code>'
                        action = '<span class="muted">授权链已承接</span>'
                    elif t["new_app_id"]:
                        status = tag("新申请待审批", "t-pending")
                        action = '<span class="muted">等待新病区护士长审批</span>'
                    elif (t["to_ward"] == st["ward_code"] and st["role"] == "head_nurse"):
                        status = tag("待本病区重新评估", "t-pending")
                        action = (f'<form method="post" action="/nurse/reapply">'
                                  f'<input type="hidden" name="patient_id" value="{t["patient_id"]}">'
                                  f'<input type="hidden" name="source_application_id" '
                                  f'value="{t["old_app_id"]}">'
                                  '<button class="btn mini">调阅旧登记并重评发码</button></form>')
                    else:
                        status = tag("待新病区重新评估", "t-pending")
                        action = '<span class="muted">等待新病区护士长评估</span>'
                    b.append(
                        f'<tr><td><b>{esc(t["alias_code"])}</b><br>{nm(t["patient_name"])}</td>'
                        f'<td>{esc(t["from_ward"])} → <b style="color:{TEAL}">{esc(t["to_ward"])}</b>'
                        f'<br>{esc(t["to_bed"] or "未排床")} 床</td>'
                        f'<td>{nm(t["companion_name"])}（{esc(t["relationship"])}）</td>'
                        f'<td>{esc(t["window_start"])}–{esc(t["window_end"])}</td>'
                        f'<td>{status}</td><td>{action}</td></tr>')
                b.append("</table>")
            else:
                b.append('<p class="muted">暂无与本病区相关的转科记录。</p>')
            b.append('</div>')

            b.append('<div class="card"><h2>② 出院撤销</h2>')
            if discharged:
                b.append('<table><tr><th>患者</th><th>出院时间</th><th>授权处理</th></tr>')
                for d in discharged:
                    b.append(f'<tr><td>{esc(d["alias_code"])} {nm(d["name"])}</td>'
                             f'<td>{esc(d["discharged_at"])}</td>'
                             f'<td>{tag("全部有效码已撤销", "t-revoked")}</td></tr>')
                b.append("</table>")
            else:
                b.append('<p class="muted">暂无出院记录。</p>')
            b.append('</div>')

            b.append('<div class="card"><h2>③ 封控状态</h2>')
            if lockdowns:
                for w in lockdowns:
                    b.append(f'<p>{tag("封控中", "t-revoked")} <b>{esc(w["name"])}</b>'
                             f' {esc(w["locked_reason"])} · 始于 {esc(w["locked_at"])}</p>')
            else:
                b.append('<p class="muted">当前无封控病区。</p>')
            b.append('</div>')
            return self._send(page("交接看板", "".join(b), nav, who))
        finally:
            conn.close()


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--port", type=int, default=int(os.environ.get("WARD_PORT", "8000")))
    args = ap.parse_args()

    conn = connect(args.db)
    init_db(conn)
    has = conn.execute("SELECT COUNT(*) c FROM wards").fetchone()["c"]
    conn.close()
    if not has:
        from .seed import seed
        seed(args.db)

    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    srv.db_path = args.db
    print(f"病区陪护通行管理已启动：http://localhost:{args.port}  （Ctrl+C 停止）")
    srv.serve_forever()


if __name__ == "__main__":
    main()
