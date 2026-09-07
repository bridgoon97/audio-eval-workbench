"""从内置指南的同一份内容生成可共享的中文 Markdown 手册。"""

import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
data = json.loads((root / "src/guide-content.json").read_text(encoding="utf-8"))
lines = ["# 听鉴 · 分角色使用手册", "", f"适用版本：{data['version']}", ""]


def append(chapters):
    for chapter in chapters:
        lines.extend([f"### {chapter['title']}", "", chapter["lead"], ""])
        lines.extend(f"{i + 1}. {step}" for i, step in enumerate(chapter["steps"]))
        lines.append("")
        lines.extend(f"- {note}" for note in chapter["notes"])
        lines.append("")


for guide in data["roles"].values():
    lines.extend([f"## {guide['name']}", "", guide["intro"], ""])
    append(guide["chapters"])
lines.extend(["## 通用操作与常见问题", ""])
append(data["common"])
(root / "docs/分角色使用手册.md").write_text("\n".join(lines), encoding="utf-8")
