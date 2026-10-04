"""Opt-in bounded live goal loops with synthetic materials, isolated user data.

Each goal has a hard admission budget of 120 seconds / 25 requests / 180k tokens.
Provider token counts can exceed estimates; this is not a billing hard cap.
No automatic rerun, external paper sources, worker teams or production writes.
"""
import argparse
import copy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--case", choices=("all", "chinese", "english", "interaction", "interaction_chinese"), default="all")
    args = parser.parse_args()
    if not args.live:
        parser.print_help()
        return
    interaction = args.case.startswith("interaction")
    scratch = ".validation-agent-ux-20261004" if interaction else ".validation-loop-engineering-20261004"
    work = ROOT / scratch / ("live-" + uuid.uuid4().hex)
    work.mkdir(parents=True)
    os.environ.update(TEMP=str(work), TMP=str(work))
    tempfile.tempdir = str(work)
    # Read credentials only in memory; do not copy them to the isolated config.
    from paperpilot.config import load_config
    settings = copy.deepcopy(load_config())
    from paperpilot import library, repo_manager, conversation, llm_usage
    from paperpilot.ai_service import AIService
    from paperpilot.agent_loop import create_task, TaskController, default_workspace
    from paperpilot.agent_runtime import AgentRun
    from paperpilot.llm_client import get_client, get_task_model
    client = get_client("chat")
    model = get_task_model("chat")
    if not client or not client.is_available:
        raise SystemExit("Chat provider not configured; no request sent.")
    settings.setdefault("agent", {}).setdefault("team", {})["enabled"] = False
    settings["data_sources"] = dict(arxiv=False, openalex=False, europepmc=False)
    settings.setdefault("cache", {})["dir"] = str(work / "api")
    library._DB_PATH = str(work / "isolated.db")
    repo_manager._REPO_ROOT = conversation._REPO_ROOT = work / "repository"
    repo_manager._CACHE_DIR = work / "cache"
    repo_manager._CACHE_PDFS = work / "cache" / "pdfs"
    repo_manager._CACHE_INDEX = work / "cache" / "cache_index.json"
    repo_manager._RECYCLE_DIR = work / "repository" / ".recycle"
    llm_usage._USAGE_PATH = work / "usage.sqlite3"

    def guard(event, values):
        paths = []
        if event == "open":
            path, mode, flags = values
            if isinstance(path, (str, bytes, os.PathLike)) and (
                mode and any(c in mode for c in "wax+") or
                flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)):
                paths = [path]
        elif event in {"os.mkdir", "os.remove", "os.rmdir"}:
            paths = [values[0]]
        elif event in {"os.rename", "os.link", "os.symlink"}:
            paths = values[:2]
        for path in paths:
            if os.fsdecode(path).lower() == os.devnull.lower():
                continue
            from paperpilot.file_paths import logical_path
            if not logical_path(os.fsdecode(path)).resolve().is_relative_to(work):
                raise PermissionError("Live validation write outside its isolated directory")
    sys.addaudithook(guard)

    expected_se = math.sqrt((4/38)*(1-4/38)/38 + (7/41)*(1-7/41)/41)
    cases = [dict(name="中文合成队列", mode="direct_edit", filename="observations.csv",
        material="group,n,events\nA,38,4\nB,41,7\n",
        objective="自主分成资料读取、复算、报告和核验阶段。读取工作区 observations.csv，"
            "比较合成观察性队列 A、B 的事件比例，使用 calculate 复算独立二项比例差的标准误。"
            "有人声称标准误为0.11347，请核对而非照抄。写一份不超过600字的 report.md，"
            "含比例、标准误和缺少随机分组/混杂校正的限制。不联网，不入库，不执行代码。",
        criteria=["实际读取 observations.csv 并用 calculate 复算标准误",
                  "report.md 给出正确比例与标准误，指出观察性证据不能证明因果",
                  "核对已保存报告的实际内容，报告交付和资料范围"], expected=expected_se),
        dict(name="English synthetic photonics", mode="read_only", filename="measurements.csv",
        material="sample,transmitted,incident\nA,30,32\nB,44,50\n",
        objective="Autonomously plan and execute a read-only analysis of measurements.csv. "
            "Read the actual data, use calculate to recompute both transmission ratios and their difference, "
            "then review the evidence before submitting completion. Provide a concise English answer "
            "with the values and explain why two aggregate counts without uncertainty cannot prove "
            "general robustness. Do not write files, access paper sources or run code.",
        criteria=["Actually read measurements.csv and calculate both ratios and their difference",
                  "The final answer gives correct numerical results and states uncertainty/generalization limits"],
        expected=30/32)]
    if interaction:
        cases = [dict(name="中文文件审批说明", mode="ask_edit", filename="observations.csv",
            material="group,n,events\nA,38,4\nB,41,7\n",
            objective="请读取 observations.csv，核对两个观察性队列的事件比例，使用 calculate 计算比例差的标准误。"
                "把正确数值和不能推断因果的限制保存为 report.md，报告不超过600个字符，并重新读取确认。"
                "修改前用自然语言说明保存报告的目的和对我的帮助。先了解实际资料，根据需要维护任务清单；"
                "无需预先划分固定阶段。不联网，不入库，不执行代码。最终回复简短说明交付结果。",
            criteria=["实际读取 observations.csv，并用 calculate 核对两组比例及独立二项比例差的标准误",
                "审批说明解释本次写入目的；report.md 不超过600字符，正确说明比例、标准误和因果限制",
                "重新核对保存文件并给出自然、易懂的交付答复"], expected=expected_se),
            dict(name="English clarification and continuation", mode="read_only", filename="measurements.csv",
            material="value\n100\n120\n150\n",
            objective="Read measurements.csv. Its numeric values have no documented physical units. "
                "Ask me one concise question using request_user_input before deciding how to interpret these values. "
                "After my reply, use calculate to compute the mean and provide a short English answer with "
                "the numerical value and a precise limitation. Proceed naturally; a pre-written plan is optional. "
                "Do not write files, access paper sources or execute code.",
            criteria=["Read the actual measurements and explicitly obtain my clarification through request_user_input",
                "Calculate the mean correctly, respect my answer and avoid inventing physical units"],
            expected=(100+120+150)/3)]
        if args.case == "interaction_chinese":
            cases = cases[:1]
    elif args.case != "all":
        cases = cases[:1] if args.case == "chinese" else cases[1:]
    evidence = dict(timestamp=datetime.now(timezone.utc).isoformat(), synthetic_materials=True,
        real_provider=True, provider=client.provider, model=model,
        limits=dict(seconds=120, requests=25, tokens=180000), cases=[],
        scope="Two bounded live goals; not hours-long stability, native input or all-provider research quality")
    with patch("paperpilot.config.load_config", return_value=settings):
        for case in cases:
            project = library.create_project(case["name"], "Synthetic long-task validation")
            service = AIService()
            cm = service.get_conversation(project.id, project.name)
            workspace = default_workspace(cm)
            workspace.mkdir(parents=True)
            (workspace / case["filename"]).write_bytes(case["material"].encode("utf-8"))
            original_files = {p.name for p in workspace.iterdir()}
            create_task(cm, project.id, case["objective"], case["criteria"], workspace,
                        case["mode"], evidence["limits"])
            run = AgentRun(cm, project.id, case["objective"], operation="loop")
            controller = TaskController(service, cm, run)
            approvals, questions = [], []
            def approve(identity, details, respond):
                approvals.append(copy.deepcopy(details))
                respond(details["request_id"], True)  # Explicit synthetic fixture decision, not human UI proof.
            def answer(identity, details, respond):
                questions.append(copy.deepcopy(details))
                respond(details["request_id"], {q["id"]: "Report only the numeric mean of these unitless counts. Do not infer physical units."
                    for q in details["questions"]})
            if interaction:
                controller.on_approval, controller.on_question = approve, answer
            error = None
            try:
                controller.execute()
            except Exception as exc:
                error = type(exc).__name__  # SDK error text may contain sensitive fields.
            finally:
                run.finish()
            state = controller.state
            values = []
            for entry in state["evidence"]:
                if entry["tool"] == "calculate" and entry["ok"]:
                    data = controller._evidence_data(entry)
                    values.append(json.loads(data["content"]).get("value"))
            numeric = any(type(v) in (int, float) and math.isclose(v, case["expected"], rel_tol=1e-5) for v in values)
            text = ((workspace / "report.md").read_text(encoding="utf-8") if (workspace / "report.md").exists()
                    else state["summary"])
            boundary = ({p.name for p in workspace.iterdir()} == original_files if case["mode"] == "read_only"
                        else (workspace / "report.md").is_file())
            interaction_ok = (bool(approvals) and all(a["preview"].get("reason", "").strip() for a in approvals)
                and len(text) <= 600) if interaction and case["mode"] == "ask_edit" else (
                bool(questions) and bool(state.get("user_answers"))) if interaction else True
            row = dict(name=case["name"], mode=case["mode"], status=state["status"], reason=state["reason"],
                error=error, used=state["used"], plan=state["plan"], tools=[e["tool"] for e in state["evidence"]],
                verification=state["verification"], numerical_tool_check=numeric, file_boundary=boundary,
                approvals=approvals, questions=questions, synthetic_human_decisions=interaction,
                result=text, interaction_check=interaction_ok, success=state["status"] == "completed" and numeric and boundary and interaction_ok,
                review="Actual numerical tool value checked; scientific wording still needs human review")
            evidence["cases"].append(row)
            print(json.dumps({k: row[k] for k in ("name", "status", "reason", "used", "success")}, ensure_ascii=False), flush=True)
        (work / "live-result.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print("LIVE EVIDENCE " + str(work / "live-result.json"), flush=True)
    if not all(row["success"] for row in evidence["cases"]):
        raise SystemExit("Live goal acceptance incomplete; evidence retained, no automatic rerun.")


if __name__ == "__main__":
    main()
