"""配置加载与校验。

配置是一份 JSON，字段见 docs/AUTOPLAY.md。这里只做校验和默认值填充，不做 IO
之外的事情；成本预估也放在这里，因为它纯粹是回合数与 Agent 数的算术。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# 每回合大致会调用多少个 LLM（导演层 8~10 个 + 叙事/观察/记忆等后台任务）。
# 实测真实存档：director_pacing / progression / payoff / skeleton / hook / audit
# 每轮必调，event / causal / viewpoint 视事件状态，另有 narrative、memory_extract、
# narrative_observer 等后台任务。取 10 作为保守估计。
AGENTS_PER_TURN = 10

DEFAULTS = {
    "base_url": "http://127.0.0.1:8888",
    "user_id": "autoplay",
    "card": "xiuxian",
    "save_name": "自动游玩",
    "turns": 5,
    "on_turn_failure": "stop",
    "wait_timeout": 180,
    "output": "reports/autoplay-{ts}.html",
    "player_agent": {
        "model": None,
        "temperature": 0.9,
        "max_tokens": 200,
        "timeout": 60,
    },
}

VALID_FAILURE_MODES = {"stop", "continue"}


class ConfigError(Exception):
    """配置本身不合法。"""


class Config:
    def __init__(self, data: dict, source: str = "<memory>"):
        self.source = source
        merged = {**DEFAULTS, **data}
        player = {**DEFAULTS["player_agent"], **(data.get("player_agent") or {})}
        merged["player_agent"] = player

        self.base_url = str(merged["base_url"]).rstrip("/")
        self.user_id = str(merged["user_id"])
        self.card = str(merged["card"])
        self.save_name = merged["save_name"]
        self.turns = _positive_int(merged["turns"], "turns")
        self.on_turn_failure = str(merged["on_turn_failure"])
        self.wait_timeout = _positive_int(merged["wait_timeout"], "wait_timeout")
        self.output = str(merged["output"])
        self.player_agent = player
        self.seed_action = merged.get("seed_action")
        self._validate()

    def _validate(self) -> None:
        if self.on_turn_failure not in VALID_FAILURE_MODES:
            raise ConfigError(
                f"on_turn_failure 必须是 {sorted(VALID_FAILURE_MODES)} 之一，"
                f"当前为 {self.on_turn_failure!r}"
            )
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigError(f"base_url 必须以 http:// 或 https:// 开头：{self.base_url!r}")
        # user_id 受服务端 _USER_ID_RE 约束，提前拦截比拿到 400 更清楚。
        import re

        if not re.fullmatch(r"[A-Za-z0-9._%~-]{1,64}", self.user_id):
            raise ConfigError(
                f"user_id 只允许字母数字和 . _ % ~ - ，长度 1~64：{self.user_id!r}"
            )

    def output_path(self) -> Path:
        """把 {ts} 占位符替换成时间戳，返回解析后的路径。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return Path(self.output.replace("{ts}", stamp)).expanduser()

    def estimate(self) -> dict:
        requests = self.turns * AGENTS_PER_TURN
        return {
            "turns": self.turns,
            "agents_per_turn": AGENTS_PER_TURN,
            "requests": requests,
            "player_requests": self.turns,
            "total_requests": requests + self.turns,
        }

    def describe(self) -> str:
        est = self.estimate()
        return (
            f"服务 {self.base_url} / 用户 {self.user_id}\n"
            f"故事卡 {self.card}，存档名 {self.save_name!r}\n"
            f"回合数 {self.turns}，预计 LLM 请求约 {est['total_requests']} 次"
            f"（叙事引擎 ~{est['requests']} + 玩家 Agent {est['player_requests']}）\n"
            f"回合失败策略 {self.on_turn_failure}，报告输出 {self.output_path()}"
        )


def _positive_int(value, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field} 必须是整数，当前为 {value!r}") from exc
    if number < 1:
        raise ConfigError(f"{field} 必须大于 0，当前为 {number}")
    return number


def load(path: str | os.PathLike | None) -> Config:
    """读取配置文件；path 为 None 时使用纯默认值。"""
    if path is None:
        return Config({}, "<defaults>")
    target = Path(path).expanduser()
    if not target.is_file():
        raise ConfigError(f"配置文件不存在：{target}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{target}：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是 JSON 对象：{target}")
    return Config(data, str(target))
