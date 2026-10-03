"""Opt-in native subscription probe; official auth cache copy, isolated runner.

PAPERPILOT_CODEX_AUTH_SOURCE names an existing auth.json. It is copied opaquely;
neither credentials nor account identifiers are printed or included in evidence.
Consumes a small amount of the selected account's Codex subscription quota.
"""
import json
import os
from pathlib import Path
import threading
import time
import queue
from collections import Counter
from types import SimpleNamespace

if not os.environ.get("PAPERPILOT_VALIDATION_ROOT"):
    raise SystemExit("Run via tools/run_validation.py")

from paperpilot.codex_transport import get_server, close_servers
from paperpilot.codex_client import CodexSubscriptionClient
from paperpilot.agent_runtime import CancellationToken, OperationCancelled, run_scope
from paperpilot.llm_client import tools_scope
from paperpilot.agent_team import CALCULATE_TOOL, calculate

source = os.environ.get("PAPERPILOT_CODEX_AUTH_SOURCE")
if not source: raise SystemExit("Set PAPERPILOT_CODEX_AUTH_SOURCE to opt in")
summary = dict(native_cli=True,account_type=None,models=[],scenarios=[])
server = get_server()
methods = Counter()
class TracedQueue(queue.Queue):
    def put(self, event, *args, **kwargs):
        if isinstance(event,dict): methods[event.get("method", "reply")] += 1
        super().put(event,*args,**kwargs)
class TracedStates(dict):
    def __setitem__(self, key, state):
        state.events = TracedQueue()
        super().__setitem__(key,state)
server.states = TracedStates()
try:
    account = server.import_login(Path(source))
    summary["account_type"] = account["type"]
    models = server.models(True)
    summary["models"] = [m["model"] for m in models]
    client = CodexSubscriptionClient()
    model = os.environ.get("PAPERPILOT_CODEX_MODEL", "codex-default")
    for label, question in (("english_science", "In at most two sentences, explain why sample size matters when comparing robot navigation accuracy."),
                            ("chinese_tools", "请用 calculate 工具计算 sqrt(9+16)，然后用一句中文说明结果；不要调用其他工具。")):
        messages = [dict(role="system",content="You are a scientific research assistant. Return short answers grounded in the given question."),
                    dict(role="user",content=question)]
        started = time.monotonic()
        token = CancellationToken()
        timer = threading.Timer(45, token.cancel); timer.start()
        try:
            with run_scope(SimpleNamespace(id=label,token=token)), tools_scope([CALCULATE_TOOL] if label.endswith("tools") else None):
                result = client._do_cancellable(messages,.3,300,40,model,False,token)
                used_tool = bool(result.tool_calls)
                for _ in range(3):
                    if not result.tool_calls: break
                    messages.append(dict(role="assistant",content=result.content,tool_calls=result.tool_calls,provider_blocks=result.provider_blocks))
                    for call in result.tool_calls:
                        if call["function"]["name"] != "calculate": raise RuntimeError("Unpermitted tool")
                        value = calculate(json.loads(call["function"]["arguments"]))
                        assert value["value"] == 5
                        messages.append(dict(role="tool",tool_call_id=call["id"],content=json.dumps(value,ensure_ascii=False)))
                    result = client._do_cancellable(messages,.3,300,40,model,False,token)
                assert result.content and not result.tool_calls
                if label.endswith("tools"): assert used_tool
                summary["scenarios"].append(dict(name=label,passed=True,model=result.model,tool=used_tool,
                    seconds=round(time.monotonic()-started,2),reported_usage=bool(result.usage)))
        finally: timer.cancel()
except OperationCancelled:
    summary["error"] = "Live probe canceled or timed out"
except Exception as exc:
    from paperpilot.codex_transport import _safe_message
    summary["error"] = _safe_message(exc)
finally:
    close_servers()
    summary["event_methods"] = dict(methods)
    Path("codex_live.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(summary,ensure_ascii=True))
if summary.get("error"): raise SystemExit(1)
