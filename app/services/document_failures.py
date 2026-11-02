"""Turn a raw ingestion failure into something a user can act on.

`documents.error_message` holds whatever the provider raised. That text
is useful to an operator and useless to the person who uploaded a PDF:

    transient provider error 429: Client error '429 Too Many Requests'
    for url 'https://generativelanguage.googleapis.com/v1beta/models/
    gemini-embedding-001:batchEmbedContents' ...

It was being rendered verbatim on the Documents page, which is wrong
twice over. It tells the reader nothing they can do, and it discloses
the embedding provider, the model name and the upstream URL -- the
phase skill's rule is that user-facing errors never include backend
URLs or internal ids.

So the raw text STAYS in the database and the logs, where operators
want it, and the API publishes a classification instead. The sentence
shown to a person is then the UI's business, which is where product
copy belongs -- and a fixed enum cannot carry a URL into a response by
accident.

Classification is pattern-based rather than structured because it also
has to work on rows ALREADY in the database, written before this
existed. A new column would have left every existing failure blank.
"""
import enum
import re


class FailureCode(str, enum.Enum):
    """Why a document could not be processed, in terms a user can act on.

    Deliberately coarse. Each member has to map to a distinct ACTION --
    wait and retry, convert the file, pick a different file, contact an
    admin. Codes that imply the same action would just be detail
    pretending to be useful.
    """

    #: The provider throttled us. Resolves by waiting; the file is fine.
    RATE_LIMITED = "rate_limited"
    #: The provider did not answer in time. Also resolves by retrying.
    TIMEOUT = "timeout"
    #: The provider was down or unreachable.
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    #: The file opened but yielded no text -- a scan, or a pure diagram.
    #: Distinct from UNREADABLE because the fix differs: this needs OCR
    #: or a text version, not a repaired file.
    NO_TEXT = "no_text"
    #: The file could not be opened at all.
    UNREADABLE = "unreadable"
    #: Not a format the extractor handles.
    UNSUPPORTED_TYPE = "unsupported_type"
    #: Nothing above matched. Honest, rather than a plausible guess.
    UNKNOWN = "unknown"


# Order matters: the first match wins, so the specific patterns come
# before the general ones. A 429 is also an HTTP client error, and a
# timeout message can mention a connection -- checking in this order
# keeps the more actionable reading.
_PATTERNS: tuple[tuple[FailureCode, re.Pattern[str]], ...] = (
    (
        FailureCode.RATE_LIMITED,
        re.compile(r"\b429\b|too many requests|rate[ _-]?limit|quota exceeded", re.I),
    ),
    (
        FailureCode.TIMEOUT,
        re.compile(r"\btimed? ?out\b|timeout|\b408\b", re.I),
    ),
    (
        FailureCode.NO_TEXT,
        re.compile(
            r"no extractable text|no text|produced 0 chunks|0 chunks|"
            r"\bis empty\b|empty document",
            re.I,
        ),
    ),
    (
        FailureCode.UNSUPPORTED_TYPE,
        re.compile(r"unsupported (file )?type|unsupported extension", re.I),
    ),
    (
        FailureCode.UNREADABLE,
        re.compile(
            r"cannot open|filedataerror|corrupt|damaged|not a (valid )?pdf|"
            r"failed to (open|parse|extract)",
            re.I,
        ),
    ),
    (
        FailureCode.PROVIDER_UNAVAILABLE,
        re.compile(
            r"\b5\d\d\b|service unavailable|connecterror|connection refused|"
            r"connection error|name or service not known|temporarily unavailable",
            re.I,
        ),
    ),
)


def classify_failure(error_message: str | None) -> FailureCode:
    """Map a raw ingestion error onto a user-facing code.

    Returns UNKNOWN for anything unrecognised rather than guessing.
    Telling someone "the provider was busy, try again" when the real
    cause was a corrupt file sends them round a loop they cannot exit.
    """
    if not error_message or not error_message.strip():
        return FailureCode.UNKNOWN

    for code, pattern in _PATTERNS:
        if pattern.search(error_message):
            return code
    return FailureCode.UNKNOWN
