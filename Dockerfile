# syntax=docker/dockerfile:1
#
# DocuMind API and worker. One image, two entrypoints: the worker must
# run the IDENTICAL code the task was defined with, or Celery's task
# name resolves to a different function body than the producer meant.
#
# Python 3.14 matches the version the test suite runs on. The previous
# Dockerfile said 3.12 while the pins had been raised specifically for
# cp314 wheels (and SQLAlchemy 2.0.35 crashed on `Mapped[Optional[...]]`
# under 3.14's typing internals) -- so the image was running a version
# the code had never been tested against. Checked before choosing:
# every one of the 22 pins has a cp314 x86_64 wheel or is pure Python,
# so nothing compiles from source.

# =========================================================== base ====
FROM python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /code

# =========================================================== deps ====
# Its own stage so the dependency layer is cached on requirements.txt
# alone. Editing application code must not reinstall 22 packages.
FROM base AS deps

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# =========================================================== test ====
# NOT part of the runtime image. Built and run in CI (and at D1) to
# execute the suite against the EXACT interpreter and wheels that will
# serve production -- which is the only way "the tests pass" says
# anything about the artefact being shipped.
FROM deps AS test

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini pytest.ini ./
COPY tests ./tests
COPY eval ./eval

# Placeholders so Settings can construct. The suite overrides both: a
# conftest fixture forces fake providers and refuses to run unless
# TEST_DATABASE_URL is set and differs from DATABASE_URL.
ENV DATABASE_URL="postgresql://unused:unused@127.0.0.1:1/unused" \
    JWT_SECRET_KEY="test-only-not-a-real-secret"

# `python -m pytest`, NOT the bare `pytest` console script.
# python -m puts the CWD (/code) on sys.path; the console script
# does not, so `import app` fails and the suite dies loading
# conftest. Locally this was invisible because every local run is
# already `python -m pytest`. Caught the first time the suite was
# actually run inside the image.
CMD ["python", "-m", "pytest", "-q"]

# ======================================================== runtime ====
FROM base AS runtime

# Installed site-packages only -- not pip's cache, not the build
# context. `tests/` and `eval/` are deliberately absent from this
# stage: they are in the build context so the test stage can use them,
# and kept out of the artefact that ships.
COPY --from=deps /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# Non-root. UID is fixed so the named `uploads` volume can be chowned
# to a known owner -- Docker creates a volume owned by root on first
# use, and a non-root process would then fail its first write.
RUN groupadd --gid 10001 documind \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin documind

COPY --chown=10001:10001 app ./app
COPY --chown=10001:10001 alembic ./alembic
COPY --chown=10001:10001 alembic.ini ./

# tiktoken's vocabulary, BAKED AT BUILD TIME.
#
# chunking.count_tokens calls tiktoken.get_encoding("cl100k_base"), and
# tiktoken downloads that vocabulary on FIRST USE, with
# `requests.get("https://openaipublic.blob.core.windows.net/...")`. So a
# container with no egress died inside chunk_pages -> count_tokens
# before reaching an embedding call -- which is exactly what happened on
# the server, and it presented as a failed ingest with no provider in
# the traceback.
#
# Baking it is better than granting egress even now that egress exists:
# it removes a third-party host from the runtime dependency list
# entirely, so an Azure blob outage cannot stop a document being
# chunked. The vocabulary is a fixed artefact, not live data.
#
# TIKTOKEN_CACHE_DIR is set with ENV, not just for this RUN, because it
# has to be present at RUNTIME too -- without it tiktoken looks in a
# temp directory, finds nothing, and downloads again.
ENV TIKTOKEN_CACHE_DIR=/code/.tiktoken

# cl100k_base is the one production uses: EMBEDDING_MODEL is
# gemini-embedding-001, which encoding_for_model does not know, so
# _encoding_for falls through to it. o200k_base costs little and covers
# anyone who points EMBEDDING_MODEL at a current OpenAI model.
RUN mkdir -p "$TIKTOKEN_CACHE_DIR" && python - <<'PY'
import tiktoken

for name in ("cl100k_base", "o200k_base"):
    enc = tiktoken.get_encoding(name)
    # Exercise it, rather than trusting that construction downloaded
    # anything. A registry entry resolves without fetching the blob.
    assert enc.encode("documind"), name
    print(f"cached {name}")
PY

# The download must have produced files. A silent failure here would
# ship an empty cache directory and move the fetch back to runtime,
# which is the bug this step exists to remove -- and it would only
# surface on a box with no egress.
RUN test -n "$(ls -A "$TIKTOKEN_CACHE_DIR")" \
 && chown -R 10001:10001 "$TIKTOKEN_CACHE_DIR"

# PROVE it loads with no network, at build time.
#
# `requests.get` is monkeypatched to raise before tiktoken is imported,
# so any attempt to fetch fails the build instead of silently working
# because the builder happened to have egress. (`RUN --network=none`
# would be stronger still, but it is not available on every BuildKit
# version and a build that fails on an unsupported flag is worse than
# one that proves the property a different way.)
RUN python - <<'PY'
import requests


def _blocked(*args, **kwargs):
    raise AssertionError(f"tiktoken tried to download: {args!r}")


requests.get = _blocked
requests.Session.get = _blocked

import tiktoken

assert len(tiktoken.get_encoding("cl100k_base").encode("hello world")) == 2
print("tiktoken cl100k_base loads with the network blocked")
PY

# The mount point, pre-created and owned, so the volume inherits the
# ownership instead of arriving as root-owned and unwritable.
RUN mkdir -p /code/uploads && chown 10001:10001 /code/uploads

USER 10001:10001

EXPOSE 8000

# Hits /health/ready, NOT /health -- the difference is load-bearing.
#
# /health reports "ok" with the database unreachable, and that is
# correct for its job: a load balancer must not drop a working
# instance because the reranker breaker tripped. But a HEALTHCHECK
# pointed at it passes while every authenticated route returns 500
# (measured: /health 200 and POST /auth/login 500, same process), so
# `depends_on: service_healthy` would admit a database-less api and
# the deploy's automatic rollback would never fire. /health/ready
# round-trips SELECT 1 and answers 503 when it cannot.
#
# Python rather than curl: curl is not in the slim image and adding
# it would mean shipping a general-purpose HTTP client to production.
# http.client, not urllib: urllib RAISES on the 503 and the check
# would then fail by traceback rather than by decision. http.client
# returns the response whatever its status, so 503 and 200 are
# compared the same way, and a refused connection still exits
# non-zero -- which is the right answer when the port is dead.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import http.client,sys;c=http.client.HTTPConnection('127.0.0.1',8000,timeout=4);c.request('GET','/health/ready');sys.exit(0 if c.getresponse().status==200 else 1)"]

# No --reload, and a single worker: concurrency comes from running more
# containers, not more processes inside one memory limit.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
