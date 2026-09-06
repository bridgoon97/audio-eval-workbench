"""复现非中文 Windows 输出编码，防止打包程序在启动提示时退出。"""

import io
import sys

from workbench.cli import main


def test_chinese_startup_with_cp1252_output(tmp_path, monkeypatch):
    output = io.BytesIO()
    stream = io.TextIOWrapper(output, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(
        sys,
        "argv",
        ["audio-eval", "--data", str(tmp_path / "中文目录"), "--no-browser"],
    )
    monkeypatch.setattr("workbench.cli.uvicorn.run", lambda *args, **kwargs: None)
    main()
    stream.flush()
    assert "听鉴" in output.getvalue().decode("utf-8")
    assert "中文目录" in output.getvalue().decode("utf-8")
