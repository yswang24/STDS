"""Step 0 验收:确认能连到 stms.db 并读到核心表的行数。"""
from __future__ import annotations

from stds.data.db import get_conn


def test_db_connect_and_counts():
    con = get_conn()
    tabs = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    assert set(["formula", "formula_chart", "stds_record", "common_chart"]).issubset(set(tabs))
    assert con.execute("SELECT COUNT(*) FROM formula").fetchone()[0] == 3322
    assert con.execute("SELECT COUNT(*) FROM stds_record").fetchone()[0] == 566
    assert con.execute("SELECT COUNT(*) FROM formula_chart").fetchone()[0] == 64
    con.close()
