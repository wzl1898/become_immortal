#!/usr/bin/env python3
"""自动化游玩入口。

流程：
    新建存档 -> 生成开场 -> 循环 N 回合：
        读最新剧情 -> 玩家 Agent 生成行动 -> 提交 /api/action
        -> 等后台任务结束 -> 抓取本回合全量 trace
    -> 产出静态自包含 HTML 报告

用法：
    python auto_play.py --config examples/autoplay/smoke.json
    python auto_play.py --card orbital --turns 3 --yes

失败策略（配置项 on_turn_failure）：
    stop      整回合生成失败就停止并保留存档（默认）
    continue  记录失败后继续下一回合

单个 Agent 失败不影响流程：叙事引擎本身有 fallback，报告里按状态着色即可。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools.autoplay import config as config_mod  # noqa: E402
from tools.autoplay import player_agent, report, trace_store  # noqa: E402
from tools.autoplay.driver import Driver, DriverError, latest_narration, latest_player_action  # noqa: E402

# 本机默认走 socks 代理且可能是 socks5h:// scheme，httpx 不认。这里统一去掉 h，
# 与 AGENTS.md 的启动约定一致；玩家 Agent 的 httpx 客户端本身 trust_env=False。
for _var in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
    _value = os.getenv(_var)
    if _value and _value.startswith("socks5h://"):
        os.environ[_var] = "socks5://" + _value[len("socks5h://"):]

# no_proxy 里的 [::1] 会让 httpx 把方括号内容当成端口，抛 InvalidURL。
# 后端 llm.py 也做了同样处理；这里再兜一次，保证玩家 Agent 的请求不会中招。
for _var in ("no_proxy", "NO_PROXY"):
    _value = os.getenv(_var)
    if _value and "[" in _value:
        os.environ[_var] = ",".join(
            p for p in _value.split(",") if "[" not in p and "]" not in p
        )

# 读取项目根目录的 .env，与后端启动时的行为一致：玩家 Agent 默认继承主模型配置，
# 不应要求用户再手工 export 一遍。
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:  # dotenv 缺失时退回纯环境变量
    pass


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="自动化游玩：驱动服务、生成玩家输入、收集 trace、产出 HTML 报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="JSON 配置文件路径")
    parser.add_argument("--url", help="服务地址，默认取配置或 http://127.0.0.1:8888")
    parser.add_argument("--user", help="userid，默认取配置或 autoplay")
    parser.add_argument("--card", help="故事卡 id")
    parser.add_argument("--name", help="存档名")
    parser.add_argument("--turns", type=int, help="游玩回合数")
    parser.add_argument("--output", help="报告输出路径，支持 {ts} 占位符")
    parser.add_argument(
        "--on-turn-failure", choices=sorted(config_mod.VALID_FAILURE_MODES),
        help="整回合失败时的行为",
    )
    parser.add_argument("--resume", help="在已有存档上继续游玩，而不是新建")
    parser.add_argument("--yes", "-y", action="store_true", help="跳过成本确认")
    parser.add_argument("--dry-run", action="store_true", help="只校验配置并打印计划，不调用模型")
    return parser.parse_args(argv)


def build_config(args) -> config_mod.Config:
    data: dict = {}
    if args.config:
        path = Path(args.config).expanduser()
        if not path.is_file():
            raise config_mod.ConfigError(f"配置文件不存在：{path}")
        import json

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise config_mod.ConfigError(f"配置文件不是合法 JSON：{path}：{exc}") from exc
        if not isinstance(data, dict):
            raise config_mod.ConfigError(f"配置文件顶层必须是 JSON 对象：{path}")

    overrides = {
        "base_url": args.url,
        "user_id": args.user,
        "card": args.card,
        "save_name": args.name,
        "turns": args.turns,
        "output": args.output,
        "on_turn_failure": args.on_turn_failure,
    }
    for key, value in overrides.items():
        if value is not None:
            data[key] = value
    return config_mod.Config(data, args.config or "<cli>")


def confirm(cfg: config_mod.Config, skip: bool) -> bool:
    log("")
    log(cfg.describe())
    log("")
    if skip:
        return True
    if not sys.stdin.isatty():
        log("非交互环境，已自动继续（用 --yes 可显式跳过确认）")
        return True
    try:
        answer = input("继续？[y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def play_turn(driver: Driver, sid: str, turn: int, cfg: config_mod.Config,
              pool: trace_store.DedupPool) -> dict:
    """执行一个回合，返回该回合的记录。任何失败都记在返回值的 error 字段。"""
    record = {
        "turn": turn,
        "action": "",
        "narrative": "",
        "agents": [],
        "summary": {},
        "error": None,
        "started_at": time.time(),
    }

    try:
        snapshot = driver.load(sid)
    except DriverError as exc:
        record["error"] = f"读档失败：{exc}"
        return record

    narrative = latest_narration(snapshot.get("transcript"))
    previous = latest_player_action(snapshot.get("transcript"))

    # 1) 玩家 Agent 生成行动
    try:
        action = player_agent.generate_action(
            narrative,
            state=snapshot.get("character_state"),
            previous_action=previous,
            **{k: v for k, v in cfg.player_agent.items() if k != "model"},
            model=cfg.player_agent.get("model"),
        )
    except player_agent.PlayerAgentError as exc:
        record["error"] = f"玩家 Agent 失败：{exc}"
        return record
    record["action"] = action
    log(f"  [回合 {turn}] 玩家行动：{action}")

    # 2) 提交行动，拿正文
    try:
        result = driver.action(sid, action)
    except DriverError as exc:
        record["error"] = f"生成失败：{exc}"
        return record
    record["narrative"] = result.get("text") or ""

    # 3) 等后台任务结束再抓 trace。
    #    必须等待：超过 180 秒仍 running 的 trace 会被 reap_stale_agent_traces
    #    标成 timeout，不等就会把正常调用误读成失败。
    try:
        driver.wait_idle(sid, cfg.wait_timeout)
    except DriverError as exc:
        log(f"  [回合 {turn}] 等待后台任务失败（仍继续抓取）：{exc}")

    # 4) 抓取本回合全量 trace
    try:
        raw = driver.traces(sid, turn)
    except DriverError as exc:
        record["error"] = f"抓取 trace 失败：{exc}"
        return record
    record["agents"] = trace_store.collect_turn_traces(raw, pool)
    record["summary"] = trace_store.summarize_turn(record["agents"])
    record["elapsed"] = time.time() - record["started_at"]

    failed = record["summary"]["failed"]
    note = f"，{failed} 个 Agent 失败" if failed else ""
    log(f"  [回合 {turn}] 完成：{len(record['agents'])} 条 trace{note}，"
        f"{record['elapsed']:.1f}s")
    return record


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        cfg = build_config(args)
    except config_mod.ConfigError as exc:
        log(f"配置错误：{exc}")
        return 2

    if args.dry_run:
        log("（dry-run，不调用模型）")
        log(cfg.describe())
        return 0

    if not confirm(cfg, args.yes):
        log("已取消")
        return 130

    pool = trace_store.DedupPool()
    turns: list[dict] = []
    started = time.time()
    aborted = None
    sid = args.resume

    try:
        with Driver(cfg.base_url, cfg.user_id) as driver:
            try:
                driver.health()
            except DriverError as exc:
                log(f"服务不可用：{cfg.base_url}：{exc}")
                log("请先启动服务：cd backend && ../.venv/bin/python -m uvicorn "
                    "main:app --reload --port 8888 --timeout-graceful-shutdown 3")
                return 4

            if sid:
                log(f"续玩已有存档 {sid}")
            else:
                sid = driver.create_save(cfg.card, cfg.save_name)
                log(f"新建存档 {sid}（故事卡 {cfg.card}）")
                log("生成开场……")
                try:
                    driver.opening(sid)
                    driver.wait_idle(sid, cfg.wait_timeout)
                except DriverError as exc:
                    log(f"开场生成失败：{exc}")
                    aborted = "开场生成失败"
                else:
                    log("开场完成")

            for turn in range(1, cfg.turns + 1):
                if aborted:
                    break
                log(f"[回合 {turn}/{cfg.turns}] 规划中……")
                record = play_turn(driver, sid, turn, cfg, pool)
                turns.append(record)
                if record["error"]:
                    log(f"  [回合 {turn}] {record['error']}")
                    if cfg.on_turn_failure == "stop":
                        aborted = f"回合 {turn}：{record['error']}"
                        break
    except KeyboardInterrupt:
        log("\n已中断，保留已完成回合")
        aborted = "用户中断"
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        aborted = "框架异常"

    elapsed = time.time() - started
    failed_agents = sum(t["summary"].get("failed", 0) for t in turns)
    meta = {
        "title": f"自动游玩报告 · {cfg.card}",
        "session_id": sid or "",
        "base_url": cfg.base_url,
        "user_id": cfg.user_id,
        "card": cfg.card,
        "turns_planned": cfg.turns,
        "turns_done": len(turns),
        "trace_count": sum(len(t["agents"]) for t in turns),
        "failed_agents": failed_agents,
        "elapsed_seconds": elapsed,
        "aborted": aborted,
        "dedup": pool.stats(),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    output = cfg.output_path()
    try:
        report.build(
            meta=meta,
            turns=turns,
            pool=pool.blocks(),
            pool_stats=pool.stats(),
            output=output,
        )
    except OSError as exc:
        log(f"报告写入失败：{exc}")
        return 1

    stats = pool.stats()
    log("")
    log(f"完成 {len(turns)}/{cfg.turns} 回合，{meta['trace_count']} 条 trace，"
        f"{failed_agents} 个 Agent 失败，耗时 {elapsed:.1f}s")
    log(f"去重：{stats['raw_chars']:,} -> {stats['unique_chars']:,} 字符"
        f"（省 {stats['saved_ratio'] * 100:.1f}%）")
    if aborted:
        log(f"中断原因：{aborted}")
    log(f"报告：{output}")
    log(f"存档：{sid}")
    return 0 if not aborted else 1


if __name__ == "__main__":
    sys.exit(main())
