"""论文排序：API 分数粗筛 → Cross-Encoder 精排 → 关键词加分 → 短摘要降权。

FAISS 已于 2026-05-25 从主流程移除。模型缓存优先，缺失时尝试下载；
加载或预测失败时回退到 API 分数。
"""

import logging
import math
import os
import threading
import time
import concurrent.futures
from contextvars import copy_context
from paperpilot.config import load_config
from paperpilot.agent_runtime import checkpoint
from paperpilot.search_metrics import timed_stage, count
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import numpy as np
import torch
from sentence_transformers import CrossEncoder

logger = logging.getLogger(__name__)

# Cross-encoder 模型：mxbai-rerank-base-v2（Qwen2-based，367M params，942MB）
_CE_PATH = str(Path.home() / ".cache/modelscope/mixedbread-ai/mxbai-rerank-base-v2")
_CE_NAME = "mixedbread-ai/mxbai-rerank-base-v2"
_CE_MAX_LENGTH = 512  # 截断长文本，防止 O(n²) 注意力爆炸
_CE_LOAD_TIMEOUT = 180
_CE_PREDICT_TIMEOUT = 300
_cross_encoder = None
_ce_lock = threading.RLock()
_ce_prediction_lock = threading.Lock()
_ce_users = 0
_ce_timer = None
_ce_epoch = 0
_ce_unload_pending = False
_ce_load_future = None


def _cancel_idle_locked():
    global _ce_timer, _ce_epoch
    _ce_epoch += 1
    if _ce_timer is not None:
        _ce_timer.cancel()
        _ce_timer = None


def _get_cross_encoder():
    """Reuse one model/load future; loading waits remain cancellable."""
    global _ce_load_future, _cross_encoder
    checkpoint()
    with _ce_lock:
        _cancel_idle_locked()
        if _cross_encoder is not None:
            count("ce_model_reused")
            return _cross_encoder
        if _ce_load_future is None:
            def load():
                with timed_stage("ce_load"):
                    path = _CE_PATH if Path(_CE_PATH).exists() else _CE_NAME
                    old_hf = old_tr = None
                    if path == _CE_NAME:
                        old_hf = os.environ.pop("HF_HUB_OFFLINE", None)
                        old_tr = os.environ.pop("TRANSFORMERS_OFFLINE", None)
                    try:
                        return CrossEncoder(path, max_length=_CE_MAX_LENGTH, device="cpu",
                                            model_kwargs={"torch_dtype": torch.float32})
                    finally:
                        if old_hf is not None:
                            os.environ["HF_HUB_OFFLINE"] = old_hf
                        if old_tr is not None:
                            os.environ["TRANSFORMERS_OFFLINE"] = old_tr
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            _ce_load_future = pool.submit(copy_context().run, load)
            def loaded(done):
                # A cancelled/timed-out caller must not leave a completed model
                # resident in an abandoned future indefinitely.
                with _ce_lock:
                    if _ce_load_future is done:
                        release_cross_encoder()
            _ce_load_future.add_done_callback(loaded)
            pool.shutdown(wait=False)
            count("ce_model_load")
        future = _ce_load_future
    deadline = time.monotonic() + _CE_LOAD_TIMEOUT
    try:
        while True:
            checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                count("ce_load_timeout")
                return None  # Retain the in-flight load; never start a duplicate.
            try:
                model = future.result(timeout=min(.1, remaining))
                break
            except concurrent.futures.TimeoutError:
                if future.done():
                    raise  # The worker itself failed with TimeoutError.
                continue
        checkpoint()
        with _ce_lock:
            if _ce_load_future is future:
                _cross_encoder = model
                _ce_load_future = None
            return model
    except Exception:
        with _ce_lock:
            if _ce_load_future is future and future.done():
                _ce_load_future = None
        logger.warning("Cross-encoder load failed; using API scores")
        return None


def _api_score(value) -> float:
    """Missing or non-finite source scores use the existing neutral default."""
    try:
        score = float(value)
        return score if math.isfinite(score) else 0.5
    except (TypeError, ValueError, OverflowError):
        return 0.5


def rerank_with_cross_encoder(
    query: str,
    results: list[tuple[dict, float]],
    top_k: int = 20,
) -> list[tuple[dict, float]]:
    """用 cross-encoder 对粗筛结果精排。

    安全措施：
    - max_length=512 已在模型加载时设置，防止长序列 OOM
    - 文本在拼接前截断到 3000 字符，双重保险
    - predict() 有 300s 超时保护，超时回退到 API 分数排序
    """
    if not results:
        return []
    results = [(paper, _api_score(score)) for paper, score in results]
    global _ce_users
    checkpoint()
    with _ce_lock:
        _ce_users += 1
    try:
        ce = _get_cross_encoder()
    except BaseException:
        with _ce_lock:
            _ce_users -= 1
        release_cross_encoder()
        raise
    if ce is None:
        with _ce_lock:
            _ce_users -= 1
        release_cross_encoder()
        scores_arr = np.array([s for _, s in results])
        min_s, max_s = scores_arr.min(), scores_arr.max()
        if max_s > min_s:
            normalized = [(p, float((s - min_s) / (max_s - min_s))) for p, s in results]
        else:
            normalized = [(p, 0.5) for p, _ in results]
        normalized.sort(key=lambda x: -x[1])
        return normalized[:top_k]

    # 截断保护：标题最多 300 字符，摘要最多 2500 字符（约 500 tokens）
    def _paper_text(p: dict) -> str:
        title = (p.get("title") or "").strip()[:300]
        abstract = (p.get("abstract") or "").strip()[:2500]
        return f"{title}. {abstract}" if title else abstract

    try:
        pairs = [(query[:2000], _paper_text(p)) for p, _ in results]
    except BaseException:
        with _ce_lock:
            _ce_users -= 1
        release_cross_encoder()
        raise

    import concurrent.futures

    def _predict():
        global _ce_users
        try:
            # Serialize model inference from simultaneous search/library calls.
            while not _ce_prediction_lock.acquire(timeout=.1):
                checkpoint()
            try:
                checkpoint()
                with timed_stage("ce_predict"):
                    value = ce.predict(pairs, show_progress_bar=False)
                checkpoint()
                return value
            finally:
                _ce_prediction_lock.release()
        finally:
            with _ce_lock:
                _ce_users -= 1
            release_cross_encoder()

    try:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    except BaseException:
        with _ce_lock:
            _ce_users -= 1
        release_cross_encoder()
        raise
    submitted = False
    try:
        future = executor.submit(copy_context().run, _predict)
        submitted = True
        deadline = time.monotonic() + _CE_PREDICT_TIMEOUT
        while True:
            checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise concurrent.futures.TimeoutError()
            try:
                scores = future.result(timeout=min(.1, remaining))
                break
            except concurrent.futures.TimeoutError:
                if future.done():
                    raise
                continue
        checkpoint()
    except concurrent.futures.TimeoutError:
        count("ce_predict_timeout")
        results.sort(key=lambda x: -x[1])
        return results[:top_k]
    except Exception:
        logger.warning("Cross-encoder prediction failed; using API scores")
        results.sort(key=lambda x: -x[1])
        return results[:top_k]
    finally:
        if not submitted:
            with _ce_lock:
                _ce_users -= 1
            release_cross_encoder()
        # The worker retains its model lease even after cancellation/timeout.
        executor.shutdown(wait=False)

    # Sigmoid normalization: preserves score differentiation
    # Unlike min-max, sigmoid doesn't force the best paper to exactly 1.0
    scores = 1 / (1 + np.exp(-scores))

    reranked = sorted(
        zip([p for p, _ in results], scores),
        key=lambda x: -x[1],
    )
    return reranked[:top_k]


# ── 关键词匹配加分 ──

def keyword_match_bonus(
    paper: dict,
    primary_kw: list[str],
    secondary_kw: list[str],
    regular_kw: list[str],
    w_primary: float = 1.0,
    w_secondary: float = 0.7,
    w_regular: float = 0.4,
) -> float:
    """计算论文的关键词命中加分（逐层二分：命中一层即得分，不重复计数）。

    在 title + abstract 中搜索关键词，每层最多计一次。
    主关键词层：任一主关键词命中即得分。
    返回原始加分值，范围 0 到 w_primary + w_secondary + w_regular (max 2.1)。
    """
    title = (paper.get("title") or "").lower()
    abstract = (paper.get("abstract") or "").lower()
    text = f"{title} {abstract}"

    bonus = 0.0
    if primary_kw:
        for pk in primary_kw:
            if pk.lower() in text:
                bonus += w_primary
                break
    for kw in secondary_kw:
        if kw.lower() in text:
            bonus += w_secondary
            break
    for kw in regular_kw:
        if kw.lower() in text:
            bonus += w_regular
            break
    return bonus


def rank_papers(
    query: str,
    papers: list[dict],
    top_k: int = 50,
    ce_candidates: int = 100,
    primary_kw: list[str] | None = None,
    secondary_kw: list[str] | None = None,
    regular_kw: list[str] | None = None,
    kw_bonus_scale: float = 0.12,
) -> list[tuple[dict, float]]:
    """完整的论文排序流水线：API 分粗筛 → cross-encoder 精排 → 关键词加分。

    FAISS 已移除。API 排序分（arXiv/OpenAlex 原始相关性）承担粗筛，
    cross-encoder 承担精排，最终得分 = CE 分 + 关键词匹配加分。

    Args:
        query: 课题描述文本
        papers: 去重后的论文列表
        top_k: 最终返回数量（默认 50）
        ce_candidates: 送入 cross-encoder 精排的候选数（默认 100）
        primary_kw: 主关键词列表
        secondary_kw: 副关键词列表
        regular_kw: 普通关键词列表
        kw_bonus_scale: 关键词加分缩放系数

    Returns:
        [(paper, final_score), ...]，按分数降序
    """
    if not papers:
        return []

    actual_top = min(top_k, len(papers))
    actual_candidates = min(ce_candidates, len(papers))
    print(f"[Rank] Starting: {len(papers)} papers, top_k={actual_top}, "
          f"ce_candidates={actual_candidates}", flush=True)

    # Stage 1: API 分粗筛
    papers_with_api = [p for p in papers if p.get("api_score") is not None]
    papers_without_api = [p for p in papers if p.get("api_score") is None]
    papers_with_api.sort(key=lambda p: _api_score(p.get("api_score")), reverse=True)
    candidates = [
        (p, _api_score(p.get("api_score")))
        for p in papers_with_api[:actual_candidates] + papers_without_api[:actual_candidates]
    ]
    print(f"[Rank] Stage 1 done: {len(candidates)} candidates", flush=True)

    # Stage 2: Cross-encoder 精排
    print(f"[Rank] Stage 2: calling rerank_with_cross_encoder...", flush=True)
    reranked = rerank_with_cross_encoder(query, candidates, top_k=top_k)
    print(f"[Rank] Stage 2 done: {len(reranked)} results", flush=True)

    # Stage 3: 关键词匹配加分（不做 API/语义加权融合）
    secondary_kw = secondary_kw or []
    regular_kw = regular_kw or []
    final = []
    for paper, ce_score in reranked:
        score = float(ce_score)
        kw_bonus = keyword_match_bonus(paper, primary_kw, secondary_kw, regular_kw)
        score += kw_bonus * kw_bonus_scale
        final.append((paper, score))

    # 无摘要论文降权：仅凭标题无法准确评估内容相关性
    for i, (paper, score) in enumerate(final):
        abstract = (paper.get("abstract") or "").strip()
        if len(abstract) < 50:
            final[i] = (paper, score * 0.7)

    final.sort(key=lambda x: -x[1])
    return final[:top_k]


def _drop_model_locked():
    global _cross_encoder, _ce_unload_pending, _ce_load_future
    _cross_encoder = None
    if _ce_load_future is not None and _ce_load_future.done():
        _ce_load_future = None
    _ce_unload_pending = False
    import gc
    gc.collect()
    count("ce_model_released")


def release_cross_encoder():
    """Release after configured idle time; never evict an active prediction."""
    global _ce_timer
    try:
        seconds = max(0, min(3600, float((load_config().get("search") or {}).get(
            "ce_idle_seconds", 300))))
    except (TypeError, ValueError):
        seconds = 300
    with _ce_lock:
        _cancel_idle_locked()
        if _ce_users:
            return
        if _ce_unload_pending or seconds == 0:
            _drop_model_locked()
            return
        if _cross_encoder is None and _ce_load_future is None:
            return
        epoch = _ce_epoch
        def expire():
            with _ce_lock:
                if epoch == _ce_epoch and not _ce_users:
                    _drop_model_locked()
        _ce_timer = threading.Timer(seconds, expire)
        _ce_timer.daemon = True
        _ce_timer.start()


def unload_cross_encoder():
    """Request immediate release (deferred until active predictions finish)."""
    global _ce_unload_pending
    with _ce_lock:
        _cancel_idle_locked()
        if _ce_users:
            _ce_unload_pending = True
        else:
            _drop_model_locked()
