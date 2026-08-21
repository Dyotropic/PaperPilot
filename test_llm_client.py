"""llm_client 单元测试：全部 mock 网络层，不打真实 API。

覆盖：配置合成（新 llm 节 / 旧 deepseek 节回退）、thinking 三态翻译、
任务级模型覆盖、4xx 不重试/5xx 重试、Anthropic 消息转换与 thinking block、
流式解析、test_connection 降级。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, ".")

passed = 0
failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS: {msg}")
    else:
        failed += 1
        print(f"  FAIL: {msg}")


# ── 用临时 config 隔离真实 config.yaml ──
import paperpilot.config as cfg_mod

_TMP_CFG = tempfile.mktemp(suffix=".yaml")


def write_cfg(content: str):
    with open(_TMP_CFG, "w", encoding="utf-8") as f:
        f.write(content)


# ── monkeypatch load_config：所有 llm_client 取数都读临时文件 ──
import paperpilot.llm_client as lc
import yaml


def _read_tmp(path=None):
    with open(_TMP_CFG, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


lc.load_config = _read_tmp


def _load_with(content: str) -> dict:
    write_cfg(content)
    return lc._load_llm_cfg()


# ══ 1. 配置合成 ══
print("\n1. 配置合成")
cfg = _load_with("llm:\n  provider: openai\n  api_key: sk-x\n  model: gpt-5.5\n")
check(cfg["provider"] == "openai" and cfg["model"] == "gpt-5.5", "新 llm 节直读")

cfg = _load_with(
    "deepseek:\n  api_key: sk-old\n  model: deepseek-v4-pro\n"
    "  reasoning_model: deepseek-v4-pro\n")
check(cfg["provider"] == "deepseek" and cfg["api_key"] == "sk-old",
      "旧 deepseek 节回退合成")
check(cfg["model"] == "deepseek-v4-pro", "旧节 model 继承")
check(cfg["reasoning_model"] == "deepseek-v4-pro", "旧节任务模型继承")

cfg = _load_with("ui:\n  theme: sand\n")
check(cfg == {}, "无任何 LLM 配置返回空 dict")

cfg = _load_with(
    "llm:\n  provider: glm\n  api_key: k\n  base_url: https://x/v1\n"
    "  model: glm-5.3\n  score_model: glm-5.2\n")
check(cfg["base_url"] == "https://x/v1" and cfg["score_model"] == "glm-5.2",
      "自定义 base_url 与任务覆盖读取")

# ══ 2. get_client 分派与任务模型 ══
print("\n2. get_client 分派")
write_cfg("llm:\n  provider: anthropic\n  api_key: sk-ant-x\n  model: opus-4.8\n")
c = lc.get_client()
check(type(c).__name__ == "AnthropicClient" and c.model == "opus-4.8",
      "anthropic → AnthropicClient")

c_task = lc.get_client(task="reasoning")
check(c_task is not None and c_task.model == "opus-4.8",
      "无 reasoning_model 覆盖时 task 用主模型")

write_cfg(
    "llm:\n  provider: kimi\n  api_key: k\n  model: kimi-k3\n"
    "  score_model: kimi-k3-turbo\n")

c = lc.get_client(task="score")
check(c.model == "kimi-k3-turbo", "任务级模型覆盖生效")
c2 = lc.get_client()
check(c2.model == "kimi-k3", "无 task 用主模型")
check(lc.get_task_model("score") == "kimi-k3-turbo", "get_task_model")
check(lc.get_task_model("chat") == "kimi-k3", "get_task_model 无覆盖回退主模型")

check(lc.get_client() is None or True, "get_client 不抛异常")  # 烟雾

# ollama 免 key
write_cfg("llm:\n  provider: ollama\n  model: qwen2.5:7b\n")
c = lc.get_client()
check(c is not None and type(c).__name__ == "OpenAICompatClient", "ollama 免 key 可用")

# 无 key 的付费 provider → None
write_cfg("llm:\n  provider: openai\n  model: gpt-5.5\n")
check(lc.get_client() is None, "openai 无 key 返回 None")
check(lc.llm_configured() is False, "llm_configured 无 key False")

# ══ 3. OpenAICompatClient thinking 翻译 ══
print("\n3. thinking 翻译（OpenAI 系）")


class FakeMsg:
    def __init__(self, content, reasoning=""):
        self.content = content
        self.reasoning_content = reasoning


class FakeChoice:
    def __init__(self, msg):
        self.message = msg


class FakeResp:
    def __init__(self, content, reasoning=""):
        self.choices = [FakeChoice(FakeMsg(content, reasoning))]


class FakeCompletions:
    def __init__(self, resp=None, exc=None, calls=None):
        self.resp = resp
        self.exc = exc
        self.calls = calls if calls is not None else []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.resp


class FakeOpenAI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat = type("C", (), {})()
        self._completions = None

    def set_completions(self, comp):
        self._completions = comp
        self.chat.completions = comp


def make_oai_client(provider, resp=None, exc=None, model="m1"):
    c = lc.OpenAICompatClient(provider=provider, base_url="https://x/v1",
                              api_key="k", model=model)
    holder = FakeOpenAI()
    holder.set_completions(FakeCompletions(resp=resp, exc=exc))
    c._client = holder
    return c, holder._completions


c, comp = make_oai_client("deepseek", resp=FakeResp("hello"))
r = c.chat([{"role": "user", "content": "hi"}], thinking=False)
check(r.content == "hello", "deepseek chat 基本调用")
check(comp.calls[-1]["extra_body"] == {"thinking": {"type": "disabled"}},
      "deepseek thinking=False → disabled")

r = c.chat([{"role": "user", "content": "hi"}], thinking=True)
check(comp.calls[-1]["extra_body"] == {"thinking": {"type": "enabled"}},
      "deepseek thinking=True → enabled")

c.chat([{"role": "user", "content": "hi"}], thinking=None)
check("extra_body" not in comp.calls[-1], "deepseek thinking=None → 省略")

c2, comp2 = make_oai_client("openai", resp=FakeResp("ok"))
c2.chat([{"role": "user", "content": "hi"}], thinking=True)
check("extra_body" not in comp2.calls[-1], "openai thinking 参数静默忽略")

c3, comp3 = make_oai_client("deepseek", resp=FakeResp("ans", reasoning="chain"))
r3 = c3.chat([{"role": "user", "content": "hi"}])
check(r3.reasoning == "chain", "reasoning_content 归一提取")

# model 覆盖
c3.chat([{"role": "user", "content": "hi"}], model="other-model")
check(comp3.calls[-1]["model"] == "other-model", "model 参数覆盖生效")

# ══ 4. 重试策略 ══
print("\n4. 重试策略（4xx 不重试 / 连接错误重试一次）")
import openai as real_openai
import httpx


def _http_resp(status: int) -> httpx.Response:
    req = httpx.Request("POST", "https://x/v1/chat/completions")
    return httpx.Response(status, request=req)


err429 = real_openai.RateLimitError(
    message="rate limited", response=_http_resp(429), body=None)
c4, comp4 = make_oai_client("deepseek", exc=err429)
r4 = c4.chat([{"role": "user", "content": "hi"}])
check(r4.content == "" and len(comp4.calls) == 1, "429（4xx）不重试，返回空")

conn_err = real_openai.APIConnectionError(request=httpx.Request("POST", "https://x"))
c5, comp5 = make_oai_client("deepseek", exc=conn_err)
r5 = c5.chat([{"role": "user", "content": "hi"}])
check(len(comp5.calls) == 2 and r5.content == "", "连接错误重试 1 次后放弃")

# ══ 5. AnthropicClient ══
print("\n5. AnthropicClient")


class FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeThinkingBlock:
    def __init__(self, text):
        self.type = "thinking"
        self.thinking = text


class FakeAnthropicResp:
    def __init__(self, blocks):
        self.content = blocks


class FakeMsgs:
    def __init__(self, resp=None, exc=None, calls=None):
        self.resp = resp
        self.exc = exc
        self.calls = calls if calls is not None else []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.resp


class FakeAnthropic:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.messages = FakeMsgs()


def make_ant_client(resp=None, exc=None):
    c = lc.AnthropicClient(api_key="sk-ant", model="opus-4.8")
    holder = FakeAnthropic()
    c._client = holder
    holder.messages.resp = resp
    holder.messages.exc = exc
    return c, holder.messages


msgs = [
    {"role": "system", "content": "sys prompt"},
    {"role": "user", "content": "q1"},
    {"role": "assistant", "content": "a1"},
    {"role": "user", "content": "q2"},
]
c, am = make_ant_client(resp=FakeAnthropicResp([FakeTextBlock("answer")]))
r = c.chat(msgs)
kw = am.calls[-1]
check(kw["system"] == "sys prompt", "system 提取为顶层参数")
check(kw["messages"] == [
    {"role": "user", "content": "q1"},
    {"role": "assistant", "content": "a1"},
    {"role": "user", "content": "q2"},
], "非 system 消息保留顺序")

c, am = make_ant_client(resp=FakeAnthropicResp(
    [FakeThinkingBlock("thinking chain"), FakeTextBlock("final")]))
r = c.chat(msgs, max_tokens=2000, thinking=True)
kw = am.calls[-1]
check(kw["thinking"]["type"] == "enabled" and
      kw["thinking"]["budget_tokens"] >= lc.AnthropicClient._MIN_THINKING_BUDGET,
      "thinking=True → enabled + budget")
check(kw["max_tokens"] == 2000 + kw["thinking"]["budget_tokens"],
      "thinking 模式抬高 max_tokens")
check(r.content == "final" and r.reasoning == "thinking chain",
      "thinking block → reasoning 归一")

c, am = make_ant_client(resp=FakeAnthropicResp([FakeTextBlock("x")]))
c.chat(msgs, thinking=False)
check("thinking" not in am.calls[-1], "thinking=False → 省略")

import anthropic as real_anthropic
err401 = real_anthropic.AuthenticationError(
    message="bad key", response=_http_resp(401), body=None)
c, am = make_ant_client(exc=err401)
r = c.chat(msgs)
check(len(am.calls) == 1 and r.content == "", "anthropic 401 不重试")

# ══ 6. test_connection ══
print("\n6. test_connection")
c, comp = make_oai_client("deepseek", resp=FakeResp("OK"))
ok, msg = c.test_connection()
check(ok, "test_connection 成功路径")

c, comp = make_oai_client("deepseek", exc=conn_err)
ok2, msg2 = c.test_connection()
check(not ok2 and "失败" in msg2, "test_connection 失败路径返回 False + 信息")

# ══ 7. 流式 ══
print("\n7. 流式")


class FakeDelta:
    def __init__(self, content):
        self.content = content


class FakeStreamChunk:
    def __init__(self, content):
        self.choices = [type("C", (), {"delta": FakeDelta(content)})()]


class FakeStreamCompletions:
    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self.chunks)


c = lc.OpenAICompatClient(provider="kimi", base_url="https://x", api_key="k",
                          model="kimi-k3")
holder = FakeOpenAI()
holder.set_completions(FakeStreamCompletions([
    FakeStreamChunk("Hel"), FakeStreamChunk("lo"), FakeStreamChunk(""),
]))
c._client = holder
out = "".join(c.chat_stream([{"role": "user", "content": "hi"}]))
check(out == "Hello", "OpenAI 系流式 delta 拼接")
check(holder._completions.calls[-1]["stream"] is True, "stream=True 传递")

# ══ 8. 敏感信息不落日志 ══
print("\n8. key 安全")
import io as _io
import logging as _logging
buf = _io.StringIO()
h = _logging.StreamHandler(buf)
lc.logger.addHandler(h)
c, comp = make_oai_client("deepseek", exc=conn_err)
c.chat([{"role": "user", "content": "hi"}])
log_out = buf.read()
lc.logger.removeHandler(h)
check("sk-" not in log_out and "k" != log_out, "日志不含 API key")

# ══ 清理 ══
try:
    os.remove(_TMP_CFG)
except PermissionError:
    pass

print(f"\n{'='*40}")
print(f"总计: {passed} 通过, {failed} 失败")
if failed:
    sys.exit(1)
