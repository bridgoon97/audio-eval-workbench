"""终端流编码配置：供服务端与独立 Agent CLI 两个入口共用。

本模块刻意保持零依赖（仅标准库），独立 Agent CLI 的入口
不得经由本文件触达服务端依赖（fastapi/uvicorn 等）。
"""

import sys


def configure_streams() -> None:
    """Windows 重定向输出可能采用 cp1252；中文输出必须可编码。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
