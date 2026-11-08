"""A provider failure on the ask path is a 503, not a 500.

Measured on the server: the api container had no egress, every question
raised TransientEmbeddingError out of prepare_query, and the client got
a bare 500 with nothing to act on. A 500 says "this server is broken",
which invites a retry that cannot work and a bug report that is not a
bug.

The other half of this file is about what the body must NOT contain. The
raw provider message carried the embedding host, the model name and the
upstream URL -- the same class of leak the document failure codes were
written to remove, one layer up.
"""
import pytest

from app.api.v1.query_errors import (
    QUERY_PROVIDER_ERRORS,
    QueryErrorCode,
    classify_query_error,
    query_unavailable,
)
from app.services.embedding import (
    EmbeddingError,
    PermanentEmbeddingError,
    TransientEmbeddingError,
)
from app.services.llm import (
    LLMEmptyResponseError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUpstreamError,
)

#: The real message from the server, with everything it disclosed.
LEAKY = (
    "transient provider error 429: Client error '429 Too Many Requests' for "
    "url 'https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-embedding-001:batchEmbedContents'"
)


# --------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc,expected",
    [
        (TransientEmbeddingError("dns"), QueryErrorCode.PROVIDER_UNAVAILABLE),
        (LLMTimeoutError("slow"), QueryErrorCode.PROVIDER_UNAVAILABLE),
        (LLMUpstreamError("502"), QueryErrorCode.PROVIDER_UNAVAILABLE),
        (LLMRateLimitError("429"), QueryErrorCode.PROVIDER_RATE_LIMITED),
        (PermanentEmbeddingError("400"), QueryErrorCode.SEARCH_UNAVAILABLE),
        (EmbeddingError("unclassified"), QueryErrorCode.SEARCH_UNAVAILABLE),
        (LLMEmptyResponseError("nothing"), QueryErrorCode.SEARCH_UNAVAILABLE),
    ],
)
def test_each_provider_error_gets_its_code(exc, expected):
    assert classify_query_error(exc) is expected


def test_rate_limited_is_checked_before_the_generic_llm_error():
    """The subclass trap, again.

    LLMRateLimitError IS an LLMError, and TransientEmbeddingError IS an
    EmbeddingError. isinstance checks in the wrong order would collapse
    all three codes into one -- which is exactly how the ingestion task
    put a DNS failure in its permanent branch.
    """
    from app.services.llm import LLMError

    assert issubclass(LLMRateLimitError, LLMError)
    assert issubclass(TransientEmbeddingError, EmbeddingError)
    assert classify_query_error(LLMRateLimitError("x")) is (
        QueryErrorCode.PROVIDER_RATE_LIMITED
    )
    assert classify_query_error(TransientEmbeddingError("x")) is (
        QueryErrorCode.PROVIDER_UNAVAILABLE
    )


def test_a_non_provider_error_is_not_silently_classified():
    """A bug must not be dressed up as a provider outage.

    Returning PROVIDER_UNAVAILABLE for a ValueError would turn every
    crash into "try again in 30 seconds" and hide real defects behind a
    retry loop.
    """
    with pytest.raises(TypeError):
        classify_query_error(ValueError("a real bug"))


def test_the_caught_tuple_covers_everything_classify_handles():
    """The route catches QUERY_PROVIDER_ERRORS.

    A type classify_query_error knows about but the tuple does not would
    leave a 500 behind -- the bug this module exists for, reintroduced
    by a later edit.
    """
    for exc in (
        TransientEmbeddingError("x"),
        PermanentEmbeddingError("x"),
        EmbeddingError("x"),
        LLMRateLimitError("x"),
        LLMTimeoutError("x"),
        LLMUpstreamError("x"),
        LLMEmptyResponseError("x"),
    ):
        assert isinstance(exc, QUERY_PROVIDER_ERRORS), type(exc).__name__
        classify_query_error(exc)  # must not raise


# --------------------------------------------------------------------
# The HTTP answer
# --------------------------------------------------------------------


def test_it_is_a_503_with_a_stable_code():
    exc = query_unavailable(TransientEmbeddingError("dns"))
    assert exc.status_code == 503
    assert exc.headers["X-Error-Code"] == "provider_unavailable"


def test_retry_after_is_sent_when_waiting_will_help():
    for error in (TransientEmbeddingError("dns"), LLMRateLimitError("429")):
        exc = query_unavailable(error)
        assert int(exc.headers["Retry-After"]) > 0, type(error).__name__


def test_retry_after_is_absent_when_waiting_will_not_help():
    """A Retry-After on a misconfiguration is a lie with a number on it.

    PermanentEmbeddingError means a rejected request or a bad key. The
    client should surface "an administrator needs to look at this", not
    count down 30 seconds and try again forever.
    """
    exc = query_unavailable(PermanentEmbeddingError("400 bad model"))
    assert "Retry-After" not in exc.headers
    assert exc.headers["X-Error-Code"] == "search_unavailable"


def test_the_providers_own_retry_after_is_honoured_and_rounded_up():
    """Rounding down produces a retry that is refused again."""
    exc = query_unavailable(LLMRateLimitError("429", retry_after=12.3))
    assert exc.headers["Retry-After"] == "13"


def test_detail_is_a_plain_string_so_the_bff_renders_it():
    """apiFetch accepts `detail` only when it is a non-empty string.

    A structured body would be DISCARDED by the BFF in favour of a
    generic per-status message -- so the user would see something less
    useful than the sentence chosen here. That is why the code travels
    in a header instead.
    """
    exc = query_unavailable(TransientEmbeddingError("dns"))
    assert isinstance(exc.detail, str)
    assert exc.detail.strip()


# --------------------------------------------------------------------
# What must never reach the client
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        TransientEmbeddingError(LEAKY),
        PermanentEmbeddingError(LEAKY),
        LLMRateLimitError(LEAKY, retry_after=5),
        LLMTimeoutError(LEAKY),
    ],
)
def test_no_internal_detail_leaks_into_the_response(error):
    """The phase rules forbid hostnames, URLs and internal ids in a
    user-facing error, and the raw provider message carries all three.

    Checked against the ACTUAL message the server produced, not a
    paraphrase: it named the embedding provider, the model and the
    upstream endpoint.
    """
    exc = query_unavailable(error)
    body = f"{exc.detail} {exc.headers}"

    for secret in (
        "generativelanguage.googleapis.com",
        "gemini-embedding-001",
        "batchEmbedContents",
        "https://",
        "429",
        "Client error",
    ):
        assert secret not in body, f"{secret!r} leaked into the response"

    # And no exception class name either -- it names the library and the
    # shape of the internals.
    for name in ("EmbeddingError", "LLMError", "Transient", "Traceback"):
        assert name not in exc.detail


def test_the_message_tells_the_user_what_to_do():
    """Three codes, three different actions. A message that fits all
    three would be useful for none -- the same reasoning as
    safe_answer_for and FailureCode."""
    messages = {
        code: query_unavailable(exc).detail
        for code, exc in (
            (QueryErrorCode.PROVIDER_UNAVAILABLE, TransientEmbeddingError("x")),
            (QueryErrorCode.PROVIDER_RATE_LIMITED, LLMRateLimitError("x")),
            (QueryErrorCode.SEARCH_UNAVAILABLE, PermanentEmbeddingError("x")),
        )
    }
    assert len(set(messages.values())) == 3, messages
    assert "try again" in messages[QueryErrorCode.PROVIDER_UNAVAILABLE].lower()
    assert "administrator" in messages[QueryErrorCode.SEARCH_UNAVAILABLE].lower()


# --------------------------------------------------------------------
# End to end, through the real route
# --------------------------------------------------------------------


def test_the_ask_route_returns_503_not_500(
    client, db_session, org_factory, user_factory, auth_headers, monkeypatch
):
    """The regression, driven through the actual endpoint.

    prepare_query is made to raise exactly what the server raised. The
    assertion that matters is `!= 500`: before the handler existed this
    reached FastAPI's default handler and the client got a bare 500.
    """
    import uuid as _uuid

    from app.api.v1.routes import conversations as route
    from app.models.conversation import Conversation

    org = org_factory()
    user = user_factory(org)
    conversation = Conversation(
        id=_uuid.uuid4(), org_id=org.id, user_id=user.id, title="t"
    )
    db_session.add(conversation)
    db_session.flush()

    async def _boom(**_kwargs):
        raise TransientEmbeddingError(LEAKY)

    monkeypatch.setattr(route.query_service, "prepare_query", _boom)

    response = client.post(
        f"/api/v1/conversations/{conversation.id}/messages",
        json={"content": "what is the expense limit?"},
        headers=auth_headers(user),
    )

    assert response.status_code == 503, response.status_code
    assert response.headers["x-error-code"] == "provider_unavailable"
    assert int(response.headers["retry-after"]) > 0

    body = response.text
    for secret in (
        "generativelanguage.googleapis.com",
        "gemini-embedding-001",
        "batchEmbedContents",
        "TransientEmbeddingError",
    ):
        assert secret not in body, f"{secret!r} leaked in the response body"
