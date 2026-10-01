"""SQLite 数据层：病区陪护通行码授权链。

所有授权状态（申请 -> 审批 -> 发证 -> 通行 -> 撤销/冻结）都落在本库的
events 表中，形成可追溯的授权链。连接使用 WAL + busy_timeout，
入区/离区状态翻转依赖条件 UPDATE 的原子性（见 services.verify_scan）。
"""
import os
import sqlite3
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.environ.get("WARDPASS_DB", os.path.join(DATA_DIR, "wardpass.db"))
ID_IMAGE_DIR = os.path.join(DATA_DIR, "id_images")

SCHEMA = """
CREATE TABLE IF NOT EXISTS wards (
    id          INTEGER PRIMARY KEY,
    code        TEXT UNIQUE NOT NULL,      -- 病区代码 A/B
    name        TEXT NOT NULL,             -- 内科一病区
    locked_down INTEGER NOT NULL DEFAULT 0 -- 封控中：暂停一切陪护通行
);

CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY,
    username     TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    role         TEXT NOT NULL,            -- nurse / head_nurse / gate
    ward_id      INTEGER REFERENCES wards(id),  -- 门岗为 NULL
    gate_name    TEXT                      -- 门岗所属闸机名
);

CREATE TABLE IF NOT EXISTS patients (
    id            INTEGER PRIMARY KEY,
    ward_id       INTEGER NOT NULL REFERENCES wards(id),
    bed_no        TEXT NOT NULL,
    name          TEXT NOT NULL,
    code_name     TEXT NOT NULL,           -- 患者代号（门岗等场景展示用）
    status        TEXT NOT NULL DEFAULT 'admitted',  -- admitted / discharged
    discharged_at TEXT
);

CREATE TABLE IF NOT EXISTS escorts (
    id           INTEGER PRIMARY KEY,
    patient_id   INTEGER NOT NULL REFERENCES patients(id),
    name         TEXT NOT NULL,
    code_name    TEXT NOT NULL,            -- 陪护代号，如 陪A-03
    relationship TEXT NOT NULL,
    id_number    TEXT NOT NULL,            -- 虚构证件号（完整，仅授权查看）
    id_masked    TEXT NOT NULL,            -- 遮罩证件号
    id_image     TEXT,                     -- 本地遮罩样图路径
    active       INTEGER NOT NULL DEFAULT 1 -- 陪护人更换后置 0
);

CREATE TABLE IF NOT EXISTS applications (
    id                  INTEGER PRIMARY KEY,
    patient_id          INTEGER NOT NULL REFERENCES patients(id),
    escort_id           INTEGER NOT NULL REFERENCES escorts(id),
    kind                TEXT NOT NULL DEFAULT 'new',  -- new / replace
    replaces_escort_id  INTEGER REFERENCES escorts(id),
    windows             TEXT NOT NULL,       -- JSON ["08:00-12:00", ...]
    reason              TEXT,
    status              TEXT NOT NULL DEFAULT 'pending', -- pending/approved/rejected
    created_by          INTEGER NOT NULL,
    created_at          TEXT NOT NULL,
    reviewed_by         INTEGER,
    reviewed_at         TEXT,
    review_note         TEXT
);

CREATE TABLE IF NOT EXISTS passes (
    id            INTEGER PRIMARY KEY,
    code          TEXT UNIQUE NOT NULL,    -- 通行码 P-XXXXXX
    application_id INTEGER NOT NULL REFERENCES applications(id),
    escort_id     INTEGER NOT NULL REFERENCES escorts(id),
    patient_id    INTEGER NOT NULL REFERENCES patients(id),
    ward_id       INTEGER NOT NULL REFERENCES wards(id),
    windows       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active', -- active/suspended(转科待评估)/revoked
    inside        INTEGER NOT NULL DEFAULT 0,     -- 当前是否在区内（原子翻转）
    issued_by     INTEGER NOT NULL,
    issued_at     TEXT NOT NULL,
    revoked_at    TEXT,
    revoke_reason TEXT
);

CREATE TABLE IF NOT EXISTS passages (
    id          INTEGER PRIMARY KEY,
    pass_id     INTEGER REFERENCES passes(id),  -- 未知码为 NULL
    code        TEXT NOT NULL,
    gate_name   TEXT NOT NULL,
    direction   TEXT,                            -- in / out（拒绝时记录意图）
    result      TEXT NOT NULL,                   -- allowed / denied
    deny_reason TEXT,
    scanned_at  TEXT NOT NULL
);

-- 授权链：每一次状态变更一条事件，交接看板据此回溯
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    ward_id    INTEGER,
    patient_id INTEGER,
    pass_id    INTEGER,
    actor      TEXT NOT NULL,
    type       TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_passes_patient ON passes(patient_id, status);
CREATE INDEX IF NOT EXISTS idx_passages_time ON passages(scanned_at);
CREATE INDEX IF NOT EXISTS idx_events_ward ON events(ward_id, id);
"""


def connect(path=None):
    """打开一个连接。autocommit 模式；多语句写请用 tx() 显式事务。"""
    p = path or DB_PATH
    if p != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    conn = sqlite3.connect(p, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # autocommit：单条 UPDATE 即原子事务
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def tx(conn):
    """多语句写事务（BEGIN IMMEDIATE 立即取写锁，避免升级死锁）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db(conn):
    conn.executescript(SCHEMA)


def get_config(conn, key, default=None):
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_config(conn, key, value):
    conn.execute(
        "INSERT INTO config(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
