"""Run an existing local test in an isolated workspace (no user-data writes).

Usage: python -B tools/run_validation.py test_unit_core.py
The runner never imports the real config.yaml. It builds a credential-free config
from config.example.yaml and redirects all writable application paths to an E-drive
scratch directory. Tested code may still attempt model downloads when required
caches are absent.
"""
import os
import re
import runpy
import shutil
import sys
import tempfile
import types
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="backslashreplace")
_dll_handles = []
if sys.platform == "win32":
    for directory in (Path(sys.base_prefix) / "Library" / "bin", Path(sys.base_prefix) / "DLLs"):
        if directory.is_dir():
            _dll_handles.append(os.add_dll_directory(str(directory)))
target = (ROOT / sys.argv[1]).resolve()
if not target.is_relative_to(ROOT) or not target.is_file():
    raise SystemExit("Expected a local test file under the project root")
scratch_root = (ROOT / os.environ.get("PAPERPILOT_VALIDATION_SCRATCH", ".validation-search-20260922")).resolve()
if not scratch_root.is_relative_to(ROOT) or not scratch_root.name.startswith(".validation-"):
    raise SystemExit("Validation scratch must be a .validation-* directory under this project")
work = scratch_root / target.stem
if work.exists():
    if not work.is_relative_to(scratch_root):
        raise SystemExit("Refusing to clear validation path outside scratch root")
    shutil.rmtree(work)
work.mkdir(parents=True, exist_ok=True)
os.environ.update({"TMP": str(work), "TEMP": str(work),
                   "PAPERPILOT_VALIDATION_ROOT": str(work),
                   "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
                   "HF_HOME": str(work / "hf"),
                   "XDG_CACHE_HOME": str(work / "xdg")})
sys.dont_write_bytecode = True
tempfile.tempdir = str(work)
sys.path.insert(0, str(ROOT))

# Build a synthetic configuration without importing or reading config.yaml.
example_path = ROOT / "config.example.yaml"
safe_config = yaml.safe_load(example_path.read_text(encoding="utf-8")) or {}

def scrub(node):
    if isinstance(node, dict):
        return {
            key: ("" if any(mark in key.casefold()
                            for mark in ("key", "token", "password")) else scrub(value))
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [scrub(value) for value in node]
    return node

safe_config = scrub(safe_config)
safe_config.setdefault("cache", {})["dir"] = str(work / "api")
safe_config_path = work / "config.yaml"
safe_config_path.write_text(
    yaml.safe_dump(safe_config, allow_unicode=True, sort_keys=False), encoding="utf-8")

def load_config(path=None):
    selected = Path(path) if path is not None else safe_config_path
    with selected.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}

def save_config(updates, path=None):
    selected = Path(path) if path is not None else safe_config_path
    current = load_config(selected) if selected.exists() else {}

    def merge(base, incoming):
        for key, value in incoming.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merge(base[key], value)
            else:
                base[key] = value

    merge(current, updates)
    selected.write_text(
        yaml.safe_dump(current, allow_unicode=True, sort_keys=False), encoding="utf-8")

config = types.ModuleType("paperpilot.config")
config.BASE_DIR = ROOT
config.CONFIG_PATH = safe_config_path
config.config = safe_config
config.load_config = load_config
config.save_config = save_config
sys.modules["paperpilot.config"] = config

class RedactedStream:
    def __init__(self, stream):
        self.stream = stream
    def write(self, text):
        text = re.sub(
            r"(?i)(api_key|apikey|token|password)(\s*[:=]\s*)[^&\s,}]+",
            r"\1\2[REDACTED]", text)
        return self.stream.write(text)
    def __getattr__(self, key):
        return getattr(self.stream, key)
sys.stdout = RedactedStream(sys.stdout)
sys.stderr = RedactedStream(sys.stderr)

from paperpilot import library, repo_manager as rm, conversation, ai_service, downloader
library._DB_PATH = str(work / "isolated.db")
rm._REPO_ROOT = work / "repository"
rm._CACHE_DIR = work / "cache"
rm._CACHE_PDFS = rm._CACHE_DIR / "pdfs"
rm._CACHE_INDEX = rm._CACHE_DIR / "cache_index.json"
rm._RECYCLE_DIR = rm._REPO_ROOT / ".recycle"
conversation._REPO_ROOT = rm._REPO_ROOT
ai_service._DEEP_READ_DIR = work / "deep_read"
downloader.DEFAULT_CACHE_DIR = work / "pdf"
downloader.HTML_CACHE_DIR = work / "html"

# Resolve existing model paths before redirecting Path.home for window caches.
if sys.platform == "win32" and not os.environ.get("FLET_VIEW_PATH"):
    from importlib.metadata import version, PackageNotFoundError
    try:
        desktop_version = version('flet-desktop')
    except PackageNotFoundError:
        desktop_version = ""
    native_client = Path.home() / ".flet" / "client" / f"flet-desktop-full-{desktop_version}" / "flet"
    if desktop_version and (native_client / "flet.exe").is_file():
        # Reuse the installed binary read-only; runtime output still goes to E:.
        os.environ["FLET_VIEW_PATH"] = str(native_client)
from paperpilot import keywords, indexer
from paperpilot import pdf_viewer, graph_window
pdf_viewer._PDFJS_DIR = work / "pdfjs"
graph_window._ECHARTS_DIR = work / "echarts"
graph_window._DATA_DIR = work / "graph"
home = work / "home"
home.mkdir(exist_ok=True)
Path.home = classmethod(lambda cls: home)

# Guard Python file writes/removals outside this test's scratch directory.
def guard(event, args):
    paths = []
    if event == "open":
        path, mode, flags = args
        if isinstance(path, (str, bytes, os.PathLike)) and (
            (mode and any(c in mode for c in "wax+")) or
            flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        ):
            paths = [path]
    elif event in ("os.mkdir", "os.remove", "os.rmdir"):
        paths = [args[0]]
    elif event in ("os.rename", "os.link", "os.symlink"):
        paths = list(args[:2])
    for path in paths:
        if os.fsdecode(path).lower() in (os.devnull.lower(), r"\\.\nul"):
            continue
        resolved = Path(os.fsdecode(path)).resolve()
        if not resolved.is_relative_to(work):
            raise PermissionError(f"Validation write outside isolated directory: {resolved}")
sys.addaudithook(guard)
os.chdir(work)
print(f"ISOLATED TEST {target.name}", flush=True)
sys.argv = [str(target), *sys.argv[2:]]
runpy.run_path(str(target), run_name="__main__")
