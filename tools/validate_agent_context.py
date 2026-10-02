"""Context business flows with isolated synthetic sessions and actual client accounting.

Run: python -B tools/run_validation.py tools/validate_agent_context.py
No external provider calls. Covers compaction safety, journal replay, continuation,
model capacity and current occupancy separately from cumulative billing.
"""
import asyncio
import copy
import threading
import unittest
import uuid
from unittest.mock import patch

from paperpilot import library
from paperpilot.ai_service import AIService
from paperpilot.agent_runtime import AgentRun, OperationCancelled, run_scope
from paperpilot.context_budget import context_policy, context_status, estimate_request_tokens
from paperpilot.llm_client import LLMClient, ChatResult
from paperpilot.llm_usage import TokenUsage, UsageStore


class Client(LLMClient):
    def __init__(self):
        super().__init__("deepseek-flash")
        self.provider = "deepseek"
        self.requests = []
        self.summary = "## 用户目标与需求\n保留研究任务\n## 研究依据与引用\nDOI:10.fixture/example；参数 0.123456789 m。"
        self.finish = "stop"
        self.entered = threading.Event()
        self.block = False

    def _do_chat(self, messages, *args):
        self.requests.append(copy.deepcopy(messages))
        return ChatResult(content=self.summary if "现在生成科研工作流" in messages[-1]["content"] else "Synthetic reply",
                          finish_reason=self.finish, usage=TokenUsage(estimate_request_tokens(messages) // 2, 20))

    def _do_cancellable(self, messages, temperature, max_tokens, timeout, model, thinking, token):
        if self.block:
            async def waiting():
                self.entered.set()
                await asyncio.sleep(30)
            return token.run_async(waiting)
        return self._do_chat(messages)


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.project = library.create_project("上下文验收 " + uuid.uuid4().hex[:8], "Synthetic topic")
        self.service = AIService()
        self.cm = self.service.get_conversation(self.project.id, self.project.name, self.project.description)
        self.client = Client()
        self.patch_client = patch("paperpilot.ai_service.get_client", return_value=self.client)
        self.patch_client.start()
        self.addCleanup(self.patch_client.stop)
        self.model = patch("paperpilot.ai_service.get_task_model", return_value="deepseek-flash")
        self.model.start()
        self.addCleanup(self.model.stop)
        self.reasoning = patch("paperpilot.ai_service.get_task_model_override", return_value="")
        self.reasoning.start()
        self.addCleanup(self.reasoning.stop)

    def populate(self, rounds=6):
        for i in range(rounds):
            self.cm.add_user_message(f"Research {i}. " + "Synthetic measured evidence. " * 250 +
                                     "DOI:10.fixture/example; 0.123456789 m.")
            self.cm.add_assistant_message(f"Decision {i}. Synthetic limitations; not verified.")

    def compact(self):
        return self.service.compact_context(self.project.id, self.project.name,
                    self.project.description, session_id=self.cm.session_id)

    def prompt(self):
        return self.service.chat_system_prompt(self.cm, self.project.name, self.project.description)

    def test_manual_compaction_prefix_tail_full_history_and_replay(self):
        self.populate()
        before = self.cm.build_api_messages(self.prompt())
        original = copy.deepcopy(self.cm.display_messages)
        result = self.compact()
        self.assertEqual(result["status"], "completed")
        sent = self.client.requests[-1]
        self.assertEqual(sent[:-1], before[:len(sent) - 1])
        self.assertIn("0.123456789", sent[-2]["content"] if sent[-2]["role"] == "user" else sent[-3]["content"])
        after = self.cm.build_api_messages(self.prompt())
        self.assertEqual(before[0], after[0])
        self.assertEqual(self.service.chat_system_prompt(self.cm,"Updated name","Updated description"), before[0]["content"])
        self.assertEqual(before[-4:], after[-4:])
        self.assertEqual(self.cm.display_messages, original)
        self.assertLess(result["record"]["after_tokens"], result["record"]["before_tokens"])
        self.cm._path.unlink()
        rebuilt = self.service.session_store(self.project.id, self.project.name).open_session(self.cm.session_id)
        self.assertEqual(rebuilt.build_api_messages(self.prompt()), after)
        self.assertEqual(rebuilt.display_messages, original)
        records = UsageStore().records(self.project.id, self.cm.session_id)
        self.assertEqual(records[0]["task"], "compression")
        self.assertEqual(records[0]["operation"], "manual_compaction")

    def test_repeated_compaction_consolidates_checkpoints(self):
        self.populate()
        self.compact()
        self.client.summary = "Consolidated checkpoint; preserved DOI:10.fixture/example; 0.123456789 m."
        self.cm.add_user_message("Additional work. " * 250)
        self.cm.add_assistant_message("Still synthetic.")
        result = self.compact()
        self.assertEqual(result["status"], "completed")
        sent = self.cm.build_api_messages(self.prompt())
        self.assertEqual(len(self.cm._active_summaries()), 1)
        self.assertEqual(len(self.cm.compressed_summaries), 2)
        self.assertIn("Consolidated checkpoint", sent[1]["content"])
        self.assertNotIn("## 用户目标", sent[1]["content"])
        self.assertEqual(len(self.cm._history), 14)

    def test_empty_and_no_reduction_keep_history(self):
        self.assertEqual(self.compact()["status"], "unchanged")
        self.assertFalse(self.client.requests)
        self.populate(1)
        original = copy.deepcopy(self.cm.build_api_messages(self.prompt()))
        self.client.summary = "Unhelpful summary. " * 1500
        self.assertEqual(self.compact()["status"], "unchanged")
        self.assertEqual(self.cm.build_api_messages(self.prompt()), original)
        self.assertFalse(self.cm.compressed_summaries)

    def test_automatic_compaction_never_consumes_reserved_recent_turns(self):
        self.populate(2)
        self.assertIsNone(self.cm.compaction_plan(self.prompt(), keep_rounds=2))
        self.assertIsNotNone(self.cm.compaction_plan(self.prompt(), keep_rounds=2, manual=True))

    def test_truncated_empty_and_machine_action_summaries_never_commit(self):
        self.populate()
        original = copy.deepcopy(self.cm.build_api_messages(self.prompt()))
        for text, reason in [("partial", "length"), ("", "stop"), ("[ACTION:search]{}[/ACTION]", "stop")]:
            self.client.summary, self.client.finish = text, reason
            self.assertEqual(self.compact()["status"], "failed")
            self.assertEqual(self.cm.build_api_messages(self.prompt()), original)
        self.assertFalse(self.cm.compressed_summaries)

    def test_stale_selected_span_is_rejected_without_history_loss(self):
        self.populate()
        plan = self.cm.compaction_plan(self.prompt())
        first = self.compact()
        self.assertEqual(first["status"], "completed")
        expected = copy.deepcopy(self.cm.build_api_messages(self.prompt()))
        with self.assertRaises(ValueError):
            self.cm.commit_compaction("stale", plan, mode="manual", provider="deepseek", model="deepseek-flash")
        self.assertEqual(expected, self.cm.build_api_messages(self.prompt()))

    def test_manual_maintenance_does_not_overwrite_stopped_goal(self):
        self.populate()
        previous = dict(state="cancelled", goal="完成文献研究", phase="检索", completed_steps=["已入库"], pending_steps=["精读"])
        self.cm.set_run_state(previous)
        run = AgentRun(self.cm, self.project.id, "/compact", "compression")
        with run_scope(run):
            self.assertEqual(self.compact()["status"], "completed")
        run.finish()
        self.assertEqual(self.cm._meta["last_run"], previous)
        resumed = AgentRun(self.cm, self.project.id, "继续")
        self.assertEqual(resumed.goal, "完成文献研究")
        self.assertIn("已入库", resumed.resume_context)
        resumed.finish()

    def test_cancel_compaction_keeps_context_and_continuation_goal(self):
        self.populate()
        self.cm.set_run_state(dict(state="cancelled", goal="继续光学研究"))
        original = copy.deepcopy(self.cm.build_api_messages(self.prompt()))
        run = AgentRun(self.cm, self.project.id, "/compact", "compression")
        self.client.block = True
        errors = []
        def work():
            with run_scope(run):
                try:
                    self.compact()
                except OperationCancelled:
                    errors.append("cancelled")
                finally:
                    run.finish()
        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(self.client.entered.wait(3))
        run.stop()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["cancelled"])
        self.assertEqual(self.cm.build_api_messages(self.prompt()), original)
        self.assertEqual(self.cm._meta["last_run"]["goal"], "继续光学研究")
        self.assertEqual(self.cm._meta["maintenance"]["state"], "cancelled")

    def test_occupancy_is_not_cumulative_usage_and_reprices_after_compaction(self):
        self.populate()
        self.service.chat(self.project.id, self.project.name, "New work", topic_desc=self.project.description,
                          session_id=self.cm.session_id)
        before = context_status(self.cm, self.prompt())
        self.assertTrue(before["anchored"])
        self.assertGreater(before["used"], before["sample"]["input_tokens"])
        self.assertEqual(self.compact()["status"], "completed")
        after = context_status(self.cm, self.prompt())
        self.assertFalse(after["anchored"])
        self.assertLess(after["used"], before["used"])
        self.assertEqual(after["window"], 1_000_000)

    def test_unknown_and_switched_models_have_scoped_capacity(self):
        with patch("paperpilot.llm_client.get_task_model", return_value="unknown-fixture"):
            self.assertIsNone(context_policy().window)
        with patch("paperpilot.config.load_config", return_value={"agent": {"context_windows": {"deepseek": {"unknown-fixture": 32768}}}}):
            self.assertEqual(context_policy("unknown-fixture").window, 32768)
            self.assertEqual(context_policy("different-fixture").window, None)

    def test_interrupted_maintenance_recovers_without_inventing_chat_turn(self):
        self.populate()
        self.cm.set_maintenance_state(dict(state="running", goal="/compact"))
        original = copy.deepcopy(self.cm.display_messages)
        rebuilt = self.service.session_store(self.project.id, self.project.name).open_session(self.cm.session_id)
        rebuilt.recover_interrupted_run()
        self.assertEqual(rebuilt._meta["maintenance"]["state"], "interrupted")
        self.assertEqual(rebuilt.display_messages, original)

    def test_smaller_model_compacts_only_complete_prefix_that_fits(self):
        self.populate()
        original = copy.deepcopy(self.cm.build_api_messages(self.prompt()))
        config = {"agent": {"context_windows": {"deepseek": {"deepseek-flash": 24000}}}}
        with patch("paperpilot.config.load_config", return_value=config):
            result = self.compact()
        self.assertEqual(result["status"], "completed")
        request = self.client.requests[0]
        self.assertEqual(request[:-1], original[:len(request) - 1])
        self.assertLess(len(request), len(original) - 3)
        self.assertEqual(request[-2]["role"], "assistant")
        self.assertLessEqual(estimate_request_tokens(request) + 3000, 24000)
        self.assertEqual(len(self.cm._history), 12)

    def test_oversized_single_turn_is_preserved_without_api_request(self):
        message = "Synthetic oversized evidence. " * 5000
        config = {"agent": {"context_windows": {"deepseek": {"deepseek-flash": 24000}}}}
        with patch("paperpilot.config.load_config", return_value=config):
            with self.assertRaisesRegex(ValueError, "超出模型上下文预算"):
                self.service.chat(self.project.id, self.project.name, message,
                    topic_desc=self.project.description, session_id=self.cm.session_id)
        self.assertFalse(self.client.requests)
        self.assertEqual(self.cm._history[-1]["content"], message)

    def test_auto_budget_uses_the_same_calibrated_usage_as_the_meter(self):
        self.populate()
        self.cm.prepare_project_context(self.project.name,self.project.description)
        estimated = estimate_request_tokens(self.cm.build_api_messages(self.prompt()))
        self.assertGreater(estimated,24000)
        self.cm.set_context_sample(dict(provider="deepseek",model="deepseek-flash",
            input_tokens=10000,estimated_input=estimated,compression_count=0))
        config = {"agent": {"context_windows": {"deepseek": {"deepseek-flash": 24000}}}}
        with patch("paperpilot.config.load_config",return_value=config):
            self.assertEqual(context_status(self.cm,self.prompt())["used"],10000)
            result = self.service.chat(self.project.id,self.project.name,"A small follow-up",
                topic_desc=self.project.description,session_id=self.cm.session_id)
        self.assertFalse(result["compressed"])
        self.assertEqual(len(self.client.requests),1)
        self.assertFalse(self.cm.compressed_summaries)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        engine = library._engine
        if engine is not None:
            engine.dispose()
