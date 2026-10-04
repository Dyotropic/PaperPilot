"""Isolated long-task acceptance: actual files/library/journal, scripted models.

Run with tools/run_validation.py. No external sources, paid models or user data.
Provider transports and native UI have separate validation entry points.
"""
import copy
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from paperpilot import library
from paperpilot.ai_service import AIService
from paperpilot.agent_budget import TaskBudget, BudgetPaused, budget_scope
from paperpilot.agent_loop import (create_task, TaskController, saved_task, live_controller, default_workspace,
                                  DEFAULT_BUDGET, loop_settings)
from paperpilot.agent_runtime import AgentRun, CancellationToken, run_scope
from paperpilot.agent_workspace import Workspace, digest
from paperpilot.conversation import ConversationManager, pending_tool_calls
from paperpilot.context_budget import estimate_request_tokens, ContextPolicy
from paperpilot.llm_client import ChatResult, LLMClient, _tool_options
from paperpilot.llm_usage import TokenUsage
from paperpilot.config import load_config


class ScriptClient(LLMClient):
    def __init__(self, callback):
        super().__init__("deepseek-flash")
        self.provider, self.callback = "deepseek", callback

    def _do_chat(self, messages, *args):
        result = self.callback(copy.deepcopy(messages))
        result.usage = result.usage or TokenUsage(input_tokens=estimate_request_tokens(messages), output_tokens=80,
                                                 total_tokens=estimate_request_tokens(messages) + 80)
        return result

    def _do_cancellable(self, messages, *args):
        return self._do_chat(messages)


class Actor:
    def __init__(self, cm, actions, *, verdicts=None):
        self.cm, self.actions = cm, list(actions)
        self.verdicts = list(verdicts or [True])
        self.requests = []
        self.verifications = 0

    def __call__(self, messages):
        self.requests.append(messages)
        if "独立验收审查者" in messages[0]["content"]:
            self.verifications += 1
            passed = self.verdicts.pop(0) if self.verdicts else True
            state = self.cm.get_task_state()
            return ChatResult(content=json.dumps(dict(passed=passed, criteria=[dict(index=i, passed=passed,
                reason="文件内容/摘要记录支持这一条件" if passed else "尚未提供条件所需的结果")
                for i in range(len(state["criteria"]))]), ensure_ascii=False), finish_reason="stop")
        if messages[-1]["content"].startswith("现在生成科研工作流的上下文检查点"):
            return ChatResult(content="## 用户目标与需求\n保留当前课题目标\n## 研究依据与引用\n早期材料已阅读\n"
                "## 关键决策与约束\n以持久化任务为准\n## 已完成工作与结果\n先前讨论完成\n"
                "## 未完成工作与下一步\n当前计划仍需执行\n## 关键数据与定位信息\n使用持久证据", finish_reason="stop")
        if not self.actions:
            return ChatResult(content="本轮回复结束，但尚未验收。", finish_reason="stop")
        action = self.actions.pop(0)
        if callable(action):
            action = action(self.cm.get_task_state())
        if isinstance(action, ChatResult):
            return action
        name, args = action
        return ChatResult(tool_calls=[dict(id=uuid.uuid4().hex, type="function",
            function=dict(name=name, arguments=json.dumps(args, ensure_ascii=False)))], finish_reason="tool_calls")


def refs(state, *names):
    return [e["id"] for e in state["evidence"] if e["ok"] and e["tool"] in names]


def plan(title="整理证据", key="work"):
    return ("set_plan", dict(steps=[dict(id=key, title=title, depends_on=[])]))


def running(key="work"):
    return ("update_step", dict(id=key, status="running", evidence_ids=[]))


def done(*names, key="work"):
    return lambda state: ("update_step", dict(id=key, status="done", evidence_ids=refs(state, *names)))


def finish(*names, summary="结果已交付，范围见工具证据。"):
    return lambda state: ("finish_task", dict(summary=summary, criteria=[dict(index=i, evidence_ids=refs(state, *names))
        for i in range(len(state["criteria"]))]))


class LoopBusinessTests(unittest.TestCase):
    def setUp(self):
        self.project = library.create_project("长任务 " + uuid.uuid4().hex[:8], "跨领域研究证据")
        self.service = AIService()
        self.cm = self.service.get_conversation(self.project.id, self.project.name)
        self.root = default_workspace(self.cm)
        self.root.mkdir(parents=True)
        self.config = copy.deepcopy(load_config())
        self.config["agent"]["team"].update(enabled=False)
        self.cfg = patch("paperpilot.config.load_config", return_value=self.config)
        self.cfg.start()

    def tearDown(self):
        self.assertIsNone(live_controller(self.cm))
        self.cfg.stop()

    def controller(self, actions, *, mode="direct_edit", objective="研究现状与证据核对", criteria=None,
                   approval=None, limits=None, cm=None, resume=False, verdicts=None):
        cm = cm or self.cm
        if not resume:
            create_task(cm, self.project.id, objective, criteria or ["整理可核对的结果"], self.root, mode,
                        limits or dict(seconds=100, requests=80, tokens=1000000))
        actor = Actor(cm, actions, verdicts=verdicts)
        client = ScriptClient(actor)
        self.service._get_client = lambda task=None: client
        self.service._resolve_task_model = lambda task: "deepseek-flash"
        run = AgentRun(cm, self.project.id, objective, operation="loop")
        controller = TaskController(self.service, cm, run, resume=resume, limits=limits,
                                    on_approval=approval)
        return controller, actor

    def execute(self, controller):
        try:
            return controller.execute()
        finally:
            controller.run.finish()

    def test_bilingual_multidomain_library_to_report(self):
        cases = [("拓扑光子学", "比较边界态与抗扰动证据", 3),
                 ("Longitudinal cancer cohort", "Separate association from causality", 35),
                 ("城市降雨与洪涝", "指出时间和空间尺度的验证限制", 180)]
        for field, question, size in cases:
            with self.subTest(field=field):
                papers = [dict(title=f"{field} evidence {i}", authors="Chen, Smith", year=2022 + i % 3,
                               doi=f"10.7777/{self.project.id}-{size}-{i}", source="openalex",
                               abstract=f"{question}；样本 {i + 20}，结论仅适用于该场景。") for i in range(size)]
                library.save_papers_to_project(self.project.id, papers)
                report = f"# {field}\n\n{question}。\n已核对摘要，未声称读取全文。\n"
                actions = [plan(question), running(), ("read_library", {}),
                    lambda s: ("read_dataset", dict(dataset_id=list(s["datasets"])[-1], start=0, count=3)),
                    ("write_file", dict(path=f"report-{size}.md", content=report)),
                    done("read_library", "write_file"), finish("read_library", "write_file", summary=report)]
                c, actor = self.controller(actions, objective=field + "：" + question,
                    criteria=["交付研究报告", "说明资料与验证范围"])
                self.execute(c)
                self.assertEqual(c.state["status"], "completed")
                self.assertEqual((self.root / f"report-{size}.md").read_text(encoding="utf-8"), report)
                self.assertEqual(actor.verifications, 1)
                self.assertFalse(pending_tool_calls(self.cm._messages))
                self.assertGreater(c.state["used"]["requests"], 7)

    def test_read_only_can_finish_analysis_and_hides_mutations(self):
        (self.root / "evidence.csv").write_text("group,n,value\ncontrol,30,2.1\n", encoding="utf-8")
        c, actor = self.controller([plan("核对样本数"), running(),
            ("read_file", dict(path="evidence.csv")), done("read_file"), finish("read_file")], mode="read_only")
        self.execute(c)
        self.assertEqual(c.state["status"], "completed")
        self.assertEqual((self.root / "evidence.csv").read_text(), "group,n,value\ncontrol,30,2.1\n")
        self.assertNotIn("write_file", {t["function"]["name"] for t in c._definitions()})

    def test_edit_requires_fresh_read_and_unique_match(self):
        path = self.root / "protocol.md"
        path.write_text("dose 5\ndose 5\n", encoding="utf-8")
        w = Workspace(self.root, "direct_edit")
        with self.assertRaises(ValueError):
            w.propose("protocol.md", old="dose 5", new="dose 6")
        w.read("protocol.md")
        with self.assertRaises(ValueError):
            w.propose("protocol.md", old="dose 5", new="dose 6")
        path.write_text("dose 7\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            w.propose("protocol.md", content="dose 8\n")
        w.read("protocol.md")
        p = w.propose("protocol.md", old="dose 7", new="dose 8")
        w.apply(p)
        self.assertEqual(path.read_text(), "dose 8\n")

    def test_ask_edit_previews_exact_diff_then_applies_once(self):
        path = self.root / "analysis.md"
        path.write_text("相关性不是因果性。\n", encoding="utf-8")
        seen = []
        def approve(identity, details, respond):
            seen.append(details)
            self.assertIn("-相关性不是因果性。", details["preview"]["diff"])
            self.assertTrue(respond(details["request_id"], True))
        c, _ = self.controller([plan(), running(), ("read_file", dict(path="analysis.md")),
            ("edit_file", dict(path="analysis.md", old_text="相关性不是因果性。", new_text="需控制混杂后检验因果假设。")),
            done("edit_file"), finish("edit_file")], mode="ask_edit", approval=approve)
        self.execute(c)
        self.assertEqual(c.state["status"], "completed")
        self.assertEqual(len(seen), 1)
        self.assertIn("控制混杂", path.read_text(encoding="utf-8"))
        self.assertFalse(c.respond(seen[0]["request_id"], True))

    def test_rejected_edit_pauses_without_alternative_write(self):
        path = self.root / "notes.txt"
        path.write_text("original", encoding="utf-8")
        def reject(identity, details, respond):
            respond(details["request_id"], False)
        actions = [plan(), running(), ("read_file", dict(path="notes.txt")),
            ("write_file", dict(path="notes.txt", content="changed")),
            lambda s: ("report_blocker", dict(reason="用户拒绝修改，需要重新讨论交付方式",
                                               evidence_ids=[s["evidence"][-1]["id"]]))]
        c, _ = self.controller(actions, mode="ask_edit", approval=reject)
        self.execute(c)
        self.assertEqual(c.state["status"], "paused")
        self.assertEqual(path.read_text(), "original")
        self.assertFalse(any(e["ok"] and e["tool"] == "write_file" for e in c.state["evidence"]))

    def test_file_changed_after_approval_is_not_overwritten(self):
        path = self.root / "review.txt"
        path.write_text("baseline", encoding="utf-8")
        def stale(identity, details, respond):
            path.write_text("human edit", encoding="utf-8")
            respond(details["request_id"], True)
        c, _ = self.controller([plan(), running(), ("read_file", dict(path="review.txt")),
            ("write_file", dict(path="review.txt", content="model edit"))], mode="ask_edit", approval=stale)
        self.execute(c)
        self.assertEqual(path.read_text(), "human edit")
        self.assertTrue(any("批准已失效" in e["result_excerpt"] for e in c.state["evidence"]))
        self.assertNotEqual(c.state["status"], "completed")

    def test_cancel_pending_approval_closes_native_pairs(self):
        ready = threading.Event()
        captured = []
        def wait(identity, details, respond):
            captured.append((details, respond))
            ready.set()
        c, _ = self.controller([plan(), running(), ("write_file", dict(path="new.md", content="proposal"))],
                               mode="ask_edit", approval=wait)
        thread = threading.Thread(target=lambda: self.execute(c))
        thread.start()
        self.assertTrue(ready.wait(4))
        c.run.stop()
        thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(c.state["status"], "paused")
        self.assertFalse((self.root / "new.md").exists())
        self.assertFalse(captured[0][1](captured[0][0]["request_id"], True))
        self.assertFalse(pending_tool_calls(self.cm._messages))
        self.assertEqual(self.cm._meta["last_run"]["state"], "paused")

    def test_request_and_token_limits_pause_before_new_work(self):
        c, actor = self.controller([plan(), running(), ("write_file", dict(path="later.txt", content="no"))],
            limits=dict(seconds=100, requests=2, tokens=1000000))
        self.execute(c)
        self.assertEqual(c.state["status"], "paused")
        self.assertEqual(c.state["used"]["requests"], 2)
        self.assertEqual(len(actor.requests), 2)
        self.assertFalse((self.root / "later.txt").exists())
        c, actor = self.controller([plan()], limits=dict(seconds=100, requests=10, tokens=2000))
        self.execute(c)
        self.assertEqual(len(actor.requests), 0)
        self.assertIn("token", c.state["reason"])

    def test_deadline_cancels_inflight_model_and_preserves_pause_reason(self):
        c, actor = self.controller([plan()])
        c.budget.base_seconds = 99.8
        c.budget.used["seconds"] = 99.8
        def slow(messages):
            for _ in range(100):
                c.run.token.check()
                time.sleep(.02)
            return ChatResult(content="unreachable")
        self.service._get_client = lambda task=None: ScriptClient(slow)
        self.execute(c)
        self.assertEqual(c.state["status"], "paused")
        self.assertIn("时长", c.state["reason"])
        self.assertNotIn("用户停止", c.final_message())
        self.assertGreater(c.state["used"]["estimated_requests"], 0)

    def test_restart_requires_confirmation_and_reconciles_applied_file(self):
        c, _ = self.controller([])
        state = c.state
        text = "already committed\n"
        (self.root / "recovered.md").write_bytes(text.encode("utf-8"))
        state.update(status="running", intent=dict(kind="file", path="recovered.md", before_hash=None,
            after_hash=digest(text.encode()), tool_call_id="pending", status="prepared"),
            approval=dict(request_id="old-approval"))
        self.cm.set_task_state(state)
        rebuilt = ConversationManager(self.project.name, session_id=self.cm.session_id, storage_path=self.cm._path)
        recovered = saved_task(rebuilt, recover=True)
        self.assertEqual(recovered["status"], "paused")
        self.assertIsNone(recovered["approval"])
        self.assertIsNone(live_controller(rebuilt))
        with self.assertRaises(ValueError):
            TaskController(self.service, rebuilt, c.run)
        resumed, _ = self.controller([plan(), running(), done("write_file"), finish("write_file")],
                                     cm=rebuilt, resume=True)
        self.execute(resumed)
        self.assertEqual(resumed.state["status"], "completed")
        self.assertIn("recovered", resumed.state["evidence"][0]["result_excerpt"])
        self.assertEqual((self.root / "recovered.md").read_text(), text)

    def test_completion_rejected_then_work_continues(self):
        actions = [plan(), running(), ("write_file", dict(path="report.md", content="v1")),
                   done("write_file"), finish("write_file"),
                   ("read_file", dict(path="report.md")), finish("read_file")]
        c, actor = self.controller(actions, verdicts=[False, True])
        self.execute(c)
        self.assertEqual(actor.verifications, 2)
        self.assertEqual(c.state["status"], "completed")

    def test_empty_claim_missing_evidence_and_deleted_artifact_never_complete(self):
        for kind in ("no_plan", "bad_evidence", "missing_file", "empty_summary"):
            with self.subTest(kind=kind):
                actions = [finish("write_file")] if kind == "no_plan" else [plan(), running(),
                    ("write_file", dict(path=f"{kind}.md", content="evidence")), done("write_file")]
                if kind == "bad_evidence":
                    actions.append(("finish_task", dict(summary="claim", criteria=[dict(index=0, evidence_ids=["fake"])])))
                elif kind == "missing_file":
                    def remove_then_finish(s):
                        (self.root / "missing_file.md").unlink()
                        return finish("write_file")(s)
                    actions.append(remove_then_finish)
                elif kind == "empty_summary":
                    actions.append(finish("write_file", summary="  "))
                c, actor = self.controller(actions)
                self.execute(c)
                self.assertNotEqual(c.state["status"], "completed")
                self.assertEqual(actor.verifications, 0)

    def test_paths_modes_files_and_limits(self):
        w = Workspace(self.root, "direct_edit")
        for path in ("../escape.txt", "..\\escape.txt", "C:\\secrets.txt", "/outside.txt", "x.txt:stream",
                     "config.yaml", ".env", ".git/config.txt", "auth.json", "file.txt ", ".ssh/key.txt", "CON.txt", "NUL", "COM1.md"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                w.resolve(path)
        with self.assertRaises(ValueError):
            Workspace(self.root, "full_access")
        huge = self.root / "huge.txt"
        huge.write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            w.read("huge.txt")
        (self.root / "broken.txt").write_bytes(b"\xff\x00")
        with self.assertRaises(ValueError):
            w.read("broken.txt")
        with self.assertRaises(ValueError):
            w.read("unknown.pdf")
        with self.assertRaises(PermissionError):
            Workspace(self.root, "read_only").propose("no.txt", content="denied")

    def test_repeated_reads_and_plain_replies_prompt_adjustment_then_continue(self):
        (self.root / "same.txt").write_text("unchanged", encoding="utf-8")
        c, _ = self.controller([plan(), running()] + [("read_file", dict(path="same.txt"))] * 9 +
                              [done("read_file"), finish("read_file")], limits=DEFAULT_BUDGET)
        self.execute(c)
        self.assertEqual(c.state["status"], "completed")
        self.assertEqual(len(c.state["evidence"]), 9)
        self.assertLessEqual(len(c.state["repeat_chain"]), 8)
        self.assertTrue(any("改变取证路径" in m["content"] for m in self.cm._history))
        c, _ = self.controller([ChatResult(content="还未验收。", finish_reason="stop")] * 8 +
            [plan(), running(), ("read_file", dict(path="same.txt")), done("read_file"), finish("read_file")],
            limits=DEFAULT_BUDGET)
        self.execute(c)
        self.assertEqual(c.state["status"], "completed")
        self.assertFalse(pending_tool_calls(self.cm._messages))

    def test_compaction_keeps_goal_plan_permissions_and_raw_history(self):
        for i in range(4):
            self.cm.add_user_message("此前论文讨论 " + "历史资料。" * 1000)
            self.cm.add_assistant_message(f"此前结论 {i}：仅为旧任务背景。")
        raw_count = len(self.cm._history)
        policy = ContextPolicy("deepseek", "deepseek-flash", 100000, "test", 8192, 22000, 1)
        c, actor = self.controller([plan(), running(), ("write_file", dict(path="goal.md", content="最新目标")),
                                    done("write_file"), finish("write_file")], objective="保持当前中文目标，不沿用旧课题")
        with patch("paperpilot.agent_loop.context_policy", return_value=policy), \
             patch("paperpilot.ai_service.context_policy", return_value=policy), \
             patch("paperpilot.ai_service.get_client", return_value=self.service._get_client()):
            self.execute(c)
        self.assertEqual(c.state["status"], "completed")
        self.assertTrue(self.cm.compressed_summaries)
        self.assertGreater(len(self.cm._history), raw_count)
        main = [m for m in actor.requests if m[-1]["content"].startswith("<runtime_task>")]
        self.assertTrue(all(c.state["objective"] in m[-1]["content"] and '"mode": "direct_edit"' in m[-1]["content"] for m in main))

    def test_single_human_goal_compacts_its_own_long_tool_cycles(self):
        actions = [plan("逐步读取不同规模的研究资料并写总结"), running()]
        for i in range(7):
            name = f"source-{i}.txt"
            (self.root / name).write_bytes((f"领域{i}：" + "双语证据与样本限制。" * (500 + i * 10)).encode("utf-8"))
            actions.append(("read_file", dict(path=name)))
        actions += [("write_file", dict(path="long-goal.md", content="保留当前目标与证据；资料仅为合成文本。")),
                    done("read_file", "write_file"), finish("read_file", "write_file")]
        c, actor = self.controller(actions, objective="一个用户目标内部的长上下文连续任务")
        # An intentionally low pressure threshold exercises multiple compact
        # cycles, while the full independent verifier evidence still fits.
        policy = ContextPolicy("deepseek", "deepseek-flash", 200000, "test", 8192, 28000, 1)
        with patch("paperpilot.agent_loop.context_policy", return_value=policy), \
             patch("paperpilot.ai_service.context_policy", return_value=policy), \
             patch("paperpilot.ai_service.get_client", return_value=self.service._get_client()):
            self.execute(c)
        self.assertEqual(c.state["status"], "completed", c.state["reason"])
        self.assertGreaterEqual(len(self.cm.compressed_summaries), 2)
        self.assertEqual(sum(m["role"] == "user" and not m.get("internal") for m in self.cm._history), 1)
        self.assertEqual(len([e for e in c.state["evidence"] if e["tool"] == "read_file"]), 7)
        for messages in actor.requests:
            pending = set()
            for m in messages:
                if m["role"] == "assistant":
                    pending.update(call["id"] for call in m.get("tool_calls", []))
                elif m["role"] == "tool":
                    self.assertIn(m["tool_call_id"], pending)
                    pending.remove(m["tool_call_id"])
                elif pending:
                    self.fail("Compaction or reminder split native tool pairing")
            self.assertFalse(pending)
        self.assertTrue(all(c.state["objective"] in r[-1]["content"] for r in actor.requests if r[-1]["content"].startswith("<runtime_task>")))

    def test_dependency_cycle_and_incomplete_plan_are_rejected(self):
        c, _ = self.controller([])
        with self.assertRaises(ValueError):
            c.set_plan([dict(id="a", title="a", depends_on=["b"]), dict(id="b", title="b", depends_on=["a"])])
        c.set_plan([dict(id="a", title="a", depends_on=[]), dict(id="b", title="b", depends_on=["a"])])
        with self.assertRaises(ValueError):
            c.update_step(dict(id="b", status="running", evidence_ids=[]))
        with self.assertRaises(ValueError):
            c.verify_completion(dict(summary="finished", criteria=[dict(index=0, evidence_ids=[])]))

    def test_host_steering_invalidates_pending_approval(self):
        captured = []
        def steer(identity, details, respond):
            captured.append(details)
            c.enqueue_instruction("先保留原文件，并缩小结论范围")
            self.assertFalse(respond(details["request_id"], True))
        c, _ = self.controller([plan(), running(), ("write_file", dict(path="steer.md", content="old plan"))],
                               mode="ask_edit", approval=steer)
        self.execute(c)
        self.assertFalse((self.root / "steer.md").exists())
        self.assertEqual(c.state["constraints"], ["先保留原文件，并缩小结论范围"])
        self.assertEqual(c.state["revision"], 2)
        self.assertFalse(pending_tool_calls(self.cm._messages))

    def test_search_rank_score_save_use_existing_services_and_project_ownership(self):
        self.config["data_sources"].update(openalex=True, arxiv=True, europepmc=True)
        papers = [dict(title="Cohort " + str(i), doi=f"10.7778/loop-{self.project.id}-{i}",
                       source="openalex", authors="Smith", year=2023, abstract="observational cohort") for i in range(6)]
        actions = [plan("多源召回到文献入库"), running(), ("search_papers", dict(primary_kw=["癌症队列"],
            sources=["openalex", "arxiv", "europepmc"], max_per_source=400, year_min="2022", year_max="2024")),
            lambda s: ("rank_papers", dict(dataset_id=list(s["datasets"])[-1], query="cohort bias", top_k=3, ce_candidates=6)),
            lambda s: ("score_papers", dict(dataset_id=list(s["datasets"])[-1], topic="cancer cohort", max_papers=3)),
            lambda s: ("save_to_library", dict(dataset_id=list(s["datasets"])[-1], indices=[0, 2])),
            done("save_to_library"), finish("save_to_library")]
        observed = []
        def collect(**kwargs):
            observed.append(kwargs)
            return papers + [papers[0]], [("arxiv", "network", "partial fixture")]
        def score(*args, **kwargs):
            self.assertFalse(_tool_options.get().get("tools"), "Scoring must not inherit loop mutation tools")
            return [dict(index=0, ai_score=88, ai_reason=dict(overall="有时间尺度依据"))]
        c, _ = self.controller(actions)
        with patch("paperpilot.search_service.collect_sources", side_effect=collect), \
             patch("paperpilot.mt_translator.translate_all_terms", return_value=(["cancer cohort"], [], [])), \
             patch("paperpilot.indexer.rank_papers", return_value=[(papers[5], .91), (papers[3], .83), (papers[1], .72)]) as rank, \
             patch.object(self.service, "score_papers", side_effect=score):
            self.execute(c)
        self.assertEqual(c.state["status"], "completed")
        self.assertTrue(observed[0]["parallel"])
        self.assertEqual(observed[0]["max_per"], 400)
        self.assertEqual(observed[0]["primary_kw"], ["cancer cohort"])
        self.assertEqual(observed[0]["filters"].year_from, 2022)
        self.assertEqual(rank.call_args.kwargs, dict(top_k=3, ce_candidates=6))
        stored = library.get_project_papers(self.project.id)
        self.assertEqual({p["doi"] for p in stored}, {papers[5]["doi"], papers[1]["doi"]})
        self.assertEqual(next(p for p in stored if p["doi"] == papers[5]["doi"])["ai_score"], 88)
        self.assertTrue(any(json.loads(c._evidence_data(e)["content"]).get("partial") is True
                            for e in c.state["evidence"] if e["tool"] == "search_papers"))

    def test_source_failures_and_invalid_filters_do_not_create_evidence_of_success(self):
        self.config["data_sources"]["openalex"] = True
        actions = [plan(), running(), ("search_papers", dict(primary_kw=["rare disease"], sources=["openalex"], max_per_source=5)),
            ("search_papers", dict(primary_kw=["rare disease"], sources=["openalex"], max_per_source=5,
                                    year_min="2024", year_max="2020")),
            ("search_papers", dict(primary_kw=["rare disease"], sources=["arxiv"], max_per_source=5))]
        c, _ = self.controller(actions)
        with patch("paperpilot.search_service.collect_sources", return_value=([], [("openalex", "network", "fixture failure")])):
            self.execute(c)
        self.assertFalse(c.state["datasets"])
        self.assertFalse(any(e["ok"] for e in c.state["evidence"]))
        self.assertNotEqual(c.state["status"], "completed")

    def test_truncated_and_unknown_native_batches_execute_no_mutation(self):
        for kind in ("truncated", "unknown"):
            with self.subTest(kind=kind):
                calls = [dict(id="valid-write", type="function", function=dict(name="write_file",
                    arguments=json.dumps(dict(path="never.txt", content="never executed"))))]
                if kind == "unknown":
                    calls.append(dict(id="malicious", type="function", function=dict(name="set_permission",
                                                                                       arguments='{"mode":"full_access"}')))
                result = ChatResult(tool_calls=calls, finish_reason="length" if kind == "truncated" else "tool_calls")
                c, _ = self.controller([plan(), running(), result])
                self.execute(c)
                self.assertFalse((self.root / "never.txt").exists())
                self.assertFalse(pending_tool_calls(self.cm._messages))
                self.assertEqual(c.state["mode"], "direct_edit")

    def test_verifier_invalid_json_is_fail_closed(self):
        c, actor = self.controller([plan(), running(), ("write_file", dict(path="closed.txt", content="evidence")),
                                    done("write_file"), finish("write_file")])
        original = actor.__call__
        def invalid(messages):
            if "独立验收审查者" in messages[0]["content"]:
                return ChatResult(content="looks complete", finish_reason="stop")
            return original(messages)
        self.service._get_client = lambda task=None: ScriptClient(invalid)
        self.execute(c)
        self.assertNotEqual(c.state["status"], "completed")
        self.assertFalse(c.state["verification"]["passed"])

    def test_journal_rebuild_and_budget_unknown_usage_are_retained(self):
        c, _ = self.controller([plan()], limits=dict(seconds=100, requests=2, tokens=1000000))
        self.execute(c)
        original = self.cm.get_task_state()
        # Projection loss is recovered from events, including the separate task metadata.
        self.cm._path.unlink()
        rebuilt = ConversationManager(self.project.name, session_id=self.cm.session_id, storage_path=self.cm._path)
        self.assertEqual(rebuilt.get_task_state(), original)
        gate = TaskBudget(dict(seconds=100, requests=10, tokens=100000),
            dict(seconds=2, requests=3, tokens=500, pending_tokens=600, pending_requests=2), lambda used: None,
            CancellationToken())
        self.assertEqual(gate.used["tokens"], 1100)
        self.assertEqual(gate.used["estimated_requests"], 2)
        first = gate.reserve([dict(role="user", content="data")], 100)
        gate.settle(first, ChatResult(content="usage omitted"))
        self.assertGreater(gate.used["tokens"], 1200)

    def test_active_incremental_checkpoint_rebuild_and_corrupt_state_rejection(self):
        c, _ = self.controller([])
        c.state["status"] = "running"
        c.save()
        c.set_plan([dict(id="audit", title="核对目标、资料和当前文件", depends_on=[])])
        c.update_step(dict(id="audit", status="running", evidence_ids=[]))
        c.tools.dataset([dict(title="Evidence", abstract="Only an abstract, no full text.")])
        (self.root / "input.csv").write_bytes(b"value\n1\n2\n")
        c.record_evidence("read_file", dict(path="input.csv"), c.workspace.read("input.csv"), True)
        original = self.cm.get_task_state()
        events = (self.cm.storage_directory / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"evidence_append"', events)
        self.assertIn('"maps"', events)
        self.cm._path.unlink()
        rebuilt = ConversationManager(self.project.name, session_id=self.cm.session_id, storage_path=self.cm._path)
        self.assertEqual(rebuilt.get_task_state(), original)
        self.assertEqual(saved_task(rebuilt, recover=True)["status"], "paused")
        for bad in ([], {}, {**original, "status": []}, {**original, "used": dict(tokens=-1)},
                    {**original, "used": dict(seconds=float("nan"))}, {**original, "mode": "full_access"}):
            rebuilt._meta["agent_task"] = bad
            with self.subTest(bad=repr(bad)), self.assertRaises(ValueError):
                saved_task(rebuilt, recover=True)
        c.run.finish()

    def test_disabled_team_tools_cannot_be_invoked_by_forged_call(self):
        c, _ = self.controller([("team_dispatch", dict(tasks=[dict(name="hidden", instruction="read only")]))] * 6)
        self.execute(c)
        self.assertEqual(c.state["status"], "paused")
        self.assertIsNone(c.team)
        self.assertFalse(c.state["evidence"])
        self.assertFalse(pending_tool_calls(self.cm._messages))

    def test_replanning_preserves_dependency_admission_without_global_write_gate(self):
        c, _ = self.controller([])
        c.set_plan([dict(id="draft", title="写报告", depends_on=[])])
        c.update_step(dict(id="draft", status="running", evidence_ids=[]))
        c.set_plan([dict(id="source", title="重新核对资料", depends_on=[]),
                    dict(id="draft", title="写报告", depends_on=["source"])])
        self.assertEqual(c.state["plan"][1]["status"], "pending")
        # The checklist no longer gates all edits: an exploratory note can be
        # written without pretending the dependent report has been completed.
        c.tools.execute("write_file", dict(path="exploration.md", content="探索笔记，尚未核对资料。"))
        self.assertEqual((self.root / "exploration.md").read_text(encoding="utf-8"), "探索笔记，尚未核对资料。")
        with self.assertRaises(ValueError):
            c.update_step(dict(id="draft", status="running", evidence_ids=[]))
        c.run.finish()

    def test_unstarted_checkpoint_requires_confirmation_after_restart(self):
        c, _ = self.controller([])
        state = saved_task(self.cm, recover=True)
        self.assertEqual(state["status"], "paused")
        with self.assertRaises(ValueError):
            TaskController(self.service, self.cm, c.run)
        self.assertEqual(state["used"]["requests"], 0)
        c.run.finish()

    def test_project_rename_relocates_paused_workspace_and_resumes(self):
        from paperpilot import repo_manager
        c, _ = self.controller([plan(), running()], limits=dict(seconds=100, requests=2, tokens=1000000))
        self.execute(c)
        old_root = self.root
        new_name = "Renamed research " + uuid.uuid4().hex[:8]
        self.assertTrue(library.update_project(self.project.id, name=new_name))
        self.assertTrue(repo_manager.rename_project(self.project.name, new_name))
        self.service.rebind_project_storage(self.project.id, new_name)
        state = saved_task(self.cm, recover=True)
        self.root = default_workspace(self.cm)
        self.assertEqual(Path(state["workspace"]), self.root)
        self.assertFalse(old_root.exists())
        self.assertEqual(state["status"], "paused")
        resumed, _ = self.controller([("write_file", dict(path="resumed.md", content="Current project workspace")),
            done("write_file"), finish("write_file")], resume=True, limits=dict(seconds=100, requests=30, tokens=1000000))
        self.execute(resumed)
        self.assertEqual(resumed.state["status"], "completed")
        self.assertTrue((self.root / "resumed.md").is_file())
        self.assertFalse(old_root.exists())
        verdict = resumed.state["verification"]
        final_name = "Completed relocation " + uuid.uuid4().hex[:8]
        self.assertTrue(library.update_project(self.project.id, name=final_name))
        self.assertTrue(repo_manager.rename_project(new_name, final_name))
        self.service.rebind_project_storage(self.project.id, final_name)
        completed = saved_task(self.cm, recover=True)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["verification"], verdict)
        self.assertTrue((Path(completed["workspace"]) / "resumed.md").is_file())

    def test_project_rename_during_team_pauses_and_releases_same_chat_owner(self):
        from paperpilot import repo_manager
        from paperpilot.agent_team import WORKER_PROMPT, saved_teams
        self.config["agent"]["team"].update(enabled=True, max_parallel=1)
        c, actor = self.controller([plan(), running(), ("team_dispatch", dict(tasks=[
            dict(name="review", instruction="Read-only source review")]))])
        new_name = "Moved during review " + uuid.uuid4().hex[:8]
        def callback(messages):
            if messages[0]["content"] == WORKER_PROMPT:
                self.assertTrue(library.update_project(self.project.id, name=new_name))
                self.assertTrue(repo_manager.rename_project(self.project.name, new_name))
                self.service.rebind_project_storage(self.project.id, new_name)
                self.assertIs(live_controller(self.cm), c)
                return ChatResult(content="Read-only reply must not resume a moved workspace.")
            return actor(messages)
        self.service._get_client = lambda task=None: ScriptClient(callback)
        self.execute(c)
        self.assertEqual(c.state["status"], "paused")
        self.assertIn("目录已经移动", c.state["reason"])
        self.assertEqual(Path(c.state["workspace"]), default_workspace(self.cm))
        self.assertTrue(c.team.closed)
        self.assertTrue(all(a["released"] for a in saved_teams(self.cm)[0]["agents"]))
        self.assertFalse(pending_tool_calls(self.cm._messages))

    def test_long_paths_preserve_private_evidence_and_workspace_boundaries(self):
        from paperpilot.file_paths import io_path
        from paperpilot.repo_manager import atomic_write_text
        name = "research-" + "x" * 180 + ".csv"
        w = Workspace(self.root, "direct_edit")
        path = w.resolve(name)
        self.assertGreater(len(str(path)), 260)
        atomic_write_text(path, "value\n30\n32\n")
        self.assertEqual(w.read(name)["total_lines"], 3)
        w.apply(w.propose(name, old="30", new="31"))
        self.assertIn("31", w.read(name)["content"])
        self.assertIn(name, [r["path"] for r in w.list()["results"]])
        c, _ = self.controller([])
        c.asset_directory = c.asset_directory.parent / ("nested-" + "y" * 96) / c.state["task_id"]
        data = c.tools.dataset([dict(title="Long path evidence", abstract="Actual private snapshot")])
        self.assertEqual(c.tools.load_dataset(data["dataset_id"])[0]["title"], "Long path evidence")
        entry = c.record_evidence("read_file", {}, w.read(name), True)
        self.assertIn("total_lines", c._evidence_data(entry)["content"])
        c._record_response(ChatResult(content="Preserve long-path raw output"), False)
        response = io_path(c.asset_directory / ("response-" + c.state["latest_response"]["id"] + ".json"))
        self.assertEqual(json.loads(response.read_text(encoding="utf-8"))["content"], "Preserve long-path raw output")
        c.run.finish()

    def test_team_parallel_requests_share_budget_and_remain_read_only(self):
        from paperpilot.agent_team import AgentTeam, WORKER_PROMPT
        self.config["agent"]["team"].update(enabled=True, max_parallel=2)
        worker_arrived = threading.Barrier(2)
        actions = [plan("独立审查两个研究角度"), running(), ("team_dispatch", dict(tasks=[
            dict(name="方法", instruction="检查 longitudinal 和 causal inference 的区别"),
            dict(name="偏差", instruction="核对队列缺失资料导致的证据限制")])),
            done("team_dispatch"), finish("team_dispatch")]
        c, actor = self.controller(actions, mode="read_only")
        original = actor.__call__
        observed = []
        def callback(messages):
            if messages[0]["content"] == WORKER_PROMPT:
                observed.append(_tool_options.get())
                worker_arrived.wait(3)
                return ChatResult(content="基于 s1 只有摘要范围证据，不能得出因果结论。", finish_reason="stop")
            return original(messages)
        self.service._get_client = lambda task=None: ScriptClient(callback)
        self.execute(c)
        self.assertEqual(c.state["status"], "completed")
        self.assertEqual(len(observed), 2)
        self.assertTrue(all({t["function"]["name"] for t in row["tools"]} == {"read_source", "calculate"} for row in observed))
        self.assertEqual(c.state["used"]["requests"], len(actor.requests) + 2)
        self.assertTrue(c.team.closed)

    def test_same_session_owner_and_cross_session_artifact_separation(self):
        first, _ = self.controller([plan()])
        arrived = threading.Event()
        release = threading.Event()
        original = self.service._get_client()
        def blocked(messages):
            arrived.set()
            while not release.wait(.02):
                first.run.token.check()
            return ChatResult(content="paused reply", finish_reason="stop")
        self.service._get_client = lambda task=None: ScriptClient(blocked)
        thread = threading.Thread(target=lambda: self.execute(first))
        thread.start()
        self.assertTrue(arrived.wait(3))
        with self.assertRaises(ValueError):
            create_task(self.cm, self.project.id, "another goal", ["another result"], self.root)
        with self.assertRaises(ValueError):
            TaskController(self.service, self.cm, first.run, resume=True)
        first.run.stop()
        thread.join(4)
        self.assertFalse(thread.is_alive())
        other = self.service.create_session(self.project.id, self.project.name)
        self.assertNotEqual(other.session_id, self.cm.session_id)
        self.assertIsNone(saved_task(other))
        c, _ = self.controller([plan(), running(), ("write_file", dict(path="other.txt", content="other chat")),
                                done("write_file"), finish("write_file")], cm=other)
        self.execute(c)
        self.assertEqual(c.state["status"], "completed")
        self.assertEqual(self.cm.get_task_state()["status"], "paused")

    def test_existing_attachment_snapshot_is_input_and_cannot_be_overwritten(self):
        create_task(self.cm, self.project.id, "核对附件摘要", ["说明附件范围"], self.root, "read_only",
                    dict(seconds=100, requests=10, tokens=1000000))
        actor = Actor(self.cm, [plan(), running(), ("read_library", {}), done("read_library"), finish("read_library")])
        self.service._get_client = lambda task=None: ScriptClient(actor)
        self.service._resolve_task_model = lambda task: "deepseek-flash"
        # Attachment formatting is exercised through real immutable session assets below.
        from paperpilot.agent_attachments import prepare_file, persist_attachments
        fixture = self.root / "source.md"
        fixture.write_text("# 资料\n摘要仅有 20 个样本，不能概括总体。", encoding="utf-8")
        items = [prepare_file(fixture)]
        attachments = persist_attachments(self.cm.storage_directory, items)
        run = AgentRun(self.cm, self.project.id, "核对附件摘要", "loop", attachments=attachments)
        c = TaskController(self.service, self.cm, run)
        self.execute(c)
        self.assertTrue(any("20 个样本" in json.dumps(m, ensure_ascii=False) for m in actor.requests))
        self.assertIn("20 个样本", fixture.read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            Workspace(self.root, "direct_edit").resolve("../sessions/source.md")

    def test_original_text_and_native_photo_reread_after_compaction_and_source_removal(self):
        import io
        from PIL import Image
        from paperpilot.agent_attachments import prepare_file, persist_attachments
        text = self.root / "evidence.md"
        text.write_text("队列样本 38 与 41，资料只有摘要。", encoding="utf-8")
        photo = self.root / "measurement.png"
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), (255, 0, 0)).save(buffer, format="PNG")
        photo.write_bytes(buffer.getvalue())
        attachments = persist_attachments(self.cm.storage_directory, [prepare_file(text), prepare_file(photo)])
        def compact_then_read(state):
            snapshot = self.cm.compaction_plan(c.system, keep_rounds=0, manual=True)
            self.assertTrue(snapshot)
            self.cm.commit_compaction("原资料已归档，使用附件索引重读。", snapshot, mode="manual", provider="deepseek", model="deepseek-flash")
            self.assertFalse(any(m.get("attachments") for m in self.cm._messages))
            return ("read_attachment", dict(index=0))
        c, actor = self.controller([plan(), running(), compact_then_read,
            ("read_attachment", dict(index=1)), done("read_attachment"), finish("read_attachment")], mode="read_only")
        c.run.attachments = attachments
        text.unlink()
        photo.unlink()
        self.execute(c)
        self.assertEqual(c.state["status"], "completed", c.state["reason"])
        observations = [e for e in c.state["evidence"] if e["tool"] == "read_attachment"]
        self.assertEqual(len(observations), 2)
        self.assertIn("38", c._evidence_data(observations[0])["content"])
        self.assertTrue(any(isinstance(m["content"], list) and any(p.get("type") == "image_url" for p in m["content"])
                            for request in actor.requests for m in request))
        native_verifier = [r for r in actor.requests if "独立验收审查者" in r[0]["content"]][0]
        self.assertTrue(any(isinstance(m["content"], list) for m in native_verifier))
        self.assertEqual(sum(m["role"] == "user" and not m.get("internal") for m in self.cm._history), 1)
        self.assertNotIn("data:image", (self.cm.storage_directory / "events.jsonl").read_text(encoding="utf-8"))
        self.assertFalse(pending_tool_calls(self.cm._messages))

    def test_three_stage_plan_executes_dependencies_to_verified_report(self):
        (self.root / "measurements.csv").write_bytes(b"sample,value\na,12\nb,7\n")
        steps = [dict(id="collect", title="读取测量记录", depends_on=[]),
                 dict(id="audit", title="核对量纲并复算", depends_on=["collect"]),
                 dict(id="deliver", title="交付结果及范围", depends_on=["audit"])]
        actions = [("set_plan", dict(steps=steps)), running("collect"),
            ("read_file", dict(path="measurements.csv")), done("read_file", key="collect"), running("audit"),
            ("calculate", dict(expression="12*7")), done("calculate", key="audit"), running("deliver"),
            ("write_file", dict(path="result.md", content="# 核对\n12 × 7 = 84，仅复算记录数值，未验证实测误差。\n")),
            done("write_file", key="deliver"), finish("read_file", "calculate", "write_file")]
        c, _ = self.controller(actions, criteria=["引用真实输入", "复算并交付报告"])
        self.execute(c)
        self.assertEqual(c.state["status"], "completed")
        self.assertEqual([s["status"] for s in c.state["plan"]], ["done"] * 3)
        self.assertIn("84", (self.root / "result.md").read_text(encoding="utf-8"))

    def test_links_junctions_and_root_replacement_cannot_escape(self):
        target = self.root / "ordinary.txt"
        target.write_text("sensitive research input", encoding="utf-8")
        os.link(target, self.root / "alias.txt")
        with self.assertRaises(ValueError):
            Workspace(self.root, "direct_edit").read("alias.txt")
        (self.root / "alias.txt").unlink()
        try:
            os.symlink(target, self.root / "symbol.txt")
        except OSError:
            pass  # Windows without Developer Mode; junction is tested below.
        else:
            with self.assertRaises(ValueError):
                Workspace(self.root, "direct_edit").read("symbol.txt")
            (self.root / "symbol.txt").unlink()
        if os.name == "nt":
            import subprocess
            destination = self.root.parent / "junction-target"
            destination.mkdir()
            (destination / "outside.txt").write_text("outside workspace", encoding="utf-8")
            link = self.root / "jump"
            # Task-owned paths within runner scratch only; native PowerShell
            # creates a Windows junction without needing symbolic-link privilege.
            q = lambda p: "'" + str(p).replace("'", "''") + "'"
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                f"New-Item -ItemType Junction -Path {q(link)} -Target {q(destination)} | Out-Null"],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0)
            with self.assertRaises(ValueError):
                Workspace(self.root, "direct_edit").read("jump/outside.txt")
            os.rmdir(link)  # Remove this junction itself, preserving its target.

    def test_model_route_switch_pauses_and_cannot_send_to_new_endpoint(self):
        c, actor = self.controller([plan(), running()])
        original = actor.__call__
        def switch(messages):
            result = original(messages)
            self.service._get_client = lambda task=None: type("Changed", (), dict(
                provider="gemini", base_url="http://different.invalid", model="changed", is_available=True))()
            return result
        self.service._get_client = lambda task=None: ScriptClient(switch)
        self.execute(c)
        self.assertEqual(c.state["status"], "paused")
        self.assertIn("配置已切换", c.state["reason"])
        self.assertEqual(len(actor.requests), 1)

    def test_actual_usage_over_budget_cancels_before_mutation(self):
        c, actor = self.controller([plan()])
        def oversized(messages):
            return ChatResult(tool_calls=[dict(id="must-not-write", type="function", function=dict(
                name="write_file", arguments='{"path":"overspent.md","content":"must not commit"}'))],
                usage=TokenUsage(input_tokens=1000000, output_tokens=20, total_tokens=1000020), finish_reason="tool_calls")
        self.service._get_client = lambda task=None: ScriptClient(oversized)
        self.execute(c)
        self.assertEqual(c.state["status"], "paused")
        self.assertIn("实际 token", c.state["reason"])
        self.assertFalse((self.root / "overspent.md").exists())
        self.assertTrue(list(c.asset_directory.glob("response-*.json")))

    def test_repeated_state_operations_remind_then_allow_real_progress(self):
        c, _ = self.controller([plan()] * 12 + [running(), ("calculate", dict(expression="30 + 32")),
            done("calculate"), finish("calculate")], limits=DEFAULT_BUDGET)
        self.execute(c)
        self.assertEqual(c.state["status"], "completed")
        self.assertGreater(c.state["used"]["requests"], 12)
        self.assertTrue(any("调整计划并实际执行" in m["content"] for m in self.cm._history))

    def test_default_unlimited_ignores_legacy_config_and_preserves_usage(self):
        self.config["agent"]["loop"] = dict(permission_mode="read_only", seconds=10, requests=2, tokens=2000)
        self.assertEqual(loop_settings()["limits"], DEFAULT_BUDGET)
        state = create_task(self.cm, self.project.id, "核对样本数量", ["给出实际数量"], self.root, "read_only")
        self.assertEqual(state["limits"], DEFAULT_BUDGET)
        token = CancellationToken()
        changes = []
        counter = TaskBudget(DEFAULT_BUDGET, dict(seconds=100000, requests=10001, tokens=100000001,
            pending_tokens=12000, pending_requests=1), changes.append, token)
        counter.start()
        ticket = counter.reserve([dict(role="user", content="继续核对")], 6000)
        counter.settle(ticket, ChatResult(usage=TokenUsage(input_tokens=40, output_tokens=20, total_tokens=60)))
        counter.check()
        counter.close()
        self.assertIsNone(counter.timer)
        self.assertFalse(token.cancelled)
        self.assertEqual(changes[-1]["tokens"], 100012061)
        self.assertEqual(changes[-1]["requests"], 10002)

    def test_long_task_passes_old_time_request_and_token_thresholds(self):
        def last_done(state):
            return ("update_step", dict(id="work", status="done", evidence_ids=refs(state, "calculate")[-1:]))
        def last_finish(state):
            return ("finish_task", dict(summary="已复算 112 条仪器读数，末条校正值 114.13；仅验证这些记录。",
                criteria=[dict(index=0, evidence_ids=refs(state, "calculate")[-1:])]))
        c, actor = self.controller([plan("复算仪器读数"), running()] +
            [("calculate", dict(expression=f"({i} + 2) * 1.01")) for i in range(112)] + [last_done, last_finish],
            objective="复算112条仪器读数，说明末条校正值和覆盖范围", limits=DEFAULT_BUDGET)
        original = actor.__call__
        def large_usage(messages):
            result = original(messages)
            result.usage = TokenUsage(input_tokens=20000, output_tokens=100, total_tokens=20100)
            return result
        self.service._get_client = lambda task=None: ScriptClient(large_usage)
        c.budget.base_seconds = c.budget.used["seconds"] = 1801
        self.execute(c)
        self.assertEqual(c.state["status"], "completed", c.state["reason"])
        self.assertGreater(c.state["used"]["seconds"], 1800)
        self.assertGreater(c.state["used"]["requests"], 100)
        self.assertGreater(c.state["used"]["tokens"], 1000000)
        self.assertEqual(len([e for e in c.state["evidence"] if e["tool"] == "calculate" and e["ok"]]), 112)
        self.assertFalse(pending_tool_calls(self.cm._messages))

    def test_invalid_batches_can_recover_after_previous_five_round_limit(self):
        invalid = ChatResult(tool_calls=[dict(id="invalid", type="function", function=dict(name="unknown", arguments="{}"))],
                             finish_reason="tool_calls")
        c, _ = self.controller([invalid] * 7 + [plan(), running(), ("calculate", dict(expression="44 / 50")),
            done("calculate"), finish("calculate")], limits=DEFAULT_BUDGET)
        self.execute(c)
        self.assertEqual(c.state["status"], "completed")
        self.assertFalse(pending_tool_calls(self.cm._messages))

    def test_confirmed_resume_removes_old_budget_without_erasing_usage(self):
        c, _ = self.controller([], limits=dict(seconds=10, requests=2, tokens=2000))
        state = c.state
        state.update(status="paused", used=dict(seconds=50, requests=101, tokens=1000001))
        self.cm.set_task_state(state)
        resumed, _ = self.controller([plan(), running(), ("calculate", dict(expression="30+32")),
            done("calculate"), finish("calculate")], resume=True)
        self.execute(resumed)
        self.assertEqual(resumed.state["status"], "completed")
        self.assertEqual(resumed.state["limits"], DEFAULT_BUDGET)
        self.assertGreater(resumed.state["used"]["requests"], 101)
        self.assertGreater(resumed.state["used"]["tokens"], 1000001)


if __name__ == "__main__":
    unittest.main(verbosity=2)
