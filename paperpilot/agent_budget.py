"""Shared usage accounting; optional finite limits for explicit test probes."""
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time

from paperpilot.context_budget import estimate_request_tokens


class BudgetPaused(BaseException):
    """Control flow, deliberately outside provider retry/error handling."""


_current = ContextVar("paperpilot_request_budget", default=None)


@contextmanager
def budget_scope(budget):
    token = _current.set(budget)
    try:
        yield
    finally:
        _current.reset(token)


def current_budget():
    return _current.get()


class TaskBudget:
    def __init__(self, limits, used, on_change, token, *, clock=time.monotonic, route=None):
        self.limits, self.used = dict(limits), dict(used)
        self.on_change, self.token, self.clock = on_change, token, clock
        self.lock = threading.RLock()
        self.started = clock()
        self.base_seconds = used.get("seconds", 0)
        self.reservations = {}
        # Unfinished requests after a crash have unknown final usage. Charge
        # their reserved envelopes once rather than treating them as free.
        if self.used.get("pending_tokens", 0):
            self.used["tokens"] = self.used.get("tokens", 0) + self.used["pending_tokens"]
            self.used["estimated_requests"] = self.used.get("estimated_requests", 0) + self.used.get("pending_requests", 1)
        self.used.update(pending_tokens=0, pending_requests=0)
        self.exhausted = ""
        self.timer = None
        self.route = route

    def start(self):
        limit = self.limits["seconds"]
        if limit is None:
            return
        remaining = limit - self.used.get("seconds", 0)
        if remaining <= 0:
            raise BudgetPaused("运行时长预算已用完")
        self.timer = threading.Timer(remaining, self._deadline)
        self.timer.daemon = True
        self.timer.start()

    def _deadline(self):
        with self.lock:
            self.exhausted = "运行时长预算已用完"
        self.token.cancel()

    def snapshot(self):
        with self.lock:
            self.used["seconds"] = round(self.base_seconds + self.clock() - self.started, 3)
            self.used["pending_tokens"] = sum(self.reservations.values())
            self.used["pending_requests"] = len(self.reservations)
            return dict(self.used)

    def check(self):
        with self.lock:
            if self.exhausted:
                raise BudgetPaused(self.exhausted)
            if self.limits["tokens"] is not None and self.used.get("tokens", 0) > self.limits["tokens"]:
                raise BudgetPaused("实际 token 用量超过预算，已保存检查点")
            if self.limits["seconds"] is not None and self.snapshot()["seconds"] >= self.limits["seconds"]:
                raise BudgetPaused("运行时长预算已用完")
        self.token.check()

    def reserve(self, messages, output_tokens, tools=None, *, route=None):
        import json
        projected = list(messages)
        if tools:
            projected += [dict(role="system", content=json.dumps(tools, ensure_ascii=False))]
        estimate = estimate_request_tokens(projected) + output_tokens
        with self.lock:
            self.check()
            if self.route is not None and route != self.route:
                raise BudgetPaused("模型服务商或端点已切换，请确认恢复后使用新配置")
            if self.limits["requests"] is not None and self.used.get("requests", 0) >= self.limits["requests"]:
                raise BudgetPaused("模型请求次数预算已用完")
            if self.limits["tokens"] is not None and self.used.get("tokens", 0) + sum(self.reservations.values()) + estimate > self.limits["tokens"]:
                raise BudgetPaused("剩余 token 预算不足以容纳下一次请求（含输出预留）")
            key = object()
            self.reservations[key] = estimate
            self.used["requests"] = self.used.get("requests", 0) + 1
            self.on_change(self.snapshot())
            return key

    def settle(self, key, result):
        with self.lock:
            estimate = self.reservations.pop(key)
            usage = getattr(result, "usage", None)
            total = getattr(usage, "total_tokens", None)
            if total is None and usage and usage.input_tokens is not None and usage.output_tokens is not None:
                total = usage.input_tokens + usage.output_tokens
            known = type(total) is int and total >= 0
            self.used["tokens"] = self.used.get("tokens", 0) + (total if known else estimate)
            if not known:
                self.used["estimated_requests"] = self.used.get("estimated_requests", 0) + 1
            self.on_change(self.snapshot())
            over = self.limits["tokens"] is not None and self.used["tokens"] > self.limits["tokens"]
            if over:
                self.exhausted = "实际 token 用量超过预算，已保存检查点"
        if over:
            self.token.cancel()

    def close(self):
        if self.timer:
            self.timer.cancel()
        self.on_change(self.snapshot())
