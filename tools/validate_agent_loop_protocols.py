"""Long tasks through real provider SDKs against loopback fixtures, no paid APIs."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import unittest

if not os.environ.get("PAPERPILOT_VALIDATION_ROOT"):
    raise SystemExit("Use tools/run_validation.py")

from paperpilot import library, llm_client
from paperpilot.ai_service import AIService
from paperpilot.agent_loop import create_task, TaskController, default_workspace
from paperpilot.agent_runtime import AgentRun
from paperpilot.config import save_config
from tools.validate_agent_loop import Actor, plan, running, done, finish
from tools.validate_llm_providers import transport, configure, completion, response, SIGNATURE


def validate_pairing(body):
    pending = set()
    if "input" in body:
        for item in body["input"]:
            if item.get("type") == "function_call":
                pending.add(item["call_id"])
            elif item.get("type") == "function_call_output":
                assert item["call_id"] in pending
                pending.remove(item["call_id"])
            elif item.get("role") and pending:
                raise AssertionError("Responses messages split native pairing")
    else:
        for message in body["messages"]:
            calls = message.get("tool_calls", [])
            if calls:
                assert not pending
                pending.update(c["id"] for c in calls)
            elif message["role"] == "tool":
                assert message["tool_call_id"] in pending
                pending.remove(message["tool_call_id"])
            elif body.get("system"):
                blocks = message.get("content", [])
                if isinstance(blocks, str):
                    assert not pending
                    continue
                for block in blocks:
                    if block["type"] == "tool_use":
                        assert not pending
                        pending.add(block["id"])
                    elif block["type"] == "tool_result":
                        assert block["tool_use_id"] in pending
                        pending.remove(block["tool_use_id"])
                    elif block["type"] == "text":
                        assert not pending
            else:
                assert not pending
    assert not pending


@contextmanager
def claude_transport(route):
    captured = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append(body)
            result = route(body)
            blocks = ([dict(type="tool_use", id=result.tool_calls[0]["id"],
                name=result.tool_calls[0]["function"]["name"],
                input=json.loads(result.tool_calls[0]["function"]["arguments"]))] if result.tool_calls else
                [dict(type="text", text=result.content)])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            def event(name, value):
                self.wfile.write(("event: " + name + "\ndata: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
            event("message_start", dict(type="message_start", message=dict(id="msg_loop", type="message",
                role="assistant", content=[], model=body["model"], stop_reason=None, stop_sequence=None,
                usage=dict(input_tokens=100, output_tokens=0))))
            for i, block in enumerate(blocks):
                start = dict(block, input={}) if block["type"] == "tool_use" else dict(type="text", text="")
                event("content_block_start", dict(type="content_block_start", index=i, content_block=start))
                delta = (dict(type="input_json_delta", partial_json=json.dumps(block["input"], ensure_ascii=False))
                         if block["type"] == "tool_use" else dict(type="text_delta", text=block["text"]))
                event("content_block_delta", dict(type="content_block_delta", index=i, delta=delta))
                event("content_block_stop", dict(type="content_block_stop", index=i))
            event("message_delta", dict(type="message_delta", delta=dict(
                stop_reason="tool_use" if result.tool_calls else "end_turn", stop_sequence=None), usage=dict(output_tokens=20)))
            event("message_stop", dict(type="message_stop"))
            self.close_connection = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", captured
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


class LoopProtocolTests(unittest.TestCase):
    def scenario(self, provider, *, interactive=False):
        project = library.create_project("协议长任务 " + provider, "scientific evidence")
        service = AIService()
        cm = service.get_conversation(project.id, project.name)
        root = default_workspace(cm)
        root.mkdir(parents=True, exist_ok=True)
        (root / "cohort.csv").write_bytes(b"cohort,n\ncontrol,30\ntreated,32\n")
        actor = Actor(cm, [plan("核对队列样本数"), running(), ("read_file", dict(path="cohort.csv")),
                           done("read_file"), finish("read_file", summary="队列样本为 30 与 32；仅核对文件记录。")])
        if interactive:
            actor.actions = [("request_user_input", dict(questions=[dict(id="scope", question="使用现有队列记录还是新增材料？",
                options=[dict(label="仅现有资料"), dict(label="新增材料")])])),
                ("read_file", dict(path="cohort.csv")), finish("read_file", summary="样本为 30 与 32；仅使用现有资料。")]
        def reply(body):
            validate_pairing(body)
            verifier = "你是独立验收审查者" in json.dumps(body, ensure_ascii=False)
            return actor([dict(role="system", content="独立验收审查者" if verifier else "main"),
                          dict(role="user", content="<runtime_task>")])
        return project, service, cm, root, actor, reply

    def run_task(self, project, service, cm, root, *, requests=6, estimated_requests=0):
        create_task(cm, project.id, "核对 cohort.csv 的样本数并说明范围", ["报告实际样本数"], root,
                    "read_only")
        run = AgentRun(cm, project.id, "核对样本数", "loop")
        controller = TaskController(service, cm, run, on_question=lambda identity, details, respond:
            respond(details["request_id"], {q["id"]: "仅现有资料" for q in details["questions"]}))
        try:
            controller.execute()
        finally:
            run.finish()
        self.assertEqual(controller.state["status"], "completed", controller.state["reason"])
        self.assertEqual(controller.state["used"]["requests"], requests)
        self.assertEqual(controller.state["used"].get("estimated_requests", 0), estimated_requests)
        self.assertTrue(controller.state["verification"]["passed"])
        return controller

    def test_api_auth_and_quota_failures_pause_instead_of_empty_response_loop(self):
        for status in (401, 402, 429):
            with self.subTest(status=status):
                with transport(lambda path, body: (status, dict(error=dict(message="fixture service unavailable", type="api_error")))) as (url, captured, *events):
                    configure("deepseek", url)
                    project, service, cm, root, actor, reply = self.scenario("api-error-" + str(status))
                    create_task(cm, project.id, "核对数量", ["实际数值"], root, "read_only")
                    run = AgentRun(cm, project.id, "核对数量", "loop")
                    c = TaskController(service, cm, run)
                    try:
                        c.execute()
                    finally:
                        run.finish()
                    self.assertEqual(c.state["status"], "paused")
                    self.assertIn("模型请求失败", c.state["reason"])
                    self.assertEqual(len(captured), 1)
                    self.assertEqual(c.state["used"]["requests"], 1)
                    self.assertEqual(len(actor.requests), 0)
                    self.assertFalse(c.state["evidence"])

    def test_transient_api_failure_retries_then_completes_without_budget(self):
        project, service, cm, root, actor, reply = self.scenario("transient")
        attempts = []
        def route(path, body):
            attempts.append(path)
            if len(attempts) == 1:
                return 500, dict(error=dict(message="fixture temporary service error", type="api_error"))
            result = reply(body)
            return 200, completion(body["model"], result.content, result.tool_calls[0] if result.tool_calls else None)
        with transport(route) as (url, captured, *events):
            configure("deepseek", url)
            c = self.run_task(project, service, cm, root, requests=7, estimated_requests=1)
            self.assertTrue(all(value is None for value in c.state["limits"].values()))
            self.assertEqual(len(captured), 7)

    def test_seven_compatible_routes_native_tools_budget_and_verifier(self):
        for provider in ("deepseek", "openai", "gemini", "glm", "kimi", "qwen", "ollama"):
            with self.subTest(provider=provider):
                project, service, cm, root, actor, reply = self.scenario(provider, interactive=True)
                def route(path, body):
                    result = reply(body)
                    call = result.tool_calls[0] if result.tool_calls else None
                    if provider == "gemini" and call:
                        call["extra_content"] = SIGNATURE
                    return 200, (response(body["model"], result.content, call) if path.endswith("/responses") else
                                 completion(body["model"], result.content, call))
                with transport(route) as (base, captured, *_):
                    configure(provider, base)
                    save_config({"agent": {"team": {"enabled": False}}})
                    c = self.run_task(project, service, cm, root, requests=4)
                    self.assertEqual(len(captured), 4)
                    self.assertEqual(c.state["plan"], [])
                    self.assertEqual(c.state["user_answers"][0]["answers"], {"scope": "仅现有资料"})
                    self.assertTrue(any("read_file" in json.dumps(body) for _, body in captured))
                    self.assertFalse(captured[-1][1].get("tools"), "Verifier must not inherit tools")
                    if provider == "gemini":
                        self.assertIn("opaque-fixture-signature", json.dumps(captured))

    def test_claude_native_tool_result_pairs_and_verification(self):
        project, service, cm, root, actor, reply = self.scenario("anthropic", interactive=True)
        with claude_transport(reply) as (base, captured):
            configure("anthropic", base)
            save_config({"agent": {"team": {"enabled": False}}})
            c = self.run_task(project, service, cm, root, requests=4)
            self.assertEqual(len(captured), 4)
            self.assertEqual(c.state["user_answers"][0]["answers"], {"scope": "仅现有资料"})
            self.assertIn("tool_result", json.dumps(captured))
            self.assertFalse(captured[-1].get("tools"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
