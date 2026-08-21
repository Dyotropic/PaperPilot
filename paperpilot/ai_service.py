"""AI 服务层 — LLM 封装 + 论文精读。

Deep Read 采用 RLM 分层阅读策略（借鉴 Feynman）：
    - < 8000 字符：直接全文注入
    - 8000-60000 字符：滑动窗口 + 渐进笔记 + 合成
    - > 60000 字符：切块分析后合成

复用 downloader.py 的 PDF/HTML 获取能力，不重复造轮子。
底层 LLM 调用统一经 llm_client 多模型抽象（PHASE3_PLAN 功能三）。
"""

import hashlib
import json
import logging
import os
import re
import uuid
from pathlib import Path

from paperpilot.llm_client import get_client, get_task_model

logger = logging.getLogger(__name__)

_DEEP_READ_DIR = Path("outputs/deep_read")

# ── RLM 参数 ──
_WINDOW_SIZE = 6000    # 每窗字符数
_OVERLAP = 500         # 窗间重叠
_TIER1_MAX = 8000      # 直接注入阈值
_TIER2_MAX = 60000     # 窗口滑读上限


# ── 全文获取 ──

def _extract_text_from_html(html_path: str) -> str | None:
    """从 downloader.fetch_full_text 缓存的 HTML 文件中提取纯文本。

    文件是自包含 HTML（含图片 base64），需移除标签和脚本。
    """
    try:
        p = Path(html_path)
        if not p.is_file():
            return None
        html = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        for tag in soup.select("script, style, nav, footer, img, svg"):
            tag.decompose()
        text = soup.get_text(separator="\n")
    except ImportError:
        # 回退：正则移除标签
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&[a-z]+;", " ", text)

    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [l.strip() for l in text.split("\n")]
    text = "\n".join(l for l in lines if l)
    return text if len(text) >= 200 else None


def get_full_text_for_paper(paper: dict) -> tuple[str | None, str]:
    """三级链路获取论文全文，供 deep_read 使用。

    优先级：PDF 提取 > arXiv/出版方 HTML > 不可用

    Args:
        paper: paper dict，需含 pdf_path / url / doi 等

    Returns:
        (full_text, source) — source 为 "pdf" / "html" / "unavailable"
    """
    from paperpilot.downloader import cache_pdf, extract_pdf_text, fetch_full_text

    # 1. PDF 缓存 + 提取
    pdf_path = paper.get("pdf_path") or cache_pdf(paper)
    if pdf_path:
        p = Path(pdf_path)
        if p.is_file():
            try:
                pdf_bytes = p.read_bytes()
                text = extract_pdf_text(pdf_bytes)
                if text and len(text.strip()) >= 100:
                    return text.strip(), "pdf"
            except Exception as e:
                logger.warning(f"PDF extraction failed: {e}")

    # 2. HTML 全文（缓存优先 → arXiv HTTP 快速路径 → CDP 浏览器）
    html_path = fetch_full_text(paper)
    if html_path:
        text = _extract_text_from_html(html_path)
        if text and len(text.strip()) >= 500:
            return text.strip(), "html"
        elif text and len(text.strip()) >= 100:
            # 正文过短（仅摘要页），标记为不可用，建议用户下载 PDF
            logger.info("HTML 正文过短 (%d chars)，跳过，建议下载 PDF", len(text.strip()))
            return None, "html_truncated"

    return None, "unavailable"


# ── AIService ──

class AIService:
    """LLM 服务封装，提供论文精读等 AI 能力（底层经 llm_client 多模型）。"""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = api_key
        self._model = model
        self._conversations: dict[int, object] = {}  # project_id → ConversationManager
        self._qa_sessions: dict[str, list[dict]] = {}  # session_id → messages

    @property
    def is_available(self) -> bool:
        client = get_client()
        return bool(client and client.is_available)

    def _resolve_task_model(self, task: str) -> str:
        """获取任务专用模型名，从 config llm.{task}_model 读取。

        Args:
            task: 任务名，如 "score"、"chat"、"reasoning"
        Returns:
            模型名，config 未配置时回退到主模型；未配置 LLM 返回空串
        """
        return get_task_model(task)

    def _get_client(self, task: str | None = None):
        """获取任务对应的 LLM 客户端（未配置返回 None）。"""
        return get_client(task)

    def _call_api(
        self,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 2000,
        timeout: int = 120,
        model: str | None = None,
        thinking: bool | None = None,
    ) -> str:
        """通用 LLM 调用。返回 content 字符串；失败返回空串。"""
        return self._call_api_full(
            messages, temperature, max_tokens, timeout, model, thinking
        )[0]

    def _call_api_full(
        self,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 2000,
        timeout: int = 120,
        model: str | None = None,
        thinking: bool | None = None,
    ) -> tuple[str, str]:
        """LLM 调用，返回 (content, reasoning) 元组。

        reasoning 仅 thinking=True 时由模型填充（DeepSeek reasoning_content
        / Anthropic thinking block），无则空串。
        """
        client = self._get_client()
        if not client or not client.is_available:
            return "", ""
        result = client.chat(
            messages, temperature=temperature, max_tokens=max_tokens,
            timeout=timeout, model=model, thinking=thinking,
        )
        return result.content, result.reasoning

    def _call_api_stream(
        self,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 2000,
        timeout: int = 120,
        model: str | None = None,
        thinking: bool | None = None,
    ):
        """流式调用 LLM，yield 每个 content delta 字符串。

        未配置/失败时直接返回（不 yield）。
        """
        client = self._get_client()
        if not client or not client.is_available:
            return
        for delta in client.chat_stream(
            messages, temperature=temperature, max_tokens=max_tokens,
            timeout=timeout, model=model, thinking=thinking,
        ):
            yield delta

    def _parse_json_response(self, content: str) -> dict | list:
        """从 LLM 回复中提取 JSON 块，失败返回空 dict。"""
        if not content:
            return {}
        # 尝试直接解析
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass
        # 尝试提取 ```json ... ``` 代码块
        m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # 尝试找 { ... } 块
        m = re.search(r"\{[\s\S]*\}", content)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        # 兜底：截断 JSON 数组恢复 —— 逐条提取完整 {...} 对象
        arr_m = re.search(r"\[([\s\S]*)\]", content)
        if arr_m:
            inner = arr_m.group(1)
            items = []
            for obj_m in re.finditer(
                r"\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}",
                inner,
            ):
                try:
                    items.append(json.loads(obj_m.group(0)))
                except json.JSONDecodeError:
                    continue
            if items:
                logger.info(
                    f"Truncated JSON recovery: salvaged {len(items)} items"
                )
                return items
        logger.warning(
            f"Failed to parse JSON from API response "
            f"(len={len(content)}, preview={content[:300]})"
        )
        return {}

    # ── RLM 分层阅读 ──

    def _rlm_window_read(
        self,
        full_text: str,
        title: str,
        window_size: int = _WINDOW_SIZE,
        overlap: int = _OVERLAP,
    ) -> str:
        """滑动窗口阅读长文本，每窗写渐进笔记，返回合成笔记。

        参照 Feynman Tier 2：文档留在内存，每窗读取 → 提取要点 → 追加笔记，
        全部读完后用笔记合成最终分析。
        """
        text_len = len(full_text)
        notes_parts: list[str] = []
        step = window_size - overlap
        total_windows = max(1, (text_len - overlap) // step)

        system_prompt = (
            "你是一位资深学术审稿人。请仔细阅读论文片段，提取关键信息。\n\n"
            "用中文输出，格式如下：\n"
            "- 核心主张：...\n"
            "- 关键方法/数据：...\n"
            "- 可能的创新点：...\n"
            "- 可疑的局限：...\n\n"
            "只基于当前片段分析，不要编造。如果片段是从论文中间开始的，"
            "直接分析看到的内容即可。"
        )

        for i in range(total_windows):
            start = i * step
            end = min(start + window_size, text_len)
            chunk = full_text[start:end]

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": (
                    f"论文标题：《{title}》\n"
                    f"—— 片段 {i + 1}/{total_windows} ——\n\n"
                    f"{chunk}"
                )},
            ]

            content = self._call_api(messages, temperature=0.3, max_tokens=600,
                                     timeout=90, thinking=True)
            if content:
                notes_parts.append(f"## 片段 {i + 1}/{total_windows}\n\n{content}")

            print(f"[DeepRead] Window {i + 1}/{total_windows} done ({len(chunk)} chars)", flush=True)

        return "\n\n".join(notes_parts)

    def _chunked_read(
        self,
        full_text: str,
        title: str,
        chunk_size: int = _TIER2_MAX,
    ) -> str:
        """超长文本 (>60K) 切块独立分析后合成。

        每块独立走一次完整分析调用，块间无重叠。
        """
        text_len = len(full_text)
        chunks = []
        for i in range(0, text_len, chunk_size):
            chunks.append(full_text[i:i + chunk_size])

        all_notes: list[str] = []
        for i, chunk in enumerate(chunks):
            notes = self._rlm_window_read(
                chunk, title,
                window_size=_WINDOW_SIZE,
                overlap=_OVERLAP,
            )
            if notes:
                all_notes.append(f"## Chunk {i + 1}/{len(chunks)}\n\n{notes}")
            print(f"[DeepRead] Chunk {i + 1}/{len(chunks)} analyzed", flush=True)

        return "\n\n".join(all_notes)

    # ── Deep Read ──

    _DEEP_READ_SYSTEM = (
        "你是一位资深学术审稿人。请基于提供的阅读笔记对论文进行结构化精读分析。\n\n"
        "严格按以下 JSON 格式输出，不要额外文字：\n"
        "{\n"
        '  "core_contribution": "论文解决的核心问题（一句话）",\n'
        '  "method": "关键技术路线或研究方法（2-3句）",\n'
        '  "key_evidence": "支撑结论的核心实验或数据，引用原文具体内容",\n'
        '  "highlights": "创新点或论文最强方面（2-3句）",\n'
        '  "limitations": "明显局限或改进空间（1-2句）",\n'
        '  "scores": {"novelty": 7, "rigor": 6, "significance": 8}\n'
        "}\n\n"
        "scores 中 novelty/rigor/significance 各为 1-10 的整数。\n"
        "保持客观，基于笔记而非猜测。如果笔记中某项信息缺失，标注'未提及'而非编造。"
    )

    def deep_read(self, paper: dict, full_text: str | None = None) -> dict:
        """对单篇论文做结构化精读分析。

        Args:
            paper: paper dict，至少含 title
            full_text: 论文全文；为 None 时自动通过三级链路获取

        Returns:
            dict 含 core_contribution / method / key_evidence /
                 highlights / limitations / scores，
            失败时返回空 dict
        """
        if not self.is_available:
            return {}

        title = (paper.get("title") or "").strip()
        if not title:
            return {}

        # 获取全文
        source = "provided"
        if not full_text:
            full_text, source = get_full_text_for_paper(paper)
            if not full_text:
                # HTML 正文过短（仅摘要页），不浪费 token 做假精读
                if source == "html_truncated":
                    return {"_truncated": True, "_title": title, "_source": source}
                abstract = (paper.get("abstract") or "").strip()
                if abstract and len(abstract) >= 50:
                    full_text = f"（注意：仅获取到摘要，无全文）\n\n{abstract}"
                    source = "abstract_fallback"
                else:
                    return {}

        text_len = len(full_text)
        print(f"[DeepRead] Title: {title[:50]}... | {text_len} chars | source: {source}", flush=True)

        # RLM 分层处理
        if text_len <= _TIER1_MAX:
            # Tier 1: 直接注入
            print(f"[DeepRead] Tier 1: direct injection", flush=True)
            notes = full_text
        elif text_len <= _TIER2_MAX:
            # Tier 2: 滑动窗口
            print(f"[DeepRead] Tier 2: sliding window", flush=True)
            notes = self._rlm_window_read(full_text, title)
            if not notes:
                # 窗口阅读失败，截断回退到 Tier 1
                notes = full_text[:_TIER1_MAX]
        else:
            # Tier 3: 切块分析
            print(f"[DeepRead] Tier 3: chunked read", flush=True)
            notes = self._chunked_read(full_text, title)
            if not notes:
                notes = full_text[:_TIER1_MAX]

        # 最终合成
        messages = [
            {"role": "system", "content": self._DEEP_READ_SYSTEM},
            {"role": "user", "content": (
                f"论文标题：《{title}》\n"
                f"全文来源：{source}\n\n"
                f"—— 阅读笔记 ——\n\n{notes[:15000]}"
            )},
        ]

        content = self._call_api(messages, temperature=0.3, max_tokens=1500,
                                 timeout=120, thinking=True)
        result = self._parse_json_response(content)

        if not result:
            # 解析失败，返回原始回复作为 fallback
            result = {
                "core_contribution": "",
                "method": "",
                "key_evidence": "",
                "highlights": "",
                "limitations": "",
                "scores": {"novelty": 0, "rigor": 0, "significance": 0},
                "_raw": content[:500],
                "_parse_error": True,
            }

        # 附上元信息
        result["_title"] = title
        result["_source"] = source
        result["_text_chars"] = text_len
        return result


    # ── AI 精排 ──

    _SCORE_PAPERS_SYSTEM = (
        "你是一位严格的学术审稿人，需要快速评估一批论文与课题的相关性和质量。\n\n"
        "## 评分维度（每个维度 1-10 分）\n"
        "- relevance（课题相关性，权重 40%）：论文核心问题与课题描述的匹配程度\n"
        "- method（方法质量，权重 25%）：实验设计是否严谨、数据是否充分、方法论是否可靠\n"
        "- novelty（创新性，权重 20%）：方法/结论是否有新意，还是重复已有工作\n"
        "- recency（时效性，权重 15%）：近年发表加分（2020+），经典老文献不减分\n\n"
        "## 分数计算\n"
        "总分 = (relevance×0.4 + method×0.25 + novelty×0.2 + recency×0.15) × 10\n"
        "结果四舍五入到整数，范围 0-100。\n\n"
        "## 分档参考\n"
        "S 必读 85-100：课题核心问题直接命中，方法/结论可直接借鉴\n"
        "A 推荐 70-84：高度相关，但方法或场景有差异\n"
        "B 可浏览 55-69：部分相关，某个子方向有参考价值\n"
        "C 可选 40-54：弱相关，可能是背景或相关领域\n"
        "D 不推荐 0-39：基本无关或质量明显有问题\n\n"
        "## 理由写作要求\n"
        "每个维度的 reason 必须写 1-3 句中文，具体引用论文中提到的技术/方法/场景，"
        "解释为什么给这个分数。不要写空洞套话。\n"
        "reason_overall 是综合判断，说明是否值得读全文及原因。\n\n"
        "## 无摘要处理\n"
        "如果论文没有摘要（abstract 为空或短于 50 字符），将 method、novelty 两项标为 0，"
        "仅基于标题评估 relevance 和 recency，tier 标注为 'no_abstract'，各 reason 写'仅标题，无法判断'。\n\n"
        "## 输出格式\n"
        "严格的 JSON 数组，按论文输入顺序，只输出 JSON 不要其他文字：\n"
        '[{"index": 0, "score": 85, "tier": "S", '
        '"relevance": 9, "method": 8, "novelty": 7, "recency": 9, '
        '"reason_relevance": "论文研究钙钛矿稳定性退化机制，与课题描述完全匹配。具体聚焦热致离子迁移，...", '
        '"reason_method": "采用原位PL光谱+ToF-SIMS联合表征，实验设计严谨，但样本量偏少（n=3），...", '
        '"reason_novelty": "首次定量建立离子迁移活化能与界面缺陷密度的关联，创新性突出。", '
        '"reason_overall": "该论文核心问题与课题高度一致，方法可靠且结论创新性强，建议优先阅读全文。", '
        '"reason_recency": "2023年发表，时效性好。"}, ...]\n\n'
        "注意：基于摘要内容判断，不要编造。"
    )

    # 单批最多 10 篇，确保 max_tokens 不超 DeepSeek 8192 上限
    # 每篇 ~5 个理由字段 × 100+ 汉字 ≈ 1000+ tokens，10 篇 = 10000+
    # 实际 max_tokens=8000 有一定截断风险，但 10 篇通常够用
    _SCORE_CHUNK_SIZE = 10

    def score_papers(self, topic_desc: str, papers: list[dict],
                     max_papers: int = 50) -> list[dict]:
        """AI 精排：基于摘要批量打分。

        Args:
            topic_desc: 课题描述
            papers: paper dict 列表（仅含摘要）
            max_papers: 最多评分篇数（默认 20，上限 100）

        Returns:
            [{index, ai_score, ai_reason: {relevance, method, novelty, overall}}, ...]
            按 ai_score 降序排列
        """
        if not self.is_available or not papers:
            return []

        max_papers = min(max_papers, 50)

        # 收集候选论文（含无摘要的，标记 has_abstract）
        candidates: list[tuple[int, dict, bool]] = []
        for i, p in enumerate(papers):
            abstract = (p.get("abstract") or "").strip()
            has_abstract = bool(abstract and len(abstract) >= 50)
            candidates.append((i, p, has_abstract))
            if len(candidates) >= max_papers:
                break

        if not candidates:
            return []

        # debug: 记录进入 score_papers 的论文摘要状态
        abs_info = [(i, len((p.get("abstract") or "").strip()), ha) for i, p, ha in candidates]
        logger.info(
            "score_papers: topic=%s, total_candidates=%d, has_abstract=%d/%d, "
            "abstract_details(first10)=%s",
            topic_desc[:60], len(candidates),
            sum(1 for _, _, ha in candidates if ha), len(candidates),
            abs_info[:10],
        )

        # 拆批：每批最多 _SCORE_CHUNK_SIZE 篇
        chunks = [
            candidates[i:i + self._SCORE_CHUNK_SIZE]
            for i in range(0, len(candidates), self._SCORE_CHUNK_SIZE)
        ]
        all_results = []
        total = len(candidates)

        for chunk_idx, chunk in enumerate(chunks):
            chunk_results = self._score_chunk(topic_desc, chunk, chunk_idx, total)
            all_results.extend(chunk_results)

        all_results.sort(key=lambda x: x["ai_score"], reverse=True)
        return all_results

    def _score_chunk(
        self, topic_desc: str, chunk: list[tuple[int, dict, bool]],
        chunk_idx: int, total: int,
    ) -> list[dict]:
        """对单批论文打分，返回 [{index, ai_score, ...}, ...]."""
        chunk_size = len(chunk)

        # 构建输入
        header = f"课题描述：{topic_desc}\n\n—— 待评分论文（第 {chunk_idx + 1} 批，共 {chunk_size} 篇）——"
        lines = [header]
        for idx, (orig_i, p, has_abstract) in enumerate(chunk):
            title = (p.get("title") or "无标题")[:120]
            if has_abstract:
                abstract = (p.get("abstract") or "")[:800]
                lines.append(f"\n[{idx}] {title}\n摘要：{abstract}")
            else:
                lines.append(f"\n[{idx}] {title}\n摘要：（无摘要，仅基于标题评分）")

        messages = [
            {"role": "system", "content": self._SCORE_PAPERS_SYSTEM},
            {"role": "user", "content": "\n".join(lines)},
        ]

        # token 配额：每篇 600 + 500 余量，上限 8000（DeepSeek 8192 安全边际）
        dyn_tokens = min(8000, chunk_size * 600 + 500)
        content = self._call_api(
            messages, temperature=0.2, max_tokens=dyn_tokens, timeout=120,
            model=self._resolve_task_model("score"),
        )
        if not content:
            logger.warning(
                f"score_papers: chunk {chunk_idx} API returned empty response"
            )
            return []
        raw = self._parse_json_response(content)

        # 解析结果
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict) and "papers" in raw:
            items = raw["papers"]
        elif isinstance(raw, dict) and "results" in raw:
            items = raw["results"]
        else:
            logger.warning(
                f"score_papers: chunk {chunk_idx} parse returned "
                f"unexpected type {type(raw).__name__}, "
                f"content preview: {content[:200]}"
            )
            return []

        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            idx = item.get("index", -1)
            if idx < 0 or idx >= chunk_size:
                continue
            orig_i, paper, has_abstract = chunk[idx]
            score = int(item.get("score", 0))
            if not has_abstract:
                score = score // 2  # 仅标题评分，减半
            results.append({
                "index": orig_i,
                "ai_score": score,
                "tier": str(item.get("tier", "") or ""),
                "ai_reason": {
                    "relevance": int(item.get("relevance", 0)),
                    "method": int(item.get("method", 0)),
                    "novelty": int(item.get("novelty", 0)),
                    "recency": int(item.get("recency", 0)),
                    "reason_relevance": str(item.get("reason_relevance", "") or ""),
                    "reason_method": str(item.get("reason_method", "") or ""),
                    "reason_novelty": str(item.get("reason_novelty", "") or ""),
                    "reason_recency": str(item.get("reason_recency", "") or ""),
                    "overall": str(item.get("reason_overall", "") or ""),
                },
            })

        logger.info(
            f"score_papers: chunk {chunk_idx} scored "
            f"{len(results)}/{chunk_size} papers"
        )
        return results

    _CHAT_SYSTEM = (
        "你是 PaperPilot 的 AI 研究助手，帮助用户理解和管理他们的学术文献库。\n\n"
        "## 当前状态\n"
        "你正在与用户讨论一个具体的科研课题。你的回答基于：\n"
        "1. 文献库中已有的论文信息（标题、作者、摘要等）\n"
        "2. 此前对话的压缩摘要（如果存在）\n"
        "3. 用户当前问题中附带的论文详情\n\n"
        "## 能力\n"
        "- 回答关于特定论文的问题：方法、结论、创新点、局限性等\n"
        "- 对比多篇论文：找出共同点、差异、各自优势\n"
        "- 课题讨论：分析研究趋势、建议技术路线、识别研究空白\n"
        "- 文献推荐：基于用户需求从文献库中推荐相关论文\n"
        "- 当用户要求检索论文时，你可以通过 [ACTION:search] 标记触发系统的检索功能\n"
        "- 当用户要求对搜索结果评分时，你可以通过 [ACTION:score] 标记触发 AI 精排\n"
        "- 当用户要求将论文导入文献库时，你可以通过 [ACTION:import] 标记触发保存操作\n\n"
        "## 可用操作标记\n"
        "你可以输出以下标记来触发系统操作（标记不会显示给用户）：\n\n"
        "### 1. 检索论文\n"
        "[ACTION:search]\n"
        '{"topic_name": "课题名称", "topic_desc": "课题描述（中文1-3句）", '
        '"primary_keywords": ["核心英文关键词1-2个"], '
        '"secondary_keywords": ["辅助英文关键词2-4个"]}\n'
        "[/ACTION]\n"
        "使用时机：用户要求查找/搜索/检索某方向的论文时。\n"
        "先确认并拓展用户需求（用自然语言），然后输出标记。\n"
        "关键词必须翻译为英文。primary 是论文必须包含的核心词（AND 逻辑），"
        "secondary 是辅助扩展词，各 2-3 个为宜。\n\n"
        "### 2. AI 精排打分\n"
        "[ACTION:score]\n"
        '{"scope": "all", "limit": 20}\n'
        "[/ACTION]\n"
        "使用时机：用户要求对检索结果进行 AI 打分/排序时。\n"
        "前提是必须先有检索结果。\n\n"
        "### 3. 导入文献库\n"
        "[ACTION:import]\n"
        '{"project": "目标课题名", "filter": "ai_score > 80"}\n'
        "[/ACTION]\n"
        "使用时机：用户要求将论文保存/导入到某课题时。\n"
        "filter 是可选的筛选条件，如 \"ai_score > 80\"、\"ai_score >= 60\"。\n"
        "如果不需筛选，省略 filter 字段。\n\n"
        "## 风格\n"
        "- 用中文回答，专业术语保留英文原名\n"
        "- 引用论文时使用「标题（作者, 年份）」格式\n"
        "- 如果问题超出文献库信息范围，诚实说明，可以基于常识补充建议\n"
        "- 保持学术但友好的语气，像实验室讨论一样自然\n"
        "- 回答简洁但有深度，避免冗长的背景铺垫\n"
        "- 标记块放在回复末尾，不要在标记前后添加多余文字"
    )

    _COMPRESS_SYSTEM = (
        "你是一个对话摘要助手。请将以下论文课题讨论对话压缩为简短的摘要。\n\n"
        "要求：\n"
        "1. 保留用户关注的核心问题（具体论文、方法、结论等）\n"
        "2. 保留 AI 给出的关键建议和结论\n"
        "3. 丢弃寒暄和过程性讨论\n"
        "4. 中文输出，不超过 500 字"
    )

    def chat(
        self,
        project_id: int,
        project_name: str,
        message: str,
        topic_desc: str = "",
        papers: list[dict] | None = None,
        project_papers: list[dict] | None = None,
        thinking_enabled: bool = False,
        display_message: str = "",
    ) -> dict:
        """课题对话：发送消息并获取 AI 回复（自动管理上下文）。

        Args:
            project_id: 课题数据库 ID
            project_name: 课题名（用于持久化路径）
            message: 用户消息文本
            topic_desc: 课题描述（首次对话时初始化 system prompt）
            papers: 用户显式选中的论文详情列表
            project_papers: 课题下全部论文（用于自动检测 @引用 / 标题匹配）
            thinking_enabled: 是否开启深度思考模式（对比分析等场景推荐开启）

        Returns:
            {"reply": str, "compressed": bool}
        """
        if not self.is_available:
            return {"reply": "AI 服务未配置。请在 config.yaml 中设置 DeepSeek API Key。",
                    "compressed": False}

        from paperpilot.conversation import ConversationManager

        # 懒加载 ConversationManager
        if project_id not in self._conversations:
            cm = ConversationManager(project_name, topic_desc)
            self._conversations[project_id] = cm
        else:
            cm = self._conversations[project_id]
            if topic_desc:
                cm.update_topic_desc(topic_desc)

        # 自动检测论文引用（@mention / 标题匹配）
        auto_papers: list[dict] = []
        if project_papers:
            auto_papers = self._detect_paper_refs(message, project_papers)

        # 合并显式选中 + 自动检测，去重
        all_papers: list[dict] = list(papers or [])
        for ap in auto_papers:
            ap_title = (ap.get("title") or "").strip().lower()
            if not any((p.get("title") or "").strip().lower() == ap_title
                       for p in all_papers):
                all_papers.append(ap)

        # 添加用户消息
        attached_refs = None
        if all_papers:
            attached_refs = []
            for p in all_papers:
                ref = p.get("doi") or p.get("title", "")[:60]
                attached_refs.append(ref)
        cm.add_user_message(message, attached_papers=attached_refs,
                           paper_details=all_papers if all_papers else None,
                           display_content=display_message)

        # 压缩检查
        was_compressed = False
        if cm.needs_compression():
            batch = cm.get_compress_batch()
            if batch:
                summary = self._compress_messages(batch)
                if summary:
                    cm.apply_compression(summary, batch)
                    was_compressed = True

        # 构建论文目录（用于 system prompt 注入）
        paper_catalog = None
        if all_papers:
            paper_catalog = []
            for p in all_papers:
                title = (p.get("title") or "无标题")[:100]
                authors = (p.get("authors") or "未知").split(",")[0].strip()
                year = p.get("year", "")
                paper_catalog.append(f"- {title} ({authors}, {year})")

        # 构建 API 消息
        sys_prompt = self._CHAT_SYSTEM
        if topic_desc:
            sys_prompt += f"\n\n当前课题：{project_name}\n课题描述：{topic_desc}"

        messages = cm.build_api_messages(sys_prompt, paper_catalog)

        # 调用 LLM（支持两步推理：reasoning_model 显式配置时，先深度推理再生成）
        reasoning_model = self._resolve_task_model("reasoning")
        chat_model = self._resolve_task_model("chat")

        if reasoning_model:
            # 两步模式：reasoning 模型推理 → chat 模型生成回复
            logger.info(
                f"chat: two-step mode — reasoning={reasoning_model}, output={chat_model}"
            )
            # Step 1: reasoning_model 推理
            _, reasoning = self._call_api_full(
                messages, temperature=0.6, max_tokens=2000,
                timeout=120, thinking=True,
                model=reasoning_model,
            )
            if reasoning:
                # Step 2: chat_model 基于推理结果生成回复
                messages.append({
                    "role": "system",
                    "content": f"[内部推理结果，基于此生成回复]\n{reasoning}"
                })
                reply = self._call_api(
                    messages, temperature=0.6, max_tokens=3000,
                    timeout=120, thinking=False,
                    model=chat_model,
                )
            else:
                # 推理失败，回退到单步 chat 模型
                logger.warning("chat: reasoning returned empty, falling back to single-step")
                reply = self._call_api(
                    messages, temperature=0.6, max_tokens=3000,
                    timeout=120, thinking=False,
                    model=chat_model,
                )
        else:
            # 单步模式：直接调用 chat_model
            thinking = True if thinking_enabled else None
            reply = self._call_api(messages, temperature=0.6, max_tokens=3000,
                                   timeout=120, thinking=thinking,
                                   model=chat_model)

        # 保存原始回复（含 ACTION 标签）供 API 上下文学习；UI 显示用剥离版
        if reply:
            clean = re.sub(
                r'\s*\[ACTION:\w+\].+?\[/ACTION\]\s*', '', reply, flags=re.DOTALL
            ).strip()
            clean = re.sub(
                r'\s*\[PROJECT_UPDATE\].+?\[/PROJECT_UPDATE\]\s*', '', clean, flags=re.DOTALL
            ).strip()
            cm.add_assistant_message(reply, display_content=clean if clean != reply else "")

        return {"reply": reply, "compressed": was_compressed}

    def chat_stream(
        self,
        project_id: int,
        project_name: str,
        message: str,
        topic_desc: str = "",
        papers: list[dict] | None = None,
        project_papers: list[dict] | None = None,
        thinking_enabled: bool = False,
        display_message: str = "",
    ):
        """课题对话流式版：yield {"chunk": str} | {"done": dict} | {"error": str}。

        与 chat() 逻辑相同，但通过 SSE 流式返回内容增量。
        对话持久化在流结束后自动完成。
        """
        if not self.is_available:
            yield {"error": "AI 服务未配置。请在 config.yaml 中设置 DeepSeek API Key。"}
            return

        from paperpilot.conversation import ConversationManager

        if project_id not in self._conversations:
            cm = ConversationManager(project_name, topic_desc)
            self._conversations[project_id] = cm
        else:
            cm = self._conversations[project_id]
            if topic_desc:
                cm.update_topic_desc(topic_desc)

        # 自动检测论文引用
        auto_papers: list[dict] = []
        if project_papers:
            auto_papers = self._detect_paper_refs(message, project_papers)
        all_papers: list[dict] = list(papers or [])
        for ap in auto_papers:
            ap_title = (ap.get("title") or "").strip().lower()
            if not any((p.get("title") or "").strip().lower() == ap_title
                       for p in all_papers):
                all_papers.append(ap)

        attached_refs = None
        if all_papers:
            attached_refs = []
            for p in all_papers:
                ref = p.get("doi") or p.get("title", "")[:60]
                attached_refs.append(ref)
        cm.add_user_message(message, attached_papers=attached_refs,
                           paper_details=all_papers if all_papers else None,
                           display_content=display_message)

        # 压缩检查
        was_compressed = False
        if cm.needs_compression():
            batch = cm.get_compress_batch()
            if batch:
                summary = self._compress_messages(batch)
                if summary:
                    cm.apply_compression(summary, batch)
                    was_compressed = True

        # 构建论文目录
        paper_catalog = None
        if all_papers:
            paper_catalog = []
            for p in all_papers:
                title = (p.get("title") or "无标题")[:100]
                authors = (p.get("authors") or "未知").split(",")[0].strip()
                year = p.get("year", "")
                paper_catalog.append(f"- {title} ({authors}, {year})")

        sys_prompt = self._CHAT_SYSTEM
        if topic_desc:
            sys_prompt += f"\n\n当前课题：{project_name}\n课题描述：{topic_desc}"

        messages = cm.build_api_messages(sys_prompt, paper_catalog)

        full_reply = ""
        try:
            reasoning_model = self._resolve_task_model("reasoning")
            chat_model = self._resolve_task_model("chat")

            if reasoning_model:
                # 两步模式：reasoning_model 推理（同步）→ chat_model 流式输出
                _, reasoning = self._call_api_full(
                    messages, temperature=0.6, max_tokens=2000,
                    timeout=120, thinking=True,
                    model=reasoning_model,
                )
                if reasoning:
                    messages.append({
                        "role": "system",
                        "content": f"[内部推理结果，基于此生成回复]\n{reasoning}"
                    })
                thinking = False
            else:
                thinking = True if thinking_enabled else None

            for delta in self._call_api_stream(messages, temperature=0.6,
                                                max_tokens=3000, timeout=120,
                                                thinking=thinking,
                                                model=chat_model):
                full_reply += delta
                yield {"chunk": delta}
        except Exception as e:
            logger.warning(f"Streaming chat failed: {e}")
            if full_reply:
                cm.add_assistant_message(full_reply + "\n\n[流输出中断]")
            yield {"error": str(e)}
            return

        if full_reply:
            clean = re.sub(
                r'\s*\[ACTION:\w+\].+?\[/ACTION\]\s*', '', full_reply, flags=re.DOTALL
            ).strip()
            clean = re.sub(
                r'\s*\[PROJECT_UPDATE\].+?\[/PROJECT_UPDATE\]\s*', '', clean, flags=re.DOTALL
            ).strip()
            cm.add_assistant_message(full_reply, display_content=clean if clean != full_reply else "")

        yield {"done": {"reply": full_reply, "compressed": was_compressed}}

    def _compress_messages(self, messages: list[dict]) -> str | None:
        """调用 API 将一批消息压缩为摘要。"""
        if not messages:
            return None

        # 格式化为可读文本
        lines = []
        for m in messages:
            role = "用户" if m["role"] == "user" else "助手"
            content = m.get("content", "")[:2000]
            lines.append(f"[{role}]: {content}")

        compress_prompt = "\n\n".join(lines)
        api_messages = [
            {"role": "system", "content": self._COMPRESS_SYSTEM},
            {"role": "user", "content": f"请压缩以下对话：\n\n{compress_prompt}"},
        ]

        try:
            summary = self._call_api(api_messages, temperature=0.2, max_tokens=800, timeout=60)
            return summary.strip() if summary else None
        except Exception:
            logger.warning("压缩对话失败", exc_info=True)
            return None

    # ── 论文问答 ──

    _QUESTION_TRIGGERS = {
        '方法', '实验', '数据', '细节', '具体', '怎么', '如何实现',
        '图', '表', '证据', '样本', '参数', '指标', '测量', '统计',
        'protocol', 'procedure', '全文', '正文', '原文',
        'method', 'experiment', 'data', 'detail', 'figure', 'table',
        '怎么做', '用了什么', '如何', '怎样', '流程', '步骤',
        '不足', '局限', '缺陷', '改进', 'limitation',
        '结果', '发现', '结论', '证明', '验证',
    }

    def _detect_paper_refs(self, message: str,
                           project_papers: list[dict]) -> list[dict]:
        """从用户消息中检测论文引用。

        两层检测：
        1. @mention：@论文标题（或部分标题）
        2. 标题子串：消息中包含标题 ≥12 字符的连续片段
        """
        matched: list[dict] = []
        msg_lower = message.lower()

        # 提取 @mention 文本
        at_mentions = re.findall(r'@(.+?)(?:$|[\n@,\.。，!！?？])', message)
        at_texts = [m.strip().lower() for m in at_mentions if len(m.strip()) >= 3]

        for paper in project_papers:
            title = (paper.get("title") or "").strip()
            if len(title) < 8:
                continue
            title_lower = title.lower()

            # ① 完整标题出现在消息中
            if title_lower in msg_lower:
                if paper not in matched:
                    matched.append(paper)
                continue

            # ② @mention 匹配
            for at_text in at_texts:
                if len(at_text) >= 3 and (at_text in title_lower
                                          or title_lower in at_text):
                    if paper not in matched:
                        matched.append(paper)
                    break
            else:
                # ③ 子串匹配：标题 ≥15 字符时，检查 12 字符滑动窗口
                if len(title) >= 15:
                    for i in range(len(title_lower) - 11):
                        chunk = title_lower[i:i + 12]
                        # 跳过纯空白/标点片段
                        if chunk in msg_lower and not chunk.isspace() and any(
                            c.isalnum() for c in chunk
                        ):
                            if paper not in matched:
                                matched.append(paper)
                            break

        return matched

    def _needs_full_text(self, question: str) -> bool:
        """判断问题是否需要全文（而非仅摘要）。"""
        q = question.lower()
        return any(t.lower() in q for t in self._QUESTION_TRIGGERS)

    def ask_question(
        self,
        paper: dict,
        question: str,
        full_text: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """针对单篇论文的深度问答。

        默认注入摘要；若问题涉及方法/数据/细节且全文可获取，则注入全文。

        Args:
            paper: paper dict（需含 title, abstract）
            question: 用户问题
            full_text: 论文全文（可选，为 None 时按需自动获取）
            session_id: 多轮对话会话 ID（可选）

        Returns:
            AI 回答文本，失败返回空字符串
        """
        if not self.is_available:
            return ""

        title = (paper.get("title") or "").strip()
        abstract = (paper.get("abstract") or "").strip()
        if not title:
            return ""

        # 按需获取全文
        if not full_text and self._needs_full_text(question):
            full_text, _ = get_full_text_for_paper(paper)

        # 构建论文内容
        content = f"论文标题：《{title}》\n"
        if abstract:
            content += f"摘要：{abstract[:1000]}\n"
        if full_text:
            content += f"\n全文（{len(full_text)} 字符）：\n{full_text[:30000]}\n"

        system = (
            "你是一位资深学术审稿人。请基于提供的论文内容回答用户问题。\n\n"
            "要求：\n"
            "- 用中文回答，专业术语保留英文原名\n"
            "- 引用论文中的具体内容支撑你的回答\n"
            "- 论文未涉及的问题，诚实说明而非编造\n"
            "- 回答简洁有深度，避免冗长的背景铺垫"
        )

        messages: list[dict] = [{"role": "system", "content": system}]

        # 多轮对话：追加历史
        if session_id and session_id in self._qa_sessions:
            messages.extend(self._qa_sessions[session_id])

        messages.append({
            "role": "user",
            "content": f"{content}\n\n用户问题：{question}",
        })

        reply = self._call_api(messages, temperature=0.5, max_tokens=2000,
                               timeout=120)

        # 保存多轮对话历史
        if session_id and reply:
            if session_id not in self._qa_sessions:
                self._qa_sessions[session_id] = []
            self._qa_sessions[session_id].append({
                "role": "user", "content": question,
            })
            self._qa_sessions[session_id].append({
                "role": "assistant", "content": reply,
            })

        return reply

    def log_message(self, project_id: int, project_name: str,
                    role: str, content: str, topic_desc: str = "") -> None:
        """保存一条消息到课题对话记录（不调用 API）。

        供 deep_read 等非 chat() 流程使用，确保所有 Agent 面板的
        AI 交互都计入 conversation.json。
        """
        from paperpilot.conversation import ConversationManager

        if project_id not in self._conversations:
            cm = ConversationManager(project_name, topic_desc)
            self._conversations[project_id] = cm
        else:
            cm = self._conversations[project_id]
            if topic_desc:
                cm.update_topic_desc(topic_desc)

        if role in ("user", "system"):
            cm.add_user_message(content)
        else:
            cm.add_assistant_message(content)

    def get_conversation_info(self, project_id: int) -> dict | None:
        """获取课题对话的摘要信息（不修改对话）。"""
        cm = self._conversations.get(project_id)
        if cm is None:
            return None
        return {
            "total_rounds": cm.total_rounds,
            "estimated_tokens": cm.estimated_tokens,
            "is_empty": cm.is_empty,
            "compressed_count": len(cm.compressed_summaries),
            "display_messages": cm.display_messages,
            "compressed_summaries": cm.compressed_summaries,
            "has_more_history": cm.has_more_history,
        }


# ── 持久化 ──

def save_deep_read_json(paper: dict, result: dict) -> str | None:
    """将精读结果保存为本地 JSON 文件。

    Args:
        paper: paper dict（需含 title）
        result: deep_read 返回的结果 dict

    Returns:
        保存的文件路径，失败返回 None
    """
    title = (paper.get("title") or "untitled").strip()
    slug = re.sub(r"[^\w\-]", "_", title[:60].lower())
    slug = re.sub(r"_+", "_", slug).strip("_") or hashlib.md5(title.encode()).hexdigest()[:12]

    _DEEP_READ_DIR.mkdir(parents=True, exist_ok=True)
    path = _DEEP_READ_DIR / f"{slug}.json"
    try:
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)
    except Exception as e:
        logger.warning(f"Failed to save deep read JSON: {e}")
        return None
