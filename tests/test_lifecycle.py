"""账号生命周期：密码自助修改、一次性恢复凭证、停用/启用。

这些测试同时充当 mutation sanity check：
- 移除停用校验（登录/认证的 active 条件）→ test_disable_blocks_all_access 失败；
- 移除恢复单次消费 → test_recover_single_use_and_concurrency 失败；
- 恢复凭证存明文 → test_recovery_plaintext_absent 失败；
- 改密不撤销旧凭证/不轮换当前凭证 → test_change_password_with_session 失败。
"""

import io
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from workbench.app import create_app


@pytest.fixture
def env(tmp_path):
    app = create_app(tmp_path)
    admin = TestClient(app)
    assert (
        admin.post(
            "/api/setup",
            json={
                "name": "组织者",
                "password": "admin-test-only-pass",
                "setup_key": app.state.setup_file.read_text(),
            },
        ).status_code
        == 200
    )
    assert (
        admin.post(
            "/api/login", json={"name": "组织者", "password": "admin-test-only-pass"}
        ).status_code
        == 200
    )
    return app, admin, tmp_path


def csrf(admin):
    return admin.get("/api/me").json()["csrf_token"]


def secure_headers(admin):
    return {"x-csrf-token": csrf(admin)}


def own_headers(client):
    """发起请求的会话自己的 CSRF 令牌（改密等本人操作必须用它）。"""
    return {"x-csrf-token": csrf(client)}


def make_user(admin, app, name, password="user-test-only-pass"):
    assert (
        admin.post("/api/users", json={"name": name, "password": password}).status_code
        == 200
    )
    client = TestClient(app)
    assert (
        client.post("/api/login", json={"name": name, "password": password}).status_code
        == 200
    )
    user_id = client.get("/api/me").json()["id"]
    return client, user_id, password


def full_onboard(admin, app, name, secret=None):
    secret = secret or ("s" * 42 + name)
    """走自助申请→批准→领取，得到设备登录浏览器。"""
    invite = admin.post(
        "/api/invites",
        json={"purpose": "测试", "kind": "team", "expires_days": 7, "max_uses": 5},
        headers=secure_headers(admin),
    ).json()
    anon = TestClient(app, client=("10.0.0.1", 1234))
    application_id = anon.post(
        "/api/apply",
        json={
            "display_name": name,
            "invite_code": invite["code"],
            "claim_secret": secret,
        },
    ).json()["id"]
    assert (
        admin.post(
            f"/api/applications/{application_id}/approve",
            json={"role": "reviewer"},
            headers=secure_headers(admin),
        ).status_code
        == 200
    )
    browser = TestClient(app, client=("10.0.0.1", 1234))
    assert (
        browser.post(
            "/api/apply/claim",
            json={"application_id": application_id, "claim_secret": secret},
        ).status_code
        == 200
    )
    user_id = next(
        row["user_id"]
        for row in admin.get("/api/applications").json()
        if row["id"] == application_id
    )
    return browser, user_id


def test_change_password_with_session(env):
    """密码会话改密：必须核验当前密码；旧密码/旧会话/其他设备全部失效，
    当前会话轮换后仍可继续。"""
    app, admin, _ = env
    member, _member_id, old_password = make_user(admin, app, "改密员")
    # 第二个会话与第二个浏览器（无设备令牌，同账号密码会话）。
    second = TestClient(app)
    second.post("/api/login", json={"name": "改密员", "password": old_password})
    # CSRF：缺头拒绝。
    assert (
        member.post(
            "/api/me/password",
            json={"current_password": old_password, "new_password": "new-pass-123456"},
        ).status_code
        == 403
    )
    # 当前密码错误。
    wrong = member.post(
        "/api/me/password",
        json={
            "current_password": "wrong-password-1",
            "new_password": "new-pass-123456",
        },
        headers=own_headers(member),
    )
    assert wrong.status_code == 403 and "当前密码不正确" in wrong.text
    # 新密码过短。
    short = member.post(
        "/api/me/password",
        json={"current_password": old_password, "new_password": "short"},
        headers=own_headers(member),
    )
    assert short.status_code == 422
    old_session = member.cookies.get("session")
    ok = member.post(
        "/api/me/password",
        json={"current_password": old_password, "new_password": "new-pass-123456"},
        headers=own_headers(member),
    )
    assert ok.status_code == 200, ok.text
    new_session = member.cookies.get("session")
    assert new_session and new_session != old_session
    # 当前会话轮换后仍可继续；旧令牌失效。
    assert member.get("/api/me").status_code == 200
    stale = TestClient(app)
    stale.cookies.set("session", old_session)
    assert stale.get("/api/me").status_code == 401
    # 其他会话失效。
    assert second.get("/api/me").status_code == 401
    # 旧密码失效、新密码可登录。
    assert (
        member.post("/api/login", json={"name": "改密员", "password": old_password}).status_code
        == 401
    )
    fresh = TestClient(app)
    assert (
        fresh.post(
            "/api/login", json={"name": "改密员", "password": "new-pass-123456"}
        ).status_code
        == 200
    )


def test_change_password_with_device(env):
    """设备会话改密：免当前密码；当前设备令牌轮换，其他设备撤销。"""
    app, admin, _ = env
    browser, _member_id = full_onboard(admin, app, "设备改密员", secret="d" * 43)
    old_token = browser.cookies.get("device")
    # 缺 CSRF 拒绝。
    assert (
        browser.post(
            "/api/me/password", json={"new_password": "device-pass-123"}
        ).status_code
        == 403
    )
    # 设备会话无需当前密码即可设置。
    ok = browser.post(
        "/api/me/password",
        json={"new_password": "device-pass-123"},
        headers=own_headers(browser),
    )
    assert ok.status_code == 200, ok.text
    new_token = browser.cookies.get("device")
    assert new_token and new_token != old_token
    # 当前设备继续有效；旧令牌失效。
    assert browser.get("/api/me").status_code == 200
    stale = TestClient(app, client=("10.0.0.1", 1))
    stale.cookies.set("device", old_token)
    assert stale.get("/api/me").status_code == 401
    # 新密码可登录（浏览器另一会话）。
    fresh = TestClient(app)
    assert (
        fresh.post("/api/login", json={"name": "设备改密员", "password": "device-pass-123"}).status_code
        == 200
    )
    # 密码会话改密仍需当前密码（认证种类决定口径）。
    assert (
        fresh.post(
            "/api/me/password", json={"new_password": "another-pass-123"}
        ).status_code
        == 403
    )


def test_recovery_plaintext_absent(env):
    """恢复凭证明文只在生成响应出现一次；数据库/WAL/SHM、列表、备份均无明文。"""
    app, admin, folder = env
    _, member_id, _ = make_user(admin, app, "恢复员")
    created = admin.post(
        f"/api/users/{member_id}/recovery",
        json={"purpose": "浏览器丢失", "expires_hours": 24},
        headers=secure_headers(admin),
    )
    assert created.status_code == 200, created.text
    code = created.json()["code"]
    assert code.count(".") == 1

    listing = admin.get(f"/api/users/{member_id}/recovery")
    assert listing.status_code == 200
    rows = listing.json()
    assert code not in listing.text and "token_key" not in listing.text
    assert rows[0]["purpose"] == "浏览器丢失" and rows[0]["used_at"] is None

    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    blob = b""
    for suffix in ("", "-wal", "-shm"):
        path = folder / ("workbench.sqlite3" + suffix)
        if path.exists():
            blob += path.read_bytes()
    assert code.encode() not in blob
    assert code.partition(".")[2].encode() not in blob
    # 备份同样不含明文。
    backup = admin.get("/api/backup")
    assert backup.status_code == 200
    with zipfile.ZipFile(io.BytesIO(backup.content)) as z:
        inner = z.read("workbench.sqlite3")
    assert code.encode() not in inner and code.partition(".")[2].encode() not in inner


def test_recover_flow_and_negative_cases(env):
    """领取设置新密码并签发会话；错误/过期/已用/停用稳定拒绝且不产生 Cookie。"""
    app, admin, folder = env
    _, member_id, old_password = make_user(admin, app, "领恢员")
    created = admin.post(
        f"/api/users/{member_id}/recovery",
        json={"expires_hours": 24},
        headers=secure_headers(admin),
    ).json()
    browser = TestClient(app, client=("10.0.0.2", 2))
    # 错误码：403 且无 Cookie。
    bad = browser.post(
        "/api/recover", json={"recovery_code": "bad.code", "new_password": "recovered-pass-1"}
    )
    assert bad.status_code == 403
    assert not browser.cookies.get("session")
    # 正确领取。
    ok = browser.post(
        "/api/recover",
        json={"recovery_code": created["code"], "new_password": "recovered-pass-1"},
    )
    assert ok.status_code == 200, ok.text
    assert browser.cookies.get("session")
    assert browser.get("/api/me").json()["name"] == "领恢员"
    # 旧密码失效；新密码可再次登录。
    assert (
        browser.post("/api/login", json={"name": "领恢员", "password": old_password}).status_code
        == 401
    )
    again = TestClient(app)
    assert (
        again.post(
            "/api/login", json={"name": "领恢员", "password": "recovered-pass-1"}
        ).status_code
        == 200
    )
    # 重复领取同一码：409 且不再发 Cookie。
    repeat = browser.post(
        "/api/recover",
        json={"recovery_code": created["code"], "new_password": "recovered-pass-2"},
    )
    assert repeat.status_code == 409
    assert "session=" not in repeat.headers.get("set-cookie", "")
    # 已用状态在管理员列表可见。
    rows = admin.get(f"/api/users/{member_id}/recovery").json()
    assert rows[0]["used_at"] is not None

    # 过期凭证拒绝：直接插入一条已过期记录（哈希不可命中明文）。
    from workbench.db import password_hash

    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        db.execute(
            "INSERT INTO recovery_keys VALUES("
            "'expired',?,'expired-key',?,'',"
            "'2020-01-01T00:00:00+00:00',NULL,'','2020-01-01T00:00:00+00:00',NULL)",
            (member_id, password_hash("expired-secret")),
        )
    expired = TestClient(app, client=("10.0.0.3", 3))
    result = expired.post(
        "/api/recover",
        json={"recovery_code": "expired-key.anysecret", "new_password": "recovered-pass-3"},
    )
    assert result.status_code == 403
    assert not expired.cookies.get("session")


def test_recover_single_use_and_concurrency(env):
    """并发领取同一凭证：恰好一次成功，且只签发一个有效会话。"""
    app, admin, folder = env
    _, member_id, _ = make_user(admin, app, "并发领恢员")
    created = admin.post(
        f"/api/users/{member_id}/recovery",
        json={"expires_hours": 24},
        headers=secure_headers(admin),
    ).json()

    def attempt():
        client = TestClient(app, client=("10.0.0.9", 9))
        response = client.post(
            "/api/recover",
            json={"recovery_code": created["code"], "new_password": "concurrent-pass-1"},
        )
        return response.status_code, client.cookies.get("session")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: attempt(), range(4)))
    codes = sorted(code for code, _ in results)
    assert codes == [200, 403, 403, 403] or codes == [200, 403, 403, 409] or codes.count(200) == 1
    sessions = [s for _, s in results if s]
    assert len(sessions) == 1
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        used = db.execute(
            "SELECT used_at IS NOT NULL FROM recovery_keys WHERE id=?",
            (created["id"],),
        ).fetchone()[0]
    assert used == 1


def test_disable_blocks_all_access(env):
    """停用立即撤销全部凭证并拒绝所有登录路径；启用不复活旧凭证；历史证据保留。"""
    app, admin, folder = env
    member, member_id, password = make_user(admin, app, "停用员")
    device_browser, device_user = full_onboard(admin, app, "停用设备员", secret="e" * 43)
    assert device_browser.get("/api/me").status_code == 200
    # 停用前先留历史证据：评分与评论（用合成任务）。
    task_id = admin.post(
        "/api/tasks", json={"title": "停用保留任务", "mode": "development"}
    ).json()["id"]
    sample_id = admin.post(
        f"/api/tasks/{task_id}/samples", json={"name": "保留片段"}
    ).json()["id"]
    import io

    import numpy as np
    import soundfile as sf

    buffer = io.BytesIO()
    sf.write(buffer, np.zeros(1600, dtype="float32"), 16000, format="WAV")
    tracks = []
    for name in ("候选甲", "候选乙"):
        tracks.append(
            admin.post(
                f"/api/samples/{sample_id}/tracks",
                data={"name": name, "version": "v"},
                files={"file": ("x.wav", buffer.getvalue(), "audio/wav")},
            ).json()["id"]
        )
    published = admin.post(
        f"/api/tasks/{task_id}/publish",
        json={"users": [member_id, device_user], "alignment_confirmed": True},
    )
    assert published.status_code == 200, published.text
    commented = member.post(
        f"/api/samples/{sample_id}/comments",
        json={"start": 0, "end": 100, "body": "停用前的标注"},
    )
    assert commented.status_code == 200, commented.text
    rated = member.post(f"/api/samples/{sample_id}/rating", json={"choice": tracks[0]})
    assert rated.status_code == 200, rated.text
    own_view = member.get(f"/api/samples/{sample_id}").json()
    assert own_view["rating"]["choice"] == tracks[0]

    # 停用：立即生效（会话路径）。
    ok = admin.post(
        f"/api/users/{member_id}/status",
        json={"active": False},
        headers=secure_headers(admin),
    )
    assert ok.status_code == 200, ok.text
    assert member.get("/api/me").status_code == 401
    # 密码登录拒绝（与口令错误同一提示，不暴露停用状态）。
    assert (
        member.post("/api/login", json={"name": "停用员", "password": password}).status_code
        == 401
    )
    # 同 IP 无令牌不能登录；恢复凭证生成被拒。
    assert TestClient(app, client=("10.0.0.1", 1)).get("/api/me").status_code == 401
    denied = admin.post(
        f"/api/users/{member_id}/recovery",
        json={"purpose": "x"},
        headers=secure_headers(admin),
    )
    assert denied.status_code == 409 and "停用" in denied.text
    # 停用设备路径用户：在线设备下一请求即 401。
    assert (
        admin.post(
            f"/api/users/{device_user}/status",
            json={"active": False},
            headers=secure_headers(admin),
        ).status_code
        == 200
    )
    assert device_browser.get("/api/me").status_code == 401
    # 生成于停用前的凭证也无法领取。
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        from workbench.db import password_hash as ph

        db.execute(
            "INSERT INTO recovery_keys VALUES("
            "'rk-dis',?,'disabled-key',?,'停用前生成',"
            "'2099-01-01T00:00:00+00:00',NULL,'','2026-01-01T00:00:00+00:00',NULL)",
            (member_id, ph("x-secret")),
        )
    recover_attempt = TestClient(app, client=("10.0.0.4", 4))
    result = recover_attempt.post(
        "/api/recover",
        json={"recovery_code": "disabled-key.anysecret", "new_password": "revived-pass-1"},
    )
    assert result.status_code == 403

    # 历史证据保留：停用期间管理员视图仍可见评论（development 任务）。
    detail = admin.get(f"/api/samples/{sample_id}").json()
    assert [c["body"] for c in detail["comments"]] == ["停用前的标注"]
    assert admin.get(f"/api/tasks/{task_id}").json()["owner"]

    # 启用：旧会话/设备不复活；新密码登录成功，身份仍是同一 user_id。
    assert (
        admin.post(
            f"/api/users/{member_id}/status",
            json={"active": True},
            headers=secure_headers(admin),
        ).status_code
        == 200
    )
    assert (
        admin.post(
            f"/api/users/{device_user}/status",
            json={"active": True},
            headers=secure_headers(admin),
        ).status_code
        == 200
    )
    assert member.get("/api/me").status_code == 401  # 旧会话不复活
    assert device_browser.get("/api/me").status_code == 401  # 旧设备不复活
    relogin = TestClient(app)
    assert (
        relogin.post("/api/login", json={"name": "停用员", "password": password}).status_code
        == 200
    )
    assert relogin.get("/api/me").json()["id"] == member_id
    # 停用期间的历史证据仍可访问：评论与本人的评分原样保留。
    relogin_view = relogin.get(f"/api/samples/{sample_id}").json()
    assert [c["body"] for c in relogin_view["comments"]] == ["停用前的标注"]
    assert relogin_view["rating"]["choice"] == tracks[0]


def test_admin_protections(env):
    """管理员账号不可停用；不能停用自己；停用幂等且无额外副作用。"""
    app, admin, folder = env
    admin_id = admin.get("/api/me").json()["id"]
    # 停用管理员（唯一可用管理员）→ 409。
    assert (
        admin.post(
            f"/api/users/{admin_id}/status",
            json={"active": False},
            headers=secure_headers(admin),
        ).status_code
        == 409
    )
    # 第二管理员同样受保护。
    admin.post(
        "/api/users",
        json={"name": "副管理员", "password": "second-admin-pass", "role": "admin"},
    )
    second_id = next(
        u["id"] for u in admin.get("/api/users").json() if u["name"] == "副管理员"
    )
    assert (
        admin.post(
            f"/api/users/{second_id}/status",
            json={"active": False},
            headers=secure_headers(admin),
        ).status_code
        == 409
    )
    # 不存在账号 → 404；重复停用幂等 200。
    _member, member_id, _ = make_user(admin, app, "幂等停用员")
    # 两次停用都精确 200；第二次无额外副作用（active 仍 0，
    # 会话与设备集合不再变化）。
    responses = [
        admin.post(
            f"/api/users/{member_id}/status",
            json={"active": False},
            headers=secure_headers(admin),
        )
        for _ in range(2)
    ]
    assert [r.status_code for r in responses] == [200, 200]
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        active = db.execute("SELECT active FROM users WHERE id=?", (member_id,)).fetchone()[0]
        sessions = db.execute(
            "SELECT count(*) FROM sessions WHERE user_id=?", (member_id,)
        ).fetchone()[0]
        devices = db.execute(
            "SELECT count(*) FROM devices WHERE user_id=? AND revoked=0",
            (member_id,),
        ).fetchone()[0]
    assert active == 0 and sessions == 0 and devices == 0
    # 404。
    assert (
        admin.post(
            "/api/users/does-not-exist/status",
            json={"active": False},
            headers=secure_headers(admin),
        ).status_code
        == 404
    )
    # 成员列表带 active 字段。
    users = admin.get("/api/users").json()
    entry = next(u for u in users if u["id"] == member_id)
    assert entry["active"] == 0 and next(u for u in users if u["id"] == admin_id)["active"] == 1


def test_permissions_matrix_and_rate_limit(env):
    """匿名/评测者/组织者访问生命周期写端点被拒；recover 限流。"""
    app, admin, _ = env
    reviewer, _, _ = make_user(admin, app, "矩阵成员")
    admin.post(
        "/api/users",
        json={"name": "矩阵组织者", "password": "org-test-only-pass", "role": "organizer"},
    )
    organizer = TestClient(app)
    organizer.post("/api/login", json={"name": "矩阵组织者", "password": "org-test-only-pass"})
    admin_id = admin.get("/api/me").json()["id"]
    member_id = reviewer.get("/api/me").json()["id"]
    cases = [
        ("post", f"/api/users/{member_id}/recovery", {"purpose": "x"}),
        ("post", f"/api/users/{member_id}/status", {"active": False}),
        ("post", f"/api/users/{admin_id}/recovery", {"purpose": "x"}),
        ("post", f"/api/users/{admin_id}/status", {"active": False}),
    ]
    for method, path, body in cases:
        for client, expected in ((TestClient(app), 401), (reviewer, 403), (organizer, 403)):
            response = getattr(client, method)(path, json=body, headers=secure_headers(admin))
            assert response.status_code == expected, (path, expected, response.text)
    # 改密端点：匿名 401；有 CSRF 才放行（reviewer 自己的会话）。
    assert (
        TestClient(app)
        .post("/api/me/password", json={"new_password": "whatever-pass-1"})
        .status_code
        == 401
    )
    # recover 限流：同 IP 10 次后 429。
    heavy = TestClient(app, client=("10.9.9.9", 9))
    codes = [
        heavy.post(
            "/api/recover",
            json={"recovery_code": "dead.beefdeadbeef", "new_password": "whatever-pass-1"},
        ).status_code
        for _ in range(11)
    ]
    assert codes[-1] == 429


def test_migration_and_backup_preserve_state(env, tmp_path):
    """旧库升级自动加 active 列；双 init 一致；备份恢复保留停用与未用恢复哈希。"""
    app, admin, folder = env
    _member, member_id, _ = make_user(admin, app, "迁移员")
    admin.post(
        f"/api/users/{member_id}/status",
        json={"active": False},
        headers=secure_headers(admin),
    )
    _keeper, keeper_id, _ = make_user(admin, app, "凭证持有人")
    created = admin.post(
        f"/api/users/{keeper_id}/recovery",
        json={"purpose": "升级前", "expires_hours": 72},
        headers=secure_headers(admin),
    ).json()
    assert "code" in created, created
    # 模拟 v0.7.0 旧库：移除 active 列（SQLite 重建表）。
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            "CREATE TABLE users_old(id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO users_old SELECT id,name,password,role FROM users"
        )
        db.execute("DROP TABLE users")
        db.execute("ALTER TABLE users_old RENAME TO users")
    restarted = create_app(folder)
    c = TestClient(restarted)
    assert (
        c.post(
            "/api/login", json={"name": "组织者", "password": "admin-test-only-pass"}
        ).status_code
        == 200
    )
    # 列迁移语义：旧库升级后所有账号默认可用（停用状态不跨列迁移保留）。
    users = c.get("/api/users").json()
    assert next(u for u in users if u["id"] == member_id)["active"] == 1
    # 重新停用，验证备份路径对停用状态与未用凭证的保留。
    assert (
        c.post(
            f"/api/users/{member_id}/status",
            json={"active": False},
            headers=secure_headers(c),
        ).status_code
        == 200
    )
    # 双 init 一致。
    from workbench.db import init as db_init

    snapshot = _snapshot(folder / "workbench.sqlite3")
    db_init(folder / "workbench.sqlite3")
    db_init(folder / "workbench.sqlite3")
    assert _snapshot(folder / "workbench.sqlite3") == snapshot
    # 备份/恢复：停用状态与未用恢复哈希保持；session 清除；无明文。
    backup = c.get("/api/backup")
    assert backup.status_code == 200
    destination = tmp_path / "恢复目录"
    with zipfile.ZipFile(io.BytesIO(backup.content)) as z:
        blob = z.read("workbench.sqlite3")
        assert created["code"].encode() not in blob
        z.extractall(destination)
    restored = TestClient(create_app(destination))
    assert (
        restored.post(
            "/api/login", json={"name": "组织者", "password": "admin-test-only-pass"}
        ).status_code
        == 200
    )
    users_after = restored.get("/api/users").json()
    assert next(u for u in users_after if u["id"] == member_id)["active"] == 0
    # 领取备份里的未用凭证：仍可用（哈希随备份）；停用状态同时保留。
    recover_client = TestClient(create_app(destination), client=("10.0.0.7", 7))
    ok = recover_client.post(
        "/api/recover",
        json={"recovery_code": created["code"], "new_password": "restored-pass-1"},
    )
    assert ok.status_code == 200
    assert recover_client.get("/api/me").json()["id"] == keeper_id
    users_after = restored.get("/api/users").json()
    assert next(u for u in users_after if u["id"] == member_id)["active"] == 0


def _snapshot(path):
    with sqlite3.connect(path) as db:
        schema = db.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        tables = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        data = {
            t: db.execute(f"SELECT * FROM {t}").fetchall()
            for t in tables
            if t != "sqlite_sequence"
        }
    return schema, data


def test_admin_reset_revokes_devices_and_requires_csrf(env):
    """管理员重置纳入统一边界：CSRF 必须、会话与设备一起作废、
    停用账号拒绝重置、历史数据不变。"""
    import hashlib

    app, admin, folder = env
    member, member_id, old_password = make_user(admin, app, "重置处置员")
    # 历史证据：先发一条评论。
    task_id = admin.post(
        "/api/tasks", json={"title": "重置保留任务", "mode": "development"}
    ).json()["id"]
    sample_id = admin.post(f"/api/tasks/{task_id}/samples", json={"name": "片段"}).json()["id"]
    buffer = io.BytesIO()
    import numpy as np
    import soundfile as sf

    buffer = io.BytesIO()
    sf.write(buffer, np.zeros(1600, dtype="float32"), 16000, format="WAV")
    tracks = []
    for name in ("候选甲", "候选乙"):
        tracks.append(
            admin.post(
                f"/api/samples/{sample_id}/tracks",
                data={"name": name, "version": "v"},
                files={"file": ("x.wav", buffer.getvalue(), "audio/wav")},
            ).json()["id"]
        )
    published = admin.post(
        f"/api/tasks/{task_id}/publish",
        json={"users": [member_id], "alignment_confirmed": True},
    )
    assert published.status_code == 200, published.text
    commented = member.post(
        f"/api/samples/{sample_id}/comments",
        json={"start": 0, "end": 100, "body": "重置前的标注"},
    )
    assert commented.status_code == 200, commented.text
    rated = member.post(f"/api/samples/{sample_id}/rating", json={"choice": tracks[0]})
    assert rated.status_code == 200, rated.text

    # 缺 CSRF / 错 CSRF：403，且凭证不受影响。
    missing = admin.post(
        f"/api/users/{member_id}/password",
        json={"password": "brand-new-pass-1", "confirm_name": "重置处置员"},
    )
    assert missing.status_code == 403
    wrong = admin.post(
        f"/api/users/{member_id}/password",
        json={"password": "brand-new-pass-1", "confirm_name": "重置处置员"},
        headers={"x-csrf-token": "0" * 64},
    )
    assert wrong.status_code == 403
    assert member.get("/api/me").status_code == 200

    # 停用账号：重置被拒，需先启用。
    admin.post(
        f"/api/users/{member_id}/status",
        json={"active": False},
        headers=secure_headers(admin),
    )
    disabled_reset = admin.post(
        f"/api/users/{member_id}/password",
        json={"password": "brand-new-pass-1", "confirm_name": "重置处置员"},
        headers=secure_headers(admin),
    )
    assert disabled_reset.status_code == 409 and "先启用" in disabled_reset.text
    admin.post(
        f"/api/users/{member_id}/status",
        json={"active": True},
        headers=secure_headers(admin),
    )

    # 启用后（此时无任何有效凭证）再挂一个设备 Cookie（模拟失窃的第二台设备），
    # 使“重置是否撤销设备”可被独立观测。
    device_browser = TestClient(app, client=("10.0.0.5", 5))
    device_token = "stolen-device-token-xyz"
    device_browser.cookies.set("device", device_token)
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        db.execute(
            "INSERT INTO devices VALUES("
            "'dev-stolen',?,?,'2026-01-01T00:00:00+00:00',0,NULL,"
            "9999999999999,0,'10.0.0.5','10.0.0.5','未知客户端')",
            (member_id, hashlib.sha256(device_token.encode()).hexdigest()),
        )
    assert device_browser.get("/api/me").status_code == 200

    # 正常重置：确认名称错误 422；正确后 200。
    wrong_name = admin.post(
        f"/api/users/{member_id}/password",
        json={"password": "brand-new-pass-1", "confirm_name": "错误名称"},
        headers=secure_headers(admin),
    )
    assert wrong_name.status_code == 422
    ok = admin.post(
        f"/api/users/{member_id}/password",
        json={"password": "brand-new-pass-1", "confirm_name": "重置处置员"},
        headers=secure_headers(admin),
    )
    assert ok.status_code == 200, ok.text

    # 旧密码会话、旧设备 Cookie、旧密码全部失效；新随机密码可登录。
    assert member.get("/api/me").status_code == 401
    assert device_browser.get("/api/me").status_code == 401
    assert (
        member.post("/api/login", json={"name": "重置处置员", "password": old_password}).status_code
        == 401
    )
    relogin = TestClient(app)
    assert (
        relogin.post(
            "/api/login", json={"name": "重置处置员", "password": "brand-new-pass-1"}
        ).status_code
        == 200
    )
    # 历史数据不变。
    detail = relogin.get(f"/api/samples/{sample_id}").json()
    assert [c["body"] for c in detail["comments"]] == ["重置前的标注"]
    assert detail["rating"]["choice"] == tracks[0]
