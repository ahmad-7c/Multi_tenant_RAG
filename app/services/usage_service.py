"""Writing and reading usage events.

## The one hard rule: metering must never fail a request

A usage row is a byproduct. If the insert fails -- a dead connection, a
constraint nobody anticipated, a migration not yet applied in one
environment -- the user's answer has already been generated and
streamed, and raising at that point would turn a bookkeeping failure
into a lost answer the tenant already paid for in provider cost.

So `record()` swallows every exception and logs once. That is the
opposite of the usual rule, and it is deliberate: the cost of a missing
row is an under-reported invoice, and the cost of a raised exception is
a broken product.

## Why it must not slow the request either

The write happens AFTER the stream is finished, on a worker thread with
its own short session, in the same place `persist_turn` already runs.
Not in the request path, and not holding a session across the stream --
the project's standing rule.

A background queue would be better still at high volume (one row per
request is one INSERT per request), and is deliberately not built: the
request has already ended, so a thread-pool insert costs the user
nothing, and an in-process queue introduces a window where a worker
restart loses rows while making the code harder to reason about. The
note is here so the next person does not have to rediscover the
trade-off.

## Why the endpoint dimension is a logical name

`endpoint="answer"`, not `"/api/v1/conversations/{id}/messages"`. The
path changes when the API is versioned; a metering table that renames
its own dimension on a route change cannot be summed across the change.
"""
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.usage_event import UsageEvent, UsageStatus

logger = logging.getLogger(__name__)

# Logical endpoint names. Constants rather than string literals at call
# sites, because a typo ("answers" vs "answer") splits one series into
# two and nothing reports an error.
ENDPOINT_ANSWER = "answer"
ENDPOINT_UPLOAD = "upload"


def record(
    db: Session,
    *,
    org_id: uuid.UUID,
    endpoint: str,
    status: UsageStatus,
    user_id: Optional[uuid.UUID] = None,
    embedding_tokens: Optional[int] = None,
    llm_input_tokens: Optional[int] = None,
    llm_output_tokens: Optional[int] = None,
    rerank_calls: Optional[int] = None,
    cache_hit: Optional[bool] = None,
    latency_ms: Optional[int] = None,
) -> Optional[UsageEvent]:
    """Insert one usage row. Never raises.

    Returns the row on success and None on failure, so a caller that
    wants to assert the write happened can -- without any caller being
    able to break a request by forgetting to handle an exception.
    """
    event = UsageEvent(
        id=uuid.uuid4(),
        org_id=org_id,
        user_id=user_id,
        endpoint=endpoint,
        embedding_tokens=embedding_tokens,
        llm_input_tokens=llm_input_tokens,
        llm_output_tokens=llm_output_tokens,
        rerank_calls=rerank_calls,
        cache_hit=cache_hit,
        latency_ms=latency_ms,
        status=status,
    )
    try:
        db.add(event)
        db.commit()
        return event
    except Exception:
        logger.warning(
            "could not record usage for org %s on %s; the request is "
            "unaffected and this interval will under-report",
            org_id,
            endpoint,
            exc_info=True,
        )
        try:
            db.rollback()
        except Exception:
            # A session too broken to roll back is about to be closed by
            # the caller's finally. Nothing useful left to do.
            pass
        return None


def daily_totals(
    db: Session, *, org_id: uuid.UUID, days: int = 30
) -> list[dict]:
    """Per-day totals for ONE org, most recent day first.

    `org_id` is a required keyword argument and is interpolated into the
    WHERE clause of the query itself, not applied to the result. A
    fetch-then-filter version of this function is a cross-tenant leak
    one `if` away, and the `if` is in Python where no constraint can
    enforce it.

    Dates are grouped in UTC. A tenant in UTC+13 will see a "day" that
    is not their local day, which is the correct trade for v1: the
    provider quota this is metering resets on the provider's clock, and
    a per-tenant timezone would make the global total a sum over
    overlapping windows.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    day = func.date_trunc("day", UsageEvent.created_at)

    rows = db.execute(
        select(
            day.label("day"),
            func.count().label("requests"),
            func.coalesce(func.sum(UsageEvent.embedding_tokens), 0).label(
                "embedding_tokens"
            ),
            func.coalesce(func.sum(UsageEvent.llm_input_tokens), 0).label(
                "llm_input_tokens"
            ),
            func.coalesce(func.sum(UsageEvent.llm_output_tokens), 0).label(
                "llm_output_tokens"
            ),
            func.coalesce(func.sum(UsageEvent.rerank_calls), 0).label("rerank_calls"),
            # Counted, not summed: cache_hit is a nullable boolean and
            # SUM over booleans is not portable. This also reports
            # "known hits" rather than treating NULL as false.
            func.count(UsageEvent.id)
            .filter(UsageEvent.cache_hit.is_(True))
            .label("cache_hits"),
            func.count(UsageEvent.id)
            .filter(UsageEvent.status == UsageStatus.OK)
            .label("ok"),
            func.count(UsageEvent.id)
            .filter(UsageEvent.status == UsageStatus.REFUSED)
            .label("refused"),
            func.count(UsageEvent.id)
            .filter(UsageEvent.status == UsageStatus.ERROR)
            .label("errored"),
            func.count(UsageEvent.id)
            .filter(UsageEvent.status == UsageStatus.REJECTED)
            .label("rejected"),
            # Average, not p95: a single mean is what one row per day
            # can honestly support, and a percentile computed from a
            # daily aggregate is not a percentile.
            func.avg(UsageEvent.latency_ms).label("avg_latency_ms"),
        )
        .where(UsageEvent.org_id == org_id)
        .where(UsageEvent.created_at >= since)
        .group_by(day)
        .order_by(day.desc())
    ).all()

    return [
        {
            "day": row.day.date() if isinstance(row.day, datetime) else row.day,
            "requests": int(row.requests),
            "embedding_tokens": int(row.embedding_tokens),
            "llm_input_tokens": int(row.llm_input_tokens),
            "llm_output_tokens": int(row.llm_output_tokens),
            "rerank_calls": int(row.rerank_calls),
            "cache_hits": int(row.cache_hits),
            "ok": int(row.ok),
            "refused": int(row.refused),
            "errored": int(row.errored),
            "rejected": int(row.rejected),
            "avg_latency_ms": (
                int(row.avg_latency_ms) if row.avg_latency_ms is not None else None
            ),
        }
        for row in rows
    ]
