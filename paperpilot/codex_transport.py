"""Official Codex app-server transport and ChatGPT-managed authentication.

No token endpoint or private ChatGPT API is implemented here. Codex owns OAuth,
refresh and inference. A dedicated, ignored profile keeps other Codex clients intact.
"""
import atexit
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time

from paperpilot.config import BASE_DIR, load_config


class CodexError(RuntimeError):
    pass


def profile_options(cli=None, home=None):
    config = load_config()
    llm = config.get("llm") or {}
    cli = (cli if cli is not None else llm.get("codex_cli", "")) or ""
    home = (home if home is not None else llm.get("codex_home", "")) or ""
    cache = Path((config.get("cache") or {}).get("dir") or BASE_DIR / "cache")
    directory = Path(home).expanduser() if home else cache / "codex-subscription"
    if not directory.is_absolute():
        directory = BASE_DIR / directory
    executable = shutil.which(cli or "codex")
    if not executable:
        raise CodexError("未找到 Codex CLI，请安装官方 CLI 或填写可执行文件路径。")
    directory = directory.resolve()
    validation_root = os.environ.get("PAPERPILOT_VALIDATION_ROOT")
    isolated = validation_root and directory.is_relative_to(Path(validation_root).resolve())
    if directory.is_relative_to(BASE_DIR.resolve()) and not directory.is_relative_to((BASE_DIR / "cache").resolve()) and not isolated:
        # A custom profile contains logs/state too. Require an ignored location.
        try:
            probe = subprocess.run(["git", "check-ignore", "--quiet", str(directory / ".profile-check")],
                cwd=BASE_DIR, capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError as exc:
            raise CodexError("自定义 Codex 登录目录无法检查 Git 忽略规则，请使用项目 cache 目录。") from exc
        if probe.returncode != 0:
            raise CodexError("Codex 登录目录必须位于 Git 忽略范围，建议放在项目 cache 内。")
    return str(Path(executable).resolve()), directory


def _safe_message(value):
    text = str(value).replace("\n", " ")[:400]
    text = re.sub(r"\b(?:sk-[\w-]+|eyJ[\w.-]+)\b", "[已隐藏凭据]", text)
    text = re.sub(r"https?://\S*\?\S+", "[已隐藏授权地址]", text)
    return text


class AppServer:
    """One hidden stdio process; RPC replies and turn events have separate queues."""
    def __init__(self, executable, home):
        self.executable, self.home = executable, Path(home)
        self.process = None
        self._start_lock = threading.RLock()
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._counter = 0
        self._pending, self._abandoned, self.states = {}, {}, {}
        self.events = queue.Queue()
        self._account = None
        self._account_at = 0
        self._models = None
        self.login_id = None

    def start(self, token=None):
        with self._start_lock:
            if token: token.check()
            if self.process and self.process.poll() is None:
                return
            if self.process:
                self.close()
            self.home.mkdir(parents=True, exist_ok=True)
            work = self.home / "work"
            temporary = self.home / "tmp"
            work.mkdir(exist_ok=True); temporary.mkdir(exist_ok=True)
            env = dict(os.environ, CODEX_HOME=str(self.home), TEMP=str(temporary), TMP=str(temporary),
                       RUST_LOG="error")
            for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"):
                env.pop(key, None)
            overrides = dict(cli_auth_credentials_store="file", model_provider="openai",
                forced_login_method="chatgpt", sandbox_mode="read-only", approval_policy="never",
                web_search="disabled", project_doc_max_bytes=0,
                log_dir=str(self.home / "log"), sqlite_home=str(self.home),
                **{"analytics.enabled": False, "features.shell_tool": False,
                   "features.apps": False, "features.plugins": False,
                   "features.collab": False, "features.multi_agent_v2": False,
                   "features.code_mode": False, "features.code_mode_only": False,
                   "features.computer_use": False, "features.view_image": False,
                   "features.image_generation": False, "features.hooks": False,
                   "features.codex_hooks": False, "features.plugin_hooks": False,
                   "features.skip_host_skill_discovery": True})
            command = [self.executable, "app-server", "--stdio"]
            for key, value in overrides.items():
                command.extend(["-c", f"{key}={json.dumps(value, ensure_ascii=False)}"])
            try:
                self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, cwd=work, env=env, text=True, encoding="utf-8",
                    bufsize=1, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except OSError as exc:
                raise CodexError("Codex CLI 启动失败，请检查路径及安装。") from exc
            process = self.process
            threading.Thread(target=self._read, args=(process,), daemon=True).start()
            # Drain diagnostics, but never copy raw SDK logs or credentials to UI/logs.
            def drain():
                for _ in process.stderr:
                    pass
            threading.Thread(target=drain, daemon=True).start()
            try:
                initialized = self.rpc("initialize", dict(clientInfo=dict(name="paperpilot", version="1.0"),
                    capabilities=dict(experimentalApi=True)), timeout=15, token=token, _started=True)
                version = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", initialized.get("userAgent", ""))
                if version and tuple(map(int, version.groups())) < (0, 159, 2):
                    raise CodexError("Codex 订阅适配需要 CLI 0.159.2 或更新版本，请更新官方 CLI。")
                self.notify("initialized", {})
                self._account_at, self._models = 0, None
            except BaseException:
                self.close()
                raise

    def _read(self, process):
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                with self._lock:
                    if process is not self.process:
                        return
                    if "id" in message and "method" not in message:
                        pending = self._pending.pop(message["id"], None)
                        if pending:
                            pending.put(message)
                        abandoned = self._abandoned.pop(message["id"], None)
                        if abandoned and "result" in message:
                            abandoned(message["result"])
                        continue
                    method, params = message.get("method", ""), message.get("params") or {}
                    thread_id = params.get("threadId")
                    state = self.states.get(thread_id)
                    if method in {"account/updated", "account/login/completed"}:
                        self._account_at, self._models = 0, None
                        self._clear_catalogue()
                    if method == "account/login/completed" and params.get("loginId") == self.login_id:
                        self.login_id = None
                    if state:
                        state.events.put(message)
                    elif "id" in message:
                        # Never approve a tool/permission request outside an owned turn.
                        self.respond(message["id"], error=dict(code=-32601, message="Request not owned by PaperPilot"))
                    elif method.startswith("account/"):
                        self.events.put(message)
        finally:
            with self._lock:
                if process is self.process:
                    error = CodexError("Codex app server 已断开；请求未自动重试，请重新连接后继续。")
                    for pending in self._pending.values(): pending.put(error)
                    self._pending.clear()
                    for state in self.states.values(): state.events.put(error)
                    self._account_at, self._models = 0, None
                    self.login_id = None

    def _send(self, message):
        with self._write_lock:
            if not self.process or self.process.poll() is not None:
                raise CodexError("Codex app server 未连接。")
            try:
                self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise CodexError("Codex app server 连接已关闭。") from exc

    def notify(self, method, params):
        self._send(dict(method=method, params=params))

    def respond(self, ident, result=None, error=None):
        self._send(dict(id=ident, **({"error": error} if error else {"result": result})))

    def rpc(self, method, params=None, timeout=15, token=None, *, _started=False, on_abandoned=None):
        if not _started:
            self.start(token)
        with self._lock:
            self._counter += 1
            ident = self._counter
            output = self._pending[ident] = queue.Queue()
        deadline = time.monotonic() + timeout
        received = False
        try:
            self._send(dict(id=ident, method=method, params=params or {}))
            while True:
                if token: token.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise CodexError(f"Codex {method} 超时；请求未自动重试。")
                try: message = output.get(timeout=min(.1, remaining))
                except queue.Empty: continue
                if isinstance(message, BaseException): raise message
                received = True
                if "error" in message:
                    raise CodexError(f"Codex {method}: {_safe_message(message['error'].get('message', '请求失败'))}")
                return message.get("result") or {}
        finally:
            with self._lock:
                pending = self._pending.pop(ident, None)
                if not received and on_abandoned:
                    if pending:
                        self._abandoned[ident] = on_abandoned
                    elif not output.empty():
                        reply = output.get_nowait()
                        if isinstance(reply, dict) and "result" in reply:
                            on_abandoned(reply["result"])

    def fire(self, method, params):
        """A real request ID is required even when cleanup does not await its reply."""
        with self._lock:
            self._counter += 1
            ident = self._counter
        try: self._send(dict(id=ident, method=method, params=params))
        except CodexError: pass

    def account(self, refresh=False, token=None):
        self.start(token)
        with self._lock:
            if not refresh and time.monotonic() - self._account_at < 5:
                return self._account
        account = self.rpc("account/read", dict(refreshToken=False), token=token).get("account")
        with self._lock:
            if account != self._account:
                self._models = None
                self._clear_catalogue()
            self._account, self._account_at = account, time.monotonic()
        return account

    def require_subscription(self, token=None):
        account = self.account(token=token)
        if not account or account.get("type") != "chatgpt":
            raise CodexError("请先在 Codex 订阅区使用 ChatGPT 登录；API Key 登录不作为订阅接入。")
        return account

    def models(self, refresh=False, token=None):
        self.require_subscription(token)
        with self._lock:
            if self._models is not None and not refresh: return list(self._models)
        result, cursor = [], None
        while True:
            page = self.rpc("model/list", dict(limit=100, cursor=cursor, includeHidden=False), token=token)
            result.extend(page.get("data") or [])
            cursor = page.get("nextCursor")
            if not cursor: break
        with self._lock: self._models = result
        from paperpilot.llm_client import MODEL_CATALOG, MODEL_CAPABILITIES
        default = next((m for m in result if m.get("isDefault")), result[0] if result else None)
        MODEL_CATALOG["codex"] = [("codex-default", "账号默认" + (f"（{default['displayName']}）" if default else ""))] + [
            (m["model"], m.get("displayName") or m["model"]) for m in result]
        for m in result:
            key = ("codex", m["model"])
            MODEL_CAPABILITIES[key] = dict(MODEL_CAPABILITIES.get(key, {}),
                model=m["model"],
                images="image" in (m.get("inputModalities") or []),
                source="https://developers.openai.com/codex/app-server")
        if default:
            MODEL_CAPABILITIES[("codex", "codex-default")] = MODEL_CAPABILITIES[("codex", default["model"])]
        return list(result)

    def _idle(self):
        if self.states: raise CodexError("有 Codex 任务正在运行，请先停止任务再更改登录。")

    @staticmethod
    def _clear_catalogue():
        from paperpilot.llm_client import MODEL_CAPABILITIES, MODEL_CATALOG
        for key in list(MODEL_CAPABILITIES):
            if key[0] == "codex": MODEL_CAPABILITIES.pop(key, None)
        MODEL_CATALOG["codex"] = [("codex-default", "账号默认（登录后刷新）")]

    def login(self):
        with self._start_lock:
            self._idle()
            self.cancel_login()
            result = self.rpc("account/login/start", dict(type="chatgpt"))
            self.login_id = result["loginId"]
            return result

    def cancel_login(self):
        with self._start_lock:
            if self.login_id:
                self.rpc("account/login/cancel", dict(loginId=self.login_id))
                self.login_id = None

    def logout(self):
        with self._start_lock:
            self._idle(); self.cancel_login()
            self.rpc("account/logout")
            self._account, self._account_at, self._models = None, 0, None
            self._clear_catalogue()

    def import_login(self, source):
        """Official auth-cache-copy fallback; never decode/print OAuth credentials."""
        source = Path(source)
        if source.name != "auth.json" or not source.is_file() or source.is_symlink():
            raise CodexError("本机没有可复用的 auth.json；请使用 ChatGPT 登录。")
        source = source.resolve()
        with self._start_lock:
            self._idle(); self.cancel_login()
            target = self.home / "auth.json"
            if source == target.resolve(): return self.require_subscription()
            previous = target.read_bytes() if target.exists() else None
            self.close()
            self.home.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copyfile(source, target)
                if os.name != "nt": target.chmod(0o600)
                return self.require_subscription()
            except BaseException:
                self.close()
                if previous is None: target.unlink(missing_ok=True)
                else: target.write_bytes(previous)
                raise

    def close(self):
        with self._start_lock:
            process, self.process = self.process, None
            with self._lock:
                error = CodexError("Codex app server 连接已关闭。")
                for pending in self._pending.values(): pending.put(error)
                self._pending.clear(); self._abandoned.clear()
                for state in self.states.values(): state.events.put(error)
            if process:
                try: process.stdin.close()
                except (OSError, ValueError): pass
                try: process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=2)
                for stream in (process.stdout, process.stderr):
                    try: stream.close()
                    except (OSError, ValueError): pass
            self._account_at, self._models = 0, None


_pool, _pool_lock = {}, threading.RLock()


def get_server(cli=None, home=None):
    executable, directory = profile_options(cli, home)
    key = (executable, str(directory))
    with _pool_lock:
        if key not in _pool: _pool[key] = AppServer(executable, directory)
        return _pool[key]


def close_servers():
    with _pool_lock:
        servers = list(_pool.values()); _pool.clear()
    for server in servers:
        for state in list(server.states.values()): state.dispose()
        server.close()


atexit.register(close_servers)
