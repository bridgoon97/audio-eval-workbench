"""局域网协作 API。音频、匿名映射和评论始终经过授权检查。"""

import csv
import hashlib
import io
import json
import re
import secrets
import sqlite3
import threading
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import numpy as np
import soundfile as sf
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .align import (
    ALGORITHM_NAME,
    ERR_DELAY_NOT_APPLICABLE,
    LAG_SIGN_DEFINITION,
    PROCESSING_ORDER,
    REASONS,
    apply_gain,
    estimate_delay,
    estimate_gain,
    shift_samples,
)
from .audio import analyze, decode_audio, demo_wav, encode_float_wav
from .db import connect, init, password_hash, verify
from .version import VERSION


def uid():
    return secrets.token_hex(16)


def now():
    return datetime.now(UTC).isoformat()


STATUS_NAMES = {"draft": "准备中", "active": "评测进行中", "closed": "已揭晓"}
MODE_NAMES = {"development": "开发诊断", "blind": "隐藏版本评测"}
CSV_HEADER = ["部分", "条目ID", "片段ID", "片段名称", "条目", "标签", "数值", "分母", "百分比", "说明"]

# 自助申请与设备登录：申请状态机 pending -> approved|rejected|expired，终态不可被普通重试反转。
DEVICE_COOKIE = "device"
DEVICE_TTL = 180 * 86400  # 设备令牌绝对过期：180 天，不滑动续期。
CLAIM_GRACE = 15 * 60  # 领取重试窗口：批准后首次领取 15 分钟内可重试并轮换令牌。
APPLICATION_STATES = ("pending", "approved", "rejected", "expired")
STATE_NAMES = {"pending": "待审批", "approved": "已批准", "rejected": "已拒绝", "expired": "已过期"}


def client_ip(request):
    """恒用 socket 对端地址作为审计与限流依据。
    不读取任何转发头：uvicorn 以 proxy_headers=False 启动（见 cli.py），
    X-Forwarded-For 等字段一律不信任，因此伪造请求头不能绕过限流，
    也不能参与身份判断。部署在可信反代后时，peer 是代理地址，限流按代理共亨口径，宁可偏严。"""
    return request.client.host if request.client else "local"


def simplify_device(user_agent):
    """把 User-Agent 缩减为“浏览器 · 系统”两级摘要；绝不返回原始 UA 噪声。"""
    ua = (user_agent or "").strip()
    if not ua:
        return "未知客户端"
    browser = ""
    for token, name in (
        ("Edg/", "Edge"),
        ("OPR/", "Opera"),
        ("Chrome/", "Chrome"),
        ("Firefox/", "Firefox"),
        ("Version/", "Safari"),
    ):
        index = ua.find(token)
        if index >= 0:
            major = ua[index + len(token) :].split(".")[0]
            browser = f"{name} {major}" if major[:1].isdigit() else name
            break
    system = ""
    for token, name in (
        ("Windows NT", "Windows"),
        ("Mac OS X", "macOS"),
        ("Android", "Android"),
        ("iPhone", "iOS"),
        ("iPad", "iPadOS"),
        ("X11", "Linux"),
        ("Linux", "Linux"),
    ):
        if token in ua:
            system = name
            break
    if not browser and not system:
        return "未知客户端"
    return " · ".join(part for part in (browser, system) if part)


def share_text(count, denominator):
    """百分比必须写明分母；没有有效提交时不呈现百分比。"""
    if not denominator:
        return "—"
    return f"{100 * count / denominator:.1f}%（{count}/{denominator}）"


def csv_safe(value):
    """在 CSV 序列化边界防止 Excel 公式注入：原始首字符为制表/回车/换行，
    或去除前导空白后以 = + - @ 开头的文本单元，加单引号前缀变为纯文本；
    数值单元保持数值含义，不做转换。"""
    if isinstance(value, str) and (
        value[:1] in ("\t", "\r", "\n") or value.lstrip()[:1] in ("=", "+", "-", "@")
    ):
        return "'" + value
    return value


def task_summary(db, task_id):
    """复盘汇总。口径固定：
    - 受邀评测者名单只来自 review_assignments；负责人自动拥有访问权限，
      但不因 owner 身份或自动成员行成为受邀评测者。
    - 完成度三态：已完成＝已提交全部片段（片段总数>0）；进行中＝已提交
      部分片段；未开始＝尚未提交。评论和回复不算提交。
    - 每个片段的分母是该片段的有效提交人数（ratings 每人每片段至多一行）。
    - 分歧＝同一片段出现两种及以上不同偏好，仅描述，不做显著性判断。
    - 标签只统计根评论，回复楼层不计入。
    """
    assigned = db.execute(
        "SELECT u.id AS id, u.name AS name FROM review_assignments a JOIN users u ON u.id=a.user_id "
        "WHERE a.task_id=? ORDER BY u.name, u.id",
        (task_id,),
    ).fetchall()
    samples = db.execute(
        "SELECT id, name FROM samples WHERE task_id=? ORDER BY rowid", (task_id,)
    ).fetchall()
    rated: dict[str, set[str]] = {}
    choices: dict[str, list[str]] = {}
    # 票数与分母只统计受邀评测者的评分；旧库异常数据中的未受邀评分不进入汇总。
    for row in db.execute(
        "SELECT r.sample_id, r.user_id, r.choice FROM ratings r "
        "JOIN samples s ON s.id=r.sample_id WHERE s.task_id=? AND r.user_id IN "
        "(SELECT user_id FROM review_assignments WHERE task_id=?)",
        (task_id, task_id),
    ):
        choices.setdefault(row["sample_id"], []).append(row["choice"])
        rated.setdefault(row["user_id"], set()).add(row["sample_id"])

    def state_of(count):
        if len(samples) and count == len(samples):
            return "已完成"
        return "进行中" if count else "未开始"

    states = {"已完成": [], "进行中": [], "未开始": []}
    members_view = []
    for m in assigned:
        count = len(rated.get(m["id"], ()))
        state = state_of(count)
        states[state].append(m["name"])
        members_view.append(
            {"ID": m["id"], "名称": m["name"], "已提交片段数": count, "状态": state}
        )
    progress = {
        "受邀评测者": len(assigned),
        "已完成": len(states["已完成"]),
        "进行中": len(states["进行中"]),
        "未开始": len(states["未开始"]),
        "已完成名单": states["已完成"],
        "进行中名单": states["进行中"],
        "未开始名单": states["未开始"],
        "成员": members_view,
    }
    per_sample = {}
    for sample in samples:
        picked = choices.get(sample["id"], [])
        counts: dict[str, int] = {}
        for choice in picked:
            counts[choice] = counts.get(choice, 0) + 1
        votes = [
            {
                "ID": track["id"],
                "名称": track["name"],
                "票数": counts.get(track["id"], 0),
            }
            for track in db.execute(
                "SELECT id, name FROM tracks WHERE sample_id=? ORDER BY rowid",
                (sample["id"],),
            )
        ]
        votes.append({"ID": "tie", "名称": "无明显差异", "票数": counts.get("tie", 0)})
        per_sample[sample["id"]] = {
            "分母": len(picked),
            "票数": votes,
            "分歧": len(set(picked)) >= 2,
        }
    raw_tags: dict[str, dict[str, int]] = {}
    for row in db.execute(
        "SELECT c.tag, c.sample_id FROM comments c JOIN samples s ON s.id=c.sample_id "
        "WHERE s.task_id=? AND c.parent IS NULL ORDER BY c.created, c.id",
        (task_id,),
    ):
        per_tag = raw_tags.setdefault(row["tag"], {})
        per_tag[row["sample_id"]] = per_tag.get(row["sample_id"], 0) + 1
    names = {sample["id"]: sample["name"] for sample in samples}
    tags = [
        {
            "标签": tag,
            "根评论数": sum(per_sample_tag.values()),
            "片段": [
                {"ID": sample_id, "名称": names.get(sample_id, ""), "根评论数": count}
                for sample_id, count in per_sample_tag.items()
            ],
        }
        for tag, per_sample_tag in raw_tags.items()
    ]
    return {"参与进度": progress, "逐片段": per_sample, "标签汇总": tags}


def results_csv(value) -> bytes:
    """UTF-8 BOM + CRLF，中文 Windows Excel 可直接打开；单一文件分四部分。"""
    task = value["任务"]
    samples = value["样本"]
    progress = value["参与进度"]
    rows = [CSV_HEADER]
    rows += [
        ["任务信息", "", "", "", label, "", text, "", "", ""]
        for label, text in (
            ("任务ID", task["id"]),
            ("任务名称", task["title"]),
            ("任务状态", STATUS_NAMES.get(task["status"], task["status"])),
            ("评测模式", MODE_NAMES.get(task["mode"], task["mode"])),
            ("比较类型", task["kind"]),
            ("生成时间", value["生成时间"]),
        )
    ]
    rows += [
        ["参与进度", "", "", "", label, "", progress[key], "", "", note]
        for label, key, note in (
            ("受邀评测者", "受邀评测者", "名单来自评测分配，负责人不自动受邀"),
            ("已完成人数", "已完成", "已提交全部片段"),
            ("进行中人数", "进行中", "已提交部分片段"),
            ("未开始人数", "未开始", "尚未提交任何偏好"),
        )
    ]
    for member in progress["成员"]:
        rows.append(
            [
                "参与进度",
                member["ID"],
                "",
                "",
                member["名称"],
                "",
                member["已提交片段数"],
                len(samples),
                share_text(member["已提交片段数"], len(samples)) if samples else "—",
                member["状态"],
            ]
        )
    for sample in samples:
        if not sample["分母"]:
            note = "该片段暂无提交"
        elif sample["分歧"]:
            note = "存在分歧：出现两种及以上不同偏好"
        else:
            note = "无分歧"
        for vote in sample["票数"]:
            rows.append(
                [
                    "逐片段偏好",
                    vote["ID"],
                    sample["id"],
                    sample["name"],
                    vote["名称"],
                    "",
                    vote["票数"],
                    sample["分母"],
                    share_text(vote["票数"], sample["分母"]),
                    note,
                ]
            )
    for sample in samples:
        summary = sample.get("处理摘要") or {}
        for track in sample["tracks"]:
            proc = track.get("处理口径")
            if not isinstance(proc, dict):
                continue
            rows.append(
                [
                    "处理口径",
                    track["id"],
                    sample["id"],
                    sample["name"],
                    proc["模式"],
                    "",
                    proc["lag"],
                    "",
                    "",
                    (
                        f"增益 {proc['增益db']:+.2f} dB；参考 {proc['参考track']}；"
                        f"{proc['算法']}；派生SHA256 {proc['派生资产SHA256']}"
                    ),
                ]
            )
        for rejection in summary.get("拒绝建议", []):
            rows.append(
                [
                    "处理口径",
                    "",
                    sample["id"],
                    sample["name"],
                    rejection["候选"],
                    "",
                    "",
                    "",
                    "",
                    f"{rejection['判据']}建议被拒绝（{rejection['拒绝码']}）：{rejection['原因']}",
                ]
            )
    for tag in value["标签汇总"]:
        location = "；".join(
            f"{part['名称']}（ID：{part['ID']}，{part['根评论数']} 条）" for part in tag["片段"]
        )
        rows.append(
            [
                "问题标签",
                "",
                "",
                "",
                tag["标签"],
                tag["标签"],
                tag["根评论数"],
                "",
                "",
                f"涉及片段：{location}；仅统计根评论，回复不计入",
            ]
        )
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerows([[csv_safe(cell) for cell in row] for row in rows])
    return buffer.getvalue().encode("utf-8-sig")


def md_cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def results_markdown(value) -> str:
    task = value["任务"]
    progress = value["参与进度"]
    total = len(value["样本"])
    lines = [
        f"# 听鉴结果报告 · {task['title']}",
        "",
        f"- 任务 ID：`{task['id']}`",
        f"- 状态：{STATUS_NAMES.get(task['status'], task['status'])}（{task['status']}）",
        f"- 模式：{MODE_NAMES.get(task['mode'], task['mode'])}（{task['mode']}）",
        f"- 比较类型：{task['kind']}",
        f"- 生成时间：{value['生成时间']}",
        "",
        (
            "> 分母口径：每个片段的百分比以该片段的有效提交人数为分母，每人每片段至多一票；"
            "评论、回复和未提交者不计入票数。分歧只是“同一片段出现两种及以上不同偏好”的描述性提示，"
            "不代表统计显著，也不说明算法优劣。"
        ),
        "",
        "## 参与进度",
        "",
        "受邀名单来自评测分配；负责人自动拥有访问权限，但不自动成为受邀评测者。",
        "",
        (
            f"- 受邀评测者 {progress['受邀评测者']} 人；已完成 {progress['已完成']} 人；"
            f"进行中 {progress['进行中']} 人；未开始 {progress['未开始']} 人。"
        ),
        f"- 已完成：{'、'.join(progress['已完成名单']) or '—'}",
        f"- 进行中：{'、'.join(progress['进行中名单']) or '—'}",
        f"- 未开始：{'、'.join(progress['未开始名单']) or '—'}",
        "",
        "| 受邀评测者 | 已提交片段 | 状态 |",
        "| --- | --- | --- |",
    ]
    for member in progress["成员"]:
        state = "已提交" if member["已提交片段数"] else "未提交"
        lines.append(
            f"| {md_cell(member['名称'])} | {member['已提交片段数']}/{total} | {state} |"
        )
    lines += ["", "## 逐片段偏好", ""]
    for sample in value["样本"]:
        distinct = len({vote["ID"] for vote in sample["票数"] if vote["票数"]})
        if not sample["分母"]:
            hint = "该片段暂无提交。"
        elif sample["分歧"]:
            hint = f"**存在分歧**：出现 {distinct} 种不同偏好（描述性提示）。"
        else:
            hint = "各提交者偏好一致。"
        lines += [
            f"### {sample['name']}",
            "",
            (
                f"场景：{sample['scene'] or '场景未填写'} ｜ "
                f"有效提交人数（分母）：{sample['分母']} ｜ {hint}"
            ),
            "",
            "| 选项 | 票数 | 占比 | 选项 ID |",
            "| --- | --- | --- | --- |",
        ]
        for vote in sample["票数"]:
            option_id = "—" if vote["ID"] == "tie" else f"`{vote['ID']}`"
            lines.append(
                f"| {md_cell(vote['名称'])} | {vote['票数']} | "
                f"{share_text(vote['票数'], sample['分母'])} | {option_id} |"
            )
        lines.append("")
    lines += [
        "## 处理口径",
        "",
        "派生试听资产按「先整数采样对齐，再固定增益」生成；原始 WAV 未修改。",
        "活动段 RMS 匹配不是 LUFS/ITU BS.1770 响度校准，也不是听感等响。",
        "",
    ]
    processed_any = False
    for sample in value["样本"]:
        summary = sample.get("处理摘要") or {}
        for track in sample["tracks"]:
            proc = track.get("处理口径")
            if not isinstance(proc, dict):
                continue
            processed_any = True
            lines.append(
                f"- {md_cell(sample['name'])} / {md_cell(track['name'])}：{proc['模式']}；"
                f"lag {proc['lag']:+d} samples（{proc['lag符号定义']}）；"
                f"增益 {proc['增益db']:+.2f} dB；派生 SHA256 `{proc['派生资产SHA256']}`。"
            )
        for rejection in summary.get("拒绝建议", []):
            lines.append(
                f"- {md_cell(sample['name'])} / {md_cell(rejection['候选'])}："
                f"{rejection['判据']}建议被拒绝（{rejection['拒绝码']}）——{rejection['原因']}。"
            )
    if not processed_any:
        lines.append("全部候选为原始音频，无派生处理。")
    lines += ["", "## 问题标签汇总", "", "仅统计根评论，回复楼层不计入。", ""]
    if value["标签汇总"]:
        lines += ["| 标签 | 根评论数 | 涉及片段 |", "| --- | --- | --- |"]
        for tag in value["标签汇总"]:
            location = "；".join(
                f"{md_cell(part['名称'])}（`{part['ID']}`，{part['根评论数']} 条）"
                for part in tag["片段"]
            )
            lines.append(f"| {md_cell(tag['标签'])} | {tag['根评论数']} | {location} |")
    else:
        lines.append("暂无根评论标签。")
    lines += ["", "## 口径与边界", "", f"- {value['播放口径']}", f"- {value['解释边界']}", ""]
    return "\n".join(lines)


class Login(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=200)
    setup_key: str = ""


class UserInput(Login):
    role: str = "reviewer"


class TaskInput(BaseModel):
    title: str = Field(min_length=1, max_length=150)
    kind: str = "算法版本"
    mode: str = "development"


class SampleInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scene: str = Field(default="", max_length=200)
    provenance: str = "PRIVATE local verification"


class MembersInput(BaseModel):
    users: list[str] = Field(default_factory=list)


class RoleInput(BaseModel):
    role: str


class ResetPasswordInput(BaseModel):
    password: str = Field(min_length=10, max_length=200)
    confirm_name: str


class DeleteTaskInput(BaseModel):
    title: str


class TrackInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(default="未知", max_length=200)


class PublishInput(BaseModel):
    users: list[str] = Field(default_factory=list)
    alignment_confirmed: bool = False


class CommentInput(BaseModel):
    track_id: str | None = None
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    body: str = Field(min_length=1, max_length=3000)
    tag: str = Field(default="听感", max_length=40)
    parent: str | None = None


class RatingInput(BaseModel):
    choice: str
    reason: str = Field(default="", max_length=2000)


class ApplyInput(BaseModel):
    display_name: str = Field(min_length=1, max_length=60)
    employee_id: str = Field(default="", max_length=60)
    email: str = Field(default="", max_length=120)
    invite_code: str = Field(min_length=6, max_length=200)
    claim_secret: str = Field(min_length=32, max_length=200)


class ClaimInput(BaseModel):
    application_id: str = Field(min_length=8, max_length=64)
    claim_secret: str = Field(min_length=32, max_length=200)


class InviteInput(BaseModel):
    purpose: str = Field(default="", max_length=120)
    kind: str = "team"
    task_id: str = ""
    expires_days: int = Field(default=7, ge=1, le=365)
    max_uses: int = Field(default=5, ge=1, le=500)


class InviteToggle(BaseModel):
    active: bool


class ApproveInput(BaseModel):
    role: str = "reviewer"
    name: str = Field(default="", max_length=60)
    task_ids: list[str] = Field(default_factory=list)


def create_app(
    data_dir: Path,
    static_dir: Path | None = None,
    secure_cookie=False,
    public_origin: str | None = None,
):
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    assets = data_dir / "assets"
    assets.mkdir(exist_ok=True)
    database = data_dir / "workbench.sqlite3"
    init(database)
    setup_file = data_dir / "setup-key.txt"
    with connect(database) as db:
        if not db.execute("SELECT 1 FROM users").fetchone() and not setup_file.exists():
            setup_file.write_text(secrets.token_urlsafe(24), encoding="utf-8")
            setup_file.chmod(0o600)
    app = FastAPI(title="听鉴音频评测工作台", docs_url=None, redoc_url=None)
    app.state.database = database
    app.state.setup_file = setup_file
    app.state.instance_id = secrets.token_hex(6)
    mutation_lock = threading.RLock()
    attempts: dict[tuple, list[float]] = {}

    def rate_limit(bucket, address, limit, window, message):
        """按 socket 对端限流，防双击与撞库；不区分转发头，无法被伪造请求头绕过。"""
        with mutation_lock:
            stamp = time.time()
            recent = [t for t in attempts.get((bucket, address), []) if t > stamp - window]
            if len(recent) >= limit:
                attempts[(bucket, address)] = recent
                raise HTTPException(429, message)
            recent.append(stamp)
            attempts[(bucket, address)] = recent
            if len(attempts) > 4096:  # 丢弃完全过期的桶，防止匿名限流表无限增长。
                for key in [k for k, v in attempts.items() if all(t <= stamp - window for t in v)]:
                    del attempts[key]

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        # Cookie 身份请求必须来自同源；无 Origin 的非浏览器本地 API 仍需身份。
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != (
                public_origin or str(request.base_url)
            ).rstrip("/"):
                return JSONResponse({"detail": "不允许跨站写入"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "不允许跨站写入"}, status_code=403)
            # 严格内容类型：所有接口只接受 JSON 或 multipart；
            # 经典表单/text/plain 跨站写入在到达业务前即被拒绝。
            content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type in ("text/plain", "application/x-www-form-urlencoded"):
                return JSONResponse({"detail": "不支持的内容类型"}, status_code=415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'"
        )
        if (
            request.url.path.startswith("/api")
            or request.url.path == "/"
            or request.url.path.endswith(".html")
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    def user(request):
        """身份解析：优先会话 Cookie，其次设备令牌 Cookie。
        IP 从不参与身份判断：相同 IP、不同浏览器没有 Cookie 就不能登录。"""
        token = request.cookies.get("session", "")
        if token:
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            with connect(database) as db:
                row = db.execute(
                    "SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token=? AND s.expires>?",
                    (token_hash, time.time()),
                ).fetchone()
            if row:
                u = dict(row)
                u["_auth_seed"] = "session:" + token_hash
                return u
        device_token = request.cookies.get(DEVICE_COOKIE, "")
        if device_token:
            digest = hashlib.sha256(device_token.encode()).hexdigest()
            with connect(database) as db:
                row = db.execute(
                    "SELECT u.*, d.id AS device_row, d.last_used AS device_last_used, d.last_ip AS device_last_ip "
                    "FROM devices d JOIN users u ON u.id=d.user_id "
                    "WHERE d.token_hash=? AND d.revoked=0 AND d.expires>?",
                    (digest, time.time()),
                ).fetchone()
            if row:
                u = dict(row)
                u["_auth_seed"] = "device:" + digest
                # 审计：最近使用时间节流更新（60 秒一次）；IP 变化立即记录，
                # 只作为风险提示，从不影响身份判断或会话有效性。
                address = client_ip(request)
                if (
                    u["device_last_used"] is None
                    or time.time() - u["device_last_used"] > 60
                    or u["device_last_ip"] != address
                ):
                    with connect(database) as db:
                        db.execute(
                            "UPDATE devices SET last_used=?, last_ip=? WHERE id=? AND revoked=0",
                            (time.time(), address, u["device_row"]),
                        )
                return u
        raise HTTPException(401, "请登录后继续")

    def admin(request):
        u = user(request)
        if u["role"] != "admin":
            raise HTTPException(403, "需要管理员权限")
        return u

    def require_csrf(request, u):
        """敏感管理操作的二次 CSRF 校验：令牌由会话/设备 Cookie 哈希派生，
        只通过 /api/me 交给同源页面；跨站攻击者拿不到 Cookie，也读不到响应。"""
        expected = hashlib.sha256(("csrf:" + u["_auth_seed"]).encode()).hexdigest()
        presented = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(presented, expected):
            raise HTTPException(403, "安全校验失败，请刷新页面后重试")

    def organizer(request):
        u = user(request)
        if u["role"] not in ("admin", "organizer"):
            raise HTTPException(403, "需要组织者权限，请联系管理员开通")
        return u

    def task_access(db, task_id, u, manage=False, include_deleted=False):
        t = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t:
            raise HTTPException(404, "任务不存在")
        owner = u["role"] == "admin" or (
            t["owner"] == u["id"] and u["role"] == "organizer"
        )
        deleted = db.execute(
            "SELECT 1 FROM deleted_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if deleted and (not include_deleted or not owner):
            raise HTTPException(404, "任务已移入回收站")
        if manage and not owner:
            raise HTTPException(403, "仅组织者可以修改任务")
        if not owner:
            member = db.execute(
                "SELECT 1 FROM members WHERE task_id=? AND user_id=?",
                (task_id, u["id"]),
            ).fetchone()
            if not member or t["status"] == "draft":
                raise HTTPException(403, "没有此任务的访问权限")
        return {**dict(t), "can_manage": owner}

    def sample_access(db, sample_id, u, manage=False):
        s = db.execute("SELECT * FROM samples WHERE id=?", (sample_id,)).fetchone()
        if not s:
            raise HTTPException(404, "样本不存在")
        return dict(s), task_access(db, s["task_id"], u, manage)

    def aliases(db, sample_id, u):
        tracks = db.execute(
            "SELECT * FROM tracks WHERE sample_id=? ORDER BY id", (sample_id,)
        ).fetchall()
        existing = db.execute(
            "SELECT track_id,position FROM aliases WHERE sample_id=? AND user_id=?",
            (sample_id, u["id"]),
        ).fetchall()
        if len(existing) != len(tracks):
            db.execute(
                "DELETE FROM aliases WHERE sample_id=? AND user_id=?",
                (sample_id, u["id"]),
            )
            order = list(tracks)
            secrets.SystemRandom().shuffle(order)
            for i, tr in enumerate(order):
                db.execute(
                    "INSERT INTO aliases VALUES(?,?,?,?)",
                    (u["id"], sample_id, tr["id"], i),
                )
            return {tr["id"]: i for i, tr in enumerate(order)}
        return {r["track_id"]: r["position"] for r in existing}

    @app.get("/api/status")
    def status():
        with connect(database) as db:
            return {
                "needs_setup": not bool(db.execute("SELECT 1 FROM users").fetchone()),
                "version": VERSION,
                "instance_id": app.state.instance_id,
            }

    @app.get("/api/server-info")
    def server_info(request: Request):
        u = user(request)
        result = {
            "version": VERSION,
            "instance_id": app.state.instance_id,
            "data_id": hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()[
                :12
            ],
        }
        if u["role"] == "admin":
            result["data_directory"] = str(data_dir.resolve())
        return result

    @app.post("/api/setup")
    def setup(body: Login):
        with mutation_lock, connect(database) as db:
            if db.execute("SELECT 1 FROM users").fetchone():
                raise HTTPException(409, "已完成初始化")
            if not setup_file.exists() or not secrets.compare_digest(
                body.setup_key.encode(), setup_file.read_text().strip().encode()
            ):
                raise HTTPException(403, "初始化密钥错误，请查看主机启动窗口")
            if len(body.password) < 10:
                raise HTTPException(422, "密码至少 10 个字符")
            db.execute(
                "INSERT INTO users VALUES(?,?,?,?)",
                (uid(), body.name, password_hash(body.password), "admin"),
            )
        setup_file.unlink(missing_ok=True)
        return {"ok": True}

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response):
        address = request.client.host if request.client else "local"
        with mutation_lock:
            recent = [x for x in attempts.get(("login", address), []) if x > time.time() - 300]
            if len(recent) >= 20:
                raise HTTPException(429, "登录尝试过多，请稍后再试")
            attempts[("login", address)] = recent + [time.time()]
        with mutation_lock, connect(database) as db:
            u = db.execute("SELECT * FROM users WHERE name=?", (body.name,)).fetchone()
            if not u or not verify(body.password, u["password"]):
                raise HTTPException(401, "账号或密码错误")
            # 成功登录重置该地址的失败尝试计数：限流只针对暴力猜测，
            # 不惩罚输错几次后正常进入的同事。
            attempts.pop(("login", address), None)
            # 会话轮换：登录成功时废弃请求中携带的旧会话，再签发全新令牌，
            # 防止会话固定攻击；同一用户的其他设备会话不受影响。
            old = request.cookies.get("session", "")
            if old:
                db.execute(
                    "DELETE FROM sessions WHERE token=?",
                    (hashlib.sha256(old.encode()).hexdigest(),),
                )
            token = secrets.token_urlsafe(32)
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    u["id"],
                    time.time() + 43200,
                ),
            )
        response.set_cookie(
            "session",
            token,
            httponly=True,
            secure=secure_cookie,
            samesite="strict",
            max_age=43200,
        )
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        with connect(database) as db:
            db.execute(
                "DELETE FROM sessions WHERE token=?",
                (
                    hashlib.sha256(
                        request.cookies.get("session", "").encode()
                    ).hexdigest(),
                ),
            )
            # 退出即撤销本浏览器设备令牌，刷新后不会免密重新进入。
            presented = request.cookies.get(DEVICE_COOKIE, "")
            if presented:
                db.execute(
                    "UPDATE devices SET revoked=1 WHERE token_hash=?",
                    (hashlib.sha256(presented.encode()).hexdigest(),),
                )
        response.delete_cookie("session")
        response.delete_cookie(DEVICE_COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        u = user(request)
        result = {k: u[k] for k in ("id", "name", "role")}
        # CSRF 令牌：由当前凭证哈希派生，交给同源页面在敏感操作请求头中回传。
        result["csrf_token"] = hashlib.sha256(("csrf:" + u["_auth_seed"]).encode()).hexdigest()
        if u["role"] == "admin":
            with connect(database) as db:
                result["pending_applications"] = db.execute(
                    "SELECT count(*) FROM applications WHERE status='pending'"
                ).fetchone()[0]
        return result

    # ---------- 自助申请与免密设备登录 ----------
    # 身份边界：IP 只用于审计与限流，从不参与登录判断；
    # 申请编号、领取秘密、邀请明文与设备令牌分离，仅哈希入库。

    @app.post("/api/apply")
    def apply(body: ApplyInput, request: Request):
        address = client_ip(request)
        rate_limit("apply", address, 10, 300, "提交过于频繁，请稍后再试")
        display = body.display_name.strip()
        if not display:
            raise HTTPException(422, "请填写显示名称")
        claim_hash = hashlib.sha256(body.claim_secret.encode()).hexdigest()
        with mutation_lock, connect(database) as db:
            if not db.execute("SELECT 1 FROM users").fetchone():
                raise HTTPException(409, "服务尚未完成初始化，请稍后再试")
            # 幂等重试：同一领取秘密始终对应同一条申请，双击或重放不会产生多条记录。
            existing = db.execute(
                "SELECT id, status FROM applications WHERE claim_hash=?", (claim_hash,)
            ).fetchone()
            if existing:
                return {"id": existing["id"], "status": existing["status"]}
            key, _, secret = body.invite_code.strip().partition(".")
            invite = (
                db.execute("SELECT * FROM invites WHERE token_key=?", (key,)).fetchone()
                if key and secret
                else None
            )
            valid = bool(invite)
            if valid:
                try:
                    valid = verify(secret, invite["token_hash"])
                except ValueError:
                    valid = False
            # 只校验资格，不消耗次数（消耗发生在批准时）；无效原因不区分细节，
            # 不暴露邀请码是否存在、是否停用、是否过期或是否用尽。
            if valid:
                valid = (
                    invite["active"] == 1
                    and invite["expires"] > now()
                    and invite["used_count"] < invite["max_uses"]
                )
            if valid and invite["kind"] == "task":
                valid = bool(
                    db.execute(
                        "SELECT 1 FROM tasks WHERE id=? AND id NOT IN (SELECT task_id FROM deleted_tasks)",
                        (invite["task_id"],),
                    ).fetchone()
                )
            if not valid:
                raise HTTPException(422, "邀请凭证无效或已失效，请向管理员核对")
            application_id = uid()
            db.execute(
                "INSERT INTO applications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'',NULL,NULL)",
                (
                    application_id,
                    claim_hash,
                    display,
                    body.employee_id.strip(),
                    body.email.strip(),
                    invite["id"],
                    "pending",
                    address,
                    simplify_device(request.headers.get("user-agent")),
                    now(),
                    None,
                    None,
                ),
            )
        return {"id": application_id, "status": "pending"}

    @app.post("/api/apply/status")
    def apply_status(body: ClaimInput, request: Request):
        address = client_ip(request)
        rate_limit("status", address, 120, 300, "查询过于频繁，请稍后再试")
        with mutation_lock, connect(database) as db:
            row = db.execute(
                "SELECT a.id, a.claim_hash, a.status, i.active AS invite_active, i.expires AS invite_expires, "
                "i.used_count AS invite_used, i.max_uses AS invite_max FROM applications a "
                "JOIN invites i ON i.id=a.invite_id WHERE a.id=?",
                (body.application_id,),
            ).fetchone()
            if not row or not secrets.compare_digest(
                hashlib.sha256(body.claim_secret.encode()).hexdigest(), row["claim_hash"]
            ):
                raise HTTPException(403, "申请编号或领取信息不正确")
            status = row["status"]
            # 申请自身没有独立有效期；其等待资格随邀请失效而结束（pending -> expired）。
            if status == "pending" and (
                row["invite_active"] != 1
                or row["invite_expires"] <= now()
                or row["invite_used"] >= row["invite_max"]
            ):
                db.execute(
                    "UPDATE applications SET status='expired', decided=? WHERE id=? AND status='pending'",
                    (now(), row["id"]),
                )
                status = "expired"
        return {"id": body.application_id, "status": status}

    @app.post("/api/apply/claim")
    def apply_claim(body: ClaimInput, request: Request, response: Response):
        address = client_ip(request)
        rate_limit("claim", address, 20, 300, "领取尝试过于频繁，请稍后再试")
        device_info = simplify_device(request.headers.get("user-agent"))
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with mutation_lock, connect(database) as db:
            row = db.execute(
                "SELECT * FROM applications WHERE id=?", (body.application_id,)
            ).fetchone()
            if not row or not secrets.compare_digest(
                hashlib.sha256(body.claim_secret.encode()).hexdigest(), row["claim_hash"]
            ):
                raise HTTPException(403, "申请编号或领取信息不正确")
            if row["status"] == "pending":
                raise HTTPException(409, "申请还在等待管理员批准")
            if row["status"] != "approved" or not row["user_id"]:
                raise HTTPException(409, "申请未处于可领取状态")
            if not row["device_id"]:
                # 首次领取：签发随机高熵设备令牌；仅哈希入库，明文只通过 Cookie 下发。
                device_id = uid()
                db.execute(
                    "INSERT INTO devices VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        device_id,
                        row["user_id"],
                        digest,
                        now(),
                        time.time(),
                        time.time(),
                        time.time() + DEVICE_TTL,
                        0,
                        address,
                        address,
                        device_info,
                    ),
                )
                db.execute(
                    "UPDATE applications SET device_id=? WHERE id=?",
                    (device_id, row["id"]),
                )
            else:
                # 重试领取：同一申请同一设备行，仅轮换令牌与审计字段；
                # 领取窗口起点（首次 claimed_at）与绝对到期（expires）固定不变，
                # 连续重试不能延长窗口或寿命；已撤销或已过期稳定 409。
                device = db.execute(
                    "SELECT claimed_at, expires, revoked FROM devices WHERE id=?",
                    (row["device_id"],),
                ).fetchone()
                if (
                    not device
                    or device["revoked"]
                    or device["expires"] <= time.time()
                    or time.time() - device["claimed_at"] > CLAIM_GRACE
                ):
                    raise HTTPException(
                        409,
                        "登录状态已在原浏览器生效；如需在新浏览器登录，请联系管理员重置密码或撤销设备",
                    )
                rotated = db.execute(
                    "UPDATE devices SET token_hash=?, last_used=NULL, last_ip=?, device=? WHERE id=? AND revoked=0",
                    (digest, address, device_info, row["device_id"]),
                ).rowcount
                if rotated != 1:
                    raise HTTPException(409, "设备状态已变化，请刷新后重试")
        response.set_cookie(
            DEVICE_COOKIE,
            token,
            httponly=True,
            secure=secure_cookie,
            samesite="lax",
            max_age=DEVICE_TTL,
            path="/",
        )
        return {"ok": True, "status": "approved"}

    @app.get("/api/invites")
    def invite_list(request: Request):
        admin(request)
        with connect(database) as db:
            rows = db.execute(
                "SELECT i.id, i.purpose, i.kind, i.task_id, t.title AS task_title, i.expires, "
                "i.max_uses, i.used_count, i.active, i.created FROM invites i "
                "LEFT JOIN tasks t ON t.id=i.task_id ORDER BY i.created DESC, i.id"
            ).fetchall()
        # 列表不含 token_key、盐和哈希，更不可能恢复明文。
        return [dict(row) for row in rows]

    @app.post("/api/invites")
    def create_invite(body: InviteInput, request: Request):
        u = admin(request)
        require_csrf(request, u)
        if body.kind not in ("team", "task"):
            raise HTTPException(422, "请选择邀请类型")
        task_id = ""
        if body.kind == "task":
            with connect(database) as db:
                if not db.execute(
                    "SELECT 1 FROM tasks WHERE id=? AND id NOT IN (SELECT task_id FROM deleted_tasks)",
                    (body.task_id,),
                ).fetchone():
                    raise HTTPException(404, "任务不存在")
            task_id = body.task_id
        # 高熵凭证 = 可识别片段 + 机密片段；库中只存片段与带独立盐的 scrypt 哈希，
        # 明文只在本次响应出现一次，之后任何接口都无法取回。
        code = secrets.token_urlsafe(6) + "." + secrets.token_urlsafe(18)
        key, _, secret = code.partition(".")
        invite_id = uid()
        expires = (
            datetime.now(UTC).replace(microsecond=0) + timedelta(days=body.expires_days)
        ).isoformat()
        with mutation_lock, connect(database) as db:
            db.execute(
                "INSERT INTO invites VALUES(?,?,?,?,?,?,?,?,0,1,?,?)",
                (
                    invite_id,
                    body.purpose.strip(),
                    body.kind,
                    task_id or None,
                    key,
                    password_hash(secret),
                    expires,
                    body.max_uses,
                    now(),
                    u["id"],
                ),
            )
        return {"id": invite_id, "code": code, "expires": expires}

    @app.patch("/api/invites/{invite_id}")
    def update_invite(invite_id: str, body: InviteToggle, request: Request):
        u = admin(request)
        require_csrf(request, u)
        with mutation_lock, connect(database) as db:
            if not db.execute("SELECT 1 FROM invites WHERE id=?", (invite_id,)).fetchone():
                raise HTTPException(404, "邀请不存在")
            db.execute("UPDATE invites SET active=? WHERE id=?", (1 if body.active else 0, invite_id))
        return {"ok": True}

    @app.get("/api/applications")
    def application_list(request: Request, status: str = ""):
        admin(request)
        sql = (
            "SELECT a.id, a.display_name, a.employee_id, a.email, a.created, a.ip, a.device, a.status, "
            "a.decided, a.user_id, a.device_id, du.name AS decided_by_name, i.purpose, i.kind AS invite_kind, "
            "i.task_id AS invite_task_id, t.title AS invite_task_title FROM applications a "
            "JOIN invites i ON i.id=a.invite_id LEFT JOIN tasks t ON t.id=i.task_id "
            "LEFT JOIN users du ON du.id=a.decided_by"
        )
        params = ()
        if status in APPLICATION_STATES:
            sql += " WHERE a.status=?"
            params = (status,)
        sql += " ORDER BY a.created DESC, a.id"
        with connect(database) as db:
            rows = db.execute(sql, params).fetchall()
        return [
            {
                "id": row["id"],
                "display_name": row["display_name"],
                "employee_id": row["employee_id"],
                "email": row["email"],
                "created": row["created"],
                "ip": row["ip"],
                "device": row["device"],
                "status": row["status"],
                "decided": row["decided"],
                "decided_by": row["decided_by_name"],
                "user_id": row["user_id"],
                "claimed": bool(row["device_id"]),
                "invite": {
                    "purpose": row["purpose"],
                    "kind": row["invite_kind"],
                    "task_id": row["invite_task_id"],
                    "task_title": row["invite_task_title"],
                },
            }
            for row in rows
        ]

    @app.post("/api/applications/{application_id}/approve")
    def approve(application_id: str, body: ApproveInput, request: Request):
        u = admin(request)
        require_csrf(request, u)
        if body.role not in ("reviewer", "organizer"):
            raise HTTPException(422, "请选择评测者或组织者角色")
        tasks = sorted({t for t in body.task_ids if t})
        with mutation_lock:
            with connect(database) as db:
                row = db.execute(
                    "SELECT a.*, i.kind AS invite_kind, i.task_id AS invite_task_id, i.active AS invite_active, "
                    "i.expires AS invite_expires, i.used_count AS invite_used, i.max_uses AS invite_max "
                    "FROM applications a JOIN invites i ON i.id=a.invite_id WHERE a.id=?",
                    (application_id,),
                ).fetchone()
                if not row:
                    raise HTTPException(404, "申请不存在")
                if row["status"] != "pending":
                    # 终态不可反转：同决策重试幂等返回，其他情形显式冲突。
                    if row["status"] == "approved":
                        resolved = (body.name or "").strip() or row["display_name"].strip()
                        snapshot = json.dumps(
                            {"name": resolved, "role": body.role, "tasks": tasks},
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if row["decision"] == snapshot:
                            return {"ok": True, "already": True, "user_id": row["user_id"]}
                    if (
                        row["status"] == "rejected"
                        and not tasks
                        and body.role == "reviewer"
                        and not (body.name or "").strip()
                    ):
                        return {"ok": True, "already": True}
                    raise HTTPException(409, "该申请已" + STATE_NAMES[row["status"]])
                resolved_name = (body.name or "").strip() or row["display_name"].strip()
                if not resolved_name or len(resolved_name) > 60:
                    raise HTTPException(422, "请填写有效的账号名称")
                snapshot = json.dumps(
                    {"name": resolved_name, "role": body.role, "tasks": tasks},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if row["invite_kind"] == "task" and tasks != [row["invite_task_id"]]:
                    raise HTTPException(422, "任务邀请只能批准到受邀任务")
                # 审批事务内重查任务最新状态（邀请创建后任务可能已被关闭/移入回收站），
                # 统一复用既有成员语义：draft/active 可新增，closed 冻结名单，回收站不可见。
                for task_id in tasks:
                    t = db.execute(
                        "SELECT status FROM tasks WHERE id=?", (task_id,)
                    ).fetchone()
                    deleted = db.execute(
                        "SELECT 1 FROM deleted_tasks WHERE task_id=?", (task_id,)
                    ).fetchone()
                    if not t or deleted:
                        raise HTTPException(404, "受邀任务不存在或已移入回收站")
                    if t["status"] == "closed":
                        raise HTTPException(409, "任务已关闭，成员名单冻结，不能新增受邀评测者")
                if db.execute(
                    "SELECT 1 FROM users WHERE name=?", (resolved_name,)
                ).fetchone():
                    raise HTTPException(409, "账号名称已存在，请修改后重试")
                expired = (
                    row["invite_active"] != 1
                    or row["invite_expires"] <= now()
                    or row["invite_used"] >= row["invite_max"]
                )
            if expired:
                with connect(database) as db:
                    db.execute(
                        "UPDATE applications SET status='expired', decided=? WHERE id=? AND status='pending'",
                        (now(), application_id),
                    )
                raise HTTPException(409, "邀请凭证已失效或次数用尽，申请已自动过期")
            # 批准事务：消耗邀请次数（SQL 级上限约束）→ 建账号 → 入成员与受邀名单 →
            # 落申请终态。任一步失败整体回滚，不会留下半批准用户、超用邀请或孤立令牌。
            user_id = uid()
            with connect(database) as db:
                consumed = db.execute(
                    "UPDATE invites SET used_count=used_count+1 WHERE id=? AND active=1 AND used_count<max_uses AND expires>?",
                    (row["invite_id"], now()),
                ).rowcount
                if consumed != 1:
                    raise HTTPException(409, "邀请凭证状态已变化，请刷新后重试")
                # 被批准账号初始不设置可用口令：随机值哈希后即刻丢弃，
                # 只能通过设备 Cookie 免密登录；必要时管理员可用既有重置口令流程。
                db.execute(
                    "INSERT INTO users VALUES(?,?,?,?)",
                    (user_id, resolved_name, password_hash(secrets.token_urlsafe(32)), body.role),
                )
                for task_id in tasks:
                    db.execute("INSERT OR IGNORE INTO members VALUES(?,?)", (task_id, user_id))
                    db.execute(
                        "INSERT OR IGNORE INTO review_assignments VALUES(?,?)", (task_id, user_id)
                    )
                db.execute(
                    "UPDATE applications SET status='approved', user_id=?, decision=?, decided=?, decided_by=? WHERE id=? AND status='pending'",
                    (user_id, snapshot, now(), u["id"], application_id),
                )
        return {"ok": True, "user_id": user_id, "already": False}

    @app.post("/api/applications/{application_id}/reject")
    def reject(application_id: str, request: Request):
        u = admin(request)
        require_csrf(request, u)
        with mutation_lock, connect(database) as db:
            row = db.execute(
                "SELECT status FROM applications WHERE id=?", (application_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "申请不存在")
            if row["status"] == "rejected":
                return {"ok": True, "already": True}
            if row["status"] != "pending":
                raise HTTPException(409, "该申请已" + STATE_NAMES[row["status"]])
            # 拒绝不消耗邀请使用次数。
            db.execute(
                "UPDATE applications SET status='rejected', decided=?, decided_by=? WHERE id=?",
                (now(), u["id"], application_id),
            )
        return {"ok": True, "already": False}

    @app.get("/api/users/{user_id}/devices")
    def device_list(user_id: str, request: Request):
        admin(request)
        with connect(database) as db:
            if not db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise HTTPException(404, "账号不存在")
            rows = db.execute(
                "SELECT id, created, last_used, expires, revoked, first_ip, last_ip, device "
                "FROM devices WHERE user_id=? ORDER BY created DESC, id",
                (user_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "created": row["created"],
                "last_used": (
                    datetime.fromtimestamp(row["last_used"], UTC).isoformat()
                    if row["last_used"]
                    else None
                ),
                "expires": datetime.fromtimestamp(row["expires"], UTC).isoformat(),
                "revoked": bool(row["revoked"]),
                "first_ip": row["first_ip"],
                "last_ip": row["last_ip"],
                "device": row["device"] or "未知客户端",
            }
            for row in rows
        ]

    @app.delete("/api/devices/{device_id}")
    def revoke_device(device_id: str, request: Request):
        u = admin(request)
        require_csrf(request, u)
        with mutation_lock, connect(database) as db:
            if not db.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone():
                raise HTTPException(404, "设备不存在")
            # 撤销幂等：重复撤销同样返回成功；下一次请求立即失效。
            db.execute("UPDATE devices SET revoked=1 WHERE id=?", (device_id,))
        return {"ok": True}

    @app.delete("/api/users/{user_id}/devices")
    def revoke_all_devices(user_id: str, request: Request):
        u = admin(request)
        require_csrf(request, u)
        with mutation_lock, connect(database) as db:
            if not db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise HTTPException(404, "账号不存在")
            db.execute("UPDATE devices SET revoked=1 WHERE user_id=? AND revoked=0", (user_id,))
        return {"ok": True}

    @app.get("/api/users")
    def users(request: Request):
        organizer(request)
        with connect(database) as db:
            return [
                dict(x)
                for x in db.execute("SELECT id,name,role FROM users ORDER BY name")
            ]

    @app.post("/api/users")
    def create_user(body: UserInput, request: Request):
        admin(request)
        if (
            body.role not in ("reviewer", "organizer", "admin")
            or len(body.password) < 10
        ):
            raise HTTPException(422, "请选择有效角色，密码至少 10 个字符")
        try:
            with connect(database) as db:
                db.execute(
                    "INSERT INTO users VALUES(?,?,?,?)",
                    (uid(), body.name, password_hash(body.password), body.role),
                )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "账号名称已存在")
        return {"ok": True}

    @app.post("/api/users/{user_id}/password")
    def reset_password(user_id: str, body: ResetPasswordInput, request: Request):
        admin(request)
        with mutation_lock, connect(database) as db:
            target = db.execute(
                "SELECT name,role FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if not target:
                raise HTTPException(404, "账号不存在")
            if target["role"] == "admin":
                raise HTTPException(409, "此入口仅用于重置同事账号，不修改管理员密码")
            if body.confirm_name != target["name"]:
                raise HTTPException(422, "请输入完整账号名称确认重置")
            db.execute(
                "UPDATE users SET password=? WHERE id=?",
                (password_hash(body.password), user_id),
            )
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        return {"ok": True}

    @app.patch("/api/users/{user_id}/role")
    def update_role(user_id: str, body: RoleInput, request: Request):
        admin(request)
        if body.role not in ("reviewer", "organizer"):
            raise HTTPException(422, "请选择评测者或组织者")
        with mutation_lock, connect(database) as db:
            target = db.execute(
                "SELECT role FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if not target:
                raise HTTPException(404, "账号不存在")
            if target["role"] == "admin":
                raise HTTPException(409, "此入口不能修改管理员角色")
            db.execute("UPDATE users SET role=? WHERE id=?", (body.role, user_id))
        return {"ok": True}

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str, body: DeleteTaskInput, request: Request):
        u = user(request)
        with mutation_lock, connect(database) as db:
            t = task_access(db, task_id, u, True, include_deleted=True)
            if body.title != t["title"]:
                raise HTTPException(422, "请输入完整任务名称确认删除")
            db.execute(
                "INSERT OR IGNORE INTO deleted_tasks VALUES(?,?,?)",
                (task_id, u["id"], now()),
            )
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/restore")
    def restore_task(task_id: str, request: Request):
        u = user(request)
        with mutation_lock, connect(database) as db:
            task_access(db, task_id, u, True, include_deleted=True)
            db.execute("DELETE FROM deleted_tasks WHERE task_id=?", (task_id,))
        return {"ok": True}

    def _all_asset_files(db):
        files = {r[0] for r in db.execute("SELECT DISTINCT path FROM tracks")}
        files |= {
            json.loads(r[0])["派生文件"]
            for r in db.execute("SELECT data FROM track_processing")
        }
        return files

    def _task_asset_files(db, task_id):
        files = {
            r[0]
            for r in db.execute(
                "SELECT DISTINCT t.path FROM tracks t JOIN samples s ON s.id=t.sample_id "
                "WHERE s.task_id=?",
                (task_id,),
            )
        }
        files |= {
            json.loads(r[0])["派生文件"]
            for r in db.execute(
                "SELECT tp.data FROM track_processing tp "
                "JOIN tracks t ON t.id=tp.track_id "
                "JOIN samples s ON s.id=t.sample_id WHERE s.task_id=?",
                (task_id,),
            )
        }
        return files

    def _unlink_asset(name):
        # 仅清理 assets 下由服务端 SHA 派生的文件名，不接受外部任意路径。
        if not re.fullmatch(r"[0-9a-f]{64}\.wav", name):
            return
        (assets / name).unlink(missing_ok=True)

    @app.delete("/api/samples/{sample_id}")
    def delete_sample(sample_id: str, request: Request):
        u = user(request)
        with mutation_lock:
            # 手工事务＋显式提交点：提交成功后按引用差集回收无引用资产。
            db = sqlite3.connect(database, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            try:
                _, _ = _sample_managers_only(db, sample_id, u)
                annotated = db.execute(
                    "SELECT 1 FROM comments WHERE sample_id=? LIMIT 1", (sample_id,)
                ).fetchone() or db.execute(
                    "SELECT 1 FROM ratings WHERE sample_id=? LIMIT 1", (sample_id,)
                ).fetchone()
                if annotated:
                    raise HTTPException(
                        409,
                        "此片段已有评论或评分，不能删除；标注是可追溯证据的一部分",
                    )
                refs_before = _all_asset_files(db)
                track_ids = [
                    r["id"]
                    for r in db.execute(
                        "SELECT id FROM tracks WHERE sample_id=?", (sample_id,)
                    ).fetchall()
                ]
                for tid in track_ids:
                    db.execute("DELETE FROM track_processing WHERE track_id=?", (tid,))
                    db.execute("DELETE FROM track_analysis WHERE track_id=?", (tid,))
                db.execute("DELETE FROM aliases WHERE sample_id=?", (sample_id,))
                db.execute("DELETE FROM tracks WHERE sample_id=?", (sample_id,))
                db.execute("DELETE FROM samples WHERE id=?", (sample_id,))
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
            with connect(database) as db:
                refs_after = _all_asset_files(db)
            for name in sorted(refs_before - refs_after):
                _unlink_asset(name)
            return {"ok": True, "已删候选": len(track_ids)}

    class PurgeInput(BaseModel):
        确认令牌: str

    @app.post("/api/tasks/{task_id}/purge/prepare")
    def prepare_purge_task(task_id: str, request: Request):
        u = user(request)
        admin(request)
        with connect(database) as db:
            task_access(db, task_id, u, True, include_deleted=True)
            if not db.execute(
                "SELECT 1 FROM deleted_tasks WHERE task_id=?", (task_id,)
            ).fetchone():
                raise HTTPException(409, "仅回收站中的任务可以永久清除")
            sample_count = db.execute(
                "SELECT count(*) FROM samples WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            track_count = db.execute(
                "SELECT count(*) FROM tracks t JOIN samples s ON s.id=t.sample_id "
                "WHERE s.task_id=?",
                (task_id,),
            ).fetchone()[0]
            task_files = _task_asset_files(db, task_id)
            exclusive = sorted(task_files - (_all_asset_files(db) - task_files))
            total_bytes = sum(
                (assets / name).stat().st_size
                for name in exclusive
                if (assets / name).exists()
            )
        token = secrets.token_urlsafe(24)
        with mutation_lock, connect(database) as db:
            db.execute("DELETE FROM purge_tokens WHERE task_id=?", (task_id,))
            db.execute(
                "INSERT INTO purge_tokens VALUES(?,?,?)", (token, task_id, now())
            )
        return {
            "确认令牌": token,
            "片段数": sample_count,
            "候选数": track_count,
            "独占资产数": len(exclusive),
            "预计释放字节": total_bytes,
        }

    @app.post("/api/tasks/{task_id}/purge")
    def purge_task(task_id: str, body: PurgeInput, request: Request):
        u = user(request)
        admin(request)
        with mutation_lock:
            # 手工事务：先提交数据库删除，成功后才按引用差集回收文件；
            # 提交失败回滚，任务与所有文件保持原状态、可恢复、可播放。
            db = sqlite3.connect(database, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            try:
                task_access(db, task_id, u, True, include_deleted=True)
                if not db.execute(
                    "SELECT 1 FROM deleted_tasks WHERE task_id=?", (task_id,)
                ).fetchone():
                    raise HTTPException(409, "仅回收站中的任务可以永久清除")
                row = db.execute(
                    "SELECT created FROM purge_tokens WHERE token=? AND task_id=?",
                    (body.确认令牌, task_id),
                ).fetchone()
                expired = not row or (
                    datetime.now(UTC)
                    - datetime.fromisoformat(row["created"])
                ) > timedelta(minutes=15)
                if expired:
                    raise HTTPException(403, "确认令牌无效或已过期，请重新发起永久清除")
                refs_before = _all_asset_files(db)
                sample_ids = [
                    r["id"]
                    for r in db.execute(
                        "SELECT id FROM samples WHERE task_id=?", (task_id,)
                    ).fetchall()
                ]
                for sid in sample_ids:
                    db.execute("DELETE FROM comments WHERE sample_id=?", (sid,))
                    db.execute("DELETE FROM ratings WHERE sample_id=?", (sid,))
                    db.execute("DELETE FROM aliases WHERE sample_id=?", (sid,))
                db.execute(
                    "DELETE FROM track_processing WHERE track_id IN "
                    "(SELECT t.id FROM tracks t JOIN samples s ON s.id=t.sample_id "
                    "WHERE s.task_id=?)",
                    (task_id,),
                )
                db.execute(
                    "DELETE FROM track_analysis WHERE track_id IN "
                    "(SELECT t.id FROM tracks t JOIN samples s ON s.id=t.sample_id "
                    "WHERE s.task_id=?)",
                    (task_id,),
                )
                db.execute(
                    "DELETE FROM tracks WHERE sample_id IN "
                    "(SELECT id FROM samples WHERE task_id=?)",
                    (task_id,),
                )
                db.execute("DELETE FROM samples WHERE task_id=?", (task_id,))
                db.execute("DELETE FROM purge_tokens WHERE task_id=?", (task_id,))
                db.execute("DELETE FROM review_assignments WHERE task_id=?", (task_id,))
                db.execute("DELETE FROM members WHERE task_id=?", (task_id,))
                db.execute("DELETE FROM deleted_tasks WHERE task_id=?", (task_id,))
                # 自助申请的邀请/申请记录保留审计线索，仅解除与本任务的关联。
                db.execute("UPDATE invites SET task_id=NULL WHERE task_id=?", (task_id,))
                db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
            with connect(database) as db:
                refs_after = _all_asset_files(db)
            reclaim_failed = []
            for name in sorted(refs_before - refs_after):
                try:
                    _unlink_asset(name)
                except OSError:
                    reclaim_failed.append(name)
            if reclaim_failed:
                return {
                    "已清除": True,
                    "回收失败": reclaim_failed,
                    "说明": "数据库已清除；以下文件因删除失败仍留在 assets，可稍后重试清理",
                }
            return {"已清除": True, "回收失败": []}

    @app.get("/api/tasks")
    def task_list(request: Request, deleted: bool = False):
        u = user(request)
        with connect(database) as db:
            rows = db.execute("SELECT * FROM tasks ORDER BY created DESC").fetchall()
            result = []
            for row in rows:
                try:
                    is_deleted = bool(
                        db.execute(
                            "SELECT 1 FROM deleted_tasks WHERE task_id=?", (row["id"],)
                        ).fetchone()
                    )
                    if is_deleted != deleted:
                        continue
                    t = task_access(db, row["id"], u, include_deleted=deleted)
                except HTTPException:
                    continue
                t["sample_count"] = db.execute(
                    "SELECT count(*) FROM samples WHERE task_id=?", (t["id"],)
                ).fetchone()[0]
                t["completed"] = db.execute(
                    "SELECT count(*) FROM ratings r JOIN samples s ON r.sample_id=s.id WHERE s.task_id=? AND r.user_id=?",
                    (t["id"], u["id"]),
                ).fetchone()[0]
                result.append(t)
            return result

    @app.post("/api/tasks")
    def create_task(body: TaskInput, request: Request):
        u = organizer(request)
        if body.mode not in ("development", "blind") or body.kind not in (
            "算法版本",
            "VPU 支路",
            "级联链路",
            "竞品算法",
            "竞品整机",
        ):
            raise HTTPException(422, "无效任务类型")
        task_id = uid()
        with connect(database) as db:
            db.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?,?,?)",
                (task_id, body.title, body.kind, body.mode, "draft", u["id"], now()),
            )
        return {"id": task_id}

    @app.patch("/api/tasks/{task_id}")
    def edit_task(task_id: str, body: TaskInput, request: Request):
        u = user(request)
        if body.mode not in ("development", "blind") or body.kind not in (
            "算法版本",
            "VPU 支路",
            "级联链路",
            "竞品算法",
            "竞品整机",
        ):
            raise HTTPException(422, "无效任务类型")
        with mutation_lock, connect(database) as db:
            t = task_access(db, task_id, u, True)
            if t["status"] != "draft":
                raise HTTPException(409, "发布后任务名称、类型和模式保持冻结")
            if not body.title.strip():
                raise HTTPException(422, "任务名称不能为空")
            if body.mode != t["mode"] and (
                db.execute(
                    "SELECT 1 FROM comments c JOIN samples s ON s.id=c.sample_id "
                    "WHERE s.task_id=? LIMIT 1",
                    (task_id,),
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM ratings r JOIN samples s ON s.id=r.sample_id "
                    "WHERE s.task_id=? LIMIT 1",
                    (task_id,),
                ).fetchone()
            ):
                raise HTTPException(
                    409,
                    "此任务已有标注或评分，比较模式保持冻结；需要更改时请创建新任务",
                )
            db.execute(
                "UPDATE tasks SET title=?,kind=?,mode=? WHERE id=?",
                (body.title.strip(), body.kind, body.mode, task_id),
            )
        return {"ok": True}

    @app.get("/api/tasks/{task_id}")
    def task_detail(task_id: str, request: Request):
        u = user(request)
        with connect(database) as db:
            t = task_access(db, task_id, u)
            rows = db.execute(
                "SELECT * FROM samples WHERE task_id=? ORDER BY rowid", (task_id,)
            ).fetchall()
            t["samples"] = []
            for row in rows:
                s = dict(row)
                s["completed"] = bool(
                    db.execute(
                        "SELECT 1 FROM ratings WHERE sample_id=? AND user_id=?",
                        (s["id"], u["id"]),
                    ).fetchone()
                )
                s["track_count"] = db.execute(
                    "SELECT count(*) FROM tracks WHERE sample_id=?", (s["id"],)
                ).fetchone()[0]
                t["samples"].append(s)
            t["members"] = (
                [
                    r[0]
                    for r in db.execute(
                        "SELECT user_id FROM members WHERE task_id=?", (task_id,)
                    )
                ]
                if t["can_manage"]
                else []
            )
            t["review_assignments"] = (
                [
                    r[0]
                    for r in db.execute(
                        "SELECT user_id FROM review_assignments WHERE task_id=?", (task_id,)
                    )
                ]
                if t["can_manage"]
                else []
            )
            return t

    @app.post("/api/tasks/{task_id}/samples")
    def create_sample(task_id: str, body: SampleInput, request: Request):
        u = user(request)
        if body.provenance not in (
            "PUBLIC reproducible",
            "DECLASSIFIED real-device",
            "PRIVATE local verification",
        ):
            raise HTTPException(422, "请选择数据来源级别")
        with mutation_lock, connect(database) as db:
            t = task_access(db, task_id, u, True)
            if t["status"] != "draft":
                raise HTTPException(409, "任务已发布，输入不可更改；请创建新任务")
            sample_id = uid()
            try:
                db.execute(
                    "INSERT INTO samples VALUES(?,?,?,?,?,?)",
                    (sample_id, task_id, body.name, body.scene, body.provenance, 0),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(409, "同名样本已存在")
        return {"id": sample_id}

    @app.patch("/api/samples/{sample_id}")
    def edit_sample(sample_id: str, body: SampleInput, request: Request):
        u = user(request)
        if body.provenance not in (
            "PUBLIC reproducible",
            "DECLASSIFIED real-device",
            "PRIVATE local verification",
        ):
            raise HTTPException(422, "请选择数据来源级别")
        with mutation_lock, connect(database) as db:
            s, t = sample_access(db, sample_id, u, True)
            if t["status"] != "draft":
                raise HTTPException(409, "发布后片段名称、场景和来源保持冻结")
            if not body.name.strip():
                raise HTTPException(422, "片段名称不能为空")
            try:
                db.execute(
                    "UPDATE samples SET name=?,scene=?,provenance=? WHERE id=?",
                    (body.name.strip(), body.scene, body.provenance, s["id"]),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(409, "同名片段已存在")
        return {"ok": True}

    @app.post("/api/samples/{sample_id}/tracks")
    def upload_track(
        sample_id: str,
        request: Request,
        file: Annotated[UploadFile, File()],
        name: str = Form(...),
        version: str = Form("未知"),
        channel: int = Form(0),
    ):
        u = user(request)
        with mutation_lock, connect(database) as db:
            s, t = sample_access(db, sample_id, u, True)
            if t["status"] != "draft":
                raise HTTPException(409, "已发布任务不可替换音频")
            if (
                db.execute(
                    "SELECT count(*) FROM tracks WHERE sample_id=?", (sample_id,)
                ).fetchone()[0]
                >= 6
            ):
                raise HTTPException(422, "首版每题最多 6 个候选")
            if not name.strip() or len(name) > 120 or len(version) > 200:
                raise HTTPException(422, "请填写有效版本名称")
            raw = file.file.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise HTTPException(413, "每个 WAV 最大 32 MB")
            try:
                wav, meta = decode_audio(raw, channel)
            except ValueError as e:
                raise HTTPException(422, str(e))
            if s["samples"] and s["samples"] != meta["samples"]:
                raise HTTPException(422, "候选长度不一致；请在外部确认时间对齐后重试")
            path = assets / (meta["asset_sha256"] + ".wav")
            if not path.exists():
                path.write_bytes(wav)
            track_id = uid()
            db.execute(
                "INSERT INTO tracks VALUES(?,?,?,?,?,?)",
                (track_id, sample_id, name, version, path.name, json.dumps(meta)),
            )
            db.execute(
                "UPDATE samples SET samples=? WHERE id=?", (meta["samples"], sample_id)
            )
        return {"id": track_id, "meta": meta}

    @app.patch("/api/tracks/{track_id}")
    def edit_track(track_id: str, body: TrackInput, request: Request):
        u = user(request)
        with mutation_lock, connect(database) as db:
            tr = db.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
            if not tr:
                raise HTTPException(404, "候选不存在")
            _, t = sample_access(db, tr["sample_id"], u, True)
            if t["status"] != "draft":
                raise HTTPException(409, "已发布任务不可修改候选")
            if not body.name.strip():
                raise HTTPException(422, "版本名称不能为空")
            db.execute(
                "UPDATE tracks SET name=?,version=? WHERE id=?",
                (body.name.strip(), body.version, track_id),
            )
        return {"ok": True}

    @app.delete("/api/tracks/{track_id}")
    def delete_track(track_id: str, request: Request):
        u = user(request)
        with mutation_lock, connect(database) as db:
            tr = db.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
            if not tr:
                raise HTTPException(404, "候选不存在")
            _, t = sample_access(db, tr["sample_id"], u, True)
            if t["status"] != "draft":
                raise HTTPException(409, "已发布任务不可删除候选")
            if db.execute(
                "SELECT 1 FROM comments WHERE track_id=? OR (sample_id=? AND track_id IS NULL)",
                (track_id, tr["sample_id"]),
            ).fetchone():
                raise HTTPException(409, "候选已有标注，保留其音频依据；请创建新片段")
            db.execute("DELETE FROM aliases WHERE sample_id=?", (tr["sample_id"],))
            db.execute("DELETE FROM tracks WHERE id=?", (track_id,))
            if not db.execute(
                "SELECT 1 FROM tracks WHERE sample_id=?", (tr["sample_id"],)
            ).fetchone():
                db.execute(
                    "UPDATE samples SET samples=0 WHERE id=?", (tr["sample_id"],)
                )
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/import-sample")
    def import_sample(
        task_id: str,
        request: Request,
        files: Annotated[list[UploadFile], File()],
        manifest: str = Form(...),
    ):
        # 每个片段独立原子提交；同一请求重试不会生成重复题目。
        u = user(request)
        with mutation_lock, connect(database) as db:
            t = task_access(db, task_id, u, True)
            if t["status"] != "draft":
                raise HTTPException(409, "已发布任务不可导入")
            try:
                data = json.loads(manifest)
                sample = SampleInput.model_validate(data)
                request_id = data["request_id"]
                if (
                    not isinstance(request_id, str)
                    or len(request_id) != 32
                    or any(c not in "0123456789abcdef" for c in request_id)
                ):
                    raise ValueError("无效导入编号")
                candidates = data["tracks"]
                if not 2 <= len(files) <= 6 or len(candidates) != len(files):
                    raise ValueError("每个片段需要 2–6 个对应候选")
                if sample.provenance not in (
                    "PUBLIC reproducible",
                    "DECLASSIFIED real-device",
                    "PRIVATE local verification",
                ):
                    raise ValueError("请选择数据来源级别")
                decoded = []
                for file, candidate in zip(files, candidates):
                    track = TrackInput.model_validate(candidate)
                    if not track.name.strip():
                        raise ValueError("版本名称不能为空")
                    raw = file.file.read(32 * 1024 * 1024 + 1)
                    if len(raw) > 32 * 1024 * 1024:
                        raise ValueError("每个 WAV 最大 32 MB")
                    wav, meta = decode_audio(raw, int(candidate.get("channel", 0)))
                    decoded.append((track, wav, meta))
                if len({meta["samples"] for _, _, meta in decoded}) != 1:
                    raise ValueError("候选长度不一致；请在外部确认时间对齐后重试")
            except (ValueError, TypeError, KeyError) as exc:
                raise HTTPException(422, "导入失败：" + str(exc))
            old = db.execute(
                "SELECT * FROM samples WHERE id=?", (request_id,)
            ).fetchone()
            if old:
                old_tracks = db.execute(
                    "SELECT * FROM tracks WHERE sample_id=? ORDER BY rowid",
                    (request_id,),
                ).fetchall()
                identical = (
                    old["task_id"] == task_id
                    and old["name"] == sample.name
                    and old["scene"] == sample.scene
                    and old["provenance"] == sample.provenance
                    and len(old_tracks) == len(decoded)
                )
                if identical:
                    identical = all(
                        a["name"] == b.name
                        and a["version"] == b.version
                        and all(
                            json.loads(a["meta"])[key] == m[key]
                            for key in ("sha256", "channel")
                        )
                        for a, (b, _, m) in zip(old_tracks, decoded)
                    )
                if identical:
                    return {"id": request_id, "reused": True}
                raise HTTPException(409, "导入编号已使用且内容不同，请重新预览")
            if db.execute(
                "SELECT 1 FROM samples WHERE task_id=? AND name=?",
                (task_id, sample.name),
            ).fetchone():
                raise HTTPException(409, "同名片段已存在，请更名后重新预览")
            db.execute(
                "INSERT INTO samples VALUES(?,?,?,?,?,?)",
                (
                    request_id,
                    task_id,
                    sample.name,
                    sample.scene,
                    sample.provenance,
                    decoded[0][2]["samples"],
                ),
            )
            for track, wav, meta in decoded:
                path = assets / (meta["asset_sha256"] + ".wav")
                if not path.exists():
                    path.write_bytes(wav)
                db.execute(
                    "INSERT INTO tracks VALUES(?,?,?,?,?,?)",
                    (
                        uid(),
                        request_id,
                        track.name,
                        track.version,
                        path.name,
                        json.dumps(meta),
                    ),
                )
        return {"id": request_id, "reused": False}

    @app.post("/api/tasks/{task_id}/publish")
    def publish(task_id: str, body: PublishInput, request: Request):
        u = user(request)
        with mutation_lock, connect(database) as db:
            t = task_access(db, task_id, u, True)
            if t["status"] != "draft":
                raise HTTPException(409, "任务已发布")
            if not body.alignment_confirmed:
                raise HTTPException(422, "发布前须人工确认同一输入、时间对齐及增益处理")
            rows = db.execute(
                "SELECT s.id,count(tr.id) n FROM samples s LEFT JOIN tracks tr ON tr.sample_id=s.id WHERE s.task_id=? GROUP BY s.id",
                (task_id,),
            ).fetchall()
            if not rows or any(r["n"] < 2 for r in rows):
                raise HTTPException(422, "每个样本至少需要两个版本")
            # body.users 才是受邀评测者；owner 自动获得访问权限，但不构成评测义务。
            for member in set(body.users):
                if not db.execute(
                    "SELECT 1 FROM users WHERE id=?", (member,)
                ).fetchone():
                    raise HTTPException(422, "评测者不存在")
                db.execute(
                    "INSERT OR IGNORE INTO review_assignments VALUES(?,?)",
                    (task_id, member),
                )
            for member in set(body.users) | {u["id"]}:
                db.execute(
                    "INSERT OR IGNORE INTO members VALUES(?,?)", (task_id, member)
                )
            db.execute("UPDATE tasks SET status='active' WHERE id=?", (task_id,))
        return {"ok": True}

    @app.patch("/api/tasks/{task_id}/members")
    def update_members(task_id: str, body: MembersInput, request: Request):
        u = user(request)
        with mutation_lock, connect(database) as db:
            t = task_access(db, task_id, u, True)
            if t["status"] == "closed":
                raise HTTPException(409, "已关闭任务的参与人员保持冻结")
            requested = set(body.users)
            if any(
                not db.execute("SELECT 1 FROM users WHERE id=?", (member,)).fetchone()
                for member in requested
            ):
                raise HTTPException(422, "参与者不存在")
            existing = {
                row[0]
                for row in db.execute(
                    "SELECT user_id FROM members WHERE task_id=?", (task_id,)
                )
            }
            # owner 的访问权限无条件保留；只有真正失去访问的人才需要移除保护。
            losing_access = existing - requested - {t["owner"]}
            for removed in losing_access:
                contributed = db.execute(
                    "SELECT 1 FROM samples s LEFT JOIN comments c ON c.sample_id=s.id AND c.user_id=? "
                    "LEFT JOIN ratings r ON r.sample_id=s.id AND r.user_id=? "
                    "WHERE s.task_id=? AND (c.id IS NOT NULL OR r.user_id IS NOT NULL) LIMIT 1",
                    (removed, removed, task_id),
                ).fetchone()
                if contributed:
                    name = db.execute(
                        "SELECT name FROM users WHERE id=?", (removed,)
                    ).fetchone()[0]
                    raise HTTPException(409, f"{name} 已提交评论或判断，不能移出任务")
            for added in requested - existing:
                db.execute("INSERT INTO members VALUES(?,?)", (task_id, added))
            for removed in losing_access:
                db.execute(
                    "DELETE FROM members WHERE task_id=? AND user_id=?",
                    (task_id, removed),
                )
            # 同步受邀评测者名单：勾选即受邀；owner 不再被强制加入；
            # 已贡献者无法被移出访问，因此也不会从受邀名单中丢失。
            assigned = {
                row[0]
                for row in db.execute(
                    "SELECT user_id FROM review_assignments WHERE task_id=?", (task_id,)
                )
            }
            for added in requested - assigned:
                db.execute(
                    "INSERT OR IGNORE INTO review_assignments VALUES(?,?)",
                    (task_id, added),
                )
            for removed in assigned - requested:
                contributed = db.execute(
                    "SELECT 1 FROM samples s LEFT JOIN comments c ON c.sample_id=s.id AND c.user_id=? "
                    "LEFT JOIN ratings r ON r.sample_id=s.id AND r.user_id=? "
                    "WHERE s.task_id=? AND (c.id IS NOT NULL OR r.user_id IS NOT NULL) LIMIT 1",
                    (removed, removed, task_id),
                ).fetchone()
                if contributed:
                    name = db.execute(
                        "SELECT name FROM users WHERE id=?", (removed,)
                    ).fetchone()[0]
                    raise HTTPException(409, f"{name} 已提交评论或判断，不能移出受邀名单")
                db.execute(
                    "DELETE FROM review_assignments WHERE task_id=? AND user_id=?",
                    (task_id, removed),
                )
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/close")
    def close(task_id: str, request: Request):
        u = user(request)
        with mutation_lock, connect(database) as db:
            t = task_access(db, task_id, u, True)
            if t["status"] != "active":
                raise HTTPException(409, "只能关闭已发布任务")
            db.execute("UPDATE tasks SET status='closed' WHERE id=?", (task_id,))
        return {"ok": True}

    def _load_track_audio(track_id):
        with connect(database) as db:
            tr = db.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
        if not tr:
            raise HTTPException(404, "候选不存在")
        path = assets / tr["path"]
        if not path.exists():
            raise HTTPException(409, "音频资产缺失，请联系组织者恢复备份")
        data, _ = sf.read(path, dtype="float64")
        return tr, data

    def _sample_managers_only(db, sample_id, u, require_draft=True):
        s, t = sample_access(db, sample_id, u)
        if not t["can_manage"]:
            raise HTTPException(403, "仅任务管理者可以操作对齐与响度")
        if require_draft and t["status"] != "draft":
            raise HTTPException(409, "发布后播放处理冻结，不能分析或修改")
        return s, t

    def _processing_modes(db, sample_id):
        rows = db.execute(
            "SELECT tp.track_id, tp.data FROM track_processing tp "
            "JOIN tracks tr ON tr.id=tp.track_id WHERE tr.sample_id=?",
            (sample_id,),
        ).fetchall()
        return {r["track_id"]: json.loads(r["data"]) for r in rows}

    def _analysis_rows(db, sample_id):
        rows = db.execute(
            "SELECT ta.track_id, ta.data FROM track_analysis ta "
            "JOIN tracks tr ON tr.id=ta.track_id WHERE tr.sample_id=?",
            (sample_id,),
        ).fetchall()
        return {r["track_id"]: json.loads(r["data"]) for r in rows}

    @app.get("/api/samples/{sample_id}/alignment")
    def analyze_sample_alignment(sample_id: str, request: Request, reference: str = ""):
        u = user(request)
        with connect(database) as db:
            s, _ = _sample_managers_only(db, sample_id, u)
            tracks = db.execute(
                "SELECT * FROM tracks WHERE sample_id=? ORDER BY rowid", (sample_id,)
            ).fetchall()
            by_id = {tr["id"]: tr for tr in tracks}
            if reference not in by_id:
                raise HTTPException(422, "参考候选必须属于当前片段")
            if reference in _processing_modes(db, sample_id):
                raise HTTPException(
                    409,
                    "参考候选已有派生处理，试听与原始不一致；请先恢复全部原始音频，再更换参考",
                )
        ref_track, ref_x = _load_track_audio(reference)
        results = []
        persist = []
        for tr in tracks:
            if tr["id"] == reference:
                results.append(
                    {
                        "track_id": tr["id"],
                        "名称": tr["name"],
                        "角色": "参考",
                        "延迟": {"lag": 0, "可应用": True, "拒绝码": None},
                        "响度": {"建议增益db": 0.0, "可应用": True, "拒绝码": None},
                    }
                )
                continue
            _, cand_x = _load_track_audio(tr["id"])
            delay = estimate_delay(ref_x, cand_x)
            lag = delay.metrics.get("lag", 0) if delay.applicable else 0
            gain = estimate_gain(ref_x, cand_x, lag, delay.applicable)
            entry = {
                "track_id": tr["id"],
                "名称": tr["name"],
                "角色": "候选",
                "延迟": dict(delay.as_dict(), lag符号定义=LAG_SIGN_DEFINITION),
                "响度": gain.as_dict(),
            }
            results.append(entry)
            persist.append((tr["id"], entry))
        with mutation_lock, connect(database) as db:
            for track_id, entry in persist:
                db.execute(
                    "INSERT OR REPLACE INTO track_analysis VALUES(?,?)",
                    (track_id, json.dumps({"参考": reference, **entry, "生成时间": now()}, ensure_ascii=False)),
                )
        return {"片段": s["name"], "参考": {"track_id": reference, "名称": ref_track["name"]}, "候选": results}

    class ProcessingItem(BaseModel):
        候选: str
        对齐: bool = False
        响度: bool = False

    class ProcessingInput(BaseModel):
        参考: str
        处理: Annotated[list[ProcessingItem], Field(default_factory=list, max_length=6)]

    def _referenced_derived_files(db):
        return {
            json.loads(r[0])["派生文件"]
            for r in db.execute("SELECT data FROM track_processing")
        }

    def _unlink_derived(name):
        # 仅清理 assets 下由服务端 SHA 派生的文件名，不接受外部任意路径。
        if not re.fullmatch(r"[0-9a-f]{64}\.wav", name):
            return
        (assets / name).unlink(missing_ok=True)

    @app.post("/api/samples/{sample_id}/processing")
    def apply_sample_processing(sample_id: str, body: ProcessingInput, request: Request):
        u = user(request)
        with mutation_lock:
            # 手工事务：显式提交点放在文件写入之后；提交失败由补偿删除本次
            # 新建的文件，保证不留下无引用资产。不修改全局 connect() 语义。
            db = sqlite3.connect(database, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            created_files: list[Path] = []
            try:
                _ = _sample_managers_only(db, sample_id, u)
                referenced_before = _referenced_derived_files(db)
                tracks = db.execute(
                    "SELECT * FROM tracks WHERE sample_id=? ORDER BY rowid",
                    (sample_id,),
                ).fetchall()
                by_id = {tr["id"]: tr for tr in tracks}
                if body.参考 not in by_id:
                    raise HTTPException(422, "参考候选必须属于当前片段")
                if body.参考 in _processing_modes(db, sample_id):
                    raise HTTPException(
                        409,
                        "参考候选已有派生处理，试听与原始不一致；请先恢复全部原始音频，再更换参考",
                    )
                annotated = db.execute(
                    "SELECT 1 FROM comments WHERE sample_id=? LIMIT 1", (sample_id,)
                ).fetchone() or db.execute(
                    "SELECT 1 FROM ratings WHERE sample_id=? LIMIT 1", (sample_id,)
                ).fetchone()
                existing_modes = _processing_modes(db, sample_id)
                if annotated and (existing_modes or body.处理):
                    raise HTTPException(
                        409,
                        "此片段已有评论或评分关联，不能替换试听资产；请创建新任务",
                    )
                if not body.处理:
                    raise HTTPException(422, "请至少为一个候选选择要应用的处理")
                _, ref_x = _load_track_audio(body.参考)
                plans = []
                requested_ids = set()
                for item in body.处理:
                    tr = by_id.get(item.候选)
                    if not tr:
                        raise HTTPException(422, "候选不属于当前片段")
                    if item.候选 == body.参考:
                        raise HTTPException(422, "参考候选保持原始资产，不需要处理")
                    if item.候选 in requested_ids:
                        raise HTTPException(422, "同一候选重复出现")
                    requested_ids.add(item.候选)
                    if not item.对齐 and not item.响度:
                        continue
                    _, cand_x = _load_track_audio(tr["id"])
                    delay = estimate_delay(ref_x, cand_x)
                    if item.对齐 and not delay.applicable:
                        raise HTTPException(
                            422,
                            f"对齐不可应用（{delay.reason_code}）：{delay.reason}",
                        )
                    lag = delay.metrics.get("lag", 0) if delay.applicable else 0
                    gain = estimate_gain(ref_x, cand_x, lag, delay.applicable)
                    if item.响度:
                        if not delay.applicable:
                            raise HTTPException(
                                422,
                                f"响度不可应用（{ERR_DELAY_NOT_APPLICABLE}）：{gain.reason}",
                            )
                        if not gain.applicable:
                            raise HTTPException(
                                422,
                                f"响度不可应用（{gain.reason_code}）：{gain.reason}",
                            )
                    lag_applied = lag if item.对齐 else 0
                    gain_db = gain.metrics.get("建议增益db", 0.0) if item.响度 else 0.0
                    derived = apply_gain(
                        shift_samples(cand_x, lag_applied), gain_db
                    ).astype(np.float32)
                    peak_before = float(np.max(np.abs(cand_x))) if len(cand_x) else 0.0
                    peak_after = float(np.max(np.abs(derived))) if len(derived) else 0.0
                    wav = encode_float_wav(derived)
                    plans.append(
                        (
                            tr,
                            {
                                "模式": (
                                    "对齐+响度"
                                    if item.对齐 and item.响度
                                    else "对齐" if item.对齐 else "响度"
                                ),
                                "参考": body.参考,
                                "lag": lag_applied,
                                "lag符号定义": LAG_SIGN_DEFINITION,
                                "gain_db": gain_db,
                                "原始资产SHA256": json.loads(tr["meta"])["asset_sha256"],
                                "派生资产SHA256": hashlib.sha256(wav).hexdigest(),
                                "派生文件": hashlib.sha256(wav).hexdigest() + ".wav",
                                "算法": ALGORITHM_NAME,
                                "活动门限": gain.metrics.get("活动门限"),
                                "活动覆盖": gain.metrics.get("覆盖"),
                                "共同活动秒": gain.metrics.get("共同活动秒"),
                                "峰值前": round(peak_before, 6),
                                "峰值后": round(peak_after, 6),
                                "处理顺序": PROCESSING_ORDER,
                                "生成时间": now(),
                            },
                            wav,
                        )
                    )
                if not plans:
                    raise HTTPException(422, "没有需要应用的处理")
                for _, proc, wav in plans:
                    # 文件名只能由服务端计算的 SHA 派生，内容寻址且幂等。
                    path = assets / proc["派生文件"]
                    if not path.exists():
                        path.write_bytes(wav)
                        created_files.append(path)
                for tr, proc, _ in plans:
                    db.execute(
                        "INSERT OR REPLACE INTO track_processing VALUES(?,?)",
                        (tr["id"], json.dumps(proc, ensure_ascii=False)),
                    )
                db.commit()
            except BaseException:
                # 补偿：回滚数据库并删除本次新建、尚未提交引用的文件。
                db.rollback()
                for path in created_files:
                    path.unlink(missing_ok=True)
                raise
            finally:
                db.close()
            # 提交成功后回收不再被任何 track_processing 行引用的旧派生文件。
            with connect(database) as db:
                referenced_after = _referenced_derived_files(db)
            for name in referenced_before - referenced_after:
                _unlink_derived(name)
            return {
                "已应用": [
                    {
                        "track_id": tr["id"],
                        "名称": tr["name"],
                        "模式": proc["模式"],
                        "lag": proc["lag"],
                        "gain_db": proc["gain_db"],
                        "派生资产SHA256": proc["派生资产SHA256"],
                        "峰值前后": [proc["峰值前"], proc["峰值后"]],
                    }
                    for tr, proc, _ in plans
                ]
            }

    @app.post("/api/samples/{sample_id}/restore-processing")
    def restore_sample_processing(sample_id: str, request: Request):
        u = user(request)
        with mutation_lock:
            # 手工事务：先提交数据库删除，成功后才回收文件；提交失败时
            # 回滚，原 DB 引用的派生资产保持完整可播放。
            db = sqlite3.connect(database, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            try:
                _ = _sample_managers_only(db, sample_id, u)
                rows = db.execute(
                    "SELECT tp.track_id, tp.data FROM track_processing tp "
                    "JOIN tracks tr ON tr.id=tp.track_id WHERE tr.sample_id=?",
                    (sample_id,),
                ).fetchall()
                if not rows:
                    raise HTTPException(409, "当前片段没有已应用的处理")
                annotated = db.execute(
                    "SELECT 1 FROM comments WHERE sample_id=? LIMIT 1", (sample_id,)
                ).fetchone() or db.execute(
                    "SELECT 1 FROM ratings WHERE sample_id=? LIMIT 1", (sample_id,)
                ).fetchone()
                if annotated:
                    raise HTTPException(
                        409,
                        "此片段已有评论或评分关联，不能替换试听资产；请创建新任务",
                    )
                referenced_before = _referenced_derived_files(db)
                restored_ids = [row["track_id"] for row in rows]
                for row in rows:
                    db.execute(
                        "DELETE FROM track_processing WHERE track_id=?",
                        (row["track_id"],),
                    )
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
            # 提交成功后回收不再被任何 track_processing 行引用的派生文件。
            with connect(database) as db:
                referenced_after = _referenced_derived_files(db)
            for name in referenced_before - referenced_after:
                _unlink_derived(name)
            return {"已恢复": restored_ids}

    @app.get("/api/tasks/{task_id}/processing-summary")
    def task_processing_summary(task_id: str, request: Request):
        u = user(request)
        with connect(database) as db:
            t = task_access(db, task_id, u, True)
            samples = db.execute(
                "SELECT * FROM samples WHERE task_id=? ORDER BY rowid", (task_id,)
            ).fetchall()
            out = []
            for s in samples:
                tracks = db.execute(
                    "SELECT * FROM tracks WHERE sample_id=? ORDER BY rowid", (s["id"],)
                ).fetchall()
                modes = _processing_modes(db, s["id"])
                analyses = _analysis_rows(db, s["id"])
                entries = []
                for tr in tracks:
                    proc = modes.get(tr["id"])
                    analysis = analyses.get(tr["id"])
                    rejected = []
                    if analysis:
                        for key in ("延迟", "响度"):
                            part = analysis.get(key) or {}
                            if not part.get("可应用", True) and part.get("拒绝码"):
                                rejected.append(
                                    {
                                        "候选": tr["name"],
                                        "判据": key,
                                        "拒绝码": part["拒绝码"],
                                        "原因": part.get("原因", REASONS.get(part["拒绝码"], "")),
                                    }
                                )
                    entries.append(
                        {
                            "track_id": tr["id"],
                            "名称": tr["name"],
                            "模式": proc["模式"] if proc else "原始",
                            "拒绝": rejected,
                        }
                    )
                mixed = len({e["模式"] for e in entries}) > 1
                out.append(
                    {
                        "片段ID": s["id"],
                        "片段": s["name"],
                        "候选": entries,
                        "混合处理": mixed,
                    }
                )
            return {"任务": task_id, "状态": t["status"], "片段": out}

    @app.get("/api/tasks/{task_id}/progress")
    def task_progress(task_id: str, request: Request):
        # 收集期间的管理者进度：只含受邀名单、每人已提交片段数与三态状态，
        # 不含 choice、候选 ID/名称、票数或评论内容等盲评信息。
        u = user(request)
        with connect(database) as db:
            t = task_access(db, task_id, u, True)
            if t["status"] == "draft":
                raise HTTPException(409, "任务发布后才有评测进度")
            summary = task_summary(db, task_id)
        return {
            "任务": {"id": task_id, "status": t["status"], "模式": t["mode"]},
            "参与进度": summary["参与进度"],
        }

    @app.get("/api/samples/{sample_id}")
    def sample_detail(sample_id: str, request: Request):
        u = user(request)
        with mutation_lock, connect(database) as db:
            s, t = sample_access(db, sample_id, u)
            blind = t["mode"] == "blind" and t["status"] == "active"
            mapping = aliases(db, sample_id, u)
            rows = db.execute(
                "SELECT * FROM tracks WHERE sample_id=? ORDER BY rowid", (sample_id,)
            ).fetchall()
            proc_modes = (
                {} if blind else _processing_modes(db, sample_id)
            )
            s["tracks"] = []
            for row in sorted(rows, key=lambda x: mapping[x["id"]]):
                tr = {
                    "id": row["id"],
                    "label": chr(65 + mapping[row["id"]]),
                    "name": "隐藏版本" if blind else row["name"],
                    "version": "" if blind else row["version"],
                }
                if not blind:
                    tr["meta"] = json.loads(row["meta"])
                    proc = proc_modes.get(row["id"])
                    tr["处理"] = (
                        {
                            "模式": proc["模式"],
                            "lag": proc["lag"],
                            "gain_db": proc["gain_db"],
                            "参考": proc["参考"],
                        }
                        if proc
                        else None
                    )
                s["tracks"].append(tr)
            comments = db.execute(
                "SELECT c.*,u.name author FROM comments c JOIN users u ON c.user_id=u.id WHERE c.sample_id=? ORDER BY c.created",
                (sample_id,),
            ).fetchall()
            s["comments"] = [
                dict(c) for c in comments if not blind or c["user_id"] == u["id"]
            ]
            rating = db.execute(
                "SELECT * FROM ratings WHERE sample_id=? AND user_id=?",
                (sample_id, u["id"]),
            ).fetchone()
            s["rating"] = dict(rating) if rating else None
            s["blind"] = blind
            return s

    def audio_access(track_id, request):
        u = user(request)
        with connect(database) as db:
            tr = db.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
            if not tr:
                raise HTTPException(404, "候选不存在")
            _, task = sample_access(db, tr["sample_id"], u)
            row = db.execute(
                "SELECT data FROM track_processing WHERE track_id=?", (track_id,)
            ).fetchone()
        if row:
            proc = json.loads(row["data"])
            path = assets / proc["派生文件"]
            if (
                not path.exists()
                or hashlib.sha256(path.read_bytes()).hexdigest()
                != proc["派生资产SHA256"]
            ):
                raise HTTPException(
                    409, "派生音频完整性检查失败，请联系组织者恢复备份"
                )
            return path, task
        path = assets / tr["path"]
        if (
            not path.exists()
            or hashlib.sha256(path.read_bytes()).hexdigest()
            != json.loads(tr["meta"])["asset_sha256"]
        ):
            raise HTTPException(409, "音频资产完整性检查失败，请联系组织者恢复备份")
        return path, task

    @app.get("/api/audio/{track_id}")
    def audio(track_id: str, request: Request):
        path, _ = audio_access(track_id, request)
        return FileResponse(
            path,
            media_type="audio/wav",
            filename="试听.wav",
            content_disposition_type="inline",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/audio/{track_id}/analysis")
    def analysis(track_id: str, request: Request):
        path, task = audio_access(track_id, request)
        if task["mode"] == "blind" and task["status"] == "active":
            raise HTTPException(403, "独立评测期间不展示分析数据")
        return analyze(path)

    @app.post("/api/samples/{sample_id}/comments")
    def comment(sample_id: str, body: CommentInput, request: Request):
        u = user(request)
        with mutation_lock, connect(database) as db:
            s, t = sample_access(db, sample_id, u)
            if not 0 <= body.start < body.end <= s["samples"]:
                raise HTTPException(422, "请选择音频范围内的有效区间")
            if (
                body.track_id
                and not db.execute(
                    "SELECT 1 FROM tracks WHERE id=? AND sample_id=?",
                    (body.track_id, sample_id),
                ).fetchone()
            ):
                raise HTTPException(422, "候选不属于此样本")
            if body.parent:
                p = db.execute(
                    "SELECT * FROM comments WHERE id=? AND sample_id=?",
                    (body.parent, sample_id),
                ).fetchone()
                if not p or (
                    t["mode"] == "blind"
                    and t["status"] == "active"
                    and p["user_id"] != u["id"]
                ):
                    raise HTTPException(403, "无法回复此评论")
            comment_id = uid()
            db.execute(
                "INSERT INTO comments VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    comment_id,
                    sample_id,
                    u["id"],
                    body.track_id,
                    body.start,
                    body.end,
                    body.body,
                    body.tag,
                    body.parent,
                    now(),
                ),
            )
        return {"id": comment_id}

    @app.post("/api/samples/{sample_id}/rating")
    def rating(sample_id: str, body: RatingInput, request: Request):
        u = user(request)
        with mutation_lock, connect(database) as db:
            _, t = sample_access(db, sample_id, u)
            if t["status"] != "active":
                raise HTTPException(409, "当前任务不接受评分")
            if (
                body.choice != "tie"
                and not db.execute(
                    "SELECT 1 FROM tracks WHERE id=? AND sample_id=?",
                    (body.choice, sample_id),
                ).fetchone()
            ):
                raise HTTPException(422, "请选择此题中的候选或无明显差异")
            existing = db.execute(
                "SELECT * FROM ratings WHERE sample_id=? AND user_id=?",
                (sample_id, u["id"]),
            ).fetchone()
            if existing:
                if (
                    existing["choice"] == body.choice
                    and existing["reason"] == body.reason
                ):
                    return {"ok": True}
                raise HTTPException(409, "判断已提交并锁定；请在复盘评论中补充")
            # 幂等重试优先保持原有语义；首次提交仅限受邀评测者，
            # 未受邀的负责人/管理员不会产生评分行。
            if not db.execute(
                "SELECT 1 FROM review_assignments WHERE task_id=? AND user_id=?",
                (t["id"], u["id"]),
            ).fetchone():
                raise HTTPException(
                    403,
                    "只有受邀评测者可以提交评分；负责人请在发布或受邀名单中勾选自己",
                )
            db.execute(
                "INSERT INTO ratings VALUES(?,?,?,?,?)",
                (sample_id, u["id"], body.choice, body.reason, now()),
            )
        return {"ok": True}

    def report(task_id, u):
        with connect(database) as db:
            t = task_access(db, task_id, u)
            if t["status"] != "closed":
                raise HTTPException(403, "任务关闭并揭晓后才能查看汇总")
            summary = task_summary(db, task_id)
            samples = []
            for row in db.execute(
                "SELECT * FROM samples WHERE task_id=?", (task_id,)
            ).fetchall():
                s = dict(row)
                s.update(summary["逐片段"][s["id"]])
                proc_modes = _processing_modes(db, s["id"])
                analyses = _analysis_rows(db, s["id"])
                track_entries = []
                for tr in db.execute(
                    "SELECT * FROM tracks WHERE sample_id=?", (s["id"],)
                ).fetchall():
                    proc = proc_modes.get(tr["id"])
                    track_entries.append(
                        {
                            "id": tr["id"],
                            "name": tr["name"],
                            "version": tr["version"],
                            "meta": json.loads(tr["meta"]),
                            "处理口径": (
                                {
                                    "模式": proc["模式"],
                                    "参考track": proc["参考"],
                                    "lag": proc["lag"],
                                    "lag符号定义": proc["lag符号定义"],
                                    "增益db": proc["gain_db"],
                                    "原始资产SHA256": proc["原始资产SHA256"],
                                    "派生资产SHA256": proc["派生资产SHA256"],
                                    "算法": proc["算法"],
                                    "处理顺序": proc["处理顺序"],
                                    "生成时间": proc["生成时间"],
                                }
                                if proc
                                else "原始"
                            ),
                        }
                    )
                s["tracks"] = track_entries
                rejected = []
                for tr in s["tracks"]:
                    analysis = analyses.get(tr["id"])
                    if not analysis:
                        continue
                    for key in ("延迟", "响度"):
                        part = analysis.get(key) or {}
                        if not part.get("可应用", True) and part.get("拒绝码"):
                            rejected.append(
                                {
                                    "候选": tr["name"],
                                    "判据": key,
                                    "拒绝码": part["拒绝码"],
                                    "原因": part.get("原因", ""),
                                }
                            )
                s["处理摘要"] = {
                    "混合处理": len({t2["处理口径"]["模式"] if isinstance(t2["处理口径"], dict) else "原始" for t2 in track_entries}) > 1,
                    "拒绝建议": rejected,
                }
                s["ratings"] = [
                    dict(x)
                    for x in db.execute(
                        "SELECT r.*,u.name author FROM ratings r JOIN users u ON r.user_id=u.id WHERE sample_id=?",
                        (s["id"],),
                    )
                ]
                s["comments"] = [
                    dict(x)
                    for x in db.execute(
                        "SELECT c.*,u.name author FROM comments c JOIN users u ON c.user_id=u.id WHERE sample_id=?",
                        (s["id"],),
                    )
                ]
                samples.append(s)
            return {
                "任务": dict(t),
                "样本": samples,
                "参与进度": summary["参与进度"],
                "处理口径": "派生试听资产按「先整数采样对齐，再固定增益」生成；原始 WAV 未修改。活动段 RMS 匹配不是 LUFS/ITU BS.1770 响度校准，也不是听感等响。",
                "标签汇总": summary["标签汇总"],
                "生成时间": now(),
                "播放口径": "原始相对电平；公共监听增益；16 kHz；无延迟自动校正",
                "解释边界": "探索性偏好统计，不是标准 MUSHRA，不以评分证明干净语音恢复",
            }

    @app.get("/api/tasks/{task_id}/report")
    def get_report(task_id: str, request: Request):
        return report(task_id, user(request))

    @app.get("/api/tasks/{task_id}/export")
    def export(task_id: str, request: Request):
        u = user(request)
        with connect(database) as db:
            task_access(db, task_id, u, True)
        value = report(task_id, u)
        return Response(
            json.dumps(value, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="evaluation.json"'},
        )

    @app.get("/api/tasks/{task_id}/export.csv")
    def export_csv(task_id: str, request: Request):
        u = user(request)
        with connect(database) as db:
            task_access(db, task_id, u, True)
        return Response(
            results_csv(report(task_id, u)),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="evaluation-results.csv"'
            },
        )

    @app.get("/api/tasks/{task_id}/export.md")
    def export_markdown(task_id: str, request: Request):
        u = user(request)
        with connect(database) as db:
            task_access(db, task_id, u, True)
        return Response(
            results_markdown(report(task_id, u)),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="evaluation-report.md"'
            },
        )

    @app.get("/api/backup")
    def backup(request: Request):
        admin(request)
        # 暂停应用写入，SQLite 在线备份与不可变音频形成一致快照。
        with mutation_lock:
            memory = sqlite3.connect(":memory:")
            with connect(database) as db:
                db.backup(memory)
            memory.execute("DELETE FROM sessions")
            memory.commit()
            blob = memory.serialize()
            paths = {
                r[0] for r in memory.execute("SELECT DISTINCT path FROM tracks")
            }
            for (data,) in memory.execute("SELECT data FROM track_processing"):
                paths.add(json.loads(data)["派生文件"])
            paths = sorted(paths)
            memory.close()
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as z:
                z.writestr("workbench.sqlite3", blob)
                for path in paths:
                    z.write(assets / path, "assets/" + path)
                z.writestr(
                    "恢复说明.txt",
                    "停止服务后，将数据库和 assets 目录解压到一个新数据目录，使用 --data 指向该目录启动。账号保留，所有会话已注销。备份包含任务音频和账号口令哈希，应仅在获准位置保存。",
                )
            output.seek(0)
        return StreamingResponse(
            output,
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="workbench-backup.zip"'
            },
        )

    @app.post("/api/demo")
    def demo(request: Request):
        u = organizer(request)
        with mutation_lock, connect(database) as db:
            task_id = uid()
            db.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?,?,?)",
                (
                    task_id,
                    "合成音试听 · 熟悉工作台",
                    "算法版本",
                    "development",
                    "draft",
                    u["id"],
                    now(),
                ),
            )
            for i, title in enumerate(
                ("谐波与短时衰减", "不同基频与底噪", "稳定包络与残留")
            ):
                sample_id = uid()
                db.execute(
                    "INSERT INTO samples VALUES(?,?,?,?,?,?)",
                    (
                        sample_id,
                        task_id,
                        title,
                        "纯合成演示 · 非真人录音",
                        "PUBLIC reproducible",
                        128000,
                    ),
                )
                for v, name in enumerate(("带噪基线", "降噪与局部衰减", "温和降噪")):
                    wav, meta = decode_audio(demo_wav(v, i))
                    path = assets / (meta["asset_sha256"] + ".wav")
                    path.write_bytes(wav)
                    db.execute(
                        "INSERT INTO tracks VALUES(?,?,?,?,?,?)",
                        (
                            uid(),
                            sample_id,
                            name,
                            "数学合成 v1",
                            path.name,
                            json.dumps(meta),
                        ),
                    )
            return {"id": task_id}

    if static_dir and Path(static_dir).exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="界面")
    return app
