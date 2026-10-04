"""Model-scoped context capacity and live estimates, separate from billing usage."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import math
import logging
import json

from paperpilot.conversation import _estimate_tokens

_target = ContextVar("paperpilot_context_sample", default=None)
logger = logging.getLogger(__name__)
DEEPSEEK_CAPACITY_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing"
# Officially advertised as 1M; use a conservative decimal million, not a guessed
# binary boundary. Unknown models never inherit another model's capacity.
_KNOWN_WINDOWS = {("deepseek", m): 1_000_000 for m in (
    "deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp", "deepseek-v4-pro")}


def estimate_request_tokens(messages):
    """Text/framing plus a conservative image allowance, never base64 characters.

    Images use 4096 tokens as a cross-provider estimate; actual provider usage
    calibrates the main-chat meter after the request.
    """
    def content_size(content):
        if isinstance(content, str):
            return _estimate_tokens(content)
        return sum(_estimate_tokens(p.get("text", "")) if p.get("type") == "text"
                   else 4096 if p.get("type") in {"image_url", "image"} else 0 for p in content or [])
    return 3 + sum(4 + content_size(m.get("content", ""))
        + (_estimate_tokens(json.dumps(m["tool_calls"], ensure_ascii=False)) if m.get("tool_calls") else 0)
        + _estimate_tokens(m.get("reasoning_content", "")) for m in messages)


@dataclass(frozen=True)
class ContextPolicy:
    provider: str
    model: str
    window: int | None
    source: str
    output_reserve: int
    compact_threshold: int
    keep_rounds: int = 2


def context_policy(model=None):
    from paperpilot.config import load_config
    from paperpilot.llm_client import _load_llm_cfg, get_task_model, MODEL_CAPABILITIES
    route = _load_llm_cfg()
    provider = route.get("provider", "")
    model = model or get_task_model("chat") or route.get("model", "")
    settings = load_config().get("agent", {}) or {}
    if not isinstance(settings, dict):
        settings = {}
    capacities = settings.get("context_windows", {}) or {}
    configured = capacities.get(provider, {}) if isinstance(capacities, dict) else {}
    if not isinstance(configured, dict):
        configured = {}
    value = configured.get(model)
    valid = isinstance(value, int) and not isinstance(value, bool) and 1024 <= value <= 50_000_000
    capability = MODEL_CAPABILITIES.get((provider, model), {})
    window = value if valid else _KNOWN_WINDOWS.get((provider, model)) or capability.get("window")
    source = "用户配置" if valid else (
        DEEPSEEK_CAPACITY_SOURCE if (provider, model) in _KNOWN_WINDOWS
        else capability.get("source", "未配置") if window else "未配置")
    reserve = min(8192, window // 8) if window else 8192
    ratio = settings.get("auto_compact_ratio", .7)
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not math.isfinite(ratio) or not .5 <= ratio <= .95:
        ratio = .7
    threshold = min(int(window * ratio), window - reserve) if window else 80_000
    keep = settings.get("keep_recent_rounds", 2)
    if not isinstance(keep, int) or isinstance(keep, bool) or not 0 <= keep <= 10:
        keep = 2
    return ContextPolicy(provider, model, window, source, reserve, threshold, keep)


@contextmanager
def track_context(cm):
    token = _target.set(cm)
    try:
        yield
    finally:
        _target.reset(token)


def observe_response(result, messages):
    """Anchor a main-chat request to provider input usage; never sample compression."""
    cm = _target.get()
    usage = getattr(result, "usage", None)
    if cm is not None and usage is not None and usage.input_tokens is not None:
        try:
            cm.set_context_sample(dict(provider=result.provider, model=result.model,
                input_tokens=usage.input_tokens, estimated_input=estimate_request_tokens(messages),
                compression_count=len(cm.compressed_summaries)))
        except (OSError, ValueError):
            logger.warning("Context usage sample could not be saved; chat response is preserved")


def _canonical_model(provider, model):
    if provider == "deepseek" and model in {"deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}:
        return "deepseek-flash"
    return model


def context_status(cm, system_prompt, draft="", model=None):
    policy = context_policy(model)
    with cm.lock:
        messages = cm.build_api_messages(system_prompt, load_images=False)
        sample = dict(cm._meta.get("context_sample", {}))
        revision = len(cm.compressed_summaries)
    estimated = estimate_request_tokens(messages)
    anchored = (sample.get("provider") == policy.provider
                and _canonical_model(policy.provider, sample.get("model")) == _canonical_model(policy.provider, policy.model)
                and sample.get("compression_count") == revision
                and isinstance(sample.get("input_tokens"), int)
                and isinstance(sample.get("estimated_input"), int))
    used = max(0, sample["input_tokens"] + estimated - sample["estimated_input"]) if anchored else estimated
    draft_tokens = _estimate_tokens(draft) + 4 if draft else 0
    return dict(provider=policy.provider, model=policy.model, window=policy.window,
                used=used, draft_tokens=draft_tokens, estimated=estimated,
                ratio=used / policy.window if policy.window else None,
                remaining=max(0, policy.window - used) if policy.window else None,
                source=policy.source, anchored=anchored, sample=sample,
                output_reserve=policy.output_reserve, compact_threshold=policy.compact_threshold)
