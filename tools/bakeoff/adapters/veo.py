"""Google Veo adapter (Gemini API, direct).

Long-running-operation shape per https://ai.google.dev/gemini-api/docs/veo:
    POST /v1beta/models/{model}:predictLongRunning  -> {"name": "<operation>"}
    GET  /v1beta/{operation}                        -> {"done": bool, "response": ...}
Auth: ``x-goog-api-key``.

Kept as a first-party integration rather than routed through an aggregator:
reference images (<=3) and the 48h media-retention behaviour are worth owning
directly, and having one non-aggregator adapter proves the abstraction.

NOTE: the REST body below follows the documented predict shape. Run
``python run.py --preflight`` before the first paid run to confirm it against
your key and model.
"""
from __future__ import annotations

import base64
import mimetypes
from datetime import timedelta
from pathlib import Path

from adapters.base import (
    ErrorKind, FetchResult, OperationState, ProviderError, Submission,
    VideoRequest,
)
from catalog import get as get_caps
from probe import sha256_file
from records import utcnow

API_BASE = "https://generativelanguage.googleapis.com/v1beta"


def _inline(path: Path) -> dict:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return {"mimeType": mime,
            "bytesBase64Encoded": base64.b64encode(path.read_bytes()).decode()}


def _classify(status_code: int, body: str) -> ProviderError:
    low = body.lower()
    if status_code in (401, 403):
        return ProviderError(ErrorKind.AUTH, "unauthorized", body[:400])
    if status_code == 429:
        return ProviderError(ErrorKind.QUOTA, "rate_limited", body[:400])
    if status_code == 400:
        if any(w in low for w in ("safety", "policy", "blocked", "person")):
            return ProviderError(ErrorKind.REFUSAL, "policy_blocked", body[:400])
        return ProviderError(ErrorKind.INVALID, "bad_request", body[:400])
    if status_code >= 500:
        return ProviderError(ErrorKind.TRANSIENT, f"http_{status_code}", body[:400])
    return ProviderError(ErrorKind.UNKNOWN, f"http_{status_code}", body[:400])


class VeoVideoAdapter:
    name = "veo"

    def __init__(self, api_key: str, *, timeout: float = 120.0,
                 person_generation: str = "allow_adult") -> None:
        import httpx
        if not api_key:
            raise ProviderError(ErrorKind.AUTH, "missing_key",
                                "GEMINI_API_KEY is not set")
        self._key = api_key
        self._person_generation = person_generation
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"x-goog-api-key": api_key,
                     "Content-Type": "application/json"},
        )

    def serves(self, adapter_name: str) -> bool:
        return adapter_name == "veo"

    async def submit(self, req: VideoRequest) -> Submission:
        caps = get_caps(req.model_key)
        parameters: dict = {
            "durationSeconds": int(req.duration_s),
            "resolution": req.resolution,
            "aspectRatio": req.aspect_ratio,
            "personGeneration": self._person_generation,
        }
        if req.seed is not None:
            parameters["seed"] = req.seed
        if req.reference_paths:
            parameters["referenceImages"] = [
                {"image": _inline(p)} for p in req.reference_paths
            ]
        instance: dict = {"prompt": req.prompt, "image": _inline(req.first_frame_path)}

        r = await self._client.post(
            f"{API_BASE}/models/{req.model_id}:predictLongRunning",
            json={"instances": [instance], "parameters": parameters},
        )
        if r.status_code >= 400:
            raise _classify(r.status_code, r.text)
        name = r.json().get("name")
        if not name:
            raise ProviderError(ErrorKind.UNKNOWN, "no_operation_name", r.text[:400])
        now = utcnow()
        return Submission(
            provider_job_id=name,
            endpoint=f"{API_BASE}/models/{req.model_id}:predictLongRunning",
            submitted_at=now,
            # Veo deletes generated media after its retention window. Download
            # is part of generation, never a later step.
            expires_at=(now + timedelta(hours=caps.retention_hours)
                        if caps.retention_hours else None),
        )

    async def poll(self, sub: Submission) -> OperationState:
        r = await self._client.get(f"{API_BASE}/{sub.provider_job_id}")
        if r.status_code >= 400:
            return OperationState(done=True, error=_classify(r.status_code, r.text))
        body = r.json()
        if not body.get("done"):
            return OperationState(done=False, progress_hint="generating", raw=body)
        if err := body.get("error"):
            return OperationState(done=True, raw=body, error=_classify(
                int(err.get("code", 0)) if str(err.get("code", "")).isdigit() else 500,
                str(err)))

        resp = body.get("response", {})
        samples = (resp.get("generateVideoResponse", {}).get("generatedSamples")
                   or resp.get("generatedSamples") or [])
        uri = None
        if samples:
            uri = (samples[0].get("video") or {}).get("uri")
        if not uri:
            return OperationState(done=True, raw=body, error=ProviderError(
                ErrorKind.UNKNOWN, "no_video_in_response", str(resp)[:400]))
        return OperationState(
            done=True, video_uri=uri,
            reported_cost_cents=None,       # Gemini reports no per-call charge
            model_version=resp.get("modelVersion"),
            raw={"response_keys": list(resp)},
        )

    async def fetch(self, state: OperationState, dest: Path) -> FetchResult:
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with self._client.stream(
            "GET", state.video_uri, headers={"x-goog-api-key": self._key}
        ) as r:
            if r.status_code >= 400:
                if r.status_code in (404, 410):
                    raise ProviderError(ErrorKind.EXPIRED, "media_expired",
                                        "generated video no longer available")
                raise _classify(r.status_code, "download failed")
            with dest.open("wb") as fh:
                async for chunk in r.aiter_bytes(1 << 16):
                    fh.write(chunk)
            ctype = r.headers.get("content-type")
        return FetchResult(dest, dest.stat().st_size, sha256_file(dest), ctype)

    async def aclose(self) -> None:
        await self._client.aclose()
