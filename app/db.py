"""SQLite 连接与授权链 schema。

授权链以 passes.parent_pass_id（转科重评）与 passes.replaced_by_pass_id
（换陪护/换设备/换码）表达：每次旧码被新码取代，旧码即 REPLACED，
授权来源可沿 parent 链回溯。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("WARD_DB", os.path.join(os.path.dirname(__file__), "..", "data", "ward.db"))
SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS wards (
    code        TEXT PRIMARY KEY,          -- 病区代码，如 HN（呼吸内科）
    name        TEXT NOT NULL,
    locked      INTEGER NOT NULL DEFAULT 0,-- 病区封控
    locked_at   TEXT,
    locked_reason TEXT
);

CREATE TABLE IF NOT EXISTS staff (
    id          INTEGER PRIMARY KEY,
    username    TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('head_nurse','nurse')),
    ward_code   TEXT NOT NULL REFERENCES wards(code)
);

CREATE TABLE IF NOT EXISTS gates (
    id          INTEGER PRIMARY KEY,
    code        TEXT UNIQUE NOT NULL,       -- 门岗编号
    name        TEXT NOT NULL,
    ward_code   TEXT NOT NULL REFERENCES wards(code)
);

CREATE TABLE IF NOT EXISTS beds (
    id          INTEGER PRIMARY KEY,
    ward_code   TEXT NOT NULL REFERENCES wards(code),
    bed_no      TEXT NOT NULL,              -- 床号，如 12
    UNIQUE (ward_code, bed_no)
);

CREATE TABLE IF NOT EXISTS patients (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    alias_code  TEXT UNIQUE NOT NULL,       -- 大字展示用代号，如 A-0217
    current_ward TEXT REFERENCES wards(code),
    bed_id      INTEGER REFERENCES beds(id),
    status      TEXT NOT NULL DEFAULT 'admitted'
                CHECK (status IN ('admitted','transferred','discharged')),
    discharged_at TEXT
);

CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY,
    patient_id      INTEGER NOT NULL REFERENCES patients(id),
    ward_code       TEXT NOT NULL REFERENCES wards(code),  -- 提交时所在病区
    companion_name  TEXT NOT NULL,
    relationship    TEXT NOT NULL,          -- 子女/配偶/护工…
    id_type         TEXT NOT NULL,          -- 身份证/护照/其他
    id_number       TEXT NOT NULL,          -- 完整号码（仅库内，界面一律遮罩）
    phone           TEXT NOT NULL,
    window_start    TEXT NOT NULL,          -- HH:MM
    window_end      TEXT NOT NULL,          -- HH:MM
    quota           INTEGER NOT NULL DEFAULT 1,  -- 该患者限制时段允许发证上限
    source_application_id INTEGER REFERENCES applications(id), -- 转科重评引用的旧申请
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','rejected','voided')),
    reject_reason   TEXT,
    created_by      INTEGER NOT NULL REFERENCES staff(id),
    reviewed_by     INTEGER REFERENCES staff(id),
    created_at      TEXT NOT NULL,
    reviewed_at     TEXT
);

CREATE TABLE IF NOT EXISTS passes (
    id              INTEGER PRIMARY KEY,
    code_hash       TEXT UNIQUE NOT NULL,   -- 通行码只存哈希
    code_mask       TEXT NOT NULL,          -- 如 WXK-••••-3291（不回显明文）
    application_id  INTEGER NOT NULL REFERENCES applications(id),
    patient_id      INTEGER NOT NULL REFERENCES patients(id),
    ward_code       TEXT NOT NULL REFERENCES wards(code),
    companion_name  TEXT NOT NULL,
    alias_code      TEXT NOT NULL,
    window_start    TEXT NOT NULL,
    window_end      TEXT NOT NULL,
    status          TEXT NOT NULL
                    CHECK (status IN ('active','revoked','replaced','expired')),
    revoke_reason   TEXT,                   -- 换陪护/手机丢失/码泄露/封控/出院/转科
    revoked_at      TEXT,
    parent_pass_id  INTEGER REFERENCES passes(id),  -- 转科重评的旧码
    replaced_by_pass_id INTEGER REFERENCES passes(id),
    in_zone         INTEGER NOT NULL DEFAULT 0,    -- 当前是否在病区内
    last_zone_change_at TEXT,
    last_gate_code  TEXT,                          -- 最近一次状态变更门岗（防重放提示）
    issued_at       TEXT NOT NULL,
    issued_by       INTEGER NOT NULL REFERENCES staff(id)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    pass_id     INTEGER REFERENCES passes(id),
    patient_id  INTEGER REFERENCES patients(id),
    ward_code   TEXT NOT NULL,
    gate_code   TEXT,
    actor       TEXT NOT NULL,              -- nurse / gate:G1 / system
    kind        TEXT NOT NULL,              -- 见 EventKind
    detail      TEXT,
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_pass ON events(pass_id);
CREATE INDEX IF NOT EXISTS idx_events_patient ON events(patient_id);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # isolation_level=None：显式事务控制，scan 用 BEGIN IMMEDIATE 做并发仲裁
    conn = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def immediate(conn: sqlite3.Connection):
    """写事务：立即加写锁，与 scan 的仲裁事务互斥。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
