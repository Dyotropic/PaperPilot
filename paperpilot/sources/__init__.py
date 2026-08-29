"""PaperPilot 数据源包（PHASE3_PLAN §2.2 数据源统一抽象层）。

import 本包即完成内置数据源注册（arxiv / openalex / europepmc）。
注册表与接口见 base.py；各源实现见同目录 *_source.py。
"""

from paperpilot.sources.base import (  # noqa: F401
    PaperSource,
    all_sources,
    get_source,
    register_source,
)
from paperpilot.sources.arxiv_source import (  # noqa: F401  注册副作用
    ArxivSource,
    fetch_arxiv,
)
from paperpilot.sources.openalex_source import (  # noqa: F401  注册副作用
    OpenAlexSource,
    fetch_openalex,
)
from paperpilot.sources.europepmc_source import (  # noqa: F401  注册副作用
    EuropePMCSource,
    fetch_europepmc,
)

__all__ = [
    "PaperSource",
    "ArxivSource",
    "OpenAlexSource",
    "EuropePMCSource",
    "register_source",
    "get_source",
    "all_sources",
    "fetch_arxiv",
    "fetch_openalex",
    "fetch_europepmc",
]
