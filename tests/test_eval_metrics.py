"""Retrieval metrics on a fixture small enough to compute by hand.

Every expected value below is worked out in a comment from the ranked
list beside it. That is the point of the file: a metric implementation
tested against its own output is tested against nothing, and these
numbers are the ones a threshold decision and a release note will
quote.

The distinctions being pinned, each of which was a live choice in
`eval/metrics.py`:

  * None (no support labelled) is not 0.0 (support labelled, never
    found). Conflating them makes a metric that falls when you add
    unanswerable cases to the golden set.
  * recall is a FRACTION of labelled units, so a synthesis case
    needing two pages cannot score a full hit on one of them.
  * a page label narrows a document label and never stands alone.
  * citation correctness is not retrieval correctness.
"""
import pytest

from eval.metrics import (
    CaseRetrieval,
    RankedItem,
    RetrievalTarget,
    citation_pages_correct,
    first_relevant_rank,
    is_relevant,
    mean_reciprocal_rank,
    recall_at_k,
    reciprocal_rank,
    summarise_retrieval,
)

TRAVEL = "travel-policy.pdf"
ENG = "engineering-handbook.pdf"
VSR = "vendor-security-review.pdf"


def _ranked(*pairs):
    return [
        RankedItem(document=doc, page=page, chunk_id=f"c{i}")
        for i, (doc, page) in enumerate(pairs)
    ]


# A fixed ranked list used by most tests. Ranks are 1-based:
#   1 travel p3   2 eng p1   3 travel p2   4 travel p3   5 eng p4
RANKED = _ranked(
    (TRAVEL, 3),
    (ENG, 1),
    (TRAVEL, 2),
    (TRAVEL, 3),
    (ENG, 4),
)


# ------------------------------------------------------------- relevance --


def test_a_page_label_narrows_the_document_label():
    target = RetrievalTarget(documents=(TRAVEL,), pages=(2,))
    assert is_relevant(RankedItem(TRAVEL, 2), target) is True
    # Right document, wrong page.
    assert is_relevant(RankedItem(TRAVEL, 3), target) is False
    # Right page number, wrong document. Without the document check
    # this would match, and "page 2" in a label that sits next to a
    # filename does not mean page 2 of anything in the tenant.
    assert is_relevant(RankedItem(ENG, 2), target) is False


def test_no_page_label_means_any_page_of_that_document():
    # Correct for .md and .txt corpora: those have no page concept and
    # are ingested as one synthetic page, so pinning a page would be
    # pinning an implementation detail.
    target = RetrievalTarget(documents=(TRAVEL,))
    assert is_relevant(RankedItem(TRAVEL, 1), target) is True
    assert is_relevant(RankedItem(TRAVEL, 4), target) is True
    assert is_relevant(RankedItem(ENG, 1), target) is False


# -------------------------------------------------------------- recall@k --


def test_recall_at_k_finds_the_single_labelled_page():
    # travel p3 is at rank 1, so every k >= 1 finds it.
    target = RetrievalTarget(documents=(TRAVEL,), pages=(3,))
    assert recall_at_k(RANKED, target, k=1) == 1.0
    assert recall_at_k(RANKED, target, k=5) == 1.0


def test_recall_at_k_respects_the_cutoff():
    # travel p2 is at rank 3. k=2 must not see it.
    target = RetrievalTarget(documents=(TRAVEL,), pages=(2,))
    assert recall_at_k(RANKED, target, k=2) == 0.0
    assert recall_at_k(RANKED, target, k=3) == 1.0


def test_recall_is_a_fraction_of_labelled_units():
    # Two labelled units: (travel,2) at rank 3 and (travel,3) at rank 1.
    # k=1 sees only (travel,3) -> 1 of 2 -> 0.5. A bool metric would
    # call this a hit and hide the missing half of a synthesis answer.
    target = RetrievalTarget(documents=(TRAVEL,), pages=(2, 3))
    assert recall_at_k(RANKED, target, k=1) == 0.5
    assert recall_at_k(RANKED, target, k=3) == 1.0


def test_duplicate_hits_on_one_unit_do_not_inflate_recall():
    # travel p3 appears at BOTH rank 1 and rank 4. Counting occurrences
    # instead of distinct units would give 2/1 = 2.0 recall.
    target = RetrievalTarget(documents=(TRAVEL,), pages=(3,))
    assert recall_at_k(RANKED, target, k=5) == 1.0


def test_recall_for_a_multi_document_label_without_pages():
    # Units are (travel, None) and (vsr, None). travel is at rank 1;
    # vsr never appears -> 1 of 2.
    target = RetrievalTarget(documents=(TRAVEL, VSR))
    assert recall_at_k(RANKED, target, k=5) == 0.5


def test_an_unanswerable_case_has_no_recall_rather_than_zero():
    # This is the distinction the whole module turns on. An off-topic
    # question labels no support; 0.0 would say "retrieval failed".
    assert recall_at_k(RANKED, RetrievalTarget(), k=5) is None


def test_labelled_support_that_is_never_retrieved_is_a_real_zero():
    target = RetrievalTarget(documents=(VSR,), pages=(2,))
    assert recall_at_k(RANKED, target, k=5) == 0.0


def test_k_of_zero_is_zero_not_a_crash():
    # Reachable through a misconfigured RERANK_TOP_N. Returning 0.0 is
    # honest -- nothing was sent to the model -- and a slice bug here
    # would have read as a retrieval regression.
    target = RetrievalTarget(documents=(TRAVEL,), pages=(3,))
    assert recall_at_k(RANKED, target, k=0) == 0.0


# ------------------------------------------------------------------- MRR --


def test_first_relevant_rank_is_one_based():
    # A 0-based rank makes the reciprocal 1/0. The first item being
    # relevant must give rank 1 and RR 1.0.
    target = RetrievalTarget(documents=(TRAVEL,), pages=(3,))
    assert first_relevant_rank(RANKED, target) == 1
    assert reciprocal_rank(RANKED, target) == 1.0


def test_reciprocal_rank_of_a_rank_three_hit():
    target = RetrievalTarget(documents=(TRAVEL,), pages=(2,))
    assert first_relevant_rank(RANKED, target) == 3
    assert reciprocal_rank(RANKED, target) == pytest.approx(1 / 3)


def test_reciprocal_rank_is_zero_when_support_is_missed_entirely():
    target = RetrievalTarget(documents=(VSR,))
    assert first_relevant_rank(RANKED, target) is None
    assert reciprocal_rank(RANKED, target) == 0.0


def test_reciprocal_rank_is_none_when_nothing_was_labelled():
    assert reciprocal_rank(RANKED, RetrievalTarget()) is None


def test_mrr_drops_nones_and_keeps_real_zeros():
    # Hand-computed: 1.0, 1/3 and 0.0 average to 1.3333/3 = 0.4444.
    # The None must be dropped, not counted as a fourth zero (which
    # would give 0.3333) and not counted as a fourth one.
    assert mean_reciprocal_rank([1.0, 1 / 3, 0.0, None]) == pytest.approx(0.44444, abs=1e-5)


def test_mrr_of_nothing_scorable_is_none():
    assert mean_reciprocal_rank([None, None]) is None
    assert mean_reciprocal_rank([]) is None


# --------------------------------------------------------- citation pages --


def test_citation_pages_need_every_expected_page():
    assert citation_pages_correct([2, 3], [2, 3]) is True
    # Half the support cited is not a correct citation for a case whose
    # answer combines two rules.
    assert citation_pages_correct([2], [2, 3]) is False
    # Extra pages are tolerated: an answer may cite more than the
    # minimum, and penalising that would push towards under-citing.
    assert citation_pages_correct([1, 2, 3], [2]) is True


def test_citation_pages_ignore_nulls_and_are_none_when_unpinned():
    # A .md document cites page None. It must not satisfy "page 2", and
    # a case with no page expectation is not scored at all.
    assert citation_pages_correct([None], [2]) is False
    assert citation_pages_correct([None], []) is None


# ------------------------------------------------------------ aggregation --


def test_summary_separates_a_miss_from_a_below_k_ranking_failure():
    # k=8. Hand-built cases:
    #   found-top   RR 1.0,  rank 1   -> neither list
    #   found-deep  RR 1/12, rank 12  -> below_k (retrieved, not sent)
    #   missed      RR 0.0,  rank None-> missed (never retrieved)
    #   refusal     no support labelled -> excluded from both
    cases = [
        CaseRetrieval("found-top", recall=1.0, reciprocal_rank=1.0, first_rank=1),
        CaseRetrieval("found-deep", recall=1.0, reciprocal_rank=1 / 12, first_rank=12),
        CaseRetrieval("missed", recall=0.0, reciprocal_rank=0.0, first_rank=None),
        CaseRetrieval("refusal", recall=None, reciprocal_rank=None, first_rank=None),
    ]
    summary = summarise_retrieval(cases, k=8)

    assert summary.n == 4
    assert summary.n_scored == 3
    # recall mean over the 3 scored cases: (1.0 + 1.0 + 0.0) / 3
    assert summary.recall_at_k == pytest.approx(2 / 3)
    # MRR over the 3 with an RR: (1.0 + 0.08333 + 0.0) / 3
    assert summary.mrr == pytest.approx(0.36111, abs=1e-5)
    assert summary.missed == ["missed"]
    assert summary.below_k == ["found-deep"]


def test_small_n_is_flagged_on_the_scored_cases_not_the_total():
    # 10 cases but only 3 with labelled support. A bucket whose recall
    # rests on 3 cases must say so, even though the bucket looks big.
    cases = [
        CaseRetrieval(f"a{i}", recall=1.0, reciprocal_rank=1.0, first_rank=1)
        for i in range(3)
    ] + [
        CaseRetrieval(f"b{i}", recall=None, reciprocal_rank=None, first_rank=None)
        for i in range(7)
    ]
    summary = summarise_retrieval(cases, k=8)
    assert summary.n == 10
    assert summary.n_scored == 3
    assert summary.small_n is True


def test_a_bucket_with_nothing_scorable_is_not_flagged_small_n():
    # All refusals. There is no retrieval claim to be under-powered
    # about, so a small-n warning would be noise.
    cases = [
        CaseRetrieval("r1", recall=None, reciprocal_rank=None, first_rank=None),
        CaseRetrieval("r2", recall=None, reciprocal_rank=None, first_rank=None),
    ]
    summary = summarise_retrieval(cases, k=8)
    assert summary.small_n is False
    assert summary.recall_at_k is None
    assert summary.mrr is None


def test_citation_rate_counts_only_cases_that_pinned_a_page():
    cases = [
        CaseRetrieval("a", 1.0, 1.0, 1, citation_pages_ok=True),
        CaseRetrieval("b", 1.0, 1.0, 1, citation_pages_ok=False),
        CaseRetrieval("c", 1.0, 1.0, 1, citation_pages_ok=None),
    ]
    summary = summarise_retrieval(cases, k=8)
    assert summary.n_citation_scored == 2
    assert summary.citation_pages_ok == 0.5
