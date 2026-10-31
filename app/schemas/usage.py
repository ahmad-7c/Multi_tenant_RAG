from datetime import date
from typing import Optional

from pydantic import BaseModel


class UsageDay(BaseModel):
    """One day of usage for one org.

    No org_id field. Not an omission: the endpoint is org-scoped by the
    token, so echoing the org id back would add nothing a caller does
    not know and would make a response that leaked another org's rows
    look plausible instead of obviously wrong.
    """

    day: date
    requests: int
    embedding_tokens: int
    llm_input_tokens: int
    llm_output_tokens: int
    rerank_calls: int
    cache_hits: int
    ok: int
    refused: int
    errored: int
    rejected: int
    # None when no row in that day recorded a latency. Not 0: a day of
    # rejected requests has no latency to average, and reporting 0ms
    # would read as impossibly fast rather than as absent.
    avg_latency_ms: Optional[int] = None


class UsageResponse(BaseModel):
    days: int
    totals: UsageDay | None = None
    series: list[UsageDay]
