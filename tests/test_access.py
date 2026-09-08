"""自助申请与免密设备登录：状态机、哈希边界、并发批准、限流与撤销。

这些测试同时充当 mutation sanity check：
- 去掉凭证/秘密/令牌的哈希存储 → test_plaintexts_never_stored_or_listed 失败；
- 去掉撤销检查或改为 IP 认证 → test_device_login_ip_change_revoke_and_logout 失败；
- 移除邀请次数的 SQL 上限约束 → test_concurrent_approval_respects_single_use 失败；
- 去掉领取秘密校验 → test_claim_requires_approval_and_exact_secret 失败。
"""

import io
import json
import sqlite3
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from workbench.app import create_app
from workbench.db import password_hash


@pytest.fixture
def env(tmp_path):
    app = create_app(tmp_path)
    admin = TestClient(app)
    assert (
        admin.post(
            "/api/setup",
            json={
                "name": "组织者",
                "password": "test-only-strong-pass",
                "setup_key": app.state.setup_file.read_text(),
            },
        ).status_code
        == 200
    )
    assert (
        admin.post(
            "/api/login", json={"name": "组织者", "password": "test-only-strong-pass"}
        ).status_code
        == 200
    )
    return app, admin, tmp_path


def csrf(admin):
    return admin.get("/api/me").json()["csrf_token"]


def secure_headers(admin):
    return {"x-csrf-token": csrf(admin)}


def make_invite(admin, **overrides):
    body = {
        "purpose": "第一轮评测",
        "kind": "team",
        "expires_days": 7,
        "max_uses": 5,
        **overrides,
    }
    response = admin.post(
        "/api/invites", json=body, headers=secure_headers(admin)
    )
    assert response.status_code == 200, response.text
    return response.json()


def apply_user(app, code, secret, name, ip="10.0.0.1", ua=""):
    """匿名申请：client 参数模拟不同来源 IP，ua 模拟不同浏览器。"""
    client = TestClient(app, client=(ip, 1234))
    response = client.post(
        "/api/apply",
        json={
            "display_name": name,
            "employee_id": "G-77",
            "email": "applicant@example.com",
            "invite_code": code,
            "claim_secret": secret,
        },
        headers={"user-agent": ua} if ua else {},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def approve(admin, application_id, **overrides):
    body = {"role": "reviewer", **overrides}
    return admin.post(
        f"/api/applications/{application_id}/approve",
        json=body,
        headers=secure_headers(admin),
    )


def full_onboard(admin, app, name, ip="10.0.0.1", ua="", secret=None):
    """申请 → 批准 → 领取，返回（领取后的浏览器、申请编号、设备令牌）。"""
    invite = make_invite(admin)
    secret = secret or ("s" * 42 + name)
    application_id = apply_user(app, invite["code"], secret, name, ip=ip, ua=ua)
    assert approve(admin, application_id).status_code == 200
    browser = TestClient(app, client=(ip, 1234))
    response = browser.post(
        "/api/apply/claim",
        json={"application_id": application_id, "claim_secret": secret},
        headers={"user-agent": ua} if ua else {},
    )
    assert response.status_code == 200, response.text
    return browser, application_id, browser.cookies.get("device")


def active_task_with_tracks(admin, title):
    """建一个可发布的 active 任务并返回 (task_id, sample_id)。"""
    task_id = admin.post("/api/tasks", json={"title": title}).json()["id"]
    sample_id = admin.post(
        f"/api/tasks/{task_id}/samples", json={"name": title + "片段"}
    ).json()["id"]
    import numpy as np
    import soundfile as sf

    buffer = io.BytesIO()
    sf.write(buffer, np.zeros(1600, dtype="float32"), 16000, format="WAV")
    for version in ("候选甲", "候选乙"):
        assert (
            admin.post(
                f"/api/samples/{sample_id}/tracks",
                data={"name": version, "version": "v"},
                files={"file": ("x.wav", buffer.getvalue(), "audio/wav")},
            ).status_code
            == 200
        )
    response = admin.post(
        f"/api/tasks/{task_id}/publish", json={"alignment_confirmed": True}
    )
    assert response.status_code == 200, response.text
    return task_id, sample_id


def test_database_bytes_hold_no_plaintext(env):
    """明文邀请、领取秘密、设备令牌在数据库与 API 列表中都不存在；正确值仍可校验。"""
    app, admin, folder = env
    invite = make_invite(admin)
    secret = "k" * 43
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
    application_id = apply_user(
        app, invite["code"], secret, "哈希边界员", ua=ua
    )
    assert approve(admin, application_id).status_code == 200
    browser = TestClient(app, client=("10.0.0.1", 1))
    claimed = browser.post(
        "/api/apply/claim",
        json={"application_id": application_id, "claim_secret": secret},
        headers={"user-agent": ua},
    )
    assert claimed.status_code == 200, claimed.text
    device_token = browser.cookies.get("device")

    # WAL 尚未合并的写入也要纳入字节搜索。
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    blob = b""
    for suffix in ("", "-wal", "-shm"):
        path = folder / ("workbench.sqlite3" + suffix)
        if path.exists():
            blob += path.read_bytes()
    assert invite["code"].encode() not in blob
    # 机密片段也不能以原文落库（独立盐 scrypt 后不可逆向）。
    assert invite["code"].partition(".")[2].encode() not in blob
    assert secret.encode() not in blob
    assert device_token.encode() not in blob

    listing = admin.get("/api/invites")
    assert listing.status_code == 200
    assert invite["code"] not in listing.text
    assert "token_key" not in listing.text and "token_hash" not in listing.text
    applications = admin.get("/api/applications")
    assert applications.status_code == 200
    assert secret not in applications.text
    devices = admin.get(f"/api/users/{admin.get('/api/me').json()['id']}/devices")
    assert devices.status_code == 200
    # 新用户的设备列表同样不含令牌明文
    user_id = admin.get("/api/applications").json()[0]["user_id"]
    devices = admin.get(f"/api/users/{user_id}/devices")
    assert devices.status_code == 200
    assert device_token not in devices.text

    # 正确值仍可校验：邀请可再次申请、秘密可查状态、令牌可登录。
    assert (
        TestClient(app, client=("10.0.0.2", 2))
        .post(
            "/api/apply",
            json={
                "display_name": "第二人",
                "invite_code": invite["code"],
                "claim_secret": "j" * 43,
            },
        )
        .status_code
        == 200
    )
    status = browser.post(
        "/api/apply/status",
        json={"application_id": application_id, "claim_secret": secret},
    )
    assert status.json() == {"id": application_id, "status": "approved"}
    assert browser.get("/api/me").status_code == 200


def test_duplicate_apply_returns_stable_application(env):
    app, admin, _ = env
    invite = make_invite(admin)
    secret = "d" * 43
    first = apply_user(app, invite["code"], secret, "重复申请人")
    second = apply_user(app, invite["code"], secret, "重复申请人")
    assert first == second
    with sqlite3.connect(app.state.database) as db:
        assert db.execute("SELECT count(*) FROM applications").fetchone()[0] == 1
    third = apply_user(app, invite["code"], "e" * 43, "重复申请人")
    assert third != first


def test_concurrent_approval_respects_single_use(env):
    app, admin, folder = env
    invite = make_invite(admin, max_uses=1)
    ids = [
        apply_user(app, invite["code"], "f" * 42 + str(i), f"并发{i}")
        for i in range(2)
    ]
    headers = secure_headers(admin)

    def approve_one(application_id):
        return admin.post(
            f"/api/applications/{application_id}/approve",
            json={"role": "reviewer"},
            headers=headers,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve_one, ids))
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], [r.text for r in results]
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        used, users = db.execute(
            "SELECT (SELECT used_count FROM invites WHERE max_uses=1),"
            " (SELECT count(*) FROM users)"
        ).fetchone()
    assert used == 1 and users == 2  # 管理员 + 恰好一个获批身份


def test_claim_requires_approval_and_exact_secret(env):
    app, admin, _ = env
    invite = make_invite(admin)
    secret = "c" * 43
    application_id = apply_user(app, invite["code"], secret, "待批人")
    claim = lambda client, sid, cid: client.post(
        "/api/apply/claim", json={"application_id": cid, "claim_secret": sid}
    )
    browser = TestClient(app, client=("10.0.0.1", 1))
    # 未批准不能领取 Cookie。
    assert claim(browser, secret, application_id).status_code == 409
    assert not browser.cookies.get("device")
    approve(admin, application_id)
    # 猜申请编号、错领取秘密、缺秘密都不能登录。
    assert claim(browser, secret, "0" * 32).status_code == 403
    assert claim(browser, "w" * 43, application_id).status_code == 403
    response = browser.post(
        "/api/apply/claim", json={"application_id": application_id}
    )
    assert response.status_code == 422  # pydantic 长度校验
    ok = claim(browser, secret, application_id)
    assert ok.status_code == 200
    assert browser.get("/api/me").status_code == 200


def test_claim_retry_issues_single_device_and_rotates_token(env):
    app, admin, folder = env
    browser, application_id, first_token = full_onboard(
        admin, app, "重试员", secret="r" * 43
    )
    retry = browser.post(
        "/api/apply/claim",
        json={"application_id": application_id, "claim_secret": "r" * 43},
    )
    assert retry.status_code == 200, retry.text
    second_token = browser.cookies.get("device")
    assert second_token and second_token != first_token
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        rows = db.execute("SELECT count(*) FROM devices").fetchone()[0]
        valid = db.execute(
            "SELECT count(*) FROM devices WHERE revoked=0"
        ).fetchone()[0]
    assert rows == 1 and valid == 1
    old = TestClient(app, client=("10.0.0.1", 1))
    old.cookies.set("device", first_token)
    assert old.get("/api/me").status_code == 401  # 旧令牌立即失效
    assert browser.get("/api/me").status_code == 200
    user_id = admin.get("/api/applications").json()[0]["user_id"]
    assert len(admin.get(f"/api/users/{user_id}/devices").json()) == 1


def test_claim_retry_fixed_window_and_expiry(env):
    """重试只轮换令牌：claimed_at 与绝对 expires 不动；窗口/过期判定用首次值。"""
    app, admin, folder = env
    browser, application_id, first_token = full_onboard(admin, app, "固窗员", secret="6" * 43)
    retry_claim = lambda: browser.post(
        "/api/apply/claim",
        json={"application_id": application_id, "claim_secret": "6" * 43},
    )
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        first_claimed, first_expires = db.execute(
            "SELECT claimed_at, expires FROM devices"
        ).fetchone()

    # 第 899 秒重试：成功，但窗口起点与绝对到期保持人为设置的值（不再后移）。
    mutated = first_claimed - 899
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        db.execute("UPDATE devices SET claimed_at=?", (mutated,))
    near_edge = retry_claim()
    assert near_edge.status_code == 200, near_edge.text
    second_token = browser.cookies.get("device")
    assert second_token and second_token != first_token
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        claimed, expires = db.execute(
            "SELECT claimed_at, expires FROM devices"
        ).fetchone()
    assert abs(claimed - mutated) < 0.01 and expires == first_expires
    old = TestClient(app, client=("10.0.0.1", 1))
    old.cookies.set("device", first_token)
    assert old.get("/api/me").status_code == 401
    assert browser.get("/api/me").status_code == 200

    # 再次重试（仍在窗口内）：同样不得移动窗口/到期。
    assert retry_claim().status_code == 200
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        claimed, expires = db.execute(
            "SELECT claimed_at, expires FROM devices"
        ).fetchone()
    assert abs(claimed - mutated) < 0.01 and expires == first_expires

    # 超过原始窗口（距首次领取 >901 秒）：稳定 409。
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        db.execute("UPDATE devices SET claimed_at=?", (time.time() - 901,))
    late = retry_claim()
    assert late.status_code == 409
    assert "set-cookie" not in {k.lower() for k in late.headers}

    # 绝对到期后：即使窗口未过也 409，且不延长。
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        db.execute(
            "UPDATE devices SET claimed_at=?, expires=?",
            (time.time() - 10, time.time() - 1),
        )
    expired = retry_claim()
    assert expired.status_code == 409
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        assert db.execute("SELECT expires FROM devices").fetchone()[0] < time.time()


def test_claim_revoked_retry_no_cookie(env):
    """撤销后窗口内重试：409 且不写 device Cookie。"""
    app, admin, _ = env
    browser, application_id, _ = full_onboard(admin, app, "撤重员", secret="7" * 43)
    user_id = next(
        row["user_id"]
        for row in admin.get("/api/applications").json()
        if row["display_name"] == "撤重员"
    )
    devices = admin.get(f"/api/users/{user_id}/devices").json()
    admin.delete(f"/api/devices/{devices[0]['id']}", headers=secure_headers(admin))
    response = browser.post(
        "/api/apply/claim",
        json={"application_id": application_id, "claim_secret": "7" * 43},
    )
    assert response.status_code == 409
    assert "device=" not in response.headers.get("set-cookie", "")
    assert browser.get("/api/me").status_code == 401


def test_claim_window_closes_after_grace(env):
    """窗口关闭后（距首次领取超过 15 分钟）重试稳定 409。"""
    app, admin, folder = env
    browser, application_id, _ = full_onboard(admin, app, "过窗员", secret="g" * 43)
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        db.execute("UPDATE devices SET claimed_at=claimed_at-3600")
    late = browser.post(
        "/api/apply/claim",
        json={"application_id": application_id, "claim_secret": "g" * 43},
    )
    assert late.status_code == 409
    assert "device=" not in late.headers.get("set-cookie", "")


def test_approval_respects_task_member_freeze(env):
    """审批时重查任务状态：closed 冻结（两类邀请），回收站拒绝，draft 可分配。"""
    app, admin, folder = env

    def new_application(name, secret):
        invite = make_invite(admin)
        return invite, apply_user(app, invite["code"], secret, name)

    # 场景 A：task invite + 审批前任务被关闭 → 稳定 409，无任何副作用。
    task_id, _ = active_task_with_tracks(admin, "将被关闭的受邀任务")
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        members_before = set(db.execute("SELECT task_id,user_id FROM members").fetchall())
        assigned_before = set(
            db.execute("SELECT task_id,user_id FROM review_assignments").fetchall()
        )
    task_invite = make_invite(admin, kind="task", task_id=task_id)
    app_a = apply_user(app, task_invite["code"], "8" * 43, "关闭前申请人甲")
    assert admin.post(f"/api/tasks/{task_id}/close").status_code == 200
    closed = approve(admin, app_a, task_ids=[task_id])
    assert closed.status_code == 409 and "冻结" in closed.text
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        used = db.execute("SELECT used_count FROM invites WHERE id=?", (task_invite["id"],)).fetchone()[0]
        users = db.execute("SELECT count(*) FROM users").fetchone()[0]
        members_after = set(db.execute("SELECT task_id,user_id FROM members").fetchall())
        assigned_after = set(
            db.execute("SELECT task_id,user_id FROM review_assignments").fetchall()
        )
        status_a = db.execute("SELECT status FROM applications WHERE id=?", (app_a,)).fetchone()[0]
    assert used == 0 and users == 1 and status_a == "pending"
    assert members_after == members_before and assigned_after == assigned_before

    # 场景 B：team invite + 审批前任务被关闭 → 409，无副作用。
    task_b, _ = active_task_with_tracks(admin, "审批前关闭的团队任务")
    team_invite, app_b = new_application("关闭前申请人乙", "9" * 43)
    assert admin.post(f"/api/tasks/{task_b}/close").status_code == 200
    closed_b = approve(admin, app_b, task_ids=[task_b])
    assert closed_b.status_code == 409 and "冻结" in closed_b.text
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        used_b = db.execute("SELECT used_count FROM invites WHERE id=?", (team_invite["id"],)).fetchone()[0]
        status_b = db.execute("SELECT status FROM applications WHERE id=?", (app_b,)).fetchone()[0]
    assert used_b == 0 and status_b == "pending"

    # 场景 C：draft 任务可分配（与 members 接口语义一致），发布前成员不可见。
    draft = admin.post("/api/tasks", json={"title": "草稿也可受邀"}).json()["id"]
    _, app_c = new_application("草稿受令人", "a" * 43)
    ok = approve(admin, app_c, task_ids=[draft])
    assert ok.status_code == 200, ok.text
    draft_user = next(
        row["user_id"]
        for row in admin.get("/api/applications").json()
        if row["id"] == app_c
    )
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        assert (draft, draft_user) in {
            tuple(r) for r in db.execute("SELECT task_id,user_id FROM members").fetchall()
        }
        assert (draft, draft_user) in {
            tuple(r)
            for r in db.execute("SELECT task_id,user_id FROM review_assignments").fetchall()
        }

    # 场景 D：任务移入回收站后批准 → 404。
    task_d, _ = active_task_with_tracks(admin, "将被回收的受邀任务")
    task_invite_d = make_invite(admin, kind="task", task_id=task_d)
    app_d = apply_user(app, task_invite_d["code"], "b" * 43, "回收前申请人")
    admin.request("DELETE", f"/api/tasks/{task_d}", json={"title": "将被回收的受邀任务"})
    trashed = approve(admin, app_d, task_ids=[task_d])
    assert trashed.status_code == 404
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        used_d = db.execute("SELECT used_count FROM invites WHERE id=?", (task_invite_d["id"],)).fetchone()[0]
    assert used_d == 0


def test_device_login_ip_change_revoke_and_logout(env):
    app, admin, folder = env
    browser, application_id, token = full_onboard(
        admin, app, "多网员", ip="10.0.0.1", ua="Mozilla/5.0 Chrome/120.0.0.0"
    )
    assert browser.get("/api/me").status_code == 200
    user_id = admin.get("/api/me").json()["id"]  # 需要区分：这里取申请获批用户的 ID
    user_id = next(
        row["user_id"]
        for row in admin.get("/api/applications").json()
        if row["id"] == application_id
    )

    # 换 IP：同一设备令牌仍登录，审计更新为最近 IP。
    elsewhere = TestClient(app, client=("10.0.0.66", 9))
    elsewhere.cookies.set("device", token)
    assert elsewhere.get("/api/me").json()["name"] == "多网员"
    devices = admin.get(f"/api/users/{user_id}/devices").json()
    assert devices[0]["first_ip"] == "10.0.0.1"
    assert devices[0]["last_ip"] == "10.0.0.66"
    assert devices[0]["device"] == "Chrome 120"

    # 相同 IP、无令牌不能登录；伪造 IP 也不能顶替。
    assert TestClient(app, client=("10.0.0.1", 9)).get("/api/me").status_code == 401
    assert TestClient(app, client=("10.0.0.66", 9)).get("/api/me").status_code == 401

    # 撤销单设备：下一请求立即失效。
    assert (
        admin.delete(
            f"/api/devices/{devices[0]['id']}", headers=secure_headers(admin)
        ).status_code
        == 200
    )
    assert elsewhere.get("/api/me").status_code == 401
    assert (
        admin.delete(
            f"/api/devices/{devices[0]['id']}", headers=secure_headers(admin)
        ).status_code
        == 200  # 幂等
    )

    # 重新走一遍并测试撤销全部设备（含手工插入的第二设备）。
    browser2, application2, token2 = full_onboard(admin, app, "重登员", ip="10.0.0.2")
    user2 = next(
        row["user_id"]
        for row in admin.get("/api/applications").json()
        if row["id"] == application2
    )
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        db.execute(
            "INSERT INTO devices VALUES('extra',?,"
            "'deadbeef','2026-01-01T00:00:00+00:00',0,NULL,9999999999999,0,'10.9.9.9','10.9.9.9','未知客户端')",
            (user2,),
        )
    assert (
        admin.delete(
            f"/api/users/{user2}/devices", headers=secure_headers(admin)
        ).status_code
        == 200
    )
    assert browser2.get("/api/me").status_code == 401
    fresh = TestClient(app, client=("10.0.0.2", 3))
    fresh.cookies.set("device", token2)
    assert fresh.get("/api/me").status_code == 401

    # 退出登录撤销本浏览器设备，刷新后不会免密重新进入。
    browser3, _, token3 = full_onboard(admin, app, "退出员", secret="l" * 43)
    assert browser3.post("/api/logout").status_code == 200
    assert browser3.get("/api/me").status_code == 401
    residual = TestClient(app, client=("10.0.0.1", 1))
    residual.cookies.set("device", token3)
    assert residual.get("/api/me").status_code == 401


def test_login_rotates_session_and_password_login_still_works(env):
    app, admin, _ = env
    first_token = admin.cookies.get("session")
    assert admin.get("/api/me").status_code == 200
    # 同一浏览器再次登录：旧会话被轮换作废。
    assert (
        admin.post(
            "/api/login", json={"name": "组织者", "password": "test-only-strong-pass"}
        ).status_code
        == 200
    )
    second_token = admin.cookies.get("session")
    assert second_token != first_token
    old = TestClient(app)
    old.cookies.set("session", first_token)
    assert old.get("/api/me").status_code == 401
    assert admin.get("/api/me").status_code == 200


def test_access_matrix_and_no_identity_leak(env):
    app, admin, _ = env
    invite = make_invite(admin)
    secret = "m" * 43
    apply_user(app, invite["code"], secret, "保密申请人甲")
    admin.post(
        "/api/users", json={"name": "普通成员", "password": "member-test-only"}
    )
    reviewer = TestClient(app)
    assert (
        reviewer.post(
            "/api/login", json={"name": "普通成员", "password": "member-test-only"}
        ).status_code
        == 200
    )
    organizer = TestClient(app)
    admin.post(
        "/api/users",
        json={"name": "组织者同事", "password": "organizer-test-only", "role": "organizer"},
    )
    assert (
        organizer.post(
            "/api/login",
            json={"name": "组织者同事", "password": "organizer-test-only"},
        ).status_code
        == 200
    )
    admin_id = admin.get("/api/me").json()["id"]
    cases = [
        ("get", "/api/invites", None),
        ("post", "/api/invites", {"purpose": "x"}),
        ("patch", "/api/invites/x", {"active": False}),
        ("get", "/api/applications", None),
        ("post", "/api/applications/x/approve", {"role": "reviewer"}),
        ("post", "/api/applications/x/reject", None),
        ("get", f"/api/users/{admin_id}/devices", None),
        ("delete", "/api/devices/x", None),
        ("delete", f"/api/users/{admin_id}/devices", None),
    ]
    for method, path, body in cases:
        for client, expected in (
            (TestClient(app), 401),
            (reviewer, 403),
            (organizer, 403),
        ):
            kwargs = {"headers": secure_headers(admin)} if body else {}
            if body:
                kwargs["json"] = body
            response = getattr(client, method)(path, **kwargs)
            assert response.status_code == expected, (path, expected, response.text)
            if expected in (401, 403):
                assert "保密申请人甲" not in response.text
    # 管理员自己可以访问。
    assert admin.get("/api/invites").status_code == 200
    assert admin.get("/api/applications").json()[0]["display_name"] == "保密申请人甲"


def test_sensitive_ops_require_csrf_token(env):
    app, admin, _ = env
    assert (
        admin.post("/api/invites", json={"purpose": "无令牌"}).status_code == 403
    )
    assert (
        admin.post(
            "/api/invites",
            json={"purpose": "假令牌"},
            headers={"x-csrf-token": "0" * 64},
        ).status_code
        == 403
    )
    assert make_invite(admin)["code"]
    invite = make_invite(admin)
    application_id = apply_user(app, invite["code"], "n" * 43, "无令牌员")
    route = f"/api/applications/{application_id}/approve"
    assert admin.post(route, json={"role": "reviewer"}).status_code == 403
    assert approve(admin, application_id).status_code == 200
    assert (
        admin.post(
            f"/api/applications/{application_id}/reject",
            headers=secure_headers(admin),
        ).status_code
        == 409  # 已批准，拒绝不能反转终态
    )


def test_form_encoded_writes_rejected(env):
    _, admin, _ = env
    response = admin.post(
        "/api/invites", data={"purpose": "表单"}, headers=secure_headers(admin)
    )
    assert response.status_code == 415


def test_task_invite_cannot_approve_to_other_task(env):
    app, admin, _ = env
    task_a, _ = active_task_with_tracks(admin, "受邀任务甲")
    task_b, _ = active_task_with_tracks(admin, "无关任务乙")
    invite = make_invite(admin, kind="task", task_id=task_a)
    application_id = apply_user(app, invite["code"], "o" * 43, "任务受令人")
    for wrong in ([task_b], [task_a, task_b], []):
        response = approve(admin, application_id, task_ids=wrong)
        assert response.status_code == 422, wrong
    assert approve(admin, application_id, task_ids=[task_a]).status_code == 200
    user_id = admin.get("/api/applications").json()[0]["user_id"]
    with sqlite3.connect(app.state.database) as db:
        members = {
            tuple(row)
            for row in db.execute("SELECT task_id,user_id FROM members").fetchall()
        }
        assigned = {
            tuple(row)
            for row in db.execute(
                "SELECT task_id,user_id FROM review_assignments"
            ).fetchall()
        }
    assert (task_a, user_id) in members and (task_a, user_id) in assigned
    assert (task_b, user_id) not in members | assigned


def test_team_invite_admin_selects_tasks_and_retry_idempotent(env):
    app, admin, _ = env
    task_id, _ = active_task_with_tracks(admin, "团队任务")
    invite = make_invite(admin)
    application_id = apply_user(app, invite["code"], "p" * 43, "团队新人")
    first = approve(admin, application_id, task_ids=[task_id])
    assert first.status_code == 200 and first.json()["already"] is False
    retry = approve(admin, application_id, task_ids=[task_id])
    assert retry.status_code == 200 and retry.json()["already"] is True
    conflict = approve(admin, application_id, task_ids=[])
    assert conflict.status_code == 409
    # 另一人批准到 draft/回收站任务：draft 可分配（与成员接口语义一致），回收站拒绝。
    other_invite = make_invite(admin)
    other_id = apply_user(app, other_invite["code"], "q" * 43, "团队新人乙")
    draft = admin.post("/api/tasks", json={"title": "还没发布"}).json()["id"]
    response = approve(admin, other_id, task_ids=[draft])
    assert response.status_code == 200, response.text
    draft_user = next(
        row["user_id"]
        for row in admin.get("/api/applications").json()
        if row["id"] == other_id
    )
    with sqlite3.connect(app.state.database) as db:
        assert (draft, draft_user) in {
            tuple(r) for r in db.execute("SELECT task_id,user_id FROM members").fetchall()
        }


def test_reject_does_not_consume_and_expired_states(env):
    app, admin, _ = env
    invite = make_invite(admin, max_uses=1)
    rejected_id = apply_user(app, invite["code"], "r" * 43, "被拒人")
    other_id = apply_user(app, invite["code"], "s" * 43, "候补人")
    assert (
        admin.post(
            f"/api/applications/{rejected_id}/reject",
            headers=secure_headers(admin),
        ).status_code
        == 200
    )
    # 拒绝重试幂等；不消耗次数。
    assert (
        admin.post(
            f"/api/applications/{rejected_id}/reject",
            headers=secure_headers(admin),
        ).status_code
        == 200
    )
    assert admin.get("/api/invites").json()[0]["used_count"] == 0
    assert approve(admin, other_id).status_code == 200
    assert admin.get("/api/invites").json()[0]["used_count"] == 1

    # 停用邀请：新申请立即无效；存量 pending 在轮询时转为 expired。
    disabled = make_invite(admin)
    assert (
        admin.patch(
            f"/api/invites/{disabled['id']}",
            json={"active": False},
            headers=secure_headers(admin),
        ).status_code
        == 200
    )
    client = TestClient(app, client=("10.0.0.3", 3))
    assert (
        client.post(
            "/api/apply",
            json={
                "display_name": "迟到的人",
                "invite_code": disabled["code"],
                "claim_secret": "t" * 43,
            },
        ).status_code
        == 422
    )
    # 早到的人：在停用前申请。
    enabled = make_invite(admin)
    pending_id = apply_user(app, enabled["code"], "u" * 43, "早到的人")
    admin.patch(
        f"/api/invites/{enabled['id']}",
        json={"active": False},
        headers=secure_headers(admin),
    )
    browser = TestClient(app, client=("10.0.0.3", 3))
    status = browser.post(
        "/api/apply/status",
        json={"application_id": pending_id, "claim_secret": "u" * 43},
    )
    assert status.json()["status"] == "expired"
    # 终态不可被普通重试反转。
    assert approve(admin, pending_id).status_code == 409
    assert (
        browser.post(
            "/api/apply/status",
            json={"application_id": pending_id, "claim_secret": "u" * 43},
        ).json()["status"]
        == "expired"
    )


def test_expired_invite_rejected_at_apply_and_approval(env):
    app, _, _ = env
    # 直接插入一条已过期邀请（哈希无法被任何明文命中），验证申请被拒。
    with sqlite3.connect(app.state.database) as db:
        db.execute(
            "INSERT INTO invites VALUES('expired', '', 'team', NULL, 'expired', ?, "
            "'2020-01-01T00:00:00+00:00', 5, 0, 1, '2020-01-01T00:00:00+00:00', NULL)",
            (password_hash("expired-secret"),),
        )
    client = TestClient(app, client=("10.0.0.4", 4))
    assert (
        client.post(
            "/api/apply",
            json={
                "display_name": "过期人",
                "invite_code": "expired.test",
                "claim_secret": "v" * 43,
            },
        ).status_code
        == 422
    )


def test_approval_name_conflict_does_not_consume_invite(env):
    app, admin, _ = env
    invite = make_invite(admin)
    application_id = apply_user(app, invite["code"], "w" * 43, "组织者")
    response = approve(admin, application_id)
    assert response.status_code == 409 and "账号名称已存在" in response.text
    assert admin.get("/api/invites").json()[0]["used_count"] == 0
    assert admin.get("/api/applications").json()[0]["status"] == "pending"
    assert approve(admin, application_id, name="获批新人甲").status_code == 200


def test_rate_limits_for_anonymous_endpoints(env):
    app, admin, _ = env
    invite = make_invite(admin)
    client = TestClient(app, client=("10.0.0.9", 9))
    codes = []
    for i in range(11):
        response = client.post(
            "/api/apply",
            json={
                "display_name": f"限流{i}",
                "invite_code": invite["code"],
                "claim_secret": f"{i:043d}",
            },
        )
        codes.append(response.status_code)
    assert codes[-1] == 429
    # 其他来源不受影响。
    assert (
        TestClient(app, client=("10.0.0.8", 8))
        .post(
            "/api/apply",
            json={
                "display_name": "别处的人",
                "invite_code": invite["code"],
                "claim_secret": "z" * 43,
            },
        )
        .status_code
        == 200
    )
    # 领取限流：20 次后拒绝。
    heavy = TestClient(app, client=("10.0.0.7", 7))
    for i in range(20):
        heavy.post(
            "/api/apply/claim",
            json={"application_id": "0" * 32, "claim_secret": "y" * 43},
        )
    assert (
        heavy.post(
            "/api/apply/claim",
            json={"application_id": "0" * 32, "claim_secret": "y" * 43},
        ).status_code
        == 429
    )


def test_apply_requires_setup_and_identity_fields_optional(env):
    app, admin, folder = env
    invite = make_invite(admin)
    client = TestClient(app, client=("10.0.0.5", 5))
    # 只填显示名也可以；邮箱/工号可省略。
    response = client.post(
        "/api/apply",
        json={
            "display_name": "极简申请人",
            "invite_code": invite["code"],
            "claim_secret": "1" * 43,
        },
    )
    assert response.status_code == 200
    row = admin.get("/api/applications").json()[0]
    assert row["employee_id"] == "" and row["email"] == ""
    # 未初始化的全新服务不接受申请。
    fresh = create_app(folder / "全新目录")
    assert (
        TestClient(fresh, client=("10.0.0.5", 5))
        .post(
            "/api/apply",
            json={
                "display_name": "谁",
                "invite_code": invite["code"],
                "claim_secret": "2" * 43,
            },
        )
        .status_code
        == 409
    )


def test_pending_count_and_device_summary(env):
    app, admin, _ = env
    invite = make_invite(admin)
    ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
    )
    apply_user(app, invite["code"], "3" * 43, "摘要员", ua=ua)
    assert admin.get("/api/me").json()["pending_applications"] == 1
    row = admin.get("/api/applications").json()[0]
    assert row["device"] == "Safari 17 · macOS"
    assert "Mozilla" not in row["device"] and ua not in json.dumps(row)
    assert row["ip"] == "10.0.0.1"
    assert row["invite"]["purpose"] == "第一轮评测"


def test_backup_keeps_new_tables_and_device_login(env, tmp_path):
    app, admin, _ = env
    _, _, token = full_onboard(admin, app, "恢复员", secret="4" * 43)
    result = admin.get("/api/backup")
    assert result.status_code == 200
    destination = tmp_path / "恢复目录"
    with zipfile.ZipFile(io.BytesIO(result.content)) as z:
        assert "workbench.sqlite3" in z.namelist()
        blob = z.read("workbench.sqlite3")
        assert "4" * 43 not in blob.decode("utf-8", errors="ignore")
        assert token not in blob.decode("utf-8", errors="ignore")
        z.extractall(destination)
    restored = TestClient(create_app(destination))
    restored.cookies.set("device", token)
    assert restored.get("/api/me").json()["name"] == "恢复员"
    assert len(admin.get("/api/applications").json()) == 1
    # 备份只保存哈希：恢复后的库同样搜不到明文。
    with sqlite3.connect(destination / "workbench.sqlite3") as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    blob = (destination / "workbench.sqlite3").read_bytes()
    assert token.encode() not in blob and b"4" * 43 not in blob


def test_old_database_upgrade_and_double_init(env):
    _, _, folder = env
    # 模拟 v0.6.0 旧库：没有三张新表。
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        db.execute("DROP TABLE applications")
        db.execute("DROP TABLE invites")
        db.execute("DROP TABLE devices")
    restarted = create_app(folder)
    c = TestClient(restarted)
    assert (
        c.post(
            "/api/login", json={"name": "组织者", "password": "test-only-strong-pass"}
        ).status_code
        == 200
    )
    assert c.get("/api/me").status_code == 200
    assert c.get("/api/invites").json() == []
    # 连续初始化两次：schema 与数据不变。
    create_app(folder)
    snapshot = _snapshot(folder / "workbench.sqlite3")
    create_app(folder)
    assert _snapshot(folder / "workbench.sqlite3") == snapshot


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
            if t not in ("sqlite_sequence",)
        }
    return schema, data
