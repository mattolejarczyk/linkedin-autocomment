"""Tests for ContentAnalyzer scoring/keyword breadth (pipeline-fixes / BUG 1).

Pure text-in -> classification-out; no browser. Locks in the broadened keywords
and the relaxed is_ai rule (any keyword match counts)."""

import pytest

from linkedin_automation import post_finder as finder

A = finder.ContentAnalyzer()


def test_ai_agent_post_qualifies():
    text = ("We're seeing huge gains from AI agents and agentic workflows. Our "
            "GPT-based automation now handles most support tickets end to end.")
    is_ai, score, kws, quality, ptype = A.analyze(text)
    assert is_ai is True
    assert score > 0
    assert quality != finder.PostQuality.SKIP


@pytest.mark.parametrize("kw", [
    "automation", "gpt", "agentic", "training data", "fine-tune",
    "rag", "machine learning", "rlhf", "chatbot",
])
def test_broadened_keywords_match(kw):
    text = f"A practical look at {kw} in real production systems this year and beyond."
    is_ai, score, kws, quality, ptype = A.analyze(text)
    assert is_ai is True, f"{kw!r} should mark the post AI-related"
    assert score > 0


def test_single_tier2_keyword_now_counts():
    # Previously a lone tier2 keyword -> is_ai False (dropped). Now it counts.
    text = "Our team relies heavily on machine learning to forecast demand each week."
    is_ai, score, kws, quality, ptype = A.analyze(text)
    assert is_ai is True


def test_recruiting_post_not_ai():
    text = ("We are hiring a senior backend developer. Competitive salary and "
            "benefits. Apply today through our careers page.")
    is_ai, score, kws, quality, ptype = A.analyze(text)
    assert is_ai is False
    assert quality == finder.PostQuality.SKIP


def test_short_text_skipped():
    is_ai, score, kws, quality, ptype = A.analyze("AI")
    assert quality == finder.PostQuality.SKIP
    assert score == 0


def test_non_ai_post_skipped():
    text = "Had a wonderful weekend hiking with the family in the mountains. So refreshing!"
    is_ai, score, kws, quality, ptype = A.analyze(text)
    assert is_ai is False


# --- Whole-word matching -----------------------------------------------------
# Indicators and keywords used to be tested as bare substrings, which produced
# the false matches below in real scans. The text here is made up; only the
# pattern is real.

def test_newsletter_subscribers_is_one_spam_signal_not_two():
    # "subscribe" fired inside "subscribers", so one word counted twice -> spam.
    text = ("Our newsletter subscribers keep asking how we evaluate retrieval "
            "quality. What do you think matters most?")
    assert A.classify_post_type(text) != "spam"


def test_intern_and_placement_inside_longer_words_are_not_career():
    # "intern" fired inside "internal"/"international" and "placement" inside
    # "replacement", which pushed a business post to "career".
    text = ("Our internal review of the international rollout found the "
            "replacement system halved support costs in one quarter.")
    assert A.classify_post_type(text) != "career"


def test_rag_does_not_match_inside_average():
    text = ("The average team spends far too long in status meetings each week, "
            "according to a survey of managers we ran this spring.")
    is_ai, score, kws, quality, ptype = A.analyze(text)
    assert "rag" not in kws
    assert is_ai is False


def test_ai_does_not_match_inside_said_or_email():
    analyzer = finder.ContentAnalyzer(keywords_tier1=["ai"])
    text = ("The client said the email arrived late on Tuesday afternoon, so the "
            "planning meeting moved to Thursday morning.")
    is_ai, score, kws, quality, ptype = analyzer.analyze(text)
    assert is_ai is False
    assert kws == []


def test_ai_still_matches_as_its_own_word():
    analyzer = finder.ContentAnalyzer(keywords_tier1=["ai"])
    text = "AI-assisted drafting has changed how our support team answers email."
    is_ai, score, kws, quality, ptype = analyzer.analyze(text)
    assert is_ai is True
    assert kws == ["ai"]


def test_genuine_career_post_still_caught():
    text = ("Thrilled to share that I have joined a great team as a summer "
            "intern! First day is Monday and I can't wait.")
    assert A.classify_post_type(text) == "career"
    is_ai, score, kws, quality, ptype = A.analyze(text)
    assert quality == finder.PostQuality.SKIP


def test_genuine_spam_post_still_caught():
    text = ("Download now: my free course on prompt engineering. Sign up today "
            "and subscribe so you never miss a lesson.")
    assert A.classify_post_type(text) == "spam"
    is_ai, score, kws, quality, ptype = A.analyze(text)
    assert quality == finder.PostQuality.SKIP


@pytest.mark.parametrize("text, phrase", [
    ("Comparing LLMs on long documents this week.", "llm"),          # plural s
    ("We now welcome three interns every summer.", "intern"),
    ("Counting tokens before every call saves money.", "token"),
    ("A Large  Language\nModel can still be wrong.", "large language model"),
    ("Is A.I. overhyped right now?", "a.i."),                        # punctuation ends
    ("Back in the job market #OpenToWork today.", "#opentowork"),
    ("Claude's answer was the clearest.", "claude"),                 # possessive
])
def test_whole_word_match_allows_plural_and_spacing(text, phrase):
    assert finder._has_phrase(text.lower(), phrase)


@pytest.mark.parametrize("text, phrase", [
    ("Most newsletter subscribers skim.", "subscribe"),
    ("An internal memo went out today.", "intern"),
    ("The replacement part shipped late.", "placement"),
    ("Better prompting beats a bigger model.", "prompt"),  # only plural s is allowed
    ("She said hello in an email.", "ai"),
])
def test_whole_word_match_rejects_longer_words(text, phrase):
    assert not finder._has_phrase(text.lower(), phrase)
