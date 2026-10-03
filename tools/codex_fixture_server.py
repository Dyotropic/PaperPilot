"""Deterministic stdio fixture for subscription bridge tests; never contacts OpenAI."""
import json
import os
from pathlib import Path
import sys
import threading
import time

home = Path(os.environ["CODEX_HOME"])
lock = threading.Lock()
threads, requests = {}, {}
counter = 0


def emit(value):
    with lock:
        print(json.dumps(value, ensure_ascii=True), flush=True)


def notice(method, params):
    emit(dict(method=method, params=params))


def account():
    path = home / "auth.json"
    kind = json.loads(path.read_text()).get("kind") if path.exists() else None
    return dict(type=kind, planType="plus") if kind else None


def log(message):
    with (home / "fixture.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(message, ensure_ascii=True) + "\n")


def finish(tid, text=None):
    state = threads[tid]
    if state.get("cancelled"): return
    text = text or state["text"]
    params = dict(threadId=tid, turnId=state["turn"])
    notice("item/started", dict(params, item=dict(type="agentMessage", id="answer", phase="final_answer")))
    notice("item/reasoning/summaryTextDelta", dict(params, delta="fixture summary"))
    middle = max(1, len(text) // 2)
    notice("item/agentMessage/delta", dict(params, itemId="answer", delta=text[:middle]))
    if "SLOW_STREAM" in state["input"]: time.sleep(.8)
    if state.get("cancelled"): return
    notice("item/agentMessage/delta", dict(params, itemId="answer", delta=text[middle:]))
    notice("item/completed", dict(params, item=dict(type="agentMessage", id="answer", text=text, phase="final_answer")))
    notice("thread/tokenUsage/updated", dict(params, tokenUsage=dict(modelContextWindow=128000,
        total=dict(inputTokens=100, cachedInputTokens=60, outputTokens=20, reasoningOutputTokens=5,totalTokens=120),
        last=dict(inputTokens=40, cachedInputTokens=20, outputTokens=8, reasoningOutputTokens=2,totalTokens=48))))
    notice("turn/completed", dict(threadId=tid, turn=dict(id=state["turn"], status="completed")))


def perform(tid):
    state = threads[tid]
    available = {t["name"] for ns in state["payload"].get("dynamicTools",[]) for t in ns.get("tools",[])}
    params = dict(threadId=tid, turnId=state["turn"])
    if "DISCONNECT" in state["input"]: os._exit(0)
    if "FAIL" in state["input"]:
        notice("turn/completed", dict(threadId=tid, turn=dict(id=state["turn"], status="failed",error=dict(message="quota fixture"))))
    elif "TIMEOUT" in state["input"]:
        return
    elif ("UNAUTHORIZED" in state["input"] or "TOOL" in state["input"] and "calculate" in available
          or "TEAM_SCENARIO" in state["input"] and "team_dispatch" in available
          or "READ_SCENARIO" in state["input"] and "read_source" in available):
        ident = "request-" + tid
        requests[ident] = tid
        name = "shell" if "UNAUTHORIZED" in state["input"] else "calculate"
        arguments = dict(expression="sqrt(9+16)")
        if "TEAM_SCENARIO" in state["input"]:
            name, arguments = "team_dispatch", dict(tasks=[dict(name="方法比较",instruction="READ_SCENARIO: 比较 s1 方法"),
                dict(name="证据核验",instruction="READ_SCENARIO: 核验 s1 证据")])
        elif "READ_SCENARIO" in state["input"]:
            name, arguments = "read_source", dict(source_id="s1",start=0,length=2000)
        state["tool"] = name
        namespace = None if name == "shell" else "paperpilot"
        notice("thread/tokenUsage/updated", dict(params, tokenUsage=dict(total=dict(inputTokens=60,
            cachedInputTokens=40, outputTokens=12,reasoningOutputTokens=3,totalTokens=72),last={},modelContextWindow=128000)))
        emit(dict(id=ident, method="item/tool/call", params=dict(params, callId="call-"+tid,
            tool=name, namespace=namespace, arguments=arguments)))
    else:
        finish(tid)


def handle(message):
    global counter
    method, params, ident = message.get("method"), message.get("params") or {}, message.get("id")
    if not method:
        tid = requests.pop(ident, None)
        if tid:
            kind = threads[tid].get("tool")
            finish(tid, "主 Agent 已审查两项结果。" if kind == "team_dispatch" else
                   "已读取 s1，资料需要独立验证。" if kind == "read_source" else "计算结果是 5。")
        return
    result = {}
    if method == "initialize":
        if (home/"delay-init").exists(): time.sleep(.5)
        result = dict(userAgent="codex-cli/0.159.2",codexHome=str(home))
    elif method == "account/read": result = dict(account=account(), requiresOpenaiAuth=True)
    elif method == "account/login/start": result = dict(type="chatgpt",loginId="login-fixture",authUrl="https://auth.openai.com/fixture")
    elif method == "account/login/cancel": result = dict(status="canceled")
    elif method == "fixture/login/complete":
        (home / "auth.json").write_text('{"kind":"chatgpt"}')
        notice("account/login/completed",dict(loginId="login-fixture",success=True,error=None))
    elif method == "account/logout": (home / "auth.json").unlink(missing_ok=True)
    elif method == "model/list":
        if (home/"delay-model").exists(): time.sleep(.5)
        result = dict(data=[dict(id="fixture-research",model="fixture-research",displayName="Research Fixture",
            isDefault=True,inputModalities=["text","image"],supportedReasoningEfforts=[dict(reasoningEffort=e,description=e) for e in ("low","high")])],nextCursor=None)
    elif method == "thread/start":
        counter += 1; tid = f"thread-{counter}"
        threads[tid] = dict(payload=params)
        if params.get("developerInstructions", "").startswith("DELAY_ACK"): time.sleep(.5)
        result = dict(thread=dict(id=tid),sandbox=dict(type="readOnly",networkAccess=False))
    elif method == "thread/inject_items": threads[params["threadId"]]["history"] = params["items"]
    elif method == "turn/start":
        tid = params["threadId"]
        prompt = "\n".join(p.get("text", "") for p in params["input"])
        history = threads[tid].get("history") or []
        if prompt.startswith("Continue from the supplied") and history and history[-1].get("role") == "user":
            prompt = "\n".join(p.get("text", "") for p in history[-1].get("content") or [])
        instructions = threads[tid]["payload"].get("developerInstructions", "")
        text = "研究回答：fixture。"
        if "两个字母" in prompt: text = "OK"
        elif "core_contribution" in instructions:
            text = json.dumps(dict(core_contribution="仅分析所提供资料",method="实验",key_evidence="观测结果",
                highlights="研究结果",limitations="仅 fixture 验证",scores=dict(novelty=6,rigor=5,significance=7)),ensure_ascii=False)
        elif "reason_relevance" in instructions:
            text = json.dumps([dict(index=0,relevance=8,method=7,novelty=6,recency=9,
                reason_relevance="相关",reason_method="需要对照",reason_novelty="需验证")],ensure_ascii=False)
        elif "scientific translator" in instructions: text = '1. perovskite\n2. interface passivation'
        elif "5-8" in instructions: text = '材料、界面、器件、稳定性、效率'
        elif "关键词提取专家" in instructions: text = '钙钛矿、界面钝化'
        threads[tid].update(turn="turn-"+tid, input=prompt, text=text)
        if "DELAY_TURN" in prompt: time.sleep(.5)
        emit(dict(id=ident,result=dict(turn=dict(id="turn-"+tid,status="inProgress"))))
        threading.Thread(target=perform,args=(tid,),daemon=True).start()
        return
    elif method == "turn/interrupt":
        state = threads.get(params["threadId"])
        if state: state["cancelled"] = True
    if ident is not None: emit(dict(id=ident,result=result))


for line in sys.stdin:
    try:
        message = json.loads(line)
        log(message)
        handle(message)
    except Exception as exc:
        emit(dict(id=message.get("id"),error=dict(code=-32603,message=type(exc).__name__)))
