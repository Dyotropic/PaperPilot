"""Per-search timings and counters, shared explicitly with source workers.

Diagnostics contain stage names and counts, never queries, URLs or credentials.
Overlapping source durations must not be added to obtain wall-clock time.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time
import uuid

_trace = ContextVar("paperpilot_search_trace", default=None)


class SearchTrace:
    def __init__(self):
        self.id = uuid.uuid4().hex[:12]
        self.started = time.perf_counter()
        self.records = []
        self.counters = {}
        self._lock = threading.Lock()

    def count(self, name, amount=1, *, source=""):
        key = f"{source}.{name}" if source else name
        with self._lock:
            self.counters[key] = self.counters.get(key, 0) + amount

    def snapshot(self):
        with self._lock:
            return dict(run_id=self.id, elapsed_seconds=time.perf_counter() - self.started,
                        stages=[dict(row) for row in self.records], counters=dict(self.counters))

    def record(self, stage, seconds, *, source="", status="ok"):
        with self._lock:
            self.records.append(dict(stage=stage, source=source, status=status, seconds=seconds))

    def report(self, boundary="pipeline"):
        data = self.snapshot()
        stages = ", ".join(
            f"{r['source'] + '.' if r['source'] else ''}{r['stage']}={r['seconds']:.3f}s"
            f"({r['status']})" for r in data["stages"])
        counters = ", ".join(f"{k}={v}" for k, v in sorted(data["counters"].items()))
        print(f"[SearchTiming] run={self.id} boundary={boundary} total={data['elapsed_seconds']:.3f}s "
              f"stages=[{stages}] counters=[{counters}]", flush=True)


@contextmanager
def trace_scope(trace):
    token = _trace.set(trace)
    try:
        yield trace
    finally:
        _trace.reset(token)


@contextmanager
def timed_stage(stage, *, source=""):
    trace = _trace.get()
    started, status = time.perf_counter(), "ok"
    try:
        yield
    except BaseException as exc:
        status = "cancelled" if type(exc).__name__ == "OperationCancelled" else "error"
        raise
    finally:
        if trace is not None:
            with trace._lock:
                trace.records.append(dict(stage=stage, source=source, status=status,
                                          seconds=time.perf_counter() - started))


def count(name, amount=1, *, source=""):
    trace = _trace.get()
    if trace is not None:
        trace.count(name, amount, source=source)
