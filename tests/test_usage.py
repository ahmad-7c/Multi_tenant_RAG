"""Usage metering: org scoping, the never-fail rule, the endpoint.

Two properties carry the weight here.

**Org scoping** is a tenancy requirement, so it is tested as a leak
would appear: a second org's rows are written and the first org's query
must not see them, with the filter inside the SQL rather than applied to
a result set.

**Metering must never fail a request.** A usage row is a byproduct of
work the user has already received; raising at that point turns a
bookkeeping failure into a lost answer that was already paid for in
provider cost. That inverts the usual rule, so it needs a test that
shows the inversion is deliberate rather than an oversight.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sql_text

from app.models.usage_event import UsageEvent, UsageStatus
from app.models.user import UserRole
from app.services import usage_service


def _record(db, org, **kwargs):
    kwargs.setdefault("endpoint", usage_service.ENDPOINT_ANSWER)
    kwargs.setdefault("status", UsageStatus.OK)
    return usage_service.record(db, org_id=org.id, **kwargs)


# ------------------------------------------------------------ the write --


def test_a_usage_row_round_trips(db_session, org_factory, user_factory):
    org = org_factory()
    user = user_factory(org)
    event = _record(
        db_session,
        org,
        user_id=user.id,
        llm_input_tokens=1200,
        llm_output_tokens=180,
        rerank_calls=1,
        cache_hit=False,
        latency_ms=2450,
    )
    assert event is not None
    db_session.refresh(event)
    assert event.status == UsageStatus.OK
    assert event.llm_input_tokens == 1200
    assert event.created_at is not None


def test_every_status_round_trips_through_postgres(db_session, org_factory):
    org = org_factory()
    for status in UsageStatus:
        event = _record(db_session, org, status=status)
        assert event is not None
        db_session.refresh(event)
        # The ORM must send the VALUE ('ok'), not the NAME ('OK'). This
        # is the bug that made every user and document insert fail in
        # the Phase 1 audit, and SQLite would have hidden it again.
        assert event.status == status


def test_unknown_tokens_stay_null_rather_than_zero(db_session, org_factory):
    org = org_factory()
    # A cache hit really spent no LLM tokens (a real zero); a provider
    # that returned no usage block spent an unknown number (NULL).
    # Flattening the second into the first makes a cost report quietly
    # too low, and nothing ever says so.
    unknown = _record(db_session, org, cache_hit=False)
    db_session.refresh(unknown)
    assert unknown.llm_input_tokens is None

    cached = _record(db_session, org, cache_hit=True, llm_input_tokens=0)
    db_session.refresh(cached)
    assert cached.llm_input_tokens == 0


def test_a_failed_insert_returns_none_instead_of_raising(
    db_session, org_factory, monkeypatch
):
    org = org_factory()

    def explode():
        raise RuntimeError("connection reset")

    monkeypatch.setattr(db_session, "commit", explode)
    # The answer has already been streamed to the user. Raising here
    # would turn a bookkeeping failure into a lost answer that the
    # provider was already paid for.
    assert _record(db_session, org) is None


def test_an_unattributable_row_is_refused_by_the_database(db_session):
    # org_id is NOT NULL per the project's first non-negotiable. An
    # unattributed usage row is a cost nobody pays, which is worse here
    # than a nullable column is elsewhere.
    event = UsageEvent(
        id=uuid.uuid4(),
        org_id=None,
        endpoint="answer",
        status=UsageStatus.OK,
    )
    db_session.add(event)
    with pytest.raises(Exception):
        db_session.flush()
    db_session.rollback()


# ------------------------------------------------------------- the read --


def test_daily_totals_are_scoped_to_one_org(db_session, org_factory):
    mine, theirs = org_factory(), org_factory()
    for _ in range(3):
        _record(db_session, mine, llm_input_tokens=100, rerank_calls=1)
    for _ in range(5):
        _record(db_session, theirs, llm_input_tokens=999, rerank_calls=1)

    rows = usage_service.daily_totals(db_session, org_id=mine.id, days=30)
    assert len(rows) == 1
    assert rows[0]["requests"] == 3
    # 300, not 5295. The filter is in the WHERE clause of the query, not
    # applied to a result set in Python where no constraint can enforce
    # it.
    assert rows[0]["llm_input_tokens"] == 300


def test_daily_totals_groups_by_day_most_recent_first(db_session, org_factory):
    org = org_factory()
    now = datetime.now(timezone.utc)
    for offset, count in ((0, 2), (1, 3), (5, 1)):
        for _ in range(count):
            event = _record(db_session, org)
            db_session.execute(
                sql_text(
                    "UPDATE usage_events SET created_at = :ts WHERE id = :id"
                ),
                {"ts": now - timedelta(days=offset), "id": str(event.id)},
            )
    db_session.commit()

    rows = usage_service.daily_totals(db_session, org_id=org.id, days=30)
    assert [r["requests"] for r in rows] == [2, 3, 1]
    assert rows[0]["day"] > rows[1]["day"] > rows[2]["day"]


def test_daily_totals_respects_the_window(db_session, org_factory):
    org = org_factory()
    old = _record(db_session, org)
    db_session.execute(
        sql_text("UPDATE usage_events SET created_at = :ts WHERE id = :id"),
        {"ts": datetime.now(timezone.utc) - timedelta(days=40), "id": str(old.id)},
    )
    _record(db_session, org)
    db_session.commit()

    assert len(usage_service.daily_totals(db_session, org_id=org.id, days=7)) == 1
    assert len(usage_service.daily_totals(db_session, org_id=org.id, days=60)) == 2


def test_status_counts_are_broken_out(db_session, org_factory):
    org = org_factory()
    _record(db_session, org, status=UsageStatus.OK)
    _record(db_session, org, status=UsageStatus.OK)
    _record(db_session, org, status=UsageStatus.REFUSED)
    _record(db_session, org, status=UsageStatus.ERROR)
    _record(db_session, org, status=UsageStatus.REJECTED)

    row = usage_service.daily_totals(db_session, org_id=org.id, days=1)[0]
    # Kept apart because they mean different things to an operator:
    # "we refuse 20% of questions" is a corpus problem and "the
    # provider drops 20% of calls" is an infrastructure one.
    assert (row["ok"], row["refused"], row["errored"], row["rejected"]) == (
        2,
        1,
        1,
        1,
    )


def test_cache_hits_are_counted_from_the_boolean_not_from_zero_tokens(
    db_session, org_factory
):
    org = org_factory()
    _record(db_session, org, cache_hit=True)
    _record(db_session, org, cache_hit=False)
    _record(db_session, org, cache_hit=None)

    row = usage_service.daily_totals(db_session, org_id=org.id, days=1)[0]
    # 1: only the explicit True. NULL is "unknown", and counting it as a
    # hit would overstate how much the cache is saving.
    assert row["cache_hits"] == 1


def test_an_org_with_no_usage_gets_an_empty_series(db_session, org_factory):
    assert usage_service.daily_totals(db_session, org_id=org_factory().id) == []


# --------------------------------------------------------- the endpoint --


def test_the_usage_endpoint_is_admin_only(
    client, db_session, org_factory, user_factory, auth_headers
):
    org = org_factory()
    member = user_factory(org, role=UserRole.MEMBER)
    _record(db_session, org)

    response = client.get("/api/v1/usage", headers=auth_headers(member))
    # Usage is commercially sensitive inside a tenant as well as
    # between them: it shows how heavily the org uses the product and
    # how often its corpus fails to answer.
    assert response.status_code == 403


def test_an_admin_sees_their_own_orgs_usage(
    client, db_session, org_factory, user_factory, auth_headers
):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)
    for _ in range(2):
        _record(db_session, org, llm_input_tokens=50, llm_output_tokens=10)

    body = client.get("/api/v1/usage", headers=auth_headers(admin)).json()
    assert body["days"] == 30
    assert body["totals"]["requests"] == 2
    assert body["totals"]["llm_input_tokens"] == 100
    assert len(body["series"]) == 1


def test_the_usage_endpoint_never_shows_another_org(
    client, db_session, org_factory, user_factory, auth_headers
):
    mine, theirs = org_factory(), org_factory()
    admin = user_factory(mine, role=UserRole.ADMIN)
    _record(db_session, mine, llm_input_tokens=1)
    for _ in range(10):
        _record(db_session, theirs, llm_input_tokens=1000)

    body = client.get("/api/v1/usage", headers=auth_headers(admin)).json()
    assert body["totals"]["requests"] == 1
    assert body["totals"]["llm_input_tokens"] == 1


def test_there_is_no_way_to_ask_for_another_orgs_usage(
    client, db_session, org_factory, user_factory, auth_headers
):
    mine, theirs = org_factory(), org_factory()
    admin = user_factory(mine, role=UserRole.ADMIN)
    _record(db_session, theirs, llm_input_tokens=777)

    # An org_id parameter is not merely ignored -- it does not exist on
    # this endpoint, so "usage for another org" is not a request this
    # API can express. FastAPI drops unknown query params, so the
    # assertion is on the DATA, not on a 4xx.
    body = client.get(
        f"/api/v1/usage?org_id={theirs.id}", headers=auth_headers(admin)
    ).json()
    assert body["totals"] is None
    assert body["series"] == []


def test_an_admin_with_no_usage_gets_an_empty_response(
    client, org_factory, user_factory, auth_headers
):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)
    body = client.get("/api/v1/usage", headers=auth_headers(admin)).json()
    assert body["totals"] is None
    assert body["series"] == []


def test_the_window_is_bounded(client, org_factory, user_factory, auth_headers):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)
    # An unbounded `days` is an unbounded scan of a table that grows
    # with every request.
    assert client.get(
        "/api/v1/usage?days=5000", headers=auth_headers(admin)
    ).status_code == 422
    assert client.get(
        "/api/v1/usage?days=0", headers=auth_headers(admin)
    ).status_code == 422


def test_the_usage_endpoint_requires_a_token(client):
    assert client.get("/api/v1/usage").status_code == 401
