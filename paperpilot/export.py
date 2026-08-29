"""论文数据导出 — BibTeX / CSV。"""

import csv
import io
import re
from pathlib import Path


def to_bibtex(papers: list[dict]) -> str:
    """论文列表 → BibTeX 字符串。"""
    lines: list[str] = []
    for paper in papers:
        key = _bibtex_key(paper)
        etype = _bibtex_type(paper)

        lines.append(f"@{etype}{{{key},")
        lines.append(f"  title = {{{_escape_bibtex(paper.get('title', ''))}}},")

        authors = paper.get("authors", "")
        if authors:
            lines.append(f"  author = {{{_format_bibtex_authors(authors)}}},")

        year = paper.get("year")
        if year:
            lines.append(f"  year = {{{year}}},")

        journal = paper.get("journal") or paper.get("source", "")
        if etype == "article" and journal:
            lines.append(f"  journal = {{{_escape_bibtex(journal)}}},")

        doi = paper.get("doi", "")
        if doi:
            lines.append(f"  doi = {{{doi}}},")

        url = paper.get("url", "")
        if url:
            lines.append(f"  url = {{{url}}},")

        abstract = paper.get("abstract", "")
        if abstract:
            lines.append(f"  abstract = {{{_escape_bibtex(abstract[:500])}}},")

        lines.append("}")
        lines.append("")

    return "\n".join(lines)


def _bibtex_key(paper: dict) -> str:
    """生成 BibTeX entry key：第一作者姓氏 + 年份 + 标题首词。"""
    authors = (paper.get("authors") or "Unknown").strip()
    first = authors.split(",")[0].strip()
    surname = first.split()[-1] if first.split() else "Unknown"
    surname = re.sub(r"[^\w]", "", surname).lower()[:30]
    if not surname or surname in {"unknown", "anonymous", "anon"}:
        surname = "unknown"

    year = str(paper.get("year") or "")

    title = (paper.get("title") or "untitled").strip()
    title_words = [w for w in re.findall(r"[a-zA-Z]+", title) if len(w) > 2 and w.lower() not in {
        "the", "for", "and", "with", "from", "that", "this", "have", "been", "was",
        "are", "not", "but", "its", "all", "has", "had", "can", "may",
    }]
    title_word = title_words[0].lower() if title_words else "untitled"

    return f"{surname}{year}{title_word}"[:60]


def _bibtex_type(paper: dict) -> str:
    journal = (paper.get("journal") or "").strip()
    if journal and journal.lower() != "arxiv":
        return "article"
    return "misc"


_BIBTEX_ESCAPES = {
    "\\": "\\textbackslash{}",
    "&": "\\&", "%": "\\%", "$": "\\$", "#": "\\#", "_": "\\_",
    "{": "\\{", "}": "\\}",
    "~": "\\textasciitilde{}", "^": "\\textasciicircum{}",
}


def _escape_bibtex(text: str) -> str:
    """转义 LaTeX 特殊字符。

    必须单遍替换：逐字符 replace 会让先插入的反斜杠被后继规则二次转义
    （旧实现 `&` → `\\&` 后，`\\` 再被转成 `\\textbackslash{}`，产出非法 BibTeX）。
    """
    return re.sub(r'[\\&%$#_{}~^]', lambda m: _BIBTEX_ESCAPES[m.group(0)], text)


def _format_bibtex_authors(authors: str) -> str:
    """将 'A, B, C' 转为 'A and B and C' 格式。"""
    names = [n.strip() for n in authors.split(",") if n.strip()]
    return " and ".join(names)


def to_csv(papers: list[dict]) -> str:
    """论文列表 → CSV 字符串（含表头）。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    writer.writerow(["标题", "作者", "年份", "来源", "期刊", "引用数", "DOI", "URL"])
    for paper in papers:
        writer.writerow([
            paper.get("title", ""),
            paper.get("authors", ""),
            paper.get("year", ""),
            paper.get("source", ""),
            paper.get("journal", ""),
            paper.get("cited_by_count", ""),
            paper.get("doi", ""),
            paper.get("url", ""),
        ])

    return buf.getvalue()


def save_file(content: str, path: str) -> None:
    """UTF-8 写入文件，自动创建父目录。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
