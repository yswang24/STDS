"""数据访问层:参数化 SQL,隔离 stms.db 读取。"""
from __future__ import annotations

from stds.data.db import get_conn
from stds.domain.models import StdsElement


def load_records_by_station(line: str, station: str) -> list:
    """按产线+工位加载待分析元素(参数化 SQL,无注入)。"""
    con = get_conn()
    rows = con.execute(
        """
        SELECT 序号,操作内容,动作代码,决策描述,频率,时间,工位,项目名称
        FROM stds_record WHERE 工位=? AND 项目名称=?
        ORDER BY 排序号
        """,
        (station, line),
    ).fetchall()
    con.close()
    result = []
    for r in rows:
        result.append(
            StdsElement(
                number=r["序号"],
                operation_des=r["操作内容"] or "",
                line_name=r["项目名称"] or "",
                station_op=r["工位"] or "",
                freq=float(r["频率"] or 1.0),
                norm_key=(r["操作内容"] or "").strip(),
            )
        )
    return result


def load_edited_history() -> list:
    """T1 kNN 用:仅取 已人工编辑='是' 的记录(chartcode+决策描述可信)。"""
    con = get_conn()
    rows = con.execute(
        """
        SELECT 操作内容,动作代码,决策描述 FROM stds_record
        WHERE 已人工编辑='是' AND 动作代码 IS NOT NULL AND 动作代码!=''
        """
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]
