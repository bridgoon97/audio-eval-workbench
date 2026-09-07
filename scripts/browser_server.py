"""仅浏览器测试使用：临时数据目录、固定本机端口。"""

import os
import sys
import tempfile
from pathlib import Path

import uvicorn

from workbench.app import create_app

with tempfile.TemporaryDirectory(prefix="audio-eval-e2e-") as root:
    app = create_app(Path(root), Path(__file__).resolve().parent.parent / "dist")
    app.state.setup_file.write_text("browser-test-setup-key", encoding="utf-8")
    port = int(os.environ.get("E2E_PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8877))
    uvicorn.run(app, host="127.0.0.1", port=port)
