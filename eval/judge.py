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

Four guards keep it honest:

  * It never sees the golden label. If it were told the expected
    verdict it would agree with it.
  * Canned refusals are matched BEFORE any model call. The system's own
    refusal strings are fixed text; spending a model call to recognise
    them adds cost and a chance of being wrong about something certain.
  * Verdicts are reported per case with the answer text, never only as
    a rate, so a disagreement can be read and overruled. It was.
  * The agreement rate against hand grades is recorded (`agreement`
    below). A judge whose reliability is unmeasured is an opinion, and
    the skill's threshold is explicit: below 80% agreement, fix the
    prompt before trusting any number downstream of it.

## Determinism, retries and the cache

Temperature 0 (`JUDGE_TEMPERATURE`), not the answerer's 0.1: a verdict
is a classification, and sampling a grade is how one answer gets two
labels in two runs.

Invalid JSON gets ONE retry (`JUDGE_MAX_RETRIES`) with the parse
failure fed back, because the common failure is a model writing prose
around the object rather than refusing the format outright. The retry
prompt is a correction, not a repeat -- re-sending the identical prompt
at temperature 0 would, by construction, tend to produce the identical
unparseable answer.

Verdicts are cached on a sha256 of (judge model, system prompt,
question, context, answer). Not on the case id: the case id is stable
while the ANSWER changes between runs, so an id-keyed cache would serve
a verdict about text the model no longer produces -- the worst kind of
stale, since it looks like a measurement. The cache is a plain JSON
file under eval/.judge-cache/ and is safe to delete.
"""
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

from app.core.config import settings
from app.services.llm import LLMProvider
from app.services.prompting import INSUFFICIENT_CONTEXT_ANSWER
from app.services.reranker import NOT_FOUND_ANSWER

DECLINED = "DECLINED"
SUPPORTED = "SUPPORTED"
UNSUPPORTED = "UNSUPPORTED"
UNPARSEABLE = "UNPARSEABLE"

VERDICTS = (DECLINED, SUPPORTED, UNSUPPORTED)

EVAL_ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(EVAL_ROOT, ".judge-cache")

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

# Appended on a retry. A correction, not a repeat: at temperature 0 the
# same prompt tends to produce the same unparseable reply, so the retry
# has to change the input to change the output.
RETRY_SUFFIX = """

Your previous reply could not be parsed as JSON. It was:
{previous}

Reply again with ONLY the JSON object, no prose, no code fence, no
explanation before or after it.
"""


@dataclass
class Verdict:
    verdict: str
    reason: str
    # True when decided without a model call.
    deterministic: bool = False
    asserts_answer: Optional[bool] = None
    supported: Optional[bool] = None
    raw: Optional[str] = None
    # How many model calls this verdict cost. 0 for a canned match or a
    # cache hit, 2 when the first reply needed a retry. Summed by the
    # harness so a run can report its API spend honestly.
    model_calls: int = 0
    cached: bool = False
    # Set when a hand grade exists for this case; see `agreement`.
    hand_verdict: Optional[str] = None

    @property
    def declined(self) -> bool:
        return self.verdict == DECLINED


CANNED = (NOT_FOUND_ANSWER, INSUFFICIENT_CONTEXT_ANSWER)


def canned_refusal(answer: str) -> Optional[Verdict]:
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


# ----------------------------------------------------------------- cache --


def cache_key(*, question: str, context: str, answer: str, model: str) -> str:
    """sha256 over everything that can change the verdict.

    The SYSTEM prompt is included: editing the grading rules must
    invalidate every stored verdict, and a cache that survives a rubric
    change would report the old rubric's grades as the new one's.
    """
    digest = hashlib.sha256()
    for part in (model, SYSTEM, question, context, answer):
        digest.update((part or "").encode("utf-8"))
        # A separator, so ("ab", "c") and ("a", "bc") are different
        # keys. Without it two different (context, answer) splits of the
        # same characters collide and one case is served the other's
        # verdict.
        digest.update(b"\x00")
    return digest.hexdigest()


class JudgeCache:
    """A tiny JSON-file cache. Deliberately not Redis.

    The eval is a batch script run by hand on one machine; a Redis
    dependency would mean the eval cannot run when Redis is down, which
    trades a real capability for a cache. Corrupt or unreadable files
    are treated as a miss rather than an error: losing a cached verdict
    costs one API call, and crashing the eval costs the whole run.
    """

    def __init__(self, directory: str = CACHE_DIR, enabled: Optional[bool] = None):
        self.directory = directory
        self.enabled = settings.JUDGE_CACHE_ENABLED if enabled is None else enabled
        self.hits = 0
        self.misses = 0

    def _path(self, key: str) -> str:
        return os.path.join(self.directory, f"{key}.json")

    def get(self, key: str) -> Optional[Verdict]:
        if not self.enabled:
            return None
        try:
            with open(self._path(key), encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            self.misses += 1
            return None
        self.hits += 1
        payload.pop("model_calls", None)
        payload.pop("cached", None)
        return Verdict(**payload, model_calls=0, cached=True)

    def put(self, key: str, verdict: Verdict) -> None:
        # An UNPARSEABLE verdict is never cached. It is a transport
        # failure, not a judgement, and caching it would make one bad
        # reply permanent for that answer.
        if not self.enabled or verdict.verdict == UNPARSEABLE:
            return
        try:
            os.makedirs(self.directory, exist_ok=True)
            with open(self._path(key), "w", encoding="utf-8") as handle:
                json.dump(asdict(verdict), handle, indent=2)
        except Exception:
            # A cache that cannot write must not fail the run.
            pass


# ------------------------------------------------------------- judging --


async def judge_answer(
    *,
    question: str,
    context: str,
    answer: str,
    llm: LLMProvider,
    cache: Optional[JudgeCache] = None,
    max_retries: Optional[int] = None,
    model_name: Optional[str] = None,
) -> Verdict:
    """Grade one answer. Canned match, then cache, then the model."""
    shortcut = canned_refusal(answer)
    if shortcut is not None:
        return shortcut

    model = model_name or settings.JUDGE_MODEL
    key = cache_key(question=question, context=context, answer=answer, model=model)
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            return hit

    retries = settings.JUDGE_MAX_RETRIES if max_retries is None else max_retries
    user = USER.format(question=question, context=context, answer=answer)

    calls = 0
    verdict = Verdict(verdict=UNPARSEABLE, reason="judge was never called")
    for attempt in range(retries + 1):
        # complete(), not stream(): a verdict is one short JSON object,
        # and nobody is watching it arrive token by token.
        raw = await llm.complete(
            system=SYSTEM,
            user=user,
            max_tokens=settings.JUDGE_MAX_TOKENS,
            temperature=settings.JUDGE_TEMPERATURE,
        )
        calls += 1
        verdict = _parse(raw)
        if verdict.verdict != UNPARSEABLE:
            break
        if attempt < retries:
            user = USER.format(
                question=question, context=context, answer=answer
            ) + RETRY_SUFFIX.format(previous=(raw or "")[:400])

    verdict.model_calls = calls
    if cache is not None:
        cache.put(key, verdict)
    return verdict


# ---------------------------------------------------- judge reliability --


@dataclass
class Agreement:
    """How often the judge matched a HAND grade.

    The skill's rule, and the reason this dataclass exists rather than a
    bare float: at least 10 hand grades including both passes and
    failures, and below 80% agreement the judge prompt is fixed before
    any number downstream of it is believed.

    Hand grades live in eval/golden/judge_grades.yaml -- a file a human
    edits -- and are matched to cases by id. They are NOT generated, for
    the same reason the golden labels are not: a judge checked against
    model-written grades is checked against itself.
    """

    n: int = 0
    agreed: int = 0
    disagreements: list[dict] = field(default_factory=list)
    # Hand grades that exist for ids the run did not produce. Reported,
    # because the usual cause is a renamed case id, and silently
    # dropping those would quietly shrink the denominator.
    unmatched_grades: list[str] = field(default_factory=list)
    # Which HAND verdicts were covered, for `covers_both_outcomes`.
    hand_outcomes: set = field(default_factory=set)

    @property
    def rate(self) -> Optional[float]:
        return self.agreed / self.n if self.n else None

    @property
    def meets_bar(self) -> Optional[bool]:
        """None when there is not enough evidence to say.

        Explicitly not False: "we have 3 grades" and "the judge is
        unreliable" are different findings, and reporting the first as
        the second sends someone to rewrite a prompt that may be fine.
        """
        if self.n < 10:
            return None
        return self.rate >= 0.80

    @property
    def covers_both_outcomes(self) -> bool:
        """Were refusals AND assertions both hand-graded?

        A judge checked only on cases that DECLINED is checked on half
        the job. The refusal measurement turns on telling a decline
        from an unsupported assertion, so agreement measured on one
        side of that line says nothing about the other.
        """
        return len({DECLINED} & self.hand_outcomes) > 0 and bool(
            self.hand_outcomes - {DECLINED}
        )


def score_agreement(judged: dict, hand: dict) -> Agreement:
    """Compare {case_id: judge verdict} against {case_id: hand verdict}.

    Case ids present in `hand` but missing from `judged` are reported
    rather than ignored -- that is what a renamed case looks like.
    """
    result = Agreement()
    for case_id, hand_verdict in sorted(hand.items()):
        judge_verdict = judged.get(case_id)
        if judge_verdict is None:
            result.unmatched_grades.append(case_id)
            continue
        result.n += 1
        result.hand_outcomes.add(hand_verdict)
        if judge_verdict == hand_verdict:
            result.agreed += 1
        else:
            result.disagreements.append(
                {"id": case_id, "judge": judge_verdict, "hand": hand_verdict}
            )
    return result


HAND_GRADES_PATH = os.path.join(EVAL_ROOT, "golden", "judge_grades.yaml")


def load_hand_grades(path: str = HAND_GRADES_PATH) -> dict:
    """Hand grades, or {} when none have been written yet.

    A missing file is not an error: the first run has nothing to compare
    against, and the harness reports "agreement UNMEASURED" rather than
    refusing to run. It is an error for the file to exist and contain a
    verdict this module does not know, because that is a typo that would
    otherwise count as a disagreement.
    """
    if not os.path.exists(path):
        return {}
    import yaml

    with open(path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    grades = raw.get("grades") or {}
    bad = {k: v for k, v in grades.items() if v not in VERDICTS}
    if bad:
        raise ValueError(
            f"{path}: unknown verdict(s) {bad}; known: {list(VERDICTS)}. A typo "
            f"here would be counted as the judge disagreeing with a human."
        )
    return dict(grades)
