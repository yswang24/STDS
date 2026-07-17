"""统一日志配置:控制台 + 文件,按模块分级。"""
from __future__ import annotations

import logging
from pathlib import Path


def setup_logging(level: str = "DEBUG", log_file: str = "stds_debug.log"):
    """初始化日志:控制台 INFO + 文件 DEBUG。"""
    log_path = Path(__file__).parent.parent.parent / log_file

    root = logging.getLogger("stds")
    root.setLevel(getattr(logging, level.upper(), logging.DEBUG))

    # 清除已有 handler(避免重复)
    root.handlers.clear()

    # 控制台:INFO 级别(不刷屏)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(console)

    # 文件:DEBUG 级别(全链路)
    file_h = logging.FileHandler(str(log_path), mode="a", encoding="utf-8")
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(file_h)

    logging.getLogger("stds").info(f"日志已初始化: {log_path}")
