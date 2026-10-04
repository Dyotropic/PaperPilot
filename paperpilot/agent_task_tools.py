"""Native task tools: plans, bounded files and existing research services."""
import copy
import json
from pathlib import Path
import uuid

from paperpilot.agent_runtime import checkpoint
from paperpilot.agent_workspace import MODES, digest


def obj(properties, required=None):
    return dict(type="object", properties=properties, required=required or [], additionalProperties=False)


def string(maximum=4000):
    return dict(type="string", maxLength=maximum)


def integer(low, high):
    return dict(type="integer", minimum=low, maximum=high)


def array(items, low=0, high=30):
    return dict(type="array", items=items, minItems=low, maxItems=high)


def tool(name, description, parameters):
    return dict(type="function", function=dict(name=name, description=description, parameters=parameters))


_REFS = array(string(60), 1, 30)
TOOLS = [
    tool("read_task", "读取持久化目标、验收条件、计划、累计用量和证据索引。", obj({})),
    tool("read_evidence", "按字符范围读取当前任务原始工具证据，start 从 0 开始。",
         obj({"evidence_id": string(60), "start": integer(0, 1000000), "count": integer(1, 8000)}, ["evidence_id"])),
    tool("set_plan", "按需要维护动态任务清单，可先探索再规划、只列当前已知事项。已完成事项保留记录。",
         obj({"steps": array(obj({"id": string(60), "title": string(300),
              "depends_on": array(string(60), 0, 30)}, ["id", "title", "depends_on"]), 1, 30)}, ["steps"])),
    tool("update_step", "更新任务事项。done 须附工具证据；不再适用的未完成事项可标为 cancelled。",
         obj({"id": string(60), "status": dict(type="string", enum=["pending", "running", "done", "cancelled"]),
              "evidence_ids": array(string(60), 0, 30)}, ["id", "status", "evidence_ids"])),
    tool("list_files", "列出工作区文本文件，最多 200 条；不跟随链接。",
         obj({"directory": string(1024)})),
    tool("search_files", "在工作区文本内查找字面片段，最多 200 条匹配。",
         obj({"query": string(200), "directory": string(1024)}, ["query"])),
    tool("read_file", "分行读取 UTF-8 文件，保留文件哈希。修改前必须读取最新版本。",
         obj({"path": string(1024), "start": integer(1, 1000000), "lines": integer(1, 300)}, ["path"])),
    tool("read_attachment", "重读本任务原始不可变附件摘录并标明范围；原生图片在完整工具批次后提供。",
         obj({"index": integer(0, 19), "start": integer(0, 1000000), "count": integer(1, 8000)}, ["index"])),
    tool("write_file", "创建/替换工作区 UTF-8 文本；已有文件须先读，遵循权限审批。",
         obj({"path": string(1024), "content": string(18000), "reason": string(1000)}, ["path", "content"])),
    tool("edit_file", "精确替换已有文件的唯一匹配片段；先读取，审批后复核版本。",
         obj({"path": string(1024), "old_text": string(10000), "new_text": string(10000), "reason": string(1000)},
             ["path", "old_text", "new_text"])),
    tool("read_library", "读取当前课题文献库，返回可分页读取的数据集和真实条数。", obj({})),
    tool("search_papers", "调用现有多源并行检索；每源内部顺序执行。返回持久化数据集。",
         obj({"primary_kw": array(string(200), 1, 10), "secondary_kw": array(string(200), 0, 10),
              "regular_kw": array(string(200), 0, 10),
              "sources": array(dict(type="string", enum=["arxiv", "openalex", "europepmc"]), 1, 3),
              "max_per_source": integer(1, 400), "year_min": string(4), "year_max": string(4)},
             ["primary_kw", "sources", "max_per_source"])),
    tool("read_dataset", "分批读取检索/文献库/排序资料，index 从 0 起。",
         obj({"dataset_id": string(60), "start": integer(0, 100000), "count": integer(1, 10)}, ["dataset_id"])),
    tool("rank_papers", "复用现有粗排与 Cross-Encoder 精排，不改变权重；返回新的排序数据集。",
         obj({"dataset_id": string(60), "query": string(3000), "top_k": integer(1, 100),
              "ce_candidates": integer(1, 400)}, ["dataset_id", "query", "top_k", "ce_candidates"])),
    tool("score_papers", "按摘要调用已有 AI 打分，不自动写入文献库；最多 50 篇。",
         obj({"dataset_id": string(60), "topic": string(3000), "max_papers": integer(1, 50)},
             ["dataset_id", "topic", "max_papers"])),
    tool("save_to_library", "把数据集指定 index 的论文保存到当前课题，受同一写权限控制；不下载 PDF。",
         obj({"dataset_id": string(60), "indices": array(integer(0, 100000), 1, 100), "reason": string(1000)},
             ["dataset_id", "indices"])),
    tool("request_user_input", "确需用户选择或补充信息时提出简短问题；可给 2–3 个选项，也接受自由回复。不要把它当修改批准。",
         obj({"questions": array(obj({"id": string(60), "question": string(1000),
              "options": array(obj({"label": string(100), "description": string(300)}, ["label"]), 2, 3)},
              ["id", "question"]), 1, 3)}, ["questions"])),
    tool("finish_task", "提出完成并独立验收全部用户条件；任务清单可选，有清单时应完成或明确撤销过时事项。摘要面向用户，引用来源名称和文件路径。",
         obj({"summary": string(10000), "criteria": array(obj({"index": integer(0, 29),
              "evidence_ids": _REFS}, ["index", "evidence_ids"]), 1, 30)}, ["summary", "criteria"])),
    tool("report_blocker", "报告连续失败后无法推进的具体原因和所需用户输入，暂停而非完成。",
         obj({"reason": string(2000), "evidence_ids": array(string(60), 0, 30)}, ["reason", "evidence_ids"])),
]
MUTATIONS = {"write_file", "edit_file", "save_to_library"}
STATE_TOOLS = {"read_task", "set_plan", "update_step", "finish_task", "report_blocker", "request_user_input"}


def validate(value, schema, location="arguments"):
    """Validate the schema subset we advertise; no implicit casts/default actions."""
    kind = schema["type"]
    correct = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "integer": type(value) is int}[kind]
    if not correct:
        raise ValueError(f"{location} 类型无效")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{location} 取值无效")
    if kind == "object":
        if set(value) - set(schema["properties"]) or set(schema["required"]) - set(value):
            raise ValueError(f"{location} 字段缺失或包含未知字段")
        for key, item in value.items():
            validate(item, schema["properties"][key], f"{location}.{key}")
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100):
            raise ValueError(f"{location} 条数超限")
        for item in value:
            validate(item, schema["items"], location)
    elif kind == "string":
        if len(value) > schema.get("maxLength", 4000):
            raise ValueError(f"{location} 文本超限")
    elif not schema["minimum"] <= value <= schema["maximum"]:
        raise ValueError(f"{location} 数值超限")


class TaskTools:
    def __init__(self, controller):
        self.owner = controller

    def dataset(self, papers, **extra):
        key = uuid.uuid4().hex
        owner = self.owner
        directory = owner.asset_directory
        owner._asset_guard()
        from paperpilot.file_paths import io_path
        io_path(directory).mkdir(parents=True, exist_ok=True)
        from paperpilot.repo_manager import atomic_write_text
        data = json.dumps(papers, ensure_ascii=False, allow_nan=False)
        if len(data.encode("utf-8")) > 64 * 1024 * 1024:
            raise ValueError("数据集超过 64 MiB，请收敛检索范围；没有截取记录冒充完整结果")
        atomic_write_text(directory / f"{key}.json", data)
        with owner.lock:
            owner.state["datasets"][key] = dict(count=len(papers), bytes=len(data.encode("utf-8")), sha256=digest(data.encode("utf-8")))
            owner.save()
        return dict(dataset_id=key, count=len(papers), preview=self.project(papers[:3]), **extra)

    def load_dataset(self, key):
        owner = self.owner
        if key not in owner.state["datasets"]:
            raise ValueError("数据集不属于当前目标")
        path = owner.asset_directory / f"{key}.json"
        if not path.resolve().is_relative_to(owner.cm.storage_directory.resolve()):
            raise ValueError("数据集目录越界")
        from paperpilot.agent_workspace import linked
        if any(linked(p) for p in (path, *path.parents)):
            raise ValueError("数据集目录包含链接")
        from paperpilot.file_paths import io_path
        path = io_path(path)
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("数据集超限或损坏")
        data = path.read_bytes()
        if digest(data) != owner.state["datasets"][key]["sha256"]:
            raise ValueError("数据集已变化或损坏，不能继续引用")
        papers = json.loads(data)
        if not isinstance(papers, list) or any(not isinstance(p, dict) for p in papers):
            raise ValueError("数据集格式损坏")
        return papers

    @staticmethod
    def project(papers):
        return [dict(index=p.get("_loop_index", i), **{k: p.get(k) for k in
            ("title", "authors", "year", "doi", "url", "source", "total_score", "ai_score")},
            abstract=str(p.get("abstract") or "")[:2000],
            abstract_truncated=len(str(p.get("abstract") or "")) > 2000) for i, p in enumerate(papers)]

    def execute(self, name, args):
        o = self.owner
        w = o.workspace
        checkpoint()
        if name == "read_task":
            return o.reminder()
        if name == "read_evidence":
            evidence = o.evidence_for([args["evidence_id"]])[0]
            return o._evidence_data(evidence, args.get("start", 0), args.get("count", 8000))
        if name == "set_plan":
            return o.set_plan(args["steps"])
        if name == "update_step":
            return o.update_step(args)
        if name == "list_files":
            return w.list(args.get("directory", ""))
        if name == "search_files":
            if not args["query"].strip():
                raise ValueError("query 不能为空")
            return w.list(args.get("directory", ""), args["query"])
        if name == "read_file":
            return w.read(args["path"], args.get("start", 1), args.get("lines", 150))
        if name == "read_attachment":
            from paperpilot.agent_attachments import read_asset, api_message
            attachments = (o.original_input() or {}).get("attachments", [])
            index = args["index"]
            if index >= len(attachments):
                raise ValueError("附件 index 不属于当前任务原始输入")
            ref = attachments[index]
            read_asset(o.cm.storage_directory, ref)
            if ref.get("images"):
                api_message(o.cm.storage_directory, dict(role="user", content="重新读取原附件图片", attachments=[ref]))
                o.pending_attachment_observations[index] = copy.deepcopy(ref)
            text = ref.get("excerpt", "")
            start, count = args.get("start", 0), args.get("count", 8000)
            return dict(index=index, name=ref["name"], scope=ref["scope"], warnings=ref.get("warnings", []),
                content=text[start:start+count], excerpt_chars=len(text), truncated=start+count < len(text),
                images=len(ref.get("images", [])), image_note="原生图片随下一次请求提供，字节不存入工具结果")
        if name in {"write_file", "edit_file"}:
            proposal = (w.propose(args["path"], content=args["content"]) if name == "write_file" else
                        w.propose(args["path"], old=args["old_text"], new=args["new_text"]))
            authorization = o.authorize(name, dict(path=proposal.path, before_hash=proposal.before_hash,
                after_hash=proposal.after_hash, diff=proposal.diff, new=proposal.new,
                reason=args.get("reason", "")))
            o.intent(dict(kind="file", path=proposal.path, before_hash=proposal.before_hash,
                          after_hash=proposal.after_hash, tool_call_id=o.call_id))
            checkpoint()
            # An approved preview must never be replaced by subsequent model output.
            result = w.apply(proposal)
            result.update(reason=args.get("reason", ""), authorization=authorization)
            o.commit_intent(result)
            return result
        if name == "read_library":
            from paperpilot import library
            papers = library.get_project_papers(o.project_id) if o.project_id else []
            return self.dataset(papers, source="current_project")
        if name == "read_dataset":
            papers = self.load_dataset(args["dataset_id"])
            start, count = args.get("start", 0), args.get("count", 5)
            batch = [dict(p, _loop_index=start + i) for i, p in enumerate(papers[start:start + count])]
            return dict(dataset_id=args["dataset_id"], count=len(papers), start=start,
                        returned=len(batch), papers=self.project(batch),
                        truncated=start + len(batch) < len(papers))
        if name == "search_papers":
            from paperpilot.config import load_config
            from paperpilot.fetcher import (fetch_with_cascade, fetch_multi_primary, deduplicate,
                                           fetch_arxiv, fetch_openalex, fetch_europepmc)
            from paperpilot.search_filters import SearchFilters
            from paperpilot.search_service import collect_sources
            settings = load_config().get("data_sources", {}) or {}
            sources = args["sources"]
            if len(set(sources)) != len(sources) or any(not settings.get(s) for s in sources):
                raise ValueError("数据源重复或未在设置中启用")
            year_min, year_max = args.get("year_min", ""), args.get("year_max", "")
            filters = SearchFilters.from_raw(year_min, year_max)
            from paperpilot.mt_translator import translate_all_terms
            from paperpilot.llm_client import tools_scope
            with tools_scope():
                primary, secondary, regular = translate_all_terms(args["primary_kw"],
                    args.get("secondary_kw", []), args.get("regular_kw", []))
            if not primary or any(not term.strip() for term in primary):
                raise ValueError("主关键词未成功翻译，请修正或提供英文术语；未提交空查询")
            papers, errors = collect_sources(sources=sources, primary_kw=primary,
                secondary_kw=secondary, regular_kw=regular,
                description="", max_per=args["max_per_source"], year_min=year_min, year_max=year_max,
                filters=filters if filters.active else None, cascade=fetch_with_cascade, multi_primary=fetch_multi_primary,
                description_fetchers=dict(arxiv=fetch_arxiv, openalex=fetch_openalex, europepmc=fetch_europepmc),
                parallel=(load_config().get("search", {}) or {}).get("parallel_sources", True))
            checkpoint()
            papers = deduplicate(papers)
            if not papers and errors:
                raise ValueError("所有请求未返回论文：" + json.dumps(errors, ensure_ascii=False))
            return self.dataset(papers, errors=errors, partial=bool(errors))
        if name == "rank_papers":
            from paperpilot.indexer import rank_papers
            if args["ce_candidates"] < args["top_k"]:
                raise ValueError("ce_candidates 不能小于 top_k")
            papers = self.load_dataset(args["dataset_id"])
            ranked = rank_papers(args["query"], papers, top_k=args["top_k"], ce_candidates=args["ce_candidates"])
            return self.dataset([dict(p, _loop_score=float(score)) for p, score in ranked])
        if name == "score_papers":
            papers = self.load_dataset(args["dataset_id"])
            from paperpilot.llm_client import tools_scope
            with tools_scope():
                scores = o.service.score_papers(args["topic"], papers, max_papers=args["max_papers"])
            if papers and not scores:
                raise ValueError("AI 打分没有返回有效结果")
            scored = [dict(p) for p in papers]
            for row in scores:
                index = row.get("index")
                if type(index) is int and 0 <= index < len(scored):
                    scored[index].update(ai_score=row.get("ai_score"), ai_reason=row.get("ai_reason"))
            return self.dataset(scored, scored_count=len(scores))
        if name == "save_to_library":
            from paperpilot import library
            if not o.project_id or not library.get_project(o.project_id):
                raise ValueError("请先选择有效课题")
            papers = self.load_dataset(args["dataset_id"])
            indices = args["indices"]
            if len(set(indices)) != len(indices) or any(i >= len(papers) for i in indices):
                raise ValueError("论文 index 重复或越界")
            selected = [papers[i] for i in indices]
            authorization = o.authorize(name, dict(project_id=o.project_id, count=len(selected),
                                  titles=[p.get("title", "") for p in selected], reason=args.get("reason", "")))
            o.intent(dict(kind="library", dataset_id=args["dataset_id"], indices=indices,
                          tool_call_id=o.call_id))
            checkpoint()
            if not library.get_project(o.project_id):
                raise ValueError("确认之后课题已移除，入库未执行")
            scores = [(p, p.get("_loop_score", p.get("total_score", 0)) or 0) for p in selected]
            added, pdf_updated = library.save_papers_to_project(o.project_id, selected, scores=scores)
            result = dict(project_id=o.project_id, selected=len(selected), added=added, pdf_updated=pdf_updated,
                          reason=args.get("reason", ""), authorization=authorization)
            o.commit_intent(result)
            return result
        if name == "finish_task":
            return o.verify_completion(args)
        if name == "report_blocker":
            return o.report_blocker(args)
        if name == "request_user_input":
            return o.ask_user(args["questions"])
        if name == "calculate":
            from paperpilot.agent_team import calculate
            return calculate(args)
        if name == "team_dispatch":
            from paperpilot.agent_team import AgentTeam, parse_team_request, result_observation
            tasks = parse_team_request("[TEAM]" + json.dumps(args, ensure_ascii=False) + "[/TEAM]")
            if o.team and o.team.data["batches"] >= o.team.settings["max_batches"]:
                if any("agent_id" in t for t in tasks):
                    raise ValueError("本团队的追问预算已用完，请以 name 创建新团队；旧资料标识保持原义")
                o.team.close("completed")
                o.team = None
            if o.team is None:
                o.team = AgentTeam(o.cm, o.project_id, o.run, o.team_material(),
                                  lambda: o.service._get_client("chat"), o.model, o.on_team_change,
                                  team_id=uuid.uuid4().hex)
            rows = o.team.dispatch(tasks)
            return dict(review_required=True, observation=result_observation(rows),
                        agents=[dict(agent_id=r["agent_id"], state=r["state"]) for r in rows])
        raise ValueError("工具未实现")
