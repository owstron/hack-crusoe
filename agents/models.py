"""Crusoe model routing + a recorded, retrying call helper.

Role -> model mapping follows action_plan.md "Model routing" (IDs verified
against Crusoe /v1/models). Models are never switched silently: on failure the
caller's explicit `fallback` value is returned with fallback_used=True.
"""
from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agents.jsonx import ExtractionError, extract_json

ROLE_MODELS: dict[str, str] = {
    "planner": "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B",
    "channel": "nvidia/Nemotron-3.5-Lightning-30B-A3B",
    "verifier": "nvidia/Nemotron-3-Nano-Omni-Reasoning-30B-A3B",
    "summarizer": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    "improver": "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B",
    "visual": "nvidia/NVIDIA-Nemotron-3.5-Super-VL",
}
ROLE_TEMPERATURE: dict[str, float | None] = {
    "planner": 0, "channel": 0, "verifier": 0, "summarizer": 0, "improver": 0,
    "visual": None,
}
# Extra request body per role. Thinking is disabled for structured-JSON roles so
# max_tokens is not consumed by reasoning; the verifier is a reasoning model and
# keeps thinking on (jsonx strips <think> blocks).
ROLE_EXTRA_BODY: dict[str, dict] = {
    "planner": {"chat_template_kwargs": {"enable_thinking": False}},
    "channel": {"chat_template_kwargs": {"enable_thinking": False}},
    "verifier": {},
    "summarizer": {"chat_template_kwargs": {"enable_thinking": False}},
    "improver": {"chat_template_kwargs": {"enable_thinking": False}},
    "visual": {},
}
ROLE_MAX_TOKENS: dict[str, int] = {
    "planner": 4096, "channel": 2048, "verifier": 4096,
    "summarizer": 2048, "improver": 4096, "visual": 2048,
}
DEFAULT_TIMEOUT_S = 120.0
# Longest single wait between retries (waits go 2, 4, 8 s, then stay at the cap).
BACKOFF_CAP_S = float(os.environ.get("MODEL_BACKOFF_CAP_S", "10"))
# Process-wide cap on in-flight requests: parallel gate campaigns would otherwise trip Crusoe's 429 limits, and a
# fallback silently degrades a gate comparison.
MAX_IN_FLIGHT = int(os.environ.get("MODEL_CONCURRENCY", "6"))
_in_flight = threading.BoundedSemaphore(MAX_IN_FLIGHT)
MAIN_ENV = str(Path(__file__).resolve().parent.parent / ".env")


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()
    if not os.getenv("CRUSOE_API_KEY") and Path(MAIN_ENV).exists():
        load_dotenv(MAIN_ENV)


def make_client(timeout: float = DEFAULT_TIMEOUT_S):
    from openai import OpenAI
    _load_env()
    return OpenAI(base_url=os.environ["CRUSOE_URL"], api_key=os.environ["CRUSOE_API_KEY"],
                  timeout=timeout, max_retries=0)


@dataclass
class CallResult:
    ok: bool
    parsed: Any = None
    raw: str | None = None
    error: str | None = None
    fallback_used: bool = False
    model: str = ""
    temperature: float | None = None
    prompt: list | None = None
    latency_s: float = 0.0
    attempts: int = 0


def _is_transient(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) in (408, 429, 502, 503, 504):
        return True
    return type(exc).__name__ in ("RateLimitError", "APITimeoutError", "APIConnectionError",
                                  "TimeoutError", "ConnectionError", "InternalServerError")


def _message_text(resp) -> str | None:
    msg = resp.choices[0].message
    content = getattr(msg, "content", None)
    if content and content.strip():
        return content
    d = msg.model_dump() if hasattr(msg, "model_dump") else {}
    return d.get("reasoning_content") or d.get("reasoning") or content


def call_model(role: str, messages: list[dict], *, client=None,
               recorder: Callable[[dict], None] | None = None,
               schema_check: Callable[[Any], Any] | None = None,
               max_retries: int = 6, fallback: Any = None,
               ctx: dict | None = None, expect: str = "object",
               max_tokens: int | None = None,
               sleep: Callable[[float], None] | None = None,
               backoff_base: float = 2.0, temperature: float | None = None) -> CallResult:
    """Call the model for `role`; retries transient errors, bad JSON and schema failures.
    `temperature` overrides the role's setting for this call (the improver's explore mode uses it); it is recorded."""
    if role not in ROLE_MODELS:
        raise KeyError(f"unknown role {role!r}; known: {sorted(ROLE_MODELS)}")
    model, temp = ROLE_MODELS[role], ROLE_TEMPERATURE[role] if temperature is None else temperature
    res = CallResult(ok=False, model=model, temperature=temp, prompt=messages)
    t0 = time.monotonic()
    try:
        client = client or make_client()
    except Exception as e:
        res.error = f"client_error: {type(e).__name__}: {e}"
        client = None
    kwargs: dict[str, Any] = dict(model=model, messages=messages,
                                  max_tokens=max_tokens or ROLE_MAX_TOKENS[role])
    if temp is not None:
        kwargs["temperature"] = temp
    if ROLE_EXTRA_BODY.get(role):
        kwargs["extra_body"] = ROLE_EXTRA_BODY[role]

    attempt = 0
    while client is not None and attempt <= max_retries:
        attempt += 1
        res.attempts = attempt
        try:
            with _in_flight:
                resp = client.chat.completions.create(**kwargs)
            res.raw = _message_text(resp)
            if expect == "text":
                if not (res.raw or "").strip():
                    raise ExtractionError("empty reply")
                parsed = res.raw.strip()
            else:
                parsed = extract_json(res.raw, expect)
            if schema_check is not None:
                parsed = schema_check(parsed)
            res.parsed, res.ok, res.error = parsed, True, None
            break
        except ExtractionError as e:
            res.error = f"extraction_error: {e}"
        except ValueError as e:
            res.error = f"schema_error: {e}"
        except Exception as e:
            res.error = f"{type(e).__name__}: {e}"
            if not _is_transient(e):
                break
        if attempt <= max_retries:
            (sleep or time.sleep)(min(BACKOFF_CAP_S, backoff_base * 2 ** (attempt - 1)) + random.uniform(0, 0.25 * backoff_base))
    if not res.ok:
        res.parsed, res.fallback_used = fallback, True
    res.latency_s = time.monotonic() - t0
    if recorder is not None:
        rec = dict(ctx or {})
        rec.update(role=role, model=model, temperature=temp, prompt=messages,
                   raw_response=res.raw, parsed_json=res.parsed if res.ok else None,
                   error=res.error, fallback_used=res.fallback_used,
                   latency_s=res.latency_s, attempts=res.attempts)
        recorder(rec)
    return res


class HTTPError(Exception):
    """Test helper: exception carrying an HTTP status_code."""

    def __init__(self, status_code: int, message: str = ""):
        super().__init__(f"HTTP {status_code} {message}")
        self.status_code = status_code


class FakeClient:
    """Mock OpenAI-style client. `responses` items: str (content) or Exception.
    The last item repeats once the list is exhausted."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item

        class _Msg:
            content = item

            def model_dump(self):
                return {"content": item}

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()
