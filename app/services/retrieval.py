"""Org-scoped hybrid retrieval over child chunks.

Two independent retrievers run over the same rows and their RANKS are
fused:

  - **Vector leg** - pgvector cosine distance (`<=>`) against the HNSW
    index from migration 0004. Finds chunks that mean the same thing as
    the question but share no words with it.
  - **Keyword leg** - Postgres full-text search against the generated
    `content_tsv` column and its partial GIN index. Finds the exact
    identifier, product name, error code or acronym that an embedding
    smears into the general neighbourhood of "technical term".

Each leg fails in a way the other covers, which is the entire argument for
running both.

Both legs filter by `org_id`, both restrict to children
(`parent_id IS NOT NULL`), and neither can return a parent.
"""
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings

logger = logging.getLogger(__name__)


# SET LOCAL, never SET. These are connection-level GUCs and the connection
# comes from a pool: a plain SET would outlive the request and silently
# change search behaviour for whatever unrelated request picked up that
# connection next. SET LOCAL is rolled back with the transaction.
SEARCH_SETTINGS_SQL = (
    "SET LOCAL hnsw.ef_search = :ef_search",
    "SET LOCAL hnsw.iterative_scan = :iterative_scan",
    "SET LOCAL hnsw.max_scan_tuples = :max_scan_tuples",
)


@dataclass(frozen=True)
class Candidate:
    """One retrieved child chunk, with everything a citation needs.

    Fetched in the search query itself rather than looked up afterwards:
    the alternative is an N+1 over 50 candidates on the hot query path.
    """

    chunk_id: uuid.UUID
    parent_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    page_number: int
    content: str
    token_count: int
    rrf_score: float
    vector_rank: int | None
    text_rank: int | None
    # Cosine similarity from the vector leg, or None when only the
    # keyword leg returned this chunk. Carried because it is the ONLY
    # calibrated relevance signal that survives the reranker being
    # down: RRF scores are rank reciprocals, so the confidence gate has
    # nothing else to threshold in that regime. Free to collect -- the
    # vector leg already computes this distance to order by.
    vector_similarity: float | None = None


def apply_search_settings(db: Session) -> None:
    """Configure HNSW search for this transaction.

    `hnsw.iterative_scan` is the important one. An HNSW index scan is NOT
    filtered: pgvector walks the graph for the nearest `ef_search`
    candidates across the *whole table*, and `WHERE org_id = ?` is applied
    to that result afterwards. For a small tenant in a large shared table
    the globally-nearest neighbours can be almost entirely another org's
    rows, so the query returns far fewer than asked for -- possibly zero --
    while the index reports a perfectly healthy scan.

    Note what that is NOT: it is not a tenant leak. The filter is applied,
    so no cross-org row is ever returned. It is a silent recall loss, which
    is worse in one specific way: nothing errors, and answers just quietly
    get worse for small tenants as the table grows.

    With iterative scan, pgvector resumes the graph walk when the filter
    discards too many candidates, until it has enough rows or reaches
    `max_scan_tuples`. `relaxed_order` rather than `strict_order` because
    RRF re-ranks everything downstream anyway -- paying for globally
    perfect distance ordering inside the vector leg buys nothing once the
    ranks are fused with a second retriever and then reranked.

    Requires pgvector >= 0.8. On an older server these GUCs do not exist
    and the SET fails; that is logged and tolerated rather than fatal,
    because degraded recall still answers questions while a hard failure
    answers none.
    """
    params = {
        "ef_search": settings.HNSW_EF_SEARCH,
        "iterative_scan": settings.HNSW_ITERATIVE_SCAN,
        "max_scan_tuples": settings.HNSW_MAX_SCAN_TUPLES,
    }
    for statement in SEARCH_SETTINGS_SQL:
        try:
            # SET LOCAL cannot take a bind parameter, so the value is
            # interpolated -- safe here because every value comes from
            # Settings and is coerced to int / checked against a fixed set
            # below, never from a request.
            key = statement.split(":")[1]
            value = params[key]
            if key == "iterative_scan":
                if value not in ("off", "strict_order", "relaxed_order"):
                    raise ValueError(f"invalid hnsw.iterative_scan: {value!r}")
                literal = f"'{value}'"
            else:
                literal = str(int(value))
            db.execute(text(statement.split(" = ")[0] + " = " + literal))
        except Exception as exc:  # pragma: no cover - old pgvector only
            logger.warning(
                "could not apply %s (%s). Vector recall under the tenant "
                "filter will be degraded; pgvector >= 0.8 is required.",
                statement,
                exc,
            )


# Vector leg. The ORDER BY uses `<=>` so the planner can use the HNSW
# index built WITH vector_cosine_ops -- any other operator silently
# degrades to a sequential scan that still returns correct-looking rows.
# `embedding IS NOT NULL` is repeated from the index predicate so the
# planner can prove the partial index applies.
_VECTOR_SQL = text(
    """
    SELECT id,
           -- `<=>` is cosine DISTANCE under vector_cosine_ops, so
           -- similarity is 1 - distance. Selected rather than
           -- recomputed in Python: this is the exact number the index
           -- ordered by, and a second computation could disagree with
           -- it through float rounding.
           1 - (embedding <=> CAST(:query_embedding AS vector)) AS similarity
    FROM document_chunks
    WHERE org_id = :org_id
      AND parent_id IS NOT NULL
      AND embedding IS NOT NULL
    ORDER BY embedding <=> CAST(:query_embedding AS vector)
    LIMIT :per_leg
    """
)

# Keyword leg. websearch_to_tsquery, NOT to_tsquery or plainto_tsquery:
# the input is raw end-user text and websearch_to_tsquery is the only one
# that never raises on malformed input (unbalanced quotes, stray operators,
# bare AND/OR). to_tsquery would turn a user typing `receipts & |` into a
# 500.
#
# ts_rank is NOT BM25. It has no document-length normalisation worth the
# name, no IDF across the corpus, and its weights are positional rather
# than frequency-saturating. It is "a reasonable lexical ordering", not a
# principled relevance score -- which is a further reason the merge fuses
# RANKS and never these numbers.
_TEXT_SQL = text(
    """
    SELECT id
    FROM document_chunks
    WHERE org_id = :org_id
      AND parent_id IS NOT NULL
      AND content_tsv @@ websearch_to_tsquery(:ts_config, :query_text)
    ORDER BY ts_rank(content_tsv, websearch_to_tsquery(:ts_config, :query_text)) DESC,
             id ASC
    LIMIT :per_leg
    """
)

# Hydration is a separate indexed-by-id query rather than a join inside
# each leg: the legs should do as little as possible, and this way the
# columns are fetched exactly once for the merged set instead of twice.
_HYDRATE_SQL = text(
    """
    SELECT c.id, c.parent_id, c.document_id, d.filename,
           c.page_number, c.content, c.token_count
    FROM document_chunks c
    JOIN documents d ON d.id = c.document_id AND d.org_id = c.org_id
    WHERE c.org_id = :org_id AND c.id = ANY(:ids)
    """
)


def reciprocal_rank_fusion(
    ranked_lists: list[list[uuid.UUID]], *, k: int | None = None
) -> list[tuple[uuid.UUID, float]]:
    """Fuse several ranked lists by RANK, not by score.

    score(d) = sum over lists of 1 / (k + rank(d))

    Why rank and not score: the two legs produce numbers that are not
    comparable and not even on the same kind of scale. Cosine similarity
    is bounded in [-1, 1]; `ts_rank` is unbounded and varies with term
    frequency and document length, so there is no constant you can divide
    by to normalise it. Any weighted sum therefore needs a per-corpus fudge
    factor that every new ingestion silently invalidates. Rank throws the
    magnitudes away and keeps the only thing both retrievers agree on: the
    ordering.

    `k` damps the influence of top ranks. At k=60 (the value from the
    original Cormack et al. paper) rank 1 scores 1/61 and rank 10 scores
    1/70 -- close enough that two retrievers agreeing a document is
    top-ten outweighs one retriever putting it first.
    """
    k = settings.RRF_K if k is None else k
    scores: dict[uuid.UUID, float] = {}
    # Insertion order is the tie-break, so equal scores do not reorder
    # between calls -- otherwise cache keys and pagination would be
    # unstable.
    for ranked in ranked_lists:
        for position, chunk_id in enumerate(ranked, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + position)
    return sorted(scores.items(), key=lambda item: -item[1])


def _ranks(ranked: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    return {chunk_id: position for position, chunk_id in enumerate(ranked, start=1)}


def retrieve_candidates(
    db: Session,
    *,
    org_id: uuid.UUID,
    query_text: str,
    query_embedding: list[float],
    limit: int | None = None,
) -> list[Candidate]:
    """The single org-scoped retrieval path.

    One function, so there is exactly one place where the tenant filter
    could be forgotten. Both legs run in the same transaction as
    `apply_search_settings`, because SET LOCAL is transaction-scoped.
    """
    limit = settings.RETRIEVE_CANDIDATES if limit is None else limit
    per_leg = max(settings.RETRIEVE_PER_LEG, limit)

    apply_search_settings(db)

    # pgvector accepts a vector literal as text; building it here rather
    # than relying on the driver keeps this independent of whether the
    # session has pgvector's type adapters registered.
    vector_literal = "[" + ",".join(repr(float(v)) for v in query_embedding) + "]"

    vector_rows = db.execute(
        _VECTOR_SQL,
        {
            "org_id": str(org_id),
            "query_embedding": vector_literal,
            "per_leg": per_leg,
        },
    ).all()
    vector_ids = [row[0] for row in vector_rows]
    # Keyed by chunk id so a chunk the keyword leg also returned keeps
    # its similarity, and one only the keyword leg found stays None
    # rather than being given a number the vector leg never produced.
    vector_similarity = {row[0]: float(row[1]) for row in vector_rows}

    text_ids: list[uuid.UUID] = []
    if query_text and query_text.strip():
        text_ids = [
            row[0]
            for row in db.execute(
                _TEXT_SQL,
                {
                    "org_id": str(org_id),
                    "ts_config": settings.TS_CONFIG,
                    "query_text": query_text,
                    "per_leg": per_leg,
                },
            ).all()
        ]

    merged = reciprocal_rank_fusion([vector_ids, text_ids])[:limit]
    if not merged:
        return []

    vector_rank, text_rank = _ranks(vector_ids), _ranks(text_ids)
    scores = dict(merged)
    ordered_ids = [chunk_id for chunk_id, _ in merged]

    rows = db.execute(
        _HYDRATE_SQL, {"org_id": str(org_id), "ids": ordered_ids}
    ).all()
    by_id = {row[0]: row for row in rows}

    candidates = []
    for chunk_id in ordered_ids:
        row = by_id.get(chunk_id)
        if row is None:
            # Deleted between the search and the hydrate. Dropping it is
            # correct: citing a chunk that no longer exists is worse than
            # returning one fewer candidate.
            continue
        candidates.append(
            Candidate(
                chunk_id=row[0],
                parent_id=row[1],
                document_id=row[2],
                filename=row[3],
                page_number=row[4],
                content=row[5],
                token_count=row[6],
                rrf_score=scores[chunk_id],
                vector_rank=vector_rank.get(chunk_id),
                text_rank=text_rank.get(chunk_id),
                vector_similarity=vector_similarity.get(chunk_id),
            )
        )
    return candidates
