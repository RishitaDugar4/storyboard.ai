"""Provider-independent structured-generation plumbing.

Every provider can enforce a JSON Schema server-side, and none of them can
enforce our cross-field rules -- "this slug must appear in that list" is not
expressible in JSON Schema. So validation and repair live here, once, and each
adapter supplies only the part that differs: making a single call.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from .ports import AIError, AIErrorKind, StructuredResult, Usage

log = logging.getLogger("hbz.ai")
T = TypeVar("T", bound=BaseModel)

REPAIR_TEMPLATE = """Your previous response was valid JSON and matched the \
schema, but it broke rules that are checked after parsing:

{errors}

Return the corrected document. Change only what is needed to satisfy these \
rules; keep everything else identical."""


@dataclass
class RawCall:
    """One provider round-trip, normalised."""

    parsed: Any                 # dict or BaseModel, already schema-valid
    raw_text: str | None
    usage: Usage


@dataclass
class Repair:
    """What the model needs to fix its own work.

    `prior_output` matters as much as the errors: without it the model has no
    memory of what it wrote, regenerates from scratch, and reproduces the same
    mistake. Sending errors alone is why a repair silently fails.
    """

    instruction: str
    prior_output: str | None


#: Given an optional repair, perform one call.
AttemptFn = Callable[["Repair | None"], Awaitable[RawCall]]


def format_errors(exc: ValidationError) -> str:
    """Render validator failures as instructions the model can act on."""
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(document)"
        msg = err["msg"].replace("Value error, ", "")
        lines.append(f"- {loc}: {msg}")
    return "\n".join(lines)


async def generate_with_repair(schema: type[T], attempt: AttemptFn
                               ) -> StructuredResult[T]:
    """Call, validate, and on cross-field failure give the model its own
    errors back -- exactly once.

    One retry, not more: a second failure is a prompt problem, and spending
    more tokens will not fix a contract the instructions never explained.
    """
    repair_errors: list[str] = []
    prior_output: str | None = None

    for attempt_no in range(2):
        call = await attempt(
            None if attempt_no == 0 else Repair(
                instruction=REPAIR_TEMPLATE.format(
                    errors="\n".join(repair_errors)),
                prior_output=prior_output))

        if call.parsed is None:
            raise AIError(AIErrorKind.INVALID, "no_parsed_output",
                          "provider returned no parseable document",
                          raw=call.raw_text)

        payload = (call.parsed.model_dump() if isinstance(call.parsed, BaseModel)
                   else call.parsed)
        try:
            value = schema.model_validate(payload)
        except ValidationError as exc:
            errors = format_errors(exc)
            if attempt_no == 1:
                raise AIError(
                    AIErrorKind.INVALID, "validation_failed",
                    "model could not satisfy the contract after one repair "
                    "attempt:\n" + errors, raw=call.raw_text) from exc
            log.info("repair triggered: %s", errors.replace("\n", " | "))
            repair_errors = errors.splitlines()
            prior_output = call.raw_text
            continue

        return StructuredResult(
            value=value, usage=call.usage, repaired=attempt_no > 0,
            repair_errors=repair_errors, raw_text=call.raw_text,
        )

    raise AIError(AIErrorKind.UNKNOWN, "unreachable", "repair loop fell through")
