"""Library/cache graph -> native ECharts views; controlled references, no network."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

from paperpilot import library, graph_service, graph_window as gw
from paperpilot.sources import openalex_source as oa

work = Path.cwd()
cached = Path(os.environ.get("USERPROFILE") or str(Path.home())) / ".paperpilot_echarts/echarts.min.js"
for name in ("USERPROFILE", "LOCALAPPDATA", "APPDATA"):
    os.environ[name] = str(work / name.lower())
    Path(os.environ[name]).mkdir(exist_ok=True)
os.environ["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
assert cached.is_file(), "Existing engine required; no downloads"
gw._ECHARTS_DIR.mkdir(parents=True, exist_ok=True)
shutil.copyfile(cached, gw._ECHARTS_DIR / cached.name)
project = library.create_project("Native graph workflow", "Cross-source navigation literature")
papers = []
for i, source in enumerate(("arxiv", "openalex", "europepmc")):
    doi = f"10.0/native-graph-{i}"
    papers.append(dict(title=("机器人导航方法", "Robot navigation experiments", "Navigation evidence review")[i],
                       source=source, doi=doi, year=2022+i, authors="Alice Example",
                       abstract="Reproducible navigation experiments and controlled scientific evidence."))
    oa._oa_cache.set(f"refs:{doi}", dict(doi=doi, openalex_id=f"W{i+1}",
        referenced_works=[f"W{i+2}"] if i < 2 else [],
        keywords=["navigation", "reproducibility"]), expire=3600)
assert library.save_papers_to_project(project.id, papers) == (3, 0)
with patch.object(oa, "source_get", side_effect=AssertionError("Cached graph must not request the network")):
    graph = graph_service.build_graph_data(project.id, library.get_project_papers(project.id))
assert graph["stats"]["n_cite_edges"] == 2 and graph["stats"]["n_cooccur_edges"] == 3

probe = r'''
def _probe():
    import ctypes,json,time,traceback
    from ctypes import wintypes
    from pathlib import Path
    from PIL import ImageGrab
    window=webview.windows[0]; out={"ok":False,"input":"production library/cache graph","views":[]}
    try:
        window.events.loaded.wait(30)
        for _ in range(100):
            if window.evaluate_js("chart !== null && chart.getOption().series.length > 0"): break
            time.sleep(.2)
        for view in ("cites","cooccur","timeline"):
            window.evaluate_js("document.querySelector('.tab[data-view="+view+"]').click()")
            time.sleep(.5)
            state=json.loads(window.evaluate_js("JSON.stringify({view:VIEW,nodes:chart.getOption().series[0].data.length,links:chart.getOption().series[0].links.length,layout:chart.getOption().series[0].layout})"))
            assert state["nodes"]==3 and state["links"]==(3 if view=="cooccur" else 2),state
            if view=="timeline": assert state["layout"]=="none",state
            out["views"].append(state)
        for _ in range(3):
            window.minimize();time.sleep(.2);window.restore();time.sleep(.5)
            assert window.evaluate_js("chart.getOption().series[0].data.length")==3
        window.evaluate_js("document.querySelector('.tab[data-view=cites]').click();onNodeClick({dataType:'node',data:{paper:DATA.nodes[0]}})")
        assert window.evaluate_js("document.getElementById('detail').textContent.includes(DATA.nodes[0].title)")
        window.evaluate_js("document.getElementById('btn-close').click()")
        assert window.evaluate_js("document.getElementById('detail').style.display")=='none'
        window.move(20,20);window.restore();time.sleep(1)
        u=ctypes.windll.user32;u.FindWindowW.restype=wintypes.HWND
        u.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
        rect=wintypes.RECT();hwnd=u.FindWindowW(None,k['title'])
        assert hwnd and u.GetWindowRect(hwnd,ctypes.byref(rect))
        ImageGrab.grab(bbox=(rect.left,rect.top,rect.right,rect.bottom),all_screens=True).save(_result.with_suffix('.png'))
        out['ok']=True
    except Exception: out['error']=traceback.format_exc()
    finally:
        _result.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8');window.destroy()
'''
original_popen = subprocess.Popen
processes = []
result = work / "graph_native_report.json"
def launch(args, **kwargs):
    extra = f"from pathlib import Path\n_result=Path({str(result)!r})\n" + probe
    code = args[2].replace("webview.start(gui='edgechromium')", extra + "\nwebview.start(func=_probe,gui='edgechromium')")
    child = original_popen([args[0], args[1], code], **kwargs)
    processes.append(child)
    return child
with patch("subprocess.Popen", side_effect=launch):
    assert gw.open_graph_window("Search workflow 20261002", graph)
    processes[0].wait(timeout=90)
payload = json.loads(result.read_text(encoding="utf-8"))
print(json.dumps(payload, ensure_ascii=False), flush=True)
assert payload["ok"], payload
