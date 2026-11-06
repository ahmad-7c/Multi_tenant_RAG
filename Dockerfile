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
