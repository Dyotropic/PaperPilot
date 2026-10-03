"""Business and boundary validation over real stdio subprocesses, isolated runner.

The fixture verifies protocol integration, never subscription entitlement/quality.
One separate native-CLI test verifies signed-out protocol and sandbox contracts.
"""
import copy
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

if not os.environ.get("PAPERPILOT_VALIDATION_ROOT"):
    raise SystemExit("Run via tools/run_validation.py")

from paperpilot.config import save_config, load_config, BASE_DIR
from paperpilot.codex_transport import get_server, AppServer, close_servers, CodexError
from paperpilot.codex_client import CodexSubscriptionClient
from paperpilot.llm_client import get_client, llm_configured, tools_scope, MODEL_CATALOG, MODEL_CAPABILITIES
from paperpilot.llm_usage import usage_scope, UsageStore
from paperpilot.agent_runtime import CancellationToken, OperationCancelled, run_scope
from paperpilot.agent_team import CALCULATE_TOOL

ROOT = Path(os.environ["PAPERPILOT_VALIDATION_ROOT"])
FIXTURE = BASE_DIR / "tools" / "codex_fixture_server.py"
original_popen = subprocess.Popen


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        close_servers()
        self.home = ROOT / "profiles" / self._testMethodName
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "auth.json").write_text('{"kind":"chatgpt"}')
        self.launched = []
        def popen(command, **kwargs):
            if command[0] == str(Path(sys.executable).resolve()) and command[1] == "app-server":
                self.launched.append((command, kwargs["env"]))
                return original_popen([sys.executable, "-B", str(FIXTURE)], **kwargs)
            return original_popen(command, **kwargs)
        self.patcher = patch("subprocess.Popen", side_effect=popen)
        self.patcher.start()
        save_config(dict(llm=dict(provider="codex",model="codex-default",codex_cli=sys.executable,
            codex_home=str(self.home),api_key="",score_model="",chat_model="",reasoning_model="")))
        self.client = get_client()

    def tearDown(self):
        close_servers()
        self.patcher.stop()

    def messages(self, text="Research question", system="Scientific research"):
        return [dict(role="system",content=system),dict(role="user",content=text)]

    def calls(self):
        path = self.home / "fixture.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def invoke(self, text="Research question", **kwargs):
        return self.client._do_chat(self.messages(text), .3, 2000, kwargs.get("timeout", 5), "codex-default", False)

    def token_scope(self, token):
        return run_scope(SimpleNamespace(id="run-fixture",token=token))

    def test_scientific_workflows_and_history_chinese_english(self):
        from paperpilot.ai_service import AIService
        from paperpilot import library
        service = AIService()
        for topic in ("机器人导航误差与样本数", "Cancer immunotherapy: limitations", "钙钛矿器件稳定性"):
            project = library.create_project(topic, topic)
            paper = dict(title=topic,abstract="Provided fixture measurements and uncertainty. " * 3,year=2026)
            self.assertEqual(service.deep_read(paper, full_text=paper["abstract"])["core_contribution"], "仅分析所提供资料")
            self.assertTrue(service.score_papers(topic, [paper]))
            self.assertIn("研究回答", service.chat(project.id, project.name, "解释研究边界", topic_desc=topic)["reply"])
            self.assertIn("研究回答", service.chat(project.id, project.name, "Compare the methods", topic_desc=topic)["reply"])
            self.assertEqual(service.get_conversation(project.id, project.name).total_rounds, 2)
        injected = [m for m in self.calls() if m.get("method") == "thread/inject_items"]
        self.assertTrue(any(any(i.get("role") == "assistant" for i in m["params"]["items"]) for m in injected))
        self.assertFalse(self.client.server.states)

    def test_keywords_translation_and_task_model_overrides(self):
        from paperpilot.core_extractor import extract_core_keywords, extract_regular_keywords
        from paperpilot.mt_translator import translate_all_terms, _cache
        for topic in ("钙钛矿界面钝化", "Interface passivation in perovskite cells"):
            self.assertEqual(extract_core_keywords(topic), ["钙钛矿", "界面钝化"])
            self.assertEqual(len(extract_regular_keywords(topic)), 5)
        _cache.clear()
        self.assertEqual(translate_all_terms(["钙钛矿","CAR-T"], ["界面钝化"], [""]),
                         [["perovskite","CAR-T"],["interface passivation"],[""]])
        save_config(dict(llm=dict(chat_model="fixture-research")))
        self.assertEqual(get_client("chat").model, "fixture-research")
        self.assertEqual(self.client.model, "codex-default")

    def test_real_agent_dispatcher_calculation_and_parallel_team_reads(self):
        from paperpilot.ai_service import AIService
        from paperpilot import library
        from paperpilot.agent_runtime import AgentRun
        for message in ("TOOL 计算 sqrt(9+16)", "TEAM_SCENARIO 比较研究方法与证据"):
            project = library.create_project(message, "研究依据、误差与边界")
            service = AIService()
            cm = service.get_conversation(project.id, project.name)
            run = AgentRun(cm, project.id, message)
            with run_scope(run):
                answer = service.chat(project.id, project.name, message)
            run.finish()
            self.assertEqual(cm.total_rounds, 1)
            self.assertIn("计算结果是 5" if message.startswith("TOOL") else "已审查", answer["reply"])
            if message.startswith("TEAM"):
                team_file = next((cm.storage_directory / "teams").glob("*/team.json"))
                team = json.loads(team_file.read_text(encoding="utf-8"))
                self.assertEqual(len(team["agents"]), 2)
                self.assertTrue(all(a["state"] == "completed" and a["sources_read"] for a in team["agents"]))
        self.assertFalse(self.client.server.states)

    def test_two_step_tool_review_and_compaction_keep_public_history(self):
        from paperpilot.ai_service import AIService
        from paperpilot import library
        from paperpilot.agent_runtime import AgentRun
        save_config(dict(llm=dict(reasoning_model="fixture-research")))
        project = library.create_project("两步推理与检查点", "误差与研究边界")
        service = AIService(); cm = service.get_conversation(project.id,project.name)
        run = AgentRun(cm,project.id,"TOOL 计算 sqrt(9+16)")
        with run_scope(run):
            answer = service.chat(project.id,project.name,run.goal)
        run.finish()
        self.assertIn("计算结果是 5",answer["reply"])
        for index in range(2):
            service.chat(project.id,project.name,"讨论研究结果及局限。" * 180)
        before = copy.deepcopy(cm.display_messages)
        result = service.compact_context(project.id,project.name)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(cm.compressed_summaries)
        self.assertEqual(cm.display_messages, before)
        self.assertIn("研究回答", service.chat(project.id,project.name,"继续比较证据")["reply"])
        self.assertFalse(self.client.server.states)

    def test_login_cancel_refresh_logout_and_key_rejection(self):
        server = self.client.server
        self.assertTrue(llm_configured())
        server.logout()
        self.assertFalse(llm_configured())
        result = server.login()
        self.assertTrue(result["authUrl"].startswith("https://auth.openai.com/"))
        server.cancel_login()
        self.assertIsNone(server.login_id)
        server.login(); server.rpc("fixture/login/complete")
        self.assertEqual(server.account(True)["type"], "chatgpt")
        self.assertEqual(server.models(True)[0]["model"], "fixture-research")
        server.close()
        (self.home / "auth.json").write_text('{"kind":"apiKey"}')
        with self.assertRaises(CodexError): server.require_subscription()
        self.assertFalse(llm_configured())

    def test_auth_cache_copy_and_rollback_no_original_changes(self):
        source = ROOT / "login-source"; source.mkdir(exist_ok=True)
        auth = source / "auth.json"
        auth.write_text('{"kind":"apiKey"}')
        with self.assertRaises(CodexError): self.client.server.import_login(auth)
        self.assertEqual(json.loads((self.home / "auth.json").read_text())["kind"], "chatgpt")
        auth.write_text('{"kind":"chatgpt"}')
        before = auth.read_bytes()
        self.assertEqual(self.client.server.import_login(auth)["type"], "chatgpt")
        self.client.server.logout()
        self.assertEqual(auth.read_bytes(), before)
        with self.assertRaises(CodexError): self.client.server.import_login(source / "missing.json")

    def test_native_tool_handoff_usage_and_observation_images(self):
        messages = self.messages("TOOL calculate")
        with usage_scope(project_id=21,session_id="tools",operation="task"), tools_scope([CALCULATE_TOOL],"auto"):
            result = self.client.chat(messages, timeout=5)
            self.assertEqual(result.tool_calls[0]["function"]["name"], "calculate")
            self.assertEqual(result.usage.total_tokens, 72)
            messages += [dict(role="assistant",content=result.content,tool_calls=result.tool_calls,
                provider_blocks=result.provider_blocks), dict(role="tool",tool_call_id=result.tool_calls[0]["id"],content="5"),
                dict(role="user",content=[dict(type="text",text="Image context"),
                    dict(type="image_url",image_url=dict(url="data:image/png;base64,fixture"))])]
            result = get_client().chat(messages, timeout=5)
            self.assertEqual(result.content, "计算结果是 5。")
            self.assertEqual(result.usage.total_tokens, 48)
        calls = self.calls()
        self.assertEqual(sum(m.get("method") == "turn/start" for m in calls), 1)
        reply = next(m for m in calls if isinstance(m.get("id"),str) and "result" in m)
        self.assertEqual(reply["result"]["contentItems"][-1]["type"], "inputImage")
        self.assertEqual(sum(r["total_tokens"] for r in UsageStore().records(21,"tools")), 120)
        self.assertFalse(self.client.server.states)

    def test_tool_session_ownership_and_restart_reconstruction(self):
        messages = self.messages("TOOL")
        with usage_scope(project_id=31,session_id="first"), tools_scope([CALCULATE_TOOL],"auto"):
            result = self.client.chat(messages, timeout=5)
        messages += [dict(role="assistant",content=result.content,tool_calls=result.tool_calls,provider_blocks=result.provider_blocks),
                     dict(role="tool",tool_call_id=result.tool_calls[0]["id"],content="5")]
        with usage_scope(project_id=32,session_id="second"), tools_scope([CALCULATE_TOOL],"auto"):
            with self.assertRaises(CodexError): self.invoke_resume(messages)
        self.assertTrue(self.client.server.states)
        close_servers()
        with usage_scope(project_id=31,session_id="first"), tools_scope([CALCULATE_TOOL],"auto"):
            self.assertIn("研究回答",self.invoke_resume(messages).content)
        injected = [m for m in self.calls() if m.get("method") == "thread/inject_items"][-1]
        self.assertIn("function_call_output", {i["type"] for i in injected["params"]["items"]})

    def invoke_resume(self, messages):
        return self.client._do_chat(messages,.3,2000,5,"codex-default",False)

    def test_cancel_before_ack_stream_and_pending_tool(self):
        for text, system in (("Question", "DELAY_ACK"),("DELAY_TURN","Scientific"),("SLOW_STREAM","Scientific"),("TOOL","Scientific")):
            with self.subTest(text=text,system=system):
                token = CancellationToken(); caught = []
                messages = self.messages(text,system)
                def run():
                    try:
                        with self.token_scope(token), usage_scope(session_id="cancel"), tools_scope([CALCULATE_TOOL],"auto"):
                            result = self.client._do_cancellable(messages,.3,2000,5,"codex-default",False,token)
                            if result.tool_calls:
                                token.cancel()
                    except OperationCancelled: caught.append(True)
                worker = threading.Thread(target=run); worker.start()
                time.sleep(.15); token.cancel(); worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertTrue(caught or text == "TOOL")
                time.sleep(.6)
                self.assertFalse(self.client.server.states)
        self.assertTrue(any(m.get("method") == "turn/interrupt" for m in self.calls()))
        self.assertIn("研究回答", self.invoke("Recovery").content)

    def test_cancel_during_initialize_and_model_discovery(self):
        for flag in ("delay-init", "delay-model"):
            close_servers()
            (self.home/flag).touch()
            token = CancellationToken(); canceled = []
            def run():
                try:
                    self.client._do_cancellable(self.messages(),.3,2000,5,"codex-default",False,token)
                except OperationCancelled: canceled.append(True)
            worker = threading.Thread(target=run); worker.start()
            time.sleep(.1); token.cancel(); worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertTrue(canceled)
            self.assertFalse(self.client.server.states)
            (self.home/flag).unlink()
        self.assertFalse(any(m.get("method") == "turn/start" for m in self.calls()))

    def test_checkpoint_does_not_consume_pending_tools_and_none_withdraws_them(self):
        messages = self.messages("TOOL")
        with usage_scope(project_id=39,session_id="checkpoint",task="chat"), tools_scope([CALCULATE_TOOL],"auto"):
            result = self.client.chat(messages, timeout=5)
            messages += [dict(role="assistant",content=result.content,tool_calls=result.tool_calls,provider_blocks=result.provider_blocks),
                         dict(role="tool",tool_call_id=result.tool_calls[0]["id"],content="5")]
            pending = set(self.client.server.states)
            with usage_scope(task="compression"), tools_scope():
                summary = self.invoke_resume(messages+[dict(role="user",content="生成科研工作流上下文检查点")])
                self.assertFalse(summary.tool_calls)
            self.assertEqual(set(self.client.server.states), pending)
            with tools_scope([CALCULATE_TOOL],"none"):
                final = self.invoke_resume(messages)
                self.assertTrue(final.content)
                self.assertFalse(final.tool_calls)
            self.assertFalse(self.client.server.states)

    def test_parallel_threads_and_permission_isolation(self):
        results, errors = [], []
        def run(index):
            try:
                with usage_scope(project_id=index,session_id=f"parallel-{index}"):
                    results.append(self.invoke(f"Research {index}"))
            except BaseException as exc: errors.append(exc)
        workers = [threading.Thread(target=run,args=(i,)) for i in range(4)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(5)
        self.assertFalse(errors)
        self.assertEqual(len({r.request_id for r in results}), 4)
        with tools_scope([CALCULATE_TOOL],"auto"):
            with self.assertRaises(CodexError): self.invoke("UNAUTHORIZED")
        self.assertFalse(self.client.server.states)
        for message in self.calls():
            if message.get("method") == "thread/start":
                params = message["params"]
                self.assertEqual(params["environments"], [])
                self.assertEqual(params["sandbox"], "read-only")
                self.assertEqual(params["approvalPolicy"], "never")
                self.assertTrue(params["ephemeral"])
        argv, env = self.launched[0]
        self.assertNotIn("OPENAI_API_KEY",env)
        self.assertIn("features.shell_tool=false",argv)
        self.assertIn('forced_login_method="chatgpt"',argv)

    def test_usage_capabilities_images_empty_input_and_stream_close(self):
        from paperpilot.agent_attachments import ensure_image_support
        from paperpilot.context_budget import context_policy
        self.assertTrue(self.client.is_available)
        ensure_image_support("codex","codex-default")
        result = self.invoke("Research")
        self.assertEqual((result.usage.input_tokens,result.usage.cache_hit_tokens,result.usage.reasoning_tokens), (100,60,5))
        self.assertEqual(context_policy().window, 128000)
        with self.assertRaises(ValueError): self.client._do_chat([],.3,2000,5,"codex-default",None)
        image_message = [dict(role="user",content=[dict(type="image_url",image_url=dict(url="data:image/png;base64,fixture"))])]
        self.assertTrue(self.invoke_resume(image_message).content)
        stream = self.client.chat_stream(self.messages("SLOW_STREAM"),timeout=5)
        self.assertTrue(next(stream)); stream.close()
        self.assertFalse(self.client.server.states)

    def test_model_failure_timeout_disconnect_and_no_automatic_retry(self):
        with self.assertRaises(CodexError): self.client._do_chat(self.messages(),.3,2000,5,"unavailable-model",False)
        self.assertFalse(any(m.get("method") == "turn/start" for m in self.calls()))
        for text in ("FAIL", "TIMEOUT", "DISCONNECT"):
            with self.subTest(text=text):
                with self.assertRaises(CodexError): self.invoke(text,timeout=.2)
                self.assertFalse(self.client.server.states)
        self.assertIn("研究回答", self.invoke("Recovery").content)

    def test_real_installed_cli_signedout_protocol_and_sandbox(self):
        executable = shutil.which("codex")
        if not executable: self.skipTest("Official Codex CLI absent")
        server = AppServer(executable, ROOT / "native-profile")
        try:
            self.assertIsNone(server.account())
            result = server.rpc("thread/start",dict(model="gpt-6.1-sol",cwd=str(server.home/"work"),
                approvalPolicy="never",sandbox="read-only",environments=[],runtimeWorkspaceRoots=[],ephemeral=True,
                dynamicTools=[dict(type="namespace",name="paperpilot",description="Read-only scientific tools",
                    tools=[dict(type="function",name="calculate",description="Calculate",inputSchema=dict(type="object",properties={}))])]))
            self.assertEqual(result["sandbox"],dict(type="readOnly",networkAccess=False))
            tid = result["thread"]["id"]
            server.rpc("thread/inject_items",dict(threadId=tid,items=[dict(type="message",role="user",
                content=[dict(type="input_text",text="Earlier scientific question")]),
                dict(type="message",role="assistant",content=[dict(type="output_text",text="Earlier scientific answer")])]))
            server.rpc("thread/unsubscribe",dict(threadId=tid))
        finally: server.close()


if __name__ == "__main__": unittest.main(verbosity=2)
