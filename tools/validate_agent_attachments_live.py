"""One opt-in DeepSeek Flash vision request with synthetic file/image fixtures.

No production session, configuration or usage changes; no retries. Only model
counters and acceptance booleans are retained as evidence, never credentials.
"""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sys

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
    from PIL import Image
    from paperpilot import conversation, llm_usage
    from paperpilot.ai_service import AIService
    from paperpilot.agent_attachments import (prepare_selection, persist_attachments,
        format_attachment_material, validate_request)
    from paperpilot.llm_client import get_client, get_task_model
    from paperpilot.llm_usage import usage_scope
    client = get_client("chat")
    model = get_task_model("chat")
    if not client or client.provider != "deepseek" or not client.is_available:
        raise SystemExit("Requires configured DeepSeek Flash; no request sent.")
    if model not in {"deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}:
        raise SystemExit("Configured chat model is not DeepSeek Flash; no request sent.")
    work = ROOT / ".validation-agent-attachments-live-20261002"
    work.mkdir(exist_ok=True)
    os.environ.update(TEMP=str(work), TMP=str(work))
    conversation._REPO_ROOT = work / "repository"
    llm_usage._USAGE_PATH = work / "usage.sqlite3"
    image_path = work / "synthetic.png"
    Image.new("RGB", (160, 100), "red").save(image_path)
    text_path = work / "synthetic.csv"
    text_path.write_text("parameter,value\nfixture,73\n", encoding="utf-8")
    service = AIService()
    cm = service.get_conversation(990000003, "Synthetic vision acceptance")
    refs = persist_attachments(cm.storage_directory, prepare_selection([image_path, text_path])[0])
    question = '只输出 JSON，color 为图片主体颜色的英文单词（小写），value 为 CSV 的数值。格式：{"color":"...","value":0}。'
    cm.add_user_message(format_attachment_material(refs) + question, display_content=question, attachments=refs)
    messages = cm.build_api_messages(service.chat_system_prompt(cm, "Synthetic vision acceptance"))
    validate_request(messages, client.provider, model)
    with usage_scope(project_id=990000003, session_id=cm.session_id, task="chat", operation="attachment_acceptance"):
        result = client.chat(messages, temperature=0, max_tokens=128, timeout=60,
                             thinking=False, model=model, retries=0)
    try:
        body = result.content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(body)
    except (ValueError, AttributeError):
        parsed = {}
    evidence = dict(timestamp=datetime.now(timezone.utc).isoformat(), synthetic=True,
        requests=1, provider=client.provider, configured_model=model, response_model=result.model,
        finish_reason=result.finish_reason, usage=asdict(result.usage) if result.usage else None,
        elapsed_ms=result.elapsed_ms, image_color_correct=parsed.get("color") == "red",
        text_value_correct=parsed.get("value") == 73)
    output = ROOT / "validation_evidence" / "agent_attachments_20261002_live.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    if not evidence["image_color_correct"] or not evidence["text_value_correct"]:
        raise SystemExit("Live vision/text acceptance failed; counters retained.")


if __name__ == "__main__":
    main()
