"""在当前操作系统构建含前端和 Python 运行时的目录包。"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Windows CI/控制台的 cp1252 输出无法编码中文提示；固定 UTF-8。
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")

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
        "--collect-all",
        "soxr",
        "--collect-submodules",
        "uvicorn",
        "launcher.py",
    ],
    cwd=root,
    check=True,
)

# ---------- 完整服务端包内的 Agent 使用文档 ----------
# 供同机 Agent 阅读：清单结构、示例与双入口命令映射（完整服务端
# audio-eval.exe agent ... 与独立 CLI 包 audio-eval-agent.exe ... 语法不同）。
package_root = root / "release" / "audio-eval"
package_root.mkdir(parents=True, exist_ok=True)
agent_docs_target = package_root / "docs"
agent_docs_target.mkdir(parents=True, exist_ok=True)

required_agent_docs = [
    root / "AGENTS.md",
    root / "docs" / "Agent创建评测任务.md",
    root / "docs" / "task-manifest.schema.json",
    root / "docs" / "task-manifest.example.json",
]
for doc in required_agent_docs:
    if not doc.is_file():
        raise SystemExit(f"Agent 文档缺失，无法打包：{doc}")
    target = package_root / doc.relative_to(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(doc, target)

entry_text = f"""Agent 使用入口 · 完整服务端包
==============================

本目录是完整服务端 Windows 包：包含评测服务（audio-eval.exe）、网页界面，
以及同机 Agent 编排所需的全部 Agent 子命令与文档。

给通用 Agent / 自动化程序的要点
------------------------------

1. Agent 是外部自动化程序（例如编码 Agent），通过终端调用本包的命令行；
   网页界面不会自动运行 Agent，也不需要在网页上做任何额外开启。
2. 同机 Agent 直接使用完整服务端包内的 agent 子命令，
   无需另装独立 Agent CLI 包（audio-eval-agent.exe）。
3. 完整服务端命令前缀为：audio-eval.exe agent ...
   （独立 Agent CLI 包的程序前缀是 audio-eval-agent.exe ...，两者语法不同，
   不要混用；本说明中的全部命令均为完整服务端语法。）

五条核心命令（在本目录下打开终端执行；Windows 程序名为 audio-eval.exe）：

  audio-eval.exe agent manifest-init --output manifest.json
  audio-eval.exe agent validate manifest.json
  audio-eval.exe agent prepare manifest.json --output-dir prepared
  audio-eval.exe agent apply manifest.json --server <服务地址> \
      --user <账号> --password-stdin --state agent-state.json \
      --mapping prepared/mapping.json --json
  audio-eval.exe agent status --state agent-state.json --json

安全说明：

- apply 默认只允许本机（127.0.0.1/localhost）或 HTTPS 服务地址；
  访问局域网明文 HTTP 必须显式加 --allow-insecure-http
  （凭据与音频不加密传输，仅限获准的可信内网）。
- 密码只允许 --password-stdin（终端输入，不回显）或 --password-env <变量名>；
  没有 --password 参数，密码不会进入命令行历史。
- 发布需要双确认：manifest.json 的 publish=true 且命令行加 --publish。

详细手册、清单结构与协作规则见同目录：

  docs/Agent创建评测任务.md
  AGENTS.md
  docs/task-manifest.schema.json
  docs/task-manifest.example.json

版本：{__import__("workbench.version", fromlist=["VERSION"]).VERSION}
"""
entry_path = package_root / "Agent使用入口.txt"
entry_path.write_text(entry_text, encoding="utf-8")

print(f"Agent 文档与使用入口已复制：{entry_path.parent}")
