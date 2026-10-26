"""多维评分的真实 API 闭环、完整性、权限、统计与无损升级。"""

import io
import json
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from workbench.app import create_app
from workbench.audio import demo_wav
from workbench.mos import DIMENSIONS


@pytest.fixture
def env(tmp_path, request):
    app = create_app(tmp_path)
    admin = TestClient(app)
    assert (
        admin.post(
            "/api/setup",
            json={
                "name": "管理员",
                "password": "test-admin-password",
                "setup_key": app.state.setup_file.read_text(),
            },
        ).status_code
        == 200
    )
    admin.post("/api/login", json={"name": "管理员", "password": "test-admin-password"})
    csrf = admin.get("/api/me").json()["csrf_token"]
    peers = []
    for i in range(2):
        assert (
            admin.post(
                "/api/users",
                json={"name": f"评测者{i}", "password": "test-review-password"},
                headers={"x-csrf-token": csrf},
            ).status_code
            == 200
        )
        c = TestClient(app)
        c.post(
            "/api/login",
            json={"name": f"评测者{i}", "password": "test-review-password"},
        )
        peers.append((c, c.get("/api/me").json()["id"]))
    tid = admin.post(
        "/api/tasks", json={"title": "多维测试", "mode": "blind", "evaluation": "mos"}
    ).json()["id"]
    if getattr(request, "param", 1) == 1:
        # 模拟升级前创建、没有精度快照的整数任务，继续覆盖旧评分语义。
        with sqlite3.connect(app.state.database) as db:
            db.execute("DELETE FROM mos_scales WHERE task_id=?", (tid,))
    sid = admin.post(f"/api/tasks/{tid}/samples", json={"name": "语音片段"}).json()[
        "id"
    ]
    tracks = []
    for i in range(3):
        r = admin.post(
            f"/api/samples/{sid}/tracks",
            data={"name": f"秘密算法{i}", "version": "秘密版本"},
            files={"file": ("input.wav", demo_wav(i, 0), "audio/wav")},
        )
        assert r.status_code == 200, r.text
        tracks.append(r.json()["id"])
    return app, admin, peers, tid, sid, tracks, tmp_path


def start(env):
    _, a, peers, tid, sid, tracks, _ = env
    assert (
        a.put(
            f"/api/samples/{sid}/timbre-reference", json={"track_id": tracks[0]}
        ).status_code
        == 200
    )
    assert (
        a.post(
            f"/api/tasks/{tid}/publish",
            json={"users": [p[1] for p in peers], "alignment_confirmed": True},
        ).status_code
        == 200
    )


def scores(tracks, value):
    return {tr: {d["key"]: value for d in DIMENSIONS} for tr in tracks[1:]}


def test_mos_full_flow_stats_exports_and_sealing(env):
    _, a, peers, tid, sid, tracks, _ = env
    start(env)
    for c, _ in peers:
        detail = c.get(f"/api/samples/{sid}").json()
        assert detail["evaluation"] == "mos" and len(detail["dimensions"]) == 4
        assert sum(t["is_reference"] for t in detail["tracks"]) == 1
        assert "秘密" not in json.dumps(detail, ensure_ascii=False)
    for path in ("report", "export", "export.csv", "export.md"):
        assert a.get(f"/api/tasks/{tid}/{path}").status_code == 403
    for index, (c, _) in enumerate(peers):
        if index == 1:
            assert c.get(f"/api/samples/{sid}").json()["rating"] is None
            assert "scores" not in json.dumps(
                a.get(f"/api/tasks/{tid}/progress").json()
            )
        body = {"choice": "mos", "scores": scores(tracks, 1 if index == 0 else 5)}
        assert c.post(f"/api/samples/{sid}/rating", json=body).status_code == 200
        assert c.post(f"/api/samples/{sid}/rating", json=body).status_code == 200
        body["scores"][tracks[1]]["quality"] = 3
        assert c.post(f"/api/samples/{sid}/rating", json=body).status_code == 409
    progress = a.get(f"/api/tasks/{tid}/progress").json()["参与进度"]
    assert (progress["受邀评测者"], progress["已完成"]) == (2, 2)
    assert "scores" not in json.dumps(progress)
    assert a.post(f"/api/tasks/{tid}/close").status_code == 200
    report = a.get(f"/api/tasks/{tid}/report").json()
    sample = report["样本"][0]
    assert sample["票数"] == [] and sample["分歧"] is False
    assert sample["MOS"]["参考ID"] == tracks[0]
    assert len(sample["MOS"]["候选"]) == 2
    for track in sample["MOS"]["候选"]:
        for d in track["维度"]:
            assert d["均值"] == 3 and d["中位数"] == 3 and d["人数"] == 2
            assert d["标准差"] == pytest.approx(2.8284271247461903)
            assert d["分布"] == [1, 0, 0, 0, 1]
    assert {r["user_id"] for r in sample["MOS"]["原始评分"]} == {p[1] for p in peers}
    assert len(sample["ratings"]) == 2
    csv = a.get(f"/api/tasks/{tid}/export.csv")
    assert (
        csv.content.startswith(b"\xef\xbb\xbf")
        and "MOS原始评分" in csv.text
        and "MOS汇总" in csv.text
    )
    md = a.get(f"/api/tasks/{tid}/export.md").text
    assert "多维 MOS" in md and tracks[0] in md and peers[0][1] in md
    assert peers[0][0].get(f"/api/tasks/{tid}/export").status_code == 403


def test_incomplete_wrong_track_and_invalid_scores_leave_no_rows(env):
    _, a, peers, tid, sid, tracks, _ = env
    start(env)
    valid = scores(tracks, 3)
    cases = [
        {},
        {tracks[1]: valid[tracks[1]]},
        {**valid, tracks[0]: valid[tracks[1]]},
        {"foreign": valid[tracks[1]]},
    ]
    for value in (0, 6, True, 2.5, "3", None):
        bad = scores(tracks, 3)
        bad[tracks[1]]["quality"] = value
        cases.append(bad)
    bad = scores(tracks, 3)
    del bad[tracks[1]]["quality"]
    cases.append(bad)
    for bad in cases:
        assert (
            peers[0][0]
            .post(f"/api/samples/{sid}/rating", json={"scores": bad})
            .status_code
            == 422
        )
    assert (
        peers[0][0]
        .post(f"/api/samples/{sid}/rating", json={"choice": tracks[1]})
        .status_code
        == 422
    )
    assert (
        a.post(f"/api/samples/{sid}/rating", json={"scores": valid}).status_code == 403
    )
    # 进度与个人评分同时证实错误提交没有落地。
    assert a.get(f"/api/tasks/{tid}/progress").json()["参与进度"]["未开始"] == 2
    assert peers[0][0].get(f"/api/samples/{sid}").json()["rating"] is None


def test_reference_permissions_and_freeze(env):
    app, a, peers, tid, sid, tracks, _ = env
    assert (
        TestClient(app)
        .put(f"/api/samples/{sid}/timbre-reference", json={"track_id": tracks[0]})
        .status_code
        == 401
    )
    assert (
        peers[0][0]
        .put(f"/api/samples/{sid}/timbre-reference", json={"track_id": tracks[0]})
        .status_code
        == 403
    )
    assert (
        a.put(
            f"/api/samples/{sid}/timbre-reference", json={"track_id": "foreign"}
        ).status_code
        == 422
    )
    start(env)
    assert (
        a.put(
            f"/api/samples/{sid}/timbre-reference", json={"track_id": tracks[1]}
        ).status_code
        == 409
    )
    assert a.post(f"/api/tasks/{tid}/close").status_code == 200
    assert (
        peers[0][0]
        .post(f"/api/samples/{sid}/rating", json={"scores": scores(tracks, 3)})
        .status_code
        == 409
    )
    assert (
        a.put(
            f"/api/samples/{sid}/timbre-reference", json={"track_id": tracks[1]}
        ).status_code
        == 409
    )


def test_mos_concurrent_submit_one_evidence_and_no_zero_fill(env):
    _, a, peers, tid, sid, tracks, _ = env
    start(env)
    c = peers[0][0]
    body = {"scores": scores(tracks, 4)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert (
            list(
                pool.map(
                    lambda _: (
                        c.post(f"/api/samples/{sid}/rating", json=body).status_code
                    ),
                    range(4),
                )
            )
            == [200] * 4
        )
    assert a.post(f"/api/tasks/{tid}/close").status_code == 200
    sample = a.get(f"/api/tasks/{tid}/report").json()["样本"][0]
    assert len(sample["MOS"]["原始评分"]) == 1
    d = sample["MOS"]["候选"][0]["维度"][0]
    assert d["人数"] == 1 and d["均值"] == 4 and d["标准差"] is None
    assert sample["分母"] == 1


def test_mos_backup_restore_and_legacy_defaults(env, tmp_path):
    _, a, peers, tid, sid, tracks, _ = env
    legacy = a.post("/api/tasks", json={"title": "旧式偏好"}).json()["id"]
    assert a.get(f"/api/tasks/{legacy}").json()["evaluation"] == "preference"
    start(env)
    peers[0][0].post(f"/api/samples/{sid}/rating", json={"scores": scores(tracks, 4)})
    a.post(f"/api/tasks/{tid}/close")
    before = a.get(f"/api/tasks/{tid}/report").json()["样本"][0]["MOS"]
    dest = tmp_path / "restored"
    with zipfile.ZipFile(io.BytesIO(a.get("/api/backup").content)) as z:
        z.extractall(dest)
    for _ in range(2):
        app = create_app(dest)
        c = TestClient(app)
        c.post("/api/login", json={"name": "管理员", "password": "test-admin-password"})
        assert c.get(f"/api/tasks/{tid}/report").json()["样本"][0]["MOS"] == before
        assert c.get(f"/api/tasks/{legacy}").json()["evaluation"] == "preference"


def test_mos_reference_deletion_and_purge_cascade(env):
    _, a, _, tid, sid, tracks, _ = env
    assert (
        a.put(
            f"/api/samples/{sid}/timbre-reference", json={"track_id": tracks[0]}
        ).status_code
        == 200
    )
    assert a.delete(f"/api/tracks/{tracks[0]}").status_code == 200
    assert not any(t["is_reference"] for t in a.get(f"/api/samples/{sid}").json()["tracks"])
    assert a.delete(f"/api/samples/{sid}").status_code == 200
    assert (
        a.request("DELETE", f"/api/tasks/{tid}", json={"title": "多维测试"}).status_code
        == 200
    )
    headers = {"x-csrf-token": a.get("/api/me").json()["csrf_token"]}
    prepared = a.post(f"/api/tasks/{tid}/purge/prepare", headers=headers)
    assert prepared.status_code == 200, prepared.text
    assert (
        a.post(
            f"/api/tasks/{tid}/purge",
            headers=headers,
            json={"确认令牌": prepared.json()["确认令牌"]},
        ).status_code
        == 200
    )


def test_legacy_without_config_defaults_to_preference(env):
    app, a, _, _, _, _, _ = env
    tid = a.post("/api/tasks", json={"title": "旧库任务"}).json()["id"]
    with sqlite3.connect(app.state.database) as db:
        db.execute("DELETE FROM evaluation_config WHERE task_id=?", (tid,))
    from workbench.db import init

    init(app.state.database)
    init(app.state.database)
    assert a.get(f"/api/tasks/{tid}").json()["evaluation"] == "preference"


def test_mos_purge_removes_score_evidence_with_task(env):
    app, a, peers, tid, sid, tracks, _ = env
    start(env)
    assert (
        peers[0][0]
        .post(f"/api/samples/{sid}/rating", json={"scores": scores(tracks, 4)})
        .status_code
        == 200
    )
    assert a.post(f"/api/tasks/{tid}/close").status_code == 200
    assert (
        a.request("DELETE", f"/api/tasks/{tid}", json={"title": "多维测试"}).status_code
        == 200
    )
    headers = {"x-csrf-token": a.get("/api/me").json()["csrf_token"]}
    token = a.post(f"/api/tasks/{tid}/purge/prepare", headers=headers).json()[
        "确认令牌"
    ]
    assert (
        a.post(
            f"/api/tasks/{tid}/purge", headers=headers, json={"确认令牌": token}
        ).status_code
        == 200
    )
    with sqlite3.connect(app.state.database) as db:
        assert db.execute("SELECT count(*) FROM mos_submissions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM timbre_references").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM evaluation_config").fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_custom_dimensions_snapshot_freeze_and_exports(env, monkeypatch):
    from workbench import mos

    _, a, peers, tid, sid, tracks, _ = env
    dims = [
        {
            "key": "speech",
            "name": "语音可懂度",
            "question": "能否听懂语音内容？",
            "anchors": [
                "完全听不懂",
                "少数能听懂",
                "一半能听懂",
                "多数能听懂",
                "全部能听懂",
            ],
        },
        {
            "key": "overall",
            "name": "综合表现",
            "question": "综合噪声和人声表现判断",
            "anchors": ["完全不可用", "问题明显", "基本可用", "表现良好", "非常满意"],
        },
    ]
    body = {
        "title": "多维测试",
        "mode": "blind",
        "evaluation": "mos",
        "dimensions": dims,
    }
    assert a.patch(f"/api/tasks/{tid}", json=body).status_code == 200
    assert a.get(f"/api/tasks/{tid}").json()["dimensions"] == dims
    # 不靠当下代码中的默认常量解释历史任务，真正从数据库快照读取。
    monkeypatch.setattr(
        mos, "DIMENSIONS", [{"key": "wrong", "name": "错误的新默认量表"}]
    )
    assert a.get(f"/api/samples/{sid}").json()["dimensions"] == dims
    start(env)
    modified = json.loads(json.dumps(body))
    modified["dimensions"][0]["anchors"][0] = "被事后改写"
    assert a.patch(f"/api/tasks/{tid}", json=modified).status_code == 409
    assert (
        peers[0][0]
        .post(f"/api/samples/{sid}/rating", json={"scores": scores(tracks, 4)})
        .status_code
        == 422
    )
    custom = {t: {"speech": 2, "overall": 5} for t in tracks[1:]}
    assert (
        peers[0][0]
        .post(f"/api/samples/{sid}/rating", json={"scores": custom})
        .status_code
        == 200
    )
    assert a.post(f"/api/tasks/{tid}/close").status_code == 200
    report = a.get(f"/api/tasks/{tid}/report").json()
    assert report["任务"]["dimensions"] == dims
    assert [d["均值"] for d in report["样本"][0]["MOS"]["候选"][0]["维度"]] == [2, 5]
    for fmt in ("export", "export.csv", "export.md"):
        text = a.get(f"/api/tasks/{tid}/{fmt}").text
        assert "完全听不懂" in text and "非常满意" in text and "被事后改写" not in text


@pytest.mark.parametrize(
    "invalid",
    [
        [],
        [{"key": "x", "name": "测试", "anchors": ["差"]}],
        [{"key": "x", "name": "  ", "anchors": ["差"] * 5}],
        [{"key": "x", "name": "测试", "anchors": [" "] * 5}],
        [{"key": "x", "name": "重复", "anchors": ["差"] * 5}] * 2,
        [{"key": "bad key", "name": "测试", "anchors": ["差"] * 5}],
        [{"key": f"x{i}", "name": str(i), "anchors": ["差"] * 5} for i in range(9)],
    ],
)
def test_custom_dimension_validation_before_creation(env, invalid):
    _, a, _, _, _, _, _ = env
    before = a.get("/api/tasks").json()
    response = a.post(
        "/api/tasks",
        json={"title": "无效量表", "evaluation": "mos", "dimensions": invalid},
    )
    assert response.status_code == 422
    assert a.get("/api/tasks").json() == before


def test_fixed_mos_trial_database_migrates_scale_snapshot(env):
    app, a, _, tid, _, _, _ = env
    from workbench.db import init

    expected = a.get(f"/api/tasks/{tid}").json()["dimensions"]
    with sqlite3.connect(app.state.database) as db:
        db.execute("ALTER TABLE evaluation_config DROP COLUMN dimensions")
    init(app.state.database)
    init(app.state.database)
    assert a.get(f"/api/tasks/{tid}").json()["dimensions"] == expected


def test_custom_scale_edit_blocks_existing_draft_contribution(env):
    _, a, _, tid, sid, _, _ = env
    original = a.get(f"/api/tasks/{tid}").json()["dimensions"]
    assert (
        a.post(
            f"/api/samples/{sid}/comments",
            json={"start": 0, "end": 16000, "body": "草稿中的已有贡献"},
        ).status_code
        == 200
    )
    changed = json.loads(json.dumps(original))
    changed[0]["anchors"][0] = "事后改变口径"
    response = a.patch(
        f"/api/tasks/{tid}",
        json={
            "title": "多维测试",
            "mode": "blind",
            "evaluation": "mos",
            "dimensions": changed,
        },
    )
    assert response.status_code == 409
    assert a.get(f"/api/tasks/{tid}").json()["dimensions"] == original


def test_mos_optional_reference_full_flow(env):
    """无参考时所有轨都必须评分，支持设定后取消，结果保留完整证据。"""
    _, a, peers, tid, sid, tracks, _ = env
    dimensions = [
        {
            "key": "quality",
            "name": "音质",
            "anchors": ["很差", "较差", "一般", "较好", "很好"],
        }
    ]
    assert (
        a.patch(
            f"/api/tasks/{tid}",
            json={"title": "无参考音质评分", "mode": "blind", "evaluation": "mos", "dimensions": dimensions},
        ).status_code
        == 200
    )
    assert (
        a.put(
            f"/api/samples/{sid}/timbre-reference", json={"track_id": tracks[0]}
        ).status_code
        == 200
    )
    assert (
        a.put(
            f"/api/samples/{sid}/timbre-reference", json={"track_id": None}
        ).status_code
        == 200
    )
    assert (
        a.post(
            f"/api/tasks/{tid}/publish",
            json={"users": [peers[0][1]], "alignment_confirmed": True},
        ).status_code
        == 200
    )
    client = peers[0][0]
    detail = client.get(f"/api/samples/{sid}").json()
    assert not any(t["is_reference"] for t in detail["tracks"])
    values = {tr: {"quality": i + 2} for i, tr in enumerate(tracks)}
    assert (
        client.post(
            f"/api/samples/{sid}/rating",
            json={"scores": {tr: values[tr] for tr in tracks[1:]}},
        ).status_code
        == 422
    )
    assert (
        client.post(f"/api/samples/{sid}/rating", json={"scores": values}).status_code
        == 200
    )
    assert (
        a.put(
            f"/api/samples/{sid}/timbre-reference", json={"track_id": None}
        ).status_code
        == 409
    )
    assert a.post(f"/api/tasks/{tid}/close").status_code == 200
    report = a.get(f"/api/tasks/{tid}/report").json()
    result = report["样本"][0]["MOS"]
    assert result["参考ID"] is None
    assert {t["ID"] for t in result["候选"]} == set(tracks)
    for track in result["候选"]:
        assert track["维度"][0]["均值"] == values[track["ID"]]["quality"]
        assert track["维度"][0]["人数"] == 1
    for suffix in ["export", "export.csv", "export.md"]:
        response = a.get(f"/api/tasks/{tid}/{suffix}")
        assert response.status_code == 200
        assert "秘密算法0" in response.text


@pytest.mark.parametrize("env", [0.1], indirect=True)
def test_slider_precision_stats_and_retry(env):
    _, a, peers, tid, sid, tracks, _ = env
    assert a.get(f"/api/tasks/{tid}").json()["score_step"] == 0.1
    start(env)
    client = peers[0][0]
    assert client.get(f"/api/samples/{sid}").json()["score_step"] == 0.1
    for value in [1.15, 0.8, 5.2, True, "3.3"]:
        assert client.post(f"/api/samples/{sid}/rating", json={"scores": scores(tracks, value)}).status_code == 422
    values = scores(tracks, 3.3)
    assert client.post(f"/api/samples/{sid}/rating", json={"scores": values}).status_code == 200
    assert client.post(f"/api/samples/{sid}/rating", json={"scores": values}).status_code == 200
    assert peers[1][0].post(f"/api/samples/{sid}/rating", json={"scores": scores(tracks, 4)}).status_code == 200
    assert a.post(f"/api/tasks/{tid}/close").status_code == 200
    report = a.get(f"/api/tasks/{tid}/report").json()
    dimension = report["样本"][0]["MOS"]["候选"][0]["维度"][0]
    assert dimension["均值"] == 3.65
    assert dimension["中位数"] == 3.65
    assert dimension["标准差"] == pytest.approx(0.7 / 2 ** 0.5)
    assert len(dimension["分布"]) == 41
    assert dimension["分布分值"][23] == 3.3
    assert dimension["分布"][23] == 1 and sum(dimension["分布"]) == 2
    _, _, _, _, _, _, root = env
    restored_app = create_app(root)
    restored_client = TestClient(restored_app)
    restored_client.post("/api/login", json={"name": "管理员", "password": "test-admin-password"})
    restored_report = restored_client.get(f"/api/tasks/{tid}/report").json()
    assert restored_report["任务"]["score_step"] == 0.1
    assert restored_report["样本"] == report["样本"]
    for suffix in ["export", "export.csv", "export.md"]:
        assert "3.3" in a.get(f"/api/tasks/{tid}/{suffix}").text


def test_legacy_integer_scale_and_results_survive_restart(env):
    app, a, peers, tid, sid, tracks, root = env
    assert a.get(f"/api/tasks/{tid}").json()["score_step"] == 1
    start(env)
    assert peers[0][0].post(f"/api/samples/{sid}/rating", json={"scores": scores(tracks, 3.3)}).status_code == 422
    assert peers[0][0].post(f"/api/samples/{sid}/rating", json={"scores": scores(tracks, 4)}).status_code == 200
    assert a.post(f"/api/tasks/{tid}/close").status_code == 200
    before = a.get(f"/api/tasks/{tid}/report").json()
    with sqlite3.connect(app.state.database) as db:
        raw_before = db.execute("SELECT scores FROM mos_submissions").fetchall()
    upgraded = create_app(root)
    c = TestClient(upgraded)
    c.post("/api/login", json={"name": "管理员", "password": "test-admin-password"})
    after = c.get(f"/api/tasks/{tid}/report").json()
    assert after["样本"] == before["样本"]
    assert after["任务"]["score_step"] == 1
    assert len(after["样本"][0]["MOS"]["候选"][0]["维度"][0]["分布"]) == 5
    with sqlite3.connect(upgraded.state.database) as db:
        assert db.execute("SELECT scores FROM mos_submissions").fetchall() == raw_before


def test_active_legacy_upgrade_keeps_votes_and_allows_fraction(env):
    app, _, peers, _, sid, tracks, root = env
    start(env)
    assert peers[0][0].post(f"/api/samples/{sid}/rating", json={"scores": scores(tracks, 4)}).status_code == 200
    with sqlite3.connect(app.state.database) as db:
        old_rows = db.execute("SELECT user_id,scores FROM mos_submissions").fetchall()
    upgraded = create_app(root)
    client = TestClient(upgraded)
    client.post("/api/login", json={"name": "评测者1", "password": "test-review-password"})
    assert client.get(f"/api/samples/{sid}").json()["score_step"] == 0.1
    assert client.post(f"/api/samples/{sid}/rating", json={"scores": scores(tracks, 3.3)}).status_code == 200
    with sqlite3.connect(upgraded.state.database) as db:
        assert db.execute("SELECT user_id,scores FROM mos_submissions WHERE user_id=?", (peers[0][1],)).fetchall() == old_rows
