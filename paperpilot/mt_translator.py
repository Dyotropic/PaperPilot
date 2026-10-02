"""中文关键词 → 英文术语翻译模块。

通过 LLM（llm_client 多模型抽象）进行学术关键词翻译。
"""

import re
import time
import threading
from collections import OrderedDict
from paperpilot.config import load_config
from paperpilot.agent_runtime import checkpoint
from paperpilot.search_metrics import count
from paperpilot.llm_client import get_client, llm_configured

_cache = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_LIMIT = 512

_SYSTEM_PROMPT = (
    "You are a scientific translator. Translate Chinese academic keywords into "
    "precise English technical terms. For each input, output only the English "
    "translation. Use domain-appropriate terminology. Never add explanations."
)


def translate_terms(chinese_terms: list[str]) -> list[str]:
    """将中文关键词列表翻译为英文术语列表（批量调用配置的 LLM）。

    Args:
        chinese_terms: 中文关键词列表（如 ["钙钛矿太阳能电池", "基因治疗"]）

    Returns:
        英文术语列表（与输入一一对应），翻译失败的项返回空字符串
    """
    if not chinese_terms:
        return []

    # Separate: already-ASCII terms pass through, Chinese terms need translation
    to_translate = []
    indices = []
    results = [""] * len(chinese_terms)

    for i, term in enumerate(chinese_terms):
        stripped = term.strip()
        if not stripped:
            results[i] = ""
            continue
        if all(ord(c) < 128 for c in stripped):
            results[i] = stripped
            continue
        to_translate.append(stripped)
        indices.append(i)

    if not to_translate:
        return results

    if not llm_configured():
        return results

    client = get_client(task="translation")
    if not client or not client.is_available:
        return results
    identity = (type(client).__name__, getattr(client, "provider", ""),
                getattr(client, "base_url", ""), getattr(client, "model", ""))
    try:
        ttl = max(0, min(86400, float((load_config().get("search") or {}).get(
            "translation_cache_ttl_seconds", 3600))))
    except (TypeError, ValueError):
        ttl = 3600
    pending = OrderedDict()
    now = time.monotonic()
    with _cache_lock:
        for term, index in zip(to_translate, indices):
            key = identity + (term,)
            cached = _cache.get(key)
            if cached and ttl > 0 and now - cached[0] < ttl:
                results[index] = cached[1]
                _cache.move_to_end(key)
                count("translation_cache_hit")
            else:
                _cache.pop(key, None)
                pending.setdefault(term, []).append(index)
    if not pending:
        return results
    # One translation per unique item, preserving all original group positions.
    numbered = "\n".join(f"{j+1}. {t}" for j, t in enumerate(pending))
    user_msg = (
        "Translate these Chinese academic keywords to English. "
        "Output one translation per line in the format: number. English term\n\n"
        f"{numbered}"
    )

    checkpoint()
    count("translation_request")
    content = client.chat(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.1, max_tokens=max(256, sum(max(50, len(t) * 2) for t in pending)),
        timeout=30, thinking=False,
    ).content
    checkpoint()
    translations = _parse_batch_response(content, len(pending))
    with _cache_lock:
        for term, translation in zip(pending, translations):
            if translation:
                for index in pending[term]:
                    results[index] = translation
                if ttl > 0:
                    _cache[identity + (term,)] = (time.monotonic(), translation)
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)

    return results


def _parse_batch_response(content: str, expected_count: int) -> list[str]:
    """Parse numbered translation output into ordered list."""
    lines = content.strip().split("\n")
    parsed = {}

    for line in lines:
        line = line.strip()
        # Match "1. English term" or "1 English term" or "1、English term"
        m = re.match(r"^(\d+)[\.\、\)\s]\s*(.+)", line)
        if m:
            idx = int(m.group(1)) - 1
            term = m.group(2).strip()
            # Remove trailing descriptions (模型偶尔会多话)
            term = re.sub(r"\s*[\(（].*[\)）]\s*$", "", term)
            term = term.rstrip(".,;:!。，；：！")
            if 0 <= idx < expected_count:
                parsed[idx] = term

    # Build result in order
    result = []
    for i in range(expected_count):
        t = parsed.get(i, "")
        # Reject if still contains Chinese
        if t and any("一" <= c <= "鿿" for c in t):
            t = ""
        result.append(t)
    return result


def translate_all_terms(*term_lists: list[str]) -> list[list[str]]:
    """一次性翻译多组术语列表，合并为单次 API 调用。

    Args:
        *term_lists: 多组中文关键词列表

    Returns:
        与输入一一对应的英文术语列表（每组对应一个输入 list）
    """
    flat = [term for terms in term_lists for term in terms]
    translated = translate_terms(flat)
    results, offset = [], 0
    for terms in term_lists:
        results.append(translated[offset:offset + len(terms)])
        offset += len(terms)
    return results
