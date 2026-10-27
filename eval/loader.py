"""Load and validate the golden set.

Validation is the point of this module, not parsing. A golden set is
only worth what its labels are worth, and the ways it silently rots are
boring: a question referencing a document that was renamed, a duplicate
id quietly overwriting an earlier case, an `expect_phrases` that does
not actually occur in the corpus (so the case can never pass however
good the system gets), or a `should_answer: false` carrying
expect_phrases (which asks for a refusal to contain words).

None of those are caught by running the eval -- they make it report a
number that looks fine. So they are errors here, loudly, before any
scoring code sees the data.
"""
import os
from dataclasses import dataclass, field
from typing import Optional

import yaml

EVAL_ROOT = os.path.dirname(os.path.abspath(__file__))
GOLDEN_DIR = os.path.join(EVAL_ROOT, "golden")
CORPUS_DIR = os.path.join(GOLDEN_DIR, "corpus")
QUESTIONS_PATH = os.path.join(GOLDEN_DIR, "questions.yaml")

CATEGORIES = {
    "answerable_direct",
    "answerable_needs_rerank",
    "answerable_multi_fact",
    "unanswerable_near",
    "unanswerable_off_topic",
    "cross_org_isolation",
    "follow_up",
}


class GoldenSetError(Exception):
    """The golden set is internally inconsistent. Never a warning."""


@dataclass(frozen=True)
class GoldenDocument:
    org: str
    filename: str
    path: str
    content: str


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
    # Prior turns, for the follow-up cases. Each is {role, content} and
    # optionally {kind}, matching what history_for_rewrite consumes.
    history: tuple[dict, ...] = ()

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

    def questions_for(self, org: str) -> tuple[GoldenQuestion, ...]:
        return tuple(q for q in self.questions if q.org == org)

    def by_category(self, category: str) -> tuple[GoldenQuestion, ...]:
        return tuple(q for q in self.questions if q.category == category)

    def get(self, question_id: str) -> GoldenQuestion:
        for q in self.questions:
            if q.id == question_id:
                return q
        raise KeyError(question_id)


def load_corpus(corpus_dir: str = CORPUS_DIR) -> tuple[GoldenDocument, ...]:
    if not os.path.isdir(corpus_dir):
        raise GoldenSetError(f"corpus directory missing: {corpus_dir}")

    documents: list[GoldenDocument] = []
    for org in sorted(os.listdir(corpus_dir)):
        org_dir = os.path.join(corpus_dir, org)
        if not os.path.isdir(org_dir):
            continue
        for filename in sorted(os.listdir(org_dir)):
            if not filename.endswith((".md", ".txt")):
                continue
            path = os.path.join(org_dir, filename)
            with open(path, encoding="utf-8") as handle:
                documents.append(
                    GoldenDocument(
                        org=org,
                        filename=filename,
                        path=path,
                        content=handle.read(),
                    )
                )
    if not documents:
        raise GoldenSetError(f"no corpus documents found under {corpus_dir}")
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

    by_org_files = {}
    for document in documents:
        by_org_files.setdefault(document.org, {})[document.filename] = document

    questions: list[GoldenQuestion] = []
    seen_ids: set[str] = set()

    for index, entry in enumerate(raw["questions"]):
        where = entry.get("id") or f"entry #{index + 1}"
        _require(isinstance(entry, dict), f"{where}: not a mapping")

        for required in ("id", "org", "question", "should_answer", "category", "why"):
            _require(required in entry, f"{where}: missing required field '{required}'")

        question_id = entry["id"]
        _require(question_id not in seen_ids,
                 f"duplicate question id '{question_id}' -- ids are referenced by "
                 f"result files, so a duplicate silently overwrites a case")
        seen_ids.add(question_id)

        org = entry["org"]
        _require(org in by_org_files,
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
            # The rot this catches: a document gets renamed and every
            # case referencing it starts scoring zero recall against a
            # file that does not exist.
            _require(
                filename in by_org_files[org],
                f"{question_id}: expect_documents references '{filename}', which "
                f"is not in org '{org}' ({sorted(by_org_files[org])})",
            )

        expect_phrases = tuple(str(p).lower() for p in (entry.get("expect_phrases") or ()))
        forbid_phrases = tuple(str(p).lower() for p in (entry.get("forbid_phrases") or ()))

        if should_answer:
            _require(expect_documents,
                     f"{question_id}: should_answer is true but no expect_documents, "
                     f"so retrieval recall is unmeasurable for it")
            _require(expect_phrases,
                     f"{question_id}: should_answer is true but no expect_phrases, "
                     f"so 'answered' could not be distinguished from 'answered wrongly'")
            # A phrase that occurs nowhere can never be produced from
            # grounded context, so the case would be permanently
            # unpassable -- and would look like a model failure.
            corpus_text = " ".join(
                by_org_files[org][name].content.lower() for name in expect_documents
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
                history=history,
            )
        )

    return GoldenSet(documents=documents, questions=tuple(questions))


def summarise(golden: GoldenSet) -> str:
    lines = [
        f"{len(golden.documents)} documents across {len(golden.orgs())} orgs: "
        f"{', '.join(golden.orgs())}",
        f"{len(golden.questions)} questions",
    ]
    answerable = sum(1 for q in golden.questions if q.should_answer)
    lines.append(
        f"  should_answer=true : {answerable}"
        f"   should_answer=false: {len(golden.questions) - answerable}"
    )
    for category in sorted(CATEGORIES):
        count = len(golden.by_category(category))
        if count:
            lines.append(f"  {category:24} {count}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(summarise(load_golden_set()))
