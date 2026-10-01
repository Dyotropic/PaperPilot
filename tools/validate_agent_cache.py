"""Bounded DeepSeek experiment. --live makes SIX paid requests with synthetic text.

Does not change config, user chats, papers or the application usage ledger.
Evidence: validation_evidence/agent_cache_20261001_live.json (counters only).
"""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="authorize the bounded six-request live experiment")
    args = parser.parse_args()
    if not args.live:
        parser.print_help()
        return
    from paperpilot import conversation, llm_usage
    from paperpilot.ai_service import AIService
    from paperpilot.llm_client import get_client
    from paperpilot.llm_usage import UsageStore, usage_scope
    client = get_client("chat")
    if client is None or client.provider != "deepseek" or not client.is_available:
        raise SystemExit("当前对话服务未配置为 DeepSeek；未调用其他服务商，也未修改配置。")
    work = ROOT / ".validation-agent-cache-20261001"
    work.mkdir(exist_ok=True)
    os.environ.update(TEMP=str(work), TMP=str(work))
    conversation._REPO_ROOT = work / "repository"
    llm_usage._USAGE_PATH = work / "usage.sqlite3"
    # Long, entirely synthetic history makes prefix disruption measurable.
    history = []
    for i in range(6):
        history.extend([
            dict(role="user", content=f"Synthetic discussion {i}.\n" +
                 "The fictitious experiment compares optical measurements under identical conditions. "
                 "Reported differences are synthetic and cannot establish a scientific finding. " * 100),
            dict(role="assistant", content=f"Synthetic response {i}: remember the measurement assumptions."),
        ])
    papers = [dict(title="Synthetic optical paper A", authors="Synthetic Author A", year=2026,
                   abstract="Synthetic findings about optical measurement stability."),
              dict(title="Synthetic optical paper B", authors="Synthetic Author B", year=2026,
                   abstract="Synthetic comparison of optical measurement protocols.")]
    project_name, topic = "缓存对照实验", "Synthetic optical measurement study"
    base = AIService._CHAT_SYSTEM + f"\n\n当前课题：{project_name}\n课题描述：{topic}"
    store = UsageStore()
    started_id = store.records(limit=1)[0]["id"] if store.records(limit=1) else 0
    evidence = dict(timestamp=datetime.now(timezone.utc).isoformat(), provider=client.provider,
                    model=client.model, synthetic=True, output_limit=64,
                    requests=[], note="Finite control experiment; not a production-wide cache guarantee.")
    baseline_id = "baseline_" + uuid.uuid4().hex
    baseline = conversation.ConversationManager(baseline_id)
    for m in history:
        (baseline.add_user_message if m["role"] == "user" else baseline.add_assistant_message)(m["content"])
    print(f"Live experiment: {client.provider}/{client.model}; 3 baseline + 3 stable-prefix calls", flush=True)
    for step, selected in enumerate(([papers[0]], [], [papers[1]])):
        question = f"Synthetic validation turn {step}. Do not take actions. Reply only OK."
        baseline.add_user_message(question, paper_details=selected or None)
        catalog = [f"- {p['title']} ({p['authors'].split(',')[0]}, {p['year']})" for p in selected] or None
        messages = baseline.build_api_messages(base, catalog)
        with usage_scope(project_id=-1, session_id=baseline_id, task="benchmark"):
            result = client.chat(messages, max_tokens=64, timeout=60, thinking=False, retries=0)
        if not result.content or result.usage is None:
            raise RuntimeError("DeepSeek did not return a successful response with usage; stopping experiment")
        baseline.add_assistant_message(result.content)
        item = dict(variant="baseline", step=step, usage=asdict(result.usage), elapsed_ms=result.elapsed_ms)
        evidence["requests"].append(item)
        print(json.dumps(item), flush=True)
        time.sleep(3)

    class BoundedService(AIService):
        def _call_api_full(self, messages, temperature=.3, max_tokens=2000, timeout=120, model=None, thinking=None):
            return super()._call_api_full(messages, temperature, 64, 60, model, False)

    service = BoundedService()
    cm = service.create_session(-2, project_name, topic)
    for m in history:
        (cm.add_user_message if m["role"] == "user" else cm.add_assistant_message)(m["content"])
    with patch("paperpilot.ai_service.get_task_model_override", return_value=""):
        for step, selected in enumerate(([papers[0]], [], [papers[1]])):
            result = service.chat(-2, project_name,
                                  f"Synthetic validation turn {step}. Do not take actions. Reply only OK.",
                                  topic_desc=topic, papers=selected, session_id=cm.session_id)
            rows = store.records(-2, cm.session_id, "chat", limit=1)
            if not result["reply"] or not rows or rows[0]["input_tokens"] is None:
                raise RuntimeError("Stable-prefix request failed or usage missing; stopping experiment")
            row = rows[0]
            item = dict(variant="stable_prefix", step=step,
                        usage={k: row[k] for k in asdict(llm_usage.TokenUsage())},
                        elapsed_ms=row["elapsed_ms"], prefix_state=row["prefix_state"],
                        common_messages=row["common_messages"])
            evidence["requests"].append(item)
            print(json.dumps(item), flush=True)
            time.sleep(3)
    for variant in ("baseline", "stable_prefix"):
        rows = [r for r in evidence["requests"] if r["variant"] == variant]
        for label, sample in ((variant, rows), (variant + "_continuations", rows[1:])):
            hits = sum(r["usage"]["cache_hit_tokens"] for r in sample)
            inputs = sum(r["usage"]["input_tokens"] for r in sample)
            evidence[label] = dict(input_tokens=inputs, cache_hit_tokens=hits, cache_ratio=hits / inputs)
    evidence["ledger_records"] = [r for r in store.records(limit=100) if r["id"] > started_id]
    destination = ROOT / "validation_evidence" / "agent_cache_20261001_live.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: evidence[k] for k in ("baseline", "stable_prefix", "baseline_continuations", "stable_prefix_continuations")}), flush=True)
    print("Evidence saved (no prompts or credentials)", flush=True)


if __name__ == "__main__":
    main()
