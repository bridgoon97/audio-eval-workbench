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


# ---------- 完整服务端包内的 Agent 文档与 agent 子命令 smoke ----------
# 缺任一文档或命令不可调用必须失败；出现把服务端写成独立包前缀
# （audio-eval-agent.exe agent ...）的误导内容也必须失败。


package_root = Path("release/audio-eval")
if not package_root.is_dir():
    raise RuntimeError("未找到完整服务端包（release/audio-eval）；请先运行 scripts/package.py")

required_agent_docs = [
    "AGENTS.md",
    "docs/Agent创建评测任务.md",
    "docs/task-manifest.schema.json",
    "docs/task-manifest.example.json",
    "Agent使用入口.txt",
]
for required in required_agent_docs:
    if not (package_root / required).is_file():
        raise RuntimeError(f"服务端包缺少 Agent 文档：{required}")

entry_text = (package_root / "Agent使用入口.txt").read_text(encoding="utf-8")
# 五条核心命令必须以完整服务端前缀出现。
for required_cmd in (
    "audio-eval.exe agent manifest-init",
    "audio-eval.exe agent validate",
    "audio-eval.exe agent prepare",
    "audio-eval.exe agent apply",
    "audio-eval.exe agent status",
):
    if required_cmd not in entry_text:
        raise RuntimeError(f"Agent使用入口.txt 缺少服务端命令示例：{required_cmd}")
# 安全说明必须存在。
for required_note in ("--password-stdin", "--password-env", "--allow-insecure-http"):
    if required_note not in entry_text:
        raise RuntimeError(f"Agent使用入口.txt 缺少安全说明：{required_note}")
# 必须说明 Agent 通过终端调用、网页 UI 不会自动运行 Agent。
if "网页界面不会自动运行" not in entry_text and "不会自动运行" not in entry_text:
    raise RuntimeError("Agent使用入口.txt 缺少『网页界面不会自动运行 Agent』说明")
# 不允许把服务端命令写成独立包前缀的误导内容。
if "audio-eval-agent.exe agent" in entry_text:
    raise RuntimeError("Agent使用入口.txt 出现独立包前缀的服务端命令误导")

# agent 子命令可调用：--help 与 manifest-init / validate（对合成素材）。
package_exe = package_root / ("audio-eval.exe" if sys.platform == "win32" else "audio-eval")


def package_agent_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(package_exe), "agent", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )


import numpy as np
import soundfile as sfile

help_result = package_agent_cli("--help")
if help_result.returncode != 0 or "manifest-init" not in help_result.stdout:
    raise RuntimeError(f"audio-eval agent --help 异常：{help_result.stderr[:200]}")

with tempfile.TemporaryDirectory(prefix="audio-eval-package-agent-") as work:
    work_path = Path(work)
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
    init = package_agent_cli("manifest-init", "--output", str(manifest_path))
    if init.returncode != 0:
        raise RuntimeError(f"agent manifest-init 异常：{init.stderr[:200]}")
    # manifest-init 的提示按调用入口显示程序名（服务端 exe 与源码运行不同），
    # 只断言其指向 validate 子命令，不锁定程序名写法。
    if "validate" not in init.stdout:
        raise RuntimeError("manifest-init 提示未指向 validate 命令")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["task"] = {"title": "打包冒烟", "kind": "算法版本", "mode": "development"}
    manifest["samples"][0].pop("segment", None)
    manifest["samples"][0]["candidates"][0]["source"] = "work/sources/cand-a/clip.wav"
    manifest["samples"][0]["candidates"][1]["source"] = "work/sources/cand-b/clip.wav"
    manifest["conversion"]["resample_to_16000"] = True
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    validate = package_agent_cli("validate", str(manifest_path))
    if validate.returncode != 0:
        raise RuntimeError(
            f"agent validate 异常：{validate.stdout[:200]}{validate.stderr[:200]}"
        )

    # 前缀错位反例：把服务端命令写成独立包前缀（audio-eval-agent.exe ...）
    # 时，服务端命令示例全部缺失——smoke 的断言逻辑必须失败。
    wrong_prefix = entry_text.replace("audio-eval.exe agent", "audio-eval-agent.exe")
    if wrong_prefix == entry_text:
        raise RuntimeError("前缀错位反例构造失败（测试自身问题）")
    for required_cmd in (
        "audio-eval.exe agent manifest-init",
        "audio-eval.exe agent validate",
        "audio-eval.exe agent prepare",
        "audio-eval.exe agent apply",
        "audio-eval.exe agent status",
    ):
        if required_cmd in wrong_prefix:
            raise RuntimeError(
                f"前缀错位反例未生效：错位文本仍含服务端命令 {required_cmd}"
            )
    # 错位文本在 smoke 的断言下必然失败（缺少全部服务端命令示例）。
    print("前缀错位反例验证通过（错位文本缺少全部服务端命令示例）")


def required_cmd_missing_in(text: str) -> bool:
    """错位文本缺少任一服务端命令示例即判定失败。"""
    return any(
        cmd not in text
        for cmd in (
            "audio-eval.exe agent manifest-init",
            "audio-eval.exe agent validate",
            "audio-eval.exe agent prepare",
            "audio-eval.exe agent apply",
            "audio-eval.exe agent status",
        )
    )
