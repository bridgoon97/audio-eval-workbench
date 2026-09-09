"""启动真实服务端打包产物，核对接口和网页可用，然后正常终止测试进程。

独立 Agent CLI 包的 smoke 见 scripts/package_agent_smoke.py（缺包必然失败）。"""

import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")

name = "audio-eval.exe" if sys.platform == "win32" else "audio-eval"
with tempfile.TemporaryDirectory(prefix="audio-eval-package-") as data:
    process = subprocess.Popen(
        [
            str(Path("release/audio-eval") / name),
            "--no-browser",
            "--data",
            data,
            "--port",
            "8878",
        ]
    )
    try:
        for _ in range(40):
            if process.poll() is not None:
                raise RuntimeError("打包程序提前退出")
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:8878/api/status", timeout=1
                ) as response:
                    assert json.load(response)["needs_setup"] is True
                break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("打包程序未启动")
        with urllib.request.urlopen("http://127.0.0.1:8878", timeout=2) as response:
            assert "听鉴" in response.read().decode()
        print("打包产物启动及界面检查通过")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
