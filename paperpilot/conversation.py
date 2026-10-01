"""对话上下文管理 — 持久化、压缩、回滚。

存储：旧记录 repository/{课题名}/conversation.json；新会话 sessions/{session_id}/
包含原始 events.jsonl 日志与可重建的 conversation.json。
与 catalog.json 并列，课题删除时随 repo_manager 回收站一起移除。

结构：
{
  "_meta": { project_name, created_at, updated_at, total_rounds, estimated_tokens, compressed_count },
  "messages": [ {role, content, attached_papers?, timestamp}, ... ],
  "compressed": [ {rounds_summary, original_rounds, compressed_at}, ... ]
}
"""

import copy
import json
import logging
import os
import re
import time
import threading
from datetime import datetime
from pathlib import Path

from paperpilot.repo_manager import _get_app_dir, atomic_write_text

logger = logging.getLogger(__name__)

_REPO_ROOT = _get_app_dir() / "repository"

# 128K 上下文窗口，80K 触发压缩，留 48K 给回复
_MAX_TOKENS = 80000
# 初始加载显示最近 N 轮对话
_DISPLAY_ROUNDS = 30
# 每"轮" = 1 user + 1 assistant
# token 估算系数
_CHINESE_CHAR_RATIO = 1.3
_ENGLISH_CHAR_RATIO = 0.75


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算：中文字符 ~1.3 tokens，英文 ~0.75。"""
    chinese = len(re.findall(r"[一-鿿㐀-䶿]", text))
    other = len(text) - chinese
    return int(chinese * _CHINESE_CHAR_RATIO + other * _ENGLISH_CHAR_RATIO)


def _estimate_messages_tokens(messages: list[dict]) -> int:
    """估算消息列表的总 token 数。"""
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += _estimate_tokens(content)
        # 附加论文
        papers = m.get("attached_papers", [])
        if papers:
            total += _estimate_tokens("\n".join(papers))
    return total


def _conversation_path(project_name: str) -> Path:
    safe = re.sub(r"[^\w\s\-]", "", project_name)[:80]
    return _REPO_ROOT / safe / "conversation.json"


class ConversationManager:
    """按课题管理对话上下文，自动持久化。"""

    def __init__(self, project_name: str, topic_desc: str = "", *,
                 session_id: str | None = None, storage_path: Path | None = None):
        self._project_name = project_name
        self._topic_desc = topic_desc
        self.session_id = session_id
        self.lock = threading.RLock()
        self.request_lock = threading.RLock()
        self._path = Path(storage_path) if storage_path is not None else _conversation_path(project_name)
        data = self._load()
        self._meta: dict = data["_meta"]
        self._messages: list[dict] = data["messages"]
        self._compressed: list[dict] = data.get("compressed", [])

    # ── 加载 / 保存 ──

    def _load(self) -> dict:
        events = self._path.with_name("events.jsonl")
        if self.session_id and events.exists():
            data = self._empty_data()
            for line in events.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                kind = event["kind"]
                if kind == "snapshot":
                    data = event["data"]
                elif kind == "message":
                    data["messages"].append(event["message"])
                elif kind == "metadata":
                    data["_meta"].update(event["changes"])
                elif kind == "compression":
                    data.setdefault("compressed", []).append(event["summary"])
                    data["messages"] = data["messages"][event["count"]:]
                elif kind == "clear":
                    data["messages"], data["compressed"] = [], []
                else:
                    raise ValueError("会话日志包含未知事件；原文件已保留")
            data["_meta"]["total_rounds"] = sum(m["role"] == "user" for m in data["messages"])
            data["_meta"]["estimated_tokens"] = _estimate_messages_tokens(data["messages"])
            data["_meta"]["compressed_count"] = len(data.get("compressed", []))
            return data
        if self._path.is_file():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                if self.session_id:
                    raise ValueError("会话记录无法读取；原文件已保留")
        return self._empty_data()

    def _append_event(self, kind, **data):
        if not self.session_id:
            return
        path = self._path.with_name("events.jsonl")
        if not path.exists():
            raise ValueError("会话日志已移除；未重新创建课题目录")
        # A complete, flushed journal entry precedes the rebuildable JSON projection.
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(kind=kind, **data), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def prepare_project_context(self, project_name: str, topic_desc: str):
        """Freeze initial project context; changes are appended to the next user message."""
        current = dict(name=project_name, description=topic_desc)
        previous = self._meta.get("request_project_context")
        if previous is None:
            previous = current
            self._meta["request_project_context"] = current
            self._meta["initial_project_context"] = current
            self._append_event("metadata", changes={
                "request_project_context": current, "initial_project_context": current})
            self._save()
            return current, ""
        initial = self._meta.get("initial_project_context", previous)
        update = ""
        if previous != current:
            update = ("[课题资料更新：以下内容替代此前课题名称与描述]\n"
                      f"当前课题：{project_name}\n课题描述：{topic_desc}\n\n")
            # The marker is committed with the user message by commit_project_context.
        return initial, update

    def commit_project_context(self, project_name: str, topic_desc: str):
        current = dict(name=project_name, description=topic_desc)
        if self._meta.get("request_project_context") != current:
            self._meta["request_project_context"] = current
            self._append_event("metadata", changes={"request_project_context": current})
            self._save()

    def _empty_data(self) -> dict:
        now = datetime.now().isoformat()
        return {
            "_meta": {
                "project_name": self._project_name,
                "topic_desc": self._topic_desc,
                "created_at": now,
                "updated_at": now,
                "total_rounds": 0,
                "estimated_tokens": 0,
                "compressed_count": 0,
            },
            "messages": [],
            "compressed": [],
        }

    def _save(self):
        if self.session_id and not self._path.with_name("events.jsonl").exists():
            raise ValueError("会话目录已移除；未重新创建课题目录")
        self._meta["updated_at"] = datetime.now().isoformat()
        self._meta["total_rounds"] = sum(1 for m in self._messages if m["role"] == "user")
        self._meta["estimated_tokens"] = _estimate_messages_tokens(self._messages)
        self._meta["compressed_count"] = len(self._compressed)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        out = {
            "_meta": self._meta,
            "messages": self._messages,
            "compressed": self._compressed,
        }
        atomic_write_text(self._path, json.dumps(out, ensure_ascii=False, indent=2))

    # ── 读写消息 ──

    def set_run_state(self, state: dict):
        """Additive metadata; run IDs/timestamps never enter the model prefix."""
        with self.lock:
            self._append_event("metadata", changes={"last_run": state})
            self._meta["last_run"] = copy.deepcopy(state)
            self._save()

    def recover_interrupted_run(self):
        """Called on load only when this process has no live owner for this chat."""
        with self.lock:
            state = self._meta.get("last_run", {})
            if state.get("state") not in {"running", "stopping"}:
                return
            goal = state.get("goal", "")
            if goal and not any(m.get("role") == "user" and
                                m.get("timestamp", "") >= state.get("started_at", "") and
                                m.get("display_content", m.get("content")) == goal
                                for m in self._messages):
                self.add_user_message(goal)
            self.add_assistant_message("上次工作在应用退出时中断；已保存的记录与结果保留。"
                                       "请重新核对未完成步骤，可以发送新消息继续。")
            self.set_run_state({**state, "state": "interrupted"})

    def add_user_message(self, content: str,
                         attached_papers: list[str] | None = None,
                         paper_details: list[dict] | None = None,
                         display_content: str = "", *, library_context_hash: str = "") -> None:
        """添加用户消息。

        Args:
            content: 用户输入文本
            attached_papers: 附带论文的简要引用（如 doi / 标题）
            paper_details: 附带论文的详细摘要/全文，进入消息体
            display_content: UI 展示用文本，空则用 content
        """
        msg: dict = {
            "role": "user",
            "content": content,
            "display_content": display_content or content,  # UI 展示用，不含论文详情
            "timestamp": datetime.now().isoformat(),
        }
        if attached_papers:
            msg["attached_papers"] = attached_papers
        if library_context_hash:
            msg["library_context_hash"] = library_context_hash
        if paper_details:
            details_text = _format_paper_details(paper_details)
            msg["content"] = details_text + "\n\n—— 用户问题 ——\n" + content
        with self.lock:
            self._append_event("message", message=msg)
            self._messages.append(msg)
            self._save()

    def add_assistant_message(self, content: str, display_content: str = "") -> None:
        """添加助手回复。display_content 用于 UI 显示，content 保留完整版供 API 上下文。"""
        msg = {
            "role": "assistant",
            "content": content,
            "timestamp": datetime.now().isoformat(),
        }
        if display_content:
            msg["display_content"] = display_content
        with self.lock:
            self._append_event("message", message=msg)
            self._messages.append(msg)
            self._save()

    # ── 查询 ──

    @property
    def display_messages(self) -> list[dict]:
        """最近 _DISPLAY_ROUNDS 轮对话，用于 UI 初始渲染。

        返回副本，用户消息用 display_content 替代 content，
        避免 UI 显示注入的论文详情。
        """
        msgs = []
        for m in self._messages[-(_DISPLAY_ROUNDS * 2):]:
            copy = dict(m)
            if m.get("display_content"):
                copy["content"] = m["display_content"]
            msgs.append(copy)
        return msgs

    @property
    def has_more_history(self) -> bool:
        """是否有更早的对话可加载。"""
        return len(self._messages) > _DISPLAY_ROUNDS * 2

    def load_more_history(self, rounds: int = _DISPLAY_ROUNDS) -> list[dict]:
        """加载更早的对话（往前多取 rounds 轮）。"""
        current_visible = len(self.display_messages) if hasattr(self, '_visible_count') else _DISPLAY_ROUNDS * 2
        # NOT a property leak — just a dict
        if not hasattr(self, '_visible_count'):
            object.__setattr__(self, '_visible_count', _DISPLAY_ROUNDS * 2)
        self._visible_count += rounds * 2
        start = max(0, len(self._messages) - self._visible_count)
        return self._messages[start:-(self._visible_count - rounds * 2)] if start > 0 else []

    @property
    def compressed_summaries(self) -> list[dict]:
        """压缩历史摘要列表。"""
        return list(self._compressed)

    @property
    def total_rounds(self) -> int:
        return self._meta.get("total_rounds", 0)

    @property
    def estimated_tokens(self) -> int:
        return self._meta.get("estimated_tokens", 0)

    @property
    def is_empty(self) -> bool:
        return len(self._messages) == 0

    def has_library_context(self, fingerprint: str) -> bool:
        """Only the latest retained snapshot counts; compressed material is reinjected."""
        with self.lock:
            latest = next((m["library_context_hash"] for m in reversed(self._messages)
                           if m.get("library_context_hash")), None)
            return latest == fingerprint

    def needs_compression(self, threshold: int = _MAX_TOKENS, *, extra_tokens: int = 0) -> bool:
        """是否需要压缩。"""
        summaries = sum(_estimate_tokens(c.get("rounds_summary", "")) for c in self._compressed)
        return _estimate_messages_tokens(self._messages) + summaries + extra_tokens > threshold

    # ── API 消息构建 ──

    def build_api_messages(self, system_prompt: str,
                           paper_catalog: list[str] | None = None) -> list[dict]:
        """构建发给 API 的消息列表。

        结构：
        1. 固定 system 消息（含初始课题信息；paper_catalog 为旧调用兼容参数）
        2. 独立历史摘要消息（不把摘要和压缩时间改写到 system 中）
        3. 所有未压缩消息
        """
        # 1. System prompt
        sys_content = system_prompt
        if paper_catalog:
            sys_content += "\n\n## 文献库论文目录\n" + "\n".join(paper_catalog)

        messages = [{"role": "system", "content": sys_content}]

        # A compaction changes history, but must preserve the reusable system prefix.
        if self._compressed:
            summaries = []
            for c in self._compressed:
                summaries.append(
                    f"[历史摘要：涵盖 {c.get('original_rounds', '?')} 轮对话]\n"
                    f"{c.get('rounds_summary', '')}"
                )
            messages.append({"role": "user", "content": "此前对话摘要（作为历史背景）：\n" + "\n\n".join(summaries)})

        # 2. 未压缩的消息
        messages.extend({"role": m["role"], "content": m["content"]} for m in self._messages)

        return messages

    # ── 压缩 ──

    def get_compress_batch(self, batch_rounds: int = 10) -> list[dict] | None:
        """取出最早 batch_rounds 轮对话，准备压缩。返回 None 表示不足一轮。"""
        count = min(batch_rounds * 2, len(self._messages) // 3)
        # Do not leave a reply detached from its question after the cut.
        while count >= 2 and (self._messages[count - 1]["role"] != "assistant"
                              or self._messages[count]["role"] != "user"):
            count -= 1
        if count < 2:
            return None
        batch = self._messages[:count]
        return batch

    def apply_compression(self, summary: str, batch: list[dict]) -> None:
        """将一批对话替换为摘要。"""
        original_rounds = sum(1 for m in batch if m["role"] == "user")
        record = {
            "rounds_summary": summary,
            "original_rounds": original_rounds,
            "compressed_at": datetime.now().isoformat(),
        }
        self._append_event("compression", summary=record, count=len(batch))
        self._compressed.append(record)
        self._messages = self._messages[len(batch):]
        self._save()

    # ── 更新课题信息 ──

    def update_topic_desc(self, topic_desc: str) -> None:
        self._topic_desc = topic_desc
        self._meta["topic_desc"] = topic_desc
        self._append_event("metadata", changes={"topic_desc": topic_desc})
        self._save()

    def update_paper_catalog(self, paper_list: list[str]) -> None:
        """刷新论文目录（不立即保存，调用 add_* 时一起存）。"""
        self._meta["paper_catalog"] = paper_list

    # ── 清理 ──

    def clear(self) -> None:
        """清空对话历史。"""
        self._append_event("clear")
        self._messages.clear()
        self._compressed.clear()
        self._save()

    def delete_file(self) -> None:
        """删除持久化文件。"""
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass


# ── 工具 ──

def _format_paper_details(papers: list[dict]) -> str:
    """将论文列表格式化为 AI 可读文本。"""
    lines = ["以下是用户选中的论文详情："]
    for i, p in enumerate(papers, 1):
        title = (p.get("title") or "无标题")[:150]
        authors = (p.get("authors") or "未知")[:100]
        year = p.get("year", "")
        abstract = (p.get("abstract") or "")[:800]
        lines.append(
            f"\n[{i}] {title}\n"
            f"    作者: {authors}\n"
            f"    年份: {year}\n"
            f"    摘要: {abstract}"
        )
    return "\n".join(lines)


def load_conversation(project_name: str, topic_desc: str = "") -> ConversationManager:
    """快捷：加载课题对话管理器。"""
    return ConversationManager(project_name, topic_desc)
