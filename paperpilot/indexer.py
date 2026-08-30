"""向量索引与检索接口。

Phase 1 核心流水线：
    论文摘要 → 本地多语言 Embedding → FAISS IndexFlatIP → 相似度检索
    → Cross-Encoder 精排 → API 分数融合 → 最终排序

使用 paraphrase-multilingual-MiniLM-L12-v2（384维），
本地运行，中英文跨语言匹配，无需网络。
"""

import logging
import os
import threading
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
_cross_encoder = None
_ce_lock = threading.Lock()  # 防止多线程同时加载模型


# ── Cross-Encoder 重排序 ──

def _get_cross_encoder():
    """加载 cross-encoder 模型（懒加载，线程安全，本地缓存优先）。

    关键参数：
    - max_length=512：截断长文本，Qwen2 默认 32K 会导致注意力 O(n²) 爆炸
    - 使用 threading.Lock 防止多线程重复加载
    - ThreadPoolExecutor 180s 超时保护，超时回退到纯 API 排序
    """
    global _cross_encoder
    if _cross_encoder is not None:
        return _cross_encoder

    with _ce_lock:
        if _cross_encoder is not None:
            return _cross_encoder

        import time as _t
        import concurrent.futures
        print("[CE] Loading cross-encoder...", flush=True)
        _t0 = _t.time()

        if Path(_CE_PATH).exists():
            print(f"[CE] Loading from cache: {_CE_PATH}", flush=True)

            def _load():
                # CPU 上强制 float32：Qwen2 系默认 bf16 在 CPU 推理慢 ~9 倍，
                # float32 既提速又更高精度（见 library-pdf 分支 9d8fa34）
                return CrossEncoder(
                    _CE_PATH,
                    max_length=_CE_MAX_LENGTH,
                    device="cpu",
                    model_kwargs={"torch_dtype": torch.float32},
                )

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                future = executor.submit(_load)
                _cross_encoder = future.result(timeout=180)
                print(f"[CE] Loaded in {_t.time()-_t0:.1f}s", flush=True)
                return _cross_encoder
            except concurrent.futures.TimeoutError:
                print(f"[CE] 加载超时(180s)，将使用纯 API 排序", flush=True)
                _cross_encoder = None
                return None
            except Exception as e:
                logger.warning(f"Cross-encoder load failed: {e}")
                _cross_encoder = None
                return None
            finally:
                # wait=False：超时后不等待阻塞线程（否则超时保护形同虚设）
                executor.shutdown(wait=False)

        # 本地无缓存，尝试在线下载
        logger.info("Cross-encoder not cached, attempting download...")
        old_hf = os.environ.pop("HF_HUB_OFFLINE", None)
        old_tr = os.environ.pop("TRANSFORMERS_OFFLINE", None)
        try:
            _cross_encoder = CrossEncoder(
                _CE_NAME,
                max_length=_CE_MAX_LENGTH,
                device="cpu",
                model_kwargs={"torch_dtype": torch.float32},
            )
        except Exception as e:
            logger.warning(f"Cross-encoder download failed: {e}")
            _cross_encoder = None
        if old_hf is not None:
            os.environ["HF_HUB_OFFLINE"] = old_hf
        if old_tr is not None:
            os.environ["TRANSFORMERS_OFFLINE"] = old_tr

        return _cross_encoder


def rerank_with_cross_encoder(
    query: str,
    results: list[tuple[dict, float]],
    top_k: int = 20,
) -> list[tuple[dict, float]]:
    """用 cross-encoder 对粗筛结果精排。

    安全措施：
    - max_length=512 已在模型加载时设置，防止长序列 OOM
    - 文本在拼接前截断到 3000 字符，双重保险
    - predict() 有 120s 超时保护，超时回退到 API 分数排序
    """
    ce = _get_cross_encoder()
    if not results:
        return []
    if ce is None:
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

    pairs = [(query[:2000], _paper_text(p)) for p, _ in results]

    import concurrent.futures

    def _predict():
        return ce.predict(pairs, show_progress_bar=False)

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(_predict)
        scores = future.result(timeout=300)
    except concurrent.futures.TimeoutError:
        print("[CE] predict 超时(300s)，回退到 API 分数排序", flush=True)
        results.sort(key=lambda x: -(x[1] if x[1] is not None else 0))
        return results[:top_k]
    except Exception as e:
        logger.warning(f"Cross-encoder prediction failed: {e}")
        results.sort(key=lambda x: -(x[1] if x[1] is not None else 0))
        return results[:top_k]
    finally:
        # wait=False：超时后不等待阻塞线程（否则 300s 超时被 with 块抵消）
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
    papers_with_api.sort(key=lambda p: p.get("api_score", 0), reverse=True)
    candidates = [
        (p, p.get("api_score", 0.5))
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


def unload_cross_encoder():
    """释放 cross-encoder 模型内存（~942MB）。

    模型文件不会被删除，下次调用 rank_papers 时自动重新加载。
    检索完成后调用此函数，可释放近 1GB 内存。
    """
    global _cross_encoder
    if _cross_encoder is not None:
        print("[CE] Unloading cross-encoder to free memory...", flush=True)
        _cross_encoder = None
        import gc
        gc.collect()
        print("[CE] Unloaded (~942MB freed).", flush=True)
