"""核心关键词提取 — 通过 DeepSeek API 从科研课题中提取高区分度关键词。

提取原则（来自中文科研课题关键词提取指南）：
1. 优先提取具体技术/材料/方法，而非上层泛化概念
2. 提取课题真正解决的关键科学问题或应用场景
3. 跳过领域内过度饱和的热词，向下挖掘一层
4. 保留复合专有名词完整性，不可拆分
5. 删除修饰性、背景性词语
6. 输出 1-3 个核心关键词
"""

import json
from paperpilot.llm_client import get_client, llm_configured

_SYSTEM_PROMPT = (
    "你是一个科研关键词提取专家。你的任务是从科研课题标题中提取1-3个最核心、"
    "最具区分度的关键词。\n\n"
    "严格遵循以下规则：\n"
    "1. 优先提取具体的核心技术/材料/方法，而非上层泛化概念"
    "（如提取 Transformer 而非多模态大语言模型）\n"
    "2. 提取课题真正解决的关键科学问题或应用场景\n"
    "3. 跳过领域内过度饱和的热词，向下挖掘一层更具区分度的概念"
    "（如课题做LNP/mRNA但创新点在器官靶向性，则提取器官靶向性而非mRNA）\n"
    "4. 即使两个术语看似属于同一大类（如CAR-T和CAR-NK），只要它们指代不同的"
    "具体技术/靶点，都必须分别提取\n"
    "5. 保留复合专有名词的完整性，不可拆分"
    "（如钙钛矿量子点、CRISPR-Cas12a、非阿贝尔编织）\n"
    "6. 删除基于、用于、研究、优化、性能等修饰性背景词\n"
    "7. 输出1-3个核心关键词，用顿号（、）分隔，不要解释、不要编号、不要换行\n\n"
    "输出格式示例：\n"
    "钙钛矿量子点、柔性显示器件\n"
    "CRISPR-Cas12a、遗传性视网膜病变\n"
    "数字孪生、自愈控制"
)


def extract_core_keywords(topic: str) -> list[str]:
    """从科研课题标题中提取核心关键词。

    Args:
        topic: 科研课题标题（中文）

    Returns:
        核心关键词列表（1-3个），失败时返回空列表
    """
    if not llm_configured():
        return []

    client = get_client()
    if not client or not client.is_available:
        return []

    content = client.chat(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": topic},
        ],
        temperature=0.3, max_tokens=30, timeout=15, thinking=False,
    ).content
    return _parse_response(content, max_results=3)


_REGULAR_SYSTEM_PROMPT = (
    "你是一个科研关键词提取专家。你的任务是从科研课题标题中提取5-8个有搜索价值的"
    "细分技术术语，用于补充文献检索。\n\n"
    "严格遵循以下规则：\n"
    "1. 提取所有有意义的技术子概念，粒度要细（如课题含'高熵电解质材料'，"
    "应拆分提取'高熵'和'电解质材料'，确保关键区分词不丢失）\n"
    "2. 提取核心材料、方法、技术、科学问题、应用场景——每个词都应有独立的搜索价值\n"
    "3. 即使两个术语看似属于同一大类（如CAR-T和CAR-NK、mRNA和siRNA），只要它们"
    "指代不同的具体技术/靶点/分子，都必须分别提取，绝不可用一个替代另一个\n"
    "4. 保留复合专有名词的完整性（如钙钛矿量子点、CRISPR-Cas12a不可拆分）\n"
    "5. 删除无搜索价值的词语：研究、方法、分析、应用、性能、优化、设计、运用等\n"
    "6. 以中文输出为主，但学科通用英文缩写必须保留原始形式（如CAR-NK、CAR-T、TCR-T、"
    "mRNA、siRNA、PD-1、CTLA-4、CRISPR-Cas12a等不得翻译为中文）\n"
    "7. 输出5-8个关键词，用顿号（、）分隔，不要解释、不要编号、不要换行\n\n"
    "输出格式示例：\n"
    "高熵、电解质材料、水系锌电池、界面调控、循环稳定性\n"
    "CAR-NK、嵌合抗原受体、肿瘤免疫微环境、NK细胞活化、同种异体移植\n"
    "CRISPR-Cas12a、遗传性视网膜病变、基因编辑、AAV递送"
)


def extract_regular_keywords(topic: str) -> list[str]:
    """从科研课题中提取细分普通关键词（细粒度、有搜索价值）。

    与 core 关键词不同：regular 追求覆盖面，拆分复合词中的关键子概念。

    Args:
        topic: 科研课题标题（中文或英文）

    Returns:
        细分关键词列表（5-8个），失败时返回空列表
    """
    if not llm_configured():
        return []

    client = get_client()
    if not client or not client.is_available:
        return []

    content = client.chat(
        [
            {"role": "system", "content": _REGULAR_SYSTEM_PROMPT},
            {"role": "user", "content": topic},
        ],
        temperature=0.3, max_tokens=80, timeout=15, thinking=False,
    ).content
    return _parse_response(content, max_results=8)


def _parse_response(content: str, max_results: int = 10) -> list[str]:
    """Parse model output into a clean keyword list."""
    import re
    keywords = [k.strip() for k in re.split(r"[、,，/\n]", content) if k.strip()]
    result = []
    for k in keywords:
        has_cjk = any("一" <= c <= "鿿" for c in k)
        has_alpha = any(c.isascii() and c.isalpha() for c in k)
        if len(k) >= 2 and (has_cjk or has_alpha):
            k = k.rstrip("。，,、.!！?？;；：:")
            if len(k) >= 2:
                result.append(k)
    return result[:max_results]
