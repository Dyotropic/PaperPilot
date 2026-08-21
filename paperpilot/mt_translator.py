"""中文关键词 → 英文术语翻译模块。

通过 LLM（llm_client 多模型抽象）进行学术关键词翻译。
"""

import re
from paperpilot.config import load_config
from paperpilot.llm_client import get_client

_SYSTEM_PROMPT = (
    "You are a scientific translator. Translate Chinese academic keywords into "
    "precise English technical terms. For each input, output only the English "
    "translation. Use domain-appropriate terminology. Never add explanations."
)


def translate_terms(chinese_terms: list[str]) -> list[str]:
    """将中文关键词列表翻译为英文术语列表（批量调用 DeepSeek API）。

    Args:
        chinese_terms: 中文关键词列表（如 ["钙钛矿太阳能电池", "基因治疗"]）

    Returns:
        英文术语列表（与输入一一对应），翻译失败的项返回空字符串
    """
    if not chinese_terms:
        return []

    config = load_config()
    api_key = config.get("deepseek", {}).get("api_key", "").strip()
    if not api_key:
        return [""] * len(chinese_terms)

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

    # Build numbered list for reliable parsing
    numbered = "\n".join(f"{j+1}. {t}" for j, t in enumerate(to_translate))
    user_msg = (
        "Translate these Chinese academic keywords to English. "
        "Output one translation per line in the format: number. English term\n\n"
        f"{numbered}"
    )

    client = get_client()
    if not client or not client.is_available:
        return results

    content = client.chat(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.1, max_tokens=len(to_translate) * 50,
        timeout=30, thinking=False,
    ).content
    translations = _parse_batch_response(content, len(to_translate))

    for j, translation in enumerate(translations):
        if translation:
            results[indices[j]] = translation

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
    all_terms: list[str] = []
    mapping: list[tuple[int, int]] = []  # (src_idx, term_idx)
    results: list[list[str]] = [[] for _ in term_lists]

    for src_idx, terms in enumerate(term_lists):
        results[src_idx] = [""] * len(terms)
        for term_idx, term in enumerate(terms):
            stripped = term.strip()
            if not stripped:
                continue
            if all(ord(c) < 128 for c in stripped):
                results[src_idx][term_idx] = stripped
                continue
            all_terms.append(stripped)
            mapping.append((src_idx, term_idx))

    if not all_terms:
        return results

    numbered = "\n".join(f"{j+1}. {t}" for j, t in enumerate(all_terms))
    user_msg = (
        "Translate these Chinese academic keywords to English. "
        "Output one translation per line in the format: number. English term\n\n"
        f"{numbered}"
    )

    client = get_client()
    if not client or not client.is_available:
        return results

    content = client.chat(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.1, max_tokens=len(all_terms) * 50,
        timeout=30, thinking=False,
    ).content
    translations = _parse_batch_response(content, len(all_terms))

    for j, translation in enumerate(translations):
        if translation and j < len(mapping):
            src_idx, term_idx = mapping[j]
            results[src_idx][term_idx] = translation

    return results
