"""An LLM judge for one narrow question: is the answer grounded?

Used only for the refusal measurement, where the thing being judged is
not a fact but a speech act -- did the answer DECLINE, or did it assert
something the context does not support? A substring check cannot do
that: "The policy does not state a motorcycle rate" and "The motorcycle
rate is the federal rate" share most of their vocabulary.

Note the deliberate inconsistency with eval/scoring.py, which rejected
an LLM judge in favour of a heuristic. That was for a REGRESSION metric
compared across runs, where a second model's non-determinism pollutes
the number. This is a one-off diagnostic answering "does the model
already refuse these?", where the alternative is reading twelve answers
by hand and calling it data. Different job, different tool.

TWO AXES, NOT ONE. The first version of this judge asked for a single
verdict and got both decisive cases wrong. Given:

    "A rollback does not revert database migrations, and reverting a
     migration is a separate, manual change [1]. The provided documents
     do not contain instructions on how to perform that manual change."

it answered SUPPORTED -- true of every claim in the sentence, and
entirely beside the point, because the answer also DECLINED. "Is this
grounded?" and "did this decline?" are independent questions, and a
judge asked for one label answers whichever is easier. So the model now
reports `asserts_answer` and `supported` separately and the label is
derived here.

Three guards keep it honest:

  * It never sees the golden label. If it were told the expected
    verdict it would agree with it.
  * Canned refusals are matched BEFORE any model call. The system's own
    refusal strings are fixed text; spending a model call to recognise
    them adds cost and a chance of being wrong about something certain.
  * Verdicts are reported per case with the answer text, never only as
    a rate, so a disagreement can be read and overruled. It was.
"""
import json
import re
from dataclasses import dataclass

from app.services.llm import LLMProvider
from app.services.prompting import INSUFFICIENT_CONTEXT_ANSWER
from app.services.reranker import NOT_FOUND_ANSWER

DECLINED = "DECLINED"
SUPPORTED = "SUPPORTED"
UNSUPPORTED = "UNSUPPORTED"
UNPARSEABLE = "UNPARSEABLE"

VERDICTS = (DECLINED, SUPPORTED, UNSUPPORTED)

SYSTEM = """You grade an ANSWER against the CONTEXT it was given.

Reply with JSON only:
{"asserts_answer": true, "supported": true, "reason": "..."}

Two SEPARATE judgements. Do not collapse them.

asserts_answer
    true  - the answer states a direct answer to the question asked.
    false - the answer says the documents do not cover what was asked.
            It may still report related facts from the context; what
            matters is whether it presents them as THE answer. An
            answer that gives related facts AND says the specific thing
            asked is not in the documents is false.

supported
    Only meaningful when asserts_answer is true. Is every claim in the
    asserted answer STATED in the CONTEXT?
    true  - yes, stated outright.
    false - some claim is not stated: an inference the context merely
            implies, outside knowledge, or a rule carried over from a
            different subject in the context.

Rules:
  - Judge only against the CONTEXT, never outside knowledge, even when
    the answer is true in the real world.
  - A yes/no answer derived from a rule that does not mention the case
    asked about is an inference, so supported is false. "Deploys run
    Tuesday to Thursday" does not state what happens on Friday.
  - Citation markers like [1] are formatting; ignore them.
  - "reason" must be one sentence, under 25 words.
"""

USER = """QUESTION:
{question}

CONTEXT:
{context}

ANSWER:
{answer}
"""


@dataclass
class Verdict:
    verdict: str
    reason: str
    # True when decided without a model call.
    deterministic: bool = False
    asserts_answer: bool | None = None
    supported: bool | None = None
    raw: str | None = None

    @property
    def declined(self) -> bool:
        return self.verdict == DECLINED


CANNED = (NOT_FOUND_ANSWER, INSUFFICIENT_CONTEXT_ANSWER)


def canned_refusal(answer: str) -> Verdict | None:
    """Match the system's own refusal text without a model call."""
    normalised = " ".join((answer or "").split()).lower()
    if not normalised:
        # An empty answer is not a refusal; it is a different failure,
        # and calling it DECLINED would credit the gate for a bug.
        return Verdict(verdict=UNSUPPORTED, reason="empty answer", deterministic=True)
    for text in CANNED:
        if " ".join(text.split()).lower() in normalised:
            return Verdict(
                verdict=DECLINED,
                reason="matched the system's own refusal text",
                deterministic=True,
                asserts_answer=False,
            )
    return None


def verdict_from_axes(asserts: bool, supported: bool) -> str:
    if not asserts:
        return DECLINED
    return SUPPORTED if supported else UNSUPPORTED


def _extract_json(raw: str) -> str:
    text = (raw or "").strip()
    # Models wrap JSON in fences regardless of instructions.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        return fenced.group(1)
    brace = re.search(r"\{.*\}", text, re.S)
    return brace.group(0) if brace else text


def _parse(raw: str) -> Verdict:
    try:
        payload = json.loads(_extract_json(raw))
        asserts = bool(payload["asserts_answer"])
        supported = bool(payload.get("supported", False))
        reason = str(payload.get("reason", "")).strip()
    except Exception:
        return Verdict(
            verdict=UNPARSEABLE,
            reason="judge returned no usable JSON",
            raw=raw,
        )
    return Verdict(
        verdict=verdict_from_axes(asserts, supported),
        reason=reason,
        asserts_answer=asserts,
        supported=supported,
        raw=raw,
    )


async def judge_answer(
    *, question: str, context: str, answer: str, llm: LLMProvider
) -> Verdict:
    """Grade one answer. Deterministic shortcut first, model second."""
    shortcut = canned_refusal(answer)
    if shortcut is not None:
        return shortcut

    # complete(), not stream(): a verdict is one short JSON object, and
    # nobody is watching it arrive token by token.
    raw = await llm.complete(
        system=SYSTEM,
        user=USER.format(question=question, context=context, answer=answer),
        max_tokens=200,
        temperature=0.0,
    )
    return _parse(raw)
