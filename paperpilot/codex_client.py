"""ChatGPT subscription backend using the official Codex app-server protocol.

PaperPilot owns history and tool execution. Ephemeral Codex threads have no host
environment; a dynamic tool request pauses the turn until our dispatcher replies.
"""
import copy
import hashlib
import json
import queue
import threading
import time
import uuid

from paperpilot.agent_runtime import current_run, OperationCancelled, publish_reply
from paperpilot.codex_transport import CodexError, get_server
from paperpilot.llm_client import LLMClient, ChatResult, _tool_options, MODEL_CAPABILITIES
from paperpilot.llm_usage import normalize_usage, usage_context, message_fingerprints


def _owner(token):
    scope = usage_context()
    run = current_run()
    # Main chat becomes team_review after dispatch; accounting labels may change
    # within the same owned run. Worker cancellation tokens remain independent.
    return (scope.get("project_id"), scope.get("session_id"),
            getattr(run, "id", id(run) if run else None), token)


def _fingerprint(messages):
    # Reasoning may be saved separately by a caller; it is not a routing identity.
    canonical = [{k: m[k] for k in ("role", "content", "tool_calls", "tool_call_id") if k in m}
                 for m in messages if m.get("role") not in {"system", "developer"}]
    return hashlib.sha256("".join(message_fingerprints(canonical)).encode()).hexdigest()


def _parts(content):
    if isinstance(content, str):
        return [dict(type="text", text=content)]
    if not isinstance(content, list):
        raise ValueError("Codex 消息内容必须是文字或图片列表。")
    output = []
    for part in content:
        if part.get("type") == "text":
            output.append(dict(type="text", text=part.get("text", "")))
        elif part.get("type") == "image_url":
            image = part["image_url"]
            output.append(dict(type="image", url=image["url"], detail=image.get("detail", "auto")))
        else:
            raise ValueError("Codex 暂不支持这种消息资料类型。")
    return output


def _history_items(messages):
    """Public thread/inject_items accepts the native Responses item format."""
    items = []
    for message in messages:
        role = message.get("role")
        if role in {"system", "developer"}:
            continue
        if role == "tool":
            items.append(dict(type="function_call_output", call_id=message["tool_call_id"],
                              output=message.get("content") or ""))
            continue
        content = []
        for part in _parts(message.get("content") or ""):
            if part["type"] == "text":
                content.append(dict(type="output_text" if role == "assistant" else "input_text",
                                    text=part["text"]))
            else:
                content.append(dict(type="input_image", image_url=part["url"], detail=part["detail"]))
        if content and (message.get("content") or not message.get("tool_calls")):
            items.append(dict(type="message", role=role, content=content))
        for call in message.get("tool_calls") or []:
            items.append(dict(type="function_call", call_id=call["id"], namespace="paperpilot",
                              name=call["function"]["name"], arguments=call["function"]["arguments"]))
    return items


class _Turn:
    def __init__(self, server, thread_id, model, owner, token, definitions):
        self.server, self.thread_id, self.model = server, thread_id, model
        self.owner, self.token, self.definitions = owner, token, definitions
        self.turn_id, self.pending, self.expected = None, None, None
        self.marker = uuid.uuid4().hex
        self.events = queue.Queue()
        self.lock = threading.RLock()
        self.disposed = False
        self.total, self.reported = None, None
        self.timer = None
        self.unsubscribe = lambda: None

    def arm_cancellation(self):
        # Register only after the state is owned by server.states. A token that
        # stops during subscription must neither leak a state nor its callback.
        with self.lock:
            if self.token:
                self.unsubscribe = self.token.subscribe(self.cancel)
                if self.disposed: self.unsubscribe()

    def cancel(self):
        self.dispose(interrupt=True)
        self.events.put(OperationCancelled())

    def dispose(self, interrupt=False):
        with self.lock:
            if self.disposed:
                return
            self.disposed = True
        if self.timer:
            self.timer.cancel()
        self.unsubscribe()
        if interrupt and self.turn_id:
            self.server.fire("turn/interrupt", dict(threadId=self.thread_id, turnId=self.turn_id))
        self.server.fire("thread/unsubscribe", dict(threadId=self.thread_id))
        with self.server._lock:
            self.server.states.pop(self.thread_id, None)

    def usage(self):
        if self.total is None:
            return None
        # One ephemeral thread has one turn; subtract already reported tool pauses.
        # Never charge the SDK's lifetime total again on each continuation.
        delta = {k: v - (self.reported or {}).get(k, 0) for k, v in self.total.items()
                 if type(v) is int and v >= (self.reported or {}).get(k, 0)}
        self.reported = dict(self.total)
        return normalize_usage(delta, "codex")


class CodexSubscriptionClient(LLMClient):
    provider = "codex"

    def __init__(self, model="codex-default", *, cli=None, home=None):
        super().__init__(model)
        self.cli, self.home = cli, home

    @property
    def server(self):
        return get_server(self.cli, self.home)

    @property
    def is_available(self):
        try:
            run = current_run()
            return bool(self.server.models(token=run.token if run else None))
        except (CodexError, OSError):
            return False

    def _resume(self, server, messages, token):
        if usage_context().get("task") in {"compression", "reasoning"}:
            return None  # Auxiliary calls must not consume a main agent's tool proposal.
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            markers = [b for b in message.get("provider_blocks") or []
                       if b.get("type") == "paperpilot_codex"]
            if not markers:
                continue
            marker = markers[-1]
            with server._lock:
                state = server.states.get(marker.get("thread_id"))
            if state is None:
                return None  # Restored history: rebuild a fresh, owned thread.
            owner = _owner(token)
            if state.marker != marker.get("continuation"):
                raise CodexError("Codex 继续标识无效。")
            if state.owner != owner:
                # A new main run may continue a failed run after its journal was
                # saved. Never deliver its observations to the previous live turn.
                if (state.owner[:2] == owner[:2] and owner[1] is not None and
                        usage_context().get("task") == "chat" and messages[-1].get("role") == "user"):
                    state.dispose(interrupt=True)
                    return None
                raise CodexError("Codex 工具结果不属于当前会话或任务。")
            if not state.pending or _fingerprint(messages[:index + 1]) != state.expected:
                raise CodexError("Codex 工具调用历史已改变，不能将结果投递给旧任务。")
            extras = messages[index + 1:]
            if not extras or extras[0].get("role") != "tool" or \
                    extras[0].get("tool_call_id") != state.pending["params"]["callId"]:
                raise CodexError("缺少对应的 Codex 工具结果。")
            if any(m.get("role") != "user" for m in extras[1:]):
                raise CodexError("Codex 工具结果后的资料归属无效。")
            options = _tool_options.get()
            if not options.get("tools") or options.get("tool_choice") == "none":
                # Codex cannot withdraw dynamic tools midway through a turn.
                # Rebuild exact resolved history in a fresh turn without tools.
                state.dispose(interrupt=True)
                return None
            if state.timer:
                state.timer.cancel(); state.timer = None
            content = [dict(type="inputText", text=extras[0].get("content") or "")]
            for extra in extras[1:]:
                for part in _parts(extra.get("content") or ""):
                    content.append(dict(type="inputText", text=part["text"]) if part["type"] == "text"
                                   else dict(type="inputImage", imageUrl=part["url"]))
            if token: token.check()
            server.respond(state.pending["id"], dict(contentItems=content, success=True))
            state.pending = None
            return state
        return None

    def _begin(self, messages, max_tokens, model, thinking, timeout, token):
        if not messages:
            raise ValueError("Codex 消息不能为空。")
        if type(max_tokens) is not int or max_tokens <= 0 or timeout <= 0:
            raise ValueError("Codex 输出长度与超时时间必须为正数。")
        server = self.server
        with server._start_lock:
            state = self._resume(server, messages, token)
            if state:
                if model not in {state.model, "codex-default"}:
                    raise CodexError("工具执行期间不能切换 Codex 模型。")
                return state
            models = server.models(token=token)
            selected = next((m for m in models if m.get("isDefault")), models[0] if models else None) \
                if model == "codex-default" else next((m for m in models if m["model"] == model), None)
            if selected is None:
                raise CodexError("当前 Codex 账号未提供所选模型，请刷新模型目录后选择。")
            definitions = _tool_options.get()
            tools = [] if definitions.get("tool_choice") == "none" else definitions.get("tools") or []
            functions = [dict(type="function", name=t["function"]["name"],
                description=t["function"].get("description", ""),
                inputSchema=copy.deepcopy(t["function"].get("parameters") or {}), deferLoading=False)
                for t in tools]
            instructions = "\n\n".join(str(m.get("content") or "") for m in messages
                                         if m.get("role") in {"system", "developer"})
            instructions += (f"\n\nReply length guideline: aim within {max_tokens} output tokens. "
                             "This is a guideline, not a hard token limit.")
            choice = definitions.get("tool_choice")
            if isinstance(choice, dict):
                instructions += "\nUse the requested tool: " + choice.get("function", {}).get("name", "")
            payload = dict(model=selected["model"], modelProvider="openai", cwd=str(server.home / "work"),
                approvalPolicy="never", sandbox="read-only", environments=[], runtimeWorkspaceRoots=[],
                ephemeral=True, baseInstructions="You are a scientific research assistant in PaperPilot. "
                "Follow the supplied research instructions. Use only provided PaperPilot tools.",
                developerInstructions=instructions, dynamicTools=[dict(type="namespace", name="paperpilot",
                    description="Tools dispatched and authorized by PaperPilot", tools=functions)] if functions else [])
            if token: token.check()
            result = server.rpc("thread/start", payload, token=token,
                on_abandoned=lambda r: server.fire("thread/unsubscribe", dict(threadId=r["thread"]["id"])))
            state = _Turn(server, result["thread"]["id"], selected["model"], _owner(token), token,
                          {f["name"] for f in functions})
            with server._lock: server.states[state.thread_id] = state
            state.arm_cancellation()
        try:
            if token: token.check()
            if state.disposed: raise OperationCancelled()
            # Only the final user message starts the turn. Prior messages remain exact history.
            split = len(messages) - 1 if messages[-1].get("role") == "user" else len(messages)
            history = _history_items(messages[:split])
            if history:
                server.rpc("thread/inject_items", dict(threadId=state.thread_id, items=history), token=token)
            effort = [e["reasoningEffort"] for e in selected.get("supportedReasoningEfforts") or []]
            args = dict(threadId=state.thread_id,
                input=_parts(messages[-1].get("content") or "") if split < len(messages) else
                      [dict(type="text", text="Continue from the supplied conversation and tool results.")],
                environments=[], approvalPolicy="never", sandboxPolicy=dict(type="readOnly", networkAccess=False))
            if thinking is not None and effort:
                preferred = ("high", "medium", "low", "minimal", "none") if thinking else \
                            ("none", "minimal", "low", "medium", "high")
                args["effort"] = next((e for e in preferred if e in effort), effort[0])
            def abandoned(r):
                server.fire("turn/interrupt", dict(threadId=state.thread_id, turnId=r["turn"]["id"]))
            started = server.rpc("turn/start", args, token=token, on_abandoned=abandoned)
            state.turn_id = started["turn"]["id"]
            if token: token.check()
            return state
        except BaseException:
            state.dispose(interrupt=True)
            raise

    def _consume(self, state, messages, timeout, token):
        result = self.last_result = ChatResult(model=state.model, request_id=state.turn_id)
        texts, phases = {}, {}
        deadline = time.monotonic() + timeout
        paused = False
        last_error = ""
        try:
            while True:
                if token: token.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexError("Codex 回复超时，当前轮已中断。" + ("服务反馈：" + last_error if last_error else ""))
                try: event = state.events.get(timeout=min(.1, remaining))
                except queue.Empty: continue
                if isinstance(event, BaseException): raise event
                method, params = event.get("method"), event.get("params") or {}
                if params.get("turnId") not in {None, state.turn_id}:
                    continue
                if method == "item/agentMessage/delta":
                    if not state.turn_id: state.turn_id = params.get("turnId")
                    ident, delta = params["itemId"], params.get("delta", "")
                    texts[ident] = texts.get(ident, "") + delta
                    result.content = "\n".join(texts.values())
                    publish_reply(result.content)
                    yield delta
                elif method in {"item/started", "item/completed"}:
                    item = params.get("item") or {}
                    if item.get("type") == "agentMessage":
                        phases[item["id"]] = item.get("phase")
                        if method == "item/completed" and item.get("text") is not None:
                            previous = texts.get(item["id"], "")
                            texts[item["id"]] = item["text"]
                            result.content = "\n".join(texts.values())
                            if item["text"].startswith(previous) and item["text"] != previous:
                                yield item["text"][len(previous):]
                elif method in {"item/reasoning/summaryTextDelta", "item/reasoning/textDelta"}:
                    result.reasoning += params.get("delta", "")
                elif method == "thread/tokenUsage/updated":
                    usage = params.get("tokenUsage") or {}
                    state.total = usage.get("total") or usage.get("last")
                    window = usage.get("modelContextWindow")
                    if type(window) is int and window > 0:
                        for key in {(self.provider, state.model), (self.provider, self.model)}:
                            MODEL_CAPABILITIES.setdefault(key, {})["window"] = window
                elif method == "item/tool/call":
                    if params.get("namespace") != "paperpilot" or params.get("tool") not in state.definitions:
                        state.server.respond(event["id"], dict(contentItems=[dict(type="inputText",
                            text="Tool is not permitted by PaperPilot")], success=False))
                        raise CodexError("Codex 请求了未授权的工具，当前轮已中断。")
                    result.tool_calls = [dict(id=params["callId"], type="function", function=dict(
                        name=params["tool"], arguments=json.dumps(params.get("arguments") or {}, ensure_ascii=False)))]
                    state.pending = event
                    result.provider_blocks = [dict(type="paperpilot_codex", thread_id=state.thread_id,
                                                  continuation=state.marker)]
                    result.finish_reason = "tool_calls"
                    paused = True
                    break
                elif "id" in event:
                    state.server.respond(event["id"], error=dict(code=-32601, message="Permission denied by PaperPilot"))
                    raise CodexError("Codex 请求了额外权限，当前轮已中断。")
                elif method == "error":
                    from paperpilot.codex_transport import _safe_message
                    last_error = _safe_message((params.get("error") or {}).get("message", "服务连接失败"))
                    if not params.get("willRetry"):
                        raise CodexError("Codex 服务失败：" + last_error)
                elif method == "turn/completed":
                    turn = params.get("turn") or {}
                    if turn.get("status") == "interrupted": raise OperationCancelled(result)
                    if turn.get("status") == "failed":
                        from paperpilot.codex_transport import _safe_message
                        raise CodexError("Codex 任务失败：" + _safe_message((turn.get("error") or {}).get("message", "未知错误")))
                    result.finish_reason = "stop"
                    break
            final = [v for k, v in texts.items() if phases.get(k) == "final_answer"]
            result.content = "\n".join(final if final else texts.values())
            result.usage = state.usage()
            if paused:
                expected = dict(role="assistant", content=result.content, tool_calls=result.tool_calls)
                state.expected = _fingerprint(messages + [expected])
                state.timer = threading.Timer(max(timeout, 300), state.cancel)
                state.timer.daemon = True; state.timer.start()
            publish_reply(result.content)
        except OperationCancelled as exc:
            result.usage = state.usage()
            exc.result = result
            raise
        finally:
            if not paused or (token and token.cancelled): state.dispose(interrupt=result.finish_reason != "stop")

    def _do_chat(self, messages, temperature, max_tokens, timeout, model, thinking):
        run = current_run()
        return self._do_cancellable(messages, temperature, max_tokens, timeout, model, thinking,
                                    run.token if run else None)

    def _do_cancellable(self, messages, temperature, max_tokens, timeout, model, thinking, token):
        state = self._begin(messages, max_tokens, model, thinking, timeout, token)
        for _ in self._consume(state, messages, timeout, token): pass
        return self.last_result

    def _do_stream(self, messages, temperature, max_tokens, timeout, model, thinking):
        run = current_run()
        token = run.token if run else None
        state = self._begin(messages, max_tokens, model, thinking, timeout, token)
        yield from self._consume(state, messages, timeout, token)
