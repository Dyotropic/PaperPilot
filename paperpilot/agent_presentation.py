"""Human-facing Agent text, separate from immutable model/tool records.

Repair double-escaped prose only; code, Windows paths and scientific identifiers
are never globally decoded. Evidence remains in the journal, with readable source
labels in chat instead of private task UUIDs.
"""
import re


_CODE = re.compile(r"(```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|`[^`\n]+`)")


def prose_newlines(text):
    """Decode misplaced literal line breaks without touching path/code syntax."""
    parts = _CODE.split(text)
    for index in range(0, len(parts), 2):
        # Require prose or Markdown at both sides. Never reinterpret C:\new,
        # a regex, JSON or an otherwise valid single escaped string.
        parts[index] = re.sub(
            r"(?<=[。！？；：，）\u4e00-\u9fff])(?:\\r)?\\n(?=(?:\\n)*\s*(?:[-*#【\u4e00-\u9fff]|$))",
            "\n", parts[index])
        parts[index] = re.sub(r"(?<=\n)\\n(?=\s*(?:[-*#【\u4e00-\u9fff]|$))", "\n", parts[index])
    return "".join(parts)


def user_text(text, task=None):
    """Readable projection; callers retain original text in durable history."""
    labels = {}
    for entry in (task or {}).get("evidence", []):
        name = entry.get("path") or {
            "calculate": "计算结果", "list_files": "工作区文件列表",
            "search_files": "工作区检索结果", "read_library": "课题文献库",
            "search_papers": "文献检索结果", "read_dataset": "文献资料",
            "read_attachment": "原始附件", "team_dispatch": "团队分析",
            "save_to_library": "入库记录",
        }.get(entry.get("tool"), "执行记录")
        labels[entry["id"]] = name
    parts = _CODE.split(prose_newlines(text))
    for index in range(0, len(parts), 2):
        value = parts[index]
        # Only known IDs, or explicitly labelled evidence IDs in old replies.
        for key, label in labels.items():
            value = value.replace(key, label)
        value = re.sub(r"(?:evidence_id|证据(?:编号|标识)?)\s*[:：=]?\s*[0-9a-f]{32}\b", "执行记录", value, flags=re.I)
        parts[index] = value
    return "".join(parts)


def edit_scope(preview):
    lines = preview.get("diff", "").splitlines()
    added = sum(line.startswith("+") and not line.startswith("+++") for line in lines)
    removed = sum(line.startswith("-") and not line.startswith("---") for line in lines)
    return added, removed
