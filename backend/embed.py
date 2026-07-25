"""本地 embedding + 余弦召回（用于物品冷热分离的"冷物品语义召回"）。

设计要点：
- 懒加载单例：首次 recall/embed 时才加载模型，避免拖慢启动。
- 优雅降级：模型装不上/加载失败/被开关关掉时，recall() 一律返回 []，
  等价于"只用热物品、不做语义召回"，服务照常跑，绝不因此崩。
- 纯本地：用 fastembed + BGE-small-zh（离线、约百 MB、不拖 torch），
  因为部署端 embedding 服务隔在内网、本机够不着。

对外只暴露 embed() 与 recall()。
"""

import math
import os
import threading

# 环境开关：默认开。设 EMBED_ENABLED=0 可强制关闭（无模型环境跑测试用）。
ENABLED = os.getenv("EMBED_ENABLED", "1").strip() not in ("0", "false", "False", "")

# fastembed 里 BGE-small-zh 的模型名（以 list_supported_models() 实测为准）。
MODEL_NAME = os.getenv("EMBED_MODEL", "BAAI/bge-small-zh-v1.5")

# 模型缓存目录：模型（GCS tar 解压后的 fast-bge-small-zh-v1.5/）预置于此。
# 默认放 backend/data/fastembed_cache（已随 data/ 一起 gitignore，不入库）。
CACHE_DIR = os.getenv(
    "EMBED_CACHE_DIR",
    os.path.join(os.path.dirname(__file__), "data", "fastembed_cache"),
)

_model = None            # 懒加载的模型单例
_load_failed = False     # 加载失败标记，失败后不再反复重试
_lock = threading.Lock()


def _get_model():
    """懒加载模型单例。返回 None 表示不可用（已禁用或加载失败）。"""
    global _model, _load_failed
    if not ENABLED or _load_failed:
        return None
    if _model is not None:
        return _model
    with _lock:
        if _model is not None:
            return _model
        if _load_failed:
            return None
        try:
            # 强制离线：模型已预置在 CACHE_DIR，绝不触网。否则 fastembed 会先试
            # HuggingFace 在线源——大文件在本机网络环境下会挂起、拖死事件循环。
            # 离线模式下缺模型会立即抛错 → 走下方 except 降级，不会卡。
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            from fastembed import TextEmbedding  # 延迟导入，未装也不影响其余功能
            _model = TextEmbedding(model_name=MODEL_NAME, cache_dir=CACHE_DIR)
        except Exception:  # noqa: BLE001 —— 任何失败都降级，不外抛
            _load_failed = True
            return None
    return _model


def embed(texts: list[str]) -> list[list[float]]:
    """批量编码。模型不可用或入参为空时返回 []。"""
    if not texts:
        return []
    model = _get_model()
    if model is None:
        return []
    try:
        return [list(v) for v in model.embed(texts)]
    except Exception:  # noqa: BLE001
        return []


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def recall(query: str, candidates: list[str], top_k: int = 3,
           threshold: float = 0.35) -> list[int]:
    """对 query 与每个候选算余弦相似度，返回超过阈值的候选下标（按相似度降序，最多 top_k 个）。

    模型不可用、query/candidates 为空时返回 []（降级为"不召回"）。
    """
    query = (query or "").strip()
    if not query or not candidates:
        return []
    vecs = embed([query] + candidates)
    if len(vecs) != len(candidates) + 1:
        return []
    qv, cvs = vecs[0], vecs[1:]
    scored = [(i, _cosine(qv, cv)) for i, cv in enumerate(cvs)]
    scored = [(i, s) for i, s in scored if s >= threshold]
    scored.sort(key=lambda t: t[1], reverse=True)
    return [i for i, _ in scored[:top_k]]
