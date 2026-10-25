"""Turn a follow-up into a standalone question.

"What about contractors?" retrieves nothing useful on its own -- there is
no embedding of that sentence that lands anywhere near a contractor
expense policy, and the keyword leg has three stopwords to work with. The
conversation knows what "that" refers to; the retriever does not.

Two rules govern this module:

  1. Rewriting happens ONLY when there is history. A first question is
     already standalone, and sending it through a model would add latency
     and a failure mode to the common case for nothing.
  2. Retrieved text NEVER reaches the rewriter. The rewriter sees the
     user's own turns and the assistant's prior answers, both of which the
     tenant authored or the system produced -- never a document. That
     keeps document content out of a model whose output is used to build a
     cache key.
"""
import logging
import re

from app.core.config import settings
from app.services.llm import LLMProvider

logger = logging.getLogger(__name__)

REWRITE_SYSTEM_PROMPT = """\
You rewrite a follow-up question into a standalone question.

Given a short conversation history and the user's latest message, output a \
single self-contained question that preserves the user's intent and \
resolves every pronoun and ellipsis using the history.

Rules:
- Output ONLY the rewritten question. No preamble, no explanation, no \
quotes.
- If the latest message is already self-contained, output it unchanged.
- Never answer the question. Never add information that is not in the \
history.
- Keep the user's own terminology; do not translate domain terms into \
synonyms."""


def normalize_question(question: str) -> str:
    """Canonical form used for cache keys.

    Deliberately conservative: case, surrounding whitespace, repeated
    internal whitespace, and trailing punctuation only. Anything more
    aggressive (stemming, stopword removal) would collide genuinely
    different questions onto one key and serve the wrong cached answer --
    "can I expense this" and "can I not expense this" must never share a
    key.
    """
    collapsed = " ".join((question or "").split())
    return re.sub(r"[?!.\s]+$", "", collapsed).casefold()


def _history_block(history: list[dict], turns: int) -> str:
    # Only the last N turns. A longer window costs tokens on every request
    # and rarely helps: the referent of "that" is almost always in the
    # immediately preceding turns.
    recent = history[-turns * 2 :] if turns else history
    lines = []
    for message in recent:
        role = "User" if message.get("role") == "user" else "Assistant"
        content = " ".join((message.get("content") or "").split())
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def rewrite_question(
    question: str,
    history: list[dict],
    *,
    llm: LLMProvider,
    turns: int | None = None,
) -> str:
    """Return a standalone question, or `question` unchanged.

    Falls back to the original question on any rewriter failure. A broken
    rewriter should degrade retrieval quality for follow-ups, not take the
    endpoint down -- and the original text is always a valid query.
    """
    if not history:
        # Rule 1: no history, no rewrite. The first question in a
        # conversation is standalone by definition.
        return question

    turns = settings.REWRITE_HISTORY_TURNS if turns is None else turns
    user = (
        f"Conversation so far:\n{_history_block(history, turns)}\n\n"
        f"Latest user message: {question}\n\n"
        f"Standalone question:"
    )

    try:
        rewritten = await llm.complete(
            system=REWRITE_SYSTEM_PROMPT,
            user=user,
            max_tokens=settings.REWRITE_MAX_TOKENS,
            # Near-zero: this is a mechanical transformation, and variation
            # in the output changes the cache key for an identical input.
            temperature=0.0,
        )
    except Exception:
        logger.warning("query rewrite failed; using the original question", exc_info=True)
        return question

    rewritten = (rewritten or "").strip().strip('"').strip()
    if not rewritten:
        return question
    # A rewriter that returns an essay has misbehaved; the original is
    # safer than a paragraph used as a search query.
    if len(rewritten) > 4 * max(len(question), 200):
        logger.warning("query rewrite returned an implausibly long result; ignoring it")
        return question
    return rewritten
