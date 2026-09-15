"""One credential rule for every Google-hosted adapter.

Text, speech and Veo all authenticate with the same ``GEMINI_API_KEY``, so the
rule lives in one place and is applied in all three. Previously only Veo
checked anything, and the two that did not failed later with a bare 401.

WHAT THIS DELIBERATELY DOES NOT DO: reject a key on its shape.

An earlier version refused anything not starting ``AIza``, on the belief that
an ``AQ.`` value was an ephemeral AI Studio session token. That was wrong.
``AQ.`` is Google's *authorization key* format -- bound to a service account,
scoped to the Gemini API, durable -- and it is the direction the Gemini API is
moving. A live ``AQ.`` key returns 200 from ``GET /v1beta/models`` listing all
50 models, which is how the belief was finally settled.

The guard was therefore refusing a perfectly good credential, and doing it at
construction time so that nothing downstream could recover. That is a worse
failure than the one it was written to prevent, and the general lesson is the
reason this comment is long: a provider owns its credential format and will
change it, so an allowlist of prefixes here is a clock counting down to an
outage that looks like our bug. The API is the authority on whether a key
works. Ask it, and classify the 401 well when it says no.

What survives is the check that cannot be wrong: a key that is absent is
absent.
"""
from __future__ import annotations

from ..ports import AIError, AIErrorKind

#: Recognised Gemini credential formats, for diagnostics ONLY -- never to
#: refuse on. An unfamiliar prefix means we have not seen it, not that it is
#: invalid.
KNOWN_PREFIXES = {
    "AIza": "standard API key (project-scoped, for billing and quota)",
    "AQ.": "authorization key (service-account bound, Gemini-scoped)",
}


def describe_key(api_key: str) -> str:
    """A human label for a credential, for logs and error messages."""
    for prefix, label in KNOWN_PREFIXES.items():
        if api_key.startswith(prefix):
            return label
    return "unrecognised format"


def validate_gemini_key(api_key: str | None, *, capability: str = "") -> str:
    """Return the key, or raise if there is nothing to authenticate with.

    `capability` appears in the message so a failure points at the call that
    provoked it ("narration", "video") rather than leaving the operator to
    guess which of three adapters spoke.
    """
    if not api_key:
        where = f" ({capability})" if capability else ""
        raise AIError(
            AIErrorKind.AUTH, "missing_key",
            f"GEMINI_API_KEY is not set{where}. Create a key at "
            f"https://aistudio.google.com/apikey -- either format works.")
    return api_key
