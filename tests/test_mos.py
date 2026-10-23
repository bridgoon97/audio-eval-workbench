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
def env(tmp_path):
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
            assert "scores" not in json.dumps(a.get(f"/api/tasks/{tid}/progress").json())
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


def test_reference_permissions_publish_gate_and_freeze(env):
    app, a, peers, tid, sid, tracks, _ = env
    assert (
        a.post(
            f"/api/tasks/{tid}/publish", json={"users": [], "alignment_confirmed": True}
        ).status_code
        == 422
    )
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
    assert a.put(f'/api/samples/{sid}/timbre-reference', json={'track_id': tracks[0]}).status_code == 200
    assert a.delete(f'/api/tracks/{tracks[0]}').status_code == 200
    assert a.post(f'/api/tasks/{tid}/publish', json={'users': [], 'alignment_confirmed': True}).status_code == 422
    assert a.delete(f'/api/samples/{sid}').status_code == 200
    assert a.request('DELETE', f'/api/tasks/{tid}', json={'title': '多维测试'}).status_code == 200
    headers = {'x-csrf-token': a.get('/api/me').json()['csrf_token']}
    prepared = a.post(f'/api/tasks/{tid}/purge/prepare', headers=headers)
    assert prepared.status_code == 200, prepared.text
    assert a.post(f'/api/tasks/{tid}/purge', headers=headers, json={'确认令牌': prepared.json()['确认令牌']}).status_code == 200


def test_legacy_without_config_defaults_to_preference(env):
    app, a, _, _, _, _, _ = env
    tid = a.post('/api/tasks', json={'title': '旧库任务'}).json()['id']
    with sqlite3.connect(app.state.database) as db:
        db.execute('DELETE FROM evaluation_config WHERE task_id=?', (tid,))
    from workbench.db import init
    init(app.state.database)
    init(app.state.database)
    assert a.get(f'/api/tasks/{tid}').json()['evaluation'] == 'preference'


def test_mos_purge_removes_score_evidence_with_task(env):
    app, a, peers, tid, sid, tracks, _ = env
    start(env)
    assert peers[0][0].post(f'/api/samples/{sid}/rating', json={'scores': scores(tracks, 4)}).status_code == 200
    assert a.post(f'/api/tasks/{tid}/close').status_code == 200
    assert a.request('DELETE', f'/api/tasks/{tid}', json={'title': '多维测试'}).status_code == 200
    headers = {'x-csrf-token': a.get('/api/me').json()['csrf_token']}
    token = a.post(f'/api/tasks/{tid}/purge/prepare', headers=headers).json()['确认令牌']
    assert a.post(f'/api/tasks/{tid}/purge', headers=headers, json={'确认令牌': token}).status_code == 200
    with sqlite3.connect(app.state.database) as db:
        assert db.execute('SELECT count(*) FROM mos_submissions').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM timbre_references').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM evaluation_config').fetchone()[0] == 0
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
