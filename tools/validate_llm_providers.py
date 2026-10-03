"""Provider acceptance with real SDKs and isolated loopback HTTP, never paid APIs.

Run through tools/run_validation.py. Exercises scientific chat, scoring, reading,
translation, tool replay, cancellation, usage, old configuration and model bounds.
Replies and metadata are fixtures; this does not assess vendor model quality.
"""
import copy
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

if not os.environ.get("PAPERPILOT_VALIDATION_ROOT"):
    raise SystemExit("Run with tools/run_validation.py to protect user configuration and data")

from paperpilot import llm_client as lc, library
from paperpilot.config import save_config, load_config, CONFIG_PATH
from paperpilot.agent_runtime import AgentRun, OperationCancelled, run_scope, reply_stream
from paperpilot.ai_service import AIService
from paperpilot.llm_usage import UsageStore, normalize_usage
from paperpilot.agent_attachments import ensure_image_support, AttachmentError
from paperpilot.context_budget import context_policy
from paperpilot.openai_responses import _input_items
from paperpilot.agent_team import CALCULATE_TOOL


SIGNATURE = {"google": {"thought_signature": "opaque-fixture-signature"}}
USAGE = dict(prompt_tokens=100, completion_tokens=20, total_tokens=120,
             prompt_tokens_details=dict(cached_tokens=60), completion_tokens_details=dict(reasoning_tokens=5))


def completion(model, text="", call=None):
    message = dict(role="assistant", content=text)
    if call:
        message["tool_calls"] = [call]
    return dict(id="local-completion", object="chat.completion", created=0, model=model,
                choices=[dict(index=0, message=message, finish_reason="tool_calls" if call else "stop")], usage=USAGE)


def response(model, text="", call=None, status="completed"):
    output = [dict(type="reasoning", id="rs_local", summary=[], encrypted_content="encrypted-local-reasoning")]
    if call:
        output.append(dict(type="function_call", id="fc_local", call_id=call["id"],
            name=call["function"]["name"], arguments=call["function"]["arguments"], status="completed"))
    else:
        output.append(dict(type="message", id="msg_local", role="assistant", status="completed",
            content=[dict(type="output_text", text=text, annotations=[])]))
    return dict(id="resp_local", object="response", created_at=0, status=status, model=model,
        output=output, parallel_tool_calls=False, tools=[], tool_choice="auto", error=None,
        incomplete_details=dict(reason="max_output_tokens") if status == "incomplete" else None,
        usage=dict(input_tokens=100, output_tokens=20, total_tokens=120,
                   input_tokens_details=dict(cached_tokens=60), output_tokens_details=dict(reasoning_tokens=5)))


def function(name="calculate", params=None, ident="call_local", signature=False):
    call = dict(id=ident, type="function", function=dict(name=name,
        arguments=json.dumps(params or dict(expression="sqrt(9+16)"), ensure_ascii=False)))
    if signature:
        call["extra_content"] = copy.deepcopy(SIGNATURE)
    return call


@contextmanager
def transport(route, *, gate=None):
    captured, arrived, delivered, release = [], threading.Event(), threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append((self.path, body))
            arrived.set()
            if gate == "headers":
                release.wait(8)
            status, value = route(self.path, body)
            try:
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream" if body.get("stream") and status == 200 else "application/json")
                self.send_header("Connection", "close")
                self.end_headers()
                if not body.get("stream") or status != 200:
                    self.wfile.write(json.dumps(value, ensure_ascii=False).encode())
                    return
                def event(data):
                    self.wfile.write(("data: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode())
                    self.wfile.flush()
                if self.path.endswith("/responses"):
                    initial = dict(value, output=[], status="in_progress", usage=None)
                    event(dict(type="response.created", response=initial, sequence_number=0))
                    for index, item in enumerate(value["output"]):
                        empty = dict(item)
                        if item["type"] == "message":
                            empty["content"] = []
                        elif item["type"] == "function_call":
                            empty["arguments"] = ""
                        event(dict(type="response.output_item.added", output_index=index, item=empty, sequence_number=1))
                        if item["type"] == "message":
                            event(dict(type="response.content_part.added", output_index=index, content_index=0,
                                item_id=item["id"], part=dict(type="output_text", text="", annotations=[]), sequence_number=2))
                            event(dict(type="response.output_text.delta", output_index=index, content_index=0,
                                item_id=item["id"], delta=item["content"][0]["text"], sequence_number=3))
                        elif item["type"] == "function_call":
                            args = item["arguments"]
                            for delta in (args[:len(args)//2], args[len(args)//2:]):
                                event(dict(type="response.function_call_arguments.delta", output_index=index,
                                    item_id=item["id"], delta=delta, sequence_number=3))
                    delivered.set()
                    if gate == "body":
                        release.wait(8)
                    event(dict(type="response." + value["status"], response=value, sequence_number=4))
                else:
                    message = value["choices"][0]["message"]
                    calls = message.get("tool_calls", [])
                    if calls:
                        call = copy.deepcopy(calls[0]); call["index"] = 0
                        args = call["function"]["arguments"]
                        call["function"]["arguments"] = args[:len(args)//2]
                        deltas = [dict(tool_calls=[call]), dict(tool_calls=[dict(index=0, function=dict(arguments=args[len(args)//2:]))])]
                    else:
                        deltas = [dict(content=message["content"])]
                    for delta in deltas:
                        event(dict(id=value["id"], object="chat.completion.chunk", model=value["model"],
                            choices=[dict(index=0, delta=delta, finish_reason=None)]))
                    delivered.set()
                    if gate == "body":
                        release.wait(8)
                    event(dict(id=value["id"], object="chat.completion.chunk", model=value["model"],
                        choices=[dict(index=0, delta={}, finish_reason=value["choices"][0]["finish_reason"])]))
                    event(dict(id=value["id"], object="chat.completion.chunk", model=value["model"], choices=[], usage=value["usage"]))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                self.close_connection = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", captured, arrived, delivered, release
    finally:
        release.set()
        server.shutdown(); server.server_close(); worker.join(2)


def configure(provider, base, model=None, **kwargs):
    model = model or lc.PROVIDERS[provider]["default_model"]
    config = dict(provider=provider, api_key="synthetic-key", api_keys={provider: "synthetic-key"},
        base_url=base, model=model, score_model="", chat_model="", reasoning_model="")
    config.update(kwargs)
    save_config({"llm": config})


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.original_bytes = CONFIG_PATH.read_bytes()
    def tearDown(self):
        # Exact restoration avoids carrying an override to the next scenario.
        CONFIG_PATH.write_bytes(self.original_bytes)

    def test_presets_keys_legacy_and_task_routing(self):
        self.assertEqual(set(lc.PROVIDERS), set(lc.MODEL_CATALOG))
        for provider, meta in lc.PROVIDERS.items():
            self.assertIn(meta["default_model"], dict(lc.MODEL_CATALOG[provider]))
        configure("gemini", "", chat_model="gemini-3.1-pro-preview")
        self.assertEqual(lc.get_client("chat").model, "gemini-3.1-pro-preview")
        self.assertEqual(lc.get_client("score").model, "gemini-3.8-flash")
        save_config({"llm": {"api_key": "wrong-active-key", "api_keys": {"gemini": "saved-gemini-key"}}})
        self.assertEqual(lc.get_client().api_key, "saved-gemini-key")
        with patch.object(lc, "load_config", return_value={"deepseek": dict(api_key="legacy-key", model="legacy-model")}):
            self.assertEqual(lc.get_client().model, "legacy-model")
        configure("gemini", "", model="tenant-custom-model")
        self.assertEqual(lc.get_client().model, "tenant-custom-model")
        save_config({"llm": {"api_keys": {"gemini": ""}}})
        self.assertIsNone(lc.get_client()); self.assertFalse(lc.llm_configured())

    def test_ollama_cloud_keys_and_local_compatibility(self):
        for base in ("", "http://localhost:11434/v1", "http://127.0.0.1:11434/v1",
                     "http://[::1]:11434/v1", "http://192.168.1.2:11434/v1"):
            self.assertFalse(lc.requires_api_key("ollama", base))
        for base in ("https://ollama.com/v1", "https://OLLAMA.COM./v1"):
            self.assertTrue(lc.requires_api_key("ollama", base))
        configure("ollama", "https://ollama.com/v1")
        save_config({"llm": {"api_key": "", "api_keys": {"ollama": ""}}})
        self.assertIsNone(lc.get_client()); self.assertFalse(lc.llm_configured())
        save_config({"llm": {"base_url": ""}})
        self.assertIsNotNone(lc.get_client()); self.assertTrue(lc.llm_configured())
        save_config({"llm": {"base_url": "http://192.168.1.2:11434/v1"}})
        self.assertIsNotNone(lc.get_client()); self.assertTrue(lc.llm_configured())
        with transport(lambda p,b:(200, completion(b["model"], "cloud fixture"))) as (base, captured, *_):
            configure("ollama", base, model="gemma4:31b")
            self.assertEqual(lc.get_client().chat([dict(role="user", content="研究摘要")]).content, "cloud fixture")
            self.assertEqual(captured[0][0], "/v1/chat/completions")

    def test_capabilities_and_explicit_overrides(self):
        configure("gemini", "")
        self.assertEqual(context_policy().window, 1_048_576)
        ensure_image_support("gemini", "gemini-3.8-flash")
        save_config({"agent": {"context_windows": {"gemini": {"gemini-3.8-flash": 32768}},
                               "image_support": {"gemini": {"gemini-3.8-flash": False}}}})
        self.assertEqual(context_policy().window, 32768)
        with self.assertRaises(AttachmentError): ensure_image_support("gemini", "gemini-3.8-flash")
        with self.assertRaises(AttachmentError): ensure_image_support("glm", "glm-5.3")
        with self.assertRaises(AttachmentError): ensure_image_support("gemini", "unknown-model")
        configure("openai", "")
        self.assertEqual(context_policy().window, 1_050_000)
        self.assertIsNone(context_policy("tenant-custom").window)

    def test_thinking_translation_for_current_models(self):
        for provider, model in (("gemini", "gemini-3.8-flash"), ("glm", "glm-5.3"), ("glm", "glm-5.3-flashx"), ("kimi", "kimi-k3")):
            client = lc.OpenAICompatClient(provider, "http://127.0.0.1", "synthetic", model)
            self.assertNotIn("reasoning_effort", client._request_kwargs([], model, 1000, None))
            self.assertEqual(client._request_kwargs([], model, 1000, False)["reasoning_effort"], "low")
            self.assertEqual(client._request_kwargs([], model, 1000, True)["reasoning_effort"], "high")
        client = lc.AnthropicClient("synthetic", "claude-sonnet-5-5")
        self.assertEqual(client._thinking_kwargs(False, 1000)[0], {"thinking": {"type": "disabled"}})
        self.assertEqual(client._thinking_kwargs(True, 1000)[0]["thinking"]["type"], "adaptive")
        client = lc.OpenAICompatClient("qwen", "", "synthetic", "qwen3.8-max")
        self.assertEqual(client._request_kwargs([], client.model, 1000, False)["extra_body"], {"enable_thinking": False})

    def test_gemini_json_sse_images_tools_and_usage(self):
        call = function(signature=True)
        def route(path, body):
            self.assertEqual(path, "/v1/chat/completions")
            return 200, completion(body["model"], call=call)
        with transport(route) as (base, captured, *_):
            configure("gemini", base)
            client = lc.get_client()
            messages = [dict(role="system", content="研究资料"), dict(role="user", content=[
                dict(type="text", text="核验图中坐标"),
                dict(type="image_url", image_url=dict(url="data:image/png;base64,synthetic-image"))])]
            with lc.tools_scope([CALCULATE_TOOL]):
                result = client.chat(messages, thinking=False)
                list(client.chat_stream(messages, thinking=False))
            self.assertEqual(result.tool_calls[0]["extra_content"], SIGNATURE)
            self.assertEqual(client.last_result.tool_calls[0]["extra_content"], SIGNATURE)
            self.assertEqual(client.last_result.finish_reason, "tool_calls")
            self.assertEqual(client.last_result.usage.cache_hit_tokens, 60)
            for _, body in captured:
                self.assertEqual(body["reasoning_effort"], "low")
                self.assertEqual(body["messages"][1]["content"], messages[1]["content"])
                self.assertFalse(body["parallel_tool_calls"])
            self.assertTrue(captured[1][1]["stream_options"]["include_usage"])

    def test_scientific_workflows_in_chinese_english_and_multiple_fields(self):
        topics = ("机器人导航：误差与样本数", "Cancer immunotherapy: outcomes and limitations", "钙钛矿稳定性：温度、寿命与重复性")
        def route(path, body):
            messages = body["messages"]
            system = str(messages[0].get("content", ""))
            if "core_contribution" in system:
                text = json.dumps(dict(core_contribution="仅分析所提供摘要", method="比较观测指标",
                    key_evidence="样本数尚未提供", highlights="需要同条件验证", limitations="全文未验证",
                    scores=dict(novelty=6, rigor=5, significance=7)), ensure_ascii=False)
            elif "reason_relevance" in system:
                text = json.dumps([dict(index=0, relevance=8, method=7, novelty=6, recency=9,
                    reason_relevance="研究问题相符", reason_method="摘要未提供对照样本", reason_novelty="未作同条件比较")], ensure_ascii=False)
            else:
                text = "已审查研究依据；样本规模与因果解释仍未验证。"
            return 200, completion(body["model"], text)
        with transport(route) as (base, captured, *_):
            configure("gemini", base)
            service = AIService()
            for topic in topics:
                project = library.create_project(topic, topic)
                paper = dict(title=topic, abstract="Fixture abstract: measured signal, method and uncertainty.",
                             doi="10.local/" + str(project.id), year=2026, authors=["Fixture"])
                read = service.deep_read(paper, full_text=paper["abstract"])
                self.assertEqual(read["core_contribution"], "仅分析所提供摘要")
                scored = service.score_papers(topic, [paper])
                self.assertTrue(scored)
                self.assertIn("未验证", service.chat(project.id, project.name, "解释证据限制", topic_desc=topic)["reply"])
                self.assertEqual(service.get_conversation(project.id, project.name).total_rounds, 1)
                records = UsageStore().records(project.id)
                self.assertTrue(records)
                self.assertTrue(all(r["provider"] == "gemini" for r in records))
            self.assertGreaterEqual(len(captured), 9)

    def test_search_keyword_extraction_translation_and_reasoning_budget(self):
        from paperpilot.core_extractor import extract_core_keywords, extract_regular_keywords
        from paperpilot.mt_translator import translate_terms, translate_all_terms, _cache
        for provider in ("gemini", "openai"):
            with self.subTest(provider=provider):
                def route(path, body):
                    messages = body.get("input", body.get("messages", []))
                    prompt = messages[0]["content"]
                    limit = body.get("max_output_tokens", body.get("max_tokens"))
                    # Simulate a mandatory-thinking response consuming a short
                    # output limit before visible text; not a real vendor budget.
                    if limit < 1000:
                        text = ""
                    elif "scientific translator" in prompt:
                        text = "1. perovskite\n2. interface passivation"
                    elif "5-8" in prompt:
                        text = "钙钛矿、界面钝化、缺陷密度、热稳定性、载流子寿命"
                    else:
                        text = "钙钛矿、界面钝化"
                    return (200, response(body["model"], text) if path.endswith("/responses")
                            else completion(body["model"], text))
                with transport(route) as (base, captured, *_):
                    configure(provider, base)
                    _cache.clear()
                    for topic in ("钙钛矿界面钝化与热稳定性", "Interface passivation in perovskite solar cells"):
                        self.assertEqual(extract_core_keywords(topic), ["钙钛矿", "界面钝化"])
                        self.assertEqual(len(extract_regular_keywords(topic)), 5)
                    groups = translate_all_terms(["钙钛矿", "CAR-T"], ["界面钝化", "钙钛矿"], [" "])
                    self.assertEqual(groups, [["perovskite", "CAR-T"], ["interface passivation", "perovskite"], [""]])
                    before = len(captured)
                    self.assertEqual(translate_terms([]), [])
                    self.assertEqual(translate_terms(["CAR-T", " "]), ["CAR-T", ""])
                    self.assertEqual(translate_terms(["钙钛矿"]), ["perovskite"])
                    self.assertEqual(len(captured), before)
                    save_config({"llm": {"reasoning_output_reserve": 0}})
                    self.assertEqual(extract_core_keywords("严格限制输出"), [])
                    save_config({"llm": {"reasoning_output_reserve": True}})
                    self.assertEqual(extract_core_keywords("错误预算配置"), ["钙钛矿", "界面钝化"])
                    self.assertEqual(lc.reasoning_output_budget("deepseek", "deepseek-flash", 30), 30)
                    save_config({"llm": {"reasoning_output_reserve": 4096}})

    def test_gemini_and_responses_agent_calculation_replay_with_real_sdk(self):
        for provider in ("gemini", "openai"):
            with self.subTest(provider=provider):
                def route(path, body):
                    items = body.get("input", body.get("messages", []))
                    observed = [m for m in items if m.get("role") == "tool" or m.get("type") == "function_call_output"]
                    call = None if observed else function(signature=provider == "gemini")
                    text = "主 Agent 复算得到5；独立性假设仍需核验。" if observed else ""
                    if observed:
                        observation = json.loads(observed[-1].get("output", observed[-1].get("content")))
                        self.assertEqual(observation["value"], 5)
                        if provider == "gemini":
                            assistant = next(m for m in items if m.get("tool_calls"))
                            self.assertEqual(assistant["tool_calls"][0]["extra_content"], SIGNATURE)
                        else:
                            self.assertTrue(any(m.get("encrypted_content") == "encrypted-local-reasoning" for m in items))
                    return 200, response(body["model"], text, call) if path.endswith("/responses") else completion(body["model"], text, call)
                with transport(route) as (base, captured, *_):
                    configure(provider, base)
                    service = AIService(); project = library.create_project(provider + " 工具核验", "测量误差")
                    cm = service.get_conversation(project.id, project.name)
                    run = AgentRun(cm, project.id, "计算 sqrt(9+16)，核验误差")
                    with run_scope(run):
                        answer = service.chat(project.id, project.name, run.goal)
                    run.finish()
                    self.assertIn("复算得到5", answer["reply"])
                    self.assertEqual(cm.total_rounds, 1)
                    self.assertEqual(len(captured), 2)
                    self.assertTrue(all(b["stream"] for _, b in captured))
                    record = next(m for m in cm._messages if m.get("tool_calls"))
                    self.assertTrue(record.get("provider_blocks") if provider == "openai" else record["tool_calls"][0].get("extra_content"))
                    self.assertEqual(UsageStore().records(project.id)[0]["cache_hit_tokens"], 60)
                    if provider == "openai":
                        self.assertTrue(all(p == "/v1/responses" for p, _ in captured))
                        self.assertFalse(captured[0][1]["tools"][0]["strict"])
                        self.assertFalse(captured[0][1]["store"])

    def test_responses_plain_stream_overrides_usage_and_incomplete(self):
        for status in ("completed", "incomplete"):
            with transport(lambda p,b:(200, response(b["model"], "部分研究结论", status=status)
                                      if p.endswith("/responses") else completion(b["model"], "旧模型兼容"))) as (base, captured, *_):
                configure("openai", base)
                client = lc.get_client()
                result = client.chat([dict(role="user", content="研究方法")], thinking=False)
                self.assertEqual(result.usage.reasoning_tokens, 5)
                self.assertEqual(result.finish_reason, "stop" if status == "completed" else "length")
                text = "".join(client.chat_stream([dict(role="user", content="Explain limitations")], thinking=True))
                self.assertEqual(text, "部分研究结论")
                self.assertEqual(client.last_result.finish_reason, result.finish_reason)
                self.assertEqual(captured[0][1]["reasoning"]["effort"], "low")
                self.assertEqual(captured[1][1]["reasoning"]["effort"], "high")
                self.assertEqual(client.chat([dict(role="user", content="兼容旧模型")], model="gpt-5.6-sol").content, "旧模型兼容")
                self.assertEqual(captured[-1][0], "/v1/chat/completions")

    def test_cancel_before_headers_midstream_and_resume(self):
        for provider in ("gemini", "openai"):
            for gate in ("headers", "body"):
                with self.subTest(provider=provider, gate=gate):
                    def route(path, body):
                        return 200, response(body["model"], "已保存的片段") if path.endswith("/responses") else completion(body["model"], "已保存的片段")
                    with transport(route, gate=gate) as (base, captured, arrived, delivered, release):
                        configure(provider, base)
                        project = library.create_project(provider + gate + str(time.time_ns()), "研究取消")
                        service = AIService(); cm = service.get_conversation(project.id, project.name)
                        run = AgentRun(cm, project.id, "请核验结论")
                        done, errors = threading.Event(), []
                        def work():
                            try:
                                with run_scope(run), reply_stream(): lc.get_client().chat([dict(role="user", content=run.goal)], timeout=10)
                            except BaseException as exc: errors.append(exc)
                            finally: run.finish(); done.set()
                        thread = threading.Thread(target=work, daemon=True); thread.start()
                        self.assertTrue(arrived.wait(4))
                        if gate == "body":
                            self.assertTrue(delivered.wait(4))
                            deadline = time.monotonic() + 3
                            while not run.partial and time.monotonic() < deadline: time.sleep(.01)
                            self.assertEqual(run.partial, "已保存的片段")
                        run.stop()
                        self.assertTrue(done.wait(2), "SDK cancellation did not drain")
                        self.assertEqual(len(errors), 1); self.assertIsInstance(errors[0], OperationCancelled)
                        self.assertEqual(len(captured), 1, "Cancelled request was retried")
                        release.set(); thread.join(2)
                    with transport(route) as (base, *_):
                        configure(provider, base)
                        self.assertEqual(lc.get_client().chat([dict(role="user", content="继续核验")]).content, "已保存的片段")

    def test_authentication_failure_does_not_retry_or_claim_success(self):
        for provider in ("gemini", "openai"):
            with transport(lambda p,b:(401, dict(error=dict(message="Synthetic bad credential", type="authentication_error", code="invalid_key")))) as (base, captured, *_):
                configure(provider, base)
                client = lc.get_client()
                result = client.chat([dict(role="user", content="科研分析")])
                self.assertFalse(result.content); self.assertIsNone(result.usage)
                self.assertEqual(len(captured), 1)
                ok, _ = client.test_connection()
                self.assertFalse(ok); self.assertEqual(len(captured), 2)

    def test_responses_vision_and_existing_tool_history_conversion(self):
        call = function()
        messages = [dict(role="user", content=[dict(type="text", text="Analyze axes"),
            dict(type="image_url", image_url=dict(url="data:image/png;base64,fixture", detail="low"))]),
            dict(role="assistant", content="", tool_calls=[call]),
            dict(role="tool", tool_call_id=call["id"], content='{"value":5}')]
        converted = _input_items(messages)
        self.assertEqual(converted[0]["content"][1]["type"], "input_image")
        self.assertEqual(converted[1]["call_id"], converted[2]["call_id"])
        self.assertEqual(converted[2]["type"], "function_call_output")
        self.assertEqual(normalize_usage(USAGE, "gemini").total_tokens, 120)


if __name__ == "__main__":
    unittest.main(verbosity=2)
