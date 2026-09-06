"""验证真实输出、任务冻结、匿名边界和多人持久化。"""

import io
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
