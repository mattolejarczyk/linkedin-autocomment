"""Tests for the on-topic relevance check in generate_authentic_linkedin_comments.

No network: the *provider* is replaced with a scripted fake, so the suite makes
zero live API calls (per the spec). Mocking at the provider boundary rather than
at a vendor SDK means these tests are provider-agnostic — they exercise the same
code path whether the profile is configured for OpenAI, Anthropic, or xAI.

Covers the relevance gate, the stay_on_post_topic flag, the prompt reframing,
and api_usage logging."""

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import providers
from linkedin_automation import comment_generator as gen


# ─── Scripted fake provider (distinguishes relevance vs generation calls) ─────

class FakeClient:
    """Stands in for a provider adapter. Keeps the counters the tests assert on."""

    name = "fake"

    def __init__(self, generation_outputs=None, relevance_outputs=None):
        self.generation_outputs = list(generation_outputs or [])
        self.relevance_outputs = list(relevance_outputs or [])
        self.generation_calls = 0
        self.relevance_calls = 0

    def complete(self, model, system, user, temperature=None,
                 max_tokens=None, json_object=False):
        if "Answer with exactly YES or NO" in user:
            self.relevance_calls += 1
            text = self.relevance_outputs.pop(0) if self.relevance_outputs else "YES"
        else:
            self.generation_calls += 1
            text = (self.generation_outputs.pop(0) if self.generation_outputs
                    else "default comment.")
        return providers.Completion(text=text.strip(), model=model, provider=self.name)


@pytest.fixture
def make_generator(monkeypatch, comments_dir):
    """Build a generator with the provider stubbed out (no network)."""
    monkeypatch.setattr(providers, "get_provider",
                        lambda name, api_key=None, base_url=None: FakeClient())

    def _make(config=None, client=None):
        monkeypatch.setattr(pm, "get_profile_config", lambda profile_name=None: config or {})
        g = gen.AuthenticLinkedInCommentGenerator("dummy_input.json", profile_name="t",
                                              platform=gen.platform_policy.LINKEDIN)
        g.provider = client if client is not None else FakeClient()
        return g
    return _make


POST_ECON = {
    "author_name": "Some Analyst",
    "text": "AI hiring is up while layoffs are blamed on AI. The real driver is "
            "capex versus headcount on the balance sheet.",
}


# ─── stay_on_post_topic flag ───────────────────────────────────────────────────

def test_flag_defaults_true_when_absent(make_generator):
    g = make_generator(config={})
    assert g.stay_on_post_topic is True


def test_flag_respects_explicit_false(make_generator):
    g = make_generator(config={"comment_generator": {"stay_on_post_topic": False}})
    assert g.stay_on_post_topic is False


# ─── check_relevance ───────────────────────────────────────────────────────────

def test_check_relevance_yes_and_no(make_generator):
    g = make_generator(client=FakeClient(relevance_outputs=["YES", "NO", "yes\n"]))
    assert g.check_relevance(POST_ECON, "on topic") is True
    assert g.check_relevance(POST_ECON, "off topic") is False
    assert g.check_relevance(POST_ECON, "case-insensitive") is True


def test_check_relevance_defaults_true_on_error(make_generator):
    class Boom:
        def complete(self, **k):
            raise RuntimeError("api down")
    g = make_generator(client=Boom())
    assert g.check_relevance(POST_ECON, "whatever") is True


def test_check_relevance_logs_to_api_usage_with_cheap_model(make_generator):
    """The relevance check logs the provider's cheap default, not the generation model.

    It also has to *follow* the provider: before Phase 8 this was pinned to
    gpt-4o-mini, so a user on Anthropic would still have been billed by OpenAI
    for every relevance check.
    """
    g = make_generator(client=FakeClient(relevance_outputs=["YES"]))
    logged = []
    g._log_api_usage = lambda endpoint, cost, model=None: logged.append((endpoint, model))
    g.check_relevance(POST_ECON, "comment")
    assert logged == [("chat.completions:relevance", g.relevance_model)]
    assert g.relevance_model == "gpt-4o-mini"      # default profile is OpenAI


def test_relevance_model_follows_the_configured_provider(make_generator):
    g = make_generator(config={"provider": {"name": "anthropic"}})
    assert g.relevance_model == providers.DEFAULT_MODELS["anthropic"]
    assert not g.relevance_model.startswith("gpt-")


# ─── Relevance gate inside generate_comment ───────────────────────────────────

def test_offtopic_comment_is_regenerated(make_generator, monkeypatch):
    # First generation is off-topic (VFX forced), relevance says NO; second is
    # on-topic, relevance says YES. The on-topic one must be returned.
    client = FakeClient(
        generation_outputs=[
            "Many production teams still rely heavily on traditional VFX skills.",
            "The capex versus headcount point is the one people skip.",
        ],
        relevance_outputs=["NO", "YES"],
    )
    g = make_generator(config={"comment_generator": {"stay_on_post_topic": True}}, client=client)
    # Isolate the relevance logic from authenticity heuristics.
    monkeypatch.setattr(g, "detect_ai_patterns", lambda c: (True, []))

    result = g.generate_comment(POST_ECON, {"post_category": "thought_piece"})
    assert result["comment"] == "The capex versus headcount point is the one people skip."
    assert client.generation_calls == 2
    assert client.relevance_calls == 2
    assert result["relevant"] is True


def test_flag_off_skips_relevance_call(make_generator, monkeypatch):
    client = FakeClient(generation_outputs=["Any authentic comment about the post."])
    g = make_generator(config={"comment_generator": {"stay_on_post_topic": False}}, client=client)
    monkeypatch.setattr(g, "detect_ai_patterns", lambda c: (True, []))

    result = g.generate_comment(POST_ECON, {"post_category": "thought_piece"})
    assert result is not None
    assert client.relevance_calls == 0          # no extra spend when flag is off
    assert client.generation_calls == 1


def test_relevant_comment_accepted_first_try(make_generator, monkeypatch):
    client = FakeClient(
        generation_outputs=["The capex versus headcount framing is the real story here."],
        relevance_outputs=["YES"],
    )
    g = make_generator(config={"comment_generator": {"stay_on_post_topic": True}}, client=client)
    monkeypatch.setattr(g, "detect_ai_patterns", lambda c: (True, []))

    result = g.generate_comment(POST_ECON, {"post_category": "thought_piece"})
    assert result["relevant"] is True
    assert client.generation_calls == 1
    assert client.relevance_calls == 1


# ─── Empty replies: never a draft, always evidence ────────────────────────────

def _evidence(g):
    import json
    import os
    path = os.path.join(g.output_dir, "reply_failures.jsonl")
    if not os.path.exists(path):
        return []
    return [json.loads(line) for line in open(path, encoding="utf-8")]


class EmptyThenClient(FakeClient):
    """Empty generation replies shaped like a thinking model that ran out of cap."""

    def complete(self, model, system, user, temperature=None,
                 max_tokens=None, json_object=False):
        completion = super().complete(model, system, user, temperature, max_tokens, json_object)
        if not completion.text:
            completion.finish_reason = "length"
            completion.reasoning_tokens = 100
        return completion


def test_all_empty_replies_give_no_draft_and_keep_evidence(make_generator, monkeypatch):
    """Three blanks used to be returned as a 0-word draft and queued."""
    client = EmptyThenClient(generation_outputs=["", "", ""])
    g = make_generator(client=client)
    monkeypatch.setattr(g, "detect_ai_patterns", lambda c: (True, []))

    assert g.generate_comment(POST_ECON, {"post_category": "thought_piece"}) is None
    assert client.relevance_calls == 0              # nothing to judge, nothing paid
    evidence = _evidence(g)
    assert [e["kind"] for e in evidence] == ["generate"] * 3
    assert [e["attempt"] for e in evidence] == [1, 2, 3]
    assert evidence[0]["finish_reason"] == "length"
    assert evidence[0]["reasoning_tokens"] == 100


def test_a_real_reply_after_an_empty_one_is_used_untouched(make_generator, monkeypatch):
    client = EmptyThenClient(generation_outputs=["", "The capex point is the real story."],
                             relevance_outputs=["YES"])
    g = make_generator(client=client)
    monkeypatch.setattr(g, "detect_ai_patterns", lambda c: (True, []))

    result = g.generate_comment(POST_ECON, {"post_category": "thought_piece"})
    assert result["comment"] == "The capex point is the real story."
    assert result["attempt"] == 2
    assert len(_evidence(g)) == 1


def test_an_empty_relevance_answer_is_not_a_no(make_generator):
    """An unjudged comment must not be thrown away and paid for again."""
    client = FakeClient(relevance_outputs=[""])
    g = make_generator(client=client)
    assert g.check_relevance(POST_ECON, "A comment.") is True
    assert [e["kind"] for e in _evidence(g)] == ["relevance"]


def test_an_unparseable_evaluation_keeps_the_reply(make_generator):
    client = FakeClient(generation_outputs=["not json at all"])
    g = make_generator(client=client)
    result = g.evaluate_post_quality({**POST_ECON, "url": "https://example.com/p/1"})
    assert result["verdict"] == "engage"
    [evidence] = _evidence(g)
    assert evidence["kind"] == "evaluate"
    assert evidence["text_start"] == "not json at all"
    assert evidence["post_url"] == "https://example.com/p/1"


def test_a_fenced_evaluation_is_read_not_defaulted(make_generator):
    """The exact shape saved from two failed evaluations on 2026-10-31."""
    client = FakeClient(generation_outputs=[
        '```json\n{"verdict": "skip", "post_category": "fluff", "reason": "r"}\n```'])
    g = make_generator(client=client)
    assert g.evaluate_post_quality(POST_ECON)["verdict"] == "skip"
    assert _evidence(g) == []


def test_an_evaluation_api_error_writes_no_reply_evidence(make_generator):
    """No reply came back, so there is nothing to keep; the log says why."""
    class Boom:
        def complete(self, **k):
            raise RuntimeError("api down")
    g = make_generator(client=Boom())
    assert g.evaluate_post_quality(POST_ECON)["verdict"] == "engage"
    assert _evidence(g) == []


# ─── Profile voice controls: disabled_styles and banned_phrases ───────────────

STANCE_STYLES = ["respectful_different", "empathetic_relate", "practical_helper", "direct_challenge"]


def test_disabled_styles_are_never_picked(make_generator):
    g = make_generator(config={"comment_generator": {"disabled_styles": STANCE_STYLES}})
    for category in ["personal_story", "question", "thought_piece", "technical_discussion"]:
        for approach in ["relate_experience", "respectful_disagree", "add_perspective"]:
            for _ in range(20):
                style, _ = g.get_matched_comment_style(
                    {"post_category": category, "engagement_approach": approach})
                assert style["name"] not in STANCE_STYLES


def test_disabling_every_style_is_ignored_not_obeyed(make_generator):
    every = ["genuine_curious", "thoughtful_addition", "brief_supportive", "direct_reaction",
             "minimal_agreement", "specific_question"] + STANCE_STYLES
    g = make_generator(config={"comment_generator": {"disabled_styles": every}})
    style, _ = g.get_matched_comment_style({"post_category": "thought_piece"})
    assert style["name"]


def _banned(g, text):
    return [i for i in g.detect_ai_patterns(text)[1] if i.startswith("profile_banned_phrase")]


def test_banned_phrases_match_whole_words_in_any_case(make_generator):
    g = make_generator(config={"comment_generator": {"banned_phrases": ["I", "my worry"]}})
    assert _banned(g, "I'd want an approval gate there.")
    assert _banned(g, "Replies reach prospects, so i’m careful there.")   # curly apostrophe
    assert _banned(g, "My worry is the consent side.")
    assert _banned(g, "AI tools are useful for research.") == []           # not inside a word
    assert _banned(g, "Which one did you find hardest to get dependable output from?") == []


def test_no_banned_phrases_configured_changes_nothing(make_generator):
    g = make_generator(config={})
    assert _banned(g, "I'd want an approval gate there.") == []


def test_a_draft_with_a_banned_phrase_is_never_returned(make_generator):
    """Not even as the fallback best attempt, which used to ship imperfect drafts."""
    client = FakeClient(generation_outputs=["I'd want one.", "I would too.", "I think so."])
    g = make_generator(config={"comment_generator": {"banned_phrases": ["I"]}}, client=client)

    assert g.generate_comment(POST_ECON, {"post_category": "thought_piece"}) is None
    assert client.relevance_calls == 0
    evidence = _evidence(g)
    assert [e["kind"] for e in evidence] == ["banned_phrase"] * 3
    assert evidence[0]["text_start"] == "I'd want one."
    assert "profile_banned_phrase: 'I'" in evidence[0]["detail"]


def test_a_clean_draft_after_a_banned_one_is_used(make_generator, monkeypatch):
    client = FakeClient(generation_outputs=["I'd want one.", "Which part did you find hardest?"],
                        relevance_outputs=["YES"])
    g = make_generator(config={"comment_generator": {"banned_phrases": ["I"]}}, client=client)
    result = g.generate_comment(POST_ECON, {"post_category": "thought_piece"})
    assert result["comment"] == "Which part did you find hardest?"
    assert result["attempt"] == 2


def test_banned_phrases_are_named_in_the_prompt(make_generator):
    g = make_generator(config={"comment_generator": {"banned_phrases": ["I", "my worry"]}})
    style = {"name": "add_insight", "instruction": "Add a point", "example": "An example."}
    prompt = g.create_authentic_comment_prompt(POST_ECON, style, "build on idea")
    assert 'a draft containing any of them is rejected: "I", "my worry"' in prompt


def test_default_config_ships_both_lists_empty():
    import json
    cg = json.load(open("default_profile_config.json", encoding="utf-8"))["comment_generator"]
    assert cg["disabled_styles"] == [] and cg["banned_phrases"] == []


# ─── Prompt reframing ──────────────────────────────────────────────────────────

def test_prompt_includes_on_topic_rules_and_examples(make_generator):
    g = make_generator(config={"comment_generator": {
        "persona": "AI Tech Lead in VFX", "topics_of_expertise": ["vfx pipelines"]}})
    style = {"name": "add_insight", "instruction": "Add a point", "example": "An example."}
    prompt = g.create_authentic_comment_prompt(POST_ECON, style, "build on idea")
    assert "STAY ON THE POST'S TOPIC" in prompt
    assert "do NOT force your industry or expertise" in prompt
    assert "BAD" in prompt and "GOOD" in prompt          # failure-mode examples
    # Persona reframed as voice/judgment, expertise surfaced only when relevant.
    assert "shapes your VOICE and JUDGMENT" in prompt
    assert "never force it in" in prompt


def test_on_topic_rules_constant_has_vfx_failure_example():
    # The block moved from gen.ON_TOPIC_RULES to platform_policy.on_topic_rules()
    # so its nouns follow the platform. Same content, same assertions.
    from linkedin_automation import platform_policy as pp
    rules = pp.on_topic_rules(pp.policy_for(pp.LINKEDIN).register)
    assert "VFX" in rules
    assert "capex" in rules.lower()
    # And it still renders for a platform with different nouns.
    x_rules = pp.on_topic_rules(pp.policy_for(pp.X).register)
    assert "VFX" in x_rules
    assert "STAY ON THE TWEET'S TOPIC" in x_rules


def test_default_config_exposes_flag():
    import json
    cfg = json.load(open("default_profile_config.json", encoding="utf-8"))
    assert cfg["comment_generator"]["stay_on_post_topic"] is True
