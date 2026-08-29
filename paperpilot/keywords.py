"""关键词提取接口。

中文：jieba TF-IDF 生成候选术语 → 本地多语言模型语义打分 → 阈值过滤泛化词
英文：KeyBERT 内置 CountVectorizer n-gram（原生支持空格分词）
"""

import os
import re
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import jieba
import jieba.analyse
import numpy as np
from keybert import KeyBERT
from sentence_transformers import SentenceTransformer

# ── jieba 专业术语自定义词典 ──
# 防止 TF-IDF 阶段将复合术语错误切分（如"硅基高动态"被拆成无意义词元）
_JIEBA_CUSTOM_TERMS = [
    # 通信/网络
    "确定性时延", "天地一体化", "视联网", "低空智联网", "认知组网",
    "光交换网络", "智算中心", "量子密钥分发", "低轨卫星", "水声通信",
    "脑机接口", "深空探测", "多维复用", "硅基光子", "异质集成",
    # 光电/传感器
    "硅基高动态", "单光子全彩夜视", "光电融合计算", "光电探测芯片",
    "固态激光雷达", "紫外光电晶体管", "宽禁带半导体", "单光子探测",
    "光子类脑计算", "图像传感器", "太赫兹激光器", "光纤激光器",
    "光声光谱", "数字全息", "编码孔径", "红外热辐射",
    # 医学/生命科学
    "巨噬细胞极化", "线粒体功能障碍", "肿瘤免疫微环境", "铁死亡",
    "细胞焦亡", "细胞间通讯", "肠道菌群", "宿主代谢", "空间组学",
    "核酸适配体", "靶向递送", "光声成像", "光疗", "超构表面",
    "高分辨成像", "多组学整合", "数字染色", "心衰预警",
    "代谢性疾病", "心血管疾病", "神经退行性疾病", "急性肺损伤",
    "遗传性疾病", "医学影像诊断", "毒性预测", "骨关节炎",
    # 控制/机器人
    "无人集群", "态势评估", "协同控制", "避碰控制", "任务分配",
    "三维感知", "柔顺作业", "水陆自适应", "抗干扰控制",
    "迁移强化学习", "多模态控制", "类脑智能决策", "自主导航",
    # 能源/环境
    "电氢耦合", "碳排放", "黄河流域", "产业结构", "微塑料",
    "精准农业", "多源污染", "传感器网络", "多时间尺度",
    # 计算机/AI
    "神经符号数据库", "幻觉检测", "异构芯片", "安全测试",
    "故障根因", "可视分析", "图数据", "逼近理论",
    "认知激活", "参数高效微调", "持续演进",
    # 社科
    "乡村旅游", "县域经济", "产业融合", "政府监管", "公共安全",
    "特大城市", "韧性治理", "社会保障", "医疗体制改革",
    "共同富裕", "粮食安全", "马克思主义", "中国式现代化",
    "拔尖创新人才", "心理健康", "红色基因", "跨文化传播",
    "国际中文教育", "碎片化学习", "沉浸式学习",
]
for _term in _JIEBA_CUSTOM_TERMS:
    jieba.add_word(_term, freq=100, tag="nz")

_EMBED_MODEL_PATH = str(Path.home() / ".cache/modelscope/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
_EMBED_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
_EMBED_MODEL_SRC = _EMBED_MODEL_PATH if Path(_EMBED_MODEL_PATH).exists() else _EMBED_MODEL_NAME

_KEYBERT_MODEL_PATH = str(Path.home() / ".cache/modelscope/sentence-transformers/all-MiniLM-L6-v2")
_KEYBERT_MODEL_NAME = "all-MiniLM-L6-v2"
_KEYBERT_MODEL_SRC = _KEYBERT_MODEL_PATH if Path(_KEYBERT_MODEL_PATH).exists() else _KEYBERT_MODEL_NAME

_CHINESE_STOP = {
    "的", "了", "在", "是", "和", "与", "及", "或", "对", "从", "被", "把", "将",
    "于", "等", "其", "该", "所", "为", "而", "且", "以", "并", "到", "要", "有",
    "更", "不", "也", "都", "就", "能", "会", "可", "已", "还", "只", "但",
    "用于", "基于", "采用", "通过", "一种", "一个", "这个", "那个", "什么",
    "怎么", "如何", "哪些", "哪", "吗", "呢", "吧", "啊", "呀",
    "提出", "证明", "表明", "发现", "使用", "利用", "可以", "能够",
    "研究", "方法", "分析", "实验", "结果", "影响", "作用", "过程",
    "问题", "应用", "发展", "技术", "系统", "模型", "数据", "性能",
    "特性", "结构", "设计", "制备", "计算", "理论", "实践",
    "特征", "策略", "机制", "综述", "进展", "挑战", "现状", "趋势",
    "关键", "主要", "重要", "不同", "相关", "相应", "显著",
    # 从 100 课题测试中发现的泛化词（非技术术语，无跨语言映射价值）
    "中心", "条件", "体系", "全国", "属性", "变革", "传承", "革命",
    "文明", "基础", "实现", "测试", "构建",
    "治理", "保护", "利用", "面向", "建设", "促进", "提升", "模式",
    "路径", "目标", "重点", "功能", "融合", "协同", "优化", "调控",
    "干预", "预警", "诊断", "预测",
}
_MIN_LEN = 2
_CANDIDATE_COUNT = 30
_SCORE_THRESHOLD_RATIO = 0.4

_embed_model: SentenceTransformer | None = None
_kw_model: KeyBERT | None = None


def _has_chinese(text: str) -> bool:
    return bool(re.search(r"[一-鿿]", text))


def _get_embed_model() -> SentenceTransformer:
    global _embed_model
    if _embed_model is None:
        _embed_model = SentenceTransformer(_EMBED_MODEL_SRC)
    return _embed_model


def _get_kw_model() -> KeyBERT:
    global _kw_model
    if _kw_model is None:
        _kw_model = KeyBERT(model=_KEYBERT_MODEL_SRC)
    return _kw_model


def _chinese_extract(text: str, top_n: int) -> list[str]:
    """中文关键词提取：jieba TF-IDF 候选 → 多语言模型语义打分。"""
    raw = jieba.analyse.extract_tags(text, topK=_CANDIDATE_COUNT)
    candidates = [c.strip() for c in raw if len(c.strip()) >= _MIN_LEN and c.strip() not in _CHINESE_STOP]
    if not candidates:
        return []
    model = _get_embed_model()
    doc_vec = model.encode([text], normalize_embeddings=True)
    cand_vecs = model.encode(candidates, normalize_embeddings=True)
    scores = (cand_vecs @ doc_vec.T).flatten()
    scored = sorted(zip(candidates, scores), key=lambda x: -x[1])
    threshold = scored[0][1] * _SCORE_THRESHOLD_RATIO
    return [kw for kw, s in scored if s >= threshold][:top_n]


def _english_extract(text: str, top_n: int) -> list[str]:
    """英文关键词提取：KeyBERT 内置 n-gram。"""
    model = _get_kw_model()
    results = model.extract_keywords(
        text,
        top_n=top_n,
        stop_words="english",
        keyphrase_ngram_range=(1, 2),
        use_mmr=True,
        diversity=0.5,
    )
    return [kw.strip() for kw, _ in results]


def extract_keywords(topic_description: str, top_n: int = 10) -> list[str]:
    """从课题描述中自动提取技术术语。

    优先使用 DeepSeek API 进行细粒度中文关键词提取，
    API 不可用时回退到 jieba TF-IDF / KeyBERT。

    Args:
        topic_description: 课题描述文本（1-3 句话）
        top_n: 返回关键词数量（仅回退模式生效）

    Returns:
        关键词字符串列表
    """
    from paperpilot.core_extractor import extract_regular_keywords

    keywords = extract_regular_keywords(topic_description)
    if keywords:
        return keywords[:top_n]
    # Fallback to jieba / KeyBERT
    if _has_chinese(topic_description):
        return _chinese_extract(topic_description, top_n)
    return _english_extract(topic_description, top_n)


def extract_keywords_local(text: str, top_n: int = 10) -> list[str]:
    """纯本地关键词提取（绝不调用 LLM）：中文 jieba+语义打分，英文 KeyBERT。

    供知识图谱逐篇提取论文关键词等批处理场景使用——批量调用时
    不能走 extract_keywords 的 LLM 优先路径（N 篇 = N 次 API 调用）。
    """
    text = (text or "").strip()
    if not text:
        return []
    try:
        if _has_chinese(text):
            return _chinese_extract(text, top_n)
        return _english_extract(text, top_n)
    except Exception:
        return []


def extract_all_keywords(topic: str, top_n: int = 10) -> list[tuple[str, float]]:
    """提取带权重的关键词列表：核心关键词（权重高）+ 普通关键词（权重低）。

    核心关键词和普通关键词均通过 DeepSeek API 提取。
    核心关键词置于列表前部，权重 1.0；普通关键词权重 0.75。

    Args:
        topic: 课题描述文本
        top_n: 普通关键词数量（仅 fallback 时生效）

    Returns:
        [(keyword, weight), ...] 列表，核心在前
    """
    from paperpilot.core_extractor import extract_core_keywords, extract_regular_keywords

    # 核心关键词提取仅对中文输入生效：其系统 prompt 是中文的，
    # 对英文输入会错误返回中文关键词，污染搜索结果。
    if _has_chinese(topic):
        core_keywords = extract_core_keywords(topic)
    else:
        core_keywords = []

    regular_keywords = extract_regular_keywords(topic)
    if not regular_keywords:
        # Fallback to jieba if DeepSeek API fails
        regular_keywords = extract_keywords(topic, top_n=top_n)

    # Remove regular keywords that overlap with core keywords
    core_lower = {kw.lower() for kw in core_keywords}
    regular_keywords = [kw for kw in regular_keywords if kw.lower() not in core_lower]

    result = []
    for kw in core_keywords:
        result.append((kw, 1.0))
    for kw in regular_keywords:
        result.append((kw, 0.75))
    return result


def merge_keywords(
    auto_keywords: list[str] | list[tuple[str, float]],
    manual_keywords: list[str],
) -> list[str]:
    """合并自动提取和手动输入的关键词，去重、去空、保持手动优先。

    支持带权重的自动关键词：手动关键词默认权重最高，置顶排列。
    返回纯字符串列表（向后兼容）。
    """
    seen = set()
    merged = []
    for kw in manual_keywords:
        kw_clean = kw.strip().lower()
        if kw_clean and kw_clean not in seen:
            seen.add(kw_clean)
            merged.append(kw_clean)

    # Handle both weighted [(str, float)] and plain [str] formats
    for item in auto_keywords:
        if isinstance(item, tuple):
            kw = item[0]
        else:
            kw = item
        kw_clean = kw.strip().lower()
        if kw_clean and kw_clean not in seen:
            seen.add(kw_clean)
            merged.append(kw_clean)
    return merged


def merge_keywords_weighted(
    auto_keywords: list[tuple[str, float]],
    manual_keywords: list[str],
) -> list[tuple[str, float]]:
    """合并带权重关键词，手动关键词权重最高（1.0），保留权重排序。"""
    seen = set()
    result = []
    for kw in manual_keywords:
        kw_clean = kw.strip().lower()
        if kw_clean and kw_clean not in seen:
            seen.add(kw_clean)
            result.append((kw_clean, 1.0))
    for kw, weight in auto_keywords:
        kw_clean = kw.strip().lower()
        if kw_clean and kw_clean not in seen:
            seen.add(kw_clean)
            result.append((kw_clean, weight))
    return result
