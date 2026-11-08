"""/health/ready exists because /health answered the wrong question.

The gap was measured, not imagined. With Postgres unreachable this
process reported:

    GET  /health            -> 200  {"status": "ok"}
    POST /api/v1/auth/login -> 500  Internal Server Error

Both are correct in isolation. /health is liveness plus quality
degradations and must stay "ok" through all of them, or a load
balancer drops a working instance over a tripped reranker breaker.
But every authenticated route begins with a query, so "ok" was the
only signal the system produced about the one condition that makes
the entire API useless -- and `app/main.py`'s lifespan waves the
unreachable database through with a comment that used to say "the
readiness probe will catch it" when no readiness probe existed.

These tests pin the SPLIT, not just the new endpoint. The reason
/health keeps saying "ok" here is deliberate, and a later edit
"fixing" it would reintroduce the outage its docstring warns about.
"""
import json
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from app.db.session import get_db
from app.main import app as fastapi_app

ROOT = Path(__file__).resolve().parents[1]

#: Stands in for the host/port/user a driver error carries. The point
#: of using something distinctive is that a leak is findable: a real
#: psycopg2 message embeds the DSN, and this endpoint is reachable
#: through the public edge.
DSN_IN_THE_ERROR = 'connection to server at "db.internal" port 5432 failed'


class _DeadSession:
    """A session whose first statement fails the way a dead DB fails.

    Not a Mock: the route calls exactly one method, and a stub that
    implements only that method fails loudly if the route ever starts
    doing something else behind the test's back.
    """

    def execute(self, *args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception(DSN_IN_THE_ERROR))


@pytest.fixture
def dead_db(client):
    """Point the real app's get_db at a database that cannot answer.

    Depends on `client` so the override lands AFTER conftest installs
    its own, and conftest's teardown still clears it.
    """
    fastapi_app.dependency_overrides[get_db] = lambda: iter([_DeadSession()])
    return client


def test_ready_is_200_when_the_database_answers(client):
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_is_503_when_the_database_is_unreachable(dead_db):
    """The whole point. 503, not 200 and not a 500 traceback."""
    response = dead_db.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "failed": ["database"]}


def test_ready_names_the_dependency_and_nothing_else(dead_db):
    """A driver error carries the host, port and user from the DSN.

    This endpoint is published through Caddy, so the response says
    which dependency failed and stops there; the detail belongs in the
    operator's log. Same rule as the user-facing failure codes, which
    exist because a 429 from Gemini was being shown to an uploader
    complete with the upstream URL.
    """
    body = dead_db.get("/health/ready").text
    assert "db.internal" not in body
    assert "5432" not in body
    assert "OperationalError" not in body
    assert "Traceback" not in body


def test_health_still_says_ok_when_the_database_is_unreachable(dead_db):
    """Deliberate, and pinned so nobody "fixes" it.

    /health is read by a load balancer to decide whether this instance
    stays in rotation. Making it fail on a dependency outage would pull
    every instance at once and turn a recoverable condition into a
    total outage, which is exactly the reasoning in its own docstring.
    Readiness is a separate question with a separate endpoint.
    """
    response = dead_db.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.repo  # reads the Dockerfile, which is not in the image
def test_the_container_healthcheck_targets_readiness_not_health():
    """The defect was never in Python -- it was which URL Docker polls.

    `depends_on: service_healthy` and the deploy's health gate both
    read the container healthcheck. Pointed at /health it reports
    success for an api that cannot reach its database, so P7's
    automatic rollback never fires on a broken migration or a wrong
    credential. Nothing else in the suite can see this line.
    """
    lines = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    # The CMD that belongs to the HEALTHCHECK, found by position rather
    # than by shape. There are two `CMD ["python" ...]` lines in this
    # file -- the test stage runs `python -m pytest` -- so matching on
    # the prefix alone picked up the wrong one and this test failed
    # against a correct Dockerfile.
    healthcheck = [
        lines[i + 1]
        for i, line in enumerate(lines)
        if line.startswith("HEALTHCHECK") and i + 1 < len(lines)
    ]
    assert len(healthcheck) == 1, "expected exactly one HEALTHCHECK"

    # Parsed as JSON rather than grepped: the exec form is JSON, and a
    # malformed array is silently treated as a shell string by Docker,
    # which would run a different command than this test inspects.
    args = json.loads(healthcheck[0].strip()[len("CMD "):])
    script = args[-1]

    assert "/health/ready" in script
    # The bare endpoint must not be what it polls. Checked by removing
    # the readiness path first, so "/health/ready" cannot satisfy a
    # naive "/health" substring search and hide a regression.
    assert "/health" not in script.replace("/health/ready", "")
