"""STEP 7: the one live run.

Everything in the test suite injects a fake client. That is the right
default -- a suite that calls a paid API is slow, flaky, and quietly
expensive -- but it means the provider code has been verified against
our BELIEF about the wire format, not against the format. This script is
the other half: the smallest set of real calls that can tell us the
belief was right.

Run it deliberately, never from pytest:

    venv/Scripts/python.exe scripts/smoke_live.py            # all legs
    venv/Scripts/python.exe scripts/smoke_live.py --leg embed

Budget. Gemini's free tier counts REQUESTS per minute, and a Cohere
trial key allows 10 rerank req/min and 1000 calls a month. Every leg
below is therefore one request, or two where the whole point is
comparing two responses. Nothing here loops.

It never prints an API key. Keys are reported as "present (NN chars)"
and the Gemini provider's __repr__ is already written to redact.
"""
import argparse
import asyncio
import math
import os
import sys

# Import the app the way the app imports itself.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

results: list[tuple[str, str, str]] = []


def record(leg: str, status: str, detail: str) -> None:
    results.append((leg, status, detail))
    marker = {PASS: "[ok]  ", FAIL: "[FAIL]", SKIP: "[skip]"}[status]
    print(f"{marker} {leg}: {detail}", flush=True)


def _key_report(name: str, value: str | None) -> str:
    # Length only. A smoke test that prints a key into a terminal buffer
    # (and a scrollback, and a screenshot) has leaked it.
    if not value:
        return f"{name} absent"
    return f"{name} present ({len(value)} chars)"


# ---- leg 1: configuration -------------------------------------------


def leg_config() -> bool:
    from app.core.config import settings

    print("  " + _key_report("GEMINI_API_KEY", settings.GEMINI_API_KEY))
    print("  " + _key_report("RERANKER_API_KEY", settings.RERANKER_API_KEY))
    print(
        f"  providers: embedding={settings.EMBEDDING_PROVIDER} "
        f"llm={settings.LLM_PROVIDER} reranker={settings.RERANKER_PROVIDER}"
    )
    print(
        f"  models: {settings.EMBEDDING_MODEL} / {settings.LLM_MODEL} "
        f"/ {settings.RERANKER_MODEL}"
    )

    if not settings.GEMINI_API_KEY:
        record("config", FAIL, "GEMINI_API_KEY is required for a live run")
        return False
    record("config", PASS, "keys and provider settings readable")
    return True


# ---- leg 2: which models this key can actually reach ----------------


def leg_models() -> bool:
    """One metadata request. Cheap insurance against a wrong model name.

    models.list costs no generation quota and answers the question that
    otherwise gets answered by a 404 halfway through a live run: does
    THIS key, on THIS project, see the models we have configured? Model
    availability on Gemini is per-project -- the 2.5 line is restricted
    for new projects -- so a name that is correct in the docs can still
    be wrong here.

    Note it does NOT check for `streamGenerateContent` in
    supportedGenerationMethods. Measured: gemini-3.5-flash-lite reports
    only generateContent/countTokens/createCachedContent/
    batchGenerateContent, and yet `:streamGenerateContent?alt=sse`
    streams correctly. The metadata is incomplete, so treating it as
    authoritative would reject a model that works.
    """
    from app.core.config import settings
    from app.core.http import get_sync_client

    try:
        response = get_sync_client().get(
            f"{settings.GEMINI_BASE_URL}/models",
            # Header auth, never ?key= -- a query parameter lands in
            # every proxy and access log on the way.
            headers={"x-goog-api-key": settings.GEMINI_API_KEY},
            params={"pageSize": 200},
            timeout=30.0,
        )
    except Exception as exc:
        record("models", FAIL, f"{type(exc).__name__}: {exc}")
        return False

    if response.status_code != 200:
        record("models", FAIL, f"HTTP {response.status_code} listing models")
        return False

    names = {
        m["name"].removeprefix("models/") for m in response.json().get("models", [])
    }
    missing = [
        name
        for name in (
            settings.EMBEDDING_MODEL,
            settings.LLM_MODEL,
            settings.REWRITE_MODEL,
        )
        if name not in names
    ]
    if missing:
        record("models", FAIL, f"not available to this key: {', '.join(missing)}")
        return False

    record(
        "models",
        PASS,
        f"{len(names)} models visible; embedding/llm/rewrite all present",
    )
    return True


# ---- leg 3: embeddings ----------------------------------------------


def leg_embed() -> bool:
    """Two requests: one DOCUMENT, one QUERY.

    Two rather than one because the thing most worth confirming is the
    part a fake cannot confirm -- that `taskType` is accepted AND
    changes the vector. If Gemini ignored it, document and query
    embeddings of the same text would be identical, our asymmetric
    retrieval would be a no-op, and nothing would ever error.
    """
    from app.core.config import settings
    from app.services.embedding import EmbeddingTask, GeminiEmbeddings

    embedder = GeminiEmbeddings(
        api_key=settings.GEMINI_API_KEY,
        model=settings.EMBEDDING_MODEL,
        dimensions=settings.EMBEDDING_DIMENSIONS,
        base_url=settings.GEMINI_BASE_URL,
        timeout=settings.EMBED_TIMEOUT_SECONDS,
        requests_per_minute=settings.EMBED_REQUESTS_PER_MINUTE,
    )

    text = "Expense reports must be submitted within thirty days of travel."
    try:
        [doc_vector] = embedder.embed([text], task=EmbeddingTask.DOCUMENT)
        [query_vector] = embedder.embed([text], task=EmbeddingTask.QUERY)
    except Exception as exc:
        record("embed", FAIL, f"{type(exc).__name__}: {exc}")
        return False

    if len(doc_vector) != settings.EMBEDDING_DIMENSIONS:
        record(
            "embed",
            FAIL,
            f"got {len(doc_vector)} dims, column is "
            f"{settings.EMBEDDING_DIMENSIONS}; storing this would fail",
        )
        return False

    norm = math.sqrt(sum(v * v for v in doc_vector))
    if abs(norm - 1.0) > 1e-3:
        # We normalize ourselves, because gemini-embedding-001 only
        # normalizes its full 3072-d output and Matryoshka-truncates
        # below that.
        record("embed", FAIL, f"vector is not unit length (norm {norm:.6f})")
        return False

    cosine = sum(a * b for a, b in zip(doc_vector, query_vector))
    if cosine > 0.9999:
        record(
            "embed",
            FAIL,
            f"DOCUMENT and QUERY vectors are identical (cos {cosine:.6f}); "
            "taskType is being ignored and asymmetric retrieval is a no-op",
        )
        return False

    record(
        "embed",
        PASS,
        f"{settings.EMBEDDING_DIMENSIONS} dims, unit norm, "
        f"taskType honoured (doc/query cos {cosine:.4f})",
    )
    return True


# ---- leg 4: streaming answer ----------------------------------------


async def _stream_answer():
    from app.core.config import settings
    from app.services.llm import GeminiLLM
    from app.services.prompting import build_prompt
    from app.services.reranker import RetrievedParent
    import uuid

    # A real prompt from the real builder, not a hand-written one: the
    # delimiters, the numbered blocks and the grounding rules are the
    # thing under test, and a bespoke prompt here would test nothing
    # that ships.
    parent = RetrievedParent(
        parent_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="expenses-policy.pdf",
        page_number=4,
        content=(
            "Expense reports must be submitted within thirty days of the end "
            "of travel. Reports submitted after this window require written "
            "approval from a department director."
        ),
        token_count=38,
        best_rank=1,
        best_score=0.91,
        matched_chunk_id=uuid.uuid4(),
    )
    prompt = build_prompt("How long do I have to submit an expense report?", [parent])

    llm = GeminiLLM(
        api_key=settings.GEMINI_API_KEY,
        model=settings.LLM_MODEL,
        base_url=settings.GEMINI_BASE_URL,
        timeout=settings.LLM_TIMEOUT_SECONDS,
    )

    chunks: list[str] = []
    async for piece in llm.stream(system=prompt.system, user=prompt.user):
        chunks.append(piece)
    return chunks


def leg_answer() -> bool:
    from app.core.http import aclose_async_client

    async def run():
        try:
            return await _stream_answer(), None
        except Exception as exc:
            return None, exc
        finally:
            await aclose_async_client()

    chunks, exc = asyncio.run(run())
    if exc is not None:
        record("answer", FAIL, f"{type(exc).__name__}: {exc}")
        return False

    answer = "".join(chunks)
    if not answer.strip():
        record("answer", FAIL, "stream finished with no text")
        return False

    print(f"  answer: {answer.strip()[:300]}")

    if len(chunks) < 2:
        # One chunk means alt=sse did not take effect and the whole
        # response arrived as a single buffered blob -- "streaming" that
        # delivers nothing until the answer is complete.
        record(
            "answer",
            FAIL,
            f"only {len(chunks)} chunk(s); alt=sse is not streaming incrementally",
        )
        return False

    if "[1]" not in answer:
        record(
            "answer",
            FAIL,
            "answer carries no [1] marker, so no citation can be resolved",
        )
        return False

    record("answer", PASS, f"{len(chunks)} chunks streamed, cites [1]")
    return True


# ---- leg 5: reranker -------------------------------------------------


async def _rerank_once():
    import uuid

    from app.core.config import settings
    from app.core.http import aclose_async_client
    from app.services.reranker import HostedReranker
    from app.services.retrieval import Candidate

    def candidate(content: str, rrf: float) -> Candidate:
        return Candidate(
            chunk_id=uuid.uuid4(),
            parent_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            filename="expenses-policy.pdf",
            page_number=1,
            content=content,
            token_count=len(content.split()),
            rrf_score=rrf,
            vector_rank=1,
            text_rank=None,
        )

    # Deliberately ordered WRONG on the way in: the answer-bearing chunk
    # is last. A reranker that works moves it to rank 1, which is a
    # result a passthrough cannot produce by accident.
    candidates = [
        candidate("The office kitchen is restocked on Mondays.", 0.016),
        candidate("Parking permits are issued by facilities.", 0.015),
        candidate(
            "Expense reports must be submitted within thirty days of travel.",
            0.014,
        ),
    ]

    reranker = HostedReranker(
        api_key=settings.RERANKER_API_KEY,
        model=settings.RERANKER_MODEL,
        base_url=settings.RERANKER_BASE_URL,
        timeout=settings.RERANKER_TIMEOUT_SECONDS,
    )
    try:
        ranked = await reranker.rerank(
            "How long do I have to submit an expense report?", candidates, 3
        )
    finally:
        await aclose_async_client()
    return ranked


def leg_rerank_live() -> bool:
    from app.core.config import settings

    if not settings.RERANKER_API_KEY:
        record("rerank", SKIP, "no RERANKER_API_KEY set; passthrough is in use")
        return True

    async def run():
        try:
            return await _rerank_once(), None
        except Exception as exc:
            return None, exc

    ranked, exc = asyncio.run(run())
    if exc is not None:
        record("rerank", FAIL, f"{type(exc).__name__}: {exc}")
        return False

    if not ranked:
        record("rerank", FAIL, "reranker returned nothing")
        return False

    top = ranked[0].candidate.content
    print(f"  top: {top[:80]} (score {ranked[0].score:.4f})")
    if "thirty days" not in top:
        record(
            "rerank",
            FAIL,
            "the answer-bearing chunk did not reach rank 1; "
            "index mapping or the model is wrong",
        )
        return False

    record(
        "rerank",
        PASS,
        f"reordered to the answer-bearing chunk, score {ranked[0].score:.4f}",
    )
    return True


# ---- runner ----------------------------------------------------------

LEGS = {
    "config": leg_config,
    "models": leg_models,
    "embed": leg_embed,
    "answer": leg_answer,
    "rerank": leg_rerank_live,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--leg",
        action="append",
        choices=sorted(LEGS),
        help="Run only these legs (repeatable). Default: all, in order.",
    )
    args = parser.parse_args()

    # Live by default -- but .env ships EMBEDDING_PROVIDER=fake, and this
    # script constructs the Gemini providers directly rather than through
    # get_embedding_provider(), so a fake default cannot silently turn
    # "the live smoke test passed" into "the fakes still work".
    chosen = args.leg or ["config", "models", "embed", "answer", "rerank"]
    print(f"live smoke test: {', '.join(chosen)}\n")

    if "config" not in chosen:
        chosen = ["config"] + chosen

    ok = True
    for name in chosen:
        if not LEGS[name]():
            ok = False
            if name == "config":
                break

    print("\n--- summary ---")
    for leg, status, detail in results:
        print(f"{status:4}  {leg:8} {detail}")
    print("---------------")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
