"""Shared adapter for Runway-compatible task APIs (Runway, Luma).

Both follow submit -> task id -> poll -> asset URL, differing only in paths and
field names, so one adapter plus a small dialect table serves both.

STATUS: EXPERIMENTAL. Endpoint paths and status values were confirmed at shape
level from public docs; the exact request-body field names were not. Any model
using this adapter is marked EXPERIMENTAL in the catalogue and the harness
refuses to run it without ``--allow-experimental``. Confirm the dialect below
against live docs, flip the catalogue entry to ACTIVE, and remove this notice.
"""
from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from adapters.base import (
    ErrorKind, FetchResult, OperationState, ProviderError, Submission,
    VideoRequest,
)
from probe import sha256_file
from records import utcnow


def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def _dig(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(obj, list):
            obj = obj[int(part)] if part.isdigit() and len(obj) > int(part) else None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
        if obj is None:
            return None
    return obj


@dataclass(frozen=True)
class Dialect:
    provider: str
    base_url: str
    submit_path: str
    poll_path: str                      # "{id}" placeholder
    auth_header: Callable[[str], dict[str, str]]
    build_body: Callable[[VideoRequest], dict]
    id_path: str
    status_path: str
    terminal_ok: tuple[str, ...]
    terminal_fail: tuple[str, ...]
    video_uri_paths: tuple[str, ...]
    version_path: str | None = None


RUNWAY = Dialect(
    provider="runway",
    base_url="https://api.dev.runwayml.com",
    submit_path="/v1/image_to_video",
    poll_path="/v1/tasks/{id}",
    auth_header=lambda k: {"Authorization": f"Bearer {k}",
                           "X-Runway-Version": "2024-11-06"},
    build_body=lambda r: {
        "promptImage": _data_uri(r.first_frame_path),
        "promptText": r.prompt,
        "model": r.model_id,
        "ratio": "1280:720" if r.resolution == "720p" else "1920:1080",
        "duration": int(r.duration_s),
        **({"seed": r.seed} if r.seed is not None else {}),
    },
    id_path="id",
    status_path="status",
    terminal_ok=("SUCCEEDED",),
    terminal_fail=("FAILED", "CANCELED"),
    video_uri_paths=("output.0", "output.0.url"),
)

LUMA = Dialect(
    provider="luma",
    base_url="https://api.lumalabs.ai/dream-machine/v1",
    submit_path="/generations",
    poll_path="/generations/{id}",
    auth_header=lambda k: {"Authorization": f"Bearer {k}"},
    build_body=lambda r: {
        "prompt": r.prompt,
        "model": r.model_id,
        "resolution": r.resolution,
        "duration": f"{int(r.duration_s)}s",
        "aspect_ratio": r.aspect_ratio,
        "keyframes": {"frame0": {"type": "image",
                                 "url": _data_uri(r.first_frame_path)}},
    },
    id_path="id",
    status_path="state",
    terminal_ok=("completed",),
    terminal_fail=("failed",),
    video_uri_paths=("assets.video",),
)

DIALECTS = {"runway": RUNWAY, "luma": LUMA}


def _classify(status_code: int, body: str) -> ProviderError:
    if status_code in (401, 403):
        return ProviderError(ErrorKind.AUTH, "unauthorized", body[:400])
    if status_code == 429:
        return ProviderError(ErrorKind.QUOTA, "rate_limited", body[:400])
    if status_code in (400, 422):
        return ProviderError(ErrorKind.INVALID, "bad_request", body[:400])
    if status_code >= 500:
        return ProviderError(ErrorKind.TRANSIENT, f"http_{status_code}", body[:400])
    return ProviderError(ErrorKind.UNKNOWN, f"http_{status_code}", body[:400])


class TaskApiVideoAdapter:
    name = "task_api"

    def __init__(self, keys: dict[str, str], *, timeout: float = 120.0) -> None:
        import httpx
        self._keys = keys
        self._client = httpx.AsyncClient(timeout=timeout)

    def serves(self, adapter_name: str) -> bool:
        return adapter_name == "task_api"

    def _dialect(self, provider: str) -> tuple[Dialect, dict[str, str]]:
        d = DIALECTS.get(provider)
        if d is None:
            raise ProviderError(ErrorKind.INVALID, "no_dialect", provider)
        key = self._keys.get(provider, "")
        if not key:
            raise ProviderError(ErrorKind.AUTH, "missing_key",
                                f"no API key configured for {provider}")
        return d, {**d.auth_header(key), "Content-Type": "application/json"}

    async def submit(self, req: VideoRequest) -> Submission:
        from catalog import get as get_caps
        d, headers = self._dialect(get_caps(req.model_key).provider)
        r = await self._client.post(f"{d.base_url}{d.submit_path}",
                                    json=d.build_body(req), headers=headers)
        if r.status_code >= 400:
            raise _classify(r.status_code, r.text)
        body = r.json()
        task_id = _dig(body, d.id_path)
        if not task_id:
            raise ProviderError(ErrorKind.UNKNOWN, "no_task_id", r.text[:400])
        return Submission(provider_job_id=str(task_id),
                          endpoint=f"{d.base_url}{d.submit_path}",
                          submitted_at=utcnow(), raw={"provider": d.provider})

    async def poll(self, sub: Submission) -> OperationState:
        d, headers = self._dialect(sub.raw["provider"])
        url = f"{d.base_url}{d.poll_path.format(id=sub.provider_job_id)}"
        r = await self._client.get(url, headers=headers)
        if r.status_code >= 400:
            return OperationState(done=True, error=_classify(r.status_code, r.text))
        body = r.json()
        status = str(_dig(body, d.status_path) or "")
        if status in d.terminal_fail:
            return OperationState(done=True, raw=body, error=ProviderError(
                ErrorKind.UNKNOWN, "task_failed",
                str(body.get("failure_reason") or body.get("error") or status)))
        if status not in d.terminal_ok:
            return OperationState(done=False, progress_hint=status or "pending",
                                  raw=body)
        uri = next((u for p in d.video_uri_paths if (u := _dig(body, p))), None)
        if not uri:
            return OperationState(done=True, raw=body, error=ProviderError(
                ErrorKind.UNKNOWN, "no_video_in_response", str(body)[:400]))
        return OperationState(
            done=True, video_uri=uri, reported_cost_cents=None,
            model_version=_dig(body, d.version_path) if d.version_path else None,
            raw={"status": status})

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
