"""Current-turn cancellation, job ownership and durable completion boundaries.

Cancellation is deliberately a BaseException, like asyncio.CancelledError: existing
network fallback handlers must not mistake a user stop for a failed API call.
"""
import asyncio
import copy
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import threading
import time
import uuid
logger = logging.getLogger(__name__)


class OperationCancelled(BaseException):
    def __init__(self, result=None):
        self.result = result
        super().__init__("当前轮已停止")


class CancellationToken:
    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.RLock()
        self._callbacks = set()

    @property
    def cancelled(self):
        return self._event.is_set()

    def check(self):
        if self.cancelled:
            raise OperationCancelled()

    def subscribe(self, callback):
        with self._lock:
            self._callbacks.add(callback)
            cancelled = self.cancelled
        if cancelled:
            callback()
        def unsubscribe():
            with self._lock:
                self._callbacks.discard(callback)
        return unsubscribe

    def cancel(self):
        with self._lock:
            self._event.set()
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            callback()

    def run_async(self, operation):
        """Interrupt network awaits, including before response headers arrive."""
        self.check()
        async def invoke():
            task, loop = asyncio.current_task(), asyncio.get_running_loop()
            def cancel_task():
                try:
                    loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    pass  # Request completed while the callback was being detached.
            unsubscribe = self.subscribe(cancel_task)
            try:
                self.check()
                return await operation()
            except asyncio.CancelledError:
                raise OperationCancelled() from None
            finally:
                unsubscribe()
        return asyncio.run(invoke())


_run = ContextVar("paperpilot_agent_run", default=None)
_reply_stream = ContextVar("paperpilot_agent_reply_stream", default=False)


@contextmanager
def run_scope(run):
    token = _run.set(run)
    try:
        checkpoint()
        yield
    finally:
        _run.reset(token)


@contextmanager
def reply_stream():
    token = _reply_stream.set(True)
    try:
        yield
    finally:
        _reply_stream.reset(token)


def current_run():
    return _run.get()


def checkpoint():
    run = current_run()
    if run:
        run.token.check()


def interruptible_wait(seconds):
    run = current_run()
    if run:
        run.token._event.wait(seconds)
        run.token.check()
    else:
        time.sleep(seconds)


def publish_reply(text):
    run = current_run()
    if run and _reply_stream.get() and not run.token.cancelled:
        run.partial = text
        if run.on_partial:
            run.on_partial(text)


class AgentRun:
    """Jobs reserve ownership before scheduling; idle follows the final job."""
    def __init__(self, cm, project_id, goal, operation="chat", on_done=None, on_partial=None, *, attachments=None):
        self.id = uuid.uuid4().hex
        self.cm = cm
        self.identity = (project_id, cm.session_id)
        self.user_message, self.operation = goal, operation
        self.attachments = copy.deepcopy(attachments or [])
        self.maintenance = operation == "compression"
        previous = copy.deepcopy(cm._meta.get("last_run", {}))
        continuing = goal.strip().strip("。.!！").casefold() in {
            "继续", "继续工作", "继续执行", "接着做", "resume", "continue"}
        self.resuming = continuing and previous.get("state") in {"cancelled", "interrupted"}
        self.goal = (previous.get("goal") or goal) if self.resuming else goal
        self.resume_context = ""
        if self.resuming:
            self.resume_context = ("[恢复此前中断工作的背景，请先核对实际进度]\n"
                f"原目标：{self.goal}\n中断步骤：{previous.get('phase', '未知')}\n"
                f"已完成：{'；'.join(previous.get('completed_steps', [])) or '未记录'}\n"
                f"尚未开始：{'；'.join(previous.get('pending_steps', [])) or '未记录'}\n\n")
        self.token = CancellationToken()
        self.on_done, self.on_partial = on_done, on_partial
        self.partial = ""
        self.user_recorded = False
        self.reply_recorded = False
        self._lock = threading.RLock()
        self._jobs = 1
        self._state = "running"
        self._completed = list(previous.get("completed_steps", [])) if self.resuming else []
        self._pending = []
        self._phase = "开始"
        self._started = datetime.now().isoformat()
        self._save()

    def _save(self):
        save = self.cm.set_maintenance_state if self.maintenance else self.cm.set_run_state
        save(dict(run_id=self.id, state=self._state,
            goal=self.goal, operation=self.operation, phase=self._phase,
            submitted_attachments=self.attachments,
            submitted_message=self.user_message, resume_context=self.resume_context,
            completed_steps=list(self._completed), started_at=self._started,
            pending_steps=list(self._pending),
            updated_at=datetime.now().isoformat()))

    def phase(self, phase):
        with self._lock:
            self.token.check()
            self._phase = phase
            self._save()

    def plan(self, actions):
        with self._lock:
            self.token.check()
            self._pending = [a["type"] for a in actions]
            self._save()

    def start_step(self, name):
        with self._lock:
            self.token.check()
            if name in self._pending:
                self._pending.remove(name)
            self._phase = name
            self._save()

    def completed(self, description):
        with self._lock:
            # A committed operation remains recorded even if stop raced its end.
            self._completed.append(description)
            self._save()

    def reserve(self):
        with self._lock:
            self.token.check()
            if self._jobs == 0:
                raise OperationCancelled()
            self._jobs += 1

    def stop(self):
        with self._lock:
            if self._jobs == 0:
                return
            self._state = "stopping"
            self.token.cancel()
            try:
                self._save()
            except (OSError, ValueError):
                logger.warning("Stop was signalled, but the chat state could not be saved")

    def fail(self):
        with self._lock:
            if not self.token.cancelled:
                self._state = "failed"

    def finish(self):
        try:
            note = self._finish_and_save()
        except (OSError, ValueError):
            logger.warning("Agent turn ended, but its final chat record could not be saved")
            note = "本轮已结束，但会话记录保存失败；原文件保留，请检查课题目录或磁盘状态。"
        if note is not None and self.on_done:
            self.on_done(self, note)

    def _finish_and_save(self):
        with self._lock:
            self._jobs -= 1
            if self._jobs != 0:
                return
            self._state = "cancelled" if self.token.cancelled else (
                "failed" if self._state == "failed" else "completed")
            note = ""
            if self.maintenance:
                if self.token.cancelled:
                    note = ("已停止压缩；已提交的摘要与原始记录保留。" if self._completed
                            else "已停止压缩，原上下文与研究目标保留。")
                self._save()
                return note
            self.cm.finish_pending_tools()
            if self.token.cancelled:
                # Record the goal even when interrupted during pre-request compaction.
                if not self.user_recorded:
                    from paperpilot.agent_attachments import format_attachment_material
                    self.cm.add_user_message(format_attachment_material(self.attachments) + self.resume_context + self.user_message,
                                             display_content=self.user_message, attachments=self.attachments)
                note = "本轮已由用户停止。保留此前记录和已完成结果；后续动作未执行。"
                if self._completed:
                    note += "\n已完成：" + "；".join(self._completed)
                if self.partial and not self.reply_recorded:
                    # Never dispatch or replay incomplete machine-action proposals.
                    import re
                    partial = re.split(r"\[(?:ACTION:|PROJECT_UPDATE|TEAM)", self.partial, maxsplit=1)[0].rstrip()
                    if partial:
                        note = partial + "\n\n[回复未完成]\n" + note
                note += "\n可以发送新消息继续；需重新核对中断步骤的实际结果。"
                self.cm.add_assistant_message(note)
            self._save()
            return note
