"""独立 Agent CLI 包 smoke：验证打包产物可执行、内容齐全且无服务端组件。

与 scripts/package_smoke.py（服务端包）分开运行；**缺包必然非零失败**，
不存在静默跳过路径。不依赖源码树或系统 Python（合成素材由本脚本生成）。
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")

import numpy as np
import soundfile as sfile

agent_dir = Path("release-agent/audio-eval-agent")
if not agent_dir.is_dir():
    raise RuntimeError(
        "未找到独立 Agent CLI 包（release-agent/audio-eval-agent）；"
        "请先运行 scripts/package_agent.py。缺包不允许通过 smoke。"
    )
agent_exe_name = "audio-eval-agent.exe" if sys.platform == "win32" else "audio-eval-agent"
agent_exe = agent_dir / agent_exe_name
if not agent_exe.is_file():
    raise RuntimeError(f"可执行文件缺失：{agent_exe}")


def agent(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(agent_exe), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )


# --help：可执行且帮助可用。
help_result = agent("--help")
if help_result.returncode != 0 or "manifest-init" not in help_result.stdout:
    raise RuntimeError(f"agent --help 异常：{help_result.stderr[:200]}")

# serve 子命令不可用（独立 CLI 不含服务端，绝不监听端口）。
serve_result = agent("serve")
combined = serve_result.stderr + serve_result.stdout
if serve_result.returncode == 0 or "invalid choice" not in combined:
    raise RuntimeError("独立 CLI 不应提供 serve 子命令")

# 解包内容：无服务端组件（fastapi/uvicorn/starlette）。
internal = agent_dir / "_internal"
for forbidden in ("fastapi", "uvicorn", "starlette"):
    if any(
        p.name.lower() == forbidden or p.name.lower().startswith(forbidden)
        for p in internal.iterdir()
    ):
        raise RuntimeError(f"独立 CLI 包混入服务端组件：{forbidden}")

# 必需文档齐全；开始使用.txt 的命令前缀必须为独立程序名且含安全说明。
for required in (
    "AGENTS.md",
    "docs/Agent创建评测任务.md",
    "docs/task-manifest.schema.json",
    "docs/task-manifest.example.json",
    "开始使用.txt",
):
    if not (agent_dir / required).is_file():
        raise RuntimeError(f"独立 CLI 包缺少必需文件：{required}")
usage_text = (agent_dir / "开始使用.txt").read_text(encoding="utf-8")
for line in usage_text.splitlines():
    stripped = line.strip()
    if stripped.startswith(("audio-eval ", "audio-eval-agent serve")):
        raise RuntimeError(f"开始使用.txt 含不允许的程序前缀：{stripped}")
for required_cmd in (
    "audio-eval-agent manifest-init",
    "audio-eval-agent validate",
    "audio-eval-agent prepare",
    "audio-eval-agent apply",
    "audio-eval-agent status",
):
    if required_cmd not in usage_text:
        raise RuntimeError(f"开始使用.txt 缺少命令示例：{required_cmd}")
for required_note in ("--password-stdin", "--password-env", "--allow-insecure-http"):
    if required_note not in usage_text:
        raise RuntimeError(f"开始使用.txt 缺少安全说明：{required_note}")

# manifest-init → 填充真实路径与合成素材 → validate → prepare（重采样路径）。
with tempfile.TemporaryDirectory(prefix="audio-eval-agent-smoke-") as work:
    work_path = Path(work)
    # 合成 48 kHz 立体声源（重采样路径一并验证；不依赖源码树/系统 Python）。
    rate, seconds = 48000, 1.0
    t = np.arange(int(rate * seconds)) / rate
    tone = (0.4 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    for sub in ("cand-a", "cand-b"):
        source_dir = work_path / "work" / "sources" / sub
        source_dir.mkdir(parents=True, exist_ok=True)
        sfile.write(
            str(source_dir / "clip.wav"),
            np.stack([tone] * 2, axis=1),
            rate,
            subtype="FLOAT",
        )

    manifest_path = work_path / "manifest.json"
    result = agent("manifest-init", "--output", str(manifest_path))
    if result.returncode != 0:
        raise RuntimeError(f"agent manifest-init 异常：{result.stderr[:200]}")
    if "audio-eval-agent validate" not in result.stdout:
        raise RuntimeError("manifest-init 提示应指向独立程序名进行校验")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["task"] = {"title": "打包冒烟", "kind": "算法版本", "mode": "development"}
    # 模板自带 0–10 秒 segment；冒烟源只有 1 秒，移除以匹配实际素材。
    manifest["samples"][0].pop("segment", None)
    manifest["samples"][0]["candidates"][0]["source"] = "work/sources/cand-a/clip.wav"
    manifest["samples"][0]["candidates"][1]["source"] = "work/sources/cand-b/clip.wav"
    manifest["conversion"]["resample_to_16000"] = True
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    validate = agent("validate", str(manifest_path))
    if validate.returncode != 0:
        raise RuntimeError(
            f"agent validate 异常：{validate.stdout[:200]}{validate.stderr[:200]}"
        )

    prepared = work_path / "prepared"
    prepare = agent("prepare", str(manifest_path), "--output-dir", str(prepared))
    if prepare.returncode != 0:
        raise RuntimeError(f"agent prepare 异常：{prepare.stderr[:200]}")
    mapping = json.loads((prepared / "mapping.json").read_text(encoding="utf-8"))
    copy_path = prepared / mapping["samples"][0]["candidates"][0]["output"]["path"]
    info = sfile.info(str(copy_path))
    if info.samplerate != 16000 or info.channels != 1:
        raise RuntimeError("prepare 产物不是 16 kHz 单声道")

print("独立 Agent CLI 包 smoke 通过（--help / serve 不可用 / 文档齐全 / manifest-init / validate / prepare）")
