"""Claude adapter.

`messages.parse()` enforces the JSON Schema server-side, so shape errors are
largely impossible. The cross-field rules it cannot express -- referential
integrity, narration pacing -- are validated and repaired by the shared
`ai.structured` loop, which both adapters use.
"""
from __future__ import annotations

import time
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from ..ports import AIError, AIErrorKind, StructuredResult, TextPort, Usage
from ..structured import RawCall, Repair, generate_with_repair

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-opus-5"

#: See the Gemini adapter: a hung request must not block a worker forever.
DEFAULT_TIMEOUT_S = 300

#: USD per million tokens (Claude API list prices).
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def _cost_cents(model: str, usage: Any) -> float:
    inp, out = PRICING.get(model, (0.0, 0.0))
    it = getattr(usage, "input_tokens", 0) or 0
    ot = getattr(usage, "output_tokens", 0) or 0
    # Cache reads bill at a fraction of input; counting them as input is a
    # slight over-estimate, which is the safe direction for a budget.
    it += getattr(usage, "cache_read_input_tokens", 0) or 0
    it += getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (it / 1e6) * inp * 100 + (ot / 1e6) * out * 100


def _usage(model: str, resp: Any, latency_ms: int) -> Usage:
    u = getattr(resp, "usage", None)
    return Usage(
        model=model,
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        latency_ms=latency_ms,
        cost_cents=_cost_cents(model, u) if u else 0.0,
    )


def _classify(exc: Exception) -> AIError:
    if isinstance(exc, anthropic.AuthenticationError):
        return AIError(AIErrorKind.AUTH, "unauthorized", str(exc)[:300])
    if isinstance(exc, anthropic.RateLimitError):
        return AIError(AIErrorKind.QUOTA, "rate_limited", str(exc)[:300])
    if isinstance(exc, anthropic.APIStatusError):
        code = exc.status_code
        kind = (AIErrorKind.TRANSIENT if code >= 500
                else AIErrorKind.INVALID if code == 400
                else AIErrorKind.UNKNOWN)
        return AIError(kind, f"http_{code}", str(exc)[:300])
    if isinstance(exc, anthropic.APIConnectionError):
        return AIError(AIErrorKind.TRANSIENT, "connection", str(exc)[:300])
    return AIError(AIErrorKind.UNKNOWN, type(exc).__name__, str(exc)[:300])


def _first_text(resp: Any) -> str | None:
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    return None


class AnthropicTextAdapter(TextPort):
    def __init__(self, client: anthropic.AsyncAnthropic | None = None,
                 model: str = DEFAULT_MODEL,
                 timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._client = client or anthropic.AsyncAnthropic(timeout=timeout_s)
        self._model = model

    async def generate_structured(
        self, *, schema: type[T], system: str, user: str,
        max_tokens: int = 16000, effort: str = "high",
        cache_prefix: str | None = None,
    ) -> StructuredResult[T]:
        # Stable content first so the cached prefix survives; the volatile
        # instruction last. On a project where scenes are regenerated one at a
        # time, this is the difference between paying for the story once and
        # paying for it every time.
        system_blocks: list[dict] = [{"type": "text", "text": system}]
        if cache_prefix:
            system_blocks.append({
                "type": "text", "text": cache_prefix,
                "cache_control": {"type": "ephemeral"},
            })

        async def attempt(repair: Repair | None) -> RawCall:
            messages: list[dict] = [{"role": "user", "content": user}]
            if repair:
                # The model must see its own prior document, or it regenerates
                # blind and repeats the mistake.
                if repair.prior_output:
                    messages.append({"role": "assistant",
                                     "content": repair.prior_output})
                messages.append({"role": "user", "content": repair.instruction})
            started = time.perf_counter()
            try:
                resp = await self._client.messages.parse(
                    model=self._model,
                    max_tokens=max_tokens,
                    system=system_blocks,
                    messages=messages,
                    output_format=schema,
                    # Adaptive thinking; budget_tokens is rejected on current
                    # models. Depth is tuned with effort instead.
                    thinking={"type": "adaptive"},
                    output_config={"effort": effort},
                )
            except Exception as exc:
                raise _classify(exc) from exc

            latency_ms = int((time.perf_counter() - started) * 1000)
            if getattr(resp, "stop_reason", None) == "refusal":
                raise AIError(AIErrorKind.REFUSAL, "provider_refused",
                              str(getattr(resp, "stop_details", "")))
            return RawCall(parsed=getattr(resp, "parsed_output", None),
                           raw_text=_first_text(resp),
                           usage=_usage(self._model, resp, latency_ms))

        return await generate_with_repair(schema, attempt)
