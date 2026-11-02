"""Conversational turns that are not retrieval questions.

A greeting, a "what can you do", a "thanks". None of these is a
question about a corpus, and putting them through retrieval produces
the worst answer the product can give: a blunt "I could not find
anything in your organization's documents", which reads as the system
being broken rather than as it being careful.

Handled here instead, with two properties that matter:

**No model call.** Replies are deterministic text assembled from the
caller's own document list. There is nothing for a model to invent, so
relaxing the grounding rules was never on the table -- the strictness
that makes answers trustworthy is untouched, and this sits in front of
it. The alternative, letting the system prompt chat when retrieval
comes back empty, would hand the model licence to answer from training
data, which is the exact failure the whole pipeline exists to prevent.

**Anchored matching.** A false positive is far worse than a false
negative here. Missing a greeting costs a slightly cold reply;
intercepting "what is the refund policy" replaces a correct grounded
answer with small talk. So every pattern matches the WHOLE normalised
message, never a substring, and a word cap backs that up.
"""
import enum
import re

#: Longest message that can be an intent. "what can you help me with"
#: is six words; anything past this is prose and belongs in retrieval
#: even if it happens to start with "hello".
_MAX_WORDS = 8

#: How many document names a reply will list before summarising.
_MAX_LISTED = 10


class Intent(str, enum.Enum):
    NONE = "none"
    GREETING = "greeting"
    CAPABILITY = "capability"
    THANKS = "thanks"


def _normalise(text: str) -> str:
    """Lowercase, strip surrounding punctuation, collapse whitespace.

    Apostrophes are kept (so "how's" still matches) but trailing
    punctuation and emoji-ish trailing characters are not, because
    "hello!" and "hello" are the same intent.
    """
    cleaned = text.strip().lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" .!?,;:\"'")


_GREETING = re.compile(
    r"^(?:"
    r"(?:hi|hello|hey|hiya|howdy|yo|greetings|sup)"
    r"(?: there| documind| again)?"
    r"|good (?:morning|afternoon|evening|day)"
    r"|(?:hi|hello|hey) (?:how are you|how're you|how are u)"
    r"|how are you(?: doing)?|how're you|how are u|how r u"
    r"|how(?:'s| is) it going"
    r")$"
)

_CAPABILITY = re.compile(
    r"^(?:"
    r"help|help me|i need help"
    r"|(?:can|could) you help(?: me)?"
    r"|how (?:can|could) (?:you|u) (?:help|hlp)(?: me)?"
    r"|how (?:can|do) i (?:use|start)(?: this| you)?"
    r"|what (?:can|do) you do"
    r"|what (?:can|should) i ask(?: you)?"
    r"|what are you(?: for)?|who are you"
    r"|what is this|what's this"
    r"|what (?:documents|docs|files) (?:do you have|are there|can you see)"
    r"|what do you know"
    r"|what (?:is|are) (?:this|these) (?:document|documents|docs?) about"
    r")$"
)

_THANKS = re.compile(
    r"^(?:thanks|thank you|thanks a lot|thank you so much|thx|ty|cheers|"
    r"appreciate it)$"
)


def classify(text: str) -> Intent:
    """Which conversational intent this message is, if any."""
    normalised = _normalise(text)
    if not normalised:
        return Intent.NONE
    # The cap is checked BEFORE the patterns: it is the cheap guard,
    # and it means a pathological input never reaches a regex.
    if len(normalised.split()) > _MAX_WORDS:
        return Intent.NONE

    if _GREETING.match(normalised):
        return Intent.GREETING
    if _CAPABILITY.match(normalised):
        return Intent.CAPABILITY
    if _THANKS.match(normalised):
        return Intent.THANKS
    return Intent.NONE


def _corpus_summary(document_names: list[str]) -> str:
    """What this organization has, in the user's own filenames."""
    if not document_names:
        return (
            "There are no documents uploaded yet, so there is nothing for me "
            "to search. Once someone with admin access uploads a PDF, "
            "Markdown or text file on the Documents page, I can answer "
            "questions about it."
        )

    total = len(document_names)
    listed = document_names[:_MAX_LISTED]
    lines = "\n".join(f"- {name}" for name in listed)
    header = (
        f"You have {total} document{'s' if total != 1 else ''} I can search"
    )
    if total > len(listed):
        # The remainder is counted, not dropped: a user told "here are
        # your documents" and shown ten of forty has been misled.
        header += f" (showing the first {len(listed)})"
    return f"{header}:\n{lines}"


def reply_for(intent: Intent, *, document_names: list[str]) -> str | None:
    """The reply text for an intent, or None if there is no intent.

    Returning None for NONE rather than a default greeting means a
    caller that forgets to check gets nothing it can accidentally send
    in place of a real answer.
    """
    if intent is Intent.NONE:
        return None

    corpus = _corpus_summary(document_names)
    has_documents = bool(document_names)
    example = (
        f'Try asking something specific, for example: "what does '
        f'{document_names[0]} say about ..."'
        if has_documents
        else ""
    )

    if intent is Intent.GREETING:
        parts = [
            "Hello. I answer questions using your organization's documents "
            "and nothing else, and I show you the passage behind every "
            "answer so you can check it.",
            corpus,
        ]
        if example:
            parts.append(example)
        return "\n\n".join(parts)

    if intent is Intent.CAPABILITY:
        parts = [
            "I answer questions from your organization's uploaded documents. "
            "Every answer cites the passage it came from, and when the "
            "documents do not cover something I say so instead of guessing "
            "— so an answer here is one you can verify.",
            corpus,
        ]
        if example:
            parts.append(example)
        return "\n\n".join(parts)

    # THANKS
    return (
        "You're welcome. Ask me anything else about your documents whenever "
        "you need to."
    )
