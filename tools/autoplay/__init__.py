"""自动化游玩框架：驱动服务、生成玩家输入、收集 Agent trace、产出静态报告。

本包位于 backend 之外，是外部脚手架：它只通过 HTTP/SSE 与服务交互，不 import
game/store/llm，因此不会污染叙事引擎的 Agent 拓扑，也不会在被 --reload 监视的
后端文件里触发重启。
"""
