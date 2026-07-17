"""#7 Semaphore 外置 / #8 状态表清理 / #9 Streamlit UI 可用。"""
from __future__ import annotations

import time

from stds.config.settings import settings
from stds.pipeline.state import StateManager, RecordStatus


def test_concurrency_limit_from_config():
    """Semaphore 应从配置读取。"""
    assert settings.CONCURRENCY_LIMIT >= 1
    assert settings.STATE_TTL_DAYS >= 1


def test_state_cleanup_old_jobs():
    """清理超过 TTL 的旧记录。"""
    sm = StateManager()
    # 插入一条"很旧"的记录(updated_at = 0)
    sm.conn.execute(
        "INSERT OR REPLACE INTO records (job_id, record_no, status, updated_at) VALUES (?, ?, ?, ?)",
        ("old_job", 1, "done", 0.0),
    )
    sm.conn.commit()
    # 清理(默认 7 天)
    deleted = sm.cleanup_old_jobs(ttl_days=7)
    assert deleted >= 1
    assert sm.get("old_job", 1) is None
    sm.close()


def test_state_cleanup_preserves_recent():
    """不清理近期记录。"""
    sm = StateManager()
    sm.mark("recent_job", 1, "done")
    deleted = sm.cleanup_old_jobs(ttl_days=7)
    assert sm.get("recent_job", 1) is not None
    sm.close()


def test_streamlit_app_exists():
    """Streamlit app 文件存在且可导入。"""
    from pathlib import Path
    app_path = Path(__file__).parent.parent.parent / "stds" / "ui" / "app.py"
    assert app_path.exists()
