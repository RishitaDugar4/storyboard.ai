"""fal image generation.

Schema verified against the live OpenAPI documents (2026-09-04):
    POST https://queue.fal.run/{model_id}   {prompt, image_size, num_images,
                                             seed, output_format}
    -> {request_id, status_url, response_url}
    GET  {status_url}    -> {"status": IN_QUEUE|IN_PROGRESS|COMPLETED}
    GET  {response_url}  -> {"images": [{url, width, height}], "seed": ...}

None of the flux or nano-banana endpoints accept a `negative_prompt`, so
exclusions are folded into the positive prompt -- the same accommodation the
video adapters make, driven by capability rather than by a branch on the
provider's name.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from ..ports import AIError, AIErrorKind, ImageResult, Usage

QUEUE_BASE = "https://queue.fal.run"
DEFAULT_MODEL = "fal-ai/flux/dev"

#: USD per generated image, from the fal model pages.
PRICING: dict[str, float] = {
    "fal-ai/flux/dev": 0.025,
    "fal-ai/flux-pro/v1.1": 0.04,
    "fal-ai/nano-banana": 0.039,
}

#: Endpoints that take a negative prompt. Empty today -- kept as data so adding
#: a model that does is a table edit, not a code change.
SUPPORTS_NEGATIVE: set[str] = set()


def _classify(status: int, body: str) -> AIError:
    if status in (401, 403):
        if "exhausted" in body.lower() or "balance" in body.lower():
            # Not an auth problem: a locked account reads as 403 but the fix is
            # a top-up, and calling it "unauthorized" sends people key-hunting.
            return AIError(AIErrorKind.QUOTA, "balance_exhausted", body[:300])
        return AIError(AIErrorKind.AUTH, "unauthorized", body[:300])
    if status == 429:
        return AIError(AIErrorKind.QUOTA, "rate_limited", body[:300])
    if status in (400, 422):
        return AIError(AIErrorKind.INVALID, "bad_request", body[:300])
    if status >= 500:
        return AIError(AIErrorKind.TRANSIENT, f"http_{status}", body[:300])
    return AIError(AIErrorKind.UNKNOWN, f"http_{status}", body[:300])


class FalImageAdapter:
    provider = "fal"

    @property
    def cost_per_image_cents(self) -> float | None:
        """None means "price unknown" -- never zero.

        Defaulting an unlisted model to free would quietly disable the budget
        gate, which is the one control standing between a fan-out and a
        surprise invoice.
        """
        usd = PRICING.get(self.model)
        return None if usd is None else usd * 100

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL,
                 *, timeout_s: float = 180.0, poll_s: float = 2.0) -> None:
        import httpx
        if not api_key:
            raise AIError(AIErrorKind.AUTH, "missing_key",
                          "FAL_KEY is not set. Create one at "
                          "https://fal.ai/dashboard/keys and put it in .env")
        if ":" not in api_key:
            # fal keys are "<key-id>:<key-secret>". Sending half of one earns a
            # 401 whose text blames the *application* -- "Cannot access
            # application fal-ai/flux" -- which reads like a permissions or
            # billing problem and sends you looking in the wrong place.
            raise AIError(
                AIErrorKind.AUTH, "malformed_key",
                f"FAL_KEY does not look like a fal key: expected "
                f"'<key-id>:<key-secret>', got {len(api_key)} characters with "
                f"no colon. Copy the whole key from "
                f"https://fal.ai/dashboard/keys")
        self.model = model
        self._poll_s = poll_s
        self._client = httpx.AsyncClient(
            timeout=timeout_s,
            headers={"Authorization": f"Key {api_key}",
                     "Content-Type": "application/json"})

    async def generate(self, *, positive: str, negative: str, size: str,
                       seed: int | None = None, n: int = 1
                       ) -> tuple[list[ImageResult], Usage]:
        width, height = (int(x) for x in size.lower().split("x"))
        payload: dict = {
            "prompt": positive if self.model in SUPPORTS_NEGATIVE
            else _fold_negative(positive, negative),
            "image_size": {"width": width, "height": height},
            "num_images": n,
            "output_format": "png",
        }
        if self.model in SUPPORTS_NEGATIVE and negative:
            payload["negative_prompt"] = negative
        if seed is not None:
            payload["seed"] = seed

        started = time.perf_counter()
        r = await self._client.post(f"{QUEUE_BASE}/{self.model}", json=payload)
        if r.status_code >= 400:
            raise _classify(r.status_code, r.text)
        sub = r.json()

        result = await self._await_result(sub)
        images: list[ImageResult] = []
        for entry in result.get("images", []) or []:
            data = await self._download(entry["url"])
            images.append(ImageResult(
                data=data, mime=entry.get("content_type", "image/png"),
                width=entry.get("width"), height=entry.get("height"),
                seed=result.get("seed")))
        if not images:
            raise AIError(AIErrorKind.UNKNOWN, "no_images", str(result)[:300])

        latency_ms = int((time.perf_counter() - started) * 1000)
        return images, Usage(model=self.model, latency_ms=latency_ms,
                             cost_cents=PRICING.get(self.model, 0.0) * 100 * len(images))

    async def _await_result(self, sub: dict) -> dict:
        status_url, response_url = sub["status_url"], sub["response_url"]
        delay = self._poll_s
        for _ in range(150):
            await asyncio.sleep(delay)
            s = await self._client.get(status_url)
            if s.status_code >= 400:
                raise _classify(s.status_code, s.text)
            state = s.json().get("status")
            if state == "COMPLETED":
                rr = await self._client.get(response_url)
                if rr.status_code >= 400:
                    raise _classify(rr.status_code, rr.text)
                return rr.json()
            if state not in ("IN_QUEUE", "IN_PROGRESS"):
                raise AIError(AIErrorKind.UNKNOWN, "unexpected_status", str(state))
            delay = min(delay * 1.2, 6.0)
        raise AIError(AIErrorKind.TRANSIENT, "timeout",
                      "image generation did not finish in time")

    async def _download(self, url: str) -> bytes:
        r = await self._client.get(url)
        if r.status_code >= 400:
            raise _classify(r.status_code, "image download failed")
        return r.content

    async def aclose(self) -> None:
        await self._client.aclose()


def _fold_negative(positive: str, negative: str) -> str:
    """Phrase exclusions positively for models with no negative-prompt input."""
    if not negative:
        return positive
    return (f"{positive} Clean, well-formed composition with no text, captions "
            f"or watermarks, and correct anatomy.")
