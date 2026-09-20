"""生成自包含的静态 HTML 报告。

设计要点：
- 全量数据内联进 HTML，一个文件即可归档、分享，不依赖服务。
- 数据放在 <script type="application/json"> 里，切到某个 Agent 才把正文注入 DOM，
  避免几十条 trace 一次性铺开导致浏览器卡顿。这不削减任何数据。
- 消息正文走去重池：相同内容只存一份，各位置按 sha256 引用（口径 A）。
"""

from __future__ import annotations

import html
import json
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent / "templates" / "report.html"


def _json_for_script(payload) -> str:
    """把 JSON 安全地嵌进 <script> 块。

    JSON 里的 "</script>" 会提前结束脚本块，< 和 > 也做转义以防解析歧义。
    """
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def build(
    *,
    meta: dict,
    turns: list[dict],
    pool: dict,
    pool_stats: dict,
    output: Path,
) -> Path:
    """渲染报告并写入 output。返回写入路径。"""
    payload = {
        "meta": meta,
        "turns": turns,
        "blocks": pool,
        "dedup": pool_stats,
    }
    template = TEMPLATE.read_text(encoding="utf-8")
    document = template.replace("__REPORT_TITLE__", html.escape(meta.get("title", "自动游玩报告")))
    document = document.replace("__REPORT_DATA__", _json_for_script(payload))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")
    return output
