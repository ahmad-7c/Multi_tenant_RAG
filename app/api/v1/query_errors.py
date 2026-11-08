"""Turn a provider failure on the ask path into an honest HTTP answer.

Before this existed, `prepare_query` was awaited with no handler, so a
TransientEmbeddingError reached FastAPI's default handler and the client
got a bare 500 with no body worth reading. Measured on the server: the
api container had no egress, every question raised
TransientEmbeddingError, and the UI showed a generic failure with
nothing to act on.

A 500 is the wrong status as well as the wrong body. It says "this
server is broken", which invites a retry that cannot work and a bug
report that is not a bug. 503 says "not right now", and Retry-After says
how long -- both of which the UI and any future client can act on
mechanically.

THREE CODES, because they map to three different ACTIONS: wait briefly,
wait longer, or tell an administrator. Same rule as FailureCode in
document_failures: a code that implies the same action as another is
detail pretending to be useful.
"""
import enum
import logging

from fastapi import HTTPException, status

from app.services.embedding import (
    EmbeddingError,
    TransientEmbeddingError,
)
from app.services.llm import (
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUpstreamError,
)

logger = logging.getLogger(__name__)


class QueryErrorCode(str, enum.Enum):
    """Stable, machine-readable reasons a question could not be answered."""

    #: Reachability or a 5xx. Resolves on its own; wait and retry.
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    #: The provider throttled us. Also resolves by waiting, but longer.
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    #: Misconfiguration -- a rejected request, a bad key, a model that
    #: does not exist. Waiting will NOT help, so no Retry-After is sent.
    SEARCH_UNAVAILABLE = "search_unavailable"


#: What the user is told. Deliberately free of hostnames, exception
#: class names, status codes and provider names: the phase rules forbid
#: them in user-facing errors, and the operator has the log.
_MESSAGES = {
    QueryErrorCode.PROVIDER_UNAVAILABLE: (
        "Search is temporarily unavailable. Please try again in a moment."
    ),
    QueryErrorCode.PROVIDER_RATE_LIMITED: (
        "Too many questions are being asked right now. "
        "Please try again shortly."
    ),
    QueryErrorCode.SEARCH_UNAVAILABLE: (
        "Search is unavailable. An administrator needs to look at this."
    ),
}

#: Default waits, in seconds. Short enough that a user will actually
#: retry, long enough that a hundred clients retrying do not become the
#: outage's second act.
_RETRY_AFTER = {
    QueryErrorCode.PROVIDER_UNAVAILABLE: 30,
    QueryErrorCode.PROVIDER_RATE_LIMITED: 60,
}


def classify_query_error(exc: BaseException) -> QueryErrorCode:
    """Map a provider exception onto one of the three codes.

    Order matters: LLMRateLimitError and LLMTimeoutError are both
    LLMError subclasses, and TransientEmbeddingError is an
    EmbeddingError subclass -- so the specific cases are tested first.
    The same subclass trap put a DNS failure in the permanent branch of
    the ingestion task.
    """
    if isinstance(exc, LLMRateLimitError):
        return QueryErrorCode.PROVIDER_RATE_LIMITED
    if isinstance(exc, (TransientEmbeddingError, LLMTimeoutError, LLMUpstreamError)):
        return QueryErrorCode.PROVIDER_UNAVAILABLE
    # A remaining EmbeddingError or LLMError is a 400-shaped or
    # unclassified failure. Waiting will not fix a rejected request, so
    # it gets the code that says so rather than a Retry-After that lies.
    if isinstance(exc, (EmbeddingError, LLMError)):
        return QueryErrorCode.SEARCH_UNAVAILABLE
    raise TypeError(f"not a provider error: {type(exc).__name__}")


def query_unavailable(exc: BaseException) -> HTTPException:
    """The 503 to raise, with the raw cause logged and never returned.

    `detail` stays a plain STRING on purpose. The BFF's apiFetch accepts
    `detail` only when it is a non-empty string and otherwise falls back
    to a generic per-status message -- so a structured body here would
    be DISCARDED and the user would see something less useful than the
    sentence we chose. The machine-readable code therefore travels in a
    header, where it costs the body nothing.
    """
    code = classify_query_error(exc)
    # The full exception goes to the log, where an operator can see the
    # host, the status and the traceback. None of it goes to the client.
    logger.warning(
        "query failed: code=%s cause=%s: %s",
        code.value,
        type(exc).__name__,
        exc,
    )

    headers = {"X-Error-Code": code.value}
    retry_after = _RETRY_AFTER.get(code)
    if isinstance(exc, LLMRateLimitError) and exc.retry_after:
        # The provider told us how long. Rounded UP, because rounding
        # down produces a retry that is refused again.
        retry_after = max(1, int(exc.retry_after + 0.999))
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)

    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=_MESSAGES[code],
        headers=headers,
    )


#: Everything query_unavailable can classify. The route catches this
#: tuple, so adding a provider error type to classify_query_error
#: without adding it here would leave a 500 behind -- which is the bug
#: this module was written for.
QUERY_PROVIDER_ERRORS = (EmbeddingError, LLMError)
