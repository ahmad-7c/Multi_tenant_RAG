"""Conversational turns that are not retrieval questions.

"hi", "what can you do", "thanks" are not questions about a corpus.
Sending them through retrieval produces the worst possible answer --
a blunt "I could not find anything in your documents" that reads as
the product being broken, which is what a real transcript showed.

Two rules govern everything here:

1. **Never swallow a real question.** A false positive is far worse
   than a false negative: missing a greeting costs a slightly cold
   reply, but intercepting "what is the refund policy" replaces a
   correct grounded answer with chatter. Matching is therefore
   ANCHORED on the whole message, not a substring search.
2. **No model call.** These replies are deterministic text built from
   the org's own document list, so there is nothing for a model to
   invent and nothing to cite.
"""
import pytest

from app.services.assistance import Intent, classify, reply_for


# ----------------------------------------------------- classification ----


@pytest.mark.parametrize(
    "text",
    [
        "hi",
        "Hi!",
        "hello",
        "Hello.",
        "hey",
        "hey there",
        "yo",
        "hiya",
        "howdy",
        "good morning",
        "Good Afternoon",
        "good evening",
        "how are you",
        "how are you?",
        "hey how are you",
        "how's it going?",
        "  hello   ",
    ],
)
def test_greetings_are_recognised(text):
    assert classify(text) is Intent.GREETING


@pytest.mark.parametrize(
    "text",
    [
        "help",
        "help me",
        "can you help me",
        "how can you help me",
        "how can u hlp me",
        "what can you do",
        "what can i ask",
        "what do you do",
        "who are you",
        "what is this",
        "what documents do you have",
        "what is this document about",
        "what are these documents about",
    ],
)
def test_capability_questions_are_recognised(text):
    assert classify(text) is Intent.CAPABILITY


@pytest.mark.parametrize(
    "text", ["thanks", "Thanks!", "thank you", "thank you so much", "ty", "cheers"]
)
def test_thanks_is_recognised(text):
    assert classify(text) is Intent.THANKS


@pytest.mark.parametrize(
    "text",
    [
        # Real questions that CONTAIN a greeting word or a question
        # shape. Intercepting any of these would replace a grounded
        # answer with small talk.
        "what is the refund policy",
        "hello world program in the onboarding guide",
        "how do I say hello to a new hire",
        "what can i ask the vendor about security",
        "who are the approvers for expenses over 500",
        "what is this document about the owner dashboard",
        "help desk escalation policy",
        "what documents do you have about parental leave",
        "how are you measuring retrieval recall",
        "thanks page copy in the handbook",
        # An empty-ish message is not an intent either.
        "",
        "   ",
        "?",
    ],
)
def test_real_questions_are_never_intercepted(text):
    assert classify(text) is Intent.NONE


def test_a_long_message_is_never_an_intent():
    """A word cap backs up the anchored match.

    Even if a pattern somehow matched a long message, the cap stops it
    -- defence in depth for the rule that matters most here.
    """
    assert classify("hello " * 40) is Intent.NONE


# ------------------------------------------------------------ replies ----


def test_the_capability_reply_names_the_orgs_documents():
    """The useful half: a user who does not know what to ask is told."""
    text = reply_for(
        Intent.CAPABILITY,
        document_names=["travel-policy.md", "owner-dashboard.pdf"],
    )

    assert "travel-policy.md" in text
    assert "owner-dashboard.pdf" in text


def test_the_capability_reply_is_honest_when_there_are_no_documents():
    text = reply_for(Intent.CAPABILITY, document_names=[])

    # Says what is missing and what to do, rather than listing nothing.
    assert "no documents" in text.lower()
    assert "upload" in text.lower()
    # And does not promise a capability that cannot work yet.
    assert "travel-policy" not in text


def test_a_long_document_list_is_truncated_with_a_count():
    """An org with 200 documents must not get 200 lines of chat."""
    names = [f"doc-{i}.pdf" for i in range(40)]

    text = reply_for(Intent.CAPABILITY, document_names=names)

    assert text.count("\n- ") <= 10
    # The ones not listed are accounted for rather than silently
    # dropped, so the count the user sees is the real one.
    assert "40" in text


def test_every_intent_produces_a_non_empty_reply():
    for intent in (Intent.GREETING, Intent.CAPABILITY, Intent.THANKS):
        assert reply_for(intent, document_names=["a.pdf"]).strip()


def test_none_has_no_reply():
    """Belt and braces: a caller that forgets to check gets nothing
    rather than a greeting in place of an answer."""
    assert reply_for(Intent.NONE, document_names=["a.pdf"]) is None


def test_replies_never_claim_to_have_searched():
    """These turns do no retrieval, so the wording must not imply it.

    "I could not find that in your documents" after a greeting is the
    exact confusion this feature exists to remove.
    """
    for intent in (Intent.GREETING, Intent.CAPABILITY, Intent.THANKS):
        text = (reply_for(intent, document_names=["a.pdf"]) or "").lower()
        assert "could not find" not in text
        assert "not enough information" not in text
