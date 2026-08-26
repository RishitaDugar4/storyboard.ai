"""fal queue API adapter.

Shape verified against https://fal.ai/docs/model-endpoints/queue (2026-08-25):
    POST   https://queue.fal.run/{model_id}      -> {request_id, status_url,
                                                    response_url, cancel_url}
    GET    {status_url}   -> {"status": IN_QUEUE | IN_PROGRESS | COMPLETED}
    GET    {response_url} -> model-specific payload; video models return
                             {"video": {"url": ...}}
Auth: ``Authorization: Key $FAL_KEY``.

One adapter serves every fal-hosted model in the catalogue -- the single
largest work saving available, and what makes interchangeable providers
affordable to build.
"""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from adapters.base import (
    ErrorKind, FetchResult, OperationState, ProviderError, Submission,
    VideoRequest,
)
from catalog import get as get_caps
from probe import sha256_file
from records import utcnow

QUEUE_BASE = "https://queue.fal.run"


def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def _classify(status_code: int, body: str) -> ProviderError:
    if status_code in (401, 403):
        return ProviderError(ErrorKind.AUTH, "unauthorized", body[:400])
    if status_code == 429:
        return ProviderError(ErrorKind.QUOTA, "rate_limited", body[:400])
    if status_code == 422:
        # Almost always a catalogue bug: wrong model_id or a field this model
        # does not accept. Fix the data, not the call site.
        return ProviderError(ErrorKind.INVALID, "unprocessable", body[:400])
    if status_code >= 500:
        return ProviderError(ErrorKind.TRANSIENT, f"http_{status_code}", body[:400])
    if status_code == 404:
        return ProviderError(ErrorKind.INVALID, "model_not_found", body[:400])
    return ProviderError(ErrorKind.UNKNOWN, f"http_{status_code}", body[:400])


class FalVideoAdapter:
    name = "fal"

    def __init__(self, api_key: str, *, timeout: float = 120.0) -> None:
        import httpx
        if not api_key:
            raise ProviderError(ErrorKind.AUTH, "missing_key", "FAL_KEY is not set")
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Key {api_key}",
                     "Content-Type": "application/json"},
        )

    def serves(self, adapter_name: str) -> bool:
        return adapter_name == "fal"

    async def submit(self, req: VideoRequest) -> Submission:
        caps = get_caps(req.model_key)
        # Build every field we *could* send, then keep only the ones this
        # endpoint actually defines. Sending an undeclared field is impossible
        # by construction, and adding a model never touches this code.
        candidate: dict = {
            "image_url": _data_uri(req.first_frame_path),
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

        r = await self._client.post(f"{QUEUE_BASE}/{req.model_id}", json=payload)
        if r.status_code >= 400:
            raise _classify(r.status_code, r.text)
        body = r.json()
        return Submission(
            provider_job_id=body["request_id"],
            endpoint=f"{QUEUE_BASE}/{req.model_id}",
            submitted_at=utcnow(),
            expires_at=None,
            raw={"status_url": body.get("status_url"),
                 "response_url": body.get("response_url"),
                 "cancel_url": body.get("cancel_url")},
        )

    async def poll(self, sub: Submission) -> OperationState:
        status_url = sub.raw.get("status_url")
        r = await self._client.get(status_url)
        if r.status_code >= 400:
            return OperationState(done=True, error=_classify(r.status_code, r.text))
        body = r.json()
        status = body.get("status")
        if status in ("IN_QUEUE", "IN_PROGRESS"):
            pos = body.get("queue_position")
            return OperationState(
                done=False,
                progress_hint=(f"queued at {pos}" if pos is not None else "in progress"),
                raw=body)
        if status != "COMPLETED":
            return OperationState(done=True, error=ProviderError(
                ErrorKind.UNKNOWN, "unexpected_status", str(status)), raw=body)

        rr = await self._client.get(sub.raw["response_url"])
        if rr.status_code >= 400:
            return OperationState(done=True, error=_classify(rr.status_code, rr.text))
        result = rr.json()
        video = result.get("video") or {}
        uri = video.get("url")
        if not uri and isinstance(result.get("videos"), list) and result["videos"]:
            uri = result["videos"][0].get("url")
        if not uri:
            return OperationState(done=True, error=ProviderError(
                ErrorKind.UNKNOWN, "no_video_in_response", str(result)[:400]),
                raw=result)
        return OperationState(
            done=True, video_uri=uri,
            # fal does not return a per-request charge; cost stays estimated.
            reported_cost_cents=None,
            model_version=result.get("model_version"),
            raw={"metrics": body.get("metrics"), "result_keys": list(result)},
        )

    async def fetch(self, state: OperationState, dest: Path) -> FetchResult:
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with self._client.stream("GET", state.video_uri) as r:
            if r.status_code >= 400:
                raise _classify(r.status_code, "download failed")
            with dest.open("wb") as fh:
                async for chunk in r.aiter_bytes(1 << 16):
                    fh.write(chunk)
            ctype = r.headers.get("content-type")
        return FetchResult(dest, dest.stat().st_size, sha256_file(dest), ctype)

    async def aclose(self) -> None:
        await self._client.aclose()
