"""GET /api/v1/usage -- what this org consumed, admin only.

Admin-gated because usage is commercially sensitive inside a tenant as
well as between them: it exposes how heavily the org uses the product,
which users drive it, and (through refusal counts) how often the corpus
fails to answer. That is an owner's view, not a member's.

Org-scoped by the TOKEN. There is no org_id parameter on this endpoint
at all -- not an optional one, not an admin-only one. The scope comes
from `require_admin` -> `get_current_user` -> the verified JWT, so
"usage for another org" is not a request this API can express.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.api.limits import limit_default
from app.db.session import get_db
from app.models.user import User
from app.schemas.usage import UsageDay, UsageResponse
from app.services import usage_service

router = APIRouter(prefix="/usage", tags=["usage"])

_SUMMABLE = (
    "requests",
    "embedding_tokens",
    "llm_input_tokens",
    "llm_output_tokens",
    "rerank_calls",
    "cache_hits",
    "ok",
    "refused",
    "errored",
    "rejected",
)


@router.get("", response_model=UsageResponse, dependencies=[Depends(limit_default)])
def get_usage(
    days: int = Query(default=30, ge=1, le=90),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    series = usage_service.daily_totals(
        db, org_id=current_user.org_id, days=days
    )

    totals = None
    if series:
        summed = {name: sum(row[name] for row in series) for name in _SUMMABLE}
        latencies = [
            row["avg_latency_ms"] for row in series if row["avg_latency_ms"] is not None
        ]
        totals = UsageDay(
            # The most recent day in the window. A total row needs a
            # date for the response model, and inventing today's date
            # when today has no rows would report a day that does not
            # exist in the series.
            day=series[0]["day"],
            avg_latency_ms=(
                # Mean of daily means, which is NOT the mean latency --
                # it weights a quiet day equally with a busy one. Stated
                # rather than silently wrong: a true mean needs the
                # request counts as weights, and that belongs in a
                # proper analytics query, not in a summary row.
                int(sum(latencies) / len(latencies)) if latencies else None
            ),
            **summed,
        )

    return UsageResponse(
        days=days,
        totals=totals,
        series=[UsageDay(**row) for row in series],
    )
