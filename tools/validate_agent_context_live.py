"""--live authorizes ONE bounded DeepSeek compaction request with synthetic data.

Uses the configured chat model. Does not change user configuration, conversations,
papers, or the production usage ledger. Writes counters and checks, never keys or
the synthetic prompt. No additional requests, retry or automatic continuation.
"""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid
from unittest.mock import patch

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
    from paperpilot import conversation, llm_usage
    from paperpilot.ai_service import AIService
    from paperpilot.llm_client import get_client, get_task_model
    from paperpilot.llm_usage import UsageStore
    client = get_client("chat")
    if not client or client.provider != "deepseek" or not client.is_available:
        raise SystemExit("Requires the configured DeepSeek chat provider; no API requests sent.")
    work = ROOT / ".validation-agent-context-20261002"
    work.mkdir(exist_ok=True)
    os.environ.update(TEMP=str(work), TMP=str(work), PAPERPILOT_VALIDATION_ROOT=str(work))
    conversation._REPO_ROOT = work / "repository"
    llm_usage._USAGE_PATH = work / "usage.sqlite3"
    service = AIService()
    name = "合成上下文实测 " + uuid.uuid4().hex[:8]
    cm = service.get_conversation(990000001, name, "Synthetic optical research; no real scientific result")
    cm.prepare_project_context(name, "Synthetic optical research; no real scientific result")
    for i in range(6):
        cm.add_user_message(f"合成实验 {i}。用户目标：核对光学测量证据，不得把未验证结论当事实。"
            "必须保留 DOI:10.fixture/example 和数值 0.123456789 m，不要四舍五入。\n" +
            "Synthetic measurements are unverified, with fictional authors and no experimental proof. " * 80)
        cm.add_assistant_message("已完成：整理合成测量方案。未完成：实验复现。尚未验证测量结论，不得声称已完成实验。")
    original = cm.build_api_messages(service.chat_system_prompt(cm,name,"Synthetic optical research; no real scientific result"))
    calls = []
    underlying = client.chat
    def bounded(messages, **kwargs):
        if calls:
            raise RuntimeError("One-request budget exhausted")
        calls.append(None)
        kwargs.update(max_tokens=min(2048, kwargs.get("max_tokens",2048)),timeout=90,retries=0)
        result = underlying(messages, **kwargs)
        calls[0] = result
        return result
    with patch("paperpilot.ai_service.get_client", return_value=client), patch.object(client,"chat",side_effect=bounded):
        result = service.compact_context(990000001,name,"Synthetic optical research; no real scientific result",session_id=cm.session_id)
    reply = calls[0] if calls else None
    summary = result.get("record", {}).get("rounds_summary", "")
    after = cm.build_api_messages(service.chat_system_prompt(cm,name,"Synthetic optical research; no real scientific result"))
    headers = ("用户目标与需求", "研究依据与引用", "关键决策与约束", "已完成工作与结果", "未完成工作与下一步", "关键数据与定位信息")
    evidence = dict(timestamp=datetime.now(timezone.utc).isoformat(),synthetic=True,
        requests=len(calls),provider=client.provider,configured_model=get_task_model("chat"),
        response_model=reply.model if reply else None, finish_reason=reply.finish_reason if reply else None,
        status=result["status"],usage=asdict(reply.usage) if reply and reply.usage else None,
        elapsed_ms=reply.elapsed_ms if reply else None,
        before_tokens=result.get("record",{}).get("before_tokens"),after_tokens=result.get("record",{}).get("after_tokens"),
        all_sections_present=all(h in summary for h in headers),
        exact_doi_preserved="10.fixture/example" in summary,
        exact_number_preserved="0.123456789" in summary,
        uncertainty_preserved="未验证" in summary or "尚未验证" in summary,
        stable_system=original[0]==after[0],recent_turns_preserved=original[-4:]==after[-4:],
        original_messages_preserved=len(cm._history)==12,
        task_attribution=UsageStore().records(990000001,cm.session_id)[0]["task"]=="compression" if calls else False)
    output=ROOT/"validation_evidence"/"agent_context_20261002_live.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(evidence,ensure_ascii=False,indent=2))
    if result["status"]!="completed" or not all(evidence[k] for k in (
        "all_sections_present","exact_doi_preserved","exact_number_preserved","uncertainty_preserved",
        "stable_system","recent_turns_preserved","original_messages_preserved","task_attribution")):
        raise SystemExit("Live compaction did not pass acceptance; evidence retained.")


if __name__=="__main__":
    main()
