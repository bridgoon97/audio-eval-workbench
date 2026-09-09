"""构建独立 Agent CLI 分发目录（不含服务端/前端）。

产物：release-agent/audio-eval-agent/（Windows 下为 audio-eval-agent.exe）
依赖收集只覆盖 CLI 运行所需（soundfile/numpy/soxr/jsonschema/httpx）；
fastapi/uvicorn/前端不在入口依赖图中，不会被打进包。
文档（AGENTS.md 与 docs/Agent创建评测任务.md 等）复制到包根目录。
"""

import shutil
import subprocess
import sys
from pathlib import Path

# Windows CI/控制台的 cp1252 输出无法编码中文提示；固定 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='backslashreplace')

root = Path(__file__).resolve().parent.parent
if not (root / "dist" / "index.html").exists():
    # Agent CLI 不含前端；dist 只作为"服务端已构建"的仓库一致性提示，不进包。
    print("提示：尚未构建服务端前端；独立 Agent CLI 打包不依赖 dist。")

subprocess.run(
    [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        "audio-eval-agent",
        "--onedir",
        "--distpath",
        "release-agent",
        "--workpath",
        "release-agent-build",
        "--specpath",
        "release-agent-build",
        "--collect-all",
        "soundfile",
        "--collect-all",
        "_soundfile_data",
        "--collect-all",
        "soxr",
        # httpx 在 agent_tasks 中为函数内导入，PyInstaller 静态分析收集不到。
        "--collect-all",
        "httpx",
        "--collect-all",
        "httpcore",
        "--collect-all",
        "h11",
        "--collect-all",
        "anyio",
        "--collect-all",
        "certifi",
        "agent_entry.py",
    ],
    cwd=root,
    check=True,
)

package_root = root / "release-agent" / "audio-eval-agent"
docs_target = package_root / "docs"
docs_target.mkdir(parents=True, exist_ok=True)
shutil.copy2(root / "AGENTS.md", package_root / "AGENTS.md")
for doc in (
    "Agent创建评测任务.md",
    "task-manifest.schema.json",
    "task-manifest.example.json",
):
    shutil.copy2(root / "docs" / doc, docs_target / doc)

usage = f"""开始使用 · 听鉴独立 Agent CLI
==============================

本包是评测任务编排命令行工具，供同事或其 Agent 单独下载使用；
不需要安装服务端、Python、uv 或 Git，也不包含评测服务本身。

评测服务运行在组织者的电脑上；请向组织者索取：
1. 服务地址（例如 http://192.168.x.x:8765 或 https://…）；
2. 一个具有组织者或管理员权限的账号；
3. 已按约定整理好的音频素材目录。

最小命令（在本目录下打开终端执行；Windows 为 audio-eval-agent.exe）：

  audio-eval-agent manifest-init --output manifest.json
  audio-eval-agent validate manifest.json
  audio-eval-agent prepare manifest.json --output-dir prepared
  audio-eval-agent apply manifest.json --server <服务地址> \\
      --user <账号> --password-stdin --state agent-state.json \\
      --mapping prepared/mapping.json --json
  audio-eval-agent status --state agent-state.json --json

安全说明：
- 密码只允许 --password-stdin（终端输入，不回显）或 --password-env <变量名>；
  没有 --password 参数，密码不会进入命令行历史。
- 默认只允许本机（127.0.0.1/localhost）或 HTTPS 服务地址；
  访问局域网明文 HTTP 必须显式加 --allow-insecure-http
  （凭据与音频不加密传输，仅限获准的可信内网）。
- 发布需要双确认：manifest.json 的 publish=true 且命令行加 --publish。

完整流程、确认清单、失败恢复与安全边界见同目录：
  docs/Agent创建评测任务.md
  AGENTS.md
  docs/task-manifest.schema.json
  docs/task-manifest.example.json

版本：{__import__("workbench.version", fromlist=["VERSION"]).VERSION}
"""
(package_root / "开始使用.txt").write_text(usage, encoding="utf-8")

print(f"独立 Agent CLI 包已生成：{package_root}")
