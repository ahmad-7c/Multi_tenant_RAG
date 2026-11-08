"""Transient ingestion failures retry; permanent ones stay permanent.

The bug this file exists for: PERMANENT_ERRORS listed EmbeddingError,
and TransientEmbeddingError SUBCLASSES it. Python takes the first
matching handler rather than the most specific, so a DNS failure was
caught by the permanent branch, written as a terminal failure, and never
retried. The document became unrecoverable and the uploader was told to
delete and re-upload a file that was perfectly fine.

It was made worse by classify_failure having no pattern for
"Temporary failure in name resolution", so the recorded code was
UNKNOWN -- which tells the uploader nothing and the operator nothing.
"""
import uuid

import pytest
from celery.exceptions import MaxRetriesExceededError, Retry

from app.services.document_failures import FailureCode, classify_failure
from app.services.embedding import (
    EmbeddingError,
    PermanentEmbeddingError,
    TransientEmbeddingError,
)
from app.workers import ingestion
from app.workers.ingestion import PERMANENT_ERRORS, TRANSIENT_ERRORS, _retry_delay

#: The exact message the server produced.
DNS_MESSAGE = "transient provider error: [Errno -3] Temporary failure in name resolution"


# --------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        DNS_MESSAGE,
        "Temporary failure in name resolution",
        "[Errno -2] Name or service not known",
        "gaierror(-3, 'Temporary failure in name resolution')",
        "getaddrinfo failed",
        "Network is unreachable",
        "No route to host",
        "Connection reset by peer",
        "Failed to establish a new connection",
    ],
)
def test_network_failures_classify_as_provider_unavailable(message):
    """Not UNKNOWN. The difference is what the uploader is told to do.

    provider_unavailable maps to "wait and try again". UNKNOWN maps to a
    generic message that invites someone to fix a file that is not
    broken -- a loop they cannot exit, which is the exact failure the
    coarse-code design was meant to prevent.
    """
    assert classify_failure(message) is FailureCode.PROVIDER_UNAVAILABLE


def test_the_dns_message_used_to_classify_as_unknown():
    """The regression, stated as a fact rather than a memory.

    Pinned by checking that the specific wording is matched by the
    pattern now -- "temporarily unavailable" is NOT "Temporary failure",
    and that one word was the whole gap.
    """
    assert "temporarily unavailable" not in DNS_MESSAGE.lower()
    assert classify_failure(DNS_MESSAGE) is not FailureCode.UNKNOWN


def test_a_corrupt_file_still_classifies_as_unreadable():
    """The widened network patterns must not swallow a real file error."""
    assert classify_failure("cannot open broken.pdf: not a valid PDF") is (
        FailureCode.UNREADABLE
    )


def test_an_unrecognised_message_is_still_unknown():
    """No guessing. A plausible-sounding guess is worse than UNKNOWN."""
    assert classify_failure("something nobody has seen before") is FailureCode.UNKNOWN


# --------------------------------------------------------------------
# Which branch catches what
# --------------------------------------------------------------------


def test_transient_is_caught_before_permanent():
    """The ordering IS the fix, and it is invisible in a diff.

    TransientEmbeddingError is a subclass of EmbeddingError, which is in
    PERMANENT_ERRORS. If the except clauses are reordered, a transient
    error is permanent again and every test below that uses the fake
    task still passes -- so the relationship is asserted directly.
    """
    assert issubclass(TransientEmbeddingError, EmbeddingError)
    assert EmbeddingError in PERMANENT_ERRORS
    assert TransientEmbeddingError in TRANSIENT_ERRORS

    source = (ingestion.__file__)
    with open(source, encoding="utf-8") as handle:
        code = handle.read()
    assert code.index("except TRANSIENT_ERRORS") < code.index("except PERMANENT_ERRORS"), (
        "TRANSIENT_ERRORS must be caught first or it never matches"
    )


def test_permanent_embedding_errors_are_not_retried():
    """A rejected input fails the same way every time.

    PermanentEmbeddingError is a 400-shaped failure -- a malformed
    request, an unsupported model. Retrying it burns quota to receive
    the same answer, four times.
    """
    assert PermanentEmbeddingError in PERMANENT_ERRORS
    assert PermanentEmbeddingError not in TRANSIENT_ERRORS
    assert not issubclass(PermanentEmbeddingError, TRANSIENT_ERRORS)


# --------------------------------------------------------------------
# Backoff
# --------------------------------------------------------------------


def test_the_backoff_grows_and_is_bounded():
    """Bounded twice: the exponent is capped, and so is the count.

    An unbounded retry during a provider outage is a queue that grows
    for as long as the outage lasts and then stampedes the provider the
    moment it recovers.
    """
    delays = [_retry_delay(i) for i in range(8)]
    assert delays[0] < delays[1] < delays[2], delays
    assert max(delays) <= 600, delays
    # And it plateaus rather than growing forever.
    assert delays[-1] == delays[-2], delays


# --------------------------------------------------------------------
# The three task branches, against a fake task
# --------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, retries: int) -> None:
        self.retries = retries


class _FakeTask:
    """Stands in for the bound Celery task.

    `self.retry()` raises -- Retry to reschedule, MaxRetriesExceededError
    when the attempts are gone -- which is the behaviour the handler is
    written against. A fake that RETURNED instead would let a broken
    handler pass.
    """

    def __init__(self, retries: int = 0, exhausted: bool = False) -> None:
        self.request = _FakeRequest(retries)
        self.exhausted = exhausted
        self.calls: list[dict] = []

    def retry(self, **kwargs):
        self.calls.append(kwargs)
        if self.exhausted:
            raise MaxRetriesExceededError()
        raise Retry()


def _run(task, exc, monkeypatch):
    """Drive ingest_document to the point of failure and record the outcome."""
    marked: list[Exception] = []
    monkeypatch.setattr(ingestion, "_mark_failed", lambda _id, e: marked.append(e))

    # Claim-the-document transaction and the slow work are both replaced:
    # this test is about which handler runs, not about the database.
    monkeypatch.setattr(ingestion, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(ingestion, "get_storage", lambda: _Boom(exc))

    # `.__wrapped__` is already BOUND to the real Celery task, so calling
    # it would use the real self and ignore the fake -- which showed up
    # as "takes 2 positional arguments but 3 were given". `.__func__` is
    # the plain function, so the fake task really is the `self` the
    # handler calls retry() on.
    run = ingestion.ingest_document.__wrapped__.__func__

    outcome = None
    try:
        run(task, str(uuid.uuid4()))
    except Retry:
        outcome = "retry"
    except Exception as raised:  # noqa: BLE001 - the branch under test
        outcome = type(raised).__name__
    else:
        outcome = "returned"
    return outcome, marked, task.calls


class _Boom:
    def __init__(self, exc):
        self.exc = exc

    def read(self, _key):
        raise self.exc


class _FakeDoc:
    org_id = uuid.uuid4()
    storage_key = "k"
    filename = "f.md"
    status = None
    error_message = None


class _FakeSession:
    def get(self, _model, _pk):
        return _FakeDoc()

    def commit(self):
        pass

    def close(self):
        pass


def test_a_transient_failure_reschedules_and_does_not_mark_failed(monkeypatch):
    """The document stays PROCESSING, which is honest: it still is."""
    task = _FakeTask(retries=0)
    outcome, marked, calls = _run(task, TransientEmbeddingError(DNS_MESSAGE), monkeypatch)

    assert outcome == "retry", outcome
    assert marked == [], "a retryable failure must not be recorded as terminal"
    assert calls and calls[0]["countdown"] == _retry_delay(0)
    assert calls[0]["max_retries"] == 3


def test_an_exhausted_transient_failure_is_recorded_with_the_real_cause(monkeypatch):
    """Terminal only once the attempts are gone -- and honest about why.

    The recorded code must be provider_unavailable, so the uploader is
    told to wait. Recording UNKNOWN here is what sent someone to fix a
    file that was never the problem.
    """
    task = _FakeTask(retries=3, exhausted=True)
    outcome, marked, _ = _run(task, TransientEmbeddingError(DNS_MESSAGE), monkeypatch)

    assert outcome == "returned", outcome
    assert len(marked) == 1
    assert classify_failure(str(marked[0])) is FailureCode.PROVIDER_UNAVAILABLE


def test_a_permanent_failure_is_recorded_immediately(monkeypatch):
    """No retry at all, and no exception out of the task."""
    task = _FakeTask()
    outcome, marked, calls = _run(
        task, PermanentEmbeddingError("400: unsupported model"), monkeypatch
    )

    assert outcome == "returned", outcome
    assert len(marked) == 1
    assert calls == [], "a permanent failure must never call retry()"


def test_an_unexpected_error_is_recorded_and_re_raised(monkeypatch):
    """A bug must leave a terminal state AND be visible in Celery.

    Returning quietly would leave the document failed with nobody
    alerted; not marking it would leave it `processing` forever.
    """
    task = _FakeTask()
    outcome, marked, calls = _run(task, ValueError("a real bug"), monkeypatch)

    assert outcome == "ValueError", outcome
    assert len(marked) == 1
    assert calls == []
