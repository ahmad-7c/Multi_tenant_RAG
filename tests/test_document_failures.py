"""What a user is told when a document fails to process.

The raw failure is a provider exception. A real one, from this
project's own database:

    transient provider error 429: Client error '429 Too Many Requests'
    for url 'https://generativelanguage.googleapis.com/v1beta/models/
    gemini-embedding-001:batchEmbedContents' For more information
    check: https://developer.mozilla.org/...

That was shown verbatim on the Documents page. It tells the person who
uploaded a PDF nothing they can act on, and it discloses the embedding
provider, the model name and the upstream URL -- which the phase
skill's own rule forbids ("user-facing errors never include stack
traces, backend URLs, or internal ids").

So the raw text stays in the database and the logs for operators, and
the API publishes a CLASSIFICATION instead. The user-facing sentence
is then the UI's business, which is where copy belongs.
"""
import pytest

from app.services.document_failures import FailureCode, classify_failure

# The exact string from the failed row in the dev database.
REAL_429 = (
    "transient provider error 429: Client error '429 Too Many Requests' for "
    "url 'https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-embedding-001:batchEmbedContents'\nFor more information check: "
    "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429"
)


def test_the_real_rate_limit_failure_is_classified():
    assert classify_failure(REAL_429) is FailureCode.RATE_LIMITED


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("transient provider error 429: ...", FailureCode.RATE_LIMITED),
        ("Client error '429 Too Many Requests'", FailureCode.RATE_LIMITED),
        ("rate limit exceeded for this project", FailureCode.RATE_LIMITED),
        ("quota exceeded", FailureCode.RATE_LIMITED),
        ("ReadTimeout: the read operation timed out", FailureCode.TIMEOUT),
        ("httpx.ConnectTimeout", FailureCode.TIMEOUT),
        ("no extractable text in the document", FailureCode.NO_TEXT),
        ("document produced 0 chunks", FailureCode.NO_TEXT),
        ("the file is empty", FailureCode.NO_TEXT),
        ("unsupported file type '.docx'", FailureCode.UNSUPPORTED_TYPE),
        ("cannot open broken document: FileDataError", FailureCode.UNREADABLE),
        ("ConnectError: connection refused", FailureCode.PROVIDER_UNAVAILABLE),
        ("Server error '503 Service Unavailable'", FailureCode.PROVIDER_UNAVAILABLE),
        ("Server error '500 Internal Server Error'", FailureCode.PROVIDER_UNAVAILABLE),
    ],
)
def test_known_failures_are_classified(raw, expected):
    assert classify_failure(raw) is expected


@pytest.mark.parametrize("raw", ["", None, "   ", "something nobody predicted"])
def test_anything_unrecognised_is_UNKNOWN_not_a_guess(raw):
    """An unmatched message must not fall into a plausible-looking bucket.

    Telling someone "the provider was busy, try again" when the real
    cause was a corrupt file sends them round a loop. UNKNOWN maps to
    honest copy that says we could not determine the cause.
    """
    assert classify_failure(raw) is FailureCode.UNKNOWN


def test_classification_never_returns_the_raw_text():
    """The point of the whole module.

    Every code is a fixed enum member, so no provider prose, URL or
    model name can reach a response through this path by construction.
    """
    code = classify_failure(REAL_429)

    assert isinstance(code, FailureCode)
    serialised = code.value
    for leak in (
        "googleapis",
        "gemini",
        "http",
        "429",
        "mozilla",
        "batchEmbedContents",
    ):
        assert leak not in serialised.lower()


def test_every_code_is_a_short_stable_slug():
    """The UI maps these to copy, so they are an API contract."""
    for code in FailureCode:
        assert code.value.islower()
        assert " " not in code.value
        assert len(code.value) <= 24


# ------------------------------------------------- through the API ----


def test_the_api_publishes_a_code_and_never_the_raw_message(
    client, db_session, org_factory, user_factory, auth_headers
):
    """The end the user actually sees.

    A unit-tested classifier is worth nothing if the route still
    returns `error_message` beside it, so this asserts on the response
    body: the code is present and every fragment of the provider's
    prose is absent.
    """
    import uuid

    from app.models.document import Document, DocumentStatus
    from app.models.user import UserRole

    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)
    document = Document(
        id=uuid.uuid4(),
        org_id=org.id,
        uploaded_by=admin.id,
        filename="big-book.pdf",
        storage_key=f"{org.id}/{uuid.uuid4()}/big-book.pdf",
        file_size_bytes=1_349_115,
        status=DocumentStatus.FAILED,
        error_message=REAL_429,
    )
    db_session.add(document)
    db_session.commit()

    body = client.get(
        f"/api/v1/documents/{document.id}", headers=auth_headers(admin)
    ).json()

    assert body["failure_code"] == "rate_limited"
    assert "error_message" not in body

    raw = str(body)
    for leak in (
        "googleapis.com",
        "gemini-embedding",
        "batchEmbedContents",
        "developer.mozilla.org",
        "Too Many Requests",
    ):
        assert leak not in raw, f"the response leaked {leak!r}"

    # The operator detail is still in the database, where it belongs.
    db_session.refresh(document)
    assert "googleapis.com" in (document.error_message or "")


def test_a_healthy_document_has_no_failure_code(
    client, db_session, org_factory, user_factory, auth_headers
):
    """Null, not a cheerful code. A ready document has not failed."""
    import uuid

    from app.models.document import Document, DocumentStatus
    from app.models.user import UserRole

    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)
    document = Document(
        id=uuid.uuid4(),
        org_id=org.id,
        uploaded_by=admin.id,
        filename="fine.md",
        storage_key=f"{org.id}/{uuid.uuid4()}/fine.md",
        file_size_bytes=10,
        status=DocumentStatus.READY,
    )
    db_session.add(document)
    db_session.commit()

    body = client.get(
        f"/api/v1/documents/{document.id}", headers=auth_headers(admin)
    ).json()

    assert body["failure_code"] is None
