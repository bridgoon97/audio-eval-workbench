"""Agent CLI：manifest 校验、素材准备、幂等编排与凭据卫生。"""

import hashlib
import io
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
import uvicorn
from fastapi.testclient import TestClient

from workbench import agent_cli, agent_tasks
from workbench.agent_tasks import (
    MANIFEST_SCHEMA,
    AgentError,
    ApplyState,
    load_manifest,
    prepare_assets,
    validate_manifest,
)
from workbench.app import create_app


def write_wav(path: Path, rate: int, seconds: float, channels: int, gain: float = 1.0):
    """确定性合成 WAV；gain 只改变幅度，不改变波形形状。"""
    n = int(rate * seconds)
    t = np.arange(n) / rate
    x = (0.4 * np.sin(2 * np.pi * 220 * t) + 0.2 * np.sin(2 * np.pi * 660 * t)) * gain
    if channels > 1:
        x = np.stack([x] * channels, axis=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), x, rate, subtype="FLOAT")
    return path


def base_manifest(sources: dict[str, str], rate=16000, **overrides):
    """构造最小合法 manifest；sources: candidate_key -> 相对路径。"""
    candidates = [
        {
            "key": key,
            "name": f"候选 {key}",
            "version": "v1",
            "source": source,
            "channel": 0,
            "reference": index == 0,
        }
        for index, (key, source) in enumerate(sources.items())
    ]
    manifest = {
        "schema_version": 1,
        "task": {"title": "Agent 验收任务", "kind": "算法版本", "mode": "development"},
        "participants": [],
        "samples": [
            {
                "key": "sample-001",
                "name": "片段 001",
                "scene": "合成场景",
                "provenance": "PUBLIC reproducible",
                "candidates": candidates,
            }
        ],
        "publish": False,
        "conversion": {
            "resample_to_16000": rate != 16000,
            "note": "测试" if rate != 16000 else "",
        },
    }
    manifest.update(overrides)
    return manifest


@pytest.fixture
def media_dir(tmp_path):
    """素材目录：manifest 与 sources 位于 work/ 子目录。"""
    work = tmp_path / "work"
    write_wav(work / "sources" / "a" / "clip.wav", 16000, 1.0, 1)
    write_wav(work / "sources" / "b" / "clip.wav", 16000, 1.0, 1, gain=0.5)
    (work / "manifest.json").write_text(
        json.dumps(base_manifest({"cand-a": "sources/a/clip.wav", "cand-b": "sources/b/clip.wav"}), ensure_ascii=False),
        encoding="utf-8",
    )
    return work


@pytest.fixture
def server(tmp_path):
    """线程内真实 uvicorn 服务（含已初始化的管理员与组织者）。"""
    data_dir = tmp_path / "server-data"
    app = create_app(data_dir)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    for _ in range(200):
        if instance.started:
            break
        time.sleep(0.05)
    port = instance.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    # 初始化管理员与组织者（CLI 使用组织者账号）。
    import httpx

    with httpx.Client(base_url=base) as probe:
        setup_key = app.state.setup_file.read_text().strip()
        probe.post(
            "/api/setup",
            json={"name": "管理员", "password": "admin-agent-pass", "setup_key": setup_key},
        )
        probe.post("/api/login", json={"name": "管理员", "password": "admin-agent-pass"})
        csrf = probe.get("/api/me").json()["csrf_token"]
        probe.post(
            "/api/users",
            json={"name": "编排组织者", "password": "organizer-agent-pass", "role": "organizer"},
            headers={"x-csrf-token": csrf},
        )
    yield base, app
    instance.should_exit = True
    thread.join(timeout=5)


def run_cli(args, cwd=None):
    """以子进程运行 CLI（端到端命令流程），返回 CompletedProcess。"""
    return subprocess.run(
        [sys.executable, "-m", "workbench.cli", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",  # CLI 输出固定 UTF-8；Windows 默认编码会破坏中文回执
        cwd=cwd,
        timeout=300,
        check=False,
    )


# ---------- manifest-init / schema ----------


def test_manifest_init_writes_template_without_overwrite(tmp_path):
    target = tmp_path / "manifest.json"
    code = agent_cli.run(
        argparse_namespace("manifest-init", "--output", str(target))
    )
    assert code == 0 and target.is_file()
    second = agent_cli.run(argparse_namespace("manifest-init", "--output", str(target)))
    assert second == 1  # 不覆盖


def argparse_namespace(*argv):
    import argparse as argparse_module

    parser = argparse_module.ArgumentParser(prog="audio-eval agent")
    agent_cli.register(parser)
    return parser.parse_args(list(argv))


def test_docs_schema_matches_code_and_example_validates():
    docs_schema = json.loads(
        (Path(__file__).resolve().parent.parent / "docs/task-manifest.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert docs_schema == MANIFEST_SCHEMA
    example_path = (
        Path(__file__).resolve().parent.parent / "docs/task-manifest.example.json"
    )
    manifest, base = load_manifest(example_path)
    # 示例的源文件不存在：validate 只允许报“文件不存在”这一类问题。
    problems = validate_manifest(manifest, base)
    assert problems and all("源文件不存在" in p for p in problems)


# ---------- validate 负例 ----------


def test_validate_rejects_path_traversal_and_absolute(tmp_path):
    write_wav(tmp_path / "sources" / "a" / "clip.wav", 16000, 1.0, 1)
    for bad in ("../outside.wav", "/etc/passwd", "sources/../../x.wav"):
        manifest = base_manifest({"cand-a": bad, "cand-b": "sources/a/clip.wav"})
        problems = validate_manifest(manifest, tmp_path)
        assert problems, bad
        assert any("逃逸" in p or "绝对路径" in p or "不存在" in p for p in problems)


def test_validate_rejects_duplicate_keys_counts_and_channels(tmp_path):
    write_wav(tmp_path / "sources" / "a" / "clip.wav", 16000, 1.0, 1)
    # 样本级重复 key 与候选数、通道负例。
    single = base_manifest({"cand-a": "sources/a/clip.wav", "cand-b": "sources/a/clip.wav"})
    single["samples"][0]["candidates"] = single["samples"][0]["candidates"][:1]
    problems = validate_manifest(single, tmp_path)
    assert any("候选数" in p for p in problems)

    seven = base_manifest({"cand-a": "sources/a/clip.wav", "cand-b": "sources/a/clip.wav"})
    template = seven["samples"][0]["candidates"][0]
    extra = []
    for i in range(5):
        clone = dict(template)
        clone["key"] = f"extra-{i}"
        clone["name"] = f"extra-{i}"
        extra.append(clone)
    seven["samples"][0]["candidates"] = [
        *seven["samples"][0]["candidates"],
        *extra,
    ]
    problems = validate_manifest(seven, tmp_path)
    assert any("候选数" in p for p in problems)

    stereo = write_wav(tmp_path / "sources" / "s" / "clip.wav", 16000, 1.0, 2)
    over = base_manifest({"cand-a": "sources/s/clip.wav", "cand-b": "sources/s/clip.wav"})
    over["samples"][0]["candidates"][0]["channel"] = 5
    problems = validate_manifest(over, tmp_path)
    assert any("超出源通道数" in p for p in problems), stereo


def test_validate_rejects_length_mismatch_and_non16k_without_license(tmp_path):
    write_wav(tmp_path / "sources" / "a" / "clip.wav", 16000, 1.0, 1)
    write_wav(tmp_path / "sources" / "b" / "clip.wav", 16000, 2.0, 1)
    manifest = base_manifest({"cand-a": "sources/a/clip.wav", "cand-b": "sources/b/clip.wav"})
    problems = validate_manifest(manifest, tmp_path)
    assert any("目标样本数不一致" in p for p in problems)

    # 非 16 kHz 源：未显式许可时拒绝；显式许可且长度一致时通过。
    write_wav(tmp_path / "sources" / "a" / "48k.wav", 48000, 1.0, 1)
    write_wav(tmp_path / "sources" / "b" / "48k.wav", 48000, 1.0, 1)
    manifest48 = base_manifest(
        {"cand-a": "sources/a/48k.wav", "cand-b": "sources/b/48k.wav"}, rate=48000
    )
    manifest48["conversion"]["resample_to_16000"] = False
    problems = validate_manifest(manifest48, tmp_path)
    assert any("16 kHz" in p and "未显式许可" in p for p in problems)
    manifest48["conversion"]["resample_to_16000"] = True
    assert validate_manifest(manifest48, tmp_path) == []

    # 失败项 1 反例：48 kHz 1.0 秒与 16 kHz 1.0 秒时长相同，许可重采样后通过。
    write_wav(tmp_path / "sources" / "a" / "mix48.wav", 48000, 1.0, 1)
    write_wav(tmp_path / "sources" / "b" / "mix16.wav", 16000, 1.0, 1)
    mixed = base_manifest({"cand-a": "sources/a/mix48.wav", "cand-b": "sources/b/mix16.wav"})
    mixed["conversion"]["resample_to_16000"] = True
    assert validate_manifest(mixed, tmp_path) == []
    # 0.9 秒与 1.0 秒：目标样本数不同 → 拒绝。
    write_wav(tmp_path / "sources" / "a" / "short48.wav", 48000, 0.9, 1)
    short = base_manifest({"cand-a": "sources/a/short48.wav", "cand-b": "sources/b/mix16.wav"})
    short["conversion"]["resample_to_16000"] = True
    problems = validate_manifest(short, tmp_path)
    assert any("目标样本数不一致" in p for p in problems)
    # 不同总长但统一合法 segment 0.2–0.8 秒 → 目标一致通过。
    write_wav(tmp_path / "sources" / "a" / "long48.wav", 48000, 1.5, 1)
    write_wav(tmp_path / "sources" / "b" / "long16.wav", 16000, 2.0, 1)
    seg = base_manifest({"cand-a": "sources/a/long48.wav", "cand-b": "sources/b/long16.wav"})
    seg["samples"][0]["segment"] = {"start_seconds": 0.2, "end_seconds": 0.8}
    seg["conversion"]["resample_to_16000"] = True
    assert validate_manifest(seg, tmp_path) == []


def test_validate_rejects_processing_without_single_reference(tmp_path):
    write_wav(tmp_path / "sources" / "a" / "clip.wav", 16000, 1.0, 1)
    write_wav(tmp_path / "sources" / "b" / "clip.wav", 16000, 1.0, 1)
    manifest = base_manifest({"cand-a": "sources/a/clip.wav", "cand-b": "sources/b/clip.wav"})
    manifest["samples"][0]["candidates"][0]["apply_alignment"] = True
    manifest["samples"][0]["candidates"][0]["reference"] = False
    manifest["samples"][0]["candidates"][1]["reference"] = False
    problems = validate_manifest(manifest, tmp_path)
    assert any("reference" in p for p in problems)

    manifest["samples"][0]["candidates"][0]["reference"] = True
    manifest["samples"][0]["candidates"][1]["reference"] = True
    problems = validate_manifest(manifest, tmp_path)
    assert any("最多一个" in p for p in problems)


def test_validate_publish_requires_scene(tmp_path):
    write_wav(tmp_path / "sources" / "a" / "clip.wav", 16000, 1.0, 1)
    write_wav(tmp_path / "sources" / "b" / "clip.wav", 16000, 1.0, 1)
    manifest = base_manifest({"cand-a": "sources/a/clip.wav", "cand-b": "sources/b/clip.wav"})
    manifest["samples"][0]["scene"] = ""
    manifest["publish"] = True
    problems = validate_manifest(manifest, tmp_path)
    assert any("发布要求字段缺失" in p for p in problems)


def test_validate_rejects_corrupt_wav(tmp_path):
    bad = tmp_path / "sources" / "a" / "clip.wav"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"this is not a wav file")
    manifest = base_manifest({"cand-a": "sources/a/clip.wav", "cand-b": "sources/a/clip.wav"})
    manifest["samples"][0]["candidates"][1]["key"] = "cand-b"
    problems = validate_manifest(manifest, tmp_path)
    assert any("无法按音频读取" in p for p in problems)


# ---------- prepare ----------


def test_prepare_creates_16k_mono_copies_with_sha_and_keeps_sources(tmp_path):
    source = write_wav(tmp_path / "work" / "sources" / "a" / "clip.wav", 48000, 2.0, 2, gain=1.0)
    source_bytes = source.read_bytes()
    manifest = base_manifest({"cand-a": "sources/a/clip.wav", "cand-b": "sources/a/clip.wav"}, rate=48000)
    manifest["samples"][0]["candidates"][1]["channel"] = 1
    manifest["conversion"]["resample_to_16000"] = True

    out = tmp_path / "prepared"
    mapping = prepare_assets(manifest, tmp_path / "work", out)
    assert (out / "mapping.json").is_file()
    entry = mapping["samples"][0]
    assert len(entry["candidates"]) == 2
    for candidate in entry["candidates"]:
        copy = out / candidate["output"]["path"]
        assert copy.is_file()

        assert hashlib.sha256(copy.read_bytes()).hexdigest() == candidate["output"]["sha256"]
        info = sf.info(str(copy))
        assert info.samplerate == 16000 and info.channels == 1
    # 源文件字节不变。
    assert source.read_bytes() == source_bytes
    # mapping 记录通道与转换。
    assert entry["candidates"][0]["source"]["channel_used"] == 0
    assert entry["candidates"][1]["source"]["channel_used"] == 1
    assert any("重采样" in t for t in entry["candidates"][0]["transforms"])


def test_prepare_preserves_relative_levels_without_normalization(tmp_path):
    """同源不同增益的两个候选，转换后 RMS 比例保持（不归一化）。"""
    write_wav(tmp_path / "work" / "sources" / "a" / "clip.wav", 48000, 1.0, 1, gain=1.0)
    write_wav(tmp_path / "work" / "sources" / "b" / "clip.wav", 48000, 1.0, 1, gain=0.25)
    manifest = base_manifest(
        {"cand-a": "sources/a/clip.wav", "cand-b": "sources/b/clip.wav"}, rate=48000
    )
    manifest["conversion"]["resample_to_16000"] = True
    out = tmp_path / "prepared"
    mapping = prepare_assets(manifest, tmp_path / "work", out)
    rms = []
    for candidate in mapping["samples"][0]["candidates"]:
        data, _ = sf.read(str(out / candidate["output"]["path"]))
        rms.append(float(np.sqrt(np.mean(np.square(data)))))
    ratio = rms[0] / rms[1]
    assert ratio == pytest.approx(4.0, rel=0.05)  # 1.0 : 0.25 保持


def test_prepare_rejects_non_empty_output_and_output_inside_sources(tmp_path):
    work = tmp_path / "work"
    write_wav(work / "sources" / "a" / "clip.wav", 16000, 1.0, 1)
    manifest = base_manifest({"cand-a": "sources/a/clip.wav", "cand-b": "sources/a/clip.wav"})
    out = tmp_path / "prepared"
    out.mkdir()
    (out / "已有文件.txt").write_text("x", encoding="utf-8")
    with pytest.raises(AgentError, match="非空"):
        prepare_assets(manifest, work, out)
    # 输出目录与候选源目录：重合、祖先、后代三个方向都拒绝。
    with pytest.raises(AgentError, match="覆盖候选源目录"):
        prepare_assets(manifest, work, work / "sources")  # 重合
    with pytest.raises(AgentError, match="覆盖候选源目录"):
        prepare_assets(manifest, work, work)  # 输出是源目录的祖先
    with pytest.raises(AgentError, match="覆盖候选源目录"):
        prepare_assets(manifest, work, work / "sources" / "a" / "prepared")  # 后代


def test_prepare_requires_explicit_resample_license(tmp_path):
    write_wav(tmp_path / "work" / "sources" / "a" / "48k.wav", 48000, 1.0, 1)
    manifest = base_manifest(
        {"cand-a": "sources/a/48k.wav", "cand-b": "sources/a/48k.wav"}, rate=48000
    )
    manifest["conversion"]["resample_to_16000"] = False
    with pytest.raises(AgentError, match="未显式许可"):
        prepare_assets(manifest, tmp_path / "work", tmp_path / "prepared")


def test_prepare_applies_uniform_segment(tmp_path):
    work = tmp_path / "work"
    write_wav(work / "sources" / "a" / "clip.wav", 16000, 3.0, 1)
    manifest = base_manifest({"cand-a": "sources/a/clip.wav", "cand-b": "sources/a/clip.wav"})
    manifest["samples"][0]["segment"] = {"start_seconds": 1.0, "end_seconds": 2.0}
    out = tmp_path / "prepared"
    mapping = prepare_assets(manifest, work, out)
    entry = mapping["samples"][0]["candidates"][0]
    assert entry["output"]["frames"] == 16000
    assert entry["source"]["segment_frames"] == [16000, 32000]


# ---------- 真实 HTTP 集成：apply / status / 断点 / 篡改 / 泄漏 ----------


def prepare_media_and_mapping(tmp_path, rate=16000):
    work = tmp_path / "work"
    write_wav(work / "sources" / "a" / "clip.wav", rate, 1.0, 1)
    write_wav(work / "sources" / "b" / "clip.wav", rate, 1.0, 1, gain=0.5)
    manifest = base_manifest(
        {"cand-a": "sources/a/clip.wav", "cand-b": "sources/b/clip.wav"}, rate=rate
    )
    if rate != 16000:
        manifest["conversion"]["resample_to_16000"] = True
    manifest_path = work / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "prepared"
    prepare_assets(manifest, work, out)
    return manifest_path, manifest, out


def test_validate_and_prepare_reject_segment_out_of_range(tmp_path):
    """失败项 2：segment 越界必须拒绝，不得静默 clamp；start>=end 拒绝。"""
    work = tmp_path / "work"
    write_wav(work / "sources" / "a" / "clip.wav", 16000, 8.0, 1)
    write_wav(work / "sources" / "b" / "clip.wav", 16000, 8.0, 1)
    manifest = base_manifest({"cand-a": "sources/a/clip.wav", "cand-b": "sources/b/clip.wav"})
    manifest["samples"][0]["segment"] = {"start_seconds": 0.0, "end_seconds": 10.0}
    problems = validate_manifest(manifest, work)
    assert any("超出源长度" in p for p in problems)
    # prepare 同样拒绝，且不产生输出文件。
    with pytest.raises(AgentError, match="超出源长度") as excinfo:
        prepare_assets(manifest, work, tmp_path / "prepared")
    assert "8.000 秒" in str(excinfo.value)
    assert not (tmp_path / "prepared" / "samples").exists()
    # start >= end：语义拒绝。
    bad = base_manifest({"cand-a": "sources/a/clip.wav", "cand-b": "sources/b/clip.wav"})
    bad["samples"][0]["segment"] = {"start_seconds": 5.0, "end_seconds": 5.0}
    problems = validate_manifest(bad, work)
    assert any("起点必须小于终点" in p for p in problems)


def test_prepare_rejects_output_dir_in_every_direction(tmp_path):
    """失败项 3：输出目录与候选源目录重合/祖先/后代三个方向都拒绝。"""
    work = tmp_path / "work"
    write_wav(work / "sources" / "a" / "clip.wav", 16000, 1.0, 1)
    manifest = base_manifest({"cand-a": "sources/a/clip.wav", "cand-b": "sources/a/clip.wav"})
    with pytest.raises(AgentError, match="覆盖候选源目录"):
        prepare_assets(manifest, work, work / "sources")  # 重合
    with pytest.raises(AgentError, match="覆盖候选源目录"):
        prepare_assets(manifest, work, work)  # 输出是源目录的祖先
    with pytest.raises(AgentError, match="覆盖候选源目录"):
        prepare_assets(manifest, work, work / "sources" / "a" / "prepared")  # 后代


def test_apply_end_to_end_idempotent_and_publish(tmp_path, server):
    base, app = server
    manifest_path, _manifest, out = prepare_media_and_mapping(tmp_path)
    state = tmp_path / "state.json"
    args = argparse_namespace(
        "apply",
        str(manifest_path),
        "--server",
        base,
        "--user",
        "编排组织者",
        "--password-stdin",
        "--state",
        str(state),
        "--mapping",
        str(out / "mapping.json"),
        "--json",
    )
    import getpass

    original = getpass.getpass
    getpass.getpass = lambda prompt="": "organizer-agent-pass"
    try:
        code = agent_cli.run(args)
    finally:
        getpass.getpass = original
    assert code == 0
    # 通过 state 与服务端核对。
    state_payload = json.loads(state.read_text(encoding="utf-8"))
    task_id = state_payload["task_id"]
    assert task_id
    # 幂等重跑：不重复创建。
    getpass.getpass = lambda prompt="": "organizer-agent-pass"
    try:
        code2 = agent_cli.run(args)
    finally:
        getpass.getpass = original
    assert code2 == 0
    state_payload = json.loads(state.read_text(encoding="utf-8"))
    assert state_payload["task_id"] == task_id
    client = TestClient(app)
    client.post("/api/login", json={"name": "管理员", "password": "admin-agent-pass"})
    tasks = client.get("/api/tasks").json()
    assert len([t for t in tasks if t["status"] == "draft"]) == 1
    detail = client.get(f"/api/tasks/{task_id}").json()
    assert len(detail["samples"]) == 1
    assert detail["samples"][0]["track_count"] == 2

    # 发布双确认：manifest.publish=false + --publish → 拒绝。
    args_publish = argparse_namespace(
        "apply",
        str(manifest_path),
        "--server",
        base,
        "--user",
        "编排组织者",
        "--password-stdin",
        "--state",
        str(state),
        "--mapping",
        str(out / "mapping.json"),
        "--publish",
        "--json",
    )
    getpass.getpass = lambda prompt="": "organizer-agent-pass"
    try:
        code3 = agent_cli.run(args_publish)
    finally:
        getpass.getpass = original
    assert code3 == 1  # manifest.publish 未开启；任务保持草稿
    assert detail["status"] == "draft"


def test_apply_resume_after_network_failure(tmp_path, server, monkeypatch):
    base, app = server
    manifest_path, _manifest, out = prepare_media_and_mapping(tmp_path)
    state = tmp_path / "state.json"

    from workbench import agent_tasks

    original_upload = agent_tasks.AgentClient.upload_track
    calls = {"count": 0}

    def flaky_upload(self, sample_id, name, version, path):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("网络中断（注入）")
        return original_upload(self, sample_id, name, version, path)

    monkeypatch.setattr(agent_tasks.AgentClient, "upload_track", flaky_upload)
    with pytest.raises(OSError):
        agent_tasks.run_apply(
            manifest_path,
            base,
            "编排组织者",
            "organizer-agent-pass",
            state,
            out / "mapping.json",
        )
    payload = json.loads(state.read_text(encoding="utf-8"))
    uploaded = {
        tkey
        for entry in payload["samples"].values()
        for tkey, tv in entry["tracks"].items()
        if tv.get("stage") == "uploaded"
    }
    assert uploaded == {"cand-a"}  # 第 1 个成功并已落盘
    monkeypatch.setattr(agent_tasks.AgentClient, "upload_track", original_upload)
    # 重跑：只上传剩余项，无重复任务/片段/候选。
    receipt = agent_tasks.run_apply(
        manifest_path,
        base,
        "编排组织者",
        "organizer-agent-pass",
        state,
        out / "mapping.json",
    )
    assert [t["stage"] for t in receipt["samples"][0]["tracks"]] == [
        "already-uploaded",
        "uploaded",
    ]
    client = TestClient(app)
    client.post("/api/login", json={"name": "管理员", "password": "admin-agent-pass"})
    tasks = client.get("/api/tasks").json()
    assert len([t for t in tasks if t["title"] == "Agent 验收任务"]) == 1
    detail = client.get(f"/api/tasks/{receipt['task_id']}").json()
    assert detail["samples"][0]["track_count"] == 2


def test_apply_stops_on_tampered_source_or_manifest(tmp_path, server):
    base, app = server
    manifest_path, manifest, out = prepare_media_and_mapping(tmp_path)
    work = tmp_path / "work"
    state = tmp_path / "state.json"
    try:
        agent_tasks.run_apply(
            manifest_path, base, "编排组织者", "organizer-agent-pass", state, out / "mapping.json"
        )
    except Exception as _exc:  # 临时诊断
        print("FIRST-APPLY-FAILED:", type(_exc).__name__, str(_exc)[:300], flush=True)
        raise
    # 篡改源文件字节（保存原始字节，之后按字节精确恢复；
    # 不经 soundfile 解码重编码——FLOAT WAV 容器在个别环境下字节不稳定）。
    source = work / "sources" / "a" / "clip.wav"
    original_bytes = source.read_bytes()
    tampered_bytes = bytearray(original_bytes)
    tampered_bytes[-1] ^= 0xFF
    source.write_bytes(bytes(tampered_bytes))
    with pytest.raises(AgentError, match="源文件 SHA256 与 mapping 不符") as e1:
        agent_tasks.run_apply(
            manifest_path, base, "编排组织者", "organizer-agent-pass", state, out / "mapping.json"
        )
    print("TAMPER-DETECT-OK:", str(e1.value)[:120], flush=True)
    # 按字节恢复源，篡改 manifest 候选显示名。
    source.write_bytes(original_bytes)
    manifest["samples"][0]["candidates"][0]["name"] = "被篡改的显示名"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(AgentError, match="差异") as excinfo:
        agent_tasks.run_apply(
            manifest_path, base, "编排组织者", "organizer-agent-pass", state, out / "mapping.json"
        )
    assert "被篡改的显示名" in str(excinfo.value)
    # 服务端草稿未被修改。
    payload = json.loads(state.read_text(encoding="utf-8"))
    client = TestClient(app)
    client.post("/api/login", json={"name": "管理员", "password": "admin-agent-pass"})
    detail = client.get(f"/api/tasks/{payload['task_id']}").json()
    assert detail["samples"][0]["track_count"] == 2


def test_apply_rejects_mismatched_server_instance(tmp_path, server):
    base, _app = server
    manifest_path, _manifest, out = prepare_media_and_mapping(tmp_path)
    state = tmp_path / "state.json"
    agent_tasks.run_apply(
        manifest_path, base, "编排组织者", "organizer-agent-pass", state, out / "mapping.json"
    )
    # 另一个全新服务实例。
    import tempfile as tempfile_module

    with tempfile_module.TemporaryDirectory() as other_root:
        import httpx

        other_app = create_app(Path(other_root))
        other_config = uvicorn.Config(
            other_app, host="127.0.0.1", port=0, log_level="warning"
        )
        other_server = uvicorn.Server(other_config)
        other_thread = threading.Thread(target=other_server.run, daemon=True)
        other_thread.start()
        for _ in range(200):
            if other_server.started:
                break
            time.sleep(0.05)
        other_port = other_server.servers[0].sockets[0].getsockname()[1]
        # 另一实例上有同名账号（模拟连错服务端口）：登录能成功，实例绑定必须拒绝。
        with httpx.Client(base_url=f"http://127.0.0.1:{other_port}") as probe:
            probe.post(
                "/api/setup",
                json={
                    "name": "管理员",
                    "password": "admin-agent-pass",
                    "setup_key": other_app.state.setup_file.read_text().strip(),
                },
            )
            probe.post("/api/login", json={"name": "管理员", "password": "admin-agent-pass"})
            csrf = probe.get("/api/me").json()["csrf_token"]
            probe.post(
                "/api/users",
                json={
                    "name": "编排组织者",
                    "password": "organizer-agent-pass",
                    "role": "organizer",
                },
                headers={"x-csrf-token": csrf},
            )
        with pytest.raises(AgentError, match="另一服务实例"):
            agent_tasks.run_apply(
                manifest_path,
                f"http://127.0.0.1:{other_port}",
                "编排组织者",
                "organizer-agent-pass",
                state,
                out / "mapping.json",
            )
        other_server.should_exit = True
        other_thread.join(timeout=5)


def test_apply_credentials_never_leak(tmp_path, server, capsys):
    base, _app = server
    manifest_path, _manifest, out = prepare_media_and_mapping(tmp_path)
    state = tmp_path / "state.json"
    secret = "organizer-agent-pass"
    wrong = "definitely-wrong-pass"
    agent_tasks.run_apply(
        manifest_path, base, "编排组织者", secret, state, out / "mapping.json"
    )
    # 故意用错误密码触发失败路径；异常文本与 CLI 输出都不得包含任何密码。
    with pytest.raises(AgentError, match="401") as excinfo:
        agent_tasks.run_apply(
            manifest_path, base, "编排组织者", wrong, state, out / "mapping.json"
        )
    assert wrong not in str(excinfo.value)
    # 走一遍 CLI 打印路径（agent_cli.run 把错误写到 stderr）。
    leak_args = argparse_namespace(
        "apply",
        str(manifest_path),
        "--server",
        base,
        "--user",
        "编排组织者",
        "--password-stdin",
        "--state",
        str(state),
        "--mapping",
        str(out / "mapping.json"),
        "--json",
    )
    import getpass

    original = getpass.getpass
    getpass.getpass = lambda prompt="": wrong
    try:
        assert agent_cli.run(leak_args) == 1
    finally:
        getpass.getpass = original
    captured = capsys.readouterr()
    # 正确密码与错误密码都不得出现在任何输出或落盘文件中。
    for sensitive in (secret, wrong):
        assert sensitive not in captured.out + captured.err, sensitive
        assert sensitive not in state.read_text(encoding="utf-8"), sensitive
        mapping_text = (out / "mapping.json").read_text(encoding="utf-8")
        assert sensitive not in mapping_text, sensitive


def test_apply_publish_double_confirmation(tmp_path, server):
    base, app = server
    manifest_path, manifest, out = prepare_media_and_mapping(tmp_path)
    manifest["publish"] = False
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    state = tmp_path / "state.json"
    agent_tasks.run_apply(
        manifest_path, base, "编排组织者", "organizer-agent-pass", state, out / "mapping.json"
    )
    # 仅 CLI 开关：拒绝。
    with pytest.raises(AgentError, match="publish"):
        agent_tasks.run_apply(
            manifest_path,
            base,
            "编排组织者",
            "organizer-agent-pass",
            state,
            out / "mapping.json",
            publish_flag=True,
        )
    client = TestClient(app)
    client.post("/api/login", json={"name": "管理员", "password": "admin-agent-pass"})
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert client.get(f"/api/tasks/{payload['task_id']}").json()["status"] == "draft"
    # 仅 manifest：不开 --publish → 保留草稿。
    manifest["publish"] = True
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    # manifest 变化触发快照差异：用新 state 演示“两确认齐备才发布”。
    state2 = tmp_path / "state2.json"
    receipt = agent_tasks.run_apply(
        manifest_path, base, "编排组织者", "organizer-agent-pass", state2, out / "mapping.json"
    )
    assert receipt["published"] is False  # 未加 --publish
    receipt = agent_tasks.run_apply(
        manifest_path,
        base,
        "编排组织者",
        "organizer-agent-pass",
        state2,
        out / "mapping.json",
        publish_flag=True,
    )
    assert receipt["published"] is True
    assert client.get(f"/api/tasks/{receipt['task_id']}").json()["status"] == "active"


def test_apply_participant_resolution_and_permissions(tmp_path, server):
    base, app = server
    import httpx

    with httpx.Client(base_url=base) as probe:
        probe.post("/api/login", json={"name": "管理员", "password": "admin-agent-pass"})
        csrf = probe.get("/api/me").json()["csrf_token"]
        probe.post(
            "/api/users",
            json={"name": "评测同事甲", "password": "reviewer-agent-pass"},
            headers={"x-csrf-token": csrf},
        )
    manifest_path, _manifest, out = prepare_media_and_mapping(tmp_path)
    manifest = _manifest | {"participants": [{"name": "不存在的同事"}]}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    state = tmp_path / "state.json"
    with pytest.raises(AgentError, match="参与者不存在"):
        agent_tasks.run_apply(
            manifest_path, base, "编排组织者", "organizer-agent-pass", state, out / "mapping.json"
        )
    # 重复条目：解析后去重为同一稳定 ID（同一账号不会被创建两次）。
    manifest["participants"] = [{"name": "评测同事甲"}, {"name": "评测同事甲"}]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    state_b = tmp_path / "state-b.json"
    receipt_b = agent_tasks.run_apply(
        manifest_path, base, "编排组织者", "organizer-agent-pass", state_b, out / "mapping.json"
    )
    assert receipt_b["participants"] == {"评测同事甲": receipt_b["participants"]["评测同事甲"]}
    detail_b = TestClient(app)
    detail_b.post("/api/login", json={"name": "管理员", "password": "admin-agent-pass"})
    payload_b = json.loads(state_b.read_text(encoding="utf-8"))
    users_b = detail_b.get(f"/api/tasks/{payload_b['task_id']}").json()["review_assignments"]
    assert users_b.count(receipt_b["participants"]["评测同事甲"]) == 1
    # 权限：评测者不能创建任务。
    manifest["participants"] = [{"name": "评测同事甲"}]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(AgentError, match="403"):
        agent_tasks.run_apply(
            manifest_path,
            base,
            "评测同事甲",
            "reviewer-agent-pass",
            tmp_path / "state-reviewer.json",
            out / "mapping.json",
        )


def test_apply_processing_rejection_blocks_publish(tmp_path, server):
    base, app = server
    # 周期纯音与宽带参考低相关 → 服务端拒绝对齐。
    write_wav(tmp_path / "sources" / "a" / "clip.wav", 16000, 2.0, 1)
    t = np.arange(32000) / 16000
    pure = (0.5 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    pure_path = tmp_path / "sources" / "b" / "clip.wav"
    pure_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(pure_path), pure, 16000, subtype="FLOAT")
    manifest = base_manifest({"cand-a": "sources/a/clip.wav", "cand-b": "sources/b/clip.wav"})
    manifest["samples"][0]["candidates"][1]["apply_alignment"] = True
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "prepared"
    prepare_assets(manifest, tmp_path, out)
    state = tmp_path / "state.json"
    with pytest.raises(AgentError, match="拒绝"):
        agent_tasks.run_apply(
            manifest_path, base, "编排组织者", "organizer-agent-pass", state, out / "mapping.json"
        )
    payload = json.loads(state.read_text(encoding="utf-8"))
    client = TestClient(app)
    client.post("/api/login", json={"name": "管理员", "password": "admin-agent-pass"})
    assert client.get(f"/api/tasks/{payload['task_id']}").json()["status"] == "draft"
    # 保留原始并继续草稿：不发布、回执记录拒绝。
    state_b = tmp_path / "state-b.json"
    receipt = agent_tasks.run_apply(
        manifest_path,
        base,
        "编排组织者",
        "organizer-agent-pass",
        state_b,
        out / "mapping.json",
        keep_original_on_rejection=True,
    )
    assert receipt["processing_rejections"]
    assert receipt["published"] is False


def test_agent_status_json(tmp_path, server):
    base, _app = server
    manifest_path, _manifest, out = prepare_media_and_mapping(tmp_path)
    state = tmp_path / "state.json"
    agent_tasks.run_apply(
        manifest_path, base, "编排组织者", "organizer-agent-pass", state, out / "mapping.json"
    )
    args = argparse_namespace("status", "--state", str(state), "--json")
    code = agent_cli.run(args)
    assert code == 0
    # status 输出由 agent_cli 打印；这里核对 state 本体结构。
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["task_id"] and payload["server"]["instance_id"]


def test_state_binds_manifest_and_rejects_corrupt(tmp_path):
    state = ApplyState(tmp_path / "state.json")
    state.payload["task_id"] = "abc"
    state.save()
    loaded = ApplyState.load(tmp_path / "state.json")
    assert loaded.payload["task_id"] == "abc"
    (tmp_path / "broken.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(AgentError, match="损坏"):
        ApplyState.load(tmp_path / "broken.json")


# ---------- 端到端命令流程（子进程）----------


def test_end_to_end_command_flow(tmp_path, server):
    base, app = server
    env = {
        **__import__("os").environ,
        "AGENT_PASS": "organizer-agent-pass",
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
    }
    manifest = base_manifest(
        {"cand-a": "sources/a/clip.wav", "cand-b": "sources/b/clip.wav"}
    )
    write_wav(tmp_path / "sources" / "a" / "clip.wav", 16000, 1.0, 1)
    write_wav(tmp_path / "sources" / "b" / "clip.wav", 16000, 1.0, 1, gain=0.5)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    for step in (
        ["agent", "validate", str(manifest_path)],
        ["agent", "prepare", str(manifest_path), "--output-dir", str(tmp_path / "prepared")],
        [
            "agent",
            "apply",
            str(manifest_path),
            "--server",
            base,
            "--user",
            "编排组织者",
            "--password-env",
            "AGENT_PASS",
            "--state",
            str(tmp_path / "state.json"),
            "--mapping",
            str(tmp_path / "prepared" / "mapping.json"),
            "--json",
        ],
        ["agent", "status", "--state", str(tmp_path / "state.json"), "--json"],
    ):
        result = subprocess.run(
            [sys.executable, "-c", "from workbench.cli import main; raise SystemExit(main())", *step],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=300,
            cwd=str(Path(__file__).resolve().parent.parent),
            check=False,
        )
        assert result.returncode == 0, (step, result.stdout, result.stderr)
        if step[1] == "apply":
            receipt = json.loads(result.stdout)
            assert receipt["task_id"]
            assert receipt["published"] is False
    # 服务端最终核对。
    client = TestClient(app)
    client.post("/api/login", json={"name": "管理员", "password": "admin-agent-pass"})
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    detail = client.get(f"/api/tasks/{state['task_id']}").json()
    assert len(detail["samples"]) == 1 and detail["samples"][0]["track_count"] == 2


def test_apply_processing_success_then_publish(tmp_path, server):
    """失败项 5：可判据的正向处理成功后，派生轨按回执 SHA 复核并成功发布。"""
    base, app = server
    # 参考 4 秒宽带（白噪声 + 扫频，确定性种子）；候选 = 延迟 320 采样 + 增益 0.7。
    rate = 16000
    t = np.arange(int(rate * 4)) / rate
    rng = np.random.default_rng(12345)
    ref = (0.6 * rng.standard_normal(len(t)) + 0.3 * np.sin(
        2 * np.pi * (40 * t + (900 * t**2) / 2)
    )).astype(np.float32)
    ref = (ref / np.max(np.abs(ref)) * 0.5).astype(np.float32)  # 与对齐 E2E 相同的 0.5 峰值
    delayed = np.zeros_like(ref)
    delayed[320:] = ref[:-320] * 0.7
    work = tmp_path / "work"
    (work / "sources" / "a").mkdir(parents=True, exist_ok=True)
    (work / "sources" / "b").mkdir(parents=True, exist_ok=True)
    sf.write(str(work / "sources" / "a" / "clip.wav"), ref, rate, subtype="FLOAT")
    sf.write(str(work / "sources" / "b" / "clip.wav"), delayed, rate, subtype="FLOAT")
    manifest = base_manifest({"cand-a": "sources/a/clip.wav", "cand-b": "sources/b/clip.wav"})
    manifest["samples"][0]["candidates"][1]["apply_alignment"] = True
    manifest["samples"][0]["candidates"][1]["apply_loudness"] = True
    manifest["publish"] = True
    manifest_path = work / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "prepared"
    prepare_assets(manifest, work, out)
    state = tmp_path / "state.json"

    # 首次编排（未加 --publish）：处理成功、保留草稿，回执含 lag/gain。
    receipt = agent_tasks.run_apply(
        manifest_path, base, "编排组织者", "organizer-agent-pass", state, out / "mapping.json"
    )
    assert receipt["published"] is False
    record = receipt["processing"][0]
    assert record["applied"] is True
    item = record["applied_items"][0]
    assert item["lag"] == 320 and abs(item["gain_db"]) > 0
    derived_sha = item["派生资产SHA256"]
    state_payload = json.loads(state.read_text(encoding="utf-8"))
    track_id = state_payload["samples"]["sample-001"]["tracks"]["cand-b"]["track_id"]

    # 发布（双确认齐备）：派生 SHA 复核通过后发布成功。
    receipt = agent_tasks.run_apply(
        manifest_path,
        base,
        "编排组织者",
        "organizer-agent-pass",
        state,
        out / "mapping.json",
        publish_flag=True,
    )
    assert receipt["published"] is True
    client = TestClient(app)
    client.post("/api/login", json={"name": "管理员", "password": "admin-agent-pass"})
    assert client.get(f"/api/tasks/{receipt['task_id']}").json()["status"] == "active"
    # 下载派生轨核对 SHA 与回执一致。
    downloaded = client.get(f"/api/audio/{track_id}")

    assert hashlib.sha256(downloaded.content).hexdigest() == derived_sha


def test_processing_rejection_evidence_survives_retry(tmp_path, server):
    """失败项 6：部分应用+拒绝 → 重跑 --publish 仍被持久化证据阻止。"""
    base, app = server
    # 三候选：参考宽带、延迟候选（可处理）、周期纯音（对齐拒绝）。
    rate = 16000
    t = np.arange(int(rate * 4)) / rate
    rng = np.random.default_rng(6789)
    ref = (0.6 * rng.standard_normal(len(t)) + 0.3 * np.sin(
        2 * np.pi * (40 * t + (900 * t**2) / 2)
    )).astype(np.float32)
    delayed = np.zeros_like(ref)
    delayed[160:] = ref[:-160] * 0.8
    pure = (0.5 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    work = tmp_path / "work"
    for sub in ("a", "b", "c"):
        (work / "sources" / sub).mkdir(parents=True, exist_ok=True)
    sf.write(str(work / "sources" / "a" / "clip.wav"), ref, rate, subtype="FLOAT")
    sf.write(str(work / "sources" / "b" / "clip.wav"), delayed, rate, subtype="FLOAT")
    sf.write(str(work / "sources" / "c" / "clip.wav"), pure, rate, subtype="FLOAT")
    manifest = base_manifest(
        {
            "cand-a": "sources/a/clip.wav",
            "cand-b": "sources/b/clip.wav",
            "cand-c": "sources/c/clip.wav",
        }
    )
    manifest["samples"][0]["candidates"][1]["apply_alignment"] = True
    manifest["samples"][0]["candidates"][2]["apply_alignment"] = True
    manifest["publish"] = True
    manifest_path = work / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "prepared"
    prepare_assets(manifest, work, out)
    state = tmp_path / "state.json"

    # 首次：部分成功（延迟候选）+ 拒绝（纯音）→ 默认停止。
    with pytest.raises(AgentError, match="拒绝"):
        agent_tasks.run_apply(
            manifest_path, base, "编排组织者", "organizer-agent-pass", state, out / "mapping.json"
        )
    payload = json.loads(state.read_text(encoding="utf-8"))
    record = payload["processing"]["sample-001"]
    assert record["applied"] is True  # 部分成功已持久化
    assert record["rejections"] and "纯音" not in "".join(record["rejections"])

    # 第二次：即便 --publish 也被持久化证据阻止。
    with pytest.raises(AgentError, match="混合口径"):
        agent_tasks.run_apply(
            manifest_path,
            base,
            "编排组织者",
            "organizer-agent-pass",
            state,
            out / "mapping.json",
            publish_flag=True,
        )
    client = TestClient(app)
    client.post("/api/login", json={"name": "管理员", "password": "admin-agent-pass"})
    assert client.get(f"/api/tasks/{payload['task_id']}").json()["status"] == "draft"


def test_publish_without_implicit_owner_and_rejects_disabled(tmp_path, server):
    """失败项 7：owner 不隐式受邀；停用参与者解析被拒绝。"""
    base, app = server
    import httpx

    with httpx.Client(base_url=base) as probe:
        probe.post("/api/login", json={"name": "管理员", "password": "admin-agent-pass"})
        csrf = probe.get("/api/me").json()["csrf_token"]
        probe.post(
            "/api/users",
            json={"name": "受邀同事", "password": "invited-agent-pass"},
            headers={"x-csrf-token": csrf},
        )
        users = probe.get("/api/users").json()
        invited_id = next(u["id"] for u in users if u["name"] == "受邀同事")
        # 停用该账号。
        probe.post(
            f"/api/users/{invited_id}/status",
            json={"active": False},
            headers={"x-csrf-token": csrf},
        )

    manifest_path, manifest, out = prepare_media_and_mapping(tmp_path)
    # 停用参与者：解析拒绝。
    manifest["participants"] = [{"name": "受邀同事"}]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    state = tmp_path / "state-disabled.json"
    with pytest.raises(AgentError, match="已停用"):
        agent_tasks.run_apply(
            manifest_path, base, "编排组织者", "organizer-agent-pass", state, out / "mapping.json"
        )
    # 服务端无任务副作用。
    client = TestClient(app)
    client.post("/api/login", json={"name": "管理员", "password": "admin-agent-pass"})
    assert client.get("/api/tasks").json() == []

    # 空 participants + 发布：受邀名单为空；owner 仍可管理但不承担评测义务。
    manifest["participants"] = []
    manifest["publish"] = True
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    state2 = tmp_path / "state-empty.json"
    receipt = agent_tasks.run_apply(
        manifest_path,
        base,
        "编排组织者",
        "organizer-agent-pass",
        state2,
        out / "mapping.json",
        publish_flag=True,
    )
    assert receipt["published"] is True
    detail = client.get(f"/api/tasks/{receipt['task_id']}").json()
    assert detail["review_assignments"] == []  # 未显式列出 ⇒ 无人承担评测义务
    assert detail["can_manage"] is True  # owner 保留管理权
    assert detail["status"] == "active"  # 真实走到发布端点
    # owner 未受邀：首次评分 403 且不产生评分行（既有受邀评分语义）。
    organizer_view = client.get(f"/api/samples/{detail['samples'][0]['id']}").json()
    assert organizer_view["blind"] is False
    rating = client.post(
        f"/api/samples/{detail['samples'][0]['id']}/rating", json={"choice": "tie"}
    )
    assert rating.status_code == 403
    client.post(
        f"/api/samples/{detail['samples'][0]['id']}/comments",
        json={"start": 0, "end": 100, "body": "owner 的管理标注"},
    )  # owner 仍可评论（访问权限保留）


def test_apply_rejects_tampered_mapping(tmp_path, server):
    """失败项 4：mapping 路径逃逸、自洽改写 SHA、额外条目、缺 mapping 全部拒绝，
    服务端无任务副作用。"""
    base, app = server
    manifest_path, _manifest, out = prepare_media_and_mapping(tmp_path)
    client = TestClient(app)
    client.post("/api/login", json={"name": "管理员", "password": "admin-agent-pass"})
    mapping_path = out / "mapping.json"
    state = tmp_path / "state.json"

    # 首次不传 mapping：登录前拒绝，服务端无任务。
    with pytest.raises(AgentError, match="--mapping"):
        agent_tasks.run_apply(
            manifest_path, base, "编排组织者", "organizer-agent-pass", state, None
        )
    assert client.get("/api/tasks").json() == []

    # 路径逃逸：output.path 指向 mapping 目录之外（自洽 SHA）。

    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    outside = out / "outside-escape.wav"
    shutil.copy(out / "samples" / "sample-001" / "cand-a.wav", outside)
    outside_sha = hashlib.sha256(outside.read_bytes()).hexdigest()
    pcm = hashlib.sha256(
        sf.read(str(outside), dtype="float32")[0].astype(np.float32).tobytes()
    ).hexdigest()
    mapping["samples"][0]["candidates"][0]["output"].update(
        {"path": "../outside.wav", "sha256": outside_sha, "pcm_sha256": pcm, "frames": 16000}
    )
    mapping_path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(AgentError, match="逃逸出 mapping 目录"):
        agent_tasks.run_apply(
            manifest_path,
            base,
            "编排组织者",
            "organizer-agent-pass",
            state,
            mapping_path,
        )  # 篡改发生在任何服务端调用之前
    assert client.get("/api/tasks").json() == []

    # 自洽改写：把 output 指向内容被篡改（增益 0.5）的 WAV，并同步改写全部
    # SHA 字段——只有“按源与声明的转换重建”校验能识破。
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    swapped = out / "swapped.wav"
    data, rate = sf.read(str(out / "samples" / "sample-001" / "cand-a.wav"), dtype="float32")
    swapped_bytes = io.BytesIO()
    sf.write(swapped_bytes, (data * 0.5).astype(np.float32), rate, subtype="FLOAT", format="WAV")
    swapped.write_bytes(swapped_bytes.getvalue())
    mapping["samples"][0]["candidates"][0]["output"].update(
        {
            "path": "swapped.wav",
            "sha256": hashlib.sha256(swapped_bytes.getvalue()).hexdigest(),
            "pcm_sha256": hashlib.sha256(
                (data * 0.5).astype(np.float32).tobytes()
            ).hexdigest(),
            "frames": len(data),
        }
    )
    mapping_path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(AgentError, match="无法由源与声明的转换重建"):
        agent_tasks.run_apply(
            manifest_path,
            base,
            "编排组织者",
            "organizer-agent-pass",
            state,
            mapping_path,
        )
    assert client.get("/api/tasks").json() == []

    # mapping SHA 绑定与中途修改拒绝由 test_mapping_sha_binding_rejects_midway_changes 覆盖。


def test_mapping_sha_binding_rejects_midway_changes(tmp_path, server):
    """失败项 4：mapping SHA 绑定 state——中途任何修改停止，草稿不受影响。"""
    base, app = server
    manifest_path, _manifest, out = prepare_media_and_mapping(tmp_path)
    mapping_path = out / "mapping.json"
    state = tmp_path / "state.json"
    # 完整草稿编排（合法 mapping），state 记录 mapping SHA。
    agent_tasks.run_apply(
        manifest_path, base, "编排组织者", "organizer-agent-pass", state, mapping_path
    )
    state_payload = json.loads(state.read_text(encoding="utf-8"))
    assert state_payload["mapping_sha256"]
    task_id = state_payload["task_id"]

    # 中途修改 mapping（改 conversion_note）→ SHA 不一致拒绝。
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    mapping["conversion_note"] = "被中途改动"
    mapping_path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(AgentError, match="不一致"):
        agent_tasks.run_apply(
            manifest_path, base, "编排组织者", "organizer-agent-pass", state, mapping_path
        )
    client = TestClient(app)
    client.post("/api/login", json={"name": "管理员", "password": "admin-agent-pass"})
    detail = client.get(f"/api/tasks/{task_id}").json()
    assert detail["samples"][0]["track_count"] == 2


def test_agent_entry_import_isolation():
    """独立入口的 import 图不得触达服务端模块（fastapi/uvicorn/starlette/cli）。"""
    import ast

    entry = Path(__file__).resolve().parent.parent / "agent_entry.py"
    tree = ast.parse(entry.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.extend(
                f"{module}.{alias.name}" if node.level == 0 else alias.name
                for alias in node.names
            )
    forbidden = (
        "workbench.cli",
        "workbench.app",
        "uvicorn",
        "fastapi",
        "starlette",
        "workbench.align",
    )
    violations = [
        name for name in imported for bad in forbidden if name == bad or name.startswith(bad + ".")
    ]
    assert violations == [], f"独立入口引入了服务端依赖：{violations}"



