"""验证真实输出、任务冻结、匿名边界和多人持久化。"""

import csv
import io
import json
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from workbench import app as app_module
from workbench.app import create_app, csv_safe
from workbench.audio import decode_audio, demo_wav, encode_float_wav


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


def csrf(client):
    return client.get("/api/me").json()["csrf_token"]


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
    owner_id = a.get("/api/me").json()["id"]
    publish(a, t, [owner_id])
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
    """受邀口径、三态完成度、票数分母、幂等提交与分歧分类的可证伪规则。"""
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

    # 负责人未受邀：受邀名单只有 3 位同事，owner 不出现在任何进度名单。
    progress = a.get(f"/api/tasks/{t}/progress").json()["参与进度"]
    assert progress["受邀评测者"] == 3
    assert {m["名称"]: (m["已提交片段数"], m["状态"]) for m in progress["成员"]} == {
        "参与者一": (3, "已完成"),
        "参与者二": (2, "进行中"),
        "参与者三": (0, "未开始"),
    }
    assert progress["已完成"] == 1 and progress["进行中"] == 1 and progress["未开始"] == 1
    assert owner_id not in [m["ID"] for m in progress["成员"]]
    assert all("组织者" not in names for names in progress.values() if isinstance(names, list))

    # 未受邀负责人首次提交评分被拒绝，且不产生评分行。
    rejected = a.post(f"/api/samples/{s1}/rating", json={"choice": "tie"})
    assert rejected.status_code == 403
    with sqlite3.connect(root / "workbench.sqlite3") as db:
        assert (
            db.execute(
                "SELECT count(*) FROM ratings WHERE sample_id=? AND user_id=?",
                (s1, owner_id),
            ).fetchone()[0]
            == 0
        )

    # 显式把负责人加入受邀名单后即可评分，并计入进度与片段分母。
    assert (
        a.patch(
            f"/api/tasks/{t}/members",
            json={"users": [first_id, second_id, third_id, owner_id]},
        ).status_code
        == 200
    )
    assert a.post(f"/api/samples/{s3}/rating", json={"choice": "tie"}).status_code == 200
    progress = a.get(f"/api/tasks/{t}/progress").json()["参与进度"]
    assert progress["受邀评测者"] == 4
    assert {"名称": "组织者", "已提交片段数": 1, "状态": "进行中"} in [
        {k: m[k] for k in ("名称", "已提交片段数", "状态")} for m in progress["成员"]
    ]

    # 负责人已贡献后不能被移出受邀名单（沿用贡献者移除保护），访问权限始终保留。
    blocked = a.patch(
        f"/api/tasks/{t}/members", json={"users": [first_id, second_id, third_id]}
    )
    assert blocked.status_code == 409 and "组织者" in blocked.text
    assert owner_id in a.get(f"/api/tasks/{t}").json()["members"]

    assert a.post(f"/api/tasks/{t}/close").status_code == 200
    report = a.get(f"/api/tasks/{t}/report").json()
    progress = report["参与进度"]
    assert progress["受邀评测者"] == 4
    assert progress["已完成"] == 1 and progress["进行中"] == 2 and progress["未开始"] == 1
    assert progress["已完成名单"] == ["参与者一"]
    assert progress["进行中名单"] == ["参与者二", "组织者"]
    assert progress["未开始名单"] == ["参与者三"]

    views = {s["id"]: s for s in report["样本"]}
    assert views[s1]["分母"] == 2 and views[s2]["分母"] == 2 and views[s3]["分母"] == 2
    assert {v["ID"]: v["票数"] for v in views[s1]["票数"]} == {
        tracks1[0]: 1,
        tracks1[1]: 0,
        "tie": 1,
    }
    assert {v["ID"]: v["票数"] for v in views[s3]["票数"]} == {
        tracks3[0]: 1,
        tracks3[1]: 0,
        "tie": 1,
    }
    assert views[s1]["分歧"] is True
    assert views[s2]["分歧"] is False
    assert views[s3]["分歧"] is True

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


def test_active_progress_visibility_and_no_preference_leak(env):
    """active 进度仅管理者可读，且不携带任何偏好内容；关闭后复盘同口径。"""
    app, a, _ = env
    first, first_id = reviewer(app, a, "进度甲")
    second, second_id = reviewer(app, a, "进度乙")
    third, third_id = reviewer(app, a, "进度丙")
    outsider, _ = reviewer(app, a, "进度局外")
    t, s, tracks = task(a)
    s2, tracks2 = sample_with_tracks(a, t, "秘密第二片段")
    publish(a, t, [first_id, second_id, third_id])
    first.post(f"/api/samples/{s}/rating", json={"choice": tracks[0]})
    first.post(f"/api/samples/{s2}/rating", json={"choice": tracks2[0]})
    second.post(f"/api/samples/{s}/rating", json={"choice": "tie"})
    third.post(
        f"/api/samples/{s}/comments", json={"start": 0, "end": 100, "body": "盲评中的评论"}
    )

    body = a.get(f"/api/tasks/{t}/progress")
    assert body.status_code == 200
    text = body.text
    progress = body.json()["参与进度"]
    assert (progress["受邀评测者"], progress["已完成"], progress["进行中"], progress["未开始"]) == (
        3,
        1,
        1,
        1,
    )
    # 不泄露：真实候选名、track ID、choice、评论文本都不得出现。
    assert "秘密" not in text and "第二片段" not in text
    assert tracks[0] not in text and tracks2[0] not in text
    assert "tie" not in text
    assert "盲评中的评论" not in text
    # active 期间普通成员、未授权用户、未登录均不可读。
    assert first.get(f"/api/tasks/{t}/progress").status_code == 403
    assert outsider.get(f"/api/tasks/{t}/progress").status_code == 403
    assert TestClient(app).get(f"/api/tasks/{t}/progress").status_code == 401

    a.post(f"/api/tasks/{t}/close")
    # 关闭后复盘使用同一口径；进度端点仍仅限管理者。
    report = first.get(f"/api/tasks/{t}/report").json()["参与进度"]
    assert (report["已完成"], report["进行中"], report["未开始"]) == (1, 1, 1)
    assert report["已完成名单"] == ["进度甲"]
    assert first.get(f"/api/tasks/{t}/progress").status_code == 403
    assert a.get(f"/api/tasks/{t}/progress").status_code == 200


def test_legacy_members_migrate_without_owner_obligation(env):
    """旧库无损迁移：非 owner 成员成为受邀评测者，owner 不因自动成员行受邀。"""
    app, a, folder = env
    _, first_id = reviewer(app, a, "旧成员一")
    _, second_id = reviewer(app, a, "旧成员二")
    t = a.post("/api/tasks", json={"title": "迁移任务", "mode": "development"}).json()["id"]
    sample_with_tracks(a, t, "迁移片段")
    publish(a, t, [first_id, second_id])
    a.post(f"/api/tasks/{t}/close")
    # 模拟 0.5 旧库：review_assignments 尚不存在任何行。
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        db.execute("DELETE FROM review_assignments")
    restarted = create_app(folder)
    c = TestClient(restarted)
    c.post("/api/login", json={"name": "组织者", "password": "test-only-strong-pass"})
    # 访问不破坏；受邀名单恢复为两位非 owner 成员，owner 不在其中。
    assert c.get(f"/api/tasks/{t}").status_code == 200
    progress = c.get(f"/api/tasks/{t}/report").json()["参与进度"]
    assert progress["受邀评测者"] == 2
    assert {m["名称"] for m in progress["成员"]} == {"旧成员一", "旧成员二"}
    assert progress["未开始"] == 2 and progress["未开始名单"] == ["旧成员一", "旧成员二"]
    assert not any(m["名称"] == "组织者" for m in progress["成员"])


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
    # 进度不同：active 期间管理者可读（仅数量与状态），其他人不可读。
    assert a.get(f"/api/tasks/{t}/progress").status_code == 200
    assert member.get(f"/api/tasks/{t}/progress").status_code == 403
    assert outsider.get(f"/api/tasks/{t}/progress").status_code == 403
    assert TestClient(app).get(f"/api/tasks/{t}/progress").status_code == 401
    a.post(f"/api/tasks/{t}/close")
    # 关闭后成员可查看汇总（既有规则），导出仍仅限任务管理者。
    assert member.get(f"/api/tasks/{t}/report").status_code == 200
    assert "秘密算法" in member.get(f"/api/tasks/{t}/report").text
    for path in ("export", "export.csv", "export.md"):
        assert member.get(f"/api/tasks/{t}/{path}").status_code == 403
        assert outsider.get(f"/api/tasks/{t}/{path}").status_code == 403
    assert outsider.get(f"/api/tasks/{t}/report").status_code == 403
    assert member.get(f"/api/tasks/{t}/progress").status_code == 403
    assert a.get(f"/api/tasks/{t}/progress").status_code == 200


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
    # 受邀名单只含两位被分配同事；负责人未受邀，不出现在进度中。
    assert (progress["受邀评测者"], progress["已完成"], progress["进行中"], progress["未开始"]) == (
        2,
        1,
        1,
        0,
    )
    assert progress["已完成名单"] == ["导出甲"] and progress["进行中名单"] == ["导出乙"]
    assert not any(m["名称"] == "组织者" for m in progress["成员"])
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
    assert any(row[4] == "受邀评测者" and row[6] == "2" for row in rows)
    assert any(row[4] == "进行中人数" and row[6] == "1" for row in rows)
    assert any(
        row[4] == "导出甲" and row[6] == "2" and row[7] == "2" and row[9] == "已完成"
        for row in rows
    )
    assert any(
        row[4] == "导出乙" and row[6] == "1" and row[7] == "2" and row[9] == "进行中"
        for row in rows
    )
    assert not any(row[0] == "参与进度" and row[4] == "组织者" for row in rows)
    assert any(row[5] == "残噪" and row[6] == "1" and s1 in row[9] for row in rows)

    markdown = a.get(f"/api/tasks/{t}/export.md").text
    assert "# 听鉴结果报告 · 导出内容验收" in markdown
    assert "closed" in markdown and "blind" in markdown
    assert "有效提交人数（分母）：2" in markdown
    assert "50.0%（1/2）" in markdown
    assert "仅统计根评论" in markdown and "描述性提示" in markdown
    assert "受邀评测者 2 人；已完成 1 人；进行中 1 人；未开始 0 人" in markdown
    assert "负责人自动拥有访问权限，但不自动成为受邀评测者" in markdown
    assert tracks1[0] in markdown and s1 in markdown


def test_csv_formula_injection_sanitized(env):
    """危险起始字符的文本单元必须变成 Excel 纯文本；数值单元不受影响。"""
    app, a, _ = env
    danger, danger_id = reviewer(app, a, "=HYPERLINK(\"http://evil.example\")")
    t = a.post("/api/tasks", json={"title": "=SUM(A1:A10)", "mode": "development"}).json()["id"]
    s = a.post(f"/api/tasks/{t}/samples", json={"name": "-2+3|片段"}).json()["id"]
    tracks = []
    for i, name in enumerate(
        ("@候选一", "   =SUM(1,1)", "\t普通文本", "\r普通文本", "\n普通文本")
    ):
        r = a.post(
            f"/api/samples/{s}/tracks",
            data={"name": name, "version": "v"},
            files={"file": ("x.wav", demo_wav(i, 0), "audio/wav")},
        )
        tracks.append(r.json()["id"])
    publish(a, t, [danger_id])
    danger.post(
        f"/api/samples/{s}/comments",
        json={"start": 0, "end": 100, "body": "危险标签", "tag": "=注入标签"},
    )
    danger.post(f"/api/samples/{s}/rating", json={"choice": tracks[0]})
    a.post(f"/api/tasks/{t}/close")

    raw = a.get(f"/api/tasks/{t}/export.csv").content
    assert raw[:3] == b"\xef\xbb\xbf" and b"\r\n" in raw
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    cells = {cell for row in rows for cell in row}
    # 危险文本全部加单引号前缀，Excel 按纯文本处理且保留可读内容。
    for dangerous in (
        "=SUM(A1:A10)",
        '=HYPERLINK("http://evil.example")',
        "-2+3|片段",
        "@候选一",
        "   =SUM(1,1)",
        "=注入标签",
        "\t普通文本",
        "\r普通文本",
        "\n普通文本",
    ):
        assert f"'{dangerous}" in cells, dangerous
        assert dangerous not in cells, dangerous
    # 数值单元保持数值含义，未被转义。
    vote_rows = [row for row in rows if row[0] == "逐片段偏好" and row[4] == "'@候选一"]
    formula_rows = [row for row in rows if row[0] == "逐片段偏好" and row[4] == "'   =SUM(1,1)"]
    assert formula_rows and formula_rows[0][6] == "0" and formula_rows[0][7] == "1"
    assert vote_rows and vote_rows[0][6] == "1" and vote_rows[0][7] == "1"
    assert "'1" not in cells and "'2" not in cells
    # Markdown 与 JSON 不做 Excel 专用转义。
    markdown = a.get(f"/api/tasks/{t}/export.md").text
    assert "=SUM(A1:A10)" in markdown and "'=SUM(A1:A10)" not in markdown
    data = json.loads(a.get(f"/api/tasks/{t}/export").text)
    assert data["任务"]["title"] == "=SUM(A1:A10)"


def test_unassigned_manager_cannot_rate(env):
    """未受邀 owner/admin 首次评分 403 且不产生评分行；显式受邀后恢复。"""
    app, a, root = env
    # 角色接口不提供提权到 admin（既有设计）；直接以 admin 角色创建第二管理员。
    assert (
        a.post(
            "/api/users",
            json={"name": "副管理员", "password": "second-admin-pass", "role": "admin"},
        ).status_code
        == 200
    )
    second_admin = TestClient(app)
    assert (
        second_admin.post(
            "/api/login", json={"name": "副管理员", "password": "second-admin-pass"}
        ).status_code
        == 200
    )
    second_admin_id = second_admin.get("/api/me").json()["id"]
    t, s, tracks = task(a)
    publish(a, t)
    owner_id = a.get("/api/me").json()["id"]

    for client, uid in ((a, owner_id), (second_admin, second_admin_id)):
        rejected = client.post(f"/api/samples/{s}/rating", json={"choice": tracks[0]})
        assert rejected.status_code == 403
        with sqlite3.connect(root / "workbench.sqlite3") as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM ratings WHERE sample_id=? AND user_id=?",
                    (s, uid),
                ).fetchone()[0]
                == 0
            )
    # 幂等重试语义不受影响：受邀后首次提交成功，重复同一请求返回 ok，改选被锁。
    assert (
        a.patch(
            f"/api/tasks/{t}/members", json={"users": [owner_id, second_admin_id]}
        ).status_code
        == 200
    )
    assert a.post(f"/api/samples/{s}/rating", json={"choice": tracks[0]}).status_code == 200
    assert a.post(f"/api/samples/{s}/rating", json={"choice": tracks[0]}).status_code == 200
    assert a.post(f"/api/samples/{s}/rating", json={"choice": "tie"}).status_code == 409
    a.post(f"/api/tasks/{t}/close")

    # 防御异常数据：库中未受邀评分不得静默进入新汇总（分母只追溯受邀名单）。
    outsider_id = reviewer(app, a, "异常评分者")[1]
    with sqlite3.connect(root / "workbench.sqlite3") as db:
        db.execute(
            "INSERT INTO ratings VALUES(?,?,?,?,?)",
            (s, outsider_id, tracks[1], "", "2026-01-01T00:00:00+00:00"),
        )
    report = a.get(f"/api/tasks/{t}/report").json()
    view = report["样本"][0]
    assert view["分母"] == 1
    assert {v["ID"]: v["票数"] for v in view["票数"]} == {
        tracks[0]: 1,
        tracks[1]: 0,
        "tie": 0,
    }
    assert {m["名称"] for m in report["参与进度"]["成员"]} == {"组织者", "副管理员"}


def test_legacy_owner_rating_migrates_and_counts(env):
    """旧库 owner 已有评分：重启迁移后 owner 成为受邀者且历史票保留。"""
    app, a, folder = env
    member, member_id = reviewer(app, a, "旧成员")
    t, s, tracks = task(a, "development")
    publish(a, t, [member_id])
    member.post(f"/api/samples/{s}/rating", json={"choice": tracks[0]})
    a.post(f"/api/tasks/{t}/close")
    owner_id = a.get("/api/me").json()["id"]
    # 模拟 0.5 旧库：owner 直接留下的评分行，且未出现在受邀名单中。
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        db.execute(
            "INSERT INTO ratings VALUES(?,?,?,?,?)",
            (s, owner_id, "tie", "旧库遗留", "2026-01-01T00:00:00+00:00"),
        )
        db.execute("DELETE FROM review_assignments WHERE user_id=?", (owner_id,))
    restarted = create_app(folder)
    c = TestClient(restarted)
    c.post("/api/login", json={"name": "组织者", "password": "test-only-strong-pass"})
    assert c.get(f"/api/tasks/{t}").status_code == 200
    report = c.get(f"/api/tasks/{t}/report").json()
    progress = report["参与进度"]
    assert {m["名称"]: (m["已提交片段数"], m["状态"]) for m in progress["成员"]} == {
        "旧成员": (1, "已完成"),
        "组织者": (1, "已完成"),
    }
    view = report["样本"][0]
    assert view["分母"] == 2
    assert {v["ID"]: v["票数"] for v in view["票数"]} == {
        tracks[0]: 1,
        tracks[1]: 0,
        "tie": 1,
    }


def test_csv_safe_control_characters():
    """控制字符与“前导空白+公式”必须转义；普通文本与数值不受影响。"""
    assert csv_safe("\t普通文本") == "'\t普通文本"
    assert csv_safe("\r普通文本") == "'\r普通文本"
    assert csv_safe("\n普通文本") == "'\n普通文本"
    assert csv_safe("   =SUM(1,1)") == "'   =SUM(1,1)"
    assert csv_safe("=危险") == "'=危险"
    assert csv_safe("+1") == "'+1"
    assert csv_safe("-1") == "'-1"
    assert csv_safe("@x") == "'@x"
    assert csv_safe("普通文本") == "普通文本"
    assert csv_safe(" x=y") == " x=y"
    assert csv_safe("5") == "5" and csv_safe(5) == 5 and csv_safe(0) == 0


def processing_sample(admin, t, name="对齐片段"):
    """参考宽带 + 延迟 320/衰减 0.7 的候选 + 周期纯音（会被拒绝）。"""
    rng = np.random.default_rng(7)
    n = 96000
    axis = np.arange(n)
    ref = 0.35 * rng.standard_normal(n) + 0.35 * np.sin(
        2 * np.pi * (50 * axis / 16000 + 1800 * (axis / 16000) ** 2 / 2)
    )
    ref /= np.max(np.abs(ref))
    ref *= 0.5
    cand = np.zeros(n)
    cand[320:] = ref[:-320] * 0.7
    sine = 0.5 * np.sin(2 * np.pi * 100 * axis / 16000)
    s = admin.post(f"/api/tasks/{t}/samples", json={"name": name}).json()["id"]
    ids = []
    for nm, data in (
        ("参考宽带", encode_float_wav(ref)),
        ("延迟衰减", encode_float_wav(cand)),
        ("周期纯音", encode_float_wav(sine)),
    ):
        r = admin.post(
            f"/api/samples/{s}/tracks",
            data={"name": nm, "version": "v"},
            files={"file": ("x.wav", data, "audio/wav")},
        )
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    return s, ids, ref


def test_processing_permission_state_and_input_gates(env):
    """仅管理者可在 draft 分析/应用/恢复；active/closed 409 无副作用。"""
    app, a, folder = env
    member, member_id = reviewer(app, a, "处理评测者")
    outsider, _ = reviewer(app, a, "处理局外")
    t, _s, tracks = task(a)  # demo 声音的两轨片段
    s2, ids, _ = processing_sample(a, t)

    # 分析输入校验（draft 阶段）：参考必须是当前片段内已有 track ID，
    # 任意路径或跨片段引用都被拒绝。
    assert (
        a.get(f"/api/samples/{s2}/alignment?reference={ids[1]}").status_code == 200
    )
    assert (
        a.get(f"/api/samples/{s2}/alignment?reference={tracks[0]}").status_code == 422
    )
    assert (
        a.get(f"/api/samples/{s2}/alignment?reference=../../etc/passwd").status_code
        == 422
    )
    publish(a, t, [member_id])

    def snapshot():
        rows = list(folder.joinpath("assets").iterdir())
        with sqlite3.connect(folder / "workbench.sqlite3") as db:
            n = db.execute("SELECT count(*) FROM track_processing").fetchone()[0]
        return {p.name for p in rows}, n

    before = snapshot()
    for client in (member, outsider, TestClient(app)):
        for path in (
            f"/api/samples/{s2}/alignment?reference={ids[0]}",
            f"/api/tasks/{t}/processing-summary",
        ):
            assert client.get(path).status_code in (401, 403)
        for method, path in (
            ("post", f"/api/samples/{s2}/processing"),
            ("post", f"/api/samples/{s2}/restore-processing"),
        ):
            assert client.request(method, path, json={"参考": ids[0], "处理": []}).status_code in (401, 403)
    # 管理者在 active 任务上一律 409，且无文件/数据库副作用。
    for path in (
        f"/api/samples/{s2}/alignment?reference={ids[0]}",
        f"/api/samples/{s2}/processing",
        f"/api/samples/{s2}/restore-processing",
    ):
        r = a.get(path) if path.startswith("/api/samples") and "alignment" in path else a.request(
            "POST", path, json={"参考": ids[0], "处理": [{"候选": ids[1], "对齐": True}]}
        )
        assert r.status_code == 409, (path, r.status_code)
    assert snapshot() == before

    # 处理摘要（只读）对管理者在任意状态可用。
    assert a.get(f"/api/tasks/{t}/processing-summary").status_code == 200


def test_processing_apply_restore_roundtrip_and_idempotency(env):
    _, a, folder = env
    t = a.post("/api/tasks", json={"title": "处理往返", "mode": "development"}).json()["id"]
    s, ids, _ = processing_sample(a, t)
    assets = folder / "assets"
    original_bytes = (assets / a.get(f"/api/samples/{s}").json()["tracks"][1]["path"]).read_bytes() if False else None
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        cand_path = db.execute("SELECT path FROM tracks WHERE id=?", (ids[1],)).fetchone()[0]
    original_bytes = (assets / cand_path).read_bytes()

    r = a.get(f"/api/samples/{s}/alignment?reference={ids[0]}")
    assert r.status_code == 200, r.text
    entries = {e["track_id"]: e for e in r.json()["候选"]}
    good = entries[ids[1]]
    assert good["延迟"]["可应用"] is True and good["延迟"]["lag"] == 320
    assert abs(good["响度"]["建议增益db"] - 3.0988) < 0.05  # 20·log10(1/0.7)
    bad = entries[ids[2]]
    # 纯音候选相对宽带参考＝内容不相关；多峰旁瓣场景由 test_align 单测覆盖。
    assert bad["延迟"]["可应用"] is False and bad["延迟"]["拒绝码"] == "ERR_LOW_CORRELATION"
    assert bad["响度"]["拒绝码"] == "ERR_DELAY_NOT_APPLICABLE"

    apply_body = {
        "参考": ids[0],
        "处理": [{"候选": ids[1], "对齐": True, "响度": True}],
    }
    assert a.post(f"/api/samples/{s}/processing", json=apply_body).status_code == 200
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        row = db.execute(
            "SELECT data FROM track_processing WHERE track_id=?", (ids[1],)
        ).fetchone()[0]
    proc = json.loads(row)
    assert proc["模式"] == "对齐+响度" and proc["lag"] == 320
    derived_path = assets / proc["派生文件"]
    assert derived_path.exists()
    # 原始资产字节不变。
    assert (assets / cand_path).read_bytes() == original_bytes
    # 试听切到派生资产（试听字节与派生文件一致，且与原始不同）。
    served = a.get(f"/api/audio/{ids[1]}").content
    assert served == derived_path.read_bytes()
    assert served != original_bytes
    detail = a.get(f"/api/samples/{s}").json()
    track_view = next(tr for tr in detail["tracks"] if tr["id"] == ids[1])
    assert track_view["处理"]["模式"] == "对齐+响度" and track_view["处理"]["lag"] == 320

    # 重复应用幂等：不新增文件、不新增行。
    files_before = {p.name for p in assets.iterdir()}
    assert a.post(f"/api/samples/{s}/processing", json=apply_body).status_code == 200
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM track_processing").fetchone()[0] == 1
    assert {p.name for p in assets.iterdir()} == files_before

    # 恢复原始：回到原资产，派生文件清理，再次恢复 409。
    assert a.post(f"/api/samples/{s}/restore-processing").status_code == 200
    assert a.get(f"/api/audio/{ids[1]}").content == original_bytes
    assert not derived_path.exists()
    assert a.post(f"/api/samples/{s}/restore-processing").status_code == 409


def test_processing_blocked_by_annotations(env):
    _, a, _ = env
    t = a.post("/api/tasks", json={"title": "标注保护", "mode": "development"}).json()["id"]
    s, ids, _ = processing_sample(a, t)
    apply_body = {"参考": ids[0], "处理": [{"候选": ids[1], "对齐": True, "响度": True}]}
    # 先应用，再写草稿评论（owner 可在草稿写评论），恢复/替换被阻止。
    assert a.post(f"/api/samples/{s}/processing", json=apply_body).status_code == 200
    assert (
        a.post(
            f"/api/samples/{s}/comments",
            json={"start": 0, "end": 100, "body": "草稿标注"},
        ).status_code
        == 200
    )
    blocked = a.post(f"/api/samples/{s}/processing", json=apply_body)
    assert blocked.status_code == 409
    blocked_restore = a.post(f"/api/samples/{s}/restore-processing")
    assert blocked_restore.status_code == 409


def test_processing_blind_seal_and_export_evidence(env):
    app, a, folder = env
    member, member_id = reviewer(app, a, "盲评处理员")
    t = a.post("/api/tasks", json={"title": "盲评处理任务", "mode": "blind"}).json()["id"]
    s, ids, _ = processing_sample(a, t, name="盲评片段")
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        db.execute("UPDATE tracks SET name='秘密算法' WHERE id=?", (ids[1],))
        db.execute("UPDATE tracks SET name='秘密参考' WHERE id=?", (ids[0],))
    apply_body = {"参考": ids[0], "处理": [{"候选": ids[1], "对齐": True, "响度": True}]}
    assert a.post(f"/api/samples/{s}/processing", json=apply_body).status_code == 200
    publish(a, t, [member_id])
    # active 盲评：处理参数与真实名不外泄。
    detail = member.get(f"/api/samples/{s}")
    assert detail.status_code == 200
    assert "秘密" not in detail.text
    assert "处理" not in detail.text and "lag" not in detail.text
    assert "派生资产SHA256" not in detail.text
    # closed 后按既有规则揭晓，导出包含处理证据。
    a.post(f"/api/tasks/{t}/close")
    report = member.get(f"/api/tasks/{t}/report").json()
    track = next(tr for srow in report["样本"] for tr in srow["tracks"] if tr["id"] == ids[1])
    assert track["处理口径"]["模式"] == "对齐+响度" and track["处理口径"]["lag"] == 320
    assert track["处理口径"]["增益db"] > 0 and "派生资产SHA256" in track["处理口径"]
    exported = a.get(f"/api/tasks/{t}/export")
    assert "处理" in exported.text and "派生资产SHA256" in exported.text
    csv_text = a.get(f"/api/tasks/{t}/export.csv").content.decode("utf-8-sig")
    assert "处理口径" in csv_text
    markdown = a.get(f"/api/tasks/{t}/export.md").text
    assert "处理口径" in markdown


def test_processing_summary_reports_mixed_and_rejected(env):
    _, a, _ = env
    t = a.post("/api/tasks", json={"title": "混合处理", "mode": "development"}).json()["id"]
    s, ids, _ = processing_sample(a, t)
    r = a.get(f"/api/samples/{s}/alignment?reference={ids[0]}")
    assert r.status_code == 200
    apply_body = {"参考": ids[0], "处理": [{"候选": ids[1], "对齐": True, "响度": True}]}
    assert a.post(f"/api/samples/{s}/processing", json=apply_body).status_code == 200
    summary = a.get(f"/api/tasks/{t}/processing-summary").json()
    sample = next(x for x in summary["片段"] if x["片段ID"] == s)
    assert sample["混合处理"] is True
    modes = {e["名称"]: e["模式"] for e in sample["候选"]}
    assert modes == {"参考宽带": "原始", "延迟衰减": "对齐+响度", "周期纯音": "原始"}
    rejected = {(x["候选"], x["判据"], x["拒绝码"]) for x in sample["候选"] for x in x["拒绝"]}
    assert ("周期纯音", "延迟", "ERR_LOW_CORRELATION") in rejected
    assert ("周期纯音", "响度", "ERR_DELAY_NOT_APPLICABLE") in rejected


def test_backup_includes_derived_assets(env):
    _, a, folder = env
    t = a.post("/api/tasks", json={"title": "备份派生", "mode": "development"}).json()["id"]
    s, ids, _ = processing_sample(a, t)
    apply_body = {"参考": ids[0], "处理": [{"候选": ids[1], "对齐": True, "响度": True}]}
    assert a.post(f"/api/samples/{s}/processing", json=apply_body).status_code == 200
    served = a.get(f"/api/audio/{ids[1]}").content
    result = a.get("/api/backup")
    assert result.status_code == 200
    destination = folder / "备份恢复验证"
    with zipfile.ZipFile(io.BytesIO(result.content)) as z:
        names = z.namelist()
        z.extractall(destination)
    assert any("派生" not in n for n in names)
    restored = create_app(destination)
    c = TestClient(restored)
    c.post("/api/login", json={"name": "组织者", "password": "test-only-strong-pass"})
    assert c.get(f"/api/audio/{ids[1]}").content == served


def test_processed_track_cannot_be_reference(env):
    """已处理轨不得作参考：分析/应用稳定 409 且无副作用；原始轨可继续作参考。"""
    _, a, folder = env
    t = a.post("/api/tasks", json={"title": "参考守卫", "mode": "development"}).json()["id"]
    s, ids, _ = processing_sample(a, t)
    apply_body = {"参考": ids[0], "处理": [{"候选": ids[1], "对齐": True, "响度": True}]}
    assert a.post(f"/api/samples/{s}/processing", json=apply_body).status_code == 200

    def snapshot():
        files = {p.name for p in (folder / "assets").iterdir()}
        with sqlite3.connect(folder / "workbench.sqlite3") as db:
            rows = db.execute(
                "SELECT track_id, data FROM track_processing"
            ).fetchall()
        return files, {r[0]: json.loads(r[1])["派生资产SHA256"] for r in rows}

    before = snapshot()
    analyze_body = f"/api/samples/{s}/alignment?reference={ids[1]}"
    rejected_analysis = a.get(analyze_body)
    assert rejected_analysis.status_code == 409
    assert "恢复全部原始音频" in rejected_analysis.text
    rejected_apply = a.post(
        f"/api/samples/{s}/processing",
        json={"参考": ids[1], "处理": [{"候选": ids[2], "对齐": False, "响度": True}]},
    )
    assert rejected_apply.status_code == 409
    assert "恢复全部原始音频" in rejected_apply.text
    assert snapshot() == before
    # 仍为原始的 A 继续作为参考正常工作。
    assert a.get(f"/api/samples/{s}/alignment?reference={ids[0]}").status_code == 200


class _CommitFailProxy:
    def __init__(self, conn):
        self._conn = conn

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    def commit(self):
        raise OSError("injected commit failure")

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _FakeSqlite:
    Row = sqlite3.Row

    @staticmethod
    def connect(path, timeout=15):
        return _CommitFailProxy(sqlite3.connect(path, timeout=timeout))


def test_apply_commit_failure_compensates_files_and_db(env, monkeypatch):
    """注入真实 commit 失败：应用不留无引用新文件，DB 行与试听不变。"""
    app, a, folder = env
    t = a.post("/api/tasks", json={"title": "提交失败补偿", "mode": "development"}).json()["id"]
    s, ids, _ = processing_sample(a, t)
    apply_body = {"参考": ids[0], "处理": [{"候选": ids[1], "对齐": True, "响度": True}]}
    assert a.post(f"/api/samples/{s}/processing", json=apply_body).status_code == 200
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        first_sha = json.loads(
            db.execute(
                "SELECT data FROM track_processing WHERE track_id=?", (ids[1],)
            ).fetchone()[0]
        )["派生资产SHA256"]
    served_before = a.get(f"/api/audio/{ids[1]}").content
    files_before = {p.name for p in (folder / "assets").iterdir()}

    monkeypatch.setattr(app_module, "sqlite3", _FakeSqlite)
    strict = TestClient(app, raise_server_exceptions=False)
    strict.cookies.update(a.cookies)
    # 换一种处理（只对齐）：会生成新的派生文件，随后 commit 失败。
    changed = {"参考": ids[0], "处理": [{"候选": ids[1], "对齐": True, "响度": False}]}
    assert strict.post(f"/api/samples/{s}/processing", json=changed).status_code == 500

    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        row = db.execute(
            "SELECT data FROM track_processing WHERE track_id=?", (ids[1],)
        ).fetchone()
    assert row is not None
    assert json.loads(row[0])["派生资产SHA256"] == first_sha
    assert {p.name for p in (folder / "assets").iterdir()} == files_before
    assert a.get(f"/api/audio/{ids[1]}").content == served_before


def test_restore_commit_failure_keeps_playable_asset(env, monkeypatch):
    """注入真实 commit 失败：恢复回滚，DB 引用的派生资产仍可播放。"""
    application, a, folder = env
    t = a.post("/api/tasks", json={"title": "恢复补偿", "mode": "development"}).json()["id"]
    s, ids, _ = processing_sample(a, t)
    apply_body = {"参考": ids[0], "处理": [{"候选": ids[1], "对齐": True, "响度": True}]}
    assert a.post(f"/api/samples/{s}/processing", json=apply_body).status_code == 200
    served = a.get(f"/api/audio/{ids[1]}").content
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        derived_name = json.loads(
            db.execute(
                "SELECT data FROM track_processing WHERE track_id=?", (ids[1],)
            ).fetchone()[0]
        )["派生文件"]

    monkeypatch.setattr(app_module, "sqlite3", _FakeSqlite)
    strict = TestClient(application, raise_server_exceptions=False)
    strict.cookies.update(a.cookies)
    assert strict.post(f"/api/samples/{s}/restore-processing").status_code == 500

    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        row = db.execute(
            "SELECT data FROM track_processing WHERE track_id=?", (ids[1],)
        ).fetchone()
    assert row is not None
    assert (folder / "assets" / derived_name).exists()
    assert a.get(f"/api/audio/{ids[1]}").content == served


def test_replacement_reclaims_old_derived_file(env):
    """不同处理替换同一轨：旧派生文件被回收，当前派生可播放。"""
    _, a, folder = env
    t = a.post("/api/tasks", json={"title": "替换回收", "mode": "development"}).json()["id"]
    s, ids, _ = processing_sample(a, t)
    first = {"参考": ids[0], "处理": [{"候选": ids[1], "对齐": True, "响度": True}]}
    assert a.post(f"/api/samples/{s}/processing", json=first).status_code == 200
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        first_name = json.loads(
            db.execute(
                "SELECT data FROM track_processing WHERE track_id=?", (ids[1],)
            ).fetchone()[0]
        )["派生文件"]
    served_first = a.get(f"/api/audio/{ids[1]}").content

    second = {"参考": ids[0], "处理": [{"候选": ids[1], "对齐": True, "响度": False}]}
    assert a.post(f"/api/samples/{s}/processing", json=second).status_code == 200
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        second_proc = json.loads(
            db.execute(
                "SELECT data FROM track_processing WHERE track_id=?", (ids[1],)
            ).fetchone()[0]
        )
    assert second_proc["模式"] == "对齐" and second_proc["gain_db"] == 0.0
    second_name = second_proc["派生文件"]
    assert second_name != first_name
    assert (folder / "assets" / second_name).exists()
    assert not (folder / "assets" / first_name).exists()
    assert a.get(f"/api/audio/{ids[1]}").content != served_first
    assert a.get(f"/api/audio/{ids[1]}").content == (folder / "assets" / second_name).read_bytes()


def test_edit_task_mode_requires_clean_draft(env):
    """比较模式仅在草稿且无任何标注/评分时可改；标题不受贡献限制。"""
    _, a, _ = env
    t, s, _ = task(a, "development")
    a.post(
        f"/api/samples/{s}/comments", json={"start": 0, "end": 100, "body": "已有标注"}
    )
    assert (
        a.patch(
            f"/api/tasks/{t}",
            json={"title": "改个名字", "kind": "算法版本", "mode": "development"},
        ).status_code
        == 200
    )
    detail = a.get(f"/api/tasks/{t}").json()
    assert detail["title"] == "改个名字" and detail["mode"] == "development"
    blocked = a.patch(
        f"/api/tasks/{t}",
        json={"title": "改个名字", "kind": "算法版本", "mode": "blind"},
    )
    assert blocked.status_code == 409 and "比较模式保持冻结" in blocked.text
    t2, _, _ = task(a, "development")
    assert (
        a.patch(
            f"/api/tasks/{t2}",
            json={"title": "任务二", "kind": "算法版本", "mode": "blind"},
        ).status_code
        == 200
    )
    assert a.get(f"/api/tasks/{t2}").json()["mode"] == "blind"


def test_delete_draft_sample_reclaims_exclusive_keeps_shared(env):
    """整段删除：行级清理＋独占资产回收＋共享原始资产保留可播放。"""
    _, a, folder = env
    t1 = a.post("/api/tasks", json={"title": "删除片段任务", "mode": "development"}).json()["id"]
    s1, ids1, ref_x = processing_sample(a, t1, name="待删片段")
    apply_body = {"参考": ids1[0], "处理": [{"候选": ids1[1], "对齐": True, "响度": True}]}
    assert a.post(f"/api/samples/{s1}/processing", json=apply_body).status_code == 200
    t2 = a.post("/api/tasks", json={"title": "共享任务", "mode": "development"}).json()["id"]
    s2 = a.post(f"/api/tasks/{t2}/samples", json={"name": "共享片段"}).json()["id"]
    r = a.post(
        f"/api/samples/{s2}/tracks",
        data={"name": "同字节参考", "version": "v"},
        files={"file": ("x.wav", encode_float_wav(ref_x), "audio/wav")},
    )
    assert r.status_code == 200
    shared_track = r.json()["id"]

    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        cand_path = db.execute(
            "SELECT path FROM tracks WHERE id=?", (ids1[1],)
        ).fetchone()[0]
        derived_name = json.loads(
            db.execute(
                "SELECT data FROM track_processing WHERE track_id=?", (ids1[1],)
            ).fetchone()[0]
        )["派生文件"]
    assets = folder / "assets"
    assert (assets / derived_name).exists()

    assert a.delete(f"/api/samples/{s1}").status_code == 200
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM samples WHERE id=?", (s1,)).fetchone()[0] == 0
        assert (
            db.execute("SELECT count(*) FROM tracks WHERE sample_id=?", (s1,)).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT count(*) FROM track_processing WHERE track_id=?", (ids1[1],)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute("SELECT count(*) FROM aliases WHERE sample_id=?", (s1,)).fetchone()[0]
            == 0
        )
    assert not (assets / derived_name).exists()
    assert not (assets / cand_path).exists()
    assert a.get(f"/api/audio/{shared_track}").status_code == 200
    assert a.get(f"/api/samples/{s1}").status_code == 404


def test_delete_draft_sample_blocks_contributions(env):
    """根评论、回复任一存在即拒绝；DB 与 assets 快照完全不变。"""
    _, a, folder = env
    t = a.post("/api/tasks", json={"title": "贡献保护", "mode": "development"}).json()["id"]
    s, _, _ = processing_sample(a, t)
    root = a.post(
        f"/api/samples/{s}/comments", json={"start": 0, "end": 100, "body": "根评论"}
    ).json()["id"]
    a.post(
        f"/api/samples/{s}/comments",
        json={"start": 0, "end": 100, "body": "回复", "parent": root},
    )

    def snapshot():
        files = {p.name for p in (folder / "assets").iterdir()}
        with sqlite3.connect(folder / "workbench.sqlite3") as db:
            rows = {
                table: db.execute(
                    f"SELECT count(*) FROM {table} WHERE sample_id=?", (s,)
                ).fetchone()[0]
                for table in ("comments", "tracks", "aliases")
            }
        return files, rows

    before = snapshot()
    blocked = a.delete(f"/api/samples/{s}")
    assert blocked.status_code == 409
    assert snapshot() == before


def test_sample_revision_permission_and_state_matrix(env):
    """匿名/成员/其他组织者/owner/管理员 × draft/active/closed/trashed。"""
    app, a, _ = env
    member, member_id = reviewer(app, a, "矩阵成员")
    other_org, other_org_id = reviewer(app, a, "矩阵组织者")
    a.patch(f"/api/users/{other_org_id}/role", json={"role": "organizer"})

    def make_task(status):
        t = a.post("/api/tasks", json={"title": f"矩阵-{status}", "mode": "development"}).json()["id"]
        s, ids, _ = processing_sample(a, t)
        if status in ("active", "closed"):
            publish(a, t, [member_id])
        if status == "closed":
            a.post(f"/api/tasks/{t}/close")
        if status == "trashed":
            a.request("DELETE", f"/api/tasks/{t}", json={"title": f"矩阵-{status}"})
        return t, s, ids

    for status in ("draft", "active", "closed", "trashed"):
        t, s, _ = make_task(status)
        expected_edit = 200 if status == "draft" else (404 if status == "trashed" else 409)
        not_found = status == "trashed"
        edit_body = {"title": f"矩阵-{status}-改", "kind": "算法版本", "mode": "development"}
        assert a.patch(f"/api/tasks/{t}", json=edit_body).status_code == expected_edit, status
        assert other_org.patch(f"/api/tasks/{t}", json=edit_body).status_code == (
            404 if not_found else 403
        ), status
        assert member.patch(f"/api/tasks/{t}", json=edit_body).status_code == (
            404 if not_found else 403
        ), status
        if status == "draft":
            # 草稿态为每个角色准备独立无贡献片段，避免同片段重复删除。
            for client, expected, tag in (
                (other_org, 403, "甲"),
                (member, 403, "乙"),
                (a, 200, "丙"),
            ):
                fresh = processing_sample(a, t, name=f"矩阵片段{tag}")[0]
                assert client.delete(f"/api/samples/{fresh}").status_code == expected, status
        else:
            expected_del = 404 if not_found else 409
            others_del = 404 if not_found else 403
            assert a.delete(f"/api/samples/{s}").status_code == expected_del, status
            assert other_org.delete(f"/api/samples/{s}").status_code == others_del, status
            assert member.delete(f"/api/samples/{s}").status_code == others_del, status
        prep = a.post(
            f"/api/tasks/{t}/purge/prepare", headers={"x-csrf-token": csrf(a)}
        )
        assert prep.status_code == (200 if status == "trashed" else 409), status
        if status != "trashed":
            # purge 执行入口同样受回收站状态保护（先于令牌校验）。
            assert (
                a.post(
                    f"/api/tasks/{t}/purge",
                    json={"确认令牌": "任意"},
                    headers={"x-csrf-token": csrf(a)},
                ).status_code
                == 409
            ), status
        # purge 仅管理员可用：非管理员一律 403（不泄露任务存在性）。
        assert other_org.post(f"/api/tasks/{t}/purge/prepare").status_code == 403, status
        assert member.post(f"/api/tasks/{t}/purge/prepare").status_code == 403, status
        assert TestClient(app).post(f"/api/tasks/{t}/purge/prepare").status_code == 401, status
        if status == "trashed":
            token = prep.json()["确认令牌"]
            assert (
                member.post(f"/api/tasks/{t}/purge", json={"确认令牌": token}).status_code
                == 403
            )
        if status == "draft":
            a.request("DELETE", f"/api/tasks/{t}", json={"title": f"矩阵-{status}"})


def test_purge_token_flow_and_single_use(env):
    """令牌绑定任务、一次性、错误令牌拒绝；清除后任务与接口 404。"""
    _, a, _ = env
    t = a.post("/api/tasks", json={"title": "令牌任务", "mode": "development"}).json()["id"]
    s, _, _ = processing_sample(a, t)
    a.request("DELETE", f"/api/tasks/{t}", json={"title": "令牌任务"})
    token_header = {"x-csrf-token": csrf(a)}
    prep = a.post(f"/api/tasks/{t}/purge/prepare", headers=token_header).json()
    wrong = a.post(f"/api/tasks/{t}/purge", json={"确认令牌": "不是令牌"})
    assert wrong.status_code == 403  # 无 CSRF 头
    wrong = a.post(
        f"/api/tasks/{t}/purge",
        json={"确认令牌": "不是令牌"},
        headers={"x-csrf-token": "0" * 64},
    )
    assert wrong.status_code == 403  # CSRF 错误
    # 重新 prepare 使旧令牌失效（一次性机制的一部分）。
    latest = a.post(f"/api/tasks/{t}/purge/prepare", headers=token_header).json()
    stale = a.post(
        f"/api/tasks/{t}/purge",
        json={"确认令牌": prep["确认令牌"]},
        headers=token_header,
    )
    assert stale.status_code == 403
    done = a.post(
        f"/api/tasks/{t}/purge",
        json={"确认令牌": latest["确认令牌"]},
        headers=token_header,
    )
    assert done.status_code == 200 and done.json()["已清除"] is True
    assert a.get(f"/api/tasks/{t}").status_code == 404
    assert a.get(f"/api/samples/{s}").status_code == 404
    again = a.post(
        f"/api/tasks/{t}/purge",
        json={"确认令牌": latest["确认令牌"]},
        headers=token_header,
    )
    assert again.status_code == 404


def test_purge_prepare_excludes_shared_assets(env):
    """共享 SHA 文件不得计入独占资产与预计释放字节；独占文件按精确字节计。"""
    _, a, folder = env
    ref = (np.random.default_rng(31).standard_normal(96000) * 0.2).astype(np.float32)
    shared_bytes = encode_float_wav(ref)
    unique_bytes = encode_float_wav(ref * 0.3)

    def upload(task_id, sample_name, track_name, data):
        s = a.post(f"/api/tasks/{task_id}/samples", json={"name": sample_name}).json()["id"]
        return s, a.post(
            f"/api/samples/{s}/tracks",
            data={"name": track_name, "version": "v"},
            files={"file": ("x.wav", data, "audio/wav")},
        ).json()["id"]

    t1 = a.post("/api/tasks", json={"title": "任务A", "mode": "development"}).json()["id"]
    upload(t1, "片段A", "共享轨", shared_bytes)
    t2 = a.post("/api/tasks", json={"title": "任务B", "mode": "development"}).json()["id"]
    _, track_b = upload(t2, "片段B", "共享轨", shared_bytes)
    # A 仅引用共享文件：独占资产 0、预计释放 0。
    a.request("DELETE", f"/api/tasks/{t1}", json={"title": "任务A"})
    prep = a.post(
        f"/api/tasks/{t1}/purge/prepare", headers={"x-csrf-token": csrf(a)}
    ).json()
    assert prep["独占资产数"] == 0 and prep["预计释放字节"] == 0
    assert a.post(
        f"/api/tasks/{t1}/purge",
        json={"确认令牌": prep["确认令牌"]},
        headers={"x-csrf-token": csrf(a)},
    ).status_code == 200
    # 共享文件保留，B 仍可播放。
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        shared_path = db.execute("SELECT path FROM tracks WHERE id=?", (track_b,)).fetchone()[0]
    assert (folder / "assets" / shared_path).exists()
    assert a.get(f"/api/audio/{track_b}").status_code == 200
    # A 加入独占文件后：只统计该文件的精确字节。
    t3 = a.post("/api/tasks", json={"title": "任务A独占", "mode": "development"}).json()["id"]
    _, track_u = upload(t3, "片段A独占", "独占轨", unique_bytes)
    a.request("DELETE", f"/api/tasks/{t3}", json={"title": "任务A独占"})
    prep3 = a.post(
        f"/api/tasks/{t3}/purge/prepare", headers={"x-csrf-token": csrf(a)}
    ).json()
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        unique_path = db.execute("SELECT path FROM tracks WHERE id=?", (track_u,)).fetchone()[0]
    assert prep3["独占资产数"] == 1
    assert prep3["预计释放字节"] == (folder / "assets" / unique_path).stat().st_size


def test_purge_reclaims_exclusive_keeps_shared_and_backup(env):
    """永久清除：独占资产回收、共享保留、备份不再包含已清除任务。"""
    _, a, folder = env
    t1 = a.post("/api/tasks", json={"title": "清除任务", "mode": "development"}).json()["id"]
    s1, ids1, ref_x = processing_sample(a, t1)
    apply_body = {"参考": ids1[0], "处理": [{"候选": ids1[1], "对齐": True, "响度": True}]}
    assert a.post(f"/api/samples/{s1}/processing", json=apply_body).status_code == 200
    t2 = a.post("/api/tasks", json={"title": "保留任务", "mode": "development"}).json()["id"]
    s2 = a.post(f"/api/tasks/{t2}/samples", json={"name": "共享片段"}).json()["id"]
    r = a.post(
        f"/api/samples/{s2}/tracks",
        data={"name": "同字节参考", "version": "v"},
        files={"file": ("x.wav", encode_float_wav(ref_x), "audio/wav")},
    )
    shared_track = r.json()["id"]
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        shared_path = db.execute(
            "SELECT path FROM tracks WHERE id=?", (shared_track,)
        ).fetchone()[0]
        derived_name = json.loads(
            db.execute(
                "SELECT data FROM track_processing WHERE track_id=?", (ids1[1],)
            ).fetchone()[0]
        )["派生文件"]
    a.request("DELETE", f"/api/tasks/{t1}", json={"title": "清除任务"})
    purge_headers = {"x-csrf-token": csrf(a)}
    prep = a.post(f"/api/tasks/{t1}/purge/prepare", headers=purge_headers).json()
    assert prep["片段数"] == 1 and prep["候选数"] == 3
    assert prep["预计释放字节"] > 0
    assert (
        a.post(
            f"/api/tasks/{t1}/purge",
            json={"确认令牌": prep["确认令牌"]},
            headers=purge_headers,
        ).status_code
        == 200
    )
    assets = folder / "assets"
    assert not (assets / derived_name).exists()
    assert (assets / shared_path).exists()
    assert a.get(f"/api/audio/{shared_track}").status_code == 200
    assert a.get(f"/api/tasks/{t1}").status_code == 404
    assert len(a.get("/api/tasks?deleted=true").json()) == 0
    result = a.get("/api/backup")
    with zipfile.ZipFile(io.BytesIO(result.content)) as z:
        names = z.namelist()
        assert f"assets/{derived_name}" not in names
        assert f"assets/{shared_path}" in names
        z.extractall(folder / "purge-restore")
    restored = create_app(folder / "purge-restore")
    c = TestClient(restored)
    c.post("/api/login", json={"name": "组织者", "password": "test-only-strong-pass"})
    assert c.get(f"/api/audio/{shared_track}").status_code == 200


def test_purge_commit_failure_keeps_task_and_files(env, monkeypatch):
    """注入真实 commit 失败：任务、回收站与所有文件保持原状态。"""
    application, a, folder = env
    t = a.post("/api/tasks", json={"title": "提交失败", "mode": "development"}).json()["id"]
    _, ids, _ = processing_sample(a, t)
    a.request("DELETE", f"/api/tasks/{t}", json={"title": "提交失败"})
    token = a.post(
        f"/api/tasks/{t}/purge/prepare", headers={"x-csrf-token": csrf(a)}
    ).json()["确认令牌"]
    files_before = {p.name for p in (folder / "assets").iterdir()}
    monkeypatch.setattr(app_module, "sqlite3", _FakeSqlite)
    strict = TestClient(application, raise_server_exceptions=False)
    strict.cookies.update(a.cookies)
    assert (
        strict.post(
            f"/api/tasks/{t}/purge",
            json={"确认令牌": token},
            headers={"x-csrf-token": csrf(a)},
        ).status_code
        == 500
    )
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        assert db.execute("SELECT 1 FROM tasks WHERE id=?", (t,)).fetchone()
        assert db.execute("SELECT 1 FROM deleted_tasks WHERE task_id=?", (t,)).fetchone()
        assert (
            db.execute("SELECT count(*) FROM samples WHERE task_id=?", (t,)).fetchone()[0]
            == 1
        )
    assert {p.name for p in (folder / "assets").iterdir()} == files_before
    assert a.get("/api/tasks?deleted=true").json()[0]["id"] == t
    # 回收站任务按既有语义禁止音频访问；恢复后可播放。
    assert a.get(f"/api/audio/{ids[0]}").status_code == 404
    assert a.post(f"/api/tasks/{t}/restore").status_code == 200
    assert a.get(f"/api/audio/{ids[0]}").status_code == 200


def test_sample_delete_unlink_failure_goes_to_ledger(env, monkeypatch):
    """整段删除回收失败：200 + 待清理账本，不伪回滚；重试成功后清空。"""
    _, a, folder = env
    t = a.post("/api/tasks", json={"title": "删除失败", "mode": "development"}).json()["id"]
    s, ids, _ = processing_sample(a, t)
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        cand_name = db.execute("SELECT path FROM tracks WHERE id=?", (ids[1],)).fetchone()[0]
    real_unlink = Path.unlink
    target = folder / "assets" / cand_name

    def failing_unlink(self, missing_ok=False):
        if self == target:
            raise OSError("文件被占用")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    r = a.delete(f"/api/samples/{s}")
    assert r.status_code == 200  # 不伪回滚为 500
    assert r.json()["待清理"] == [cand_name]
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        assert not db.execute("SELECT 1 FROM samples WHERE id=?", (s,)).fetchone()
        ledger = db.execute("SELECT name, last_error FROM cleanup_pending").fetchall()
    assert [x[0] for x in ledger] == [cand_name]
    assert target.exists()  # 文件与账本保留

    # 解除占用后重试：文件消失、账本清空。
    monkeypatch.setattr(Path, "unlink", real_unlink)
    retry = a.post(
        "/api/maintenance/cleanup-retry", headers={"x-csrf-token": csrf(a)}
    )
    assert retry.status_code == 200
    assert cand_name in retry.json()["已清理"]
    assert not target.exists()
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM cleanup_pending").fetchone()[0] == 0


def test_cleanup_retry_keeps_re_referenced_asset(env, monkeypatch):
    """失败后资产重新被某轨引用：重试不得删除，且该轨仍可播放。"""
    _, a, folder = env
    t = a.post("/api/tasks", json={"title": "重新引用", "mode": "development"}).json()["id"]
    s, ids, _ = processing_sample(a, t)
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        cand_name = db.execute("SELECT path FROM tracks WHERE id=?", (ids[1],)).fetchone()[0]
        cand_meta = db.execute("SELECT meta FROM tracks WHERE id=?", (ids[1],)).fetchone()[0]
    real_unlink = Path.unlink
    target = folder / "assets" / cand_name

    def failing_unlink(self, missing_ok=False):
        if self == target:
            raise OSError("文件被占用")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    assert a.delete(f"/api/samples/{s}").status_code == 200
    monkeypatch.setattr(Path, "unlink", real_unlink)
    # 失败后另一任务的新轨重新引用同一资产。
    t2 = a.post("/api/tasks", json={"title": "引用任务", "mode": "development"}).json()["id"]
    s2 = a.post(f"/api/tasks/{t2}/samples", json={"name": "引用片段"}).json()["id"]
    new_track = "f" * 32
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        db.execute(
            "INSERT INTO tracks VALUES(?,?,?,?,?,?)",
            (new_track, s2, "引用轨", "v", cand_name, cand_meta),
        )
    retry = a.post(
        "/api/maintenance/cleanup-retry", headers={"x-csrf-token": csrf(a)}
    )
    assert retry.status_code == 200
    assert retry.json()["重新被引用"] == [cand_name]
    assert target.exists()  # 不得删除
    assert a.get(f"/api/audio/{new_track}").status_code == 200
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM cleanup_pending").fetchone()[0] == 0


def test_purge_unlink_failure_goes_to_ledger_and_retry(env, monkeypatch):
    """永久清除回收失败同样入账本并可通过重试闭环。"""
    _, a, folder = env
    t = a.post("/api/tasks", json={"title": "清除失败", "mode": "development"}).json()["id"]
    _, ids, _ = processing_sample(a, t)
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        cand_name = db.execute("SELECT path FROM tracks WHERE id=?", (ids[1],)).fetchone()[0]
    real_unlink = Path.unlink
    target = folder / "assets" / cand_name

    def failing_unlink(self, missing_ok=False):
        if self == target:
            raise OSError("文件被占用")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    a.request("DELETE", f"/api/tasks/{t}", json={"title": "清除失败"})
    purge_headers = {"x-csrf-token": csrf(a)}
    prep = a.post(f"/api/tasks/{t}/purge/prepare", headers=purge_headers).json()
    done = a.post(
        f"/api/tasks/{t}/purge",
        json={"确认令牌": prep["确认令牌"]},
        headers=purge_headers,
    )
    assert done.status_code == 200
    assert done.json()["待清理"] == [cand_name]
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        assert not db.execute("SELECT 1 FROM tasks WHERE id=?", (t,)).fetchone()
        assert db.execute("SELECT count(*) FROM cleanup_pending").fetchone()[0] == 1
    monkeypatch.setattr(Path, "unlink", real_unlink)
    retry = a.post(
        "/api/maintenance/cleanup-retry", headers={"x-csrf-token": csrf(a)}
    )
    assert cand_name in retry.json()["已清理"]
    assert not target.exists()
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM cleanup_pending").fetchone()[0] == 0


def test_cleanup_ledger_survives_backup_restore(env, monkeypatch):
    """备份恢复后账本仍可安全重试（缺失文件按成功清理退出）。"""
    _, a, folder = env
    t = a.post("/api/tasks", json={"title": "账本备份", "mode": "development"}).json()["id"]
    s, ids, _ = processing_sample(a, t)
    with sqlite3.connect(folder / "workbench.sqlite3") as db:
        cand_name = db.execute("SELECT path FROM tracks WHERE id=?", (ids[1],)).fetchone()[0]
    real_unlink = Path.unlink
    target = folder / "assets" / cand_name

    def failing_unlink(self, missing_ok=False):
        if self == target:
            raise OSError("文件被占用")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    a.delete(f"/api/samples/{s}")
    assert a.post(
        "/api/maintenance/cleanup-retry", headers={"x-csrf-token": csrf(a)}
    ).status_code == 200
    result = a.get("/api/backup")
    destination = folder / "账本恢复"
    with zipfile.ZipFile(io.BytesIO(result.content)) as z:
        z.extractall(destination)
    with sqlite3.connect(destination / "workbench.sqlite3") as db:
        assert db.execute(
            "SELECT name FROM cleanup_pending WHERE name=?", (cand_name,)
        ).fetchone()
    restored = create_app(destination)
    c = TestClient(restored)
    c.post("/api/login", json={"name": "组织者", "password": "test-only-strong-pass"})
    retry = c.post("/api/maintenance/cleanup-retry", headers={"x-csrf-token": csrf(c)})
    assert retry.status_code == 200
    assert cand_name in retry.json()["已清理"]  # 缺失文件按成功清理退出
    with sqlite3.connect(destination / "workbench.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM cleanup_pending").fetchone()[0] == 0
