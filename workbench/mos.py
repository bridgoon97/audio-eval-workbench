"""多维评分：固定锚点、完整提交校验与逐片段描述统计。"""

import json
import statistics

from fastapi import HTTPException

DIMENSIONS = [
    {
        "key": "noise_reduction",
        "name": "降噪",
        "question": "背景噪声抑制得是否充分？",
        "anchors": ["非常不足", "不足", "一般", "充分", "非常充分"],
    },
    {
        "key": "timbre_similarity",
        "name": "音色相似度",
        "question": "与参考音色有多接近？",
        "anchors": ["完全不像", "不太接近", "一般", "接近", "非常接近"],
    },
    {
        "key": "quality",
        "name": "音质",
        "question": "整体是否自然、清晰且无失真？",
        "anchors": ["非常差", "较差", "一般", "良好", "优秀"],
    },
    {
        "key": "intelligibility",
        "name": "可懂度",
        "question": "语音内容是否容易听清？",
        "anchors": ["完全听不清", "多数听不清", "部分听清", "多数听清", "完全听清"],
    },
]
BOUNDARY = "多维 1–5 分主观评分；指标及 1–5 分文字锚点按任务量表快照解释，参考音色不评分。均值为逐片段逐候选逐维度算术平均；标准差为样本标准差，少于两人不计算。不跨片段混合，不合成总分，不作显著性或标准测试合规声明。"


def method(db, task_id):
    row = db.execute(
        "SELECT method FROM evaluation_config WHERE task_id=?", (task_id,)
    ).fetchone()
    return row[0] if row else "preference"


def get_dimensions(db, task_id):
    row = db.execute(
        "SELECT dimensions FROM evaluation_config WHERE task_id=?", (task_id,)
    ).fetchone()
    return json.loads(row[0]) if row and row[0] else []


def normalize_dimensions(values):
    raw = [d.model_dump() for d in values] if values is not None else DIMENSIONS
    dims = [
        {
            **d,
            "name": d["name"].strip(),
            "question": d["question"].strip(),
            "anchors": [a.strip() for a in d["anchors"]],
        }
        for d in raw
    ]
    if len({d["key"] for d in dims}) != len(dims) or len(
        {d["name"] for d in dims}
    ) != len(dims):
        raise HTTPException(422, "指标标识和名称不能重复")
    if any(
        not d["name"].strip() or any(not a.strip() for a in d["anchors"]) for d in dims
    ):
        raise HTTPException(422, "指标名称与每个档位的说明不能为空白")
    return dims


def reference(db, sample_id):
    row = db.execute(
        "SELECT track_id FROM timbre_references WHERE sample_id=?", (sample_id,)
    ).fetchone()
    return row[0] if row else None


def validate_scores(db, sample_id, scores):
    ref = reference(db, sample_id)
    ids = {
        r[0]
        for r in db.execute("SELECT id FROM tracks WHERE sample_id=?", (sample_id,))
    } - {ref}
    if not ids:
        raise HTTPException(422, "请先添加待评分候选")
    if not isinstance(scores, dict) or set(scores) != ids:
        raise HTTPException(422, "必须为全部待测候选评分；参考音色不参与评分")
    task_id = db.execute(
        "SELECT task_id FROM samples WHERE id=?", (sample_id,)
    ).fetchone()[0]
    keys = {d["key"] for d in get_dimensions(db, task_id)}
    for values in scores.values():
        if not isinstance(values, dict) or set(values) != keys:
            raise HTTPException(422, "每个候选须完整填写本任务的全部指标")
        if any(type(v) is not int or not 1 <= v <= 5 for v in values.values()):
            raise HTTPException(422, "分值须为 1–5 的整数")
    return json.dumps(scores, sort_keys=True, ensure_ascii=False)


def summary(db, task_id):
    result = {}
    for sample in db.execute("SELECT id FROM samples WHERE task_id=?", (task_id,)):
        sid = sample[0]
        ref = reference(db, sid)
        raw = [
            dict(r)
            for r in db.execute(
                "SELECT m.user_id,u.name author,m.scores FROM mos_submissions m JOIN users u ON u.id=m.user_id "
                "JOIN review_assignments a ON a.user_id=m.user_id AND a.task_id=? WHERE m.sample_id=? ORDER BY m.user_id",
                (task_id, sid),
            )
        ]
        for row in raw:
            row["scores"] = json.loads(row["scores"])
        tracks = []
        for tr in db.execute(
            "SELECT id,name FROM tracks WHERE sample_id=? ORDER BY rowid", (sid,)
        ):
            if tr["id"] == ref:
                continue
            dimensions = []
            for d in get_dimensions(db, task_id):
                values = [r["scores"][tr["id"]][d["key"]] for r in raw]
                dimensions.append(
                    {
                        **d,
                        "人数": len(values),
                        "均值": statistics.mean(values) if values else None,
                        "中位数": statistics.median(values) if values else None,
                        "标准差": statistics.stdev(values) if len(values) > 1 else None,
                        "分布": [values.count(i) for i in range(1, 6)],
                    }
                )
            tracks.append({"ID": tr["id"], "名称": tr["name"], "维度": dimensions})
        result[sid] = {"参考ID": ref, "候选": tracks, "原始评分": raw}
    return result
