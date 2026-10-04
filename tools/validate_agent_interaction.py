"""Human-facing Agent business cases: real journal/files, scripted model only."""
import copy
import json
import threading
import time
import unittest
import uuid

from paperpilot.agent_loop import saved_task, TaskController
from paperpilot.agent_runtime import AgentRun
from paperpilot.agent_presentation import user_text, prose_newlines
from paperpilot.conversation import pending_tool_calls, ConversationManager
from paperpilot.llm_client import ChatResult
import tools.validate_agent_loop as fixtures
from tools.validate_agent_loop import plan, running, done, finish


def question(text="报告使用什么单位？", key="units", options=None):
    value = dict(id=key, question=text)
    if options:
        value["options"] = [dict(label=label, description=description) for label, description in options]
    return "request_user_input", dict(questions=[value])


class InteractionBusinessTests(unittest.TestCase):
    # Reuse setup, not the inherited previous suite or its test count.
    setUp, tearDown = fixtures.LoopBusinessTests.setUp, fixtures.LoopBusinessTests.tearDown
    controller, execute = fixtures.LoopBusinessTests.controller, fixtures.LoopBusinessTests.execute

    def test_capability_question_has_no_demo_file_or_mandatory_plan(self):
        c, _ = self.controller([("list_files", {}), finish("list_files", summary="当前为只读模式，可读取课题工作区的文本文件。")],
            mode="read_only", objective="你能读取本地文件吗？")
        reply = self.execute(c)
        self.assertEqual(c.state["status"], "completed")
        self.assertEqual(c.state["plan"], [])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(reply, "当前为只读模式，可读取课题工作区的文本文件。")

    def test_direct_edit_without_plan_preserves_literal_file_content(self):
        content = '# 光学记录\n路径 C:\\new\\notes.txt\n正则 r"\\n"\n'
        c, _ = self.controller([("write_file", dict(path="光学.md", content=content, reason="保存这份光学记录，便于复核。")),
            ("read_file", dict(path="光学.md")), finish("read_file", summary="已保存并重新读取光学记录。")])
        self.execute(c)
        self.assertEqual(c.state["status"], "completed")
        self.assertEqual((self.root / "光学.md").read_text(encoding="utf-8"), content)

    def test_explore_then_revise_cancelled_item_without_lowering_criteria(self):
        c, _ = self.controller([("list_files", {}), plan("核对洪涝观测资料"), running(),
            ("calculate", dict(expression="30/32")), done("calculate"),
            ("set_plan", dict(steps=[dict(id="work", title="核对洪涝观测资料", depends_on=[]),
                dict(id="extras", title="补充不存在的传感器资料", depends_on=["work"])])),
            ("update_step", dict(id="extras", status="cancelled", evidence_ids=[])),
            finish("calculate", summary="已核对现有比例；没有补充传感器资料，不推断空间外推能力。")])
        self.execute(c)
        self.assertEqual(c.state["status"], "completed")
        self.assertEqual([s["status"] for s in c.state["plan"]], ["done", "cancelled"])
        self.assertEqual(c.state["criteria"], ["整理可核对的结果"])

    def test_approval_has_plain_reason_and_still_freezes_exact_edit(self):
        seen = []
        def approve(identity, details, respond):
            seen.append(copy.deepcopy(details))
            self.assertEqual(details["preview"]["reason"], "将队列样本范围写入报告，方便之后复核。")
            self.assertTrue(respond(details["request_id"], True))
            self.assertFalse(respond(details["request_id"], True))
        c, _ = self.controller([("write_file", dict(path="cohort.md", content="观察结果，不能证明因果。",
            reason="将队列样本范围写入报告，方便之后复核。")), finish("write_file")], mode="ask_edit", approval=approve)
        self.execute(c)
        self.assertEqual(c.state["status"], "completed")
        self.assertEqual(len(seen), 1)
        self.assertEqual((self.root / "cohort.md").read_text(encoding="utf-8"), "观察结果，不能证明因果。")
        record = next(e for e in c.state["evidence"] if e["tool"] == "write_file")
        observed = json.loads(c._evidence_data(record)["content"])
        self.assertEqual(observed["reason"], seen[0]["preview"]["reason"])
        self.assertEqual(observed["authorization"]["request_id"], seen[0]["request_id"])
        self.assertEqual(observed["authorization"]["decision"], "approved_once")

    def test_old_file_evidence_gives_current_refs_without_accepting_stale_version(self):
        c, actor = self.controller([
            ("write_file", dict(path="report.md", content="初稿", reason="保存初稿。")),
            ("read_file", dict(path="report.md")),
            ("edit_file", dict(path="report.md", old_text="初稿", new_text="复核稿", reason="修正复核结果。")),
            ("read_file", dict(path="report.md")),
            finish("write_file"), finish("edit_file")])
        self.execute(c)
        self.assertEqual(c.state["status"], "completed")
        self.assertEqual(actor.verifications, 1)
        failure = next(e for e in c.state["evidence"] if e["tool"] == "finish_task" and not e["ok"])
        message = json.loads(c._evidence_data(failure)["content"])["error"]
        current = next(e for e in c.state["evidence"] if e["tool"] == "edit_file")
        self.assertIn(current["id"], message)
        payload = next(m for m in actor.requests if "独立验收审查者" in m[0]["content"])[1]["content"]
        self.assertIn("修正复核结果", payload)
        self.assertIn("allowed_by_selected_mode", payload)

    def test_question_answer_is_human_persisted_and_reaches_verifier(self):
        c, actor = self.controller([question(options=[("kPa", "适合实验压力记录"), ("Pa", "保留基本单位")]),
            ("calculate", dict(expression="2500/1000")), finish("calculate", summary="2500 Pa 等于 2.5 kPa。")], mode="read_only")
        def answer(identity, details, respond):
            self.assertTrue(pending_tool_calls(self.cm._messages))
            self.assertTrue(respond(details["request_id"], {"units": "kPa\n保留一位小数"}))
            self.assertFalse(respond(details["request_id"], {"units": "Pa"}))
        c.on_question = answer
        self.execute(c)
        self.assertEqual(c.state["status"], "completed")
        self.assertEqual(c.state["mode"], "read_only")
        self.assertEqual(c.state["question"]["status"], "answered")
        self.assertEqual(c.state["user_answers"][0]["answers"]["units"], "kPa\n保留一位小数")
        self.assertFalse(pending_tool_calls(self.cm._messages))
        verifier = next(m for m in actor.requests if "独立验收审查者" in m[0]["content"])
        self.assertIn("保留一位小数", verifier[1]["content"])
        restored = ConversationManager(self.project.name, session_id=self.cm.session_id, storage_path=self.cm._path)
        self.assertEqual(saved_task(restored)["user_answers"], c.state["user_answers"])

    def test_confirmed_restart_retains_answer_committed_before_history_flush(self):
        self._crashed_answer_recovery(history_written=False)

    def test_confirmed_restart_does_not_duplicate_already_written_human_reply(self):
        self._crashed_answer_recovery(history_written=True)

    def _crashed_answer_recovery(self, *, history_written):
        original, _ = self.controller([])
        details = dict(request_id=uuid.uuid4().hex, task_id=original.state["task_id"],
            run_id=original.run.id, revision=1, status="answered",
            questions=[dict(id="scope", question="用哪部分材料？")], answers={"scope": "仅现有摘要"})
        state = self.cm.get_task_state()
        state.update(status="waiting_input", question=details)
        self.cm.set_task_state(state)
        content = "[用户对本次问询的明确回复；请求 " + details["request_id"] + "]\n用哪部分材料？\n仅现有摘要"
        if history_written:
            self.cm.add_user_message(content, display_content="用哪部分材料？\n仅现有摘要")
        original.run.finish()
        self.assertEqual(saved_task(self.cm, recover=True)["status"], "paused")
        resumed, actor = self.controller([("calculate", dict(expression="2+2")), finish("calculate")], resume=True)
        resumed.on_question = lambda *args: self.fail("已明确回答的问题不应重新提问")
        self.execute(resumed)
        self.assertEqual(resumed.state["status"], "completed")
        self.assertEqual(resumed.state["user_answers"][0]["answers"], {"scope": "仅现有摘要"})
        self.assertEqual(sum(m["role"] == "user" and m["content"] == content for m in self.cm._history), 1)
        self.assertIn("仅现有摘要", actor.requests[0][-1]["content"])

    def test_multiple_questions_reject_missing_blank_unknown_and_old_answers(self):
        c, _ = self.controller([("request_user_input", dict(questions=[
            dict(id="topic", question="Which topic?"), dict(id="scope", question="使用哪些资料？")])),
            ("calculate", dict(expression="2+3")), finish("calculate")])
        def answer(identity, details, respond):
            for answers in ({}, {"topic": "Photonics"}, {"topic": "", "scope": "摘要"},
                            {"topic": "Photonics", "scope": "摘要", "x": "extra"}):
                self.assertFalse(respond(details["request_id"], answers))
            self.assertFalse(respond(uuid.uuid4().hex, {"topic": "Photonics", "scope": "摘要"}))
            self.assertTrue(respond(details["request_id"], {"topic": "Photonics", "scope": "仅现有摘要"}))
        c.on_question = answer
        self.execute(c)
        self.assertEqual(c.state["status"], "completed")

    def test_waiting_has_no_timer_default_answer_or_extra_model_request(self):
        c, actor = self.controller([question(), ("calculate", dict(expression="1+1")), finish("calculate")])
        pending, errors = threading.Event(), []
        c.on_question = lambda *args: pending.set()
        def run():
            try:
                self.execute(c)
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(pending.wait(5))
        requests = len(actor.requests)
        time.sleep(.25)
        self.assertEqual(len(actor.requests), requests)
        self.assertEqual(c.state["status"], "waiting_input")
        self.assertIsNone(c.question.answers)
        details = c.question.details
        self.assertTrue(c.respond_question(details["request_id"], {"units": "Pa"}))
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertEqual(c.state["status"], "completed")

    def test_defer_then_confirm_resume_asks_fresh_question_without_api_round(self):
        old = []
        c, _ = self.controller([question()])
        def defer(identity, details, respond):
            old.append(details["request_id"])
            self.assertTrue(respond(details["request_id"], None))
        c.on_question = defer
        self.execute(c)
        self.assertEqual(c.state["status"], "paused")
        self.assertEqual(c.state["question"]["status"], "deferred")
        resumed, actor = self.controller([("calculate", dict(expression="5+3")), finish("calculate")], resume=True)
        def answer(identity, details, respond):
            self.assertEqual(actor.requests, [])
            self.assertNotEqual(details["request_id"], old[0])
            self.assertFalse(respond(old[0], {"units": "Pa"}))
            self.assertTrue(respond(details["request_id"], {"units": "Pa"}))
        resumed.on_question = answer
        self.execute(resumed)
        self.assertEqual(resumed.state["status"], "completed")
        self.assertFalse(pending_tool_calls(self.cm._messages))

    def test_cancel_waiting_and_resume_retains_question_and_native_pairs(self):
        c, _ = self.controller([question()])
        c.on_question = lambda *args: c.run.token.cancel()
        self.execute(c)
        self.assertEqual(c.state["status"], "paused")
        self.assertEqual(c.state["question"]["status"], "waiting")
        self.assertFalse(pending_tool_calls(self.cm._messages))
        resumed, _ = self.controller([("calculate", dict(expression="5+3")), finish("calculate")], resume=True)
        resumed.on_question = lambda identity, details, respond: respond(details["request_id"], {"units": "Pa"})
        self.execute(resumed)
        self.assertEqual(resumed.state["status"], "completed")

    def test_extra_instruction_withdraws_old_question_without_granting_edit(self):
        c, _ = self.controller([question(), ("calculate", dict(expression="1+1")), finish("calculate")], mode="read_only")
        def steer(identity, details, respond):
            c.enqueue_instruction("不必继续问单位，只需要核对当前计算。")
            self.assertFalse(respond(details["request_id"], {"units": "direct_edit"}))
        c.on_question = steer
        self.execute(c)
        self.assertEqual(c.state["status"], "completed")
        self.assertEqual(c.state["question"]["status"], "withdrawn")
        self.assertEqual(c.state["mode"], "read_only")
        self.assertIn("不必继续问单位", c.state["constraints"][0])

    def test_human_answer_survives_immediate_stop(self):
        c, _ = self.controller([question()])
        def answer_and_stop(identity, details, respond):
            self.assertTrue(respond(details["request_id"], {"units": "Pa"}))
            c.run.token.cancel()
        c.on_question = answer_and_stop
        self.execute(c)
        self.assertEqual(c.state["status"], "paused")
        self.assertEqual(c.state["user_answers"][0]["answers"], {"units": "Pa"})
        self.assertFalse(pending_tool_calls(self.cm._messages))

    def test_visible_progress_is_model_prose_checkpoints_remain_hidden(self):
        raw = "已读取资料。\\n\\n接下来核对数值。"
        c, _ = self.controller([ChatResult(content=raw, finish_reason="stop"),
            ("calculate", dict(expression="1+1")), finish("calculate", summary="数值核对完成。")])
        seen = []
        c.on_message = lambda identity, text, role: seen.append((text, role))
        self.execute(c)
        self.assertIn(("已读取资料。\n\n接下来核对数值。", "agent"), seen)
        visible = [m for m in self.cm._history if not m.get("internal")]
        self.assertTrue(any(m["content"] == raw for m in visible))
        self.assertFalse(any("[任务进度]" in m["content"] for m in visible))
        self.assertTrue(any(m.get("internal") and m.get("loop_checkpoint") for m in self.cm._history))

    def test_text_projection_preserves_paths_code_identifiers_and_originals(self):
        key = "a" * 32
        raw = "已核对数据。\\n\\n下一步见证据 " + key + "。\n`C:\\new\\notes.txt`\n```python\nx = r'\\n'\n```"
        projected = user_text(raw, dict(evidence=[dict(id=key, tool="read_file", path="measurements.csv")]))
        self.assertIn("数据。\n\n下一步", projected)
        self.assertIn("measurements.csv", projected)
        self.assertNotIn(key, projected)
        self.assertIn("`C:\\new\\notes.txt`", projected)
        self.assertIn("x = r'\\n'", projected)
        self.assertIn(key, raw)
        literal = 'Unclosed code:\n```python\nx = "文字。\\n下一行"'
        self.assertEqual(prose_newlines(literal), literal)
        self.assertEqual(user_text("SHA256 " + key), "SHA256 " + key)

    def test_question_and_write_cannot_be_smuggled_in_same_batch(self):
        calls = [dict(id=uuid.uuid4().hex, type="function", function=dict(name=name,
            arguments=json.dumps(args, ensure_ascii=False))) for name, args in
            [question(), ("write_file", dict(path="smuggled.md", content="forbidden"))]]
        c, _ = self.controller([ChatResult(tool_calls=calls, finish_reason="tool_calls"),
            ("calculate", dict(expression="1+1")), finish("calculate")])
        self.execute(c)
        self.assertFalse((self.root / "smuggled.md").exists())
        self.assertEqual(c.state["status"], "completed")

    def test_corrupt_question_checkpoint_is_preserved_and_cannot_resume(self):
        c, _ = self.controller([])
        state = saved_task(self.cm)
        state["question"] = dict(request_id="bad", status="waiting", questions=[])
        self.cm.set_task_state(state)
        with self.assertRaises(ValueError):
            saved_task(self.cm, recover=True)
        self.assertEqual(self.cm.get_task_state()["question"]["request_id"], "bad")
        c.run.finish()


if __name__ == "__main__":
    unittest.main(verbosity=2)
