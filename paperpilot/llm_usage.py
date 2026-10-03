"""Request-level token accounting. Stores counters and hashes, never prompts or keys."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading

from paperpilot.repo_manager import _get_app_dir

logger = logging.getLogger(__name__)
_USAGE_PATH = Path(os.environ.get("PAPERPILOT_VALIDATION_ROOT", str(_get_app_dir()))) / "outputs" / "llm_usage.sqlite3"
_scope = ContextVar("paperpilot_llm_scope", default={})
_lock = threading.RLock()


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None


def _value(obj, name):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _count(obj, name):
    value = _value(obj, name)
    return value if type(value) is int and value >= 0 else None


def normalize_usage(raw, provider: str) -> TokenUsage | None:
    if raw is None:
        return None
    if provider == "codex":
        input_tokens = _count(raw, "inputTokens")
        output = _count(raw, "outputTokens")
        hit = _count(raw, "cachedInputTokens")
        miss = input_tokens - hit if hit is not None and input_tokens is not None and hit <= input_tokens else None
        write = None
        reasoning = _count(raw, "reasoningOutputTokens")
    elif provider == "anthropic":
        uncached = _count(raw, "input_tokens")
        hit = _count(raw, "cache_read_input_tokens")
        write = _count(raw, "cache_creation_input_tokens")
        # Anthropic input_tokens excludes reads AND writes; both are input.
        input_tokens = (uncached + (hit or 0) + (write or 0)
                        if uncached is not None else None)
        miss = uncached + (write or 0) if hit is not None and uncached is not None else None
        output = _count(raw, "output_tokens")
        reasoning = None  # Thinking is already included in output_tokens.
    elif provider == "openai" and _count(raw, "input_tokens") is not None:
        # Responses uses different field names from Chat Completions.
        input_tokens = _count(raw, "input_tokens")
        output = _count(raw, "output_tokens")
        hit = _count(_value(raw, "input_tokens_details"), "cached_tokens")
        miss = input_tokens - hit if hit is not None and hit <= input_tokens else None
        write = None
        reasoning = _count(_value(raw, "output_tokens_details"), "reasoning_tokens")
    else:
        input_tokens = _count(raw, "prompt_tokens")
        output = _count(raw, "completion_tokens")
        hit = _count(raw, "prompt_cache_hit_tokens")
        miss = _count(raw, "prompt_cache_miss_tokens")
        if hit is None:
            hit = _count(_value(raw, "prompt_tokens_details"), "cached_tokens")
        if input_tokens is None and hit is not None and miss is not None:
            input_tokens = hit + miss
        if miss is None and hit is not None and input_tokens is not None and hit <= input_tokens:
            miss = input_tokens - hit
        write = None
        reasoning = _count(_value(raw, "completion_tokens_details"), "reasoning_tokens")
    total = _count(raw, "totalTokens" if provider == "codex" else "total_tokens")
    if total is None and input_tokens is not None and output is not None:
        total = input_tokens + output
    result = TokenUsage(input_tokens, output, hit, miss, write, reasoning, total)
    return result if any(v is not None for v in asdict(result).values()) else None


@contextmanager
def usage_scope(**values):
    """Scopes are copied; background threads must explicitly enter their own scope."""
    token = _scope.set({**_scope.get(), **values})
    try:
        yield
    finally:
        _scope.reset(token)


def usage_context():
    """Read-only ownership snapshot for transports continuing a paused tool turn."""
    return dict(_scope.get())


def usage_task(task):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with usage_scope(task=task):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def message_fingerprints(messages):
    # Same semantic fields as the current adapters; UI metadata stays local.
    fields = ("role", "content", "name", "tool_calls", "tool_call_id", "reasoning_content", "prefix")
    return [hashlib.sha256(json.dumps(
        {k: m[k] for k in fields if k in m}, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest() for m in messages]


class UsageStore:
    _COUNTERS = """COUNT(*) AS requests,
        COALESCE(SUM(status='error'),0) AS failed,
        COALESCE(SUM(status='cancelled'),0) AS cancelled, COUNT(input_tokens) AS reported,
        COUNT(output_tokens) AS output_reported,
        COALESCE(SUM(cache_hit_tokens IS NOT NULL AND cache_miss_tokens IS NOT NULL),0) AS cache_reported,
        COALESCE(SUM(input_tokens),0) AS input_tokens,
        COALESCE(SUM(output_tokens),0) AS output_tokens,
        COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,
        COALESCE(SUM(CASE WHEN cache_hit_tokens IS NOT NULL AND cache_miss_tokens IS NOT NULL
            THEN cache_hit_tokens ELSE 0 END),0) AS cache_hit_tokens,
        COALESCE(SUM(cache_hit_tokens+cache_miss_tokens),0) AS cache_input_tokens"""
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else _USAGE_PATH

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL,
            project_id INTEGER, session_id TEXT, task TEXT NOT NULL,
            provider TEXT NOT NULL, model TEXT NOT NULL, request_id TEXT,
            status TEXT NOT NULL, elapsed_ms INTEGER, first_token_ms INTEGER,
            input_tokens INTEGER, output_tokens INTEGER, cache_hit_tokens INTEGER,
            cache_miss_tokens INTEGER, cache_write_tokens INTEGER,
            reasoning_tokens INTEGER, total_tokens INTEGER,
            prefix_state TEXT, common_messages INTEGER, fingerprints TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS request_scope ON requests(project_id, session_id, task)")
        # Additive migration: retain earlier counters and mark unknown metadata as NULL.
        with _lock:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(requests)")}
            for name in ("operation", "thinking_mode"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE requests ADD COLUMN {name} TEXT")
        return conn

    def record(self, result, messages, *, task="other", status="ok", provider="", model="", thinking=None):
        scope = _scope.get()
        values = dict(timestamp=datetime.now(timezone.utc).isoformat(),
                      project_id=scope.get("project_id"), session_id=scope.get("session_id"),
                      task=scope.get("task", task), provider=provider, model=model,
                      request_id=getattr(result, "request_id", None), status=status,
                      elapsed_ms=getattr(result, "elapsed_ms", None),
                      first_token_ms=getattr(result, "first_token_ms", None),
                      operation=scope.get("operation"),
                      thinking_mode="enabled" if thinking is True else "disabled" if thinking is False else "default")
        usage = getattr(result, "usage", None)
        values.update(asdict(usage) if isinstance(usage, TokenUsage) else asdict(TokenUsage()))
        hashes = message_fingerprints(messages)
        with _lock:
            conn = self._connect()
            try:
                previous = conn.execute("""SELECT fingerprints FROM requests
                    WHERE project_id IS ? AND session_id IS ? AND task=?
                    AND provider=? AND model=? AND status='ok' ORDER BY id DESC LIMIT 1""",
                    (values["project_id"], values["session_id"], values["task"], provider, model)).fetchone()
                old = json.loads(previous[0]) if previous else []
                common = 0
                for before, after in zip(old, hashes):
                    if before != after:
                        break
                    common += 1
                state = "cold_start"
                if old:
                    if common == len(old) and len(hashes) >= len(old):
                        state = "stable_append"
                    elif old[0] != (hashes[0] if hashes else None):
                        state = "system_changed"
                    elif common == len(hashes):
                        state = "history_shortened"
                    else:
                        state = "history_changed"
                values.update(prefix_state=state, common_messages=common,
                              fingerprints=json.dumps(hashes))
                keys = list(values)
                conn.execute(f"INSERT INTO requests ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                             [values[k] for k in keys])
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _where(project_id=None, session_id=None, task=None):
        terms, args = [], []
        for key, value in (("project_id", project_id), ("session_id", session_id), ("task", task)):
            if value is not None:
                terms.append(f"{key}=?")
                args.append(value)
        return (" WHERE " + " AND ".join(terms) if terms else ""), args

    def summary(self, project_id=None, session_id=None, task=None):
        where, args = self._where(project_id, session_id, task)
        if not self.path.exists():
            return self._summarize([])
        with _lock:
            conn = self._connect()
            try:
                result = dict(conn.execute("SELECT " + self._COUNTERS + " FROM requests" + where, args).fetchone())
                result["cache_ratio"] = (result["cache_hit_tokens"] / result["cache_input_tokens"]
                                          if result["cache_input_tokens"] else None)
                last = conn.execute("SELECT * FROM requests" + where + " ORDER BY id DESC LIMIT 1", args).fetchone()
                result["last"] = {k: last[k] for k in last.keys() if k != "fingerprints"} if last else None
                return result
            finally:
                conn.close()

    def groups(self, project_id=None, session_id=None):
        if not self.path.exists():
            return []
        where, args = self._where(project_id, session_id)
        conn = self._connect()
        try:
            rows = conn.execute("SELECT task,provider,model," + self._COUNTERS +
                                " FROM requests" + where + " GROUP BY task,provider,model ORDER BY task,provider,model", args)
            result = []
            for row in rows:
                group = dict(row)
                group["cache_ratio"] = (group["cache_hit_tokens"] / group["cache_input_tokens"]
                                         if group["cache_input_tokens"] else None)
                result.append(group)
            return result
        finally:
            conn.close()

    @staticmethod
    def _summarize(rows):
        def total(key):
            return sum(r[key] or 0 for r in rows)
        eligible = [r for r in rows if r["cache_hit_tokens"] is not None and r["cache_miss_tokens"] is not None]
        cache_input = sum(r["cache_hit_tokens"] + r["cache_miss_tokens"] for r in eligible)
        hits = sum(r["cache_hit_tokens"] for r in eligible)
        return dict(requests=len(rows), failed=sum(r["status"] == "error" for r in rows),
                    cancelled=sum(r["status"] == "cancelled" for r in rows),
                    reported=sum(r["input_tokens"] is not None for r in rows),
                    output_reported=sum(r["output_tokens"] is not None for r in rows),
                    cache_reported=len(eligible), input_tokens=total("input_tokens"),
                    output_tokens=total("output_tokens"), reasoning_tokens=total("reasoning_tokens"),
                    cache_hit_tokens=hits, cache_input_tokens=cache_input,
                    cache_ratio=hits / cache_input if cache_input else None,
                    last={k: rows[-1][k] for k in rows[-1].keys() if k != "fingerprints"} if rows else None)

    def records(self, project_id=None, session_id=None, task=None, limit=50):
        if not self.path.exists():
            return []
        where, args = self._where(project_id, session_id, task)
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM requests" + where + " ORDER BY id DESC LIMIT ?",
                                [*args, max(1, min(1000, int(limit)))]).fetchall()
            return [{k: r[k] for k in r.keys() if k != "fingerprints"} for r in rows]
        finally:
            conn.close()


def record_request(result, messages, *, task="other", status="ok", provider="", model="", thinking=None):
    """Telemetry failures never turn a successful model response into a failed chat."""
    try:
        from paperpilot.config import load_config
        if (load_config().get("agent", {}) or {}).get("usage_enabled", True):
            UsageStore().record(result, messages, task=task, status=status, provider=provider, model=model,
                                thinking=thinking)
    except Exception:
        logger.warning("LLM usage could not be saved", exc_info=True)
