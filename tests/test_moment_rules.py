"""Comments that fit where the person is at. No network: the provider is scripted."""

import json

import pytest

from linkedin_automation import comment_generator as gen
from linkedin_automation import moment_rules as mr
from linkedin_automation import post_store
from linkedin_automation import profile_manager as pm
from linkedin_automation import providers

ON = {"comment_generator": {"moment_rules": True, "max_words": 35}}
PERSONA = {"comment_generator": {"moment_rules": True, "max_words": 35,
                                 "tone": "No exclamation marks.",
                                 "voice": "Every comment asks a question."}}


class FakeClient:
    name = "fake"

    def __init__(self, moment=mr.WIN, drafts=None):
        self.moment = moment
        self.drafts = list(drafts or [])
        self.moment_calls = []
        self.draft_calls = []

    def complete(self, model, system, user, temperature=None, max_tokens=None,
                 json_object=False):
        if "Answer with exactly YES or NO" in user:
            text = "YES"
        elif "where the person writing" in user:
            self.moment_calls.append(user)
            if isinstance(self.moment, Exception):
                raise self.moment
            text = (self.moment if self.moment.startswith("{") or self.moment == "not json"
                    else json.dumps({"moment": self.moment, "reason": "x"}))
        elif json_object:
            text = json.dumps({"verdict": "engage", "post_category": "announcement"})
        else:
            self.draft_calls.append((system, user))
            text = self.drafts.pop(0) if self.drafts else "A plain default comment."
        return providers.Completion(text=text, model=model, provider=self.name)


@pytest.fixture
def make(monkeypatch, comments_dir):
    monkeypatch.setattr(providers, "get_provider",
                        lambda name, api_key=None, base_url=None: FakeClient())

    def _make(client, config=ON):
        monkeypatch.setattr(pm, "get_profile_config", lambda profile_name=None: config)
        g = gen.AuthenticCommentGenerator("dummy.json", profile_name="t",
                                          platform=gen.platform_policy.LINKEDIN)
        g.provider = client
        return g
    return _make


LATE_LINE = "After the illness last spring this was not a given."
POST = {"author_name": "Sam Example", "url": "https://example.com/p/1",
        "text": "We opened the doors today. " + ("Some long middle part. " * 60) + LATE_LINE}
EVALUATION = {"post_category": "announcement", "engagement_approach": "add_perspective"}


# ─── Off by default ───────────────────────────────────────────────────────────

def test_off_makes_no_moment_call_and_adds_nothing(make):
    client = FakeClient(drafts=["The pricing point stands out. How long did it take?"])
    result = make(client, {"comment_generator": {}}).generate_comment(POST, EVALUATION)
    assert client.moment_calls == []
    assert "moment" not in result and "needs_care" not in result
    system, user = client.draft_calls[0]
    assert system != mr.SYSTEM_MESSAGE
    assert LATE_LINE not in user                  # the old prompt, cut at 500 characters


# ─── Reading the moment ───────────────────────────────────────────────────────

def test_the_moment_is_read_from_the_whole_post(make):
    client = FakeClient(moment=mr.WIN_AFTER_HARDSHIP, drafts=["Congrats on opening. Well earned!"])
    make(client).generate_comment(POST, EVALUATION)
    assert LATE_LINE in client.moment_calls[0]
    assert LATE_LINE in client.draft_calls[0][1]


@pytest.mark.parametrize("moment", [RuntimeError("api down"), "not json", "made_up_code"])
def test_a_moment_it_cannot_read_gives_no_draft_and_keeps_evidence(make, comments_dir, moment):
    """Never falls back to a business comment: that is the failure this step prevents."""
    client = FakeClient(moment=moment)
    g = make(client)
    assert g.generate_comment(POST, EVALUATION) is None
    assert client.draft_calls == []
    with open(f"{g.output_dir}/reply_failures.jsonl", encoding="utf-8") as f:
        assert json.loads(f.readlines()[-1])["kind"] == "moment"


@pytest.mark.parametrize("reply,code", [
    ('{"reason": "x", "moment": "win"}', "win"),
    ('```json\n{"reason": "x", "moment": "win"}\n```', "win"),
    ('Checking in order: lead_funnel does not fit.\n\n{"reason": "x", "moment": "hard_time"}',
     "hard_time"),
    ('Maybe {"moment": "win"} but no.\n{"moment": "hard_time"}', "hard_time"),
    ("Checking in order: no answer here.", None),
    ('{"moment": broken}', None),
    ("", None),
    (None, None),
])
def test_the_answer_is_found_in_the_reply(reply, code):
    assert mr.code_from_reply(reply) == code


def test_prose_instead_of_json_is_asked_once_more(make):
    class ProseFirst(FakeClient):
        def complete(self, model, system, user, temperature=None, max_tokens=None,
                     json_object=False):
            if "where the person writing" in user and not self.moment_calls:
                self.moment_calls.append(user)
                return providers.Completion(text="Checking the order: this is a launch.",
                                            model=model, provider=self.name)
            return super().complete(model, system, user, temperature, max_tokens, json_object)

    client = ProseFirst(moment=mr.WIN, drafts=["Congrats on the launch."])
    g = make(client)
    assert g.generate_comment(POST, EVALUATION)["moment"] == mr.WIN
    assert len(client.moment_calls) == 2
    with open(f"{g.output_dir}/reply_failures.jsonl", encoding="utf-8") as f:
        assert "Checking the order" in json.loads(f.readlines()[-1])["text_start"]


def test_the_moment_is_asked_at_most_twice(make):
    client = FakeClient(moment="not json")
    assert make(client).generate_comment(POST, EVALUATION) is None
    assert len(client.moment_calls) == 2


def test_company_funding_news_is_a_win_even_written_as_we():
    assert "funding news counts, even written as we" in mr.BY_CODE[mr.WIN].spot


def test_the_list_puts_the_funnel_first_and_business_last():
    codes = [m.code for m in mr._MOMENTS]
    assert codes[0] == mr.LEAD_FUNNEL and codes[-1] == mr.BUSINESS
    assert codes.index(mr.HARD_TIME) < codes.index(mr.WIN)
    assert codes.index(mr.WIN_AFTER_HARDSHIP) < codes.index(mr.WIN)
    assert set(codes) == set(mr.BY_CODE)


# ─── Each moment's rule ───────────────────────────────────────────────────────

@pytest.mark.parametrize("code,draft", [
    (mr.WIN, "Congrats on the launch. Excited for what comes next!"),
    (mr.WIN_AFTER_HARDSHIP, "Congrats on opening. That resolve is rare!"),
    (mr.HARD_TIME, "Sorry you're going through this. Thank you for sharing it."),
    (mr.PERSONAL_LESSON, "That took real honesty. Good on you for saying it."),
    (mr.BUSINESS_LESSON, "Owning that helps people. What changed first?"),
    (mr.ASKED, "The approval step is the first thing to settle."),
])
def test_a_personal_moment_gets_its_own_rule_and_the_draft_passes(make, code, draft):
    client = FakeClient(moment=code, drafts=[draft])
    result = make(client, PERSONA).generate_comment(POST, EVALUATION)
    assert result["comment"] == draft and result["moment"] == code
    assert result["style"] == f"moment_{code}" and result["issues"] == []
    system, user = client.draft_calls[0]
    assert system == mr.SYSTEM_MESSAGE
    assert mr.BY_CODE[code].does in user and mr.BY_CODE[code].never in user
    assert "wins over anything in PROFILE" in user
    assert "Every comment asks a question." in user      # the persona stays in the prompt
    assert "Style:" not in user and "GOOD QUESTION ENDINGS" not in user
    assert LATE_LINE in user


@pytest.mark.parametrize("code,draft,broken", [
    (mr.WIN, "Congrats on the launch. What stack did you use?", "question"),
    (mr.WIN_AFTER_HARDSHIP, "Congrats. How are you feeling now?", "question"),
    (mr.HARD_TIME, "So sorry. What happened?", "question"),
    (mr.HARD_TIME, "Congrats on getting through it.", "congrats"),
    (mr.HARD_TIME, "So sorry to hear this!", "exclamation"),
    (mr.PERSONAL_LESSON, "Lovely lesson. How did your family take it?", "question"),
    (mr.WIN, "Congrats! So happy for you!", "exclamation"),
])
def test_a_draft_that_breaks_its_moment_is_never_shipped(make, code, draft, broken):
    client = FakeClient(moment=code, drafts=[draft, draft, draft])
    g = make(client)
    assert g.generate_comment(POST, EVALUATION) is None
    assert len(client.draft_calls) == 3
    with open(f"{g.output_dir}/reply_failures.jsonl", encoding="utf-8") as f:
        last = json.loads(f.readlines()[-1])
    assert last["kind"] == "moment_rule" and broken in last["detail"]


def test_a_retry_can_fix_a_broken_draft(make):
    client = FakeClient(moment=mr.WIN, drafts=["Congrats. What stack?", "Congrats on the launch!"])
    result = make(client).generate_comment(POST, EVALUATION)
    assert result["comment"] == "Congrats on the launch!" and result["attempt"] == 2


def test_only_a_personal_lesson_refuses_a_question_a_business_lesson_allows_one():
    assert mr.problems(mr.BY_CODE[mr.PERSONAL_LESSON], "Good. Why?") == ["question"]
    assert mr.problems(mr.BY_CODE[mr.BUSINESS_LESSON], "Good. Why?") == []


@pytest.mark.parametrize("words", ["Thanks for sharing this.", "I appreciate you sharing this."])
def test_thanking_them_is_right_on_a_hard_time_and_generic_elsewhere(make, words):
    issues = make(FakeClient()).detect_ai_patterns(words)[1]
    assert issues
    assert mr.judge(mr.BY_CODE[mr.HARD_TIME], words, issues) == []
    assert mr.judge(mr.BY_CODE[mr.WIN], words, issues) == issues


def test_a_hard_time_is_flagged_for_care_and_a_win_is_not(make):
    hard = make(FakeClient(moment=mr.HARD_TIME, drafts=["So sorry to read this."]))
    assert hard.generate_comment(POST, EVALUATION)["needs_care"] is True
    win = make(FakeClient(moment=mr.WIN, drafts=["Congrats on the launch."]))
    assert win.generate_comment(POST, EVALUATION)["needs_care"] is False


# ─── Business and funnels keep today's rules ──────────────────────────────────

def _prompt_without_moment(make, client_cfg):
    client = FakeClient(drafts=["A plain default comment."])
    make(client, client_cfg).generate_comment(POST, EVALUATION)
    return client.draft_calls[0]


def test_business_uses_todays_rules_unchanged(make, monkeypatch):
    monkeypatch.setattr(gen.random, "choices", lambda seq, weights=None: [seq[0]])
    monkeypatch.setattr(gen.random, "choice", lambda seq: seq[0])
    system_off, user_off = _prompt_without_moment(make, {"comment_generator": {"max_words": 35}})
    client = FakeClient(moment=mr.BUSINESS, drafts=["A plain default comment."])
    result = make(client).generate_comment(POST, EVALUATION)
    assert client.draft_calls[0] == (system_off, user_off)
    assert result["moment"] == mr.BUSINESS and result["needs_care"] is False


def test_a_lead_funnel_gets_todays_rules_and_never_types_their_word(make):
    client = FakeClient(moment=mr.LEAD_FUNNEL, drafts=["How long did the template take?"])
    result = make(client).generate_comment(POST, EVALUATION)
    assert result["moment"] == mr.LEAD_FUNNEL and result["style"] != f"moment_{mr.LEAD_FUNNEL}"
    system, user = client.draft_calls[0]
    assert system != mr.SYSTEM_MESSAGE
    assert "Do not write that word" in user


# ─── Skipping and the store ───────────────────────────────────────────────────

@pytest.mark.parametrize("reply", ["SKIP", "skip.", " Skip "])
def test_skip_means_no_draft_and_the_post_is_turned_down(make, reply):
    client = FakeClient(moment=mr.ASKED, drafts=[reply])
    g = make(client)
    g.rejected_posts = []
    assert g.generate_comment(POST, EVALUATION) is None
    assert g.rejected_posts == [POST]
    assert len(client.draft_calls) == 1


def test_the_word_skip_inside_a_real_comment_is_not_a_skip():
    assert mr.is_skip("Skip the tool, settle the approval step first.") is False


def test_the_moment_and_flag_reach_the_store(make, comments_dir):
    client = FakeClient(moment=mr.HARD_TIME, drafts=["So sorry to read this."])
    g = make(client)
    data = g.generate_comment(POST, EVALUATION)
    g._mark_generated_in_store([{"post_url": POST["url"], "comment": data["comment"],
                                 "style": data["style"], "approach": data["approach"],
                                 "word_count": data["word_count"], "moment": data["moment"],
                                 "needs_care": data["needs_care"]}])
    rec = post_store.PostStore("t", platform=g.platform)._resolve(POST["url"])
    assert rec["comment_meta"]["moment"] == mr.HARD_TIME
    assert rec["comment_meta"]["needs_care"] is True


def test_with_no_moment_the_store_meta_is_as_before(make, comments_dir):
    g = make(FakeClient(), {"comment_generator": {}})
    g._mark_generated_in_store([{"post_url": POST["url"], "comment": "x", "style": "s",
                                 "approach": "a", "word_count": 1}])
    rec = post_store.PostStore("t", platform=g.platform)._resolve(POST["url"])
    assert set(rec["comment_meta"]) == {"style", "approach", "word_count"}
