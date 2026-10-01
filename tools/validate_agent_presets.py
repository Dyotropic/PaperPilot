"""--live runs three paid DeepSeek chat/preset calls with synthetic papers.

Preserves the configured chat model and thinking modes. Does not modify user
configuration, conversations, papers or the production usage ledger.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding="utf-8")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    if not parser.parse_args().live:
        parser.print_help()
        return
    work = ROOT / ".validation-agent-presets-20261001"
    work.mkdir(exist_ok=True)
    os.environ.update(TEMP=str(work), TMP=str(work), PAPERPILOT_VALIDATION_ROOT=str(work))
    from paperpilot import conversation, llm_usage
    from paperpilot.ai_service import AIService
    from paperpilot.llm_client import get_client, get_task_model_override
    from paperpilot.llm_usage import UsageStore
    client = get_client("chat")
    if client is None or client.provider != "deepseek" or get_task_model_override("reasoning"):
        raise SystemExit("This bounded probe requires the configured single-step DeepSeek chat path.")
    conversation._REPO_ROOT = work / "repository"
    llm_usage._USAGE_PATH = work / "usage.sqlite3"

    class ProbeService(AIService):
        def _call_api_full(self, messages, temperature=.3, max_tokens=2000, timeout=120, model=None, thinking=None):
            result = client.chat(messages, temperature=temperature, max_tokens=min(max_tokens,6000),
                                 timeout=90, model=model, thinking=thinking, retries=0)
            if not result.content or result.usage is None:
                raise RuntimeError("Model response failed, empty or missing usage; stopping probe")
            return result.content, result.reasoning

    papers = [dict(id=1, title="Synthetic optical calibration study", authors="Synthetic Author A", year=2026,
                   doi="synthetic:calibration", abstract="This is fictional validation data. A simulated optical measurement "
                   "protocol compares temperature-controlled calibration with an uncalibrated baseline. The synthetic results "
                   "suggest lower drift across repeated measurements, but the simulation omits instrument aging and outdoor "
                   "conditions. No empirical scientific finding can be inferred from this fixture."),
              dict(id=2, title="Synthetic optical uncertainty study", authors="Synthetic Author B", year=2026,
                   doi="synthetic:uncertainty", abstract="This is fictional validation data. A simulated uncertainty analysis "
                   "separates detector noise, temperature fluctuations and reference-source variation. Repeat measurements "
                   "are modeled under controlled laboratory conditions. Correlated error and nonstationary instrument "
                   "response remain limitations. These fictional results are used only to validate software context routing.")]
    service = ProbeService()
    name = "虚构业务验收-" + uuid.uuid4().hex[:8]
    topic = "Synthetic optical measurement calibration and uncertainty; no real scientific evidence."
    cm = service.create_session(-3, name, topic)
    stages = [("chat", "用两句中文说明这次讨论需要哪些文献资料。", False, False),
              ("research_overview", "基于文献库梳理研究现状，限三句中文，注明资料为虚构验收数据。", True, True),
              ("research_gaps", "沿用这些文献指出两个研究空白，限两句中文，注明仅依据虚构资料。", True, True)]
    evidence = dict(timestamp=datetime.now(timezone.utc).isoformat(), synthetic=True,
                    provider=client.provider, configured_model=client.model, requests=[],
                    note="Three current chat/preset requests; no baseline or artificial cache warm-up; not a cache-rate guarantee.")
    for operation, prompt, thinking, include in stages:
        reply = service.chat(-3, name, prompt, topic_desc=topic, project_papers=papers,
                             session_id=cm.session_id, thinking_enabled=thinking,
                             include_library_context=include, operation=operation)
        row = UsageStore().records(-3, cm.session_id, "chat", limit=1)[0]
        item = {k: row[k] for k in ("operation", "model", "thinking_mode", "input_tokens", "output_tokens",
                 "reasoning_tokens", "cache_hit_tokens", "cache_miss_tokens", "prefix_state", "common_messages", "elapsed_ms")}
        item["cache_ratio"] = row["cache_hit_tokens"] / row["input_tokens"] if row["input_tokens"] else None
        item["nonempty_reply"] = bool(reply["reply"])
        evidence["requests"].append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
    records = evidence["requests"]
    evidence["weighted_cache_ratio"] = sum(r["cache_hit_tokens"] for r in records) / sum(r["input_tokens"] for r in records)
    path = ROOT / "validation_evidence" / "agent_presets_20261001_live.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Three actual responses and usage recorded; prompts and credentials excluded from evidence.", flush=True)

if __name__ == "__main__":
    main()
