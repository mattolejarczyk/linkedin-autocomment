"""Fit a comment to where the person posting is at.

A business question under a post about someone's launch, or their layoff, reads as if
nobody read it. So one step runs before writing: name the moment, then follow that
moment's fixed rule. A model still names the moment, but that is one clear question with
a set answer for each case, and it reads the whole post, because a line that changes the
answer can sit anywhere in it.

Off unless a profile sets ``comment_generator.moment_rules``. With it off the generator
behaves exactly as before.
"""

import json
import re
from dataclasses import dataclass

WIN = "win"
WIN_AFTER_HARDSHIP = "win_after_hardship"
HARD_TIME = "hard_time"
PERSONAL_LESSON = "personal_lesson"
BUSINESS_LESSON = "business_lesson"
ASKED = "asked"
BUSINESS = "business"
LEAD_FUNNEL = "lead_funnel"

#: The whole reply when answering would mean inventing an experience.
SKIP_WORD = "SKIP"

SYSTEM_MESSAGE = ("You write short, warm, respectful comments the way a decent person "
                  "would reply to someone they respect. Plain words, no performance.")


@dataclass(frozen=True)
class Moment:
    code: str
    spot: str
    does: str = ""
    never: str = ""
    example: str = ""
    allows_question: bool = False
    max_exclamations: int = 0
    #: A draft here needs a human's eye for tone, whoever reads it.
    needs_care: bool = False

    @property
    def has_own_prompt(self) -> bool:
        return bool(self.does)


# Examples are made up, to show the shape only.
_MOMENTS = (
    Moment(LEAD_FUNNEL,
           "It asks readers to comment a word, or to connect, in order to be sent something "
           "such as a guide, template or link. This wins even if the post is also a story "
           "or a launch."),
    Moment(HARD_TIME,
           "Their own layoff, illness, loss, failure or burnout, with no win in it.",
           does="Acknowledge it in plain words. You may thank them for sharing, or say you "
                "appreciate their willingness to share. Then stop.",
           never="Ask a question. Give advice. Look for a silver lining. Say congrats.",
           example="Sorry you're going through this. Thank you for sharing it.",
           needs_care=True),
    Moment(WIN_AFTER_HARDSHIP,
           "Their own milestone, in a post that also mentions illness, loss or a hard stretch.",
           does="Celebrate the win, then add one encouraging or complimentary, respectful "
                "line about them. One or two short sentences.",
           never="Name the illness or hardship. Treat it as a story to dig into. Ask a "
                 "question.",
           example="Congrats on opening the doors. That kind of resolve is rare!",
           max_exclamations=1),
    Moment(WIN,
           "Their own milestone, told personally: a launch, new role, funding, award, "
           "anniversary or book. Words like finally, today I'm, proud. Not a post whose point "
           "is selling or showing off a tool, a result or a method, even one that says I built "
           "or brags about a number: that is business.",
           does="Congratulate them and name the thing. One or two short, warm sentences. You "
                "may say how it makes you feel.",
           never="Ask about tools, process or results. Give advice. Ask a question.",
           example="Congrats on the new role. They're lucky to have you!",
           max_exclamations=1),
    Moment(PERSONAL_LESSON,
           "Something they learned from their own life, health, family, faith or character, "
           "not about how to run a business or do a job.",
           does="React with something positive, encouraging or uplifting, as good manners "
                "would suggest.",
           never="Ask a question. Turn it into a lesson of your own.",
           example="That took real honesty. Good on you for saying it out loud."),
    Moment(BUSINESS_LESSON,
           "Something they learned from their own experience running a business or doing a "
           "job: I learned, I got this wrong, what this cost me.",
           does="React to their lesson with something positive and encouraging. One question "
                "about their experience is fine.",
           never="Turn it into a lesson of your own. Ask more than one question.",
           example="Owning that mistake in public helps a lot of people. What changed first "
                   "once you saw it?",
           allows_question=True),
    Moment(ASKED,
           "It asks readers for their experience or opinion, such as what has been your "
           "experience or what are your thoughts. Not a sales line such as want to know "
           "more, and not a request to comment in order to receive something.",
           does="Respond to what they asked. Reply with exactly SKIP if answering would mean "
                "inventing an experience, result or number.",
           never="Change the subject. Claim an experience you were not given.",
           example="The approval step sounds like the first thing to settle. Worth doing "
                   "before picking the tool.",
           allows_question=True),
    Moment(BUSINESS,
           "How-to, framework, data, tool, news, a pitch or a product announcement, including "
           "I built this tool posts, with no personal stake in it."),
)

BY_CODE = {m.code: m for m in _MOMENTS}


def classify_prompt(post_text: str, r) -> str:
    """Ask which one moment a post is. Earlier in the list wins over later."""
    rows = "\n".join(f"- {m.code}: {m.spot}" for m in _MOMENTS)
    return f"""Decide where the person writing this {r.item} is at. Read all of it: a line near
the end can change the answer.

Pick the FIRST one of these that fits. If it is both personal and business, the person wins.
{rows}

{r.item.upper()}:
{post_text}

Reply with only this JSON, with your reasoning inside it and nothing outside it:
{{"reason": "<one short sentence>", "moment": "<one code from the list>"}}"""


def code_from_reply(text: str):
    """The moment code in a reply, or None.

    Takes the last JSON object naming a moment, wherever it sits. On a post that takes
    some thought the model reasons out loud first, whatever it was told, and at temperature
    0 it does so every time, so asking again does not help (saved replies, 2026-11-04).
    """
    found = re.findall(r'\{[^{}]*"moment"[^{}]*\}', text or "")
    try:
        return json.loads(found[-1]).get("moment") if found else None
    except ValueError:
        return None


def comment_prompt(moment: Moment, post: dict, profile_block: str, max_words, r) -> str:
    """The prompt for a moment with its own rule. The whole post, no style lottery."""
    exclamation = (f"At most {moment.max_exclamations} exclamation mark."
                   if moment.max_exclamations else "No exclamation marks.")
    question = ("At most one question." if moment.allows_question else "No question at all.")
    return f"""Write {r.article} {r.surface} {r.action}.

{profile_block}

WHERE THIS PERSON IS AT. This wins over anything in PROFILE that differs, including any
rule that a {r.action} should ask a question or engage with a detail, and any rule against
stating a feeling or using an exclamation mark.
Do: {moment.does}
Never: {moment.never}
Shape only, do not copy it: "{moment.example}"

Rules that still hold: {question} {exclamation} Under {max_words or 35} words. Never use a
hyphen or dash as punctuation between clauses. Never say we or our. Nothing about yourself,
your work or your products. No hashtags or emojis.

{r.item.upper()} TO RESPOND TO:
Author: {post.get('author_name', 'Unknown')}
{post.get('text', '')}

Write ONLY the {r.action} text:"""


def lead_funnel_line(r) -> str:
    """Added to the business prompt. Typing their word would sign up for the funnel."""
    return (f"\n\nThis {r.item} asks readers to comment a word to be sent something. Do not "
            "write that word and do not ask for the thing.")


def problems(moment: Moment, comment: str) -> list:
    """What this draft breaks of its moment's rule. Checked, not left to the model."""
    found = []
    if moment.has_own_prompt:
        if not moment.allows_question and "?" in comment:
            found.append("question")
        if comment.count("!") > moment.max_exclamations:
            found.append("exclamation")
    if moment.code == HARD_TIME and re.search(r"congrat", comment, re.IGNORECASE):
        found.append("congrats")
    return found


def judge(moment: Moment, comment: str, issues: list) -> list:
    """The general checker's issues, adjusted for this moment.

    Thanking someone for sharing is generic everywhere except a hard time, where it is
    exactly right.
    """
    if moment.code == HARD_TIME:
        issues = [i for i in issues
                  if "thanks for sharing" not in i and "i appreciate" not in i]
    return issues + [f"moment_rule: {p}" for p in problems(moment, comment)]


def is_skip(comment: str) -> bool:
    return comment.strip().rstrip(".").upper() == SKIP_WORD
