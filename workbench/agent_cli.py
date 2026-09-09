"""`audio-eval agent ...` 子命令：manifest、素材准备与服务端编排的薄封装。

逻辑全部位于 workbench.agent_tasks；本模块只做参数解析、凭据读取与输出。
凭据规则：密码只允许 stdin 或指定环境变量读取，禁止命令行取值；
错误输出不回显密码、Cookie 或 CSRF。
"""

import getpass
import json
import sys
from pathlib import Path

from .agent_tasks import (
    AgentError,
    ApplyState,
    load_manifest,
    prepare_assets,
    run_apply,
    validate_manifest,
)

MANIFEST_TEMPLATE = {
    "schema_version": 1,
    "task": {
        "title": "示例任务 · 请修改为实际标题",
        "kind": "算法版本",
        "mode": "development",
    },
    "participants": [{"name": "同事账号名（可留空数组）"}],
    "samples": [
        {
            "key": "room-001",
            "name": "室内 001",
            "scene": "安静室内（发布必填）",
            "provenance": "PUBLIC reproducible",
            "segment": {"start_seconds": 0.0, "end_seconds": 10.0},
            "candidates": [
                {
                    "key": "candidate-a",
                    "name": "候选 A",
                    "version": "commit 或模型 SHA",
                    "source": "sources/candidate-a/room-001.wav",
                    "channel": 0,
                    "reference": True,
                    "apply_alignment": False,
                    "apply_loudness": False,
                },
                {
                    "key": "candidate-b",
                    "name": "候选 B",
                    "version": "commit 或模型 SHA",
                    "source": "sources/candidate-b/room-001.wav",
                    "channel": 0,
                    "reference": False,
                    "apply_alignment": False,
                    "apply_loudness": False,
                },
            ],
        }
    ],
    "publish": False,
    "conversion": {
        "resample_to_16000": False,
        "note": "源为 16 kHz 时保持 false；非 16 kHz 源需与用户确认后改为 true",
    },
}


def _read_password(args) -> str:
    """密码只允许 stdin 或指定环境变量读取；绝不接受命令行取值。"""
    if args.password_stdin:
        print("请在下一行输入服务登录密码（输入不会回显到日志）：", file=sys.stderr)
        password = getpass.getpass()
    elif args.password_env:
        password = __import__("os").environ.get(args.password_env, "")
    else:
        raise AgentError("必须使用 --password-stdin 或 --password-env 提供密码")
    if not password:
        raise AgentError("密码为空；请检查输入或环境变量")
    if len(password) < 10:
        raise AgentError("密码长度不足（至少 10 个字符）")
    return password


def cmd_manifest_init(args) -> int:
    output = Path(args.output)
    if output.exists():
        raise AgentError(f"目标已存在，不覆盖：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(MANIFEST_TEMPLATE, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    # 按调用入口提示正确的校验命令（独立 EXE 与服务端 CLI 的程序名不同）。
    program = getattr(args, "entry_program", "audio-eval")
    print(f"已生成 manifest 模板：{output}（请编辑后运行 {program} validate 校验）")
    return 0


def cmd_validate(args) -> int:
    manifest, base = load_manifest(args.manifest)
    problems = validate_manifest(manifest, base)
    payload = {"ok": not problems, "problems": problems, "manifest": str(Path(args.manifest).resolve())}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not problems else 1


def cmd_prepare(args) -> int:
    manifest, base = load_manifest(args.manifest)
    mapping = prepare_assets(manifest, base, args.output_dir)
    print(json.dumps({"ok": True, "output_dir": str(Path(args.output_dir).resolve()), "mapping": str((Path(args.output_dir) / "mapping.json").resolve()), "samples": len(mapping["samples"])}, ensure_ascii=False, indent=2))
    return 0


def cmd_apply(args) -> int:
    password = _read_password(args)
    receipt = run_apply(
        args.manifest,
        args.server,
        args.user,
        password,
        args.state,
        args.mapping,
        publish_flag=args.publish,
        allow_insecure_http=args.allow_insecure_http,
        keep_original_on_rejection=args.keep_original_on_rejection,
    )
    if args.json:
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
    else:
        state = "已发布" if receipt["published"] else "草稿（未发布）"
        print(f"任务 {receipt['task_id']} · {state}")
        for sample in receipt["samples"]:
            tracks = "、".join(
                f"{t['key']}→{t['track_id']}（{t['stage']}）" for t in sample["tracks"]
            )
            print(f"  片段 {sample['key']}：{tracks}")
        if receipt["processing_rejections"]:
            print("处理被拒绝（未发布）：")
            for item in receipt["processing_rejections"]:
                print(f"  - {item}")
        print(f"state：{receipt['state_path']}")
    return 0


def cmd_status(args) -> int:
    state = ApplyState.load(Path(args.state))
    if state is None:
        raise AgentError(f"state 文件不存在：{args.state}")
    payload = state.payload
    summary = {
        "state_path": str(Path(args.state).resolve()),
        "server": payload["server"],
        "user": payload["user"],
        "manifest_path": payload["manifest_path"],
        "manifest_sha256": payload["manifest_sha256"],
        "task_id": payload["task_id"],
        "published": payload["published"],
        "samples": [
            {
                "key": key,
                "sample_id": entry["sample_id"],
                "tracks": {
                    tkey: {"track_id": tvalue.get("track_id"), "stage": tvalue.get("stage")}
                    for tkey, tvalue in entry["tracks"].items()
                },
            }
            for key, entry in payload["samples"].items()
        ],
        "participants": payload["participants"],
        "processing": payload["processing"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def register(parser, entry_program: str = "audio-eval") -> None:
    """注册 agent 子命令；entry_program 用于提示文案中的程序名。"""
    sub = parser.add_subparsers(dest="agent_command", required=True)

    init = sub.add_parser("manifest-init", help="生成 manifest 模板（JSON）")
    init.add_argument("--output", required=True, help="模板输出路径")
    init.set_defaults(func=cmd_manifest_init, entry_program=entry_program)

    validate = sub.add_parser("validate", help="离线校验 manifest（无服务副作用）")
    validate.add_argument("manifest", help="manifest JSON 路径")
    validate.set_defaults(func=cmd_validate)

    prepare = sub.add_parser(
        "prepare", help="按 manifest 生成 16 kHz 单声道合规副本与 mapping.json（不覆盖源）"
    )
    prepare.add_argument("manifest", help="manifest JSON 路径")
    prepare.add_argument("--output-dir", required=True, help="副本输出目录（必须是新的空目录）")
    prepare.set_defaults(func=cmd_prepare)

    apply = sub.add_parser("apply", help="把 manifest 编排到正在运行的服务（幂等、断点续传）")
    apply.add_argument("manifest", help="manifest JSON 路径")
    apply.add_argument("--server", required=True, help="服务地址（默认仅允许本机/HTTPS）")
    apply.add_argument("--user", required=True, help="组织者或管理员账号名")
    auth = apply.add_mutually_exclusive_group(required=True)
    auth.add_argument("--password-stdin", action="store_true", help="从 stdin 读取密码")
    auth.add_argument("--password-env", help="从指定环境变量读取密码")
    apply.add_argument("--state", required=True, help="编排状态文件路径（原子写）")
    apply.add_argument("--mapping", help="prepare 生成的 mapping.json（首次 apply 必填）")
    apply.add_argument(
        "--publish",
        action="store_true",
        help="请求发布；还必须 manifest.publish=true 才会真正发布",
    )
    apply.add_argument("--json", action="store_true", help="以 JSON 回执输出")
    apply.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="允许局域网明文 HTTP（凭据与音频不加密；仅限获准的可信内网）",
    )
    apply.add_argument(
        "--keep-original-on-rejection",
        action="store_true",
        help="对齐/响度被服务端拒绝时保留原始素材继续草稿（绝不静默发布混合口径）",
    )
    apply.set_defaults(func=cmd_apply)

    status = sub.add_parser("status", help="查看 state 编排进度（JSON）")
    status.add_argument("--state", required=True)
    status.add_argument("--json", action="store_true", help="以 JSON 输出")
    status.set_defaults(func=cmd_status)


def run(args) -> int:
    try:
        return args.func(args)
    except AgentError as exc:
        # 错误消息只含可读原因，不回显密码、Cookie 或 CSRF。
        print(f"agent 失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - 顶层兜底；异常文本不含凭据（凭据只在请求体内）
        print(f"agent 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
