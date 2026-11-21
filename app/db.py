"""SQLite 存储层。DB_PATH 可用环境变量覆盖，测试时替换为临时库。"""

import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "recs_eval.db")
DEFAULT_DATASET_ID = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    event_no TEXT NOT NULL,
    item_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    clicked INTEGER NOT NULL,
    propensity REAL NOT NULL,
    new_rank INTEGER,
    UNIQUE (dataset_id, event_no)
);
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    new_rank INTEGER NOT NULL,
    UNIQUE (dataset_id, session_id, item_id)
);
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id INTEGER NOT NULL,
    cap REAL NOT NULL,
    min_ess REAL NOT NULL,
    created_at TEXT NOT NULL,
    result_json TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO datasets (id, name, created_at) VALUES (?, 'default', ?)",
            (DEFAULT_DATASET_ID, utcnow()),
        )
        conn.commit()
    finally:
        conn.close()
