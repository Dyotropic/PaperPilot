"""多模型 LLM 统一抽象层（PHASE3_PLAN 功能三）。

支持 provider：deepseek / openai / anthropic / glm / kimi / qwen / ollama。
- OpenAI 兼容系（deepseek/openai/glm/kimi/qwen/ollama）经 openai SDK，
  仅 base_url 不同。
- Anthropic 经 anthropic SDK。

统一接口：
    get_client(task=None) -> LLMClient | None   # 每次现读 config，task 解析任务级模型覆盖
    client.chat(messages, ...) -> ChatResult    # 非流式，含归一化的 reasoning
    client.chat_stream(messages, ...) -> Iterator[str]  # 流式，yield content delta
    client.is_available / client.test_connection()

thinking 参数三态归一（跨家翻译）：
    None  = 调用方不关心，不传该参数（采用模型默认值）
    True  = 请求推理（DeepSeek: enabled；GPT-6: high；Claude 新模型: adaptive/high）
    False = 尽量减少推理（DeepSeek: disabled；GPT-6 Sol/Luna: none；
            Claude Fable 5.1/Opus 5.5 不支持关闭，改用 adaptive/low）

配置（config.yaml llm 节，缺失键回退默认，不报错）：
    llm:
      provider: deepseek
      api_key: ""          # 当前 provider 的密钥（兼容旧配置）
      api_keys: {}          # 设置页保存各 provider 的密钥；可省略
      base_url: ""          # 留空用 PROVIDERS 默认
      model: deepseek-flash
      score_model: ""       # 任务级覆盖（score/chat/reasoning），空=用 model
      chat_model: ""
      reasoning_model: ""   # 非空才启用两步推理

旧 deepseek: 节兼容：无 llm.provider 且存在 deepseek.api_key 时自动合成。
"""

import logging
from time import perf_counter
from dataclasses import dataclass, field
from contextlib import contextmanager
from contextvars import ContextVar
import json
from typing import Iterator

from paperpilot.config import load_config
from paperpilot.llm_usage import TokenUsage, normalize_usage, record_request, usage_scope
from paperpilot.agent_runtime import current_run, checkpoint, publish_reply, OperationCancelled

logger = logging.getLogger(__name__)
_tool_options = ContextVar("paperpilot_llm_tools", default={})


@contextmanager
def tools_scope(tools=None, tool_choice=None):
    """Request-local tool definitions for independently running agents."""
    token = _tool_options.set(dict(tools=tools, tool_choice=tool_choice))
    try:
        yield
    finally:
        _tool_options.reset(token)


@dataclass
class ChatResult:
    """chat() 统一返回。content 为正文；reasoning 为推理链
    （DeepSeek reasoning_content / Anthropic thinking block），无则为空串。"""
    content: str = ""
    reasoning: str = ""
    usage: TokenUsage | None = None
    provider: str = ""
    model: str = ""
    request_id: str | None = None
    elapsed_ms: int | None = None
    first_token_ms: int | None = None
    finish_reason: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    provider_blocks: list[dict] = field(default_factory=list)


# ── Provider 注册表 ──

PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-flash",
        "key_hint": "platform.deepseek.com 获取，sk- 开头",
    },
    "openai": {
        "label": "OpenAI / ChatGPT",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-6-sol",
        "key_hint": "platform.openai.com 获取，sk- 开头",
    },
    "anthropic": {
        "label": "Anthropic / Claude",
        "base_url": "",  # SDK 默认
        "default_model": "claude-opus-5-5",
        "key_hint": "console.anthropic.com 获取，sk-ant- 开头",
    },
    "glm": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-5.3",
        "key_hint": "open.bigmodel.cn 获取",
    },
    "kimi": {
        "label": "Kimi / Moonshot",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "kimi-k3",
        "key_hint": "platform.moonshot.cn 获取，sk- 开头",
    },
    "qwen": {
        "label": "阿里通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "key_hint": "dashscope.console.aliyun.com 获取，sk- 开头",
    },
    "ollama": {
        "label": "Ollama（本地）",
        "base_url": "http://localhost:11434/v1",
        "default_model": "qwen2.5:7b",
        "key_hint": "本地运行无需 Key，留空即可",
    },
}

# 每家内置模型清单：(model_id, 显示名)；下拉框额外提供"自定义…"手输
MODEL_CATALOG: dict[str, list[tuple[str, str]]] = {
    "deepseek": [
        ("deepseek-flash", "DeepSeek V4.1 Flash（当前）"),
        ("deepseek-v4-flash", "旧 ID（暂时转发至 V4.1 Flash）"),
        ("deepseek-v4-pro", "DeepSeek V4 Pro（深度推理）"),
    ],
    "openai": [
        ("gpt-6-astra", "GPT-6 Astra"),
        ("gpt-6-sol", "GPT-6 Sol"),
        ("gpt-6-luna", "GPT-6 Luna"),
        ("gpt-5.6-sol", "GPT-5.6 Sol"),
        ("gpt-5.6-terra", "GPT-5.6 Terra"),
        ("gpt-5.6-luna", "GPT-5.6 Luna"),
        ("gpt-5.5", "GPT-5.5"),
    ],
    "anthropic": [
        ("claude-fable-5-1", "Claude Fable 5.1"),
        ("claude-opus-5-5", "Claude Opus 5.5"),
        ("claude-sonnet-5", "Claude Sonnet 5"),
        ("claude-haiku-4-5", "Claude Haiku 4.5"),
    ],
    "glm": [
        ("glm-5.3", "GLM-5.3"),
        ("glm-5.2", "GLM-5.2"),
    ],
    "kimi": [
        ("kimi-k3", "Kimi K3"),
    ],
    "qwen": [
        ("qwen-plus", "Qwen Plus"),
        ("qwen-turbo", "Qwen Turbo"),
        ("qwen-max", "Qwen Max"),
    ],
    "ollama": [
        ("qwen2.5:7b", "qwen2.5:7b"),
    ],
}

_TASK_KEYS = ("score_model", "chat_model", "reasoning_model")


def _load_llm_cfg() -> dict:
    """读取 llm 配置，兼容旧 deepseek: 节（只读合成，不写回）。

    Returns:
        {provider, api_key, base_url, model, score_model, chat_model,
         reasoning_model}；无可用配置返回空 dict。
    """
    cfg = load_config()
    llm = cfg.get("llm", {}) or {}

    if llm.get("provider"):
        provider = str(llm.get("provider", "")).strip()
        provider_keys = llm.get("api_keys") or {}
        if not isinstance(provider_keys, dict):
            provider_keys = {}
        if provider in provider_keys:
            api_key = provider_keys[provider]
        else:
            api_key = llm.get("api_key", "")
            if not api_key and provider == "deepseek":
                api_key = (cfg.get("deepseek") or {}).get("api_key", "")
        return {
            "provider": provider,
            "api_key": str(api_key or "").strip(),
            "base_url": str(llm.get("base_url", "") or "").strip(),
            "model": str(llm.get("model", "") or "").strip(),
            **{k: str(llm.get(k, "") or "").strip() for k in _TASK_KEYS},
        }

    # 旧 deepseek 节迁移回退
    ds = cfg.get("deepseek", {}) or {}
    old_key = str(ds.get("api_key", "") or "").strip()
    if old_key:
        return {
            "provider": "deepseek",
            "api_key": old_key,
            "base_url": "",
            "model": str(ds.get("model", "") or "").strip() or PROVIDERS["deepseek"]["default_model"],
            **{k: str(ds.get(k, "") or "").strip() for k in _TASK_KEYS},
        }

    return {}


def get_client(task: str | None = None) -> "LLMClient | None":
    """按当前配置构建 LLM 客户端。

    Args:
        task: 任务名（"score"/"chat"/"reasoning"），用于解析 llm.{task}_model
              任务级模型覆盖；None 用主 model。

    Returns:
        LLMClient 实例；未配置（无 provider 且无旧 key）返回 None。
    """
    cfg = _load_llm_cfg()
    if not cfg:
        return None

    provider = cfg["provider"]
    if provider not in PROVIDERS:
        logger.warning("Unknown llm provider: %s", provider)
        return None

    model = cfg["model"] or PROVIDERS[provider]["default_model"]
    if task:
        task_model = cfg.get(f"{task}_model", "")
        if task_model:
            model = task_model

    base_url = cfg["base_url"] or PROVIDERS[provider]["base_url"]
    api_key = cfg["api_key"]

    # ollama 本地服务免 key；其余无 key 视为不可用
    if not api_key and provider != "ollama":
        return None

    if provider == "anthropic":
        client = AnthropicClient(api_key=api_key, model=model, base_url=base_url)
    else:
        client = OpenAICompatClient(provider=provider, base_url=base_url,
                                    api_key=api_key, model=model)
    client.task = task or "other"
    return client


def get_task_model(task: str) -> str:
    """解析任务级模型名（不动网络）。未配置覆盖时返回主 model。"""
    cfg = _load_llm_cfg()
    if not cfg:
        return ""
    model = cfg["model"] or PROVIDERS.get(cfg["provider"], {}).get("default_model", "")
    task_model = cfg.get(f"{task}_model", "")
    return task_model or model


def get_task_model_override(task: str) -> str:
    """返回显式配置的任务模型；留空时不回退到主模型。"""
    if task not in ("score", "chat", "reasoning"):
        return ""
    cfg = _load_llm_cfg()
    return cfg.get(f"{task}_model", "") if cfg else ""


def llm_configured() -> bool:
    """是否已配置任何可用的 LLM（provider + key，或 ollama）。"""
    cfg = _load_llm_cfg()
    if not cfg:
        return False
    return cfg["provider"] in PROVIDERS and (
        bool(cfg["api_key"]) or cfg["provider"] == "ollama")


def _is_retryable(exc: Exception) -> bool:
    """仅网络错误/5xx/超时重试；4xx（认证/参数错误）不重试。"""
    import httpx
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    try:
        import openai as _openai
        if isinstance(exc, _openai.APIStatusError):
            return exc.status_code >= 500
        if isinstance(exc, _openai.APIConnectionError):
            return True
    except Exception:
        pass
    try:
        import anthropic as _anthropic
        if isinstance(exc, _anthropic.APIStatusError):
            return exc.status_code >= 500
        if isinstance(exc, _anthropic.APIConnectionError):
            return True
    except Exception:
        pass
    return False


class LLMClient:
    """LLM 客户端基类。子类实现 _create_kwargs / _do_chat / _do_stream。"""

    def __init__(self, model: str):
        self.model = model
        self.task = "other"
        self.last_result = ChatResult()

    def _record(self, result, messages, use_model, status="ok", *, thinking=None):
        result.provider = getattr(self, "provider", "")
        result.model = result.model if isinstance(result.model, str) and result.model else use_model
        result.request_id = result.request_id if isinstance(result.request_id, str) else None
        self.last_result = result
        definitions = _tool_options.get().get("tools")
        fingerprint_messages = ([dict(role="system", content=json.dumps(definitions, sort_keys=True))] + messages
                                if definitions else messages)
        record_request(result, fingerprint_messages, task=self.task, status=status,
                       provider=result.provider, model=result.model, thinking=thinking)

    @property
    def is_available(self) -> bool:
        return True

    def chat(self, messages: list[dict], temperature: float = 0.3,
             max_tokens: int = 2000, timeout: int = 120,
             model: str | None = None, thinking: bool | None = None,
             retries: int = 1) -> ChatResult:
        """非流式调用。失败重试 1 次（仅网络/5xx），最终失败返回空 ChatResult。"""
        use_model = model or self.model
        for attempt in (1, 2):
            started = perf_counter()
            self.last_result = ChatResult()
            try:
                checkpoint()
                run = current_run()
                if run:
                    result = self._do_cancellable(messages, temperature, max_tokens,
                                                  timeout, use_model, thinking, run.token)
                else:
                    result = self._do_chat(messages, temperature, max_tokens,
                                           timeout, use_model, thinking)
                checkpoint()
                result.elapsed_ms = round((perf_counter() - started) * 1000)
                self._record(result, messages, use_model, thinking=thinking)
                return result
            except OperationCancelled as exc:
                result = exc.result or self.last_result
                result.elapsed_ms = round((perf_counter() - started) * 1000)
                self._record(result, messages, use_model, "cancelled", thinking=thinking)
                raise
            except Exception as e:
                failed = ChatResult(elapsed_ms=round((perf_counter() - started) * 1000))
                self._record(failed, messages, use_model, "error", thinking=thinking)
                if attempt <= retries and _is_retryable(e):
                    logger.warning("LLM call failed (attempt %d, %s): %s",
                                   attempt, type(e).__name__, e)
                    continue
                logger.warning("LLM call failed (%s): %s", type(e).__name__, e)
                break
        return self.last_result

    def chat_stream(self, messages: list[dict], temperature: float = 0.3,
                    max_tokens: int = 2000, timeout: int = 120,
                    model: str | None = None,
                    thinking: bool | None = None) -> Iterator[str]:
        """流式调用，yield content delta。失败抛异常（调用方自行降级）。"""
        use_model = model or self.model
        started = perf_counter()
        self.last_result = ChatResult()
        status, first_token, parts = "ok", None, []
        try:
            for delta in self._do_stream(messages, temperature, max_tokens,
                                         timeout, use_model, thinking):
                if first_token is None:
                    first_token = round((perf_counter() - started) * 1000)
                parts.append(delta)
                yield delta
        except GeneratorExit:
            status = "cancelled"
            raise
        except Exception:
            status = "error"
            raise
        finally:
            result = self.last_result
            result.content = "".join(parts)
            result.elapsed_ms = round((perf_counter() - started) * 1000)
            result.first_token_ms = first_token
            self._record(result, messages, use_model, status, thinking=thinking)

    def test_connection(self) -> tuple[bool, str]:
        """轻量连通性测试。Returns: (ok, message)。失败信息含具体异常。"""
        messages = [{"role": "user", "content": "回复 OK 两个字母即可"}]
        started = perf_counter()
        try:
            result = self._do_chat(
                messages,
                0.0, 512, 30, self.model, False,
            )
            result.elapsed_ms = round((perf_counter() - started) * 1000)
            with usage_scope(task="connection_test"):
                self._record(result, messages, self.model, thinking=False)
            if result.content:
                return True, f"连接成功（模型 {self.model}）"
            return False, "API 返回空内容，请检查 Key 与模型名"
        except Exception as e:
            with usage_scope(task="connection_test"):
                self._record(ChatResult(elapsed_ms=round((perf_counter() - started) * 1000)),
                             messages, self.model, "error", thinking=False)
            return False, f"连接失败：{type(e).__name__}: {e}"

    # 子类实现
    def _do_cancellable(self, messages, temperature, max_tokens, timeout, model, thinking, token):
        # Compatibility for custom clients; built-in providers override transport.
        token.check()
        self.last_result = self._do_chat(messages, temperature, max_tokens, timeout, model, thinking)
        token.check()
        publish_reply(self.last_result.content)
        return self.last_result

    def _do_chat(self, messages, temperature, max_tokens, timeout,
                 model, thinking) -> ChatResult:
        raise NotImplementedError

    def _do_stream(self, messages, temperature, max_tokens, timeout,
                   model, thinking) -> Iterator[str]:
        raise NotImplementedError


class OpenAICompatClient(LLMClient):
    """OpenAI 兼容客户端：deepseek / openai / glm / kimi / qwen / ollama。"""

    def __init__(self, provider: str, base_url: str, api_key: str, model: str):
        super().__init__(model)
        self.provider = provider
        self.base_url = base_url
        self.api_key = api_key
        self._client = None  # 懒创建，避免 import 期开销

    def _get_client(self, timeout: int):
        import openai
        if self._client is None:
            self._client = openai.OpenAI(
                api_key=self.api_key or "ollama",
                base_url=self.base_url or None,
                timeout=timeout,
                max_retries=0,  # 重试由 LLMClient.chat 统一控制
            )
        return self._client

    def _request_kwargs(self, messages, model, max_tokens, thinking) -> dict:
        fields = ("role", "content", "name", "tool_calls", "tool_call_id", "reasoning_content", "prefix")
        kwargs = {"messages": [{k: m[k] for k in fields if k in m} for m in messages], "model": model}
        options = _tool_options.get()
        if options.get("tools"):
            kwargs["tools"] = options["tools"]
            if options.get("tool_choice"):
                kwargs["tool_choice"] = options["tool_choice"]
            if self.provider in {"openai", "deepseek"}:
                kwargs["parallel_tool_calls"] = False
        if self.provider == "openai":
            kwargs["max_completion_tokens"] = max_tokens
            if model in ("gpt-6-astra", "gpt-6-sol", "gpt-6-luna"):
                if thinking is True:
                    kwargs["reasoning_effort"] = "high"
                elif thinking is False:
                    kwargs["reasoning_effort"] = (
                        "low" if model == "gpt-6-astra" else "none"
                    )
        else:
            kwargs["max_tokens"] = max_tokens
        if self.provider == "deepseek" and thinking is not None:
            kwargs["extra_body"] = {
                "thinking": {"type": "enabled" if thinking else "disabled"}
            }
        return kwargs

    def _do_chat(self, messages, temperature, max_tokens, timeout,
                 model, thinking) -> ChatResult:
        kwargs = self._request_kwargs(messages, model, max_tokens, thinking)
        client = self._get_client(timeout)
        resp = client.chat.completions.create(**kwargs)
        content = ""
        reasoning = ""
        if resp.choices:
            msg = resp.choices[0].message
            content = msg.content or ""
            reasoning = getattr(msg, "reasoning_content", "") or ""
        return ChatResult(content=content, reasoning=reasoning,
                          tool_calls=[t.model_dump(exclude_none=True) for t in getattr(msg, "tool_calls", []) or []] if resp.choices else [],
                          finish_reason=getattr(resp.choices[0], "finish_reason", None) if resp.choices else None,
                          usage=normalize_usage(getattr(resp, "usage", None), self.provider),
                          model=getattr(resp, "model", None) or model,
                          request_id=getattr(resp, "id", None))

    def _do_cancellable(self, messages, temperature, max_tokens, timeout, model, thinking, token):
        import openai
        from contextlib import aclosing
        from openai.types.chat import ChatCompletionChunk
        class ManagedChatStream(openai.AsyncStream[ChatCompletionChunk]):
            """Close SSE iterators in order before the worker's event loop exits.

            The installed SDK closes the response at [DONE], leaving its nested
            generators for concurrent loop shutdown. Keep its decoder and typed
            parsing, but explicitly own that iterator lifetime.
            """
            def _iter_events(self):
                self._managed_events = self._read_events()
                return self._managed_events

            async def _read_events(self):
                async with aclosing(self.response.aiter_bytes()) as chunks:
                    async with aclosing(self._decoder.aiter_bytes(chunks)) as events:
                        async for event in events:
                            yield event

            async def __stream__(self):
                async with aclosing(super().__stream__()) as chunks:
                    try:
                        async for chunk in chunks:
                            yield chunk
                    finally:
                        events = getattr(self, "_managed_events", None)
                        if events is not None:
                            await events.aclose()
        kwargs = self._request_kwargs(messages, model, max_tokens, thinking)
        kwargs["stream"] = True
        if self.provider in {"openai", "deepseek"}:
            kwargs["stream_options"] = {"include_usage": True}
        result = self.last_result = ChatResult(model=model)
        calls = {}
        async def request():
            async with openai.AsyncOpenAI(api_key=self.api_key or "ollama",
                    base_url=self.base_url or None, timeout=timeout, max_retries=0) as client:
                async with client.chat.completions.with_streaming_response.create(**kwargs) as response:
                    stream = await response.parse(to=ManagedChatStream)
                    async for chunk in stream:
                        result.request_id = getattr(chunk, "id", None) or result.request_id
                        result.model = getattr(chunk, "model", None) or result.model
                        usage = normalize_usage(getattr(chunk, "usage", None), self.provider)
                        if usage is not None:
                            result.usage = usage
                        if chunk.choices:
                            result.finish_reason = getattr(chunk.choices[0], "finish_reason", None) or result.finish_reason
                            delta = chunk.choices[0].delta
                            result.reasoning += getattr(delta, "reasoning_content", "") or ""
                            for change in getattr(delta, "tool_calls", []) or []:
                                row = calls.setdefault(change.index, dict(id="", type="function", function=dict(name="", arguments="")))
                                if change.id:
                                    row["id"] = change.id
                                if change.function:
                                    if change.function.name:
                                        row["function"]["name"] += change.function.name
                                    if change.function.arguments:
                                        row["function"]["arguments"] += change.function.arguments
                            result.tool_calls = [calls[i] for i in sorted(calls)]
                            if delta.content:
                                result.content += delta.content
                                publish_reply(result.content)
            return result
        try:
            return token.run_async(request)
        except OperationCancelled:
            raise OperationCancelled(result) from None

    def _do_stream(self, messages, temperature, max_tokens, timeout,
                   model, thinking) -> Iterator[str]:
        kwargs = self._request_kwargs(messages, model, max_tokens, thinking)
        kwargs["stream"] = True
        if self.provider in {"openai", "deepseek"}:
            kwargs["stream_options"] = {"include_usage": True}
        client = self._get_client(timeout)
        resp = client.chat.completions.create(**kwargs)
        for chunk in resp:
            usage = normalize_usage(getattr(chunk, "usage", None), self.provider)
            if usage is not None:
                self.last_result.usage = usage
            self.last_result.model = getattr(chunk, "model", None) or model
            self.last_result.request_id = getattr(chunk, "id", None)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content


class AnthropicClient(LLMClient):
    """Anthropic Messages API 客户端（Claude 系）。"""

    # Anthropic thinking 模式的最小预算
    _MIN_THINKING_BUDGET = 1024
    _ADAPTIVE_MODELS = frozenset({
        "claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5",
    })
    _ALWAYS_THINKING_MODELS = frozenset({
        "claude-fable-5-1", "claude-opus-5-5",
    })

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        super().__init__(model)
        self.provider = "anthropic"
        self.api_key = api_key
        self.base_url = base_url
        self._client = None

    def _get_client(self, timeout: int):
        import anthropic
        if self._client is None:
            kw = {"api_key": self.api_key, "timeout": timeout, "max_retries": 0}
            if self.base_url:
                kw["base_url"] = self.base_url
            self._client = anthropic.Anthropic(**kw)
        return self._client

    def _split_messages(self, messages: list[dict]) -> tuple[str, list[dict]]:
        """OpenAI messages → (system_text, non_system_messages)。"""
        from paperpilot.agent_attachments import anthropic_content
        system_parts = []
        rest = []
        for m in messages:
            if m.get("role") == "system":
                system_parts.append(m.get("content", ""))
            elif m.get("role") == "tool":
                rest.append(dict(role="user", content=[dict(type="tool_result",
                    tool_use_id=m["tool_call_id"], content=anthropic_content(m.get("content", "")))]))
            elif m.get("role") == "assistant" and m.get("provider_blocks"):
                rest.append(dict(role="assistant", content=m["provider_blocks"]))
            else:
                role = "assistant" if m.get("role") == "assistant" else "user"
                content = anthropic_content(m.get("content", ""))
                if m.get("tool_calls"):
                    content = [dict(type="text", text=content)] if isinstance(content, str) and content else content or []
                    for call in m["tool_calls"]:
                        content.append(dict(type="tool_use", id=call["id"], name=call["function"]["name"],
                            input=json.loads(call["function"]["arguments"])))
                rest.append({"role": role, "content": content})
        if not rest:
            rest = [{"role": "user", "content": ""}]
        return "\n\n".join(p for p in system_parts if p), rest

    def _tool_kwargs(self):
        options = _tool_options.get()
        tools = options.get("tools")
        if not tools:
            return {}
        result = dict(tools=[dict(name=t["function"]["name"],
            description=t["function"].get("description", ""),
            input_schema=t["function"]["parameters"]) for t in tools])
        choice = options.get("tool_choice") or "auto"
        result["tool_choice"] = dict(type=choice)
        if choice != "none":
            result["tool_choice"]["disable_parallel_tool_use"] = True
        return result

    @staticmethod
    def _native_calls(blocks):
        return [dict(id=b.id, type="function", function=dict(name=b.name,
            arguments=json.dumps(b.input, ensure_ascii=False))) for b in blocks if b.type == "tool_use"]

    def _do_cancellable(self, messages, temperature, max_tokens, timeout, model, thinking, token):
        import anthropic
        system, rest = self._split_messages(messages)
        tkw, use_max = self._thinking_kwargs(thinking, max_tokens, model)
        kwargs = dict(model=model, messages=rest, max_tokens=use_max, **tkw, **self._tool_kwargs())
        if system:
            kwargs["system"] = system
        options = dict(api_key=self.api_key, timeout=timeout, max_retries=0)
        if self.base_url:
            options["base_url"] = self.base_url
        result = self.last_result = ChatResult(model=model)
        async def request():
            async with anthropic.AsyncAnthropic(**options) as client:
                async with client.messages.stream(**kwargs) as stream:
                    async for event in stream:
                        if event.type == "content_block_delta":
                            if event.delta.type == "text_delta":
                                result.content += event.delta.text
                                publish_reply(result.content)
                            elif event.delta.type == "thinking_delta":
                                result.reasoning += event.delta.thinking
                    final = await stream.get_final_message()
                    result.request_id, result.model = final.id, final.model
                    result.finish_reason = final.stop_reason
                    result.usage = normalize_usage(final.usage, "anthropic")
                    result.tool_calls = self._native_calls(final.content)
                    result.provider_blocks = [b.model_dump(exclude_none=True) for b in final.content]
            return result
        try:
            return token.run_async(request)
        except OperationCancelled:
            # Without message_stop the final billing usage is unknown, not zero.
            raise OperationCancelled(result) from None

    def _thinking_kwargs(self, thinking: bool | None,
                         max_tokens: int, model: str | None = None) -> tuple[dict, int]:
        """按模型支持的思考模式构造请求参数。"""
        selected_model = model or self.model
        if selected_model in self._ADAPTIVE_MODELS:
            if thinking is True:
                budget = max(self._MIN_THINKING_BUDGET, min(4000, max_tokens))
                return ({"thinking": {"type": "adaptive"},
                         "output_config": {"effort": "high"}}, max_tokens + budget)
            if thinking is False:
                if selected_model == "claude-sonnet-5":
                    return {"thinking": {"type": "disabled"}}, max_tokens
                # Fable 5.1 / Opus 5.5 的 adaptive thinking 不能关闭。
                return ({"thinking": {"type": "adaptive"},
                         "output_config": {"effort": "low"}},
                        max(max_tokens, self._MIN_THINKING_BUDGET))
            if selected_model in self._ALWAYS_THINKING_MODELS:
                return {}, max(max_tokens, self._MIN_THINKING_BUDGET)
            return {}, max_tokens
        if thinking is not True:
            return {}, max_tokens
        budget = max(self._MIN_THINKING_BUDGET, min(4000, max_tokens))
        return ({"thinking": {"type": "enabled", "budget_tokens": budget}},
                max_tokens + budget)

    def _do_chat(self, messages, temperature, max_tokens, timeout,
                 model, thinking) -> ChatResult:
        system, rest = self._split_messages(messages)
        tkw, use_max = self._thinking_kwargs(thinking, max_tokens, model)
        kwargs: dict = {
            "model": model,
            "messages": rest,
            "max_tokens": use_max,
            **tkw,
        }
        if system:
            kwargs["system"] = system
        kwargs.update(self._tool_kwargs())
        client = self._get_client(timeout)
        resp = client.messages.create(**kwargs)
        content_parts = []
        reasoning_parts = []
        for block in (resp.content or []):
            btype = getattr(block, "type", "")
            if btype == "text":
                content_parts.append(getattr(block, "text", ""))
            elif btype == "thinking":
                reasoning_parts.append(getattr(block, "thinking", "")
                                       or getattr(block, "text", ""))
        return ChatResult(content="".join(content_parts),
                          tool_calls=self._native_calls(resp.content or []),
                          provider_blocks=[b.model_dump(exclude_none=True) for b in resp.content or []],
                          finish_reason=getattr(resp, "stop_reason", None),
                          reasoning="\n".join(p for p in reasoning_parts if p),
                          usage=normalize_usage(getattr(resp, "usage", None), "anthropic"),
                          model=getattr(resp, "model", None) or model,
                          request_id=getattr(resp, "id", None))

    def _do_stream(self, messages, temperature, max_tokens, timeout,
                   model, thinking) -> Iterator[str]:
        system, rest = self._split_messages(messages)
        tkw, use_max = self._thinking_kwargs(thinking, max_tokens, model)
        kwargs: dict = {
            "model": model,
            "messages": rest,
            "max_tokens": use_max,
            **tkw,
        }
        if system:
            kwargs["system"] = system
        client = self._get_client(timeout)
        with client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                yield text
            final = stream.get_final_message()
            self.last_result.usage = normalize_usage(getattr(final, "usage", None), "anthropic")
            self.last_result.model = getattr(final, "model", None) or model
            self.last_result.request_id = getattr(final, "id", None)
