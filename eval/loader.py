"""Load and validate the golden set.

Validation is the point of this module, not parsing. A golden set is
only worth what its labels are worth, and the ways it silently rots are
boring: a question referencing a document that was renamed, a duplicate
id quietly overwriting an earlier case, an `expect_phrases` that does
not actually occur in the corpus (so the case can never pass however
good the system gets), an `expect_pages` pointing at a page where the
answer is not.

None of those are caught by running the eval -- they make it report a
number that looks fine. So they are errors here, loudly, before any
scoring code sees the data.

PDF documents are read through the REAL pipeline (extract -> clean), not
a text sidecar. That is deliberate: the page numbers a citation must
resolve to are whatever PyMuPDF produces, and the text a phrase must be
found in is whatever survives header/footer stripping. Validating
against anything else would validate a fiction.
"""
import os
from dataclasses import dataclass
from typing import Optional

import yaml

from app.services.cleaning import clean_pages
from app.services.extraction import extract_pages

EVAL_ROOT = os.path.dirname(os.path.abspath(__file__))
GOLDEN_DIR = os.path.join(EVAL_ROOT, "golden")
CORPUS_DIR = os.path.join(GOLDEN_DIR, "corpus")
QUESTIONS_PATH = os.path.join(GOLDEN_DIR, "questions.yaml")

# Types that count toward the confidence gate's precision/recall.
GATE_SCORED = {
    "answerable_direct",
    "answerable_needs_rerank",
    # Added alongside the original categories rather than replacing
    # them. These four name the RETRIEVAL shape a case exercises --
    # a distinctive token, a table row, a code fence, support that
    # straddles a child boundary -- which `answerable_direct` hid.
    # Without its own bucket a per-type table cannot show that, say,
    # every table case fails while the prose cases pass.
    "exact_term",
    "table",
    "code",
    "spans_two_chunks",
    "synthesis",
    "unanswerable_near",
    "unanswerable_off_topic",
    "cross_org_isolation",
    "follow_up",
}

# Excluded from the gate score and reported on its own. A case whose
# correct answer is genuinely arguable cannot discipline a threshold:
# whichever way the gate decides, the "error" is in the label. Keeping
# them in the set is still worth it -- they are exactly the cases a
# future reader should re-litigate -- but averaging them into a
# precision figure launders a judgement call into a number.
GATE_EXCLUDED = {"ambiguous"}

CATEGORIES = GATE_SCORED | GATE_EXCLUDED

KNOWN_FIELDS = {
    "id",
    "org",
    "question",
    "should_answer",
    "category",
    "why",
    "expect_documents",
    "expect_pages",
    "expect_phrases",
    "forbid_phrases",
    "rubric",
    "ambiguity",
    "history",
}

TEXT_SUFFIXES = (".md", ".txt")
PDF_SUFFIXES = (".pdf",)


class GoldenSetError(Exception):
    """The golden set is internally inconsistent. Never a warning."""


@dataclass(frozen=True)
class GoldenPage:
    page_number: int
    text: str


@dataclass(frozen=True)
class GoldenDocument:
    org: str
    filename: str
    path: str
    pages: tuple[GoldenPage, ...]

    @property
    def content(self) -> str:
        return "\n\n".join(page.text for page in self.pages)

    @property
    def is_pdf(self) -> bool:
        return self.filename.lower().endswith(PDF_SUFFIXES)

    def page(self, number: int) -> Optional[GoldenPage]:
        for page in self.pages:
            if page.page_number == number:
                return page
        return None


@dataclass(frozen=True)
class GoldenQuestion:
    id: str
    org: str
    question: str
    should_answer: bool
    category: str
    why: str
    expect_documents: tuple[str, ...] = ()
    expect_phrases: tuple[str, ...] = ()
    forbid_phrases: tuple[str, ...] = ()
    # Pages a citation must resolve to. Only meaningful for PDFs, where
    # a wrong page is a wrong citation even when the text is right.
    expect_pages: tuple[int, ...] = ()
    # Structured check for synthesis cases, where a bare forbidden
    # substring is too blunt (see eval/scoring.py).
    rubric: Optional[dict] = None
    history: tuple[dict, ...] = ()

    @property
    def counts_toward_gate(self) -> bool:
        return self.category in GATE_SCORED

    @property
    def is_isolation_case(self) -> bool:
        return self.category == "cross_org_isolation"


@dataclass(frozen=True)
class GoldenSet:
    documents: tuple[GoldenDocument, ...]
    questions: tuple[GoldenQuestion, ...]

    def orgs(self) -> tuple[str, ...]:
        return tuple(sorted({d.org for d in self.documents}))

    def documents_for(self, org: str) -> tuple[GoldenDocument, ...]:
        return tuple(d for d in self.documents if d.org == org)

    def document(self, org: str, filename: str) -> GoldenDocument:
        for d in self.documents:
            if d.org == org and d.filename == filename:
                return d
        raise KeyError(f"{org}/{filename}")

    def questions_for(self, org: str) -> tuple[GoldenQuestion, ...]:
        return tuple(q for q in self.questions if q.org == org)

    def by_category(self, category: str) -> tuple[GoldenQuestion, ...]:
        return tuple(q for q in self.questions if q.category == category)

    def gate_scored(self) -> tuple[GoldenQuestion, ...]:
        return tuple(q for q in self.questions if q.counts_toward_gate)

    def get(self, question_id: str) -> GoldenQuestion:
        for q in self.questions:
            if q.id == question_id:
                return q
        raise KeyError(question_id)


def _load_document(org: str, path: str, filename: str) -> GoldenDocument:
    if filename.lower().endswith(PDF_SUFFIXES):
        with open(path, "rb") as handle:
            data = handle.read()
        # The real pipeline, so expect_pages is validated against the
        # page numbers a citation will actually carry, and expect_phrases
        # against the text that survives header/footer stripping.
        pages = clean_pages(extract_pages(data, filename))
    else:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        # Markdown and text have no page concept; extraction assigns a
        # single synthetic page 1 and so do we, rather than inventing
        # page breaks the format does not have.
        pages = [type("P", (), {"page_number": 1, "text": text})()]

    return GoldenDocument(
        org=org,
        filename=filename,
        path=path,
        pages=tuple(GoldenPage(p.page_number, p.text) for p in pages),
    )


def load_corpus(corpus_dir: str = CORPUS_DIR) -> tuple[GoldenDocument, ...]:
    if not os.path.isdir(corpus_dir):
        raise GoldenSetError(f"corpus directory missing: {corpus_dir}")

    documents: list[GoldenDocument] = []
    for org in sorted(os.listdir(corpus_dir)):
        org_dir = os.path.join(corpus_dir, org)
        if not os.path.isdir(org_dir):
            continue
        for filename in sorted(os.listdir(org_dir)):
            if not filename.endswith(TEXT_SUFFIXES + PDF_SUFFIXES):
                continue
            documents.append(
                _load_document(org, os.path.join(org_dir, filename), filename)
            )
    if not documents:
        raise GoldenSetError(
            f"no corpus documents found under {corpus_dir}. "
            f"PDFs are generated: run `python -m eval.build_corpus`"
        )
    return tuple(documents)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise GoldenSetError(message)


def load_golden_set(
    questions_path: str = QUESTIONS_PATH, corpus_dir: str = CORPUS_DIR
) -> GoldenSet:
    documents = load_corpus(corpus_dir)
    with open(questions_path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)

    _require(isinstance(raw, dict) and "questions" in raw,
             f"{questions_path} must be a mapping with a 'questions' key")

    by_org: dict[str, dict[str, GoldenDocument]] = {}
    for document in documents:
        by_org.setdefault(document.org, {})[document.filename] = document

    questions: list[GoldenQuestion] = []
    seen_ids: set[str] = set()

    for index, entry in enumerate(raw["questions"]):
        where = (entry or {}).get("id") or f"entry #{index + 1}"
        _require(isinstance(entry, dict), f"{where}: not a mapping")

        for required in ("id", "org", "question", "should_answer", "category", "why"):
            _require(required in entry, f"{where}: missing required field '{required}'")

        question_id = entry["id"]

        # Unknown fields are an ERROR, not ignored. A typo'd key is the
        # worst kind of golden-set rot: `expect_page: [2]` (singular)
        # parses fine, validates fine, and silently disables the page
        # check -- so the case reports a pass it never earned.
        unknown_fields = set(entry) - KNOWN_FIELDS
        _require(
            not unknown_fields,
            f"{question_id}: unknown field(s) {sorted(unknown_fields)}; "
            f"known: {sorted(KNOWN_FIELDS)}",
        )
        _require(question_id not in seen_ids,
                 f"duplicate question id '{question_id}' -- ids are referenced by "
                 f"result files, so a duplicate silently overwrites a case")
        seen_ids.add(question_id)

        org = entry["org"]
        _require(org in by_org,
                 f"{question_id}: org '{org}' has no corpus under {corpus_dir}")

        category = entry["category"]
        _require(category in CATEGORIES,
                 f"{question_id}: unknown category '{category}'; "
                 f"known: {sorted(CATEGORIES)}")

        should_answer = entry["should_answer"]
        _require(isinstance(should_answer, bool),
                 f"{question_id}: should_answer must be a bool, got "
                 f"{type(should_answer).__name__} -- YAML turns a bare `yes` into "
                 f"True but a quoted \"yes\" into a string")

        expect_documents = tuple(entry.get("expect_documents") or ())
        for filename in expect_documents:
            _require(
                filename in by_org[org],
                f"{question_id}: expect_documents references '{filename}', which "
                f"is not in org '{org}' ({sorted(by_org[org])})",
            )

        expect_phrases = tuple(str(p).lower() for p in (entry.get("expect_phrases") or ()))
        forbid_phrases = tuple(str(p).lower() for p in (entry.get("forbid_phrases") or ()))
        expect_pages = tuple(int(p) for p in (entry.get("expect_pages") or ()))
        rubric = entry.get("rubric")

        if category == "ambiguous":
            _require(
                "ambiguity" in entry and str(entry["ambiguity"]).strip(),
                f"{question_id}: an ambiguous case must state the 'ambiguity' -- "
                f"what exactly the corpus leaves open. Without it the category is "
                f"a place to hide cases that merely fail.",
            )

        if should_answer:
            _require(expect_documents,
                     f"{question_id}: should_answer is true but no expect_documents, "
                     f"so retrieval recall is unmeasurable for it")
            _require(expect_phrases or rubric,
                     f"{question_id}: should_answer is true but has neither "
                     f"expect_phrases nor a rubric, so 'answered' could not be "
                     f"distinguished from 'answered wrongly'")

            corpus_text = " ".join(
                by_org[org][name].content.lower() for name in expect_documents
            )
            for phrase in expect_phrases:
                _require(
                    phrase in corpus_text,
                    f"{question_id}: expect_phrases contains '{phrase}', which does "
                    f"not occur in {list(expect_documents)} -- a grounded answer "
                    f"could never contain it",
                )
        else:
            _require(not expect_phrases,
                     f"{question_id}: should_answer is false but expect_phrases is "
                     f"set; a refusal cannot be required to contain content words")

        # expect_pages must name real pages, and the support must really
        # be on them -- otherwise a correct citation would score as wrong.
        if expect_pages:
            _require(expect_documents,
                     f"{question_id}: expect_pages without expect_documents is "
                     f"meaningless -- a page number belongs to a document")
            for page_number in expect_pages:
                found_on = []
                for filename in expect_documents:
                    document = by_org[org][filename]
                    page = document.page(page_number)
                    if page is None:
                        continue
                    found_on.append(filename)
                    if expect_phrases:
                        page_text = page.text.lower()
                        _require(
                            any(phrase in page_text for phrase in expect_phrases),
                            f"{question_id}: expect_pages says page {page_number} of "
                            f"'{filename}', but none of {list(expect_phrases)} appears "
                            f"there -- a correct citation would be scored wrong",
                        )
                _require(
                    found_on,
                    f"{question_id}: page {page_number} does not exist in any of "
                    f"{list(expect_documents)}",
                )

        if rubric is not None:
            _require(isinstance(rubric, dict), f"{question_id}: rubric must be a mapping")
            unknown = set(rubric) - {"must_mention", "must_not_assert"}
            _require(not unknown,
                     f"{question_id}: unknown rubric keys {sorted(unknown)}")
            _require(rubric.get("must_mention"),
                     f"{question_id}: a rubric needs must_mention, otherwise it "
                     f"only says what the answer must avoid")

        history = tuple(entry.get("history") or ())
        for turn in history:
            _require(
                isinstance(turn, dict) and "role" in turn and "content" in turn,
                f"{question_id}: each history turn needs 'role' and 'content'",
            )
            _require(
                turn["role"] in ("user", "assistant"),
                f"{question_id}: history role must be user or assistant",
            )
        if category == "follow_up":
            _require(history,
                     f"{question_id}: category follow_up requires history, otherwise "
                     f"there is nothing for the rewriter to resolve against")

        questions.append(
            GoldenQuestion(
                id=question_id,
                org=org,
                question=entry["question"],
                should_answer=should_answer,
                category=category,
                why=entry["why"],
                expect_documents=expect_documents,
                expect_phrases=expect_phrases,
                forbid_phrases=forbid_phrases,
                expect_pages=expect_pages,
                rubric=rubric,
                history=history,
            )
        )

    # ---- set-level rules -------------------------------------------
    #
    # These three cannot live in the loop above: each is a property of
    # the SET, not of one entry. They were previously asserted only in
    # tests/test_golden_set.py, which catches them in CI but lets every
    # interactive `load_golden_set()` -- the eval harness included --
    # run happily on a set that has quietly rotted.

    isolation = [q for q in questions if q.category == "cross_org_isolation"]
    for case in isolation:
        _require(
            case.forbid_phrases,
            f"{case.id}: an isolation probe needs forbid_phrases -- without "
            f"them a cross-tenant answer is indistinguishable from any other "
            f"answer and the leak is undetectable",
        )
        twins = [
            q for q in questions
            if q.question == case.question and q.org != case.org
        ]
        _require(
            twins,
            f"{case.id}: no other org asks this exact question, so the probe "
            f"has no twin. 'Refuse everything for this org' would score a "
            f"perfect isolation result",
        )
        _require(
            any(t.should_answer for t in twins),
            f"{case.id}: another org asks this question but none ANSWERS it, "
            f"so refusing it proves nothing about the org filter",
        )

    near = sum(1 for q in questions if q.category == "unanswerable_near")
    off_topic = sum(1 for q in questions if q.category == "unanswerable_off_topic")
    if off_topic:
        # An off-topic question is refused by any threshold at all, so a
        # set weighted towards them certifies a gate that is broken.
        _require(
            near >= 2 * off_topic,
            f"near-miss unanswerables ({near}) must be at least twice the "
            f"off-topic ones ({off_topic}) -- off-topic cases are refused by "
            f"any threshold, so they cannot discipline one",
        )

    return GoldenSet(documents=documents, questions=tuple(questions))


def summarise(golden: GoldenSet) -> str:
    lines: list[str] = []

    lines.append("corpus")
    for org in golden.orgs():
        docs = golden.documents_for(org)
        for document in docs:
            kind = "pdf" if document.is_pdf else "text"
            lines.append(
                f"  {org:8} {document.filename:28} {kind:5} "
                f"{len(document.pages)} page(s)"
            )

    lines.append("")
    lines.append(f"questions: {len(golden.questions)}")

    lines.append("")
    lines.append("per type")
    for category in sorted(CATEGORIES):
        cases = golden.by_category(category)
        if not cases:
            continue
        answerable = sum(1 for q in cases if q.should_answer)
        scored = "scored" if category in GATE_SCORED else "EXCLUDED from gate score"
        lines.append(
            f"  {category:24} {len(cases):3}  "
            f"(answer={answerable}, refuse={len(cases) - answerable})  {scored}"
        )

    lines.append("")
    lines.append("per org")
    for org in golden.orgs():
        cases = golden.questions_for(org)
        answerable = sum(1 for q in cases if q.should_answer)
        lines.append(
            f"  {org:24} {len(cases):3}  "
            f"(answer={answerable}, refuse={len(cases) - answerable})"
        )

    lines.append("")
    lines.append("per org x type")
    for org in golden.orgs():
        for category in sorted(CATEGORIES):
            count = sum(
                1 for q in golden.questions if q.org == org and q.category == category
            )
            if count:
                lines.append(f"  {org:10} {category:24} {count:3}")

    scored = golden.gate_scored()
    answerable = sum(1 for q in scored if q.should_answer)
    lines.append("")
    lines.append(
        f"gate-scored: {len(scored)} "
        f"(answer={answerable}, refuse={len(scored) - answerable}); "
        f"excluded: {len(golden.questions) - len(scored)}"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    print(summarise(load_golden_set()))
