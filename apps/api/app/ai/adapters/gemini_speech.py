"""Gemini text-to-speech.

Verified live against google-genai 2.22.0 (2026-09-04):
    client.aio.models.generate_content(
        model="gemini-2.5-flash-preview-tts", contents=text,
        config=GenerateContentConfig(response_modalities=["AUDIO"],
                                     speech_config=SpeechConfig(...)))
    -> parts[0].inline_data: mime "audio/L16;codec=pcm;rate=24000", raw PCM

Uses the typed `models.generate_content` path rather than the newer
`interactions.create` the docs show, for the same reason as the text adapter:
that surface takes an untyped passthrough, this one is introspectable.
"""
from __future__ import annotations

import time
from typing import Any

from google import genai
from google.genai import types

from ..audio import parse_pcm_mime, pcm_duration_ms, pcm_to_wav
from ..ports import AIError, AIErrorKind, SpeechResult, Usage

DEFAULT_MODEL = "gemini-2.5-flash-preview-tts"
DEFAULT_VOICE = "Kore"

#: USD per million tokens (Gemini API pricing, 2026-09-04). Output dominates:
#: a narration line is ~80 output tokens, so roughly 0.08 cents.
PRICING: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash-preview-tts": (0.50, 10.00),
    "gemini-2.5-pro-preview-tts": (1.00, 20.00),
    "gemini-3.1-flash-tts-preview": (1.00, 20.00),
}

#: Prebuilt voices. Kept as data so voice assignment is a menu, not a guess.
VOICES = [
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
    "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
    "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
    "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
]


def _classify(exc: Exception) -> AIError:
    text = str(exc)[:300]
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if status in (401, 403) or "API key" in text:
        return AIError(AIErrorKind.AUTH, "unauthorized", text)
    if status == 429 or "RESOURCE_EXHAUSTED" in text:
        return AIError(AIErrorKind.QUOTA, "rate_limited", text)
    if isinstance(status, int) and status >= 500:
        return AIError(AIErrorKind.TRANSIENT, f"http_{status}", text)
    if status == 400 or "INVALID_ARGUMENT" in text:
        return AIError(AIErrorKind.INVALID, "bad_request", text)
    return AIError(AIErrorKind.UNKNOWN, type(exc).__name__, text)


def _cost_cents(model: str, usage: Any) -> float:
    inp, out = PRICING.get(model, (0.0, 0.0))
    p = getattr(usage, "prompt_token_count", 0) or 0
    c = getattr(usage, "candidates_token_count", 0) or 0
    return (p / 1e6) * inp * 100 + (c / 1e6) * out * 100


class GeminiSpeechAdapter:
    provider = "gemini"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL,
                 *, timeout_s: float = 120.0) -> None:
        if not api_key:
            raise AIError(AIErrorKind.AUTH, "missing_key",
                          "GEMINI_API_KEY is not set")
        self.model = model
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))

    def voices(self) -> list[str]:
        return list(VOICES)

    async def synthesize(self, *, text: str, voice: str = DEFAULT_VOICE,
                         style: str | None = None
                         ) -> tuple[SpeechResult, Usage]:
        if voice not in VOICES:
            raise AIError(AIErrorKind.INVALID, "unknown_voice",
                          f"{voice!r} is not a prebuilt voice")
        # Delivery is steered in the prompt: the API has no separate style
        # parameter, and the model follows a plain instruction well.
        prompt = f"Say this {style}: {text}" if style else text

        started = time.perf_counter()
        try:
            resp = await self._client.aio.models.generate_content(
                model=self.model, contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=voice)))))
        except Exception as exc:
            raise _classify(exc) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        blob = _inline_audio(resp)
        rate, bits = parse_pcm_mime(blob.mime_type or "")
        pcm = blob.data
        # Headerless PCM: wrap it so anything downstream can open the file, and
        # take the duration from the byte count -- exact, not measured.
        wav = pcm_to_wav(pcm, sample_rate=rate, bits=bits)
        return (
            SpeechResult(data=wav, mime="audio/wav",
                         duration_ms=pcm_duration_ms(len(pcm), rate, bits),
                         sample_rate=rate, voice=voice),
            Usage(model=self.model, latency_ms=latency_ms,
                  input_tokens=getattr(resp.usage_metadata, "prompt_token_count", 0) or 0,
                  output_tokens=getattr(resp.usage_metadata, "candidates_token_count", 0) or 0,
                  cost_cents=_cost_cents(self.model, resp.usage_metadata)),
        )


def _inline_audio(resp: Any):
    for cand in getattr(resp, "candidates", None) or []:
        for part in getattr(cand.content, "parts", None) or []:
            if getattr(part, "inline_data", None) and part.inline_data.data:
                return part.inline_data
    raise AIError(AIErrorKind.UNKNOWN, "no_audio",
                  "the response contained no audio part")
