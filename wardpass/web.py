"""HTTP 层：标准库实现的微型路由 + 会话 + JSON API + 静态页服务。

鉴权约定：登录后下发 wp_sid Cookie；API 处理函数内用 services 的
require_role / require_ward 做角色与病区隔离校验（门岗不能审批、
护士只能处理本病区，均在服务层强制）。
"""
import json
import os
import re
import secrets
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db, services
from .gate_adapter import SimulatedGateAdapter

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")

SESSIONS = {}  # sid -> user_id（演示用内存会话，单进程足够）

PAGE_MAP = {
    "/": "index.html",
    "/nurse": "nurse.html",
    "/head": "head.html",
    "/gate": "gate.html",
    "/board": "board.html",
    "/records": "records.html",
}

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
}

ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, _compile(pattern), fn))
        return fn
    return deco


def _compile(pattern):
    parts = []
    for seg in pattern.strip("/").split("/"):
        m = re.fullmatch(r"<int:(\w+)>", seg)
        parts.append(f"(?P<{m.group(1)}>\\d+)" if m else re.escape(seg))
    return re.compile("^/" + "/".join(parts) + "$")


class Req:
    def __init__(self, handler):
        self.h = handler
        self._json = None
        self._user = None

    @property
    def json(self):
        if self._json is None:
            length = int(self.h.headers.get("Content-Length") or 0)
            body = self.h.rfile.read(length) if length else b"{}"
            try:
                self._json = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                raise services.ServiceError("请求体不是合法 JSON", 400)
        return self._json

    @property
    def user(self):
        if self._user is None:
            c = cookies.SimpleCookie(self.h.headers.get("Cookie"))
            morsel = c.get("wp_sid")
            uid = SESSIONS.get(morsel.value) if morsel else None
            if uid:
                conn = db.connect()
                try:
                    self._user = services.get_user(conn, uid)
                finally:
                    conn.close()
        return self._user


# ---------------------------------------------------------------- 查询组装

def _ward_overview(conn, ward_id):
    """护士视图：按床位组织患者、陪护与通行码。"""
    ward = conn.execute("SELECT * FROM wards WHERE id=?", (ward_id,)).fetchone()
    patients = conn.execute(
        "SELECT * FROM patients WHERE ward_id=? ORDER BY bed_no", (ward_id,)).fetchall()
    beds = []
    for p in patients:
        escorts = conn.execute(
            "SELECT * FROM escorts WHERE patient_id=? ORDER BY id", (p["id"],)).fetchall()
        escort_list = []
        for e in escorts:
            passes = conn.execute(
                "SELECT * FROM passes WHERE escort_id=? ORDER BY id DESC",
                (e["id"],)).fetchall()
            escort_list.append({
                "id": e["id"], "name": e["name"], "code_name": e["code_name"],
                "name_masked": services.mask_name(e["name"]),
                "relationship": e["relationship"], "id_masked": e["id_masked"],
                "id_image": e["id_image"], "active": e["active"],
                "passes": [dict(
                    id=ps["id"], code=ps["code"], status=ps["status"],
                    inside=ps["inside"], windows=json.loads(ps["windows"]),
                    revoke_reason=ps["revoke_reason"], issued_at=ps["issued_at"],
                    revoked_at=ps["revoked_at"],
                ) for ps in passes],
            })
        beds.append({
            "id": p["id"], "bed_no": p["bed_no"], "name": p["name"],
            "code_name": p["code_name"], "status": p["status"],
            "escorts": escort_list,
        })
    return {"ward": dict(id=ward["id"], name=ward["name"],
                         locked_down=ward["locked_down"]), "beds": beds}


def _applications(conn, ward_id, status=None):
    sql = (
        "SELECT a.*, e.name AS escort_name, e.code_name AS escort_code,"
        " e.relationship, e.id_masked, p.bed_no, p.name AS patient_name,"
        " u.display_name AS created_by_name"
        " FROM applications a"
        " JOIN escorts e ON e.id=a.escort_id"
        " JOIN patients p ON p.id=a.patient_id"
        " JOIN users u ON u.id=a.created_by"
        " WHERE p.ward_id=?")
    args = [ward_id]
    if status:
        sql += " AND a.status=?"
        args.append(status)
    sql += " ORDER BY a.id DESC LIMIT 100"
    rows = conn.execute(sql, args).fetchall()
    return [dict(
        id=r["id"], patient_id=r["patient_id"], bed_no=r["bed_no"],
        patient_name=r["patient_name"], escort_name=r["escort_name"],
        escort_name_masked=services.mask_name(r["escort_name"]),
        escort_code=r["escort_code"], relationship=r["relationship"],
        id_masked=r["id_masked"], kind=r["kind"],
        windows=json.loads(r["windows"]), reason=r["reason"], status=r["status"],
        created_by=r["created_by_name"], created_at=r["created_at"],
        review_note=r["review_note"], reviewed_at=r["reviewed_at"],
    ) for r in rows]


def _passes_of_ward(conn, ward_id, status=None):
    sql = (
        "SELECT ps.*, e.code_name AS escort_code, e.name AS escort_name,"
        " e.relationship, p.bed_no, p.name AS patient_name, p.code_name AS patient_code"
        " FROM passes ps"
        " JOIN escorts e ON e.id=ps.escort_id"
        " JOIN patients p ON p.id=ps.patient_id"
        " WHERE ps.ward_id=?")
    args = [ward_id]
    if status:
        sql += " AND ps.status=?"
        args.append(status)
    sql += " ORDER BY ps.id DESC LIMIT 200"
    rows = conn.execute(sql, args).fetchall()
    return [dict(
        id=r["id"], code=r["code"], status=r["status"], inside=r["inside"],
        escort_code=r["escort_code"], escort_name_masked=services.mask_name(r["escort_name"]),
        relationship=r["relationship"], bed_no=r["bed_no"],
        patient_name=r["patient_name"], patient_code=r["patient_code"],
        windows=json.loads(r["windows"]), issued_at=r["issued_at"],
        revoked_at=r["revoked_at"], revoke_reason=r["revoke_reason"],
    ) for r in rows]


# ---------------------------------------------------------------- API：会话

@route("GET", "/api/users")
def api_users(req):
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT u.username, u.display_name, u.role, u.gate_name, w.name AS ward_name"
            " FROM users u LEFT JOIN wards w ON w.id=u.ward_id ORDER BY u.id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@route("POST", "/api/login")
def api_login(req):
    username = (req.json.get("username") or "").strip()
    conn = db.connect()
    try:
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not user:
            raise services.ServiceError("用户不存在", 404)
        sid = secrets.token_hex(16)
        SESSIONS[sid] = user["id"]
        req.h.extra_headers.append(
            ("Set-Cookie", f"wp_sid={sid}; Path=/; HttpOnly; SameSite=Lax"))
        home = {"nurse": "/nurse", "head_nurse": "/head", "gate": "/gate"}[user["role"]]
        return {"ok": True, "home": home}
    finally:
        conn.close()


@route("POST", "/api/logout")
def api_logout(req):
    c = cookies.SimpleCookie(req.h.headers.get("Cookie"))
    morsel = c.get("wp_sid")
    if morsel:
        SESSIONS.pop(morsel.value, None)
    req.h.extra_headers.append(
        ("Set-Cookie", "wp_sid=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"))
    return {"ok": True}


@route("GET", "/api/me")
def api_me(req):
    u = req.user
    if not u:
        raise services.ServiceError("未登录", 401)
    conn = db.connect()
    try:
        ward = conn.execute("SELECT name FROM wards WHERE id=?",
                            (u["ward_id"],)).fetchone() if u["ward_id"] else None
        return {"username": u["username"], "display_name": u["display_name"],
                "role": u["role"], "ward_id": u["ward_id"],
                "ward_name": ward["name"] if ward else None,
                "gate_name": u["gate_name"]}
    finally:
        conn.close()


@route("GET", "/api/meta")
def api_meta(req):
    if not req.user:
        raise services.ServiceError("未登录", 401)
    conn = db.connect()
    try:
        wards = conn.execute("SELECT id,code,name,locked_down FROM wards").fetchall()
        return {
            "wards": [dict(w) for w in wards],
            "restricted_windows": services.restricted_windows(conn),
            "normal_quota": services.quotas(conn)[0],
            "restricted_quota": services.quotas(conn)[1],
            "revoke_reasons": list(services.REVOKE_REASONS),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------- API：病区

@route("GET", "/api/ward/overview")
def api_overview(req):
    u = req.user
    services.require_role(u, services.ROLE_NURSE, services.ROLE_HEAD)
    conn = db.connect()
    try:
        return _ward_overview(conn, u["ward_id"])
    finally:
        conn.close()


@route("GET", "/api/applications")
def api_applications(req):
    u = req.user
    services.require_role(u, services.ROLE_NURSE, services.ROLE_HEAD)
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(req.h.path).query)
    conn = db.connect()
    try:
        return _applications(conn, u["ward_id"], (q.get("status") or [None])[0])
    finally:
        conn.close()


@route("POST", "/api/applications")
def api_create_application(req):
    u = req.user
    body = req.json
    conn = db.connect()
    try:
        app_id, escort_id = services.create_application(
            conn, u, int(body.get("patient_id")), body.get("escort") or {},
            body.get("windows") or [], body.get("reason") or "",
            body.get("kind") or "new", body.get("replaces_escort_id"))
        from . import idcards
        e = conn.execute("SELECT * FROM escorts WHERE id=?", (escort_id,)).fetchone()
        path = idcards.write_id_card(e["code_name"], services.mask_name(e["name"]),
                                     e["id_masked"], e["relationship"])
        conn.execute("UPDATE escorts SET id_image=? WHERE id=?", (path, escort_id))
        return {"ok": True, "application_id": app_id}
    finally:
        conn.close()


@route("POST", "/api/applications/<int:app_id>/approve")
def api_approve(req, app_id):
    u = req.user
    conn = db.connect()
    try:
        pass_id, code = services.approve_application(
            conn, u, app_id, req.json.get("note") or "")
        return {"ok": True, "pass_id": pass_id, "code": code}
    finally:
        conn.close()


@route("POST", "/api/applications/<int:app_id>/reject")
def api_reject(req, app_id):
    u = req.user
    conn = db.connect()
    try:
        services.reject_application(conn, u, app_id, req.json.get("note") or "")
        return {"ok": True}
    finally:
        conn.close()


@route("GET", "/api/passes")
def api_passes(req):
    u = req.user
    services.require_role(u, services.ROLE_NURSE, services.ROLE_HEAD)
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(req.h.path).query)
    conn = db.connect()
    try:
        return _passes_of_ward(conn, u["ward_id"], (q.get("status") or [None])[0])
    finally:
        conn.close()


@route("POST", "/api/passes/<int:pass_id>/revoke")
def api_revoke(req, pass_id):
    u = req.user
    reason = (req.json.get("reason") or "").strip()
    if not reason:
        raise services.ServiceError("请填写撤销原因")
    conn = db.connect()
    try:
        services.revoke_pass(conn, u, pass_id, reason)
        return {"ok": True}
    finally:
        conn.close()


@route("POST", "/api/passes/<int:pass_id>/reevaluate")
def api_reevaluate(req, pass_id):
    u = req.user
    conn = db.connect()
    try:
        services.reevaluate_pass(conn, u, pass_id, req.json.get("decision") or "")
        return {"ok": True}
    finally:
        conn.close()


@route("POST", "/api/patients/<int:patient_id>/discharge")
def api_discharge(req, patient_id):
    u = req.user
    conn = db.connect()
    try:
        n = services.discharge_patient(conn, u, patient_id)
        return {"ok": True, "revoked": n}
    finally:
        conn.close()


@route("POST", "/api/patients/<int:patient_id>/transfer")
def api_transfer(req, patient_id):
    u = req.user
    body = req.json
    conn = db.connect()
    try:
        n = services.transfer_patient(conn, u, patient_id,
                                      int(body.get("to_ward_id")),
                                      (body.get("bed_no") or "").strip())
        return {"ok": True, "suspended": n}
    finally:
        conn.close()


@route("POST", "/api/ward/lockdown")
def api_lockdown(req):
    u = req.user
    conn = db.connect()
    try:
        n = services.set_lockdown(conn, u, u["ward_id"], bool(req.json.get("on")))
        return {"ok": True, "revoked": n}
    finally:
        conn.close()


# ---------------------------------------------------------------- API：门岗

@route("POST", "/api/gate/scan")
def api_gate_scan(req):
    u = req.user
    services.require_role(u, services.ROLE_GATE)
    body = req.json
    code = (body.get("code") or "").strip().upper()
    if not code:
        raise services.ServiceError("请输入或扫描通行码")
    gate = (body.get("gate") or u["gate_name"] or "东门").strip()
    direction = body.get("direction") or "auto"
    adapter = SimulatedGateAdapter(gate, direction)  # 门禁走模拟适配器
    return adapter.scan(code)


@route("GET", "/api/gate/recent")
def api_gate_recent(req):
    u = req.user
    services.require_role(u, services.ROLE_GATE)
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT pg.*, ps.escort_id FROM passages pg"
            " LEFT JOIN passes ps ON ps.id=pg.pass_id"
            " ORDER BY pg.id DESC LIMIT 20").fetchall()
        out = []
        for r in rows:
            escort_code = None
            if r["escort_id"]:
                e = conn.execute("SELECT code_name FROM escorts WHERE id=?",
                                 (r["escort_id"],)).fetchone()
                escort_code = e["code_name"] if e else None
            out.append(dict(scanned_at=r["scanned_at"], gate=r["gate_name"],
                            code=r["code"], direction=r["direction"],
                            result=r["result"], deny_reason=r["deny_reason"],
                            escort_code=escort_code))
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------- API：看板与记录

@route("GET", "/api/board")
def api_board(req):
    u = req.user
    if not u:
        raise services.ServiceError("未登录", 401)
    conn = db.connect()
    try:
        if u["role"] == services.ROLE_GATE:
            wards = conn.execute("SELECT * FROM wards ORDER BY id").fetchall()
        else:
            wards = conn.execute("SELECT * FROM wards WHERE id=?",
                                 (u["ward_id"],)).fetchall()
        today = services.now_str()[:10]
        blocks = []
        for w in wards:
            inside = conn.execute(
                "SELECT ps.code, ps.id AS pass_id, e.code_name AS escort_code,"
                " e.relationship, p.bed_no, p.code_name AS patient_code,"
                " (SELECT MAX(scanned_at) FROM passages"
                "   WHERE pass_id=ps.id AND direction='in' AND result='allowed'"
                " ) AS last_in"
                " FROM passes ps JOIN escorts e ON e.id=ps.escort_id"
                " JOIN patients p ON p.id=ps.patient_id"
                " WHERE ps.ward_id=? AND ps.inside=1 AND ps.status='active'",
                (w["id"],)).fetchall()
            pending = conn.execute(
                "SELECT COUNT(*) c FROM applications a JOIN patients p"
                " ON p.id=a.patient_id WHERE p.ward_id=? AND a.status='pending'",
                (w["id"],)).fetchone()["c"]
            suspended = conn.execute(
                "SELECT COUNT(*) c FROM passes WHERE ward_id=? AND status='suspended'",
                (w["id"],)).fetchone()["c"]
            revoked_today = conn.execute(
                "SELECT COUNT(*) c FROM passes WHERE ward_id=? AND status='revoked'"
                " AND revoked_at LIKE ?",
                (w["id"], today + "%")).fetchone()["c"]
            blocks.append(dict(
                ward_id=w["id"], ward_name=w["name"], locked_down=w["locked_down"],
                inside=[dict(r) for r in inside], pending=pending,
                suspended=suspended, revoked_today=revoked_today))
        events = conn.execute(
            "SELECT e.*, w.name AS ward_name FROM events e"
            " LEFT JOIN wards w ON w.id=e.ward_id ORDER BY e.id DESC LIMIT 40"
        ).fetchall()
        return {"wards": blocks,
                "events": [dict(
                    id=r["id"], type=r["type"], actor=r["actor"],
                    detail=r["detail"], ward_name=r["ward_name"],
                    created_at=r["created_at"]) for r in events]}
    finally:
        conn.close()


@route("GET", "/api/passages")
def api_passages(req):
    u = req.user
    if not u:
        raise services.ServiceError("未登录", 401)
    conn = db.connect()
    try:
        if u["role"] == services.ROLE_GATE:
            rows = conn.execute(
                "SELECT pg.*, e.code_name AS escort_code, w.name AS ward_name"
                " FROM passages pg"
                " LEFT JOIN passes ps ON ps.id=pg.pass_id"
                " LEFT JOIN escorts e ON e.id=ps.escort_id"
                " LEFT JOIN wards w ON w.id=ps.ward_id"
                " ORDER BY pg.id DESC LIMIT 200").fetchall()
        else:
            rows = conn.execute(
                "SELECT pg.*, e.code_name AS escort_code, w.name AS ward_name"
                " FROM passages pg"
                " LEFT JOIN passes ps ON ps.id=pg.pass_id"
                " LEFT JOIN escorts e ON e.id=ps.escort_id"
                " LEFT JOIN wards w ON w.id=ps.ward_id"
                " WHERE ps.ward_id=? OR ps.ward_id IS NULL"
                " ORDER BY pg.id DESC LIMIT 200", (u["ward_id"],)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@route("GET", "/api/revocations")
def api_revocations(req):
    u = req.user
    services.require_role(u, services.ROLE_NURSE, services.ROLE_HEAD)
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT ps.code, ps.revoked_at, ps.revoke_reason,"
            " e.code_name AS escort_code, e.name AS escort_name,"
            " p.bed_no, p.name AS patient_name"
            " FROM passes ps JOIN escorts e ON e.id=ps.escort_id"
            " JOIN patients p ON p.id=ps.patient_id"
            " WHERE ps.ward_id=? AND ps.status='revoked'"
            " ORDER BY ps.revoked_at DESC LIMIT 100", (u["ward_id"],)).fetchall()
        return [dict(code=r["code"], revoked_at=r["revoked_at"],
                     revoke_reason=r["revoke_reason"],
                     escort_code=r["escort_code"],
                     escort_name_masked=services.mask_name(r["escort_name"]),
                     bed_no=r["bed_no"], patient_name=r["patient_name"])
                for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------- 静态与分发

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.extra_headers = []

    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        from urllib.parse import unquote, urlparse
        self.extra_headers = []  # setup() 每连接只调一次，响应头需按请求重置
        path = unquote(urlparse(self.path).path)
        try:
            if path.startswith("/api/"):
                self._handle_api(method, path)
            else:
                self._handle_static(path)
        except services.ServiceError as e:
            self._send_json({"error": e.message}, e.status)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self._send_json({"error": f"服务器内部错误：{e}"}, 500)

    def _handle_api(self, method, path):
        req = Req(self)
        for m, regex, fn in ROUTES:
            if m != method:
                continue
            match = regex.match(path)
            if match:
                kwargs = {k: int(v) for k, v in match.groupdict().items()}
                result = fn(req, **kwargs)
                self._send_json(result if result is not None else {"ok": True})
                return
        self._send_json({"error": "接口不存在"}, 404)

    def _handle_static(self, path):
        if path in PAGE_MAP:
            return self._send_file(os.path.join(STATIC_DIR, PAGE_MAP[path]))
        if path.startswith("/static/"):
            return self._send_file(os.path.join(STATIC_DIR, path[len("/static/"):]))
        if path.startswith("/id_images/"):
            return self._send_file(os.path.join(db.ID_IMAGE_DIR, path[len("/id_images/"):]))
        self._send_json({"error": "页面不存在"}, 404)

    def _send_file(self, full):
        full = os.path.normpath(full)
        if not (full.startswith(os.path.normpath(STATIC_DIR))
                or full.startswith(os.path.normpath(db.ID_IMAGE_DIR))):
            return self._send_json({"error": "禁止访问"}, 403)
        if not os.path.isfile(full):
            return self._send_json({"error": "文件不存在"}, 404)
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in getattr(self, "extra_headers", []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)


def serve(port=8000):
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"病区陪护通行码系统已启动：http://localhost:{port}")
    print("演示账号见登录页；按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
