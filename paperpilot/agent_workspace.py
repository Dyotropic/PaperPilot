"""Bounded UTF-8 workspace tools with observed-version edits and exact approvals.

No shell, deletion, outside-root escalation, symlinks, junctions or secret files.
The three permission modes share these boundaries.
"""
from dataclasses import dataclass
import difflib
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import threading
from paperpilot.file_paths import io_path


MODES = {"read_only": "只读", "ask_edit": "修改前询问", "direct_edit": "可直接修改"}
MAX_FILE_BYTES = 1024 * 1024
MAX_READ_CHARS = 20000
_PROTECTED = {".git", ".ssh", ".codex", ".agents", ".aws", ".azure", ".env", "auth.json", "credentials",
              "credentials.json", "config.yaml", "conversation.json",
              "events.jsonl", "teams", "sessions", "attachments", "node_modules", ".venv"}
_TEXT = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".tex", ".bib", ".py", ".r",
         ".m", ".toml", ".xml", ".html", ".css", ".js", ".ts", ".log", ".rst", ".tsv"}
_WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)}
_locks = {}
_locks_guard = threading.Lock()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def linked(path):
    try:
        info = io_path(path).lstat()
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


@dataclass(frozen=True)
class EditProposal:
    path: str
    before_hash: str | None
    after_hash: str
    diff: str
    content: str
    new: bool


class Workspace:
    def __init__(self, root, mode, observed=None):
        if not isinstance(mode, str) or mode not in MODES:
            raise ValueError("未知权限模式")
        # Validate the original path before resolve(), which would hide links.
        raw = Path(root).absolute()
        for part in (raw, *raw.parents):
            if linked(part):
                raise ValueError("工作区不能经过链接或 junction")
        if not io_path(raw).is_dir():
            raise ValueError("工作区必须是已存在的目录")
        if raw == Path(raw.anchor) or raw.name.casefold() in _PROTECTED:
            raise ValueError("请选择具体课题工作区，不能使用磁盘根目录或受保护目录")
        self.root, self.mode = raw.resolve(), mode
        self.observed = dict(observed or {})

    def resolve(self, name):
        if not isinstance(name, str) or not name.strip() or len(name) > 1024:
            raise ValueError("需要有效的工作区相对路径")
        # Forbid Windows drive/ADS, UNC and traversal on all supported hosts.
        if any(char in name for char in (":", "\x00")) or name.startswith(("/", "\\")):
            raise ValueError("仅允许工作区相对路径")
        parts = name.replace("\\", "/").split("/")
        if any(p in {"", ".", ".."} or p.rstrip(" .") != p or
               p.split(".")[0].upper() in _WINDOWS_DEVICES or
               p.casefold() in _PROTECTED or p.casefold().startswith(".env") or
               p.casefold().startswith(("id_rsa", "id_ed25519")) or
               Path(p).suffix.casefold() in {".pem", ".key", ".p12", ".db", ".sqlite3"}
               for p in parts):
            raise ValueError("路径包含越界或受保护内容")
        candidate = self.root.joinpath(*parts)
        # Recheck the entire root chain in case it changed while awaiting approval.
        for part in (candidate, *candidate.parents):
            if linked(part):
                raise ValueError("拒绝读取或修改链接/junction")
        if not candidate.resolve().is_relative_to(self.root):
            raise ValueError("路径不在工作区内")
        candidate = io_path(candidate)
        if candidate.exists() and candidate.is_file() and candidate.stat().st_nlink > 1:
            raise ValueError("拒绝读取或修改硬链接")
        return candidate

    def _data(self, path):
        if path.suffix.casefold() not in _TEXT:
            raise ValueError("仅支持 UTF-8 文本文件；PDF/Office/图片请使用会话附件")
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("文件缺失、非普通文件或超过 1 MiB")
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES or b"\x00" in data:
            raise ValueError("文件超限或为二进制内容")
        data.decode("utf-8")
        return data

    def read(self, path, start=1, lines=150):
        if type(start) is not int or start < 1 or type(lines) is not int or not 1 <= lines <= 300:
            raise ValueError("start 从 1 开始，lines 为 1–300")
        target = self.resolve(path)
        data = self._data(target)
        text = data.decode("utf-8")
        version = digest(data)
        self.observed[path] = version
        chunks = text.splitlines(keepends=True)
        selected, chars = [], 0
        for n, line in enumerate(chunks[start - 1:start - 1 + lines], start):
            if chars + len(line) > MAX_READ_CHARS:
                break
            selected.append(f"{n}: {line.rstrip()}")
            chars += len(line)
        return dict(path=path, sha256=version, total_lines=len(chunks), start=start,
                    returned_lines=len(selected), content="\n".join(selected),
                    truncated=start - 1 + len(selected) < len(chunks))

    def list(self, directory="", pattern=None):
        self.resolve("paperpilot-boundary-check.txt")
        root = self.resolve(directory) if directory else io_path(self.root)
        if not root.is_dir():
            raise ValueError("目录不存在")
        rows, scanned, truncated = [], 0, False
        for base, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d.casefold() not in _PROTECTED
                             and not linked(Path(base) / d))
            for name in sorted(files):
                scanned += 1
                if scanned > 2000 or len(rows) >= 200:
                    truncated = True
                    break
                relative = (Path(base) / name).relative_to(io_path(self.root)).as_posix()
                try:
                    path = self.resolve(relative)
                    if path.suffix.casefold() not in _TEXT:
                        continue
                    if pattern is None:
                        rows.append(dict(path=relative, bytes=path.stat().st_size))
                    else:
                        data = self._data(path).decode("utf-8")
                        for n, line in enumerate(data.splitlines(), 1):
                            if pattern.casefold() in line.casefold():
                                rows.append(dict(path=relative, line=n, text=line[:300]))
                                if len(rows) >= 200:
                                    break
                except (ValueError, OSError, UnicodeError):
                    continue
            if truncated:
                break
        return dict(results=rows, truncated=truncated)

    def propose(self, path, *, content=None, old=None, new=None):
        if self.mode == "read_only":
            raise PermissionError("只读模式拒绝文件修改")
        target = self.resolve(path)
        if target.suffix.casefold() not in _TEXT:
            raise ValueError("仅允许创建或修改 UTF-8 文本文件")
        exists = target.exists()
        before_data = self._data(target) if exists else b""
        before_hash = digest(before_data) if exists else None
        if exists and self.observed.get(path) != before_hash:
            raise ValueError("修改前必须先 read_file，且读取后文件不得变化；请重新读取")
        before = before_data.decode("utf-8")
        if content is not None:
            if not isinstance(content, str):
                raise ValueError("content 必须是文本")
            after = content
        else:
            if not exists or not isinstance(old, str) or not old or not isinstance(new, str):
                raise ValueError("edit_file 需要已有文件及非空 old_text 和 new_text")
            if before.count(old) != 1:
                raise ValueError("old_text 必须精确且唯一匹配；请增加上下文")
            after = before.replace(old, new, 1)
        data = after.encode("utf-8")
        if len(data) > MAX_FILE_BYTES or "\x00" in after or after == before:
            raise ValueError("内容超限、为二进制或没有实际变化")
        if not target.parent.is_dir():
            raise ValueError("父目录必须已存在；请使用现有工作区目录")
        diff = "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                          fromfile=path, tofile=path, lineterm=""))
        if not diff:
            diff = f"文本行相同，但换行/编码字节变化。修改前 SHA256：{before_hash}；修改后：{digest(data)}"
        return EditProposal(path, before_hash, digest(data), diff, after, not exists)

    def apply(self, proposal):
        if self.mode == "read_only":
            raise PermissionError("只读模式拒绝文件修改")
        with _locks_guard:
            lock = _locks.setdefault(str(self.root / proposal.path).casefold(), threading.RLock())
        with lock:
            target = self.resolve(proposal.path)
            before = digest(self._data(target)) if target.exists() else None
            if before != proposal.before_hash:
                raise ValueError("预览之后文件发生变化，批准已失效；请重新读取并提出修改")
            if digest(proposal.content.encode("utf-8")) != proposal.after_hash:
                raise ValueError("修改内容与预览不一致")
            fd, temp = tempfile.mkstemp(prefix=".paperpilot-edit-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(proposal.content.encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                # Repeat policy/version checks at the commit boundary.
                self.resolve(proposal.path)
                current = digest(self._data(target)) if target.exists() else None
                if current != before:
                    raise ValueError("文件在提交前变化，未覆盖新内容")
                if proposal.new:
                    # Atomic create-without-overwrite; a concurrent creator wins safely.
                    os.link(temp, target)
                else:
                    os.replace(temp, target)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
            self.observed[proposal.path] = proposal.after_hash
            return dict(path=proposal.path, sha256=proposal.after_hash, created=proposal.new)
