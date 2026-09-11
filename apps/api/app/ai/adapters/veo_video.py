"""Google Veo image-to-video, via the Gemini API's long-running operations.

Graduated from ``tools/bakeoff/adapters/veo.py``. Documented shape:

    POST /v1beta/models/{model}:predictLongRunning
         {"instances": [{"prompt", "image"}], "parameters": {...}}
      -> {"name": "models/.../operations/..."}
    GET  /v1beta/{operation}
      -> {"done": bool, "response": {"generateVideoResponse": {...}}}

Auth: ``x-goog-api-key``.

STATUS: this code has never made a successful call. The bake-off never invoked
Veo (docs/adr/001-bakeoff-results.md: "0 calls ... its adapter has never
executed"), and the catalogue entries it serves are marked EXPERIMENTAL for
that reason, so `plan_motion` refuses them unless a caller explicitly opts in.
Treat every field below as documentation-derived until a real generation
succeeds.

Kept as a first-party integration rather than routed through an aggregator:
reference images (<=3) are the only real answer to character drift anywhere in
the catalogue, and having one non-aggregator adapter proves the abstraction is
an abstraction.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

from ..catalog import get as get_caps
from ..ports import (AIError, AIErrorKind, OperationState, Submission,
                     VideoRequest, VideoResult)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"


def _inline(data: bytes, mime: str) -> dict:
    return {"mimeType": mime, "bytesBase64Encoded": base64.b64encode(data).decode()}


#: A long-running operation that fails reports a google.rpc.Status, whose
#: `code` is a gRPC code -- NOT an HTTP status. Feeding one to the HTTP
#: classifier turns every operation failure into `unknown:http_8`, and the
#: consequence is not cosmetic: RESOURCE_EXHAUSTED classified as UNKNOWN is
#: not retryable, so a quota blip would fail a paid generation permanently
#: instead of backing off.
_GRPC_TO_KIND = {
    3: (AIErrorKind.INVALID, "invalid_argument"),
    5: (AIErrorKind.INVALID, "not_found"),
    7: (AIErrorKind.AUTH, "permission_denied"),
    8: (AIErrorKind.QUOTA, "resource_exhausted"),
    9: (AIErrorKind.REFUSAL, "failed_precondition"),
    13: (AIErrorKind.TRANSIENT, "internal"),
    14: (AIErrorKind.TRANSIENT, "unavailable"),
    16: (AIErrorKind.AUTH, "unauthenticated"),
}


def _classify_operation_error(err: dict) -> AIError:
    """Classify a failed operation from its gRPC status."""
    detail = str(err)[:400]
    code = err.get("code")
    if isinstance(code, int) and code in _GRPC_TO_KIND:
        kind, name = _GRPC_TO_KIND[code]
        return AIError(kind, name, detail)
    # Safety refusals arrive as a message rather than a distinct code.
    if any(w in detail.lower() for w in ("safety", "policy", "blocked")):
        return AIError(AIErrorKind.REFUSAL, "policy_blocked", detail)
    return AIError(AIErrorKind.UNKNOWN, f"grpc_{code}", detail)


def _classify(status: int, body: str) -> AIError:
    low = body.lower()
    if status in (401, 403):
        return AIError(AIErrorKind.AUTH, "unauthorized", body[:400])
    if status == 429:
        return AIError(AIErrorKind.QUOTA, "rate_limited", body[:400])
    if status == 400:
        # Veo refuses people-generation by policy in some configurations, and
        # that is a refusal to never retry as-is -- not a malformed request.
        if any(w in low for w in ("safety", "policy", "blocked", "person")):
            return AIError(AIErrorKind.REFUSAL, "policy_blocked", body[:400])
        return AIError(AIErrorKind.INVALID, "bad_request", body[:400])
    if status == 404:
        return AIError(AIErrorKind.INVALID, "model_not_found", body[:400])
    if status >= 500:
        return AIError(AIErrorKind.TRANSIENT, f"http_{status}", body[:400])
    return AIError(AIErrorKind.UNKNOWN, f"http_{status}", body[:400])


class VeoVideoAdapter:
    name = "veo"
    provider = "veo"

    def __init__(self, api_key: str, *, timeout_s: float = 120.0,
                 person_generation: str = "allow_adult") -> None:
        import httpx
        if not api_key:
            raise AIError(AIErrorKind.AUTH, "missing_key",
                          "GEMINI_API_KEY is not set")
        if not api_key.startswith("AIza"):
            # An ephemeral AI Studio token ("AQ.…") authenticates for a few
            # minutes and then 401s on everything, which reads as a broken
            # integration rather than an expired credential. Named here so it
            # is diagnosed in one line instead of one afternoon.
            raise AIError(
                AIErrorKind.AUTH, "not_an_api_key",
                f"GEMINI_API_KEY does not look like a Gemini API key: expected "
                f"a 39-character value starting 'AIza', got {len(api_key)} "
                f"characters starting {api_key[:3]!r}. Ephemeral tokens "
                f"(AQ.…) expire within minutes. Create a durable key at "
                f"https://aistudio.google.com/apikey")
        self._key = api_key
        self._person_generation = person_generation
        self._client = httpx.AsyncClient(
            timeout=timeout_s,
            headers={"x-goog-api-key": api_key,
                     "Content-Type": "application/json"})

    def serves(self, adapter_name: str) -> bool:
        return adapter_name == "veo"

    async def submit(self, req: VideoRequest) -> Submission:
        caps = get_caps(req.model_key)
        payload = self.build_payload(req)

        r = await self._client.post(
            f"{API_BASE}/models/{req.model_id}:predictLongRunning", json=payload)
        if r.status_code >= 400:
            raise _classify(r.status_code, r.text)
        name = r.json().get("name")
        if not name:
            raise AIError(AIErrorKind.UNKNOWN, "no_operation_name", r.text[:400])
        expires = (datetime.now(timezone.utc)
                   + timedelta(hours=caps.retention_hours)
                   if caps.retention_hours else None)
        return Submission(
            provider_job_id=name,
            endpoint=f"{API_BASE}/models/{req.model_id}:predictLongRunning",
            # Veo deletes generated media after its retention window, so the
            # download is part of generation and never a later step.
            expires_at=expires.isoformat() if expires else None,
            raw={"operation": name})

    def build_payload(self, req: VideoRequest) -> dict:
        """Assemble the request, filtered through the catalogue's allowlist.

        Separated from `submit` so it can be asserted without a network call,
        which matters more here than anywhere else: nothing in this file has
        ever run against the live API.

        The allowlist is the point. The bake-off paid to learn that Kling and
        Hailuo reject fields they do not declare, and the fix -- a per-model
        `request_fields` set -- was applied only to the fal adapter. The Veo
        adapter it was copied from built its parameters dict unconditionally,
        so the same class of 400 was still live here.
        """
        caps = get_caps(req.model_key)
        candidate = {
            "durationSeconds": int(req.duration_s),
            "resolution": req.resolution,
            "aspectRatio": req.aspect_ratio,
            "personGeneration": self._person_generation,
        }
        if req.seed is not None:
            candidate["seed"] = req.seed
        if req.negative_prompt:
            candidate["negativePrompt"] = req.negative_prompt
        if req.reference_images:
            candidate["referenceImages"] = [
                {"image": _inline(b, "image/png")}
                for b in req.reference_images[:caps.max_reference_images]]

        parameters = {k: v for k, v in candidate.items()
                      if k in caps.request_fields}
        parameters.update(caps.extra_params)
        instance = {"prompt": req.prompt,
                    "image": _inline(req.first_frame, req.first_frame_mime)}
        # The closing keyframe is an *instance* field, a sibling of `image`,
        # not a generation parameter -- so it is gated on the catalogue naming
        # it rather than on `request_fields`, which is the parameters
        # allowlist and would silently drop it.
        if req.last_frame and caps.last_frame_field:
            instance[caps.last_frame_field] = _inline(
                req.last_frame, req.last_frame_mime or req.first_frame_mime)
        return {"instances": [instance], "parameters": parameters}

    async def poll(self, sub: Submission) -> OperationState:
        r = await self._client.get(f"{API_BASE}/{sub.provider_job_id}")
        if r.status_code >= 400:
            return OperationState(done=True,
                                  error=_classify(r.status_code, r.text))
        body = r.json()
        if not body.get("done"):
            return OperationState(done=False, progress_hint="generating",
                                  raw=body)
        if err := body.get("error"):
            return OperationState(done=True, raw=body,
                                  error=_classify_operation_error(err))

        resp = body.get("response", {})
        samples = (resp.get("generateVideoResponse", {}).get("generatedSamples")
                   or resp.get("generatedSamples") or [])
        uri = (samples[0].get("video") or {}).get("uri") if samples else None
        if not uri:
            return OperationState(done=True, raw=body, error=AIError(
                AIErrorKind.UNKNOWN, "no_video_in_response", str(resp)[:400]))
        return OperationState(
            done=True, video_uri=uri,
            reported_cost_cents=None,     # Gemini reports no per-call charge
            model_version=resp.get("modelVersion"),
            raw={"response_keys": list(resp)})

    async def fetch(self, state: OperationState) -> VideoResult:
        r = await self._client.get(state.video_uri,
                                   headers={"x-goog-api-key": self._key})
        if r.status_code in (404, 410):
            raise AIError(
                AIErrorKind.EXPIRED, "media_expired",
                "Veo's retention window elapsed before this clip was "
                "downloaded; it must be regenerated, which costs again")
        if r.status_code >= 400:
            raise _classify(r.status_code, "clip download failed")
        return VideoResult(data=r.content,
                           mime=r.headers.get("content-type", "video/mp4"))

    async def aclose(self) -> None:
        await self._client.aclose()
