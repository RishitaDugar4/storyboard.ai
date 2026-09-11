"""fal image-to-video, over the queue API.

Graduated from ``tools/bakeoff/adapters/fal.py``, which made the only real
generations this project has performed (docs/adr/001-bakeoff-results.md).
Shape verified against https://fal.ai/docs/model-endpoints/queue:

    POST   https://queue.fal.run/{model_id}  -> {request_id, status_url,
                                                 response_url, cancel_url}
    GET    {status_url}    -> {"status": IN_QUEUE | IN_PROGRESS | COMPLETED}
    GET    {response_url}  -> {"video": {"url": ...}}

One adapter serves every fal-hosted model in the catalogue. That is the single
largest work saving available here, and it is what makes interchangeable
providers affordable to build rather than merely aspirational.
"""
from __future__ import annotations

import base64

from ..catalog import get as get_caps
from ..ports import (AIError, AIErrorKind, OperationState, Submission,
                     VideoRequest, VideoResult)

QUEUE_BASE = "https://queue.fal.run"


def _data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _classify(status: int, body: str) -> AIError:
    if status in (401, 403):
        # An exhausted fal balance also returns 403. Calling that
        # "unauthorized" sends people hunting for a key when the fix is a
        # top-up -- a correction the bake-off had to make the expensive way.
        if "exhausted" in body.lower() or "balance" in body.lower():
            return AIError(AIErrorKind.QUOTA, "balance_exhausted", body[:400])
        return AIError(AIErrorKind.AUTH, "unauthorized", body[:400])
    if status == 429:
        return AIError(AIErrorKind.QUOTA, "rate_limited", body[:400])
    if status == 422:
        # Almost always a catalogue bug: a wrong model_id, or a field this
        # endpoint does not define. Fix the data, not the call site.
        return AIError(AIErrorKind.INVALID, "unprocessable", body[:400])
    if status == 404:
        return AIError(AIErrorKind.INVALID, "model_not_found", body[:400])
    if status >= 500:
        return AIError(AIErrorKind.TRANSIENT, f"http_{status}", body[:400])
    return AIError(AIErrorKind.UNKNOWN, f"http_{status}", body[:400])


class FalVideoAdapter:
    name = "fal"
    provider = "fal"

    def __init__(self, api_key: str, *, timeout_s: float = 120.0) -> None:
        import httpx
        if not api_key:
            raise AIError(AIErrorKind.AUTH, "missing_key",
                          "FAL_KEY is not set. Create one at "
                          "https://fal.ai/dashboard/keys and put it in .env")
        if ":" not in api_key:
            raise AIError(
                AIErrorKind.AUTH, "malformed_key",
                f"FAL_KEY does not look like a fal key: expected "
                f"'<key-id>:<key-secret>', got {len(api_key)} characters with "
                f"no colon.")
        self._client = httpx.AsyncClient(
            timeout=timeout_s,
            headers={"Authorization": f"Key {api_key}",
                     "Content-Type": "application/json"})

    def serves(self, adapter_name: str) -> bool:
        return adapter_name == "fal"

    async def submit(self, req: VideoRequest) -> Submission:
        caps = get_caps(req.model_key)
        # Build every field we *could* send, then keep only the ones this
        # endpoint actually declares. Sending an undeclared field is
        # impossible by construction, and adding a model never touches this
        # code. The bake-off found three separate cases where the old
        # send-everything approach produced a 422.
        candidate: dict = {
            "image_url": _data_uri(req.first_frame, req.first_frame_mime),
            "prompt": req.prompt,
            "duration": str(int(req.duration_s)),
            "resolution": req.resolution,
            "aspect_ratio": req.aspect_ratio,
        }
        if req.negative_prompt:
            candidate["negative_prompt"] = req.negative_prompt
        if req.seed is not None:
            candidate["seed"] = req.seed
        payload = {k: v for k, v in candidate.items() if k in caps.request_fields}
        payload.update(caps.extra_params)
        # Written after the allowlist on purpose: the catalogue naming the
        # field IS the declaration that this endpoint accepts it, so filtering
        # it through a second list could only ever drop a field we just said
        # was legal. Models with no closing-frame input leave it None and
        # nothing is sent.
        if req.last_frame and caps.last_frame_field:
            payload[caps.last_frame_field] = _data_uri(
                req.last_frame, req.last_frame_mime or req.first_frame_mime)

        r = await self._client.post(f"{QUEUE_BASE}/{req.model_id}", json=payload)
        if r.status_code >= 400:
            raise _classify(r.status_code, r.text)
        body = r.json()
        return Submission(
            provider_job_id=body["request_id"],
            endpoint=f"{QUEUE_BASE}/{req.model_id}",
            expires_at=None,          # fal keeps results indefinitely
            raw={"status_url": body.get("status_url"),
                 "response_url": body.get("response_url"),
                 "cancel_url": body.get("cancel_url")})

    async def poll(self, sub: Submission) -> OperationState:
        r = await self._client.get(sub.raw["status_url"])
        if r.status_code >= 400:
            return OperationState(done=True,
                                  error=_classify(r.status_code, r.text))
        body = r.json()
        status = body.get("status")
        if status in ("IN_QUEUE", "IN_PROGRESS"):
            pos = body.get("queue_position")
            return OperationState(
                done=False, raw=body,
                progress_hint=(f"queued at {pos}" if pos is not None
                               else "rendering"))
        if status != "COMPLETED":
            return OperationState(done=True, raw=body, error=AIError(
                AIErrorKind.UNKNOWN, "unexpected_status", str(status)))

        rr = await self._client.get(sub.raw["response_url"])
        if rr.status_code >= 400:
            return OperationState(done=True,
                                  error=_classify(rr.status_code, rr.text))
        result = rr.json()
        uri = (result.get("video") or {}).get("url")
        if not uri and isinstance(result.get("videos"), list) and result["videos"]:
            uri = result["videos"][0].get("url")
        if not uri:
            return OperationState(done=True, raw=result, error=AIError(
                AIErrorKind.UNKNOWN, "no_video_in_response", str(result)[:400]))
        return OperationState(
            done=True, video_uri=uri,
            # fal reports no per-request charge, so cost stays estimated and
            # the budget gate -- not the invoice -- is what governs spend.
            reported_cost_cents=None,
            model_version=result.get("model_version"),
            raw={"metrics": body.get("metrics")})

    async def fetch(self, state: OperationState) -> VideoResult:
        r = await self._client.get(state.video_uri)
        if r.status_code == 404:
            raise AIError(AIErrorKind.EXPIRED, "media_gone",
                          "the provider no longer has this clip; it must be "
                          "regenerated, which costs again")
        if r.status_code >= 400:
            raise _classify(r.status_code, "clip download failed")
        return VideoResult(data=r.content,
                           mime=r.headers.get("content-type", "video/mp4"))

    async def aclose(self) -> None:
        await self._client.aclose()
