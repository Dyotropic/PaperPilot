"""Continuous cross-module workflow: production adapters, local HTTP/SDK, cached CE.

Three domains, Chinese/English, 400/source -> 100 CE -> 50 displayed records ->
SQLite/library -> CE re-sort -> AI score/read/chat -> exports/graph/PDF/import.
Source metadata and model replies are deterministic fixtures, not cloud quality.
"""
import hashlib
import argparse
import json
import re
import threading
import time
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

import requests
import fitz
from pages import search_page as search
from paperpilot import config, library, indexer, keywords, ai_service, export, graph_service, downloader, repo_manager, local_import
from paperpilot.agent_runtime import AgentRun, run_scope
from paperpilot.search_metrics import SearchTrace
from paperpilot.sources import arxiv_source as ax, europepmc_source as ep, openalex_source as oa

assert Path(indexer._CE_PATH).is_dir(), "Cached CE required; this probe must not download models"
parser=argparse.ArgumentParser()
parser.add_argument("--case-index",type=int,choices=(0,1,2))
args=parser.parse_args()
cases = [("自主机器人导航", "研究自主机器人导航的路径规划与避障", "自主机器人导航", "robot navigation"),
         ("Cancer immunotherapy", "cancer immunotherapy", "cancer immunotherapy", "cancer immunotherapy"),
         ("钙钛矿太阳能电池", "研究钙钛矿太阳能电池的界面钝化与稳定性", "钙钛矿太阳能电池", "perovskite solar cells")]
case_index = 0
http_calls, model_calls, reports = [], [], []
pdf = Path.cwd()/"workflow.pdf"
# The PDF skill's ReportLab generator is absent in this environment; reuse the
# project's installed PyMuPDF for this disposable text fixture, without installs.
doc=fitz.open(); doc.set_metadata({"title":"Workflow original paper","author":"Alice Example"})
page=doc.new_page();page.insert_text((50,60),"Workflow original paper",fontsize=18)
page.insert_text((50,95),"Abstract")
for line in range(20): page.insert_text((50,120+line*22),"We study robot navigation and reproducible scientific evidence.")
doc.save(pdf);doc.close()
pdf_bytes = pdf.read_bytes()


def paper(source, i):
    term = cases[case_index][3]
    title = f"{source} {hashlib.sha256((source+term+str(i)).encode()).hexdigest()[:24]} {term}"
    return dict(title=title, abstract=(f"We investigate {term} with rigorous experiments and reproducible methods. "*4),
                doi=f"10.0/{case_index}-{source}-{i}", authors="Alice Example", year=2024)


def work(i):
    p = paper("openalex",i); inv = {}
    for pos, token in enumerate(p["abstract"].split()): inv.setdefault(token,[]).append(pos)
    return dict(id=f"https://openalex.org/W{case_index+1}{i:05d}", title=p["title"], doi="https://doi.org/"+p["doi"],
                publication_year=2024, type="article", cited_by_count=400-i,
                authorships=[{"author":{"display_name":"Alice Example"}}], abstract_inverted_index=inv,
                primary_location={"source":{"display_name":"Nature"},"landing_page_url":"https://doi.org/"+p["doi"]},
                referenced_works=[f"https://openalex.org/W{case_index+1}{i+1:05d}"] if i < 399 else [],
                keywords=[{"display_name":cases[case_index][3]},{"display_name":"reproducibility"}])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *args): pass
    def send(self, body, mime="application/json"):
        data = body if isinstance(body,bytes) else json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type",mime); self.send_header("Content-Length",str(len(data)))
        self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        parsed=urlparse(self.path); q=parse_qs(parsed.query)
        http_calls.append((parsed.path, time.perf_counter(), self.client_address[1]))
        time.sleep(.03)
        if parsed.path == "/arxiv":
            start=int(q.get("start",[0])[0]); n=int(q["max_results"][0]); entries=[]
            for i in range(start,min(400,start+n)):
                p=paper("arxiv",i)
                entries.append(f'''<entry><id>https://arxiv.org/abs/240{case_index+1}.{i:05d}v1</id>
                    <title>{escape(p['title'])}</title><summary>{escape(p['abstract'])}</summary>
                    <published>2024-01-01T00:00:00Z</published><updated>2024-01-01T00:00:00Z</updated>
                    <author><name>Alice Example</name></author><arxiv:primary_category term="cs.AI"/>
                    <category term="cs.AI"/><arxiv:doi>{p['doi']}</arxiv:doi><arxiv:journal_ref>Nature</arxiv:journal_ref></entry>''')
            feed=f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"
                xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
                <title>local</title><id>local</id><updated>2024-01-01T00:00:00Z</updated>
                <opensearch:totalResults>400</opensearch:totalResults>{''.join(entries)}</feed>'''
            self.send(feed.encode(),"application/atom+xml")
        elif parsed.path == "/epmc":
            records=[]
            for i in range(min(400,int(q["pageSize"][0]))):
                p=paper("europepmc",i)
                records.append(dict(id=str(i),source="MED",title=p["title"],abstractText=p["abstract"],doi=p["doi"],
                                    authorString=p["authors"],pubYear="2024",citedByCount=400-i,
                                    journalInfo={"journal":{"title":"Nature"}}))
            self.send({"resultList":{"result":records},"hitCount":400,"nextCursorMark":"end"})
        elif parsed.path == "/oa":
            n=int(q.get("per_page",[100])[0]); start=(int(q.get("page",[1])[0])-1)*n
            self.send({"results":[work(i) for i in range(start,min(400,start+n))],"meta":{"count":400,"next_cursor":None}})
        elif parsed.path.startswith("/pdf/"): self.send(pdf_bytes,"application/pdf")
        else: raise AssertionError(parsed.path)
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        model_calls.append(body); messages=body["messages"]; system=messages[0]["content"]; user=messages[-1]["content"]
        if "scientific translator" in system:
            numbered=re.findall(r"(?m)^(\d+)\. (.+)$",user)
            content="\n".join(f"{i}. {cases[case_index][3]}" for i,_ in numbered)
        elif "待评分论文" in user:
            indices=re.findall(r"(?m)^\[(\d+)\]",user)
            content=json.dumps([dict(index=int(i),score=88,relevance=9,method=8,novelty=7,recency=9,overall="Local fixture evidence") for i in indices])
        elif "阅读笔记" in user:
            content=json.dumps(dict(core_contribution="Measured improvement",method="Controlled experiment",key_evidence="Full text data",
                                    highlights="Reproducible",limitations="Fixture only",scores=dict(novelty=7,rigor=8,significance=7)))
        elif "1-3个最核心" in system: content=cases[case_index][2]
        elif "5-8个有搜索价值" in system: content="reproducibility、controlled experiments"
        else: content="Based on the selected library material, further verification is required."
        if body.get("stream"):
            chunk=dict(id="local-workflow",object="chat.completion.chunk",created=1,model=body["model"],
                       choices=[dict(index=0,delta=dict(role="assistant",content=content),finish_reason=None)])
            last=dict(id="local-workflow",object="chat.completion.chunk",created=1,model=body["model"],
                      choices=[dict(index=0,delta={},finish_reason="stop")],
                      usage=dict(prompt_tokens=100,completion_tokens=40,total_tokens=140))
            payload=("data: "+json.dumps(chunk)+"\n\ndata: "+json.dumps(last)+"\n\ndata: [DONE]\n\n").encode()
            self.send(payload,"text/event-stream"); return
        self.send(dict(id="local-workflow",object="chat.completion",created=1,model=body["model"],
                       choices=[dict(index=0,message=dict(role="assistant",content=content),finish_reason="stop")],
                       usage=dict(prompt_tokens=100,completion_tokens=40,total_tokens=140)))


server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
root=f"http://127.0.0.1:{server.server_port}"
config.save_config({"llm":{"provider":"deepseek","base_url":root+"/v1","api_key":"local-test","model":"workflow-local",
                           "chat_model":"","reasoning_model":"","score_model":"","translation_model":"","keyword_extraction_model":""},
                    "search":{"ce_idle_seconds":300,"parallel_sources":True}})
original_get=requests.Session.get
def route(session,url,*args,**kwargs):
    if url.startswith("https://api.openalex.org/works"): url=root+"/oa"
    return original_get(session,url,*args,**kwargs)

try:
    with ExitStack() as stack:
        stack.enter_context(patch.object(ax.arxiv.Client,"query_url_format",root+"/arxiv?{}"))
        stack.enter_context(patch.object(ax,"_ARXIV_RATE_LIMIT",0))
        stack.enter_context(patch.object(ep,"_EPMC_BASE",root+"/epmc"))
        stack.enter_context(patch.object(requests.Session,"get",route))
        stack.enter_context(patch.object(downloader,"_ARXIV_PDF",root+"/pdf/{}"))
        stack.enter_context(patch.object(downloader,"_new_session",return_value=(None,"")))
        ai=ai_service.AIService()
        for case_index,(name,desc,primary,term) in enumerate(cases):
            if args.case_index is not None and case_index != args.case_index: continue
            print(f"WORKFLOW START {name}",flush=True)
            extracted=keywords.extract_all_keywords(desc)
            assert extracted, (name, extracted)
            trace=SearchTrace(); began=time.perf_counter()
            context=dict(topic_desc=desc,primary_keywords=[primary],secondary_keywords=[],regular_keywords=[],_metrics_trace=trace)
            rows,scores,errors=search._run_pipeline(400,"","",True,True,True,50,100,search_context=context)
            assert not errors, errors
            assert len(rows)==1200, len(rows)
            assert len(scores)==50 and all(p["abstract"] for p,_ in scores)
            timings=trace.snapshot(); assert sum(r["stage"]=="ce_predict" and r["status"]=="ok" for r in timings["stages"])==1
            proj=library.create_project(name,desc)
            assert library.save_papers_to_project(proj.id,[p for p,_ in scores],scores)==(50,0)
            assert library.save_papers_to_project(proj.id,[p for p,_ in scores],scores)==(0,0)
            db=library.get_project_papers(proj.id); assert len(db)==50
            resort=indexer.rank_papers(term,db[:5],top_k=5,ce_candidates=5,primary_kw=[term])
            indexer.release_cross_encoder(); assert library.update_paper_scores(proj.id,resort)==5
            scored=ai.score_papers(desc,db[:3],max_papers=3)
            assert len(scored)==3 and library.update_paper_ai_scores(proj.id,scored,db[:3])==3
            assert library.update_paper_status(db[0]["project_paper_id"],"skimmed")
            assert len(library.get_project_papers(proj.id,status_filter="skimmed"))==1
            for suffix,convert in (("csv",export.to_csv),("bib",export.to_bibtex)):
                output=Path.cwd()/f"{case_index}.{suffix}";export.save_file(convert(db),str(output));assert output.stat().st_size>100
            cm=ai.get_conversation(proj.id,name,desc); run=AgentRun(cm,proj.id,"Analyse selected search results")
            with run_scope(run):
                result=ai.chat(proj.id,name,"Analyse this project",desc,papers=db[:2],project_papers=db,
                               session_id=cm.session_id,include_library_context=True)
            run.finish(); assert result["reply"] and result["session_id"]==cm.session_id
            other=ai.create_session(proj.id,name,desc);assert other.session_id != cm.session_id
            assert ai.get_conversation(proj.id,name,desc,cm.session_id).total_rounds==1
            # Use downloaded original text, then the no-original abstract fallback.
            original=dict(next(p for p in rows if p["source"]=="arxiv"))
            pdf_path=downloader.cache_pdf(original); assert pdf_path and Path(pdf_path).is_file()
            original["pdf_path"]=pdf_path
            text=downloader.extract_pdf_text(Path(pdf_path).read_bytes());assert "reproducible scientific evidence" in text
            archived=repo_manager.import_pdf(original,name);assert archived and Path(archived).is_file()
            original["pdf_path"]=archived;library.save_papers_to_project(proj.id,[original])
            dr=ai.deep_read(original);assert dr.get("_source")=="pdf" and not dr.get("_parse_error"),dr
            assert ai_service.save_deep_read_json(original,dr)
            fallback=dict(title="Abstract only",abstract=paper("openalex",0)["abstract"])
            dr2=ai.deep_read(fallback);assert dr2.get("_source")=="abstract_fallback" and not dr2.get("_parse_error")
            assert ai.deep_read({"title":"No text"})=={}
            imported=local_import.extract_pdf(pdf_path);assert imported and imported["source"]=="local_pdf"
            assert library.save_papers_to_project(proj.id,[imported])[0]==1
            # Cached OpenAlex refs obtained through the search adapter must feed graph facts.
            # API-score sorting can put 0, 50, 100 first. Pick the known citation
            # chain by DOI instead of assuming the first three are consecutive.
            graph_rows=sorted((p for p in rows if p["source"]=="openalex"),
                              key=lambda p:int(p["doi"].rsplit("-",1)[-1]))[:3]
            library.save_papers_to_project(proj.id,graph_rows)
            graph_dois={p["doi"] for p in graph_rows}
            graph_db=[p for p in library.get_project_papers(proj.id) if p["doi"] in graph_dois]
            before=len(http_calls);graph=graph_service.build_graph_data(proj.id,graph_db)
            assert len(http_calls)==before and graph["stats"]["n_cite_edges"]==2,graph["stats"]
            report=dict(topic=name,raw_sources="local HTTP metadata",model="cached real CPU CE",llm="real SDK/local fixture",
                        displayed=len(scores),deduplicated=len(rows),pipeline_seconds=timings["elapsed_seconds"],
                        workflow_seconds=time.perf_counter()-began,graph=graph["stats"],timings=timings)
            reports.append(report);(Path.cwd()/"workflow_report.json").write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding="utf-8")
            print(f"WORKFLOW PASS {name}: 3 sources -> CE -> save/re-sort -> AI -> chat -> exports -> PDF/import -> cached graph",flush=True)
finally:
    indexer.unload_cross_encoder(); server.shutdown();server.server_close();thread.join(2)
