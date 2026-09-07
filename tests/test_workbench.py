"""验证真实输出、任务冻结、匿名边界和多人持久化。"""

import csv
import io
import json
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from workbench.app import create_app
from workbench.audio import decode_audio, demo_wav


@pytest.fixture
def env(tmp_path):
    app = create_app(tmp_path)
    c = TestClient(app)
    assert (
        c.post(
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
        c.post(
            "/api/login", json={"name": "组织者", "password": "test-only-strong-pass"}
        ).status_code
        == 200
    )
    return app, c, tmp_path


def reviewer(app, admin, name):
    assert (
        admin.post(
            "/api/users", json={"name": name, "password": "reviewer-test-only"}
        ).status_code
        == 200
    )
    c = TestClient(app)
    c.post("/api/login", json={"name": name, "password": "reviewer-test-only"})
    return c, c.get("/api/me").json()["id"]


def task(admin, mode="blind"):
    t = admin.post("/api/tasks", json={"title": "测试任务", "mode": mode}).json()["id"]
    s = admin.post(f"/api/tasks/{t}/samples", json={"name": "同一片段"}).json()["id"]
    tracks = []
    for i in range(2):
        response = admin.post(
            f"/api/samples/{s}/tracks",
            data={"name": f"秘密算法-{i}", "version": "秘密SHA"},
            files={"file": ("秘密文件.wav", demo_wav(i, 0), "audio/wav")},
        )
        assert response.status_code == 200, response.text
        tracks.append(response.json()["id"])
    return t, s, tracks


def publish(admin, t, users=None):
    response = admin.post(
        f"/api/tasks/{t}/publish",
        json={"users": users or [], "alignment_confirmed": True},
    )
    assert response.status_code == 200, response.text


def sample_with_tracks(admin, t, name):
    """一个片段两个候选；名称可预测，便于断言与身份泄露检查。"""
    s = admin.post(f"/api/tasks/{t}/samples", json={"name": name}).json()["id"]
    tracks = []
    for i, suffix in enumerate("甲乙"):
        r = admin.post(
            f"/api/samples/{s}/tracks",
            data={"name": f"{name}候选{suffix}", "version": "v"},
            files={"file": ("x.wav", demo_wav(i, 0), "audio/wav")},
        )
        assert r.status_code == 200, r.text
        tracks.append(r.json()["id"])
    return s, tracks


def test_setup_key_and_single_initialization(tmp_path):
    app = create_app(tmp_path)
    c = TestClient(app)
    body = {"name": "管理员", "password": "1234567890", "setup_key": "错误"}
    assert c.post("/api/setup", json=body).status_code == 403
    body["setup_key"] = app.state.setup_file.read_text()
    assert c.post("/api/setup", json=body).status_code == 200
    assert not app.state.setup_file.exists()
    assert c.post("/api/setup", json=body).status_code == 409


def test_blind_api_and_audio_metadata_do_not_reveal_identity(env):
    app, a, _ = env
    c, member = reviewer(app, a, "甲")
    t, s, tracks = task(a)
    publish(a, t, [member])
    response = c.get(f"/api/samples/{s}")
    assert "秘密" not in response.text
    assert "meta" not in response.json()["tracks"][0]
    assert a.get(f"/api/samples/{s}").json()["blind"]
    assert c.get(f"/api/audio/{tracks[0]}/analysis").status_code == 403
    raw = c.get(f"/api/audio/{tracks[0]}")
    assert raw.status_code == 200
    assert "秘密" not in str(raw.headers) and "秘密".encode() not in raw.content
    data, sr = sf.read(io.BytesIO(raw.content))
    expected, _ = sf.read(io.BytesIO(demo_wav(0, 0)))
    assert sr == 16000 and np.array_equal(data, expected)


def test_unassigned_and_anonymous_cannot_read_assets_or_reports(env):
    app, a, _ = env
    c, _ = reviewer(app, a, "未分配")
    t, s, tracks = task(a)
    publish(a, t)
    for path in (
        f"/api/tasks/{t}",
        f"/api/samples/{s}",
        f"/api/audio/{tracks[0]}",
        f"/api/audio/{tracks[0]}/analysis",
        f"/api/tasks/{t}/report",
    ):
        assert c.get(path).status_code == 403
        assert TestClient(app).get(path).status_code == 401
    assert c.get("/api/tasks").json() == []
    assert c.get("/api/backup").status_code == 403


def test_comments_private_until_close_and_rating_idempotent(env):
    app, a, _ = env
    c, member = reviewer(app, a, "甲")
    d, member2 = reviewer(app, a, "乙")
    t, s, tracks = task(a)
    publish(a, t, [member, member2])
    body = {"start": 100, "end": 800, "body": "字尾断续", "track_id": tracks[0]}
    cid = c.post(f"/api/samples/{s}/comments", json=body).json()["id"]
    assert len(c.get(f"/api/samples/{s}").json()["comments"]) == 1
    assert d.get(f"/api/samples/{s}").json()["comments"] == []
    assert a.get(f"/api/samples/{s}").json()["comments"] == []
    assert (
        d.post(f"/api/samples/{s}/comments", json={**body, "parent": cid}).status_code
        == 403
    )
    vote = {"choice": tracks[1], "reason": "更完整"}
    assert c.post(f"/api/samples/{s}/rating", json=vote).status_code == 200
    assert c.post(f"/api/samples/{s}/rating", json=vote).status_code == 200
    assert c.post(f"/api/samples/{s}/rating", json={"choice": "tie"}).status_code == 409
    assert a.get(f"/api/tasks/{t}/export").status_code == 403
    assert a.post(f"/api/tasks/{t}/close").status_code == 200
    assert not c.get(f"/api/samples/{s}").json()["blind"]
    assert len(d.get(f"/api/samples/{s}").json()["comments"]) == 1
    assert (
        d.post(f"/api/samples/{s}/comments", json={**body, "parent": cid}).status_code
        == 200
    )
    assert d.post(f"/api/samples/{s}/rating", json={"choice": "tie"}).status_code == 409
    report = a.get(f"/api/tasks/{t}/report").json()
    assert len(report["样本"][0]["ratings"]) == 1


def test_publish_requires_pair_and_alignment_and_freezes_inputs(env):
    _, a, _ = env
    t, s, _ = task(a)
    assert a.post(f"/api/tasks/{t}/publish", json={}).status_code == 422
    publish(a, t)
    assert a.post(f"/api/tasks/{t}/samples", json={"name": "新增"}).status_code == 409
    assert (
        a.post(
            f"/api/samples/{s}/tracks",
            data={"name": "改动"},
            files={"file": ("a.wav", demo_wav(0, 0))},
        ).status_code
        == 409
    )
    assert (
        a.post(
            f"/api/tasks/{t}/publish", json={"alignment_confirmed": True}
        ).status_code
        == 409
    )


def test_mismatched_lengths_and_invalid_wav_rejected(env):
    _, a, _ = env
    _, s, _ = task(a)
    short = io.BytesIO()
    sf.write(short, np.zeros(1600), 16000, format="WAV")
    for data in (short.getvalue(), b"not wav"):
        assert (
            a.post(
                f"/api/samples/{s}/tracks",
                data={"name": "不合法"},
                files={"file": ("a.wav", data)},
            ).status_code
            == 422
        )


@pytest.mark.parametrize(
    "sr,channels,nan", [(48000, 1, False), (16000, 2, False), (16000, 1, True)]
)
def test_unsupported_inputs(sr, channels, nan):
    x = np.zeros((1600, channels))
    if nan:
        x[1, 0] = np.nan
    data = io.BytesIO()
    sf.write(data, x, sr, format="WAV", subtype="FLOAT")
    with pytest.raises(ValueError):
        decode_audio(data.getvalue())


def test_channel_mapping_and_gain_preserved():
    x = np.zeros((1600, 4), dtype=np.float32)
    x[:, 1] = 0.125
    x[:, 3] = -0.0625
    raw = io.BytesIO()
    sf.write(raw, x, 16000, format="WAV", subtype="FLOAT")
    ff, fmeta = decode_audio(raw.getvalue(), 1)
    vpu, vmeta = decode_audio(raw.getvalue(), 3)
    assert np.array_equal(sf.read(io.BytesIO(ff))[0], x[:, 1])
    assert np.array_equal(sf.read(io.BytesIO(vpu))[0], x[:, 3])
    assert fmeta["rms_dbfs"] - vmeta["rms_dbfs"] == pytest.approx(6.020599913)


def test_range_validation_cross_sample_and_csrf(env):
    _, a, _ = env
    _, s, _ = task(a)
    _, _, other_tracks = task(a)
    for start, end in ((-1, 100), (100, 10), (0, 999999)):
        assert (
            a.post(
                f"/api/samples/{s}/comments",
                json={"start": start, "end": end, "body": "评论"},
            ).status_code
            == 422
        )
    assert (
        a.post(
            f"/api/samples/{s}/comments",
            json={"start": 0, "end": 10, "body": "评论", "track_id": other_tracks[0]},
        ).status_code
        == 422
    )
    assert (
        a.post(
            "/api/tasks",
            json={"title": "不允许"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )


def test_mutated_asset_is_detected(env):
    _, a, root = env
    _, _, tracks = task(a)
    with sqlite3.connect(root / "workbench.sqlite3") as db:
        path = db.execute(
            "SELECT path FROM tracks WHERE id=?", (tracks[0],)
        ).fetchone()[0]
    (root / "assets" / path).write_bytes(demo_wav(1, 2))
    assert a.get(f"/api/audio/{tracks[0]}").status_code == 409


def test_backup_restore_includes_votes_and_audio_excludes_sessions(env, tmp_path):
    _, a, _ = env
    t, s, tracks = task(a)
    publish(a, t)
    a.post(f"/api/samples/{s}/rating", json={"choice": tracks[0]})
    a.post(
        f"/api/samples/{s}/comments",
        json={"start": 100, "end": 1000, "body": "需要复测", "track_id": tracks[0]},
    )
    before = a.get(f"/api/audio/{tracks[0]}").content
    result = a.get("/api/backup")
    assert result.status_code == 200
    destination = tmp_path / "恢复验证"
    with zipfile.ZipFile(io.BytesIO(result.content)) as z:
        z.extractall(destination)
    app = create_app(destination)
    c = TestClient(app)
    c.cookies.update(a.cookies)
    assert c.get("/api/me").status_code == 401
    c.post("/api/login", json={"name": "组织者", "password": "test-only-strong-pass"})
    assert c.get(f"/api/audio/{tracks[0]}").content == before
    assert c.get(f"/api/samples/{s}").json()["rating"]["choice"] == tracks[0]
    assert c.get(f"/api/samples/{s}").json()["comments"][0]["start"] == 100


def test_ten_concurrent_reviewers_no_lost_or_overwritten_votes(env):
    app, a, _ = env
    people = [reviewer(app, a, f"同事{i}") for i in range(10)]
    t, s, tracks = task(a)
    publish(a, t, [p[1] for p in people])

    def submit(person):
        c, _ = person
        assert c.get(f"/api/samples/{s}").status_code == 200
        assert (
            c.post(
                f"/api/samples/{s}/comments",
                json={"start": 0, "end": 160, "body": "独立判断"},
            ).status_code
            == 200
        )
        return c.post(
            f"/api/samples/{s}/rating", json={"choice": tracks[0]}
        ).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        assert list(pool.map(submit, people)) == [200] * 10
    a.post(f"/api/tasks/{t}/close")
    sample = a.get(f"/api/tasks/{t}/report").json()["样本"][0]
    assert len(sample["ratings"]) == 10 and len(sample["comments"]) == 10
    assert len({r["user_id"] for r in sample["ratings"]}) == 10


def test_analysis_frequency_axis_and_common_scale(env):
    _, a, _ = env
    _, _, tracks = task(a, "development")
    result = a.get(f"/api/audio/{tracks[0]}/analysis")
    assert result.status_code == 200
    value = result.json()
    assert value["hop"] == 160 and value["n_fft"] == 512 and value["win"] == 480
    assert len(value["spectrogram"][0]) == 257
    assert value["db_range"] == [-100, 0]


def test_explicit_https_proxy_origin_and_cookie(tmp_path):
    app = create_app(
        tmp_path, secure_cookie=True, public_origin="https://audio.example.internal"
    )
    c = TestClient(app, base_url="https://audio.example.internal")
    body = {
        "name": "管理员",
        "password": "password-test-only",
        "setup_key": app.state.setup_file.read_text(),
    }
    assert (
        c.post(
            "/api/setup",
            json=body,
            headers={"origin": "https://audio.example.internal"},
        ).status_code
        == 200
    )
    response = c.post(
        "/api/login", json=body, headers={"origin": "https://audio.example.internal"}
    )
    assert "Secure" in response.headers["set-cookie"]
    assert c.get("/api/me").status_code == 200
    assert (
        c.post(
            "/api/tasks",
            json={"title": "跨站"},
            headers={"origin": "https://unrelated.example"},
        ).status_code
        == 403
    )


def test_atomic_batch_retry_and_freeze(env):
    import json

    app, a, _ = env
    t = a.post("/api/tasks", json={"title": "批量任务"}).json()["id"]
    manifest = {
        "request_id": "a" * 32,
        "name": "车内/001.wav",
        "tracks": [
            {"name": "自研", "version": "abc"},
            {"name": "竞品", "version": "未知"},
        ],
    }

    def send(second):
        return a.post(
            f"/api/tasks/{t}/import-sample",
            data={"manifest": json.dumps(manifest)},
            files=[
                ("files", ("001.wav", demo_wav(0, 0), "audio/wav")),
                ("files", ("001.wav", second, "audio/wav")),
            ],
        )

    assert send("坏音频".encode()).status_code == 422
    assert a.get(f"/api/tasks/{t}").json()["samples"] == []
    response = send(demo_wav(1, 0))
    assert response.status_code == 200, response.text
    import time

    time.sleep(1.05)  # 跨秒重试，防止 WAV 的可变时间戳破坏幂等性。
    retry = send(demo_wav(1, 0))
    assert retry.status_code == 200, retry.text
    assert retry.json()["reused"] is True
    assert len(a.get(f"/api/tasks/{t}").json()["samples"]) == 1
    assert send(demo_wav(2, 0)).status_code == 409
    manifest["request_id"] = "b" * 32
    assert send(demo_wav(1, 0)).status_code == 409
    outsider, _ = reviewer(app, a, "无权限")
    assert (
        outsider.post(
            f"/api/tasks/{t}/import-sample",
            data={"manifest": json.dumps(manifest)},
            files=[("files", ("x.wav", b"x"))],
        ).status_code
        == 403
    )
    publish(a, t)
    assert send(demo_wav(1, 0)).status_code == 409


def test_draft_candidate_management_preserves_evidence(env):
    _, a, _ = env
    t, s, tracks = task(a, "development")
    assert (
        a.patch(
            f"/api/tracks/{tracks[0]}", json={"name": "更新名称", "version": "新证据"}
        ).status_code
        == 200
    )
    assert (
        next(
            tr
            for tr in a.get(f"/api/samples/{s}").json()["tracks"]
            if tr["id"] == tracks[0]
        )["name"]
        == "更新名称"
    )
    assert (
        a.post(
            f"/api/samples/{s}/comments",
            json={"track_id": tracks[0], "start": 0, "end": 160, "body": "保留依据"},
        ).status_code
        == 200
    )
    assert a.delete(f"/api/tracks/{tracks[0]}").status_code == 409
    assert a.delete(f"/api/tracks/{tracks[1]}").status_code == 200
    assert len(a.get(f"/api/samples/{s}").json()["tracks"]) == 1
    a.post(
        f"/api/samples/{s}/tracks",
        data={"name": "补充版本"},
        files={"file": ("a.wav", demo_wav(1, 0))},
    )
    publish(a, t)
    assert (
        a.patch(f"/api/tracks/{tracks[0]}", json={"name": "禁止修改"}).status_code
        == 409
    )
    assert a.delete(f"/api/tracks/{tracks[0]}").status_code == 409


def test_remove_all_unannotated_tracks_resets_length(env):
    _, a, _ = env
    _, s, tracks = task(a, "development")
    for track_id in tracks:
        assert a.delete(f"/api/tracks/{track_id}").status_code == 200
    assert a.get(f"/api/samples/{s}").json()["samples"] == 0
    raw = io.BytesIO()
    sf.write(raw, np.zeros(16000), 16000, format="WAV")
    assert (
        a.post(
            f"/api/samples/{s}/tracks",
            data={"name": "新的短片段"},
            files={"file": ("a.wav", raw.getvalue())},
        ).status_code
        == 200
    )


def test_anonymous_wav_is_deterministic_and_preserves_float_samples():
    import time

    original = demo_wav(1, 0)
    first, meta = decode_audio(original)
    time.sleep(1.05)
    second, retry_meta = decode_audio(original)
    assert first == second
    assert meta == retry_meta
    decoded, sr = sf.read(io.BytesIO(first), dtype="float32")
    reference, _ = sf.read(io.BytesIO(original), dtype="float32")
    assert sr == 16000
    np.testing.assert_array_equal(decoded, reference)


def test_organizer_owns_tasks_without_site_privileges(env):
    app, a, _ = env
    org, org_id = reviewer(app, a, "同事组织者")
    other, other_id = reviewer(app, a, "另一位同事")
    assert org.post("/api/tasks", json={"title": "尚未授权"}).status_code == 403
    assert (
        a.patch(f"/api/users/{org_id}/role", json={"role": "organizer"}).status_code
        == 200
    )
    t, s, _ = task(org)
    assert org.get(f"/api/tasks/{t}").json()["can_manage"] is True
    assert other.get(f"/api/tasks/{t}").status_code == 403
    assert org.get("/api/users").status_code == 200
    assert org.get("/api/backup").status_code == 403
    assert (
        org.post(
            "/api/users", json={"name": "越权账号", "password": "1234567890"}
        ).status_code
        == 403
    )
    assert (
        org.patch(f"/api/users/{other_id}/role", json={"role": "organizer"}).status_code
        == 403
    )
    assert (
        a.patch(f"/api/users/{other_id}/role", json={"role": "organizer"}).status_code
        == 200
    )
    foreign, _, _ = task(other)
    assert (
        org.request(
            "DELETE", f"/api/tasks/{foreign}", json={"title": "测试任务"}
        ).status_code
        == 403
    )
    publish(org, t, [other_id])
    assert other.get(f"/api/tasks/{t}").json()["can_manage"] is False
    assert other.post(f"/api/tasks/{t}/close").status_code == 403
    assert org.get(f"/api/samples/{s}").json()["blind"] is True
    assert org.get(f"/api/tasks/{t}/export").status_code == 403
    assert org.post(f"/api/tasks/{t}/close").status_code == 200
    assert org.get(f"/api/tasks/{t}/export").status_code == 200
    assert other.get(f"/api/tasks/{t}/export").status_code == 403
    # 现有会话在降权后立即失去管理权限。
    assert (
        a.patch(f"/api/users/{org_id}/role", json={"role": "reviewer"}).status_code
        == 200
    )
    assert (
        org.request("DELETE", f"/api/tasks/{t}", json={"title": "测试任务"}).status_code
        == 403
    )


def test_task_trash_blocks_all_access_and_restores_evidence(env):
    app, a, _ = env
    c, member = reviewer(app, a, "参与者")
    t, s, tracks = task(a)
    publish(a, t, [member])
    assert (
        c.post(
            f"/api/samples/{s}/comments",
            json={
                "track_id": tracks[0],
                "start": 0,
                "end": 160,
                "body": "应保留的标注",
            },
        ).status_code
        == 200
    )
    assert (
        c.post(f"/api/samples/{s}/rating", json={"choice": tracks[0]}).status_code
        == 200
    )
    assert (
        c.request("DELETE", f"/api/tasks/{t}", json={"title": "测试任务"}).status_code
        == 403
    )
    assert (
        a.request("DELETE", f"/api/tasks/{t}", json={"title": "错误名称"}).status_code
        == 422
    )
    assert (
        a.request("DELETE", f"/api/tasks/{t}", json={"title": "测试任务"}).status_code
        == 200
    )
    assert (
        a.request("DELETE", f"/api/tasks/{t}", json={"title": "测试任务"}).status_code
        == 200
    )
    assert not a.get("/api/tasks").json()
    assert len(a.get("/api/tasks?deleted=true").json()) == 1
    assert not c.get("/api/tasks?deleted=true").json()
    for client in (a, c):
        for path in (
            f"/api/tasks/{t}",
            f"/api/samples/{s}",
            f"/api/audio/{tracks[0]}",
            f"/api/audio/{tracks[0]}/analysis",
            f"/api/tasks/{t}/report",
            f"/api/tasks/{t}/export",
        ):
            assert client.get(path).status_code == 404
        assert (
            client.post(
                f"/api/samples/{s}/comments",
                json={"start": 0, "end": 160, "body": "不能追加"},
            ).status_code
            == 404
        )
    assert c.post(f"/api/tasks/{t}/restore").status_code == 404
    assert a.post(f"/api/tasks/{t}/restore").status_code == 200
    restored = c.get(f"/api/samples/{s}").json()
    assert restored["rating"]["choice"] == tracks[0]
    assert restored["comments"][0]["body"] == "应保留的标注"
    assert c.get(f"/api/tasks/{t}").json()["status"] == "active"
    assert (
        a.patch(f"/api/tracks/{tracks[0]}", json={"name": "仍然冻结"}).status_code
        == 409
    )
    assert c.get(f"/api/audio/{tracks[0]}").status_code == 200


def test_existing_database_adds_recycle_bin_without_changing_tasks(env):
    app, a, folder = env
    t, s, _ = task(a)
    with sqlite3.connect(app.state.database) as db:
        db.execute("DROP TABLE deleted_tasks")
    restarted = create_app(folder)
    c = TestClient(restarted)
    c.post("/api/login", json={"name": "组织者", "password": "test-only-strong-pass"})
    assert c.get(f"/api/tasks/{t}").json()["samples"][0]["id"] == s
    assert (
        c.request("DELETE", f"/api/tasks/{t}", json={"title": "测试任务"}).status_code
        == 200
    )


def test_password_reset_revokes_sessions_and_preserves_work(env):
    app, a, _ = env
    c, member = reviewer(app, a, "需要重置")
    second = TestClient(app)
    second.post(
        "/api/login", json={"name": "需要重置", "password": "reviewer-test-only"}
    )
    other, _ = reviewer(app, a, "其他同事")
    t, s, tracks = task(a)
    publish(a, t, [member])
    c.post(f"/api/samples/{s}/rating", json={"choice": tracks[0]})
    payload = {"password": "new-random-test-only-2026", "confirm_name": "需要重置"}
    route = f"/api/users/{member}/password"
    assert c.post(route, json=payload).status_code == 403
    assert (
        a.post(route, json={**payload, "confirm_name": "其他同事"}).status_code == 422
    )
    assert c.get("/api/me").status_code == 200
    result = a.post(route, json=payload)
    assert result.status_code == 200
    assert payload["password"] not in result.text
    assert c.get("/api/me").status_code == 401
    assert second.get("/api/me").status_code == 401
    assert other.get("/api/me").status_code == 200
    assert a.get("/api/me").status_code == 200
    assert (
        c.post(
            "/api/login", json={"name": "需要重置", "password": "reviewer-test-only"}
        ).status_code
        == 401
    )
    assert (
        c.post(
            "/api/login", json={"name": "需要重置", "password": payload["password"]}
        ).status_code
        == 200
    )
    assert c.get(f"/api/samples/{s}").json()["rating"]["choice"] == tracks[0]
    with sqlite3.connect(app.state.database) as db:
        stored = db.execute(
            "SELECT password FROM users WHERE id=?", (member,)
        ).fetchone()[0]
    assert stored != payload["password"]
    admin_id = a.get("/api/me").json()["id"]
    assert (
        a.post(
            f"/api/users/{admin_id}/password",
            json={**payload, "confirm_name": "组织者"},
        ).status_code
        == 409
    )


def test_service_identity_and_page_cache_boundary(env):
    app, a, folder = env
    c, _ = reviewer(app, a, "服务核对")
    admin_info = a.get("/api/server-info").json()
    colleague_info = c.get("/api/server-info").json()
    assert admin_info["version"] == a.get("/api/status").json()["version"]
    assert colleague_info["data_id"] == admin_info["data_id"]
    assert colleague_info["instance_id"] == admin_info["instance_id"]
    assert "data_directory" not in colleague_info
    assert admin_info["data_directory"] == str(folder.resolve())
    assert TestClient(app).get("/api/server-info").status_code == 401
    assert a.get("/").headers["cache-control"] == "no-store"


def test_draft_task_and_sample_metadata_can_be_corrected_then_freeze(env):
    app, admin, _ = env
    colleague, _ = reviewer(app, admin, "无权编辑者")
    task_id, sample_id, _ = task(admin, "development")
    task_body = {"title": "修正后的任务", "kind": "竞品算法", "mode": "blind"}
    assert colleague.patch(f"/api/tasks/{task_id}", json=task_body).status_code == 403
    assert admin.patch(f"/api/tasks/{task_id}", json=task_body).status_code == 200
    sample_body = {
        "name": "修正后的片段",
        "scene": "车内",
        "provenance": "PUBLIC reproducible",
    }
    assert admin.patch(f"/api/samples/{sample_id}", json=sample_body).status_code == 200
    detail = admin.get(f"/api/tasks/{task_id}").json()
    assert (detail["title"], detail["kind"], detail["mode"]) == (
        "修正后的任务",
        "竞品算法",
        "blind",
    )
    assert detail["samples"][0]["name"] == "修正后的片段"
    assert detail["samples"][0]["scene"] == "车内"
    publish(admin, task_id)
    assert admin.patch(f"/api/tasks/{task_id}", json=task_body).status_code == 409
    assert admin.patch(f"/api/samples/{sample_id}", json=sample_body).status_code == 409


def test_active_member_changes_preserve_contributors(env):
    app, admin, _ = env
    first, first_id = reviewer(app, admin, "首批参与者")
    second, second_id = reviewer(app, admin, "后加入者")
    third, third_id = reviewer(app, admin, "可移除者")
    task_id, sample_id, tracks = task(admin)
    owner_id = admin.get("/api/me").json()["id"]
    publish(admin, task_id, [first_id, third_id])
    endpoint = f"/api/tasks/{task_id}/members"
    assert first.patch(endpoint, json={"users": [second_id]}).status_code == 403
    assert (
        admin.patch(endpoint, json={"users": [first_id, second_id]}).status_code == 200
    )
    assert second.get(f"/api/tasks/{task_id}").status_code == 200
    assert third.get(f"/api/tasks/{task_id}").status_code == 403
    first.post(
        f"/api/samples/{sample_id}/comments",
        json={"track_id": tracks[0], "start": 0, "end": 160, "body": "已贡献"},
    )
    response = admin.patch(endpoint, json={"users": [second_id]})
    assert response.status_code == 409
    assert "首批参与者" in response.text
    members = set(admin.get(f"/api/tasks/{task_id}").json()["members"])
    assert {owner_id, first_id, second_id} <= members
    assert admin.post(f"/api/tasks/{task_id}/close").status_code == 200
    assert admin.patch(endpoint, json={"users": [first_id]}).status_code == 409


def test_nested_replies_keep_exact_parent(env):
    app, admin, _ = env
    first, first_id = reviewer(app, admin, "发帖人")
    second, second_id = reviewer(app, admin, "回复人")
    task_id, sample_id, tracks = task(admin, "development")
    publish(admin, task_id, [first_id, second_id])
    base = {"track_id": tracks[0], "start": 100, "end": 300, "tag": "音色"}
    root = first.post(
        f"/api/samples/{sample_id}/comments", json={**base, "body": "原评论"}
    ).json()["id"]
    reply = second.post(
        f"/api/samples/{sample_id}/comments",
        json={**base, "body": "一级回复", "parent": root},
    ).json()["id"]
    nested = first.post(
        f"/api/samples/{sample_id}/comments",
        json={**base, "body": "回复回复", "parent": reply},
    ).json()["id"]
    comments = admin.get(f"/api/samples/{sample_id}").json()["comments"]
    by_id = {comment["id"]: comment for comment in comments}
    assert by_id[reply]["parent"] == root
    assert by_id[nested]["parent"] == reply


def test_progress_and_vote_summary_rules(env):
    """参与进度、票数分母、幂等提交与分歧分类的可证伪规则。"""
    app, a, root = env
    first, first_id = reviewer(app, a, "参与者一")
    second, second_id = reviewer(app, a, "参与者二")
    third, third_id = reviewer(app, a, "参与者三")
    t = a.post("/api/tasks", json={"title": "进度与汇总规则", "mode": "development"}).json()["id"]
    s1, tracks1 = sample_with_tracks(a, t, "片段一")
    s2, tracks2 = sample_with_tracks(a, t, "片段二")
    s3, tracks3 = sample_with_tracks(a, t, "片段三")
    publish(a, t, [first_id, second_id, third_id])
    owner_id = a.get("/api/me").json()["id"]

    # 幂等重复提交不增加票数；只评论不算提交。
    assert first.post(f"/api/samples/{s1}/rating", json={"choice": tracks1[0]}).status_code == 200
    assert first.post(f"/api/samples/{s1}/rating", json={"choice": tracks1[0]}).status_code == 200
    assert second.post(f"/api/samples/{s1}/rating", json={"choice": "tie"}).status_code == 200
    assert (
        third.post(
            f"/api/samples/{s1}/comments", json={"start": 0, "end": 100, "body": "只评论"}
        ).status_code
        == 200
    )
    # 不同片段提交数不同：片段二两人一致，片段三仅一人。
    assert first.post(f"/api/samples/{s2}/rating", json={"choice": tracks2[0]}).status_code == 200
    assert second.post(f"/api/samples/{s2}/rating", json={"choice": tracks2[0]}).status_code == 200
    assert first.post(f"/api/samples/{s3}/rating", json={"choice": tracks3[0]}).status_code == 200

    assert a.post(f"/api/tasks/{t}/close").status_code == 200
    report = a.get(f"/api/tasks/{t}/report").json()
    progress = report["参与进度"]
    # 发布时负责人自动进入成员表，因此按“被要求评分”计入未提交，不漏算也不重复算。
    assert progress["总参与者"] == 4
    assert progress["已提交"] == 2 and progress["未提交"] == 2
    assert set(progress["已提交名单"]) == {"参与者一", "参与者二"}
    assert set(progress["未提交名单"]) == {"参与者三", "组织者"}
    assert {m["名称"]: m["已提交片段数"] for m in progress["成员"]} == {
        "参与者一": 3,
        "参与者二": 2,
        "参与者三": 0,
        "组织者": 0,
    }

    views = {s["id"]: s for s in report["样本"]}
    assert views[s1]["分母"] == 2 and views[s2]["分母"] == 2 and views[s3]["分母"] == 1
    assert {v["ID"]: v["票数"] for v in views[s1]["票数"]} == {
        tracks1[0]: 1,
        tracks1[1]: 0,
        "tie": 1,
    }
    assert views[s1]["分歧"] is True
    assert views[s2]["分歧"] is False
    assert views[s3]["分歧"] is False

    # 故意改变一票：分歧分类随之变化（两个方向都验证）。
    with sqlite3.connect(root / "workbench.sqlite3") as db:
        db.execute(
            "UPDATE ratings SET choice=? WHERE sample_id=? AND user_id=?",
            (tracks1[0], s1, second_id),
        )
    views = {s["id"]: s for s in a.get(f"/api/tasks/{t}/report").json()["样本"]}
    assert views[s1]["分歧"] is False
    with sqlite3.connect(root / "workbench.sqlite3") as db:
        db.execute(
            "UPDATE ratings SET choice=? WHERE sample_id=? AND user_id=?",
            ("tie", s2, second_id),
        )
    views = {s["id"]: s for s in a.get(f"/api/tasks/{t}/report").json()["样本"]}
    assert views[s2]["分歧"] is True

    # 进度只依据成员表：负责人被移出成员表后不得被补记为缺失或参与者。
    with sqlite3.connect(root / "workbench.sqlite3") as db:
        db.execute("DELETE FROM members WHERE task_id=? AND user_id=?", (t, owner_id))
    progress = a.get(f"/api/tasks/{t}/report").json()["参与进度"]
    assert progress["总参与者"] == 3
    assert owner_id not in [m["ID"] for m in progress["成员"]]
    assert "组织者" not in progress["未提交名单"]


def test_active_blind_summary_and_exports_stay_sealed(env):
    """active 盲评任务对任何人都拒绝汇总与导出，关闭后按既有规则揭晓。"""
    app, a, _ = env
    member, member_id = reviewer(app, a, "盲评成员")
    outsider, _ = reviewer(app, a, "局外人")
    t, s, tracks = task(a)
    publish(a, t, [member_id])
    member.post(f"/api/samples/{s}/rating", json={"choice": tracks[0]})
    member.post(f"/api/samples/{s}/comments", json={"start": 0, "end": 100, "body": "盲评中"})
    for path in ("report", "export", "export.csv", "export.md"):
        sealed = a.get(f"/api/tasks/{t}/{path}")
        assert sealed.status_code == 403
        assert "秘密" not in sealed.text
        assert member.get(f"/api/tasks/{t}/{path}").status_code == 403
        assert outsider.get(f"/api/tasks/{t}/{path}").status_code == 403
        assert TestClient(app).get(f"/api/tasks/{t}/{path}").status_code == 401
    a.post(f"/api/tasks/{t}/close")
    # 关闭后成员可查看汇总（既有规则），导出仍仅限任务管理者。
    assert member.get(f"/api/tasks/{t}/report").status_code == 200
    assert "秘密算法" in member.get(f"/api/tasks/{t}/report").text
    for path in ("export", "export.csv", "export.md"):
        assert member.get(f"/api/tasks/{t}/{path}").status_code == 403
        assert outsider.get(f"/api/tasks/{t}/{path}").status_code == 403
    assert outsider.get(f"/api/tasks/{t}/report").status_code == 403


def test_tag_summary_counts_root_comments_only_and_locates_samples(env):
    """标签只统计根评论，回复不计入；汇总携带片段定位信息。"""
    app, a, _ = env
    member, member_id = reviewer(app, a, "标签员")
    t = a.post("/api/tasks", json={"title": "标签口径", "mode": "development"}).json()["id"]
    s1, _ = sample_with_tracks(a, t, "室内")
    s2, _ = sample_with_tracks(a, t, "车内")
    publish(a, t, [member_id])
    root = member.post(
        f"/api/samples/{s1}/comments",
        json={"start": 0, "end": 100, "body": "残噪明显", "tag": "残噪"},
    ).json()["id"]
    member.post(
        f"/api/samples/{s1}/comments",
        json={"start": 0, "end": 100, "body": "回复也提标签", "tag": "听感", "parent": root},
    )
    member.post(
        f"/api/samples/{s2}/comments",
        json={"start": 0, "end": 100, "body": "车内同样残噪", "tag": "残噪"},
    )
    a.post(f"/api/tasks/{t}/close")
    tags = {x["标签"]: x for x in a.get(f"/api/tasks/{t}/report").json()["标签汇总"]}
    assert tags["残噪"]["根评论数"] == 2
    assert "听感" not in tags
    assert [part["ID"] for part in tags["残噪"]["片段"]] == [s1, s2]
    assert tags["残噪"]["片段"][0]["名称"] == "室内"


def test_export_formats_content_and_compatibility(env):
    """JSON/CSV/Markdown 三种导出内容断言与旧字段兼容。"""
    app, a, _ = env
    first, first_id = reviewer(app, a, "导出甲")
    second, second_id = reviewer(app, a, "导出乙")
    t = a.post("/api/tasks", json={"title": "导出内容验收", "mode": "blind"}).json()["id"]
    s1, tracks1 = sample_with_tracks(a, t, "室内")
    s2, tracks2 = sample_with_tracks(a, t, "车内")
    publish(a, t, [first_id, second_id])
    first.post(f"/api/samples/{s1}/rating", json={"choice": tracks1[0], "reason": "更干净"})
    second.post(f"/api/samples/{s1}/rating", json={"choice": "tie"})
    first.post(f"/api/samples/{s2}/rating", json={"choice": tracks2[1]})
    first.post(
        f"/api/samples/{s1}/comments",
        json={"start": 0, "end": 100, "body": "残噪", "tag": "残噪"},
    )
    a.post(f"/api/tasks/{t}/close")

    payload = a.get(f"/api/tasks/{t}/export")
    assert payload.status_code == 200
    data = json.loads(payload.text)
    assert data["任务"]["id"] == t
    assert data["任务"]["title"] == "导出内容验收"
    assert data["任务"]["status"] == "closed" and data["任务"]["mode"] == "blind"
    assert data["生成时间"]
    progress = data["参与进度"]
    assert (progress["总参与者"], progress["已提交"], progress["未提交"]) == (3, 2, 1)
    indoor = next(s for s in data["样本"] if s["id"] == s1)
    outdoor = next(s for s in data["样本"] if s["id"] == s2)
    assert indoor["分母"] == 2 and outdoor["分母"] == 1
    assert {v["ID"]: v["票数"] for v in indoor["票数"]} == {
        tracks1[0]: 1,
        tracks1[1]: 0,
        "tie": 1,
    }
    assert indoor["分歧"] is True and outdoor["分歧"] is False
    assert data["标签汇总"][0]["标签"] == "残噪"
    assert data["标签汇总"][0]["片段"][0]["ID"] == s1
    # 旧字段保持兼容：旧入口/旧数据仍可读取。
    assert data["播放口径"] and isinstance(indoor["ratings"], list)
    assert "室内候选甲" in payload.text

    raw = a.get(f"/api/tasks/{t}/export.csv").content
    assert raw[:3] == b"\xef\xbb\xbf" and b"\r\n" in raw
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    assert rows[0][:3] == ["部分", "条目ID", "片段ID"]
    assert {"任务信息", "参与进度", "逐片段偏好", "问题标签"} <= {row[0] for row in rows}
    indoor_rows = [row for row in rows if row[0] == "逐片段偏好" and row[3] == "室内"]
    assert any(row[4] == "无明显差异" and row[6] == "1" and row[7] == "2" for row in indoor_rows)
    assert any(row[4] == "室内候选甲" and row[8] == "50.0%（1/2）" for row in indoor_rows)
    assert any(row[9].startswith("存在分歧") for row in indoor_rows)
    assert any(row[4] == "总参与者" and row[6] == "3" for row in rows)
    assert any(row[4] == "组织者" and row[6] == "0" and row[9] == "未提交" for row in rows)
    assert any(row[4] == "导出乙" and row[6] == "1" and row[9] == "已提交" for row in rows)
    assert any(row[5] == "残噪" and row[6] == "1" and s1 in row[9] for row in rows)

    markdown = a.get(f"/api/tasks/{t}/export.md").text
    assert "# 听鉴结果报告 · 导出内容验收" in markdown
    assert "closed" in markdown and "blind" in markdown
    assert "有效提交人数（分母）：2" in markdown
    assert "50.0%（1/2）" in markdown
    assert "仅统计根评论" in markdown and "描述性提示" in markdown
    assert "未提交 1 人" in markdown
    assert tracks1[0] in markdown and s1 in markdown
