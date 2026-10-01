"""Independent chats inside a project; legacy history is copied, never overwritten."""

import copy
from datetime import datetime
import json
import re
import threading
import uuid

from paperpilot import conversation
from paperpilot.repo_manager import atomic_write_text

_lock = threading.RLock()


class SessionStore:
    def __init__(self, project_name: str, project_id: int = 0, topic_desc: str = ""):
        self.project_name = project_name
        self.project_id = project_id
        self.topic_desc = topic_desc
        self.legacy_path = conversation._conversation_path(project_name)
        self.directory = self.legacy_path.parent / "sessions"
        self.index_path = self.directory / "index.json"
        if not self.index_path.exists():
            matches = []
            for candidate in conversation._REPO_ROOT.glob("*/sessions/index.json"):
                if candidate.parent.parent.name.startswith("."):
                    continue
                try:
                    index = json.loads(candidate.read_text(encoding="utf-8"))
                    if isinstance(index, dict) and index.get("project_id") == project_id:
                        matches.append(candidate)
                except (OSError, ValueError):
                    continue
            if len(matches) > 1:
                raise ValueError("同一课题存在多个会话目录；原记录已保留，请检查目录")
            if matches:
                # A failed directory rename must not hide history after restarting the app.
                self.index_path = matches[0]
                self.directory = self.index_path.parent
                self.legacy_path = self.directory.parent / "conversation.json"
        self._index_created = self.index_path.exists()

    def _write(self, index):
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.index_path, json.dumps(index, ensure_ascii=False, indent=2))
        self._index_created = True

    def _index(self):
        if self.index_path.exists():
            index = json.loads(self.index_path.read_text(encoding="utf-8"))
            if not isinstance(index, dict) or index.get("version") != 1 or not isinstance(index.get("sessions"), list):
                raise ValueError("会话索引格式异常；原记录已保留，请勿覆盖")
            if index.get("project_id") != self.project_id:
                raise ValueError("课题目录与会话索引不匹配；请检查同名课题")
            entries = index["sessions"]
            if (not entries or any(not isinstance(s, dict) or not isinstance(s.get("id"), str)
                                   or not isinstance(s.get("title"), str) or not isinstance(s.get("updated_at"), str)
                                   for s in entries)
                    or len({s["id"] for s in entries}) != len(entries)
                    or index.get("active_session_id") not in {s["id"] for s in entries}):
                raise ValueError("会话索引条目异常；原记录已保留，请勿覆盖")
            return index
        if self._index_created:
            raise ValueError("课题会话目录已移除或会话索引缺失；未重新创建记录")
        index = dict(version=1, project_id=self.project_id, active_session_id=None, sessions=[])
        # A fixed migration directory makes recovery idempotent after an interrupted index write.
        session_id = "legacy" if self.legacy_path.exists() else uuid.uuid4().hex
        data = None
        if self.legacy_path.exists():
            original = self.legacy_path.read_text(encoding="utf-8")
            data = json.loads(original)
            if not isinstance(data, dict) or not isinstance(data.get("messages"), list) or not isinstance(data.get("_meta"), dict):
                raise ValueError("旧对话格式异常；已保留原文件，未进行迁移")
            self.directory.mkdir(parents=True, exist_ok=True)
            backup = self.directory / "legacy-conversation.json"
            if not backup.exists():
                atomic_write_text(backup, original)
        self._create(index, session_id, "历史对话" if data is not None else "新对话", data)
        self._write(index)
        return index

    def _path(self, session_id):
        if session_id != "legacy" and not re.fullmatch(r"[0-9a-f]{32}", session_id or ""):
            raise ValueError("无效的会话标识")
        return self.directory / session_id / "conversation.json"

    def _create(self, index, session_id, title, data=None):
        path = self._path(session_id)
        now = datetime.now().isoformat()
        if data is None:
            data = dict(_meta=dict(project_name=self.project_name, topic_desc=self.topic_desc,
                                   created_at=now, updated_at=now, total_rounds=0,
                                   estimated_tokens=0, compressed_count=0), messages=[], compressed=[])
        data = copy.deepcopy(data)
        data["_meta"]["session_id"] = session_id
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
        events = path.with_name("events.jsonl")
        if not events.exists():
            atomic_write_text(events, json.dumps(dict(kind="snapshot", data=data), ensure_ascii=False) + "\n")
        index["sessions"].append(dict(id=session_id, title=title[:80],
                                      created_at=now, updated_at=now, auto_title=title == "新对话"))
        index["active_session_id"] = session_id

    def list_sessions(self):
        with _lock:
            index = self._index()
            return sorted(copy.deepcopy(index["sessions"]), key=lambda s: s["updated_at"], reverse=True)

    @property
    def active_session_id(self):
        with _lock:
            return self._index()["active_session_id"]

    def create_session(self, title="新对话"):
        with _lock:
            index = self._index()
            session_id = uuid.uuid4().hex
            self._create(index, session_id, title.strip() or "新对话")
            self._write(index)
            return session_id

    def _entry(self, index, session_id):
        self._path(session_id)
        return next((s for s in index["sessions"] if s["id"] == session_id), None)

    def select_session(self, session_id):
        with _lock:
            index = self._index()
            if self._entry(index, session_id) is None:
                raise ValueError("会话不存在")
            index["active_session_id"] = session_id
            self._write(index)

    def touch(self, session_id, first_message=None):
        with _lock:
            index = self._index()
            entry = self._entry(index, session_id)
            if entry is None:
                raise ValueError("会话不存在")
            entry["updated_at"] = datetime.now().isoformat()
            if first_message and entry.get("auto_title"):
                entry["title"] = re.sub(r"\s+", " ", first_message).strip()[:40] or "新对话"
                entry["auto_title"] = False
            self._write(index)

    def rename_session(self, session_id, title):
        title = title.strip()
        if not title:
            raise ValueError("会话名称不能为空")
        with _lock:
            index = self._index()
            entry = self._entry(index, session_id)
            if entry is None:
                raise ValueError("会话不存在")
            entry.update(title=title[:80], auto_title=False)
            self._write(index)

    def open_session(self, session_id=None):
        with _lock:
            index = self._index()
            session_id = session_id or index["active_session_id"]
            if self._entry(index, session_id) is None:
                raise ValueError("会话不存在")
            path = self._path(session_id)
            if not path.exists() and not path.with_name("events.jsonl").exists():
                raise ValueError("会话文件缺失；未创建空记录，请检查原目录")
            return conversation.ConversationManager(
                self.project_name, self.topic_desc, session_id=session_id,
                storage_path=path,
            )
