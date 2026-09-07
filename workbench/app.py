"""局域网协作 API。音频、匿名映射和评论始终经过授权检查。"""

import csv
import hashlib
import io
import json
import secrets
import sqlite3
import threading
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .audio import analyze, decode_audio, demo_wav
from .db import connect, init, password_hash, verify
from .version import VERSION


def uid():
    return secrets.token_hex(16)


def now():
    return datetime.now(UTC).isoformat()


STATUS_NAMES = {"draft": "准备中", "active": "评测进行中", "closed": "已揭晓"}
MODE_NAMES = {"development": "开发诊断", "blind": "隐藏版本评测"}
CSV_HEADER = ["部分", "条目ID", "片段ID", "片段名称", "条目", "标签", "数值", "分母", "百分比", "说明"]


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
    lines += ["## 问题标签汇总", "", "仅统计根评论，回复楼层不计入。", ""]
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
    attempts: dict[str, list[float]] = {}

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
        token = hashlib.sha256(request.cookies.get("session", "").encode()).hexdigest()
        with connect(database) as db:
            row = db.execute(
                "SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token=? AND s.expires>?",
                (token, time.time()),
            ).fetchone()
        if not row:
            raise HTTPException(401, "请登录后继续")
        return dict(row)

    def admin(request):
        u = user(request)
        if u["role"] != "admin":
            raise HTTPException(403, "需要管理员权限")
        return u

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
            recent = [x for x in attempts.get(address, []) if x > time.time() - 300]
            if len(recent) >= 20:
                raise HTTPException(429, "登录尝试过多，请稍后再试")
            attempts[address] = recent + [time.time()]
        with mutation_lock, connect(database) as db:
            u = db.execute("SELECT * FROM users WHERE name=?", (body.name,)).fetchone()
            if not u or not verify(body.password, u["password"]):
                raise HTTPException(401, "账号或密码错误")
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
        response.delete_cookie("session")
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        u = user(request)
        return {k: u[k] for k in ("id", "name", "role")}

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
                s["tracks"] = [
                    {
                        "id": tr["id"],
                        "name": tr["name"],
                        "version": tr["version"],
                        "meta": json.loads(tr["meta"]),
                    }
                    for tr in db.execute(
                        "SELECT * FROM tracks WHERE sample_id=?", (s["id"],)
                    )
                ]
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
            paths = [r[0] for r in memory.execute("SELECT DISTINCT path FROM tracks")]
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
