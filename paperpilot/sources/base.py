"""数据源统一抽象层（PHASE3_PLAN §2.2 / §5.2）。

所有论文数据源实现 PaperSource 接口并注册到本模块注册表：
    source.fetch_raw(query, max_results, year_min, year_max)   # 原始查询串抓取
    source.fetch(keywords, max_results, logic, ...)            # 关键词引号包裹 → fetch_raw
    source.is_available()                                      # 读 config data_sources.{name}

统一输出 paper dict（完整协议见 paperpilot.fetcher 模块 docstring）：
    title / authors / abstract / year / source / url / doi /
    api_score / type / cited_by_count / journal

新增数据源步骤：
    1. 本包内新建 xxx_source.py，实现 PaperSource 子类并调用 register_source()
    2. 在 __init__.py import 该模块完成注册
    3. fetcher._FETCH_RAW 由注册表自动构建，无需额外接线

本模块保持零重依赖（仅 config + diskcache 可选），避免与 fetcher 形成循环导入。
"""

from pathlib import Path

from paperpilot.config import config

try:
    from diskcache import Cache as _Cache
except Exception:  # pragma: no cover - diskcache 不可用时降级为不缓存
    _Cache = None


def _build_search_query(keywords: list[str], logic: str = "OR") -> str:
    """Build quoted-phrase query for search APIs (arXiv, OpenAlex, Europe PMC).

    Args:
        keywords: list of keyword phrases
        logic: "OR" (default, broad recall) or "AND" (strict, all must match)
    """
    joiner = " AND " if logic == "AND" else " OR "
    return joiner.join(f'"{kw}"' for kw in keywords)


def cache_ttl_seconds() -> int:
    """API 响应缓存 TTL（秒），来自 config cache.ttl_hours。"""
    return int(config.get("cache", {}).get("ttl_hours", 24)) * 3600


def api_cache_dir() -> Path:
    """API 响应缓存根目录（config cache.dir，默认 ./cache/api）。"""
    return Path(config.get("cache", {}).get("dir", "./cache/api"))


def open_cache(rel_subdir: str):
    """打开 API 响应缓存（diskcache），不可用时返回 None 降级为不缓存。"""
    if _Cache is None:
        return None
    try:
        return _Cache(str(api_cache_dir() / rel_subdir))
    except Exception:
        return None


class SourceRateLimited(RuntimeError):
    """数据源限流/被拒（HTTP 429 重试耗尽，或 403 被拒绝访问）。

    由各源实现抛出；编排层（fetcher）捕获后保持降级语义，
    并通过可选 errors 列表回传给 UI 做明确提示。
    """

    def __init__(self, source: str, status: int, message: str = ""):
        self.source = source
        self.status = status
        self.message = message or f"{source} HTTP {status}"
        super().__init__(self.message)


class PaperSource:
    """论文数据源基类。子类需设置 name/label 并实现 fetch_raw。"""

    #: 内部标识，须与 paper dict 的 source 字段、config data_sources 键一致
    name: str = ""
    #: UI 展示名
    label: str = ""
    #: 一句话说明（UI 提示用）
    description: str = ""
    #: config data_sources.{name} 缺失时的默认开关状态
    default_enabled: bool = False
    #: 与 fetch_raw 等价的模块级函数（供 fetcher._FETCH_RAW 注册表直引）
    raw_fetcher = None

    def fetch(self, keywords: list[str], max_results: int = 30,
              logic: str = "OR", year_min: str = "", year_max: str = "") -> list[dict]:
        """按关键词检索：引号包裹查询串后调用 fetch_raw（与各源原 fetch_xxx 一致）。"""
        if not keywords:
            return []
        query = _build_search_query(keywords, logic=logic)
        return self.fetch_raw(query, max_results, year_min=year_min, year_max=year_max)

    def fetch_raw(self, query: str, max_results: int = 30,
                  year_min: str = "", year_max: str = "") -> list[dict]:
        """以原始查询串抓取，返回统一 paper dict 列表。子类必须实现。"""
        raise NotImplementedError

    def is_available(self) -> bool:
        """数据源是否启用（config data_sources.{name}，缺失回退 default_enabled）。"""
        enabled = config.get("data_sources", {}) or {}
        return bool(enabled.get(self.name, self.default_enabled))

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"


# ── 数据源注册表 ──

_REGISTRY: dict[str, PaperSource] = {}


def register_source(source: PaperSource) -> None:
    """注册一个数据源实例（同名覆盖）。"""
    if not source.name:
        raise ValueError("PaperSource.name 不能为空")
    _REGISTRY[source.name] = source


def get_source(name: str) -> PaperSource | None:
    """按名称取数据源实例，未注册返回 None。"""
    return _REGISTRY.get(name)


def all_sources() -> list[PaperSource]:
    """返回全部已注册数据源（按注册顺序）。"""
    return list(_REGISTRY.values())
