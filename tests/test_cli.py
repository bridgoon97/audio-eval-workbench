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
        [
            "audio-eval",
            "--data",
            str(tmp_path / "中文目录"),
            "--no-browser",
            "--port",
            "0",
        ],
    )
    monkeypatch.setattr(
        "workbench.cli.uvicorn.Server.run", lambda *args, **kwargs: None
    )
    main()
    stream.flush()
    assert "听鉴" in output.getvalue().decode("utf-8")
    assert "中文目录" in output.getvalue().decode("utf-8")


def test_busy_port_never_opens_old_service(tmp_path, monkeypatch):
    import socket
    from unittest.mock import Mock

    import pytest

    opened = Mock()
    monkeypatch.setattr("workbench.cli.webbrowser.open", opened)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        data = tmp_path / "不会创建"
        monkeypatch.setattr(
            sys, "argv", ["audio-eval", "--data", str(data), "--port", str(port)]
        )
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 1
        opened.assert_not_called()
        assert not data.exists()
