"""Gemini adapter.

Verified against google-genai 2.22.0 and the live docs (2026-09-02):
    client.aio.models.generate_content(model, contents, config)
    GenerateContentConfig(response_mime_type="application/json",
                          response_schema=<PydanticModel>, system_instruction=...)
    response.parsed        -> typed instance
    response.usage_metadata-> prompt/candidates/cached/thoughts token counts

Chosen over the newer `client.interactions.create` because that surface takes
an untyped `request: Any` passthrough; this one is typed, introspectable, and
gives `.parsed` directly.

Note there is no explicit prompt-cache control here as there is on Claude:
Gemini caches implicitly. The stable prefix is still placed first so implicit
caching can find it.
"""
from __future__ import annotations

import json
import time
from typing import Any, Literal, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

import logging

from ..ports import AIError, AIErrorKind, StructuredResult, TextPort, Usage
from ..structured import RawCall, Repair, generate_with_repair

log = logging.getLogger("hbz.ai")
T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "gemini-3.1-pro-preview"

#: A large structured document with thinking can legitimately take minutes, but
#: it must not take forever: without an explicit ceiling a hung request blocks
#: a worker indefinitely.
DEFAULT_TIMEOUT_S = 300

#: USD per million tokens, from the Gemini API pricing page (2026-09-02).
PRICING: dict[str, tuple[float, float]] = {
    "gemini-3.1-pro-preview": (2.00, 12.00),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
}

#: How a given schema is sent to Gemini.
#:
#:   native -> config.response_schema, enforced server-side (preferred)
#:   prompt -> JSON Schema written into the system instruction, enforced only
#:             by our own validators
#:
#: Gemini rejects schemas past a modest cumulative size with an opaque
#: 400 INVALID_ARGUMENT (measured: ~14 properties / 1.7KB passes, ~19 / 2.4KB
#: fails). Rather than hard-code a threshold that will drift, the adapter tries
#: native once per schema and pins the fallback if it is refused. A rejected
#: request is not billed, so the probe is free.
Strategy = Literal["native", "prompt"]
_STRATEGY: dict[str, Strategy] = {}


def schema_strategy(schema: type[BaseModel]) -> Strategy:
    return _STRATEGY.get(schema.__name__, "native")


def reset_strategies() -> None:
    _STRATEGY.clear()


def extract_json(text: str) -> Any:
    """Pull the first complete JSON value out of a model response.

    Without a server-enforced schema the model sometimes wraps the document in
    a markdown fence, or appends a closing remark, or emits a second object.
    `json.loads` rejects all of those outright; `raw_decode` reads one value and
    ignores whatever follows, which is what we actually want.
    """
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1]
        if (fence := body.rfind("```")) != -1:
            body = body[:fence]
        body = body.strip()
    start = min((i for i in (body.find("{"), body.find("[")) if i != -1),
                default=-1)
    if start == -1:
        raise json.JSONDecodeError("no JSON object in response", body or "", 0)
    value, _ = json.JSONDecoder().raw_decode(body[start:])
    return value


SCHEMA_INSTRUCTION = """Return JSON conforming EXACTLY to this JSON Schema. \
Include every required property. Do not add properties that are not in the \
schema. Output the JSON document and nothing else -- no markdown fence, no \
commentary before or after it.

{schema}"""


#: finish_reason values that mean "declined", not "failed".
_REFUSALS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "RECITATION",
             "IMAGE_PROHIBITED_CONTENT", "IMAGE_SAFETY"}


def _cost_cents(model: str, usage: Any) -> float:
    inp, out = PRICING.get(model, (0.0, 0.0))
    prompt = getattr(usage, "prompt_token_count", 0) or 0
    prompt += getattr(usage, "cached_content_token_count", 0) or 0
    cand = getattr(usage, "candidates_token_count", 0) or 0
    # Thinking tokens are billed as output; omitting them under-reports spend.
    cand += getattr(usage, "thoughts_token_count", 0) or 0
    return (prompt / 1e6) * inp * 100 + (cand / 1e6) * out * 100


def _usage(model: str, resp: Any, latency_ms: int) -> Usage:
    u = getattr(resp, "usage_metadata", None)
    return Usage(
        model=model,
        input_tokens=getattr(u, "prompt_token_count", 0) or 0,
        output_tokens=((getattr(u, "candidates_token_count", 0) or 0)
                       + (getattr(u, "thoughts_token_count", 0) or 0)),
        cache_read_tokens=getattr(u, "cached_content_token_count", 0) or 0,
        latency_ms=latency_ms,
        cost_cents=_cost_cents(model, u) if u else 0.0,
    )


def _classify(exc: Exception) -> AIError:
    name = type(exc).__name__
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    text = str(exc)[:300]
    if status in (401, 403) or "API key" in text or "PERMISSION_DENIED" in text:
        return AIError(AIErrorKind.AUTH, "unauthorized", text)
    if status == 429 or "RESOURCE_EXHAUSTED" in text:
        return AIError(AIErrorKind.QUOTA, "rate_limited", text)
    if isinstance(status, int) and status >= 500:
        return AIError(AIErrorKind.TRANSIENT, f"http_{status}", text)
    if status == 400 or "INVALID_ARGUMENT" in text:
        return AIError(AIErrorKind.INVALID, "bad_request", text)
    return AIError(AIErrorKind.UNKNOWN, name, text)


def _check_refusal(resp: Any) -> None:
    feedback = getattr(resp, "prompt_feedback", None)
    if feedback is not None and getattr(feedback, "block_reason", None):
        raise AIError(AIErrorKind.REFUSAL, "prompt_blocked",
                      str(feedback.block_reason))
    for cand in getattr(resp, "candidates", None) or []:
        reason = str(getattr(cand, "finish_reason", "") or "").rsplit(".", 1)[-1]
        if reason in _REFUSALS:
            raise AIError(AIErrorKind.REFUSAL, "provider_refused", reason)
        if reason == "MAX_TOKENS":
            raise AIError(
                AIErrorKind.INVALID, "max_tokens",
                "the model hit max_output_tokens before finishing the document; "
                "raise max_tokens or ask for fewer scenes")


class GeminiTextAdapter(TextPort):
    def __init__(self, client: genai.Client | None = None,
                 model: str = DEFAULT_MODEL, api_key: str | None = None,
                 timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._client = client or genai.Client(
            api_key=api_key,
            # http_options takes milliseconds.
            http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))
        self._model = model

    async def generate_structured(
        self, *, schema: type[T], system: str, user: str,
        max_tokens: int = 16000, effort: str = "high",
        cache_prefix: str | None = None,
    ) -> StructuredResult[T]:
        # Stable content first so implicit caching can match the prefix; the
        # volatile instruction last.
        prompt = f"{cache_prefix}\n\n{user}" if cache_prefix else user

        async def call_once(strategy: Strategy, contents: str) -> RawCall:
            instruction = system
            cfg: dict[str, Any] = dict(
                response_mime_type="application/json",
                max_output_tokens=max_tokens,
            )
            if strategy == "native":
                cfg["response_schema"] = schema
            else:
                instruction = system + "\n\n" + SCHEMA_INSTRUCTION.format(
                    schema=json.dumps(schema.model_json_schema(), indent=1))

            started = time.perf_counter()
            resp = await self._client.aio.models.generate_content(
                model=self._model, contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=instruction, **cfg),
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            _check_refusal(resp)

            parsed = getattr(resp, "parsed", None)
            raw = getattr(resp, "text", None)
            if parsed is None and raw:
                # Without a native schema the SDK does not populate .parsed;
                # hand the extracted JSON to the shared validator instead.
                parsed = extract_json(raw)
            return RawCall(parsed=parsed, raw_text=raw,
                           usage=_usage(self._model, resp, latency_ms))

        async def attempt(repair: Repair | None) -> RawCall:
            if repair is None:
                contents = prompt
            else:
                contents = "\n\n".join(filter(None, [
                    prompt,
                    ("Here is the document you returned previously:\n"
                     + repair.prior_output) if repair.prior_output else None,
                    repair.instruction,
                ]))
            strategy = schema_strategy(schema)
            try:
                return await call_once(strategy, contents)
            except AIError:
                raise
            except json.JSONDecodeError as exc:
                raise AIError(AIErrorKind.INVALID, "malformed_json",
                              str(exc)[:200]) from exc
            except Exception as exc:
                err = _classify(exc)
                if not (strategy == "native" and err.kind is AIErrorKind.INVALID):
                    raise err from exc
                # Gemini refused the schema itself. Pin the prompt strategy for
                # this document type and carry on; our validators still enforce
                # the contract.
                _STRATEGY[schema.__name__] = "prompt"
                log.warning(
                    "gemini rejected the %s schema natively (%s); falling back "
                    "to schema-in-prompt with local validation",
                    schema.__name__, err.code)
                try:
                    return await call_once("prompt", contents)
                except AIError:
                    raise
                except Exception as exc2:
                    raise _classify(exc2) from exc2

        return await generate_with_repair(schema, attempt)
