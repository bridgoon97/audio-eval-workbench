"""在当前操作系统构建含前端和 Python 运行时的目录包。"""

import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
if not (root / "dist" / "index.html").exists():
    raise SystemExit("请先运行 npm run build")
subprocess.run(
    [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        "audio-eval",
        "--onedir",
        "--distpath",
        "release",
        "--add-data",
        f"dist{os.pathsep}dist",
        "--collect-all",
        "soundfile",
        "--collect-all",
        "_soundfile_data",
        "--collect-submodules",
        "uvicorn",
        "launcher.py",
    ],
    cwd=root,
    check=True,
)
