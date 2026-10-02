"""对话上下文管理 — 持久化、压缩、回滚。

存储：旧记录 repository/{课题名}/conversation.json；新会话 sessions/{session_id}/
包含原始 events.jsonl 日志与可重建的 conversation.json。
与 catalog.json 并列，课题删除时随 repo_manager 回收站一起移除。

结构：
{
  "_meta": { project_name, created_at, updated_at, total_rounds, estimated_tokens, compressed_count },
  "messages": [ {role, content, attached_papers?, timestamp}, ... ],
  "history": [ 全部已保存的原始消息，压缩不删除 ],
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

# Compatibility fallback for models whose context capacity has not been configured.
# Model-scoped capacity and pressure policy live in context_budget.py.
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
        self._history: list[dict] = data.get("history", copy.deepcopy(self._messages))
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
                    data.setdefault("history", copy.deepcopy(data["messages"]))
                    data["_meta"].setdefault("history_complete", not bool(data.get("compressed")))
                elif kind == "message":
                    data["messages"].append(event["message"])
                    data["history"].append(event["message"])
                elif kind == "metadata":
                    data["_meta"].update(event["changes"])
                elif kind == "compression":
                    event["summary"].setdefault("history_boundary",
                        len(data["history"]) - len(data["messages"]) + event["count"])
                    data.setdefault("compressed", []).append(event["summary"])
                    data["messages"] = data["messages"][event["count"]:]
                elif kind == "clear":
                    data["messages"], data["compressed"], data["history"] = [], [], []
                    data["_meta"].pop("context_sample", None)
                    data["_meta"]["history_complete"] = True
                else:
                    raise ValueError("会话日志包含未知事件；原文件已保留")
            data["_meta"]["total_rounds"] = sum(m["role"] == "user" for m in data["messages"])
            data["_meta"]["estimated_tokens"] = _estimate_messages_tokens(data["messages"])
            data["_meta"]["compressed_count"] = len(data.get("compressed", []))
            return data
        if self._path.is_file():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                data.setdefault("history", copy.deepcopy(data["messages"]))
                data["_meta"].setdefault("history_complete", not bool(data.get("compressed")))
                return data
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
                "history_complete": True,
            },
            "messages": [],
            "history": [],
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
            "history": self._history,
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

    def set_maintenance_state(self, state: dict):
        """Compaction is maintenance; it must not replace an interrupted user goal."""
        with self.lock:
            self._append_event("metadata", changes={"maintenance": state})
            self._meta["maintenance"] = copy.deepcopy(state)
            self._save()

    def set_context_sample(self, sample: dict):
        with self.lock:
            self._append_event("metadata", changes={"context_sample": sample})
            self._meta["context_sample"] = copy.deepcopy(sample)
            self._save()

    def recover_interrupted_run(self):
        """Called on load only when this process has no live owner for this chat."""
        with self.lock:
            maintenance = self._meta.get("maintenance", {})
            if maintenance.get("state") in {"running", "stopping"}:
                self.set_maintenance_state({**maintenance, "state": "interrupted"})
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
            self._history.append(msg)
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
            self._history.append(msg)
            self._save()

    # ── 查询 ──

    @property
    def display_messages(self) -> list[dict]:
        """最近 _DISPLAY_ROUNDS 轮对话，用于 UI 初始渲染。

        返回副本，用户消息用 display_content 替代 content，
        避免 UI 显示注入的论文详情。
        """
        msgs = []
        for m in self._history[-(_DISPLAY_ROUNDS * 2):]:
            copy = dict(m)
            if m.get("display_content"):
                copy["content"] = m["display_content"]
            msgs.append(copy)
        return msgs

    @property
    def has_more_history(self) -> bool:
        """是否有更早的对话可加载。"""
        return len(self._history) > _DISPLAY_ROUNDS * 2

    def load_more_history(self, rounds: int = _DISPLAY_ROUNDS) -> list[dict]:
        """加载更早的对话（往前多取 rounds 轮）。"""
        previous = getattr(self, "_visible_count", _DISPLAY_ROUNDS * 2)
        self._visible_count = previous + max(0, rounds) * 2
        end = max(0, len(self._history) - previous)
        start = max(0, len(self._history) - self._visible_count)
        return copy.deepcopy(self._history[start:end])

    @property
    def compressed_summaries(self) -> list[dict]:
        """压缩历史摘要列表。"""
        return copy.deepcopy(self._compressed)

    def _active_summaries(self):
        # Legacy checkpoints accumulated; a consolidated checkpoint supersedes
        # all its predecessors, while keeping their records available in the UI.
        start = next((i for i in range(len(self._compressed) - 1, -1, -1)
                      if self._compressed[i].get("consolidated")), 0)
        return self._compressed[start:]

    def display_timeline(self):
        with self.lock:
            start = max(0, len(self._history) - _DISPLAY_ROUNDS * 2)
            markers = {}
            for record in self._compressed:
                boundary = max(start, record.get("history_boundary", start))
                markers.setdefault(boundary, []).append(copy.deepcopy(record))
            timeline = []
            for i in range(start, len(self._history) + 1):
                timeline.extend(dict(kind="compression", record=r) for r in markers.get(i, []))
                if i < len(self._history):
                    m = dict(self._history[i])
                    m["content"] = m.get("display_content") or m["content"]
                    timeline.append(dict(kind="message", message=m))
            return timeline

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

    def needs_compression(self, threshold: int = _MAX_TOKENS, *, extra_tokens: int = 0,
                          current_tokens: int | None = None) -> bool:
        """是否需要压缩；调用方可提供与仪表一致的已校准占用。

        不提供 current_tokens 时保持历史字符估算接口的语义。
        """
        if current_tokens is not None:
            return current_tokens + extra_tokens > threshold
        summaries = sum(_estimate_tokens(c.get("rounds_summary", "")) for c in self._active_summaries())
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
            for c in self._active_summaries():
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
        if self._messages[:len(batch)] != batch:
            raise ValueError("待压缩上下文已变化，原记录保留；请重试")
        original_rounds = sum(1 for m in batch if m["role"] == "user")
        record = {
            "rounds_summary": summary,
            "original_rounds": original_rounds,
            "compressed_at": datetime.now().isoformat(),
            "history_boundary": len(self._history) - len(self._messages) + len(batch),
        }
        self._append_event("compression", summary=record, count=len(batch))
        self._compressed.append(record)
        self._messages = self._messages[len(batch):]
        self._save()

    def compaction_plan(self, system_prompt, keep_rounds=2, *, manual=False):
        """Snapshot a complete prefix, keeping recent question/answer turns verbatim."""
        with self.lock:
            boundaries = [i + 1 for i, m in enumerate(self._messages)
                          if m["role"] == "assistant"
                          and (i + 1 == len(self._messages) or self._messages[i + 1]["role"] == "user")]
            if not boundaries:
                return None
            if not manual and len(boundaries) <= keep_rounds:
                return None
            index = max(0, len(boundaries) - keep_rounds - 1)
            count = boundaries[index]
            # For short chats manual compaction can include the completed history.
            # An unanswered trailing user message always remains verbatim.
            if manual and len(boundaries) <= keep_rounds:
                count = boundaries[-1]
            batch = copy.deepcopy(self._messages[:count])
            api = self.build_api_messages(system_prompt)
            prefix_count = len(api) - len(self._messages)
            return dict(batch=batch, summaries=copy.deepcopy(self._active_summaries()),
                        api_messages=api[:prefix_count + count], system_prompt=system_prompt)

    def commit_compaction(self, summary, plan, *, mode, provider, model):
        from paperpilot.context_budget import estimate_request_tokens
        from paperpilot.agent_runtime import checkpoint
        with self.lock:
            checkpoint()
            batch = plan["batch"]
            if self._messages[:len(batch)] != batch or self._active_summaries() != plan["summaries"]:
                raise ValueError("待压缩上下文已变化，原记录保留；请重试")
            before = estimate_request_tokens(self.build_api_messages(plan["system_prompt"]))
            prior_rounds = sum(c.get("original_rounds", 0) for c in plan["summaries"])
            rounds = prior_rounds + sum(m["role"] == "user" for m in batch)
            record = dict(rounds_summary=summary, original_rounds=rounds,
                          compressed_at=datetime.now().isoformat(), consolidated=True,
                          mode=mode, provider=provider, model=model,
                          history_boundary=len(self._history),
                          messages_compacted=len(batch), before_tokens=before)
            after_messages = [dict(role="system", content=plan["system_prompt"]),
                dict(role="user", content=f"此前对话摘要（作为历史背景）：\n[历史摘要：涵盖 {rounds} 轮对话]\n{summary}")]
            after_messages.extend(dict(role=m["role"], content=m["content"]) for m in self._messages[len(batch):])
            after = estimate_request_tokens(after_messages)
            if after >= before:
                return None
            record["after_tokens"] = after
            # One flushed event is the replacement checkpoint. Original message
            # events and the history projection remain intact and recoverable.
            self._append_event("compression", summary=record, count=len(batch))
            self._compressed.append(record)
            self._messages = self._messages[len(batch):]
            self._save()
            return copy.deepcopy(record)

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
        self._history.clear()
        self._compressed.clear()
        self._meta.pop("context_sample", None)
        self._meta["history_complete"] = True
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
