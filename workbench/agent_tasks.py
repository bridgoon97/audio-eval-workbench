"""Agent 驱动的评测任务编排：manifest 校验、素材准备与服务端编排。

设计边界：
- 纯 Python 可复用模块，CLI（workbench.cli）与未来 MCP 工具只做薄封装；
- 只走服务公开 HTTP API，不直接读写 SQLite/assets；
- 原始相对电平神圣：prepare 仅做显式许可的采样率转换/通道选择/截取，
  绝不归一化、不自动对齐、不自动增益；
- 凭据（密码/会话/CSRF）只保存在内存，不落 state/mapping/日志。
"""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import jsonschema
import numpy as np

from .version import VERSION

MANIFEST_SCHEMA_VERSION = 1
TARGET_RATE = 16000
STATE_SCHEMA_VERSION = 1
MIN_PASSWORD = 10

MANIFEST_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "听鉴 Agent 评测任务清单",
    "type": "object",
    "required": ["schema_version", "task", "samples"],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"const": MANIFEST_SCHEMA_VERSION},
        "task": {
            "type": "object",
            "required": ["title", "kind", "mode"],
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 150},
                "kind": {
                    "enum": ["算法版本", "VPU 支路", "级联链路", "竞品算法", "竞品整机"]
                },
                "mode": {"enum": ["development", "blind"]},
            },
        },
        "participants": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name"],
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 60}
                },
            },
        },
        "samples": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["key", "name", "provenance", "candidates"],
                "additionalProperties": False,
                "properties": {
                    "key": {
                        "type": "string",
                        "pattern": "^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$",
                    },
                    "name": {"type": "string", "minLength": 1, "maxLength": 100},
                    "scene": {"type": "string", "maxLength": 200},
                    "provenance": {
                        "enum": [
                            "PUBLIC reproducible",
                            "DECLASSIFIED real-device",
                            "PRIVATE local verification",
                        ]
                    },
                    "segment": {
                        "type": "object",
                        "required": ["start_seconds", "end_seconds"],
                        "additionalProperties": False,
                        "properties": {
                            "start_seconds": {"type": "number", "minimum": 0},
                            "end_seconds": {"type": "number", "exclusiveMinimum": 0},
                        },
                    },
                    "candidates": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 6,
                        "items": {
                            "type": "object",
                            "required": [
                                "key",
                                "name",
                                "version",
                                "source",
                                "channel",
                            ],
                            "additionalProperties": False,
                            "properties": {
                                "key": {
                                    "type": "string",
                                    "pattern": "^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$",
                                },
                                "name": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 120,
                                },
                                "version": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 200,
                                },
                                "source": {"type": "string", "minLength": 1},
                                "channel": {"type": "integer", "minimum": 0},
                                "reference": {"type": "boolean", "default": False},
                                "apply_alignment": {"type": "boolean", "default": False},
                                "apply_loudness": {"type": "boolean", "default": False},
                            },
                        },
                    },
                },
            },
        },
        "publish": {"type": "boolean", "default": False},
        "conversion": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "resample_to_16000": {
                    "type": "boolean",
                    "default": False,
                    "description": "显式许可把非 16 kHz 源重采样到 16 kHz；不许可时遇到非 16 kHz 源必须拒绝",
                },
                "note": {"type": "string", "maxLength": 500},
            },
        },
    },
}


class AgentError(Exception):
    """面向 Agent/用户的可读失败；message 可直接展示。"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: Any) -> None:
    """临时文件 + 同目录替换，保证 state/mapping 的原子落盘。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------- manifest 加载与离线校验 ----------


def load_manifest(manifest_path: str | Path) -> tuple[dict[str, Any], Path]:
    """解析并按 schema 校验 manifest；返回 (manifest, manifest 所在目录)。

    相对路径（候选 source）一律以 manifest 所在目录为基准。
    """
    path = Path(manifest_path)
    if not path.is_file():
        raise AgentError(f"manifest 文件不存在：{path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentError(f"manifest 无法读取或不是合法 JSON：{exc}") from exc
    try:
        jsonschema.validate(manifest, MANIFEST_SCHEMA)
    except jsonschema.ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path) or "(根)"
        raise AgentError(f"manifest 不符合 schema（{location}）：{exc.message}") from exc
    return manifest, path.resolve().parent


def _resolve_source(base_dir: Path, source: str) -> Path:
    """解析候选源路径：仅接受相对路径，解析后必须仍在 manifest 目录内。"""
    if Path(source).is_absolute() or source.startswith("~"):
        raise AgentError(
            f"候选源路径必须是相对 manifest 的路径，拒绝绝对路径：{source}"
        )
    resolved = (base_dir / source).resolve()
    base = base_dir.resolve()
    if not resolved.is_relative_to(base):
        raise AgentError(f"候选源路径逃逸出 manifest 目录：{source}")
    return resolved


def validate_manifest(manifest: dict[str, Any], base_dir: Path) -> list[str]:
    """离线校验（无服务副作用）：返回问题列表；空列表表示可用。

    检查：候选文件存在且可解码、每题候选数 2–6、同题长度一致、通道在源内、
    stable key 唯一、发布所需字段完整、参考候选唯一、非 16 kHz 源需显式许可。
    """
    problems: list[str] = []
    sample_keys: set[str] = set()
    samples = manifest["samples"]
    want_publish = manifest.get("publish", False)

    for sample in samples:
        key = sample["key"]
        if key in sample_keys:
            problems.append(f"片段 stable key 重复：{key}")
        sample_keys.add(key)
        candidate_keys: set[str] = set()
        lengths: dict[int, int] = {}
        references = 0
        needs_resample: list[str] = []
        for candidate in sample["candidates"]:
            ckey = candidate["key"]
            if ckey in candidate_keys:
                problems.append(f"候选 stable key 重复：{key}/{ckey}")
            candidate_keys.add(ckey)
            if candidate.get("reference"):
                references += 1
            try:
                source = _resolve_source(base_dir, candidate["source"])
            except AgentError as exc:
                problems.append(f"{key}/{ckey}: {exc}")
                continue
            if not source.is_file():
                problems.append(f"{key}/{ckey}: 源文件不存在：{candidate['source']}")
                continue
            import soundfile as sf

            try:
                info = sf.info(str(source))
            except Exception as exc:  # noqa: BLE001 - sf 对坏文件抛任意异常
                problems.append(f"{key}/{ckey}: 无法按音频读取（{exc}）")
                continue
            if info.samplerate != TARGET_RATE:
                needs_resample.append(ckey)
            if candidate["channel"] >= info.channels:
                problems.append(
                    f"{key}/{ckey}: channel={candidate['channel']} 超出源通道数 {info.channels}"
                )
            lengths.setdefault(info.frames, 0)
            lengths[info.frames] += 1
        if len(sample["candidates"]) < 2 or len(sample["candidates"]) > 6:
            problems.append(f"{key}: 候选数必须是 2–6，当前 {len(sample['candidates'])}")
        if len(lengths) > 1:
            detail = "、".join(f"{frames} 帧 ×{count}" for frames, count in lengths.items())
            problems.append(f"{key}: 同题候选长度不一致（{detail}）；请先向用户确认截取规则")
        if references != 1 and any(
            c.get("apply_alignment") or c.get("apply_loudness")
            for c in sample["candidates"]
        ):
            problems.append(f"{key}: 请求处理时必须且只能指定一个 reference 候选")
        if references > 1:
            problems.append(f"{key}: reference 候选最多一个，当前 {references} 个")
        if needs_resample and not manifest.get("conversion", {}).get(
            "resample_to_16000", False
        ):
            problems.append(
                f"{key}: 存在非 16 kHz 源（{'、'.join(needs_resample)}），"
                "但 manifest.conversion.resample_to_16000 未显式许可；"
                "请先与用户确认重采样规则后再设置"
            )
        if want_publish:
            missing = [
                field
                for field in ("name", "scene")
                if not (sample.get(field) or "").strip()
            ]
            if missing:
                problems.append(f"{key}: 发布要求字段缺失：{'、'.join(missing)}")
    if want_publish and len(samples) < 1:
        problems.append("发布要求至少一个片段")
    return problems


# ---------- 素材准备（绝不覆盖源文件）----------


def prepare_assets(
    manifest: dict[str, Any], base_dir: Path, output_dir: str | Path
) -> dict[str, Any]:
    """生成 16 kHz 单声道合规副本与 mapping.json；绝不修改源文件。

    转换顺序固定：按 channel 选通道 → 按 segment（源采样率）截取 →
    soxr VHQ 重采样到 16 kHz → float32 WAV。无归一化、无增益、无对齐。
    """
    output = Path(output_dir)
    output_resolved = output.resolve()
    # 输出目录不得与任何候选源目录重合，也不得包含它（防止副本混写源目录）。
    # 该检查优先于“非空”检查：安全性先于便利性。
    for sample in manifest["samples"]:
        for candidate in sample["candidates"]:
            source = _resolve_source(base_dir, candidate["source"])
            source_dir = source.parent.resolve()
            if output_resolved == source_dir or source_dir.is_relative_to(
                output_resolved
            ):
                raise AgentError(
                    f"输出目录 {output} 会覆盖候选源目录（{source_dir}）；请使用独立目录"
                )
    if output.exists() and any(output.iterdir()):
        raise AgentError(
            f"输出目录已存在且非空：{output}；请使用新的空目录，避免覆盖已有素材"
        )
    problems = validate_manifest(manifest, base_dir)
    if problems:
        raise AgentError(
            "素材尚未就绪，请先与用户确认以下问题：\n- " + "\n- ".join(problems)
        )

    import soundfile as sf
    import soxr

    conversion = manifest.get("conversion", {})
    allow_resample = conversion.get("resample_to_16000", False)
    mapping: dict[str, Any] = {
        "schema_version": 1,
        "target_rate": TARGET_RATE,
        "resample_licensed": allow_resample,
        "conversion_note": conversion.get("note", ""),
        "samples": [],
    }
    output.mkdir(parents=True, exist_ok=True)
    for sample in manifest["samples"]:
        segment = sample.get("segment")
        entry: dict[str, Any] = {
            "key": sample["key"],
            "name": sample["name"],
            "segment": segment,
            "candidates": [],
        }
        for candidate in sample["candidates"]:
            source = _resolve_source(base_dir, candidate["source"])
            info = sf.info(str(source))
            raw, rate = sf.read(str(source), dtype="float32", always_2d=True)
            channel = candidate["channel"]
            mono = raw[:, channel].copy()
            start = end = None
            if segment:
                start = round(segment["start_seconds"] * rate)
                end = round(segment["end_seconds"] * rate)
                start = max(0, min(start, len(mono)))
                end = max(start, min(end, len(mono)))
                mono = mono[start:end]
            transforms = []
            if segment:
                transforms.append(
                    f"截取 {segment['start_seconds']}–{segment['end_seconds']} 秒"
                )
            if rate != TARGET_RATE:
                if not allow_resample:
                    raise AgentError(
                        f"{sample['key']}/{candidate['key']}: 源采样率 {rate} Hz，"
                        "但 manifest 未显式许可重采样"
                    )
                mono = soxr.resample(mono, rate, TARGET_RATE, quality="VHQ")
                transforms.append(f"重采样 {rate}→{TARGET_RATE} Hz（soxr VHQ，确定性）")
            mono = mono.astype(np.float32)
            rel_out = Path("samples") / sample["key"] / f"{candidate['key']}.wav"
            dest = output / rel_out
            dest.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(dest), mono, TARGET_RATE, subtype="FLOAT")
            entry["candidates"].append(
                {
                    "key": candidate["key"],
                    "source": {
                        "path": candidate["source"],
                        "sha256": sha256_file(source),
                        "samplerate": rate,
                        "channels": info.channels,
                        "frames": info.frames,
                        "channel_used": channel,
                        "segment_frames": [start, end] if segment else None,
                    },
                    "output": {
                        "path": str(rel_out),
                        "sha256": sha256_file(dest),
                        # 服务端会以自身规范容器保存音频；逐样本哈希用于发布前复核。
                        "pcm_sha256": hashlib.sha256(
                            mono.astype(np.float32).tobytes()
                        ).hexdigest(),
                        "samplerate": TARGET_RATE,
                        "channels": 1,
                        "frames": len(mono),
                    },
                    "transforms": transforms,
                }
            )
        frames = {c["output"]["frames"] for c in entry["candidates"]}
        if len(frames) > 1:
            raise AgentError(
                f"{sample['key']}: 转换后同题候选样本数不一致（{sorted(frames)}）；"
                "请检查截取规则与源长度"
            )
        mapping["samples"].append(entry)
    atomic_write_json(output / "mapping.json", mapping)
    return mapping


# ---------- state（幂等与断点续传）----------


def manifest_snapshot(manifest: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """manifest 的规范化快照：重试时逐字段比对，检测被篡改的关键配置。"""
    samples = []
    for sample in manifest["samples"]:
        samples.append(
            {
                "key": sample["key"],
                "name": sample["name"],
                "scene": sample.get("scene", ""),
                "provenance": sample["provenance"],
                "segment": sample.get("segment"),
                "candidates": [
                    {
                        "key": c["key"],
                        "name": c["name"],
                        "version": c["version"],
                        "source": c["source"],
                        "channel": c["channel"],
                        "reference": bool(c.get("reference")),
                        "apply_alignment": bool(c.get("apply_alignment")),
                        "apply_loudness": bool(c.get("apply_loudness")),
                        "source_sha256": sha256_file(_resolve_source(base_dir, c["source"])),
                    }
                    for c in sample["candidates"]
                ],
            }
        )
    return {
        "task": manifest["task"],
        "participants": [
            {"name": p["name"]} for p in manifest.get("participants", [])
        ],
        "samples": samples,
        "publish": manifest.get("publish", False),
    }


def diff_snapshot(old: dict[str, Any], new: dict[str, Any], prefix="") -> list[str]:
    """字段级差异列表，供篡改反例展示；不做任何自动合并。"""
    diffs: list[str] = []
    keys = sorted(set(old) | set(new)) if isinstance(old, dict) and isinstance(new, dict) else []
    if not keys and old != new:
        return [f"{prefix or '值'}: {old!r} → {new!r}"]
    for key in keys:
        path = f"{prefix}.{key}" if prefix else str(key)
        if key not in old:
            diffs.append(f"{path}: (缺失) → {new[key]!r}")
        elif key not in new:
            diffs.append(f"{path}: {old[key]!r} → (被删除)")
        elif isinstance(old[key], dict) and isinstance(new[key], dict):
            diffs.extend(diff_snapshot(old[key], new[key], path))
        elif old[key] != new[key]:
            diffs.append(f"{path}: {old[key]!r} → {new[key]!r}")
    return diffs


class ApplyState:
    """apply 的状态文件：原子写、绑定服务实例、支持断点续传。"""

    def __init__(self, path: Path, payload: dict[str, Any] | None = None):
        self.path = Path(path)
        self.payload = payload or {
            "schema_version": STATE_SCHEMA_VERSION,
            "tool_version": VERSION,
            "server": None,
            "user": None,
            "manifest_path": None,
            "manifest_sha256": None,
            "snapshot": None,
            "task_id": None,
            "samples": {},
            "participants": {},
            "processing": {},
            "published": False,
        }

    @classmethod
    def load(cls, path: Path) -> "ApplyState | None":
        path = Path(path)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentError(f"state 文件损坏，请人工检查后删除重建：{exc}") from exc
        if payload.get("schema_version") != STATE_SCHEMA_VERSION:
            raise AgentError("state 文件版本不兼容，请使用新的 state 文件")
        return cls(path, payload)

    def save(self) -> None:
        atomic_write_json(self.path, self.payload)

    def sample_entry(self, key: str) -> dict[str, Any]:
        return self.payload["samples"].setdefault(key, {"sample_id": None, "tracks": {}})

    def candidate_entry(self, sample_key: str, candidate_key: str) -> dict[str, Any]:
        return self.sample_entry(sample_key)["tracks"].setdefault(candidate_key, {})


# ---------- 服务交互（只走公开 HTTP API）----------

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def check_server_url(url: str, allow_insecure_http: bool) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AgentError("服务 URL 不应包含凭据、查询或片段")
    host = parsed.hostname or ""
    if parsed.scheme == "https":
        pass
    elif parsed.scheme == "http":
        if host not in LOCAL_HOSTS and not allow_insecure_http:
            raise AgentError(
                f"明文 HTTP 仅允许 127.0.0.1/localhost；访问局域网 {host} "
                "必须显式加 --allow-insecure-http（凭据与音频将不加密传输，请确认网络环境）"
            )
    else:
        raise AgentError("服务 URL 必须以 http:// 或 https:// 开头")
    return url.rstrip("/")


class AgentClient:
    """面向编排的薄客户端：会话/CSRF 只在内存；错误不回显凭据。"""

    def __init__(self, base_url: str):
        import httpx

        self.base = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base, timeout=120.0)
        self.token: str | None = None
        self.csrf: str | None = None
        self.user: dict[str, Any] | None = None

    def close(self) -> None:
        self.http.close()

    def _post(self, path: str, **kwargs) -> dict[str, Any]:
        headers = kwargs.pop("headers", {})
        if self.csrf:
            headers["x-csrf-token"] = self.csrf
        response = self.http.post(path, headers=headers, **kwargs)
        return self._json(response)

    def _get(self, path: str, **kwargs) -> dict[str, Any]:
        return self._json(self.http.get(path, **kwargs))

    def _patch(self, path: str, **kwargs) -> dict[str, Any]:
        headers = kwargs.pop("headers", {})
        if self.csrf:
            headers["x-csrf-token"] = self.csrf
        return self._json(self.http.patch(path, headers=headers, **kwargs))

    @staticmethod
    def _json(response) -> dict[str, Any]:
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", "")
            except Exception:  # noqa: BLE001
                detail = ""
            raise AgentError(
                f"服务返回 {response.status_code}：{detail or response.reason_phrase}"
            )
        return response.json()

    def identity(self) -> dict[str, Any]:
        status = self._get("/api/status")
        me = {"version": status.get("version"), "needs_setup": status.get("needs_setup")}
        if status.get("version") != VERSION:
            raise AgentError(
                f"服务版本 {status.get('version')} 与 CLI {VERSION} 不一致，"
                "请先核对正在运行的服务"
            )
        return {**me, "instance_id": status.get("instance_id")}

    def login(self, user: str, password: str) -> dict[str, Any]:
        self._post("/api/login", json={"name": user, "password": password})
        me = self._get("/api/me")
        self.user = me
        self.csrf = me.get("csrf_token")
        return me

    def data_id(self) -> str:
        info = self._get("/api/server-info")
        return info["data_id"]

    def users(self) -> list[dict[str, Any]]:
        return self._get("/api/users")

    def create_task(self, task: dict[str, Any]) -> str:
        return self._post("/api/tasks", json=task)["id"]

    def task_detail(self, task_id: str) -> dict[str, Any]:
        return self._get(f"/api/tasks/{task_id}")

    def create_sample(self, task_id: str, payload: dict[str, Any]) -> str:
        return self._post(f"/api/tasks/{task_id}/samples", json=payload)["id"]

    def upload_track(self, sample_id: str, name: str, version: str, path: Path) -> str:
        with Path(path).open("rb") as handle:
            response = self.http.post(
                f"/api/samples/{sample_id}/tracks",
                data={"name": name, "version": version},
                files={"file": (path.name, handle, "audio/wav")},
                headers={"x-csrf-token": self.csrf or ""},
            )
        return self._json(response)["id"]

    def update_members(self, task_id: str, user_ids: list[str]) -> None:
        self._patch(f"/api/tasks/{task_id}/members", json={"users": user_ids})

    def analyze_alignment(self, sample_id: str, reference: str) -> dict[str, Any]:
        return self._get(
            f"/api/samples/{sample_id}/alignment", params={"reference": reference}
        )

    def apply_processing(self, sample_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(f"/api/samples/{sample_id}/processing", json=payload)

    def download_audio(self, track_id: str) -> bytes:
        response = self.http.get(f"/api/audio/{track_id}")
        if response.status_code >= 400:
            raise AgentError(f"音频下载失败 {response.status_code}（track {track_id}）")
        return response.content

    def publish(self, task_id: str, user_ids: list[str]) -> None:
        self._post(
            f"/api/tasks/{task_id}/publish",
            json={"users": user_ids, "alignment_confirmed": True},
        )


def resolve_participants(
    client: AgentClient, manifest: dict[str, Any]
) -> dict[str, str]:
    """把 participants 解析为稳定 user ID：精确名或精确 ID；歧义/不存在拒绝。"""
    users = client.users()
    resolved: dict[str, str] = {}
    problems: list[str] = []
    for participant in manifest.get("participants", []):
        name = participant["name"]
        matches = [u for u in users if u["name"] == name or u["id"] == name]
        if not matches:
            problems.append(f"参与者不存在：{name}")
        elif len(matches) > 1:
            problems.append(f"参与者歧义（{name} 命中 {len(matches)} 个账号）")
        else:
            resolved[name] = matches[0]["id"]
    if problems:
        raise AgentError(
            "参与者解析失败，请修正 manifest 后重试：\n- " + "\n- ".join(problems)
        )
    return resolved


# ---------- apply：服务端编排（幂等、断点续传）----------


def _publish_guard(manifest: dict[str, Any], publish_flag: bool) -> bool:
    """发布双确认：manifest.publish 与 CLI --publish 必须同时为真。"""
    want = manifest.get("publish", False)
    if publish_flag and not want:
        raise AgentError(
            "CLI 传入了 --publish，但 manifest.publish 为 false；请先在 manifest 中显式设置 publish=true"
        )
    return bool(want) and publish_flag


def _verify_server_binding(state: ApplyState, identity: dict[str, Any], data_id: str) -> None:
    recorded = state.payload["server"]
    if not recorded:
        return
    if recorded["instance_id"] != identity.get("instance_id") or recorded["data_id"] != data_id:
        raise AgentError(
            "state 文件绑定的是另一服务实例/数据目录"
            f"（instance {recorded['instance_id']} / data {recorded['data_id']}），"
            "当前服务不匹配；请确认连接的服务，或显式新建 state 文件"
        )


def run_apply(
    manifest_path: str | Path,
    server_url: str,
    user: str,
    password: str,
    state_path: str | Path,
    mapping_path: str | Path | None,
    publish_flag: bool = False,
    allow_insecure_http: bool = False,
    keep_original_on_rejection: bool = False,
) -> dict[str, Any]:
    """把 manifest 编排到正在运行的服务；返回 JSON 回执。

    幂等：同一 manifest + state 重试不会重复创建任务/片段/候选；
    每步成功立即原子落盘；manifest 关键配置或源文件变化时停止并列出差异。
    """
    manifest, base = load_manifest(manifest_path)
    problems = validate_manifest(manifest, base)
    if problems:
        raise AgentError("manifest 离线校验未通过：\n- " + "\n- ".join(problems))
    url = check_server_url(server_url, allow_insecure_http)
    want_publish = _publish_guard(manifest, publish_flag)

    state = ApplyState.load(Path(state_path))
    if state is None:
        state = ApplyState(Path(state_path))

    client = AgentClient(url)
    try:
        identity = client.identity()
        client.login(user, password)
        data_id = client.data_id()
        _verify_server_binding(state, identity, data_id)
        state.payload["server"] = {
            "base": url,
            "version": identity.get("version"),
            "instance_id": identity.get("instance_id"),
            "data_id": data_id,
        }
        state.payload["user"] = user

        snapshot = manifest_snapshot(manifest, base)
        if state.payload["snapshot"] is None:
            state.payload["snapshot"] = snapshot
            state.payload["manifest_path"] = str(Path(manifest_path).resolve())
            state.payload["manifest_sha256"] = sha256_file(Path(manifest_path))
        else:
            diffs = diff_snapshot(state.payload["snapshot"], snapshot)
            if diffs:
                raise AgentError(
                    "manifest 关键配置或源文件与上次运行存在差异，已停止；"
                    "请恢复输入或使用新的 state 文件。字段级差异：\n- "
                    + "\n- ".join(diffs)
                )
        state.save()

        # 任务：首次创建，之后校验仍存在且可管理。
        if state.payload["task_id"] is None:
            task_id = client.create_task(manifest["task"])
            state.payload["task_id"] = task_id
            state.save()
        else:
            task_id = state.payload["task_id"]
            detail = client.task_detail(task_id)
            if not detail.get("can_manage"):
                raise AgentError(
                    f"state 中的任务 {task_id} 对当前账号不可管理；请确认账号与服务"
                )
            if detail["status"] != "draft":
                raise AgentError(
                    f"任务 {task_id} 已是 {detail['status']} 状态，不可继续修改"
                )

        # mapping：首次必须提供；state 记录路径。
        if state.payload.get("mapping_path") is None:
            if mapping_path is None:
                raise AgentError(
                    "首次 apply 需要 --mapping 指向 prepare 生成的 mapping.json"
                )
            state.payload["mapping_path"] = str(Path(mapping_path).resolve())
        mapping_file = Path(state.payload["mapping_path"])
        if not mapping_file.is_file():
            raise AgentError(f"mapping.json 不存在：{mapping_file}")
        mapping = json.loads(mapping_file.read_text(encoding="utf-8"))
        mapping_by_key = {entry["key"]: entry for entry in mapping["samples"]}

        receipt_samples: list[dict[str, Any]] = []
        for sample in manifest["samples"]:
            entry = state.sample_entry(sample["key"])
            if entry["sample_id"] is None:
                try:
                    entry["sample_id"] = client.create_sample(
                        task_id,
                        {
                            "name": sample["name"],
                            "scene": sample.get("scene", ""),
                            "provenance": sample["provenance"],
                        },
                    )
                    state.save()
                except AgentError as exc:
                    if "同名样本已存在" in str(exc):
                        raise AgentError(
                            f"服务端已存在同名样本「{sample['name']}」，而 state 未记录；"
                            "为避免重复建题已停止。请确认是否连接了正确服务，或使用新的 state"
                        ) from exc
                    raise
            mapping_sample = mapping_by_key.get(sample["key"])
            if not mapping_sample:
                raise AgentError(f"mapping.json 缺少片段 {sample['key']} 的条目")
            tracks_view: list[dict[str, Any]] = []
            for candidate in sample["candidates"]:
                centry = entry["tracks"].setdefault(candidate["key"], {})
                mapping_candidate = next(
                    (
                        c
                        for c in mapping_sample["candidates"]
                        if c["key"] == candidate["key"]
                    ),
                    None,
                )
                if not mapping_candidate:
                    raise AgentError(
                        f"mapping.json 缺少候选 {sample['key']}/{candidate['key']}"
                    )
                copy_path = (
                    mapping_file.parent / mapping_candidate["output"]["path"]
                )
                if not copy_path.is_file():
                    raise AgentError(f"合规副本缺失：{copy_path}")
                actual_sha = sha256_file(copy_path)
                if actual_sha != mapping_candidate["output"]["sha256"]:
                    raise AgentError(
                        f"合规副本 SHA256 与 mapping 不符：{copy_path}"
                    )
                if centry.get("track_id"):
                    tracks_view.append(
                        {
                            "key": candidate["key"],
                            "track_id": centry["track_id"],
                            "stage": "already-uploaded",
                        }
                    )
                    continue
                if centry.get("source_sha256") not in (None, actual_sha):
                    raise AgentError(f"副本 SHA 与 state 记录不一致：{candidate['key']}")
                track_id = client.upload_track(
                    entry["sample_id"],
                    candidate["name"],
                    candidate["version"],
                    copy_path,
                )
                centry.update(
                    {
                        "track_id": track_id,
                        "stage": "uploaded",
                        "output_sha256": actual_sha,
                        "source_sha256": actual_sha,
                    }
                )
                state.save()
                tracks_view.append(
                    {"key": candidate["key"], "track_id": track_id, "stage": "uploaded"}
                )
            receipt_samples.append(
                {"key": sample["key"], "sample_id": entry["sample_id"], "tracks": tracks_view}
            )

        # 参与者：解析为稳定 ID 后立即增补受邀名单（幂等；closed 任务会被服务端拒绝）。
        participants_resolved: dict[str, str] = dict(state.payload["participants"])
        if manifest.get("participants") and not participants_resolved:
            participants_resolved = resolve_participants(client, manifest)
            if participants_resolved:
                client.update_members(task_id, sorted(set(participants_resolved.values())))
            state.payload["participants"] = participants_resolved
            state.save()

        # 对齐与响度：服务端判据拒绝时默认停止。
        processing_receipt: list[dict[str, Any]] = []
        rejections: list[str] = []
        for sample in manifest["samples"]:
            requested = [
                c for c in sample["candidates"] if c.get("apply_alignment") or c.get("apply_loudness")
            ]
            if not requested:
                continue
            key = sample["key"]
            if state.payload["processing"].get(key, {}).get("applied"):
                continue
            reference = next(c for c in sample["candidates"] if c.get("reference"))
            entry = state.sample_entry(key)
            ref_track = entry["tracks"][reference["key"]]["track_id"]
            analysis = client.analyze_alignment(entry["sample_id"], ref_track)
            items = []
            for candidate in requested:
                target = next(
                    (r for r in analysis["候选"] if r["track_id"] == entry["tracks"][candidate["key"]]["track_id"]),
                    None,
                )
                if target is None:
                    raise AgentError(f"{key}: 对齐分析缺少候选 {candidate['key']}")
                if candidate.get("apply_alignment") and not target["延迟"]["可应用"]:
                    rejections.append(
                        f"{key}/{candidate['key']}: 对齐拒绝（{target['延迟']['拒绝码']}）"
                    )
                    continue
                if candidate.get("apply_loudness") and not target["响度"]["可应用"]:
                    rejections.append(
                        f"{key}/{candidate['key']}: 响度拒绝（{target['响度']['拒绝码']}）"
                    )
                    continue
                items.append(
                    {
                        "候选": entry["tracks"][candidate["key"]]["track_id"],
                        "对齐": bool(candidate.get("apply_alignment")),
                        "响度": bool(candidate.get("apply_loudness")),
                    }
                )
            result: dict[str, Any] = {"applied": False, "items": []}
            if items:
                outcome = client.apply_processing(
                    entry["sample_id"], {"参考": ref_track, "处理": items}
                )
                result = {"applied": True, "items": outcome.get("处理结果", items)}
            state.payload["processing"][key] = result
            state.save()
            processing_receipt.append({"key": key, **result})
        if rejections and not keep_original_on_rejection:
            raise AgentError(
                "服务端拒绝部分对齐/响度处理，默认停止发布（可保留原始并继续草稿）：\n- "
                + "\n- ".join(rejections)
            )

        # 发布前重拉任务并复核。
        detail = client.task_detail(task_id)
        checks: list[str] = []
        if len(detail.get("samples") or []) != len(manifest["samples"]):
            checks.append(
                f"片段数不符：服务端 {len(detail.get('samples') or [])}，manifest {len(manifest['samples'])}"
            )
        for sample, view in zip(manifest["samples"], detail.get("samples") or []):
            if view["track_count"] != len(sample["candidates"]):
                checks.append(
                    f"{sample['key']}: 候选数不符（服务端 {view['track_count']}）"
                )
        assigned = set(detail.get("review_assignments") or [])
        for name, user_id in participants_resolved.items():
            if user_id not in assigned:
                checks.append(f"参与者未在受邀名单：{name}")
        for sample in manifest["samples"]:
            entry = state.sample_entry(sample["key"])
            mapping_sample = mapping_by_key[sample["key"]]
            for candidate, view in zip(
                sample["candidates"], detail.get("samples") or []
            ):
                if view["id"] != entry["sample_id"]:
                    continue
                if view["track_count"] != len(sample["candidates"]):
                    continue
            # SHA 复核：逐候选下载服务端音频并与 mapping 比对。
            for candidate in sample["candidates"]:
                track_id = entry["tracks"][candidate["key"]]["track_id"]
                mapping_candidate = next(
                    c
                    for c in mapping_by_key[sample["key"]]["candidates"]
                    if c["key"] == candidate["key"]
                )
                payload = client.download_audio(track_id)
                import io as _io

                import soundfile as _sf

                samples, _rate = _sf.read(_io.BytesIO(payload), dtype="float32")
                actual = hashlib.sha256(
                    np.asarray(samples, dtype=np.float32).tobytes()
                ).hexdigest()
                if actual != mapping_candidate["output"]["pcm_sha256"]:
                    checks.append(
                        f"{sample['key']}/{candidate['key']}: 服务端音频样本与 mapping 不符"
                    )
        if checks:
            raise AgentError("发布前检查未通过：\n- " + "\n- ".join(checks))

        published = False
        if want_publish:
            member_ids = sorted(
                set(participants_resolved.values()) | {client.user["id"]}
            )
            client.update_members(task_id, member_ids)
            client.publish(task_id, member_ids)
            state.payload["published"] = True
            state.save()
            published = True

        return {
            "ok": True,
            "task_id": task_id,
            "server": {
                "base": url,
                "version": state.payload["server"]["version"],
                "instance_id": state.payload["server"]["instance_id"],
                "data_id": data_id,
            },
            "state_path": str(Path(state_path)),
            "mapping_path": str(mapping_file),
            "samples": receipt_samples,
            "participants": participants_resolved,
            "processing": processing_receipt,
            "processing_rejections": rejections,
            "published": published,
            "anonymous_mapping_note": "盲评任务的候选真实身份与匿名映射在任务关闭前不会对参与者揭晓",
        }
    finally:
        client.close()
