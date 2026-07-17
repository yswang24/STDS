"""只读 DB 连接封装。stms.db 为 root 只读,这里只读访问。"""
from __future__ import annotations

import sqlite3

from stds.config.settings import settings


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
