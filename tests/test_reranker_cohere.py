"""Cohere v2 /rerank: wire format, failure classification, breaker.

These tests inject a fake HTTP client, so they prove what we SEND and
how we read what comes back -- not that Cohere is up. The one thing a
fake cannot check is whether the contract itself is right, which is why
the payload assertions here spell out the v2 field names explicitly: if
a future edit renames `max_tokens_per_doc` back to v1's
`max_chunks_per_doc`, a test should fail rather than a live key.
"""
import logging
import uuid

import httpx
import pytest

from app.services.reranker import (
    HostedReranker,
    RerankerAuthError,
    RerankerDisabledError,
    RerankerProtocolError,
    RerankerRateLimitError,
    RerankerRequestError,
    RerankerTimeoutError,
    RerankerUpstreamError,
    classify_rerank_status,
    rerank_with_fallback,
    should_answer,
)
from app.services.retrieval import Candidate

pytestmark = pytest.mark.asyncio


def _candidate(content: str, *, rrf: float = 0.016):
    return Candidate(
        chunk_id=uuid.uuid4(),
        parent_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="doc.pdf",
        page_number=1,
        content=content,
        token_count=len(content.split()),
        rrf_score=rrf,
        vector_rank=1,
        text_rank=None,
    )


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    """Records requests; replays a queue of responses or raises."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests: list[dict] = []

    async def post(self, url, *, json, headers):
        self.requests.append({"url": url, "json": json, "headers": headers})
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def _reranker(client, **kwargs):
    kwargs.setdefault("api_key", "k")
    kwargs.setdefault("model", "rerank-v3.5")
    kwargs.setdefault("base_url", "https://api.cohere.com/v2/rerank")
    kwargs.setdefault("timeout", 1.0)
    return HostedReranker(client=client, **kwargs)


# ---- the wire format -------------------------------------------------


async def test_payload_matches_the_cohere_v2_contract():
    client = _FakeClient(_FakeResponse({"results": [{"index": 0, "relevance_score": 0.8}]}))
    reranker = _reranker(client, max_tokens_per_doc=4096)

    await reranker.rerank("who approves expenses", [_candidate("a"), _candidate("b")], 1)

    body = client.requests[0]["json"]
    # v2 takes documents as plain STRINGS. v1's {"text": ...} objects are
    # gone, and sending them is a 400.
    assert body["documents"] == ["a", "b"]
    assert all(isinstance(d, str) for d in body["documents"])
    # v2's name for per-document truncation. v1 called it
    # max_chunks_per_doc and meant something different.
    assert body["max_tokens_per_doc"] == 4096
    assert "max_chunks_per_doc" not in body
    assert body["query"] == "who approves expenses"
    assert body["model"] == "rerank-v3.5"


async def test_top_n_is_clamped_to_the_documents_actually_sent():
    client = _FakeClient(_FakeResponse({"results": [{"index": 0, "relevance_score": 0.8}]}))
    reranker = _reranker(client)

    # Asking for more results than there are documents is a 400, and it
    # happens whenever retrieval returns fewer candidates than
    # RERANK_TOP_N -- a small tenant, or a narrow filter.
    await reranker.rerank("q", [_candidate("only one")], 8)

    assert client.requests[0]["json"]["top_n"] == 1


async def test_auth_and_accept_headers_are_sent():
    client = _FakeClient(_FakeResponse({"results": []}))
    reranker = _reranker(client, api_key="secret-key")

    await reranker.rerank("q", [_candidate("a")], 1)

    headers = client.requests[0]["headers"]
    assert headers["Authorization"] == "Bearer secret-key"
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"


async def test_the_api_key_never_appears_in_the_payload_or_url():
    client = _FakeClient(_FakeResponse({"results": []}))
    reranker = _reranker(client, api_key="secret-key")

    await reranker.rerank("q", [_candidate("a")], 1)

    request = client.requests[0]
    # A key in a query string lands in every proxy and access log, the
    # same reason Gemini auth rides a header here.
    assert "secret-key" not in request["url"]
    assert "secret-key" not in str(request["json"])


# ---- blank documents and the index remap -----------------------------


async def test_blank_documents_are_dropped_and_indices_still_resolve():
    # The subtle one. Cohere's returned index refers to the list we
    # SENT, so dropping a blank without remapping would attach "second"'s
    # score to "third" -- in a plausible-looking order, with a citation
    # pointing at the wrong chunk.
    candidates = [_candidate("first"), _candidate("   "), _candidate("third")]
    client = _FakeClient(
        _FakeResponse(
            {"results": [{"index": 1, "relevance_score": 0.9},
                         {"index": 0, "relevance_score": 0.3}]}
        )
    )
    reranker = _reranker(client)

    ranked = await reranker.rerank("q", candidates, 2)

    assert client.requests[0]["json"]["documents"] == ["first", "third"]
    # index 1 of what we sent is "third", not the blank and not "second".
    assert [r.candidate.content for r in ranked] == ["third", "first"]
    assert ranked[0].candidate is candidates[2]


async def test_all_blank_candidates_fall_back_without_an_http_call():
    client = _FakeClient(_FakeResponse({"results": []}))
    reranker = _reranker(client)
    candidates = [_candidate(" "), _candidate("")]

    ranked, used_fallback = await rerank_with_fallback(
        reranker, "q", candidates, top_n=2
    )

    assert client.requests == []
    # RRF order over blank chunks is useless but harmless; what matters
    # is the flag, so the gate does not read an empty list as calibrated.
    assert used_fallback is True
    assert len(ranked) == 2


async def test_candidates_over_the_document_cap_are_truncated():
    client = _FakeClient(_FakeResponse({"results": [{"index": 0, "relevance_score": 0.5}]}))
    reranker = _reranker(client, max_documents=3)

    await reranker.rerank("q", [_candidate(f"c{i}") for i in range(10)], 2)

    # Over the limit is a 400 for the whole query. Truncating loses the
    # tail of an already rank-ordered list instead.
    assert len(client.requests[0]["json"]["documents"]) == 3


# ---- status classification -------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, RerankerAuthError),
        (403, RerankerAuthError),
        (400, RerankerRequestError),
        (404, RerankerRequestError),
        (422, RerankerRequestError),
        (429, RerankerRateLimitError),
        (408, RerankerTimeoutError),
        (500, RerankerUpstreamError),
        (503, RerankerUpstreamError),
        # Unknown statuses are transient on purpose: guessing "permanent"
        # would let one odd response from a proxy disable reranking for
        # the life of the process.
        (418, RerankerUpstreamError),
    ],
)
async def test_status_codes_map_to_their_own_error(status, expected):
    assert isinstance(classify_rerank_status(status), expected)


async def test_a_429_does_not_disable_the_reranker():
    client = _FakeClient(_FakeResponse({}, status=429))
    reranker = _reranker(client)

    with pytest.raises(RerankerRateLimitError):
        await reranker.rerank("q", [_candidate("a")], 1)

    # A trial key is 10 req/min. Treating a throttle as permanent would
    # turn one busy second into reranking being off until restart.
    assert reranker.is_disabled is False
    with pytest.raises(RerankerRateLimitError):
        await reranker.rerank("q", [_candidate("a")], 1)
    assert len(client.requests) == 2


async def test_an_httpx_timeout_is_classified_as_a_timeout():
    # httpx's timeouts are not TimeoutError subclasses, so without
    # explicit handling they arrive as an unexplained generic failure.
    client = _FakeClient(httpx.ReadTimeout("too slow"))
    reranker = _reranker(client)

    with pytest.raises(RerankerTimeoutError):
        await reranker.rerank("q", [_candidate("a")], 1)
    assert reranker.is_disabled is False


async def test_a_connect_error_is_transient_not_permanent():
    client = _FakeClient(httpx.ConnectError("dns is having a day"))
    reranker = _reranker(client)

    with pytest.raises(RerankerUpstreamError):
        await reranker.rerank("q", [_candidate("a")], 1)
    assert reranker.is_disabled is False


# ---- the breaker -----------------------------------------------------


async def test_a_401_disables_the_reranker_and_stops_calling():
    client = _FakeClient(_FakeResponse({}, status=401))
    reranker = _reranker(client)

    with pytest.raises(RerankerAuthError):
        await reranker.rerank("q", [_candidate("a")], 1)
    assert reranker.is_disabled is True

    # The second call must not reach the network. A bad key cannot become
    # a good one without a restart, so every further request would be
    # pure added latency on a user's query.
    with pytest.raises(RerankerDisabledError):
        await reranker.rerank("q", [_candidate("a")], 1)
    assert len(client.requests) == 1


async def test_a_404_on_a_retired_model_disables_the_reranker():
    client = _FakeClient(_FakeResponse({}, status=404))
    reranker = _reranker(client, model="rerank-v2.0-retired")

    with pytest.raises(RerankerRequestError):
        await reranker.rerank("q", [_candidate("a")], 1)

    assert reranker.is_disabled is True


async def test_the_breaker_logs_an_error_once_not_per_query(caplog):
    client = _FakeClient(_FakeResponse({}, status=401))
    reranker = _reranker(client)

    with caplog.at_level(logging.DEBUG, logger="app.services.reranker"):
        for _ in range(4):
            await rerank_with_fallback(reranker, "q", [_candidate("a")], top_n=1)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    # Once. Repeating it per query buries the cause under thousands of
    # copies of its own consequence.
    assert len(errors) == 1
    assert "disabled for the life of this process" in errors[0].getMessage()


async def test_a_disabled_reranker_still_answers_on_rrf_order():
    client = _FakeClient(_FakeResponse({}, status=401))
    reranker = _reranker(client)
    candidates = [_candidate("a", rrf=0.016), _candidate("b", rrf=0.015)]

    await rerank_with_fallback(reranker, "q", candidates, top_n=2)
    ranked, used_fallback = await rerank_with_fallback(
        reranker, "q", candidates, top_n=2
    )

    # The reranker is a quality improvement, not a correctness
    # requirement: a dead provider degrades the ranking, it does not take
    # the product down.
    assert used_fallback is True
    assert [r.candidate.content for r in ranked] == ["a", "b"]
    # And the gate must open, because RRF scores top out near 0.016 and
    # would fail a 0.3 threshold -- which would refuse every question.
    assert should_answer(ranked, reranker=reranker, used_fallback=True) is True


# ---- malformed responses ---------------------------------------------


async def test_a_body_without_results_is_a_protocol_error():
    client = _FakeClient(_FakeResponse({"message": "not found"}))
    reranker = _reranker(client)

    with pytest.raises(RerankerProtocolError):
        await reranker.rerank("q", [_candidate("a")], 1)
    # NOT permanent: a proxy error page looks exactly like this and
    # resolves on its own.
    assert reranker.is_disabled is False


async def test_a_non_json_body_is_a_protocol_error():
    client = _FakeClient(_FakeResponse(ValueError("Expecting value")))
    reranker = _reranker(client)

    with pytest.raises(RerankerProtocolError):
        await reranker.rerank("q", [_candidate("a")], 1)


async def test_a_duplicate_index_is_rejected():
    client = _FakeClient(
        _FakeResponse(
            {"results": [{"index": 0, "relevance_score": 0.9},
                         {"index": 0, "relevance_score": 0.8}]}
        )
    )
    reranker = _reranker(client)

    # Two scores for one chunk would consume a top_n slot and silently
    # drop a real candidate.
    with pytest.raises(RerankerProtocolError):
        await reranker.rerank("q", [_candidate("a"), _candidate("b")], 2)


async def test_a_boolean_index_is_rejected():
    client = _FakeClient(_FakeResponse({"results": [{"index": True, "relevance_score": 0.9}]}))
    reranker = _reranker(client)

    # bool is an int subclass, so True would quietly index position 1.
    with pytest.raises(RerankerProtocolError):
        await reranker.rerank("q", [_candidate("a"), _candidate("b")], 2)


async def test_a_missing_relevance_score_is_rejected():
    client = _FakeClient(_FakeResponse({"results": [{"index": 0}]}))
    reranker = _reranker(client)

    with pytest.raises(RerankerProtocolError):
        await reranker.rerank("q", [_candidate("a")], 1)
