"""Step 13:records 状态表(复核队列)。

用独立 SQLite 存记录状态(不碰 stms.db,它是只读的)。
四态:pending -> computing -> need_review / done
复核队列 = status='need_review'
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from stds.config.settings import settings

_STATE_DB = Path(__file__).resolve().parents[2] / "stds_state.db"  # 放在 stds_project/ 下(可写)


class RecordStatus:
    PENDING = "pending"
    COMPUTING = "computing"
    NEED_REVIEW = "need_review"
    DONE = "done"


def _get_state_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_STATE_DB), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS records (
            job_id     TEXT,
            record_no  INTEGER,
            status     TEXT DEFAULT 'pending',
            result_json TEXT,
            error      TEXT,
            updated_at REAL,
            PRIMARY KEY (job_id, record_no)
        )
        """
    )
    conn.commit()
    return conn


class StateManager:
    def __init__(self):
        self.conn = _get_state_conn()

    def mark(self, job_id: str, record_no: int, status: str, result=None, error: str = None):
        result_json = json.dumps(result, default=str, ensure_ascii=False) if result else None
        self.conn.execute(
            """
            INSERT INTO records (job_id, record_no, status, result_json, error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, record_no) DO UPDATE SET
                status=excluded.status, result_json=excluded.result_json,
                error=excluded.error, updated_at=excluded.updated_at
            """,
            (job_id, record_no, status, result_json, error, time.time()),
        )
        self.conn.commit()

    def mark_many(self, job_id: str, elements: list, status: str):
        now = time.time()
        for el in elements:
            self.conn.execute(
                """
                INSERT INTO records (job_id, record_no, status, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id, record_no) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at
                """,
                (job_id, el.number, status, now),
            )
        self.conn.commit()

    def list_by_status(self, job_id: str, status: str) -> list:
        rows = self.conn.execute(
            "SELECT * FROM records WHERE job_id=? AND status=? ORDER BY record_no",
            (job_id, status),
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, job_id: str, record_no: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM records WHERE job_id=? AND record_no=?",
            (job_id, record_no),
        ).fetchone()
        return dict(row) if row else None

    def cleanup_old_jobs(self, ttl_days: int = 7) -> int:
        """清理超过 ttl_days 天的旧记录,返回删除条数。"""
        import time
        cutoff = time.time() - ttl_days * 86400
        cur = self.conn.execute("DELETE FROM records WHERE updated_at < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount

    def close(self):
        self.conn.close()
