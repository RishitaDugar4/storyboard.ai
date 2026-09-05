"""M2 tests: the AI contracts, the services, and the adapters.

No network. The real Anthropic adapter is exercised for its pure parts (cost
maths, error classification); its request path is covered by the fake, which
honours the same contract.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import anthropic
import pytest
from pydantic import ValidationError

from app.ai.adapters.anthropic_text import PRICING, _classify, _cost_cents
from app.ai.adapters import gemini_text as gem
from app.ai.structured import format_errors as _format_errors
from app.ai.adapters.fakes import FakeTextAdapter
from app.ai.pacing import PAD_S, WORDS_PER_SECOND, required_seconds, word_budget
from app.ai.ports import AIError, AIErrorKind
from app.schemas.ai import StoryAnalysis, Storyboard
from app.services.story_service import MAX_STORY_WORDS, analyze_story
from app.services.storyboard_service import (SCENE_MAX, SCENE_MIN,
                                             StoryboardRequest,
                                             build_cache_prefix,
                                             build_user_prompt)

FIXTURES = Path(__file__).parent / "fixtures" / "ai"


@pytest.fixture(scope="module")
def storyboard_data() -> dict:
    return json.loads((FIXTURES / "storyboard.json").read_text())


@pytest.fixture(scope="module")
def analysis() -> StoryAnalysis:
    return StoryAnalysis.model_validate_json(
        (FIXTURES / "story_analysis.json").read_text())


def _mutate(data: dict, fn) -> dict:
    out = copy.deepcopy(data)
    fn(out)
    return out


# ---- pacing ---------------------------------------------------------------
def test_word_budget_matches_the_stated_model():
    assert word_budget(6.0) == int((6.0 - PAD_S) * WORDS_PER_SECOND) == 12
    assert word_budget(4.0) == 7
    assert word_budget(1.0) == 0          # shorter than the padding itself


def test_required_seconds_is_zero_for_silence():
    assert required_seconds(0) == 0.0
    assert required_seconds(10) == pytest.approx(10 / WORDS_PER_SECOND + PAD_S)


# ---- golden fixtures ------------------------------------------------------
def test_analysis_fixture_is_valid(analysis):
    assert analysis.title and len(analysis.beats) >= 3


def test_storyboard_fixture_is_valid(storyboard_data):
    sb = Storyboard.model_validate(storyboard_data)
    assert SCENE_MIN <= len(sb.scenes) <= SCENE_MAX
    assert sb.shot_count >= len(sb.scenes)


def test_storyboard_fixture_has_no_narration_overflow(storyboard_data):
    sb = Storyboard.model_validate(storyboard_data)
    for sc in sb.scenes:
        for sh in sc.shots:
            used = sum(n.word_count for n in sc.narration
                       if n.shot_local_index in (None, sh.local_index))
            assert used <= word_budget(sh.target_duration_s), \
                f"scene {sc.local_index} shot {sh.local_index}"


def test_fixture_round_trips(storyboard_data):
    sb = Storyboard.model_validate(storyboard_data)
    assert Storyboard.model_validate_json(sb.model_dump_json()) == sb


# ---- storyboard validators ------------------------------------------------
@pytest.mark.parametrize("mutate,expected", [
    (lambda d: d["scenes"][0]["shots"][0]["subject_slugs"].append("ghost"),
     "not in characters"),
    (lambda d: d["scenes"][0].update(location_slug="atlantis"),
     "not in locations"),
    (lambda d: d["scenes"][0]["narration"][0].update(speaker="nobody"),
     "neither 'narrator'"),
    (lambda d: d["scenes"][0]["narration"][0].update(shot_local_index=9),
     "does not exist in this scene"),
    (lambda d: d["scenes"][0]["present_slugs"].append("ghost"),
     "present_slugs"),
    (lambda d: d["characters"].append(dict(d["characters"][0])),
     "duplicate character slug"),
    (lambda d: d["scenes"].reverse(), "unique and ordered"),
])
def test_integrity_violations_are_rejected(storyboard_data, mutate, expected):
    with pytest.raises(ValidationError, match=expected):
        Storyboard.model_validate(_mutate(storyboard_data, mutate))


def test_narration_overflow_is_rejected_with_a_usable_message(storyboard_data):
    broken = _mutate(storyboard_data, lambda d: d["scenes"][0]["narration"][0]
                     .update(text=" ".join(["word"] * 40)))
    with pytest.raises(ValidationError) as exc:
        Storyboard.model_validate(broken)
    msg = str(exc.value)
    assert "40 words" in msg and "budget" in msg
    assert "raise target_duration_s" in msg     # tells the model how to fix it


def test_shot_subject_cap_is_three(storyboard_data):
    broken = _mutate(storyboard_data, lambda d: d["scenes"][0]["shots"][0]
                     .update(subject_slugs=["a", "b", "c", "d"]))
    with pytest.raises(ValidationError):
        Storyboard.model_validate(broken)


def test_target_duration_is_not_a_provider_grid(storyboard_data):
    """7.3s is illegal on every provider we know of and must still validate:
    the storyboard carries intent, not a vendor's clip lengths."""
    ok = _mutate(storyboard_data,
                 lambda d: d["scenes"][0]["shots"][0].update(target_duration_s=7.3))
    assert Storyboard.model_validate(ok).scenes[0].shots[0].target_duration_s == 7.3


# ---- analysis validators --------------------------------------------------
def test_analysis_requires_a_protagonist(analysis):
    data = analysis.model_dump()
    for c in data["characters"]:
        c["role"] = "supporting"
    with pytest.raises(ValidationError, match="no protagonist"):
        StoryAnalysis.model_validate(data)


def test_analysis_rejects_unordered_beats(analysis):
    data = analysis.model_dump()
    data["beats"].reverse()
    with pytest.raises(ValidationError, match="narrative order"):
        StoryAnalysis.model_validate(data)


# ---- services -------------------------------------------------------------
@pytest.mark.parametrize("seconds", [15, 30, 60, 90, 120, 300, 900])
def test_scene_suggestion_stays_inside_schema_bounds(seconds, analysis):
    lo, hi = StoryboardRequest(story_text="x", analysis=analysis,
                               target_length_s=seconds).suggested_scene_count
    assert SCENE_MIN <= lo <= hi <= SCENE_MAX


def test_scene_bounds_match_the_schema():
    """The service must not suggest a count the schema would reject."""
    props = Storyboard.model_json_schema()["properties"]["scenes"]
    assert props["minItems"] == SCENE_MIN and props["maxItems"] == SCENE_MAX


def test_user_prompt_states_the_word_budgets(analysis):
    prompt = build_user_prompt(StoryboardRequest(story_text="x", analysis=analysis))
    assert f"at most {word_budget(6.0)} words" in prompt
    assert "16:9" in prompt


def test_cache_prefix_carries_story_and_analysis(analysis):
    prefix = build_cache_prefix(StoryboardRequest(story_text="Once upon a time.",
                                                  analysis=analysis))
    assert "<story>" in prefix and "Once upon a time." in prefix
    assert "<analysis>" in prefix and analysis.title in prefix


async def test_empty_story_is_rejected():
    with pytest.raises(ValueError, match="empty"):
        await analyze_story("   ", FakeTextAdapter())


async def test_overlong_story_is_rejected():
    with pytest.raises(ValueError, match="limit"):
        await analyze_story("word " * (MAX_STORY_WORDS + 1), FakeTextAdapter())


# ---- fake adapter ---------------------------------------------------------
async def test_fake_returns_valid_documents():
    res = await FakeTextAdapter().generate_structured(
        schema=Storyboard, system="", user="")
    assert isinstance(res.value, Storyboard) and not res.repaired
    assert res.usage.cost_cents == 0.0


async def test_fake_repair_reports_real_validator_errors():
    res = await FakeTextAdapter(repair_once=True).generate_structured(
        schema=Storyboard, system="", user="")
    assert res.repaired and res.repair_errors
    assert "not in characters" in res.repair_errors[0]
    assert isinstance(res.value, Storyboard)      # still returns a valid doc


async def test_fake_can_simulate_a_refusal():
    with pytest.raises(AIError) as exc:
        await FakeTextAdapter(refuse=True).generate_structured(
            schema=Storyboard, system="", user="")
    assert exc.value.kind is AIErrorKind.REFUSAL
    assert not exc.value.retryable


# ---- anthropic adapter (pure parts) --------------------------------------
def test_cost_accounting_matches_list_pricing():
    class U:
        input_tokens = output_tokens = 1_000_000
        cache_read_input_tokens = cache_creation_input_tokens = 0
    inp, out = PRICING["claude-opus-5"]
    assert _cost_cents("claude-opus-5", U()) == pytest.approx((inp + out) * 100)


def test_cache_reads_are_counted_not_dropped():
    class U:
        input_tokens = output_tokens = 0
        cache_read_input_tokens, cache_creation_input_tokens = 1_000_000, 0
    assert _cost_cents("claude-opus-5", U()) > 0


def test_unknown_model_costs_zero_rather_than_crashing():
    class U:
        input_tokens = output_tokens = 1000
        cache_read_input_tokens = cache_creation_input_tokens = 0
    assert _cost_cents("some-future-model", U()) == 0.0


def test_error_classification():
    assert _classify(RuntimeError("boom")).kind is AIErrorKind.UNKNOWN


def test_quota_errors_are_retryable_but_refusals_are_not():
    assert AIError(AIErrorKind.QUOTA, "x").retryable
    assert not AIError(AIErrorKind.REFUSAL, "x").retryable
    assert not AIError(AIErrorKind.INVALID, "x").retryable


def test_validation_errors_format_for_the_model(storyboard_data):
    broken = _mutate(storyboard_data, lambda d: d["scenes"][0]["shots"][0]
                     ["subject_slugs"].append("ghost"))
    try:
        Storyboard.model_validate(broken)
    except ValidationError as exc:
        text = _format_errors(exc)
    assert text.startswith("- ") and "not in characters" in text
    assert "Value error," not in text     # pydantic noise stripped


# ---- gemini adapter (pure parts) -----------------------------------------
def test_gemini_cost_counts_thinking_tokens_as_output():
    """Thinking tokens bill as output; omitting them under-reports spend."""
    class U:
        prompt_token_count, candidates_token_count = 1000, 1000
        cached_content_token_count, thoughts_token_count = 0, 1000
    class V(U):
        thoughts_token_count = 0
    assert gem._cost_cents("gemini-3.1-pro-preview", U()) > \
           gem._cost_cents("gemini-3.1-pro-preview", V())


def test_gemini_pricing_table_matches_published_rates():
    assert gem.PRICING["gemini-3.1-pro-preview"] == (2.00, 12.00)
    assert gem.PRICING["gemini-3.8-flash"] == (0.75, 3.75)


def test_gemini_unknown_model_costs_zero():
    class U:
        prompt_token_count = candidates_token_count = 1000
        cached_content_token_count = thoughts_token_count = 0
    assert gem._cost_cents("gemini-99", U()) == 0.0


@pytest.mark.parametrize("reason,kind", [
    ("SAFETY", AIErrorKind.REFUSAL),
    ("PROHIBITED_CONTENT", AIErrorKind.REFUSAL),
    ("MAX_TOKENS", AIErrorKind.INVALID),
])
def test_gemini_finish_reasons_are_classified(reason, kind):
    class C:
        finish_reason = reason
    class R:
        prompt_feedback = None
        candidates = [C()]
    with pytest.raises(AIError) as exc:
        gem._check_refusal(R())
    assert exc.value.kind is kind


def test_gemini_normal_finish_is_not_an_error():
    class C:
        finish_reason = "STOP"
    class R:
        prompt_feedback = None
        candidates = [C()]
    gem._check_refusal(R())        # must not raise


def test_gemini_blocked_prompt_is_a_refusal():
    class F:
        block_reason = "SAFETY"
    class R:
        prompt_feedback = F()
        candidates = []
    with pytest.raises(AIError) as exc:
        gem._check_refusal(R())
    assert exc.value.kind is AIErrorKind.REFUSAL


# ---- shared repair loop ---------------------------------------------------
async def test_repair_loop_is_shared_by_both_adapters():
    """One implementation, so a fix in the loop cannot apply to only one
    provider."""
    from app.ai.adapters import anthropic_text, gemini_text
    src_a = Path(anthropic_text.__file__).read_text()
    src_g = Path(gemini_text.__file__).read_text()
    assert "generate_with_repair" in src_a and "generate_with_repair" in src_g
    assert "REPAIR_TEMPLATE" not in src_a and "REPAIR_TEMPLATE" not in src_g


async def test_repair_loop_gives_up_after_one_retry():
    from app.ai.ports import Usage
    from app.ai.structured import RawCall, generate_with_repair
    calls = []

    async def always_broken(repair):
        calls.append(repair)
        data = json.loads((FIXTURES / "storyboard.json").read_text())
        data["scenes"][0]["shots"][0]["subject_slugs"] = ["ghost"]
        return RawCall(parsed=data, raw_text="{}", usage=Usage(model="t"))

    with pytest.raises(AIError) as exc:
        await generate_with_repair(Storyboard, always_broken)
    assert exc.value.kind is AIErrorKind.INVALID
    assert len(calls) == 2 and calls[0] is None
    assert "not in characters" in calls[1].instruction
    # The model must be shown what it wrote, or it repeats the mistake.
    assert calls[1].prior_output is not None


async def test_repair_loop_succeeds_on_the_second_try():
    from app.ai.ports import Usage
    from app.ai.structured import RawCall, generate_with_repair
    state = {"n": 0}

    async def broken_then_fixed(repair):
        state["n"] += 1
        data = json.loads((FIXTURES / "storyboard.json").read_text())
        if state["n"] == 1:
            data["scenes"][0]["shots"][0]["subject_slugs"] = ["ghost"]
        return RawCall(parsed=data, raw_text="{}", usage=Usage(model="t"))

    res = await generate_with_repair(Storyboard, broken_then_fixed)
    assert res.repaired and state["n"] == 2
    assert isinstance(res.value, Storyboard)


# ---- gemini json extraction ----------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', {"a": 1}),
    ('{"a": 1}\n\nHope this helps!', {"a": 1}),          # trailing commentary
    ('{"a": 1}\n{"b": 2}', {"a": 1}),                     # a second document
    ('```json\n{"a": 1}\n```', {"a": 1}),                 # markdown fence
    ('Here you go:\n{"a": 1}', {"a": 1}),                 # leading prose
    ('```json\n{"a": 1}\n```\nDone.', {"a": 1}),
    ('[1, 2]', [1, 2]),
])
def test_extract_json_survives_model_chatter(raw, expected):
    """Without server-side schema enforcement the model wraps, prefixes and
    appends; strict json.loads rejects all of it."""
    assert gem.extract_json(raw) == expected


def test_extract_json_still_fails_on_actual_garbage():
    with pytest.raises(json.JSONDecodeError):
        gem.extract_json("there is no document here")


# ---- rate limiting --------------------------------------------------------
async def test_limiter_admits_up_to_the_limit_without_waiting():
    from app.ai.ratelimit import RateLimiter
    lim = RateLimiter("test:burst", limit=3, window_s=60, redis_url=None)
    for _ in range(3):
        assert await lim.acquire() == pytest.approx(0, abs=0.05)


async def test_limiter_makes_the_next_caller_wait_for_the_window():
    """The eleventh call is the one that earned the 429; here it waits."""
    from app.ai.ratelimit import RateLimiter
    lim = RateLimiter("test:wait", limit=2, window_s=0.4, redis_url=None)
    await lim.acquire()
    await lim.acquire()
    waited = await lim.acquire()
    assert waited > 0.2, "third call should have been held for the window"


async def test_limiter_slides_rather_than_resetting_on_a_boundary():
    """A fixed bucket would let 2 through at the end of one window and 2 more
    at the start of the next -- 4 inside one real window, which is the bug."""
    from app.ai.ratelimit import RateLimiter
    import asyncio
    lim = RateLimiter("test:slide", limit=2, window_s=0.5, redis_url=None)
    await lim.acquire()
    await asyncio.sleep(0.3)
    await lim.acquire()
    assert await lim.acquire() > 0.1


async def test_a_wait_longer_than_the_cap_surfaces_as_our_own_quota_error():
    from app.ai.adapters.fakes import FakeSpeechAdapter
    from app.ai.ratelimit import RateLimitedSpeech, RateLimiter
    lim = RateLimiter("test:cap", limit=1, window_s=60,
                      max_wait_s=0.05, redis_url=None)
    port = RateLimitedSpeech(FakeSpeechAdapter(), lim)
    await port.synthesize(text="one", voice="Kore")
    with pytest.raises(AIError) as err:
        await port.synthesize(text="two", voice="Kore")
    assert err.value.kind is AIErrorKind.QUOTA
    # Named for us, not for the provider: nobody should go quota-hunting in
    # Google's console for a limit this process imposed.
    assert err.value.code == "local_rate_limit"
    assert err.value.retryable


async def test_the_decorator_is_transparent():
    from app.ai.adapters.fakes import FakeSpeechAdapter
    from app.ai.ratelimit import RateLimitedSpeech, RateLimiter
    inner = FakeSpeechAdapter()
    port = RateLimitedSpeech(inner, RateLimiter("test:pass", limit=5,
                                                redis_url=None))
    assert (port.model, port.provider) == (inner.model, inner.provider)
    assert port.voices() == inner.voices()
    speech, usage = await port.synthesize(text="hello", voice="Kore",
                                          style="warmly")
    expected, _ = await inner.synthesize(text="hello", voice="Kore",
                                         style="warmly")
    assert speech.duration_ms == expected.duration_ms


def test_pacing_is_configurable_and_can_be_switched_off(monkeypatch):
    from app.ai import registry
    from app.ai.adapters.fakes import FakeSpeechAdapter
    from app.ai.ratelimit import RateLimitedSpeech
    port = FakeSpeechAdapter()
    assert registry._paced(port, "speech", "0", 8) is port
    paced = registry._paced(port, "speech", None, 8)
    assert isinstance(paced, RateLimitedSpeech)
    assert paced._limiter.limit == 8
    assert registry._paced(port, "speech", "3", 8)._limiter.limit == 3


def test_the_pacing_key_separates_models():
    """Quotas are per model; one counter would throttle text on speech."""
    from app.ai import registry
    from app.ai.adapters.fakes import FakeSpeechAdapter

    class Other(FakeSpeechAdapter):
        model = "gemini-2.5-pro-preview-tts"

    a = registry._paced(FakeSpeechAdapter(), "speech", "8", 8)
    b = registry._paced(Other(), "speech", "8", 8)
    assert a._limiter.key != b._limiter.key
